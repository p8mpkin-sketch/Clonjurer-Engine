"""
V2 批量任务调度器(纯线程模型)。

- N 个守护工作线程(N == 并发 Context 槽位), 每个线程通过线程本地
  SyncBrowserPool 持有独立 Chromium 实例, 自动消费 pending 任务。
- 每个任务由 ClonerEngine(同步版) 全流程执行, 异常自动落库为 failed。
- 退出时尽力关闭全部线程本地浏览器实例。
"""
import threading

import config
from core.browser_pool import SyncBrowserPool
from core.cloner_engine import ClonerEngine
from core.uir import generate as uir_generate


class Scheduler:
    def __init__(self, db, bus, pool: SyncBrowserPool = None):
        self.db = db
        self.bus = bus
        self.pool = pool or SyncBrowserPool.instance(config.MAX_CONCURRENT_CONTEXTS)
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._threads = []
        self._retried = set()

    # ------------------------------------------------------------------
    def start(self):
        if any(t.is_alive() for t in self._threads):
            return
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._worker, args=(i,),
                             name=f"clone-worker-{i}", daemon=True)
            for i in range(self.pool.max)
        ]
        for thread in self._threads:
            thread.start()
        self.bus.publish(None, "SCHED", f"调度器已启动: {self.pool.max} 个工作线程就绪")

    def pause(self):
        """暂停队列消费: 已领取的任务继续执行, 未领取的留在队列。"""
        self._paused.set()
        self.bus.publish(None, "SCHED", "调度器已暂停: 队列任务暂不领取")

    def resume(self):
        self._paused.clear()
        self.bus.publish(None, "SCHED", "调度器已恢复: 继续消费队列")

    def paused(self) -> bool:
        return self._paused.is_set()

    def _worker(self, index: int):
        while not self._stop.is_set():
            while self._paused.is_set() and not self._stop.is_set():
                self._stop.wait(0.5)
            if self._stop.is_set():
                return
            task = None
            try:
                task = self.db.claim_next_task()
            except Exception as exc:
                self.bus.publish(None, "ERROR", f"领取任务失败: {exc}", "ERROR")
            if task is None:
                self._stop.wait(1.0)
                continue
            self.bus.publish(task["id"], "SCHED",
                          f"Worker-{index} 接手任务 #{task['id']} ({task['domain']})")
            try:
                if uir_generate.is_generate_task(task):
                    # 通用 UI 重建引擎(L1 生成任务): 按 target_url 前缀分流, 不进克隆管线
                    uir_generate.run_generation_task(task, self.db, self.bus, self.pool)
                else:
                    engine = ClonerEngine(task["id"], self.db, self.bus, self.pool)
                    engine.run()
            except Exception as exc:
                # 瞬时 Playwright 会话崩溃(页面运行时重导航/浏览器回收)与
                # 稳定失败不同源: 换新会话整体重跑一次, 避免偶发即判死
                if self._is_transient(exc) and task["id"] not in self._retried:
                    self._retried.add(task["id"])
                    self.bus.publish(task["id"], "SCHED",
                                     f"任务 #{task['id']} 遇瞬时会话崩溃, 自动重试一次")
                    self.db.update_task(task["id"], status="pending", stage="重试排队",
                                        progress=0, error_msg="")
                    continue
                self._retried.discard(task["id"])
                self.bus.publish(task["id"], "ERROR", f"引擎异常终止: {type(exc).__name__}: {exc}", "ERROR")
                self.db.update_task(task["id"], status="failed",
                                    error_msg=f"{type(exc).__name__}: {exc}"[:1500])

    _TRANSIENT_MARKS = (
        "Execution context was destroyed",
        "Target closed",
        "Target page, context or browser has been closed",
        "Browser has been closed",
        "Session closed",
        "net::ERR_ABORTED",
        "net::ERR_CONNECTION_RESET",
        "net::ERR_CONNECTION_CLOSED",
        "net::ERR_TIMED_OUT",
        "net::ERR_EMPTY_RESPONSE",
        "Navigation failed because page was aborted",
    )

    @classmethod
    def _is_transient(cls, exc: Exception) -> bool:
        text = f"{type(exc).__name__}: {exc}"
        return any(m in text for m in cls._TRANSIENT_MARKS)

    # ------------------------------------------------------------------
    def shutdown(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=8)
        self.pool.shutdown_all()
        self.bus.publish(None, "SCHED", "调度器已停止")
