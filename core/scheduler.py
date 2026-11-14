"""
V2 批量任务调度器(纯线程模型)。

- N 个守护工作线程(N == 并发 Context 槽位), 每个线程通过线程本地
  SyncBrowserPool 持有独立 Chromium 实例, 自动消费 pending 任务。
- 每个任务由 ClonerEngine(同步版) 全流程执行, 异常自动落库为 failed。
- 退出时尽力关闭全部线程本地浏览器实例。
"""
import json
import shutil
import threading
import time
from pathlib import Path

import config
from core import live
from core.browser_pool import SyncBrowserPool
from core.cloner_engine import ClonerEngine
from core.uir import generate as uir_generate


def with_suppress_publish(bus, task_id, kind, msg):
    """日志总线发布本身失败(库/磁盘写不进)不能再抛, 否则又把调用方带走。"""
    try:
        bus.publish(task_id, kind, msg, kind if kind in ("ERROR", "WARN") else None)
    except Exception:   # noqa: BLE001
        pass


def _plan_concurrency(plan: str):
    spec = config.PLANS.get(plan) or {}
    return spec.get("concurrent")


class Scheduler:
    def __init__(self, db, bus, pool: SyncBrowserPool = None):
        self.db = db
        self.bus = bus
        self.pool = pool or SyncBrowserPool.instance(config.MAX_CONCURRENT_CONTEXTS)
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._threads = []
        self._retried = set()
        self._active: dict = {}          # 本进程正在执行的任务 id → 工作线程序号
        self._disk_low = False
        self._recovered = False

    # ------------------------------------------------------------------
    def start(self):
        if any(t.is_alive() for t in self._threads):
            return
        self._stop.clear()
        if not self._recovered:
            # 上次进程留下的 running 任务(崩溃/被杀/休眠断电): 本进程还没领过任何任务, 它们不可能还活着
            self._recovered = True
            try:
                ids = self.db.recover_orphan_tasks()
                if ids:
                    self.bus.publish(None, "SCHED", f"启动收回上次中断的 {len(ids)} 个任务(置为失败, 可重建): "
                                     + ", ".join(f"#{i}" for i in ids[:20]), "WARN")
            except Exception as exc:
                self.bus.publish(None, "ERROR", f"收回中断任务失败: {exc}", "ERROR")
        self._threads = [
            threading.Thread(target=self._worker, args=(i,),
                             name=f"clone-worker-{i}", daemon=True)
            for i in range(self.pool.max)
        ]
        self._threads.append(threading.Thread(target=self._watchdog, name="clone-watchdog", daemon=True))
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
        # 工作线程绝不能因异常退出: 以前「落库为 failed」这一步自己抛错(磁盘写满时 sqlite 报 database or disk is full)
        # 就把线程带走, 任务永远停在 running, 前端重建也被 409 拒绝
        while not self._stop.is_set():
            try:
                self._worker_step(index)
            except Exception as exc:   # noqa: BLE001
                with_suppress_publish(self.bus, None, "ERROR", f"Worker-{index} 循环异常(已恢复): {type(exc).__name__}: {exc}")
                self._stop.wait(2.0)

    def _disk_ok(self) -> bool:
        """工作区所在卷剩余空间 ≥ MIN_FREE_DISK_MB。不足时不领新任务(写到一半写满, 任务半成品、库也写不进)。"""
        need = int(getattr(config, "MIN_FREE_DISK_MB", 0) or 0)
        if need <= 0:
            return True
        try:
            free_mb = shutil.disk_usage(str(config.WORKSPACE_ROOT)).free // (1024 * 1024)
        except OSError:
            return True
        low = free_mb < need
        if low != self._disk_low:
            self._disk_low = low
            if low:
                with_suppress_publish(self.bus, None, "WARN",
                                      f"磁盘剩余 {free_mb}MB 低于 {need}MB: 暂停领取新任务(清理空间后自动恢复)")
            else:
                with_suppress_publish(self.bus, None, "SCHED", f"磁盘剩余 {free_mb}MB, 恢复领取任务")
        return not low

    def _worker_step(self, index: int):
        while self._paused.is_set() and not self._stop.is_set():
            self._stop.wait(0.5)
        if self._stop.is_set():
            return
        if not self._disk_ok():
            self._stop.wait(15.0)
            return
        task = None
        try:
            task = self.db.claim_next_task(
                concurrency=_plan_concurrency)
        except Exception as exc:
            self.bus.publish(None, "ERROR", f"领取任务失败: {exc}", "ERROR")
        if task is None:
            self._stop.wait(1.0)
            return
        tid = task["id"]
        self._active[tid] = index
        live.begin(tid, index)
        try:
            self._run_task(index, task)
        finally:
            self._active.pop(tid, None)
            live.end(tid)

    def _run_task(self, index: int, task: dict):
        self.bus.publish(task["id"], "SCHED",
                         f"Worker-{index} 接手任务 #{task['id']} ({task['domain']})")
        try:
            if uir_generate.is_generate_task(task):
                # 通用 UI 重建引擎(L1 生成任务): 按 target_url 前缀分流, 不进克隆管线
                uir_generate.run_generation_task(task, self.db, self.bus, self.pool)
            else:
                engine = ClonerEngine(task["id"], self.db, self.bus, self.pool)
                engine.run()
            self._package_if_requested(task)
            self._record_usage(task["id"])
        except Exception as exc:
            # 瞬时 Playwright 会话崩溃(页面运行时重导航/浏览器回收)与
            # 稳定失败不同源: 换新会话整体重跑一次, 避免偶发即判死
            if self._is_transient(exc) and task["id"] not in self._retried:
                self._retried.add(task["id"])
                self.bus.publish(task["id"], "SCHED",
                                 f"任务 #{task['id']} 遇瞬时会话崩溃, 自动重试一次")
                self._save(task["id"], status="pending", stage="重试排队", progress=0, error_msg="")
                return
            self._retried.discard(task["id"])
            msg = f"{type(exc).__name__}: {exc}"
            if self._is_disk_full(exc):
                msg = "磁盘空间不足(清理后可重建): " + msg
            with_suppress_publish(self.bus, task["id"], "ERROR", f"引擎异常终止: {msg}")
            self._save(task["id"], status="failed", error_msg=msg[:1500])

    def _record_usage(self, task_id: int):
        """克隆成功记 1 次用量(失败不计), 配额与账单都从 usage_events 流水算。"""
        try:
            cur = self.db.get_task(task_id) or {}
            if cur.get("status") == "completed" and cur.get("workspace_id"):
                self.db.record_usage(cur["workspace_id"], "clone", 1, task_id)
        except Exception:  # noqa: BLE001 - 计量失败不影响任务结果
            pass

    def _save(self, task_id: int, **fields):
        """任务状态落库; 写库失败(磁盘满/库锁)退避重试, 仍失败只记日志, 由看门狗兜底收回。"""
        for i in range(4):
            try:
                self.db.update_task(task_id, **fields)
                return True
            except Exception as exc:   # noqa: BLE001
                if i == 3:
                    with_suppress_publish(self.bus, task_id, "ERROR", f"任务 #{task_id} 状态落库失败: {exc}")
                    return False
                time.sleep(0.5 * (2 ** i))
        return False

    @staticmethod
    def _is_disk_full(exc: BaseException) -> bool:
        text = f"{type(exc).__name__}: {exc}".lower()
        return getattr(exc, "errno", None) == 28 or "no space left" in text or "disk is full" in text \
            or "database or disk is full" in text

    def _watchdog(self):
        """心跳停了的 running 任务判失败: 不在本进程执行名单里的(工作线程已死)停 TASK_ORPHAN_S 即收回;
        在执行的停 TASK_STALE_S(整段卡死, 引擎各长阶段都有心跳)。"""
        while not self._stop.wait(60.0):
            try:
                self._watch_once()
            except Exception:   # noqa: BLE001
                pass

    def _watch_once(self) -> list:
        orphan_s = float(getattr(config, "TASK_ORPHAN_S", 120) or 0)
        stale_s = float(getattr(config, "TASK_STALE_S", 1800) or 0)
        if orphan_s <= 0 and stale_s <= 0:
            return []
        cand = set(self.db.stale_running_tasks(min(x for x in (orphan_s, stale_s) if x > 0)))
        long_stale = set(self.db.stale_running_tasks(stale_s)) if stale_s > 0 else set()
        out = []
        for tid in sorted(cand):
            active = tid in self._active
            if active and tid not in long_stale:
                continue
            if not active and orphan_s <= 0:
                continue
            why = (f"心跳停止超过 {int(stale_s // 60)} 分钟, 判定卡死" if active
                   else "执行线程已不在, 任务中断")
            if self._save(tid, status="failed", stage="中断", error_msg=f"{why}(可重建)"):
                with_suppress_publish(self.bus, tid, "WARN", f"看门狗: 任务 #{tid} {why}, 已置为失败")
                out.append(tid)
        return out

    def _package_if_requested(self, task: dict):
        """交付形态后置步骤: 批次配置 package=desktop|apk 时, 把已完成任务的网站工程装进应用壳构建安装包。
        在克隆/生成管线之外执行(不改 cloner_engine), 失败只记缺口, 不改变任务的完成状态与评分。"""
        try:
            batch = self.db.get_batch(task["batch_id"]) or {}
            kind = (json.loads(batch.get("config_json") or "{}") or {}).get("package") or ""
        except Exception:
            return
        if kind not in ("desktop", "apk"):
            return
        cur = self.db.get_task(task["id"]) or {}
        if cur.get("status") != "completed" or not cur.get("workspace_path"):
            return
        from core import app_shell
        tid = task["id"]
        label = {"desktop": "桌面应用", "apk": "Android 应用"}[kind]
        self.db.update_task(tid, stage=f"构建{label}")
        self.bus.publish(tid, "BUILD", f"开始构建{label}(交付形态)")
        try:
            stats = json.loads(cur.get("stats_json") or "{}")
        except Exception:
            stats = {}
        dom = cur.get("domain") or ""
        title = dom if dom and dom != "generated" else (batch.get("name") or f"task{tid}").replace("生成 · ", "")
        vp = config.VIEWPORTS.get(json.loads(batch.get("config_json") or "{}").get("viewport") or "", {})
        res = app_shell.package_task(tid, Path(cur["workspace_path"]), kind, title=title, viewport=vp,
                                     log=lambda m: self.bus.publish(tid, "BUILD", m))
        stats["package"] = res
        self.db.update_task(tid, stage="完成" if res.get("ok") else f"完成(构建{label}失败)",
                            stats_json=json.dumps(stats, ensure_ascii=False))

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
