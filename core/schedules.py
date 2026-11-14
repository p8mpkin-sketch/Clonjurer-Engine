"""定时克隆: 按 每小时 / 每天 / 每周 / 工作日 的本机时间自动把一组网址重新克隆一次(批次入队)。

时间按平台所在机器的本地时区理解(与用户看到的时钟一致), 下次运行时刻以 UTC 存库。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

FREQS = ("hourly", "daily", "weekdays", "weekly")
_FMT = "%Y-%m-%d %H:%M:%S"


def next_run(freq: str, hour: int, minute: int, weekday: int = 0,
             after: Optional[datetime] = None) -> str:
    """下次触发时刻(UTC 字符串)。weekday: 0=周一 … 6=周日。"""
    now_local = (after or datetime.now(timezone.utc)).astimezone()
    minute = max(0, min(59, int(minute)))
    hour = max(0, min(23, int(hour)))
    if freq == "hourly":
        cand = now_local.replace(minute=minute, second=0, microsecond=0)
        if cand <= now_local:
            cand += timedelta(hours=1)
    else:
        cand = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand <= now_local:
            cand += timedelta(days=1)
        for _ in range(8):
            wd = cand.weekday()
            if freq == "weekdays" and wd >= 5:
                cand += timedelta(days=1)
            elif freq == "weekly" and wd != int(weekday) % 7:
                cand += timedelta(days=1)
            else:
                break
    return cand.astimezone(timezone.utc).strftime(_FMT)


def describe(freq: str, hour: int, minute: int, weekday: int = 0) -> str:
    hm = f"{int(hour):02d}:{int(minute):02d}"
    if freq == "hourly":
        return f"每小时第 {int(minute)} 分"
    if freq == "weekdays":
        return f"工作日 {hm}"
    if freq == "weekly":
        return "每周" + "一二三四五六日"[int(weekday) % 7] + " " + hm
    return f"每天 {hm}"
