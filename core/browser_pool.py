"""
V2 常驻 Playwright Chromium 资源池(线程本地实例 + 全局并发信号量)。

设计要点:
- 使用 threading.local() 为每个工作线程维护独立的 sync_playwright 与 Chromium
  Browser 实例, 彻底杜绝多线程共用一个 playwright 对象时报
  "Cannot switch to a different thread" 的 Greenlet 冲突。
- 全局 threading.BoundedSemaphore(MAX_CONCURRENT_CONTEXTS) 限制平台并发
  BrowserContext 总数, 防止批量任务导致内存暴涨(OOM)。
- 崩溃自愈: 每次获取 Context 前校验 browser.is_connected(), 断连自动销毁旧
  实例并按退避重试重建; 运行中崩溃的异常由引擎层兜底, 下一次调用即自愈。
- 每个 Context 注入反爬特征隐藏脚本(抹除 navigator.webdriver 等)。
- active/max 计数供前端遥测导航栏实时展示。
"""
import threading
import time
from contextlib import contextmanager, suppress

STEALTH_JS = r"""
(() => {
  try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (e) {}
  try { window.chrome = window.chrome || { runtime: {}, loadTimes: () => ({}), csi: () => ({}) }; } catch (e) {}
  try { Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'zh-CN'] }); } catch (e) {}
  try {
    Object.defineProperty(navigator, 'plugins', {
      get: () => [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
        { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer' },
        { name: 'Native Client', filename: 'internal-nacl-plugin' },
      ],
    });
  } catch (e) {}
  try {
    const originalQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
    window.navigator.permissions.query = (parameters) =>
      parameters && parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters);
  } catch (e) {}
  try { Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 }); } catch (e) {}
})();
"""

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-notifications",
    "--mute-audio",
]

RESTART_ATTEMPTS = 3
RESTART_BACKOFF_SECONDS = 1.5


class SyncBrowserPool:
    """全平台唯一的线程本地 Chromium 资源池。"""

    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls, max_contexts: int = 4) -> "SyncBrowserPool":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(max_contexts)
            return cls._instance

    def __init__(self, max_contexts: int = 4):
        self.max = max_contexts
        self.active = 0
        self._local = threading.local()
        self._semaphore = threading.BoundedSemaphore(max_contexts)
        self._active_lock = threading.Lock()
        self._registry_lock = threading.Lock()
        self._registry = []  # [(playwright, browser), ...] 供全局关闭
        self._restart_pending = False

    # ------------------------------------------------------------------
    def request_restart(self):
        """线程安全的实例池重启请求: 每个工作线程在下次获取 Context 时
        自行销毁并重建本线程实例(Playwright 对象不可跨线程操作)。"""
        with self._registry_lock:
            self._restart_pending = True

    def _register(self, playwright, browser):
        with self._registry_lock:
            self._registry.append((playwright, browser))

    def _unregister(self, browser):
        with self._registry_lock:
            self._registry = [
                pair for pair in self._registry if pair[1] is not browser
            ]

    def _teardown_thread_browser(self):
        """销毁当前线程的 playwright/browser(若存在)。"""
        playwright = getattr(self._local, "playwright", None)
        browser = getattr(self._local, "browser", None)
        if browser is not None:
            with suppress(Exception):
                browser.close()
            self._unregister(browser)
        if playwright is not None:
            with suppress(Exception):
                playwright.stop()
        self._local.playwright = None
        self._local.browser = None

    def _ensure_browser(self):
        """确保当前线程持有存活的 Chromium(断连自动重建, 带退避重试)。"""
        if getattr(self, "_restart_pending", False):
            self._teardown_thread_browser()
            with self._registry_lock:
                if not self._registry:  # 全部线程重建完毕后清除标志
                    self._restart_pending = False
        browser = getattr(self._local, "browser", None)
        if browser is not None and browser.is_connected():
            return
        last_error = None
        for attempt in range(1, RESTART_ATTEMPTS + 1):
            self._teardown_thread_browser()
            playwright = None
            try:
                from playwright.sync_api import sync_playwright

                playwright = sync_playwright().start()
                browser = playwright.chromium.launch(
                    headless=True, args=CHROMIUM_ARGS
                )
                self._local.playwright = playwright
                self._local.browser = browser
                self._register(playwright, browser)
                return
            except Exception as exc:  # 启动失败: 退避后重试
                last_error = exc
                # launch 失败时 playwright 还没挂到线程上, 不停掉它的事件循环,
                # 下一次 sync_playwright().start() 会报 "Sync API inside the asyncio loop"
                if playwright is not None and getattr(self._local, "playwright", None) is not playwright:
                    with suppress(Exception):
                        playwright.stop()
                self._teardown_thread_browser()
                if attempt < RESTART_ATTEMPTS:
                    time.sleep(RESTART_BACKOFF_SECONDS * attempt)
        raise RuntimeError(
            f"Chromium 启动失败(已重试 {RESTART_ATTEMPTS} 次): {last_error}"
        )

    # ------------------------------------------------------------------
    @contextmanager
    def context(self, viewport=None, user_agent=None, extra_headers=None, **device):
        """受全局信号量保护的 BrowserContext 生命周期管理(线程安全)。
        device: 设备仿真参数(is_mobile / has_touch / device_scale_factor), 原样交给 new_context。"""
        with self._semaphore:
            with self._active_lock:
                self.active += 1
            ctx = None
            try:
                self._ensure_browser()
                launched = False
                for attempt in range(2):  # 首次崩溃自愈重试一轮
                    try:
                        ctx = self._local.browser.new_context(
                            viewport=viewport or {"width": 1920, "height": 1080},
                            user_agent=user_agent,
                            extra_http_headers=extra_headers or {},
                            ignore_https_errors=True,
                            **device,
                        )
                        launched = True
                        break
                    except Exception:
                        if attempt == 0:
                            self._teardown_thread_browser()
                            self._ensure_browser()
                            continue
                        raise
                if not launched:
                    raise RuntimeError("BrowserContext 创建失败")
                ctx.add_init_script(STEALTH_JS)
                yield ctx
            finally:
                if ctx is not None:
                    with suppress(Exception):
                        ctx.close()
                with self._active_lock:
                    self.active -= 1

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        with self._registry_lock:
            browsers = list(self._registry)
        alive = sum(1 for _pw, browser in browsers if browser.is_connected())
        return {
            "active": self.active,
            "max": self.max,
            "alive": alive > 0,
            "instances": len(browsers),
        }

    def shutdown_all(self):
        """尽力关闭全部线程实例(进程退出时调用)。"""
        with self._registry_lock:
            pairs = list(self._registry)
            self._registry = []
        for playwright, browser in pairs:
            with suppress(Exception):
                browser.close()
            with suppress(Exception):
                playwright.stop()
