"""运行态实时快照: 谁在跑什么、每个阶段花了多久、预计还要多久、本机资源占用。

只在内存里, 不落库。调度器领到任务时 begin(), 结束时 end(); 克隆引擎把自己的阶段时间线
(_timeline: [(阶段名, monotonic 时刻)]) 挂上来, 快照时按它算各阶段已用时间。
预计剩余 = 近期完成任务各阶段耗时的中位数, 减去当前阶段已用时间(通用统计, 不分站点)。
"""
from __future__ import annotations

import os
import shutil
import statistics
import subprocess
import threading
import time
from typing import Optional

_lock = threading.Lock()
_runs: dict = {}            # task_id -> {"worker": int, "started": float(wall), "timeline": list|None}
_proc_cache: tuple = (0.0, {})
_eta_cache: tuple = (0.0, {}, 0.0)


def begin(task_id: int, worker: int) -> None:
    with _lock:
        _runs[int(task_id)] = {"worker": int(worker), "started": time.time(), "timeline": None}


def attach_timeline(task_id: int, timeline: list) -> None:
    with _lock:
        run = _runs.get(int(task_id))
        if run is not None:
            run["timeline"] = timeline


def end(task_id: int) -> None:
    with _lock:
        _runs.pop(int(task_id), None)


def runs() -> dict:
    with _lock:
        return {tid: dict(r) for tid, r in _runs.items()}


def stage_trail(run: dict) -> list:
    """[(阶段, 已用秒, 是否进行中)], 按出现顺序合并同名阶段。"""
    tl = list(run.get("timeline") or [])
    if not tl:
        return []
    now_m = time.monotonic()
    out: list = []
    for i, (stage, t0) in enumerate(tl):
        t1 = tl[i + 1][1] if i + 1 < len(tl) else now_m
        if out and out[-1][0] == stage:
            out[-1][1] += t1 - t0
        else:
            out.append([stage, t1 - t0, False])
    out[-1][2] = True
    return [(s, round(d, 1), cur) for s, d, cur in out]


def eta_model(db, max_age_s: float = 20.0) -> tuple:
    """(各阶段耗时中位数, 整体耗时中位数), 来自最近 60 个完成任务的 stats_json.stage_times。"""
    global _eta_cache
    t, stages, total = _eta_cache
    if time.time() - t < max_age_s:
        return stages, total
    samples: dict = {}
    totals = []
    for st in db.recent_stage_times(60):
        for k, v in (st.get("stage_times") or {}).items():
            if isinstance(v, (int, float)):
                samples.setdefault(k, []).append(float(v))
        if isinstance(st.get("elapsed_s"), (int, float)):
            totals.append(float(st["elapsed_s"]))
    stages = {k: round(statistics.median(v), 1) for k, v in samples.items() if v}
    total = round(statistics.median(totals), 1) if totals else 0.0
    _eta_cache = (time.time(), stages, total)
    return stages, total


def eta_seconds(trail: list, elapsed: float, stages: dict, total: float) -> Optional[float]:
    if not total:
        return None
    if not trail:
        return max(0.0, total - elapsed)
    done = {s for s, _d, cur in trail if not cur}
    cur_stage, cur_d = trail[-1][0], trail[-1][1]
    rest = max(0.0, stages.get(cur_stage, 0.0) - cur_d)
    seen_cur = False
    for name in stages:                      # 中位数字典按首次出现顺序 ≈ 管线顺序
        if name == cur_stage:
            seen_cur = True
            continue
        if seen_cur and name not in done and name != "完成":
            rest += stages[name]
    # 兜底: 不比整体中位数剩余更乐观太多
    return round(max(rest, (total - elapsed) * 0.5, 0.0), 0)


def browser_processes(max_age_s: float = 3.0) -> dict:
    """本机 Chromium 进程数 / 常驻内存 MB / CPU%(ps, macOS 与 Linux 通用)。"""
    global _proc_cache
    t, cached = _proc_cache
    if time.time() - t < max_age_s:
        return cached
    out = {"count": 0, "rss_mb": 0.0, "cpu": 0.0}
    try:
        p = subprocess.run(["ps", "-A", "-o", "rss=,%cpu=,comm="], capture_output=True,
                           text=True, timeout=3)
        for line in p.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            name = parts[2].lower()
            if "chrom" in name or "headless_shell" in name:
                out["count"] += 1
                out["rss_mb"] += float(parts[0]) / 1024.0
                out["cpu"] += float(parts[1].replace(",", "."))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    out["rss_mb"] = round(out["rss_mb"], 0)
    out["cpu"] = round(out["cpu"], 1)
    _proc_cache = (time.time(), out)
    return out


def host(path) -> dict:
    cpus = os.cpu_count() or 1
    try:
        load = os.getloadavg()[0]
    except (OSError, AttributeError):
        load = 0.0
    try:
        du = shutil.disk_usage(str(path))
        disk = {"free_gb": round(du.free / 1e9, 1), "used_pct": round(du.used * 100 / du.total, 0)}
    except OSError:
        disk = {"free_gb": None, "used_pct": None}
    return {"cpus": cpus, "load": round(load, 2),
            "load_pct": round(min(100.0, load * 100 / cpus), 0), "disk": disk}
