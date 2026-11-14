"""
V3 多任务实时日志总线(结构化、无重复前缀、SSE 流式推送)。

结构化协议: 每条日志为
    {seq, time, stage, level, task_id, message}
  - stage : 管线阶段标签(RENDER/CSSOM/CAS-HIT/MOCK-API/BUILD/...), 前端胶囊渲染;
  - level : 严重级别(INFO/WARN/ERROR), 前端正文着色;
  - message: 纯正文, 自动剥离任何 "[STAGE]" 重复前缀, 彻底杜绝
    "[SIMILARITY] [SIMILARITY]" / "[INFO] [DONE]" 这类双重标签。

- 内存环形队列(有界, 防止 OOM)
- threading.Condition 实现发布/订阅, SSE 客户端按 seq 游标增量拉取
- 15s 无新日志时发送 ': ping' 心跳保持连接
"""
import json
import threading
from datetime import datetime, timezone


class LogBus:
    """进程内全局日志扇出总线。"""

    def __init__(self, capacity: int = 5000):
        self._capacity = capacity
        self._cond = threading.Condition()
        self._entries = []  # [{seq, time, stage, level, task_id, message}]
        self._seq = 0

    # ------------------------------------------------------------------
    @staticmethod
    def _strip_prefix(stage: str, message: str) -> str:
        """剥离 message 开头与 stage 重复的 [STAGE] 前缀(幂等, 至多两轮)。"""
        text = str(message or "").replace("\r", " ").replace("\n", " ").strip()
        for _round in range(2):
            probe = f"[{stage}]"
            if text[: len(probe)].upper() == probe.upper():
                text = text[len(probe):].strip()
            else:
                break
        return text[:2000]

    def publish(self, task_id, stage: str, message: str, level: str = None,
                workspace_id=None):
        """发布一条结构化日志。task_id 为 None 表示系统级消息;
        workspace_id 标明归属的工作区(多租户订阅按它过滤)。"""
        stage = str(stage or "INFO").upper()[:24]
        clean = self._strip_prefix(stage, message)
        if level is None:
            if stage == "ERROR":
                level = "ERROR"
            elif stage in ("WARN", "CRAWL"):
                level = "WARN"
            else:
                level = "INFO"
        level = str(level).upper()
        with self._cond:
            self._seq += 1
            self._entries.append({
                "seq": self._seq,
                "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "stage": stage,
                "level": level,
                "task_id": task_id,
                "workspace_id": workspace_id,
                "message": clean,
            })
            if len(self._entries) > self._capacity:
                del self._entries[: len(self._entries) - self._capacity]
            self._cond.notify_all()

    def info(self, task_id, message: str):
        self.publish(task_id, "INFO", message, "INFO")

    def warn(self, task_id, message: str):
        self.publish(task_id, "WARN", message, "WARN")

    def error(self, task_id, message: str):
        self.publish(task_id, "ERROR", message, "ERROR")

    # ------------------------------------------------------------------
    def stream(self, task_id=None, replay: int = 300, accept=None):
        """SSE 生成器: 过滤指定任务(系统级消息始终放行)。

        全任务流订阅时只回放最近 replay 条(前端也只保留 300 条),
        避免连上即灌入整个环形队列(默认 5000 条)把页面卡死。"""
        last_seq = 0
        with self._cond:
            if self._entries:
                if task_id is not None:
                    last_seq = self._entries[-1]["seq"]  # 订阅时刻之后的日志
                elif len(self._entries) > replay:
                    last_seq = self._entries[-replay - 1]["seq"]
        while True:
            with self._cond:
                fresh = [
                    e for e in self._entries
                    if e["seq"] > last_seq and self._match(e, task_id)
                ]
                if not fresh:
                    self._cond.wait(timeout=15.0)
                    fresh = [
                        e for e in self._entries
                        if e["seq"] > last_seq and self._match(e, task_id)
                    ]
            if accept is not None and fresh:
                last_seq = fresh[-1]["seq"]  # 被过滤掉的也要推进游标
                fresh = [e for e in fresh if accept(e)]
            if not fresh:
                yield ": ping\n\n"
                continue
            for entry in fresh:
                last_seq = entry["seq"]
                payload = json.dumps(entry, ensure_ascii=False)
                yield f"data: {payload}\n\n"

    @staticmethod
    def _match(entry, task_id) -> bool:
        if task_id is None:
            return True
        return entry["task_id"] is None or entry["task_id"] == task_id

    def snapshot(self, task_id=None, limit: int = 300):
        """最近日志快照(控制台初次加载)。"""
        with self._cond:
            entries = list(self._entries)
        picked = [e for e in entries if self._match(e, task_id)]
        return picked[-limit:]
