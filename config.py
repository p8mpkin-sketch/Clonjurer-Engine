"""
Enterprise Web Cloner & Flask App Generator - 全局配置中心.

集中管理路径、并发参数、视口预设、资源分类与各类上限值,
所有模块统一从此处导入配置, 保证全平台行为一致。
"""
import os
import pathlib

# ---------------------------------------------------------------------------
# 路径布局
# ---------------------------------------------------------------------------
BASE_DIR = pathlib.Path(__file__).resolve().parent
WORKSPACE_ROOT = BASE_DIR / "workspaces"       # 每个任务一个独立工作空间
CACHE_ROOT = BASE_DIR / "cache_cas"            # 全局 CAS(内容寻址)静态资源池
EXPORTS_DIR = BASE_DIR / "exports"             # 生成的 Flask 工程 ZIP 输出目录
DB_PATH = BASE_DIR / "cloner.db"               # SQLite 主数据库(WAL 模式)


def ensure_dirs():
    """创建运行期所需的全部目录(幂等)。"""
    for directory in (WORKSPACE_ROOT, CACHE_ROOT, EXPORTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


ensure_dirs()

# ---------------------------------------------------------------------------
# 并发与调度
# ---------------------------------------------------------------------------
MAX_CONCURRENT_CONTEXTS = 4        # 常驻 Chromium 最大并发 BrowserContext 槽位
DOWNLOAD_WORKERS = 16              # CAS 并发下载线程数
LOG_RING_SIZE = 5000               # SSE 日志环形队列容量

# ---------------------------------------------------------------------------
# 任务约束
# ---------------------------------------------------------------------------
MAX_URLS_PER_BATCH = 50            # 单批次最多提交站点数
MAX_PAGES_LIMIT = 20               # 同域名爬虫模式下最大子页面数
REQUEST_TIMEOUT = 20               # 单个上游资源请求超时(秒, 浏览器会话降级为总时长)
REQUEST_CONNECT_READ = (6, 10)     # requests 直连: (连接, 读空闲) 超时; 读超时按无数据间隔计, 不限大文件总时长
PAGELOAD_TIMEOUT = 45_000          # Playwright 页面导航超时(毫秒)
REACHABILITY_PROBE_TIMEOUT = 12_000  # 首导航失败后的网络层可达性探测上限(毫秒)
AUTO_SCROLL_SMOOTH = False       # 采集滚动改页内平滑连续滚动(400px/两帧): 防苹果类性能守卫把跳滚判成掉帧降级;
                                 # 机器高负载下页面加载期本身就会降级, 语料未回归前默认关(见 DESIGN 动态形态一节)
AUTO_SCROLL_BUDGET = 25          # 采集期自动滚动总时限(秒)
SHOT_TIMEOUT = 30_000             # 单次截图上限(毫秒), 超时走限高退化
NETWORK_IDLE_TIMEOUT = 9_000       # networkidle 等待上限(毫秒)
NAV_QUIET_MS = 500                # 采集导航静置: 这么久没有新请求
NAV_STALE_MS = 2_500              # 在途超过这么久的请求(长轮询/流式/卡死第三方)不再等
MAX_ASSET_BYTES = 64 * 1024 * 1024     # 单个静态资源体积上限(64MB)
BASELINE_IMG_WAIT_MS = 8000       # 基准截图前等待加载中可见图片的上限
CLONE_VIDEO_WAIT_MS = 1500        # 克隆截图前等首屏 autoplay 视频出帧的上限
CLONE_IMG_WAIT_MS = 2000          # 克隆截图前等懒加载图到齐的上限(资源都在本地, 通常百毫秒级)
BASELINE_IMG_FROZEN_WAIT_MS = 5000  # 时间冻结后补等可见图的上限(版面已静止)
BASELINE_TWEEN_SETTLE_MS = 800    # 时间冻结后让在飞的 JS 补间走完再停 rAF
SCORE_MASK_ADS = True             # 评分时两侧第三方广告位遮成中灰(竞价创意随机, 不计分)
BASELINE_EAGER_MAX = 40           # 基准截图前转 eager 的懒加载图上限(文档序)
BASELINE_IMG_STALL_MS = 4000      # 等待期间连续这么久无一张到齐即放行
HOST_FAIL_TRIP = 3                # 单主机网络层失败(SSL/超时/连接)达此次数即熔断
MAX_ASSETS_PER_TASK = 400          # 单任务最大静态资源条目数
CLONE_SCROLL_MAX_MS = 30000        # 克隆页懒加载滚动的完成时限(超时即判页面脚本卡死)
CLONE_RESPONSIVE_MS = 8000         # 克隆页主线程看门狗: 加载后这么久执行不了一行脚本即判卡死
RENDER_ASSET_RESERVE = 300         # 名额外预留给"浏览器实际加载过/克隆渲染实际请求"的资源:
                                   # 静态提取先把名额用满(苹果 @font-face 每字重 woff/woff2/ttf 三份,
                                   # 上百个从不加载的字体), 首屏视频这类真用到的反被挤掉
MAX_MOCK_ENTRIES = 300             # 单任务最大录制 API 条目数
MAX_MOCK_BODY_BYTES = 2 * 1024 * 1024  # 单个录制响应体上限(2MB)
MAX_TAPE_BODY_BYTES = 512 * 1024       # VCR 磁带单条响应体上限(512KB)
MAX_TAPE_TOTAL_BYTES = 16 * 1024 * 1024  # VCR 磁带总体积上限(16MB)
TAPE_RECORD = True                      # 采集期磁带录制开关(A/B 实验用)
PROXY_CACHE_MAX_BYTES = 8 * 1024 * 1024   # 反向代理兜底单响应缓存上限(8MB)

# ---------------------------------------------------------------------------
# 设备视口预设
# ---------------------------------------------------------------------------
VIEWPORTS = {
    "desktop": {"width": 1920, "height": 1080, "device_scale_factor": 1},
    "laptop": {"width": 1440, "height": 900, "device_scale_factor": 1},
    "mobile": {"width": 390, "height": 844, "device_scale_factor": 2},
}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# 静态资源分类映射(扩展名 -> 工作空间子目录)
# ---------------------------------------------------------------------------
EXT_CATEGORIES = {
    ".css": "css",
    ".js": "js", ".mjs": "js", ".cjs": "js",
    ".woff": "fonts", ".woff2": "fonts", ".ttf": "fonts",
    ".otf": "fonts", ".eot": "fonts",
    ".png": "img", ".jpg": "img", ".jpeg": "img", ".gif": "img",
    ".webp": "img", ".svg": "img", ".ico": "img", ".avif": "img", ".bmp": "img",
    ".mp4": "media", ".webm": "media", ".mov": "media",
    ".mp3": "media", ".wav": "media", ".ogg": "media", ".m4a": "media",
}

CONTENT_TYPE_FALLBACK = {
    "text/css": ".css",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "font/woff2": ".woff2",
    "font/woff": ".woff",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "image/x-icon": ".ico",
    "video/mp4": ".mp4",
    "audio/mpeg": ".mp3",
}

# 前端用于展示统计的四类资源桶
STAT_BUCKETS = ("css", "js", "img", "font", "api")

# ---------------------------------------------------------------------------
# V4 工程修复参数(Tier 0, 无 AI 参与)
# ---------------------------------------------------------------------------
MEDIA_SHOT_MAX = 24           # 单页最多从基准截图回填的空白 Canvas/无帧视频数
MAX_FRAME_PAGES = 6            # 单任务最多递归克隆的跨域 iframe 子页面数
MAX_FRAME_DEPTH = 2            # iframe 递归深度上限(主页面为第 0 层)
RENDER_REPAIR_ROUNDS = 6       # 视觉修复迭代轮数上限
REPAIR_MIN_GAIN = 0.5          # 修复循环收敛判定: 缺失集合不变且分数涨幅低于此值即停
FIDELITY_SKIP_ABOVE = 97.0    # 交互轨达到此分时不渲染保真轨(头条取优不受影响)
VIEWPORT_VARIANT_MAX = 40      # 窄屏 DOM 变体: 结构不同的子树超过此数视为内容随机(推荐流), 不注入
VIEWPORT_VARIANT_BYTES = 3_000_000   # 窄屏变体 HTML / 附带样式各自上限
STATIC_TRACK_MARGIN = 5.0     # 保真轨领先交互轨达此分差时, 试做"静态版"(剥离应用脚本)交付物
                              # 并实测; 静态版也领先交互轨此分差才设为默认预览/导出
BACKFILL_BUDGET = 20          # 单轮渲染缺失回补的回源时限(秒)
REPAIR_TIME_BUDGET = 300       # 修复循环时间预算(秒): 快站快修, 慢站限时收敛(P0-A 速度调度)
AI_TIME_BUDGET = 900           # 含 AI 修复的任务时间预算(秒): 超预算跳过 AI 轮       # 渲染驱动回补循环轮数(本地渲染->收集404->回源补抓);
                               # Vite/webpack 动态 chunk 是"entry->视图chunk->懒路由->
                               # 图片"多层依赖链, 每轮只能剥一层, 浅轮数补不到深层资源
LIVE_BODY_MAX = 1500          # 活体响应体复用: 单任务最多留存的浏览器已加载资源数
LIVE_BODY_BYTES = 256 * 1024 * 1024  # 活体响应体复用: 单任务留存总字节上限
LIVE_HARVEST_MAX = 60          # 活体会话请求差集回补的单轮最大补抓数
JS_REWRITE_MAX_BYTES = 8 * 1024 * 1024  # 参与本地化重写的单个 JS 文件体积上限

# ---------------------------------------------------------------------------
# V4 大模型(OpenAI 兼容)配置
#
# 优先级: 任务级(UI 提交的 ai_api_key/ai_model/ai_base_url)
#         > 环境变量(CLONER_AI_KEY / CLONER_AI_MODEL / CLONER_AI_BASE_URL)
#         > 此处默认值。
# 视觉闭环修复要求模型具备多模态(图像理解)能力; 未配置 Key 时相关功能
# 自动跳过, 不影响确定性克隆管线。
# ---------------------------------------------------------------------------
AI_API_BASE_URL = os.environ.get("CLONER_AI_BASE_URL", "https://api.deepseek.com/v1")
AI_API_KEY = os.environ.get("CLONER_AI_KEY", "")
AI_MODEL = os.environ.get("CLONER_AI_MODEL", "deepseek-v4.1")
AI_REQUEST_TIMEOUT = 90        # 单次大模型请求超时(秒)
AI_PATCH_MAX_TOKENS = 8192     # AI 补丁补全预算基档(推理模型的思考 token 计入, 耗尽时自动×2 重试)
AI_REPAIR_TRIGGER_SCORE = 85.0  # 视觉闭环触发门限: 诚实评分低于此值才调用大模型
AI_REPAIR_TARGET_SCORE = 90.0   # 修复循环的目标分: 达标即停
AI_REPAIR_MAX_ROUNDS = 2       # 视觉闭环最大迭代轮数
AI_VISION_MAX_IMAGE_EDGE = 1280  # 送入大模型前截图最长边压缩上限(像素)
AI_MOCK_MAX_FORMS = 10         # 智能 Mock 最多生成的表单路由数

# ---------------------------------------------------------------------------
# V4 仿真交互层(克隆页前端行为模拟)
# ---------------------------------------------------------------------------
INTERACTION_SIM = True             # 默认启用: 登录点击提示繁忙 / 验证码 60s 倒计时
SIM_LOGIN_FAIL_MESSAGES = (        # 登录类点击随机提示池
    "网络繁忙，请稍后重试",
    "系统繁忙，请稍后再试",
    "网络异常，请稍后重试",
)
SIM_SMS_SENT_MESSAGE = "验证码已发送，请注意查收"
SIM_SMS_COUNTDOWN_SECONDS = 60     # 验证码按钮倒计时秒数

# 主服务监听(可用环境变量覆盖, 便于同机并行起多个独立实例, 如 CLONER_PORT=8898)
HOST = os.environ.get("CLONER_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLONER_PORT", "8899"))

# 平台对外可达地址(导出工程内埋点引擎的上报回传地址;
# 留空则用导出时的宿主机局域网推断, 建议部署时显式配置)
PLATFORM_PUBLIC_URL = os.environ.get("CLONER_PUBLIC_URL", f"http://127.0.0.1:{PORT}")

# 审计日志保留天数(超过自动清理; 0 = 永久保留)
AUDIT_RETENTION_DAYS = int(os.environ.get("CLONER_AUDIT_RETENTION_DAYS", "90"))

