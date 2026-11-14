"""克隆站 Docker 部署: 极速启动 / 停止 / 重启 / 一键销毁 / 自动销毁 / 启动对账。

设计要点(见 UI 改版方案文档「部署」一节):
- 极速启动(mode=fast): 不构建镜像。一次性准备通用运行时镜像(python-slim + flask/requests/
  gunicorn/waitress), 部署时把任务的 flask_app 目录只读挂载进容器直接起, 秒级可访问。
- 构建镜像(mode=image): 用生成工程自带的 Dockerfile 打成独立镜像, 适合长期运行或搬机器。
- 只管自己的东西: 所有容器/镜像打 clonjurer.managed=1 与 clonjurer.task=<id> 标签,
  列表、清理、对账、销毁都只按标签/固定名操作, 不碰用户的其他容器。
- 销毁幂等: 容器不存在、镜像不存在都视为成功, 最后删登记、释放端口。
- 默认安全: 只绑 127.0.0.1; 内存/CPU/进程数上限; cap-drop ALL; no-new-privileges;
  根文件系统只读, 只有 /tmp 与代理缓存目录可写(tmpfs)。
- 调 docker CLI 一律参数列表、不经 shell, 用户输入只有整数任务号与白名单枚举。
"""
from __future__ import annotations

import builtins
import json
import os
import signal
import sys
import socket
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

RUNTIME_IMAGE = "clonjurer-runtime:1"
RUNTIME_BASE = "python:3.12-slim"
RUNTIME_DOCKERFILE = f"""FROM {RUNTIME_BASE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
RUN pip install --no-cache-dir "flask>=3.0.0" "requests>=2.31.0" "gunicorn>=21.2.0" "waitress>=3.0.0"
LABEL clonjurer.managed="1" clonjurer.runtime="1"
WORKDIR /app
EXPOSE 8000
"""
SITE_DOCKERFILE = f"""FROM {RUNTIME_IMAGE}
COPY . /app
"""
SITE_IMAGE_PREFIX = "clonjurer-site"
NAME_PREFIX = "clone_site_"
PORT_RANGE = (9100, 9699)
MODES = ("fast", "image", "process")   # process: 本机进程, 不需要 Docker
BINDS = ("127.0.0.1", "0.0.0.0")
MEM_LIMITS = ("256m", "512m", "1g", "2g")
# 自动销毁: 小时数; 0 = 不自动销毁
TTL_CHOICES = (0, 1, 24, 168)
DEFAULT_TTL_HOURS = 24
HEALTH_TIMEOUT_S = 20.0
GUNICORN_CMD = ["gunicorn", "-b", "0.0.0.0:8000", "-w", "2", "--threads", "4",
                "--access-logfile", "-", "app:app"]
_TIME_FMT = "%Y-%m-%d %H:%M:%S"


class DeployError(RuntimeError):
    """面向用户的部署错误(message 是可直接展示的中文)。"""


def _now() -> datetime:
    return datetime.now()


def _fmt(dt: datetime) -> str:
    return dt.strftime(_TIME_FMT)


def _parse(ts: str) -> Optional[datetime]:
    try:
        return datetime.strptime(ts, _TIME_FMT) if ts else None
    except ValueError:
        return None


def container_name(task_id: int) -> str:
    return f"{NAME_PREFIX}{int(task_id)}"


def site_image(task_id: int) -> str:
    return f"{SITE_IMAGE_PREFIX}:{int(task_id)}"


class DockerDeployer:
    """部署管理器。run 参数可注入(测试用假 docker)。"""

    def __init__(self, db, workspace_of: Callable[[dict], Path],
                 regenerate: Callable[[dict, Path], object],
                 get_task: Callable[[int], Optional[dict]],
                 run: Optional[Callable] = None,
                 port_free: Optional[Callable[[str, int], bool]] = None,
                 health: Optional[Callable[[int, float], bool]] = None,
                 spawn: Optional[Callable[[Path, str, int, Path], int]] = None,
                 alive: Optional[Callable[[int], bool]] = None,
                 kill: Optional[Callable[[int], None]] = None):
        self.db = db
        self.workspace_of = workspace_of
        self.regenerate = regenerate
        self.get_task = get_task
        self._run = run or self._subprocess_run
        self._port_free = port_free or _port_free
        self._health = health or _wait_health
        self._lock = threading.RLock()           # 端口分配与同任务操作串行
        self._avail = (0.0, False, "")           # (探测时间, 可用, 版本)
        self._reaper: Optional[threading.Thread] = None
        self._spawn = spawn or _spawn_process
        self._alive = alive or _pid_alive
        self._kill = kill or _kill_process
        self._steps: dict = {}                   # task_id -> 当前部署步骤(前端进度)
        self._warm = {"state": "idle", "error": ""}

    # ------------------------------------------------------------------ 本机进程模式
    @staticmethod
    def _pid_of(rec: Optional[dict]) -> int:
        cid = str((rec or {}).get("container_id") or "")
        return int(cid[4:]) if cid.startswith("pid:") and cid[4:].isdigit() else 0

    def _proc_live(self, rec: dict) -> dict:
        pid = self._pid_of(rec)
        up = bool(pid) and self._alive(pid)
        return {"state": "running" if up else "exited", "id": f"pid:{pid}" if pid else "",
                "image": "", "status_text": f"进程 {pid}" if up else ""}

    def _proc_stop(self, rec: Optional[dict]) -> None:
        pid = self._pid_of(rec)
        if pid and self._alive(pid):
            self._kill(pid)

    def _proc_log(self, task_id: int) -> Path:
        task = self.get_task(int(task_id)) or {"id": task_id}
        return Path(self.workspace_of(task)) / "flask_app" / "logs" / "deploy.log"

    def default_mode(self) -> str:
        return "fast" if self.status()["available"] else "process"

    def step(self, task_id: int) -> str:
        return self._steps.get(int(task_id), "")

    # ------------------------------------------------------------------ docker CLI
    @staticmethod
    def _subprocess_run(args, timeout=60, input_text=None):
        try:
            p = subprocess.run(["docker", *args], capture_output=True, text=True,
                               timeout=timeout, input=input_text)
            return p.returncode, p.stdout, p.stderr
        except FileNotFoundError:
            return 127, "", "docker 命令不存在"
        except subprocess.TimeoutExpired:
            return 124, "", "docker 命令超时"

    def _docker(self, *args, timeout=60, input_text=None, check=True):
        code, out, err = self._run(list(args), timeout=timeout, input_text=input_text)
        if check and code != 0:
            raise DeployError(_humanize(err or out))
        return code, out, err

    # ------------------------------------------------------------------ 状态
    def status(self, force: bool = False) -> dict:
        t, ok, ver = self._avail
        if force or time.time() - t > 5:
            code, out, _ = self._run(["version", "--format", "{{.Server.Version}}"], timeout=5)
            ok, ver = code == 0 and bool(out.strip()), out.strip() if code == 0 else ""
            self._avail = (time.time(), ok, ver)
        return {"available": ok, "version": ver,
                "hint": "" if ok else "没有检测到 Docker, 将以本机进程方式部署(同样秒级可访问)。"}

    def _require(self):
        if not self.status()["available"]:
            raise DeployError("没有检测到 Docker。请先安装并启动 Docker 后再部署。")

    def _inspect_managed(self) -> dict:
        """按标签列出平台管理的容器: {task_id: {state, id, image, started}}。"""
        code, out, _ = self._run(["ps", "-a", "--filter", "label=clonjurer.managed=1",
                                  "--format", "{{json .}}"], timeout=10)
        found = {}
        if code != 0:
            return found
        for line in out.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            name = row.get("Names", "")
            if not name.startswith(NAME_PREFIX):
                continue
            try:
                tid = int(name[len(NAME_PREFIX):])
            except ValueError:
                continue
            found[tid] = {"state": row.get("State", ""), "id": row.get("ID", ""),
                          "image": row.get("Image", ""), "status_text": row.get("Status", "")}
        return found

    def list(self) -> list:
        """登记 + 实际容器合并后的部署列表(供前端)。"""
        live = self._inspect_managed() if self.status()["available"] else {}
        rows = []
        seen = set()
        for rec in self.db.list_docker():
            tid = rec["task_id"]
            seen.add(tid)
            if rec.get("mode") == "process":
                rows.append(self._present(rec, self._proc_live(rec)))
            else:
                rows.append(self._present(rec, live.get(tid)))
        for tid, c in live.items():                # 有容器无登记: 未登记(可清理)
            if tid not in seen:
                rows.append(self._present({"task_id": tid, "port": 0, "image_tag": c["image"],
                                           "status": "orphan"}, c, orphan=True))
        return rows

    def _present(self, rec: dict, live: Optional[dict], orphan: bool = False) -> dict:
        if rec.get("status") == "starting" and rec["task_id"] in self._steps:
            live = {"state": "created", "id": "", "image": "", "status_text": ""}
        if live is None:
            st = "missing" if rec.get("status") not in ("failed",) else "failed"
        else:
            st = {"running": "running", "restarting": "starting", "created": "starting",
                  "paused": "stopped", "exited": "stopped", "dead": "failed"}.get(live["state"], "stopped")
            if rec.get("status") == "failed" and st != "running":
                st = "failed"
        bind = rec.get("bind") or "127.0.0.1"
        host = "127.0.0.1"
        port = rec.get("port") or 0
        exp = _parse(rec.get("expires_at", ""))
        return {
            "task_id": rec["task_id"], "domain": rec.get("domain") or "",
            "target_url": rec.get("target_url") or "",
            "status": st, "orphan": orphan,
            "container_id": (live or {}).get("id", rec.get("container_id", ""))[:12],
            "image_tag": rec.get("image_tag", ""), "mode": rec.get("mode", "fast"),
            "port": port, "bind": bind,
            "url": f"http://{host}:{port}" if port and st == "running" else "",
            "lan": bind == "0.0.0.0",
            "mem_limit": rec.get("mem_limit", "512m"),
            "expires_at": rec.get("expires_at", ""),
            "expires_in_s": int((exp - _now()).total_seconds()) if exp else None,
            "created_at": rec.get("created_at", ""), "updated_at": rec.get("updated_at", ""),
            "last_error": rec.get("last_error", ""),
            "status_text": (live or {}).get("status_text", ""),
            "step": self._steps.get(rec["task_id"], "") if rec.get("status") == "starting" else "",
        }

    def summary(self) -> dict:
        st = self.status()
        items = self.list()
        return {"available": st["available"], "version": st["version"], "hint": st["hint"],
                "containers": items, "total": len(items),
                "running": sum(1 for c in items if c["status"] == "running"),
                "stopped": sum(1 for c in items if c["status"] in ("stopped", "failed", "missing")),
                "ports": sum(1 for c in items if c["status"] == "running" and c["port"]),
                "port_range": list(PORT_RANGE), "default_mode": self.default_mode(),
                "runtime": dict(self._warm)}

    def stats(self) -> dict:
        res = self._proc_stats()
        if not self.status()["available"]:
            return res
        res.update(self._docker_stats())
        return res

    def _proc_stats(self) -> dict:
        pids = {}
        for rec in self.db.list_docker():
            pid = self._pid_of(rec)
            if rec.get("mode") == "process" and pid and self._alive(pid):
                pids[pid] = container_name(rec["task_id"])
        if not pids:
            return {}
        res = {}
        try:
            p = subprocess.run(["ps", "-o", "pid=,%cpu=,rss=", "-p", ",".join(map(str, pids))],
                               capture_output=True, text=True, timeout=5)
            for line in p.stdout.splitlines():
                parts = line.split()
                if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) in pids:
                    res[pids[int(parts[0])]] = {"cpu": parts[1] + "%",
                                                "mem": f"{int(parts[2]) / 1024:.0f}MiB", "mem_pct": ""}
        except (OSError, subprocess.SubprocessError):
            pass
        return res

    def _docker_stats(self) -> dict:
        names = [container_name(t) for t, c in self._inspect_managed().items()
                 if c["state"] == "running"]
        if not names:
            return {}
        code, out, _ = self._run(["stats", "--no-stream", "--format", "{{json .}}", *names],
                                 timeout=15)
        res = {}
        if code != 0:
            return res
        for line in out.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            res[r.get("Name", "")] = {"cpu": r.get("CPUPerc", ""),
                                      "mem": (r.get("MemUsage", "").split("/")[0]).strip(),
                                      "mem_pct": r.get("MemPerc", "")}
        return res

    def logs(self, task_id: int, tail: int = 200) -> str:
        tail = max(1, min(int(tail), 2000))
        rec = self.db.get_docker(int(task_id)) or {}
        if rec.get("mode") == "process":
            try:
                lines = self._proc_log(task_id).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return rec.get("last_error") or "(暂无日志)"
            return "\n".join(lines[-tail:]).strip() or "(暂无日志)"
        code, out, err = self._run(["logs", "--tail", str(tail), container_name(task_id)],
                                   timeout=15)
        if code != 0:
            rec = self.db.get_docker(int(task_id)) or {}
            return rec.get("last_error") or "(容器不存在或已销毁)"
        return (out + err).strip() or "(暂无日志)"

    # ------------------------------------------------------------------ 端口
    def _alloc_port(self, task_id: int, prefer: int = 0) -> int:
        used = self.db.docker_used_ports()
        rec = self.db.get_docker(task_id)
        mine = rec["port"] if rec else 0
        used.discard(mine)
        for cand in ([prefer] if prefer else []) + ([mine] if mine else []) + \
                list(range(PORT_RANGE[0], PORT_RANGE[1] + 1)):
            if not (PORT_RANGE[0] <= cand <= PORT_RANGE[1]) or cand in used:
                continue
            if self._port_free("127.0.0.1", cand):
                return cand
        raise DeployError(f"端口池 {PORT_RANGE[0]}–{PORT_RANGE[1]} 已用完, 请先销毁不用的部署。")

    # ------------------------------------------------------------------ 运行时镜像
    def prewarm(self) -> None:
        """平台启动后预备运行时镜像: 第一次部署不再等几分钟装依赖。"""
        if not self.status(force=True)["available"]:
            self._warm = {"state": "skipped", "error": ""}
            return
        self._warm = {"state": "warming", "error": ""}
        try:
            self.ensure_runtime()
            self._warm = {"state": "ready", "error": ""}
        except DeployError as e:
            self._warm = {"state": "failed", "error": str(e)}

    def ensure_runtime(self) -> None:
        code, _, _ = self._run(["image", "inspect", RUNTIME_IMAGE], timeout=10)
        if code == 0:
            return
        self._docker("build", "-t", RUNTIME_IMAGE, "-", timeout=600,
                     input_text=RUNTIME_DOCKERFILE)

    # ------------------------------------------------------------------ 生命周期
    def deploy(self, task_id: int, mode: str = "fast", ttl_hours: int = DEFAULT_TTL_HOURS,
               bind: str = "127.0.0.1", mem_limit: str = "512m", port: int = 0) -> dict:
        """启动(或替换)该任务的部署, 返回 {deploy, timings}。同任务再次部署 = 替换, 端口沿用。"""
        task_id = int(task_id)
        self.precheck(task_id, mode, ttl_hours, bind, mem_limit)
        ttl_hours = int(ttl_hours)
        try:
            return self._deploy(task_id, mode, ttl_hours, bind, mem_limit, port)
        finally:
            self._steps.pop(task_id, None)

    def precheck(self, task_id: int, mode: str, ttl_hours, bind: str, mem_limit: str) -> None:
        """参数与前置条件校验(同步, 失败抛 DeployError); 异步部署前先过这一步。"""
        if mode not in MODES:
            raise DeployError("启动方式只能是 极速(fast) 或 构建镜像(image)")
        if bind not in BINDS:
            raise DeployError("访问范围只能是 本机 或 局域网")
        if mem_limit not in MEM_LIMITS:
            raise DeployError("内存上限只能是 " + " / ".join(MEM_LIMITS))
        ttl_hours = int(ttl_hours)
        if ttl_hours not in TTL_CHOICES:
            raise DeployError("自动销毁只能是 1 小时 / 24 小时 / 7 天 / 不自动")
        if mode != "process":
            self._require()
        task = self.get_task(int(task_id))
        if not task:
            raise DeployError("任务不存在")
        if task.get("status") not in ("completed", "partial"):
            raise DeployError("任务还没有完成, 完成后才能部署")

    def mark_starting(self, task_id: int, mode: str) -> None:
        """异步部署: 先登记为启动中, 列表立刻能看到进度。"""
        self._steps[int(task_id)] = "排队"
        self.db.upsert_docker(int(task_id), status="starting", mode=mode, last_error="")

    def _deploy(self, task_id: int, mode: str, ttl_hours: int, bind: str, mem_limit: str,
                port: int) -> dict:
        task = self.get_task(task_id) or {}
        timings = {}
        with self._lock:
            t0 = time.time()
            self._steps[task_id] = "准备工程"
            ws = Path(self.workspace_of(task))
            app_dir = ws / "flask_app"
            if not (app_dir / "app.py").is_file():
                self.regenerate(task, ws)
            if not (app_dir / "app.py").is_file():
                raise DeployError("生成工程失败, 请在任务详情里重新导出后再试")
            (app_dir / "cache").mkdir(exist_ok=True)   # tmpfs 挂载点(只读挂载下无法现建)
            timings["prepare"] = round(time.time() - t0, 2)

            t1 = time.time()
            self._steps[task_id] = "启动"
            self._proc_stop(self.db.get_docker(task_id))
            if self.status()["available"]:
                self._docker("rm", "-f", container_name(task_id), check=False, timeout=30)
            port = self._alloc_port(task_id, int(port or 0))
            if mode == "process":
                return self._deploy_process(task_id, app_dir, ttl_hours, bind, port, timings, t0, t1)
            if mode == "fast":
                self.ensure_runtime()
                image = RUNTIME_IMAGE
                extra = ["-v", f"{app_dir.resolve()}:/app:ro"]
                cmd = GUNICORN_CMD
            else:
                # 独立镜像 = 运行时镜像 + 拷入工程: 不再 pip 安装, 离线可建, 秒级完成
                self.ensure_runtime()
                image = site_image(task_id)
                self._docker("build", "-t", image, "--label", "clonjurer.managed=1",
                             "--label", f"clonjurer.task={task_id}", "-f", "-", str(app_dir),
                             timeout=900, input_text=SITE_DOCKERFILE)
                extra, cmd = [], GUNICORN_CMD
            expires = _fmt(_now() + timedelta(hours=ttl_hours)) if ttl_hours else ""
            self.db.upsert_docker(task_id, port=port, image_tag=image, status="starting",
                                  mode=mode, bind=bind, expires_at=expires,
                                  mem_limit=mem_limit, last_error="")
            run_args = ["run", "-d", "--name", container_name(task_id),
                        "--label", "clonjurer.managed=1", "--label", f"clonjurer.task={task_id}",
                        "--label", f"clonjurer.mode={mode}",
                        "-p", f"{bind}:{port}:8000", "-e", "PORT=8000", "-e", "HOME=/tmp",
                        "--memory", mem_limit, "--cpus", "1", "--pids-limit", "256",
                        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                        "--read-only", "--tmpfs", "/tmp", "--tmpfs", "/app/cache",
                        "--restart", "unless-stopped",
                        *extra, image, *cmd]
            try:
                _, out, _ = self._docker(*run_args, timeout=60)
            except DeployError as e:
                self.db.upsert_docker(task_id, status="failed", last_error=str(e))
                raise
            cid = out.strip()[:64]
            timings["start"] = round(time.time() - t1, 2)
            t2 = time.time()
            self._steps[task_id] = "健康检查"
            healthy = self._health(port, HEALTH_TIMEOUT_S)
            timings["health"] = round(time.time() - t2, 2)
            if not healthy:
                tail = self.logs(task_id, 60)
                self._docker("stop", "-t", "2", container_name(task_id), check=False, timeout=20)
                msg = "健康检查 20 秒内没有通过, 容器已停止。最近日志:\n" + tail[-1500:]
                self.db.upsert_docker(task_id, container_id=cid, status="failed", last_error=msg)
                raise DeployError(msg)
            self.db.upsert_docker(task_id, container_id=cid, status="running")
        timings["total"] = round(time.time() - t0, 2)
        return {"deploy": self.get(task_id), "timings": timings}

    def _deploy_process(self, task_id: int, app_dir: Path, ttl_hours: int, bind: str, port: int,
                        timings: dict, t0: float, t1: float) -> dict:
        expires = _fmt(_now() + timedelta(hours=ttl_hours)) if ttl_hours else ""
        log = app_dir / "logs" / "deploy.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            pid = self._spawn(app_dir, bind, port, log)
        except OSError as e:
            self.db.upsert_docker(task_id, status="failed", last_error=f"启动进程失败: {e}")
            raise DeployError(f"启动进程失败: {e}")
        self.db.upsert_docker(task_id, port=port, image_tag="本机进程", status="starting", mode="process",
                              bind=bind, expires_at=expires, mem_limit="", last_error="",
                              container_id=f"pid:{pid}")
        timings["start"] = round(time.time() - t1, 2)
        t2 = time.time()
        self._steps[task_id] = "健康检查"
        healthy = self._health(port, HEALTH_TIMEOUT_S)
        timings["health"] = round(time.time() - t2, 2)
        if not healthy:
            self._kill(pid)
            tail = self.logs(task_id, 60)
            msg = "健康检查 20 秒内没有通过, 进程已停止。最近日志:\n" + tail[-1500:]
            self.db.upsert_docker(task_id, status="failed", last_error=msg)
            raise DeployError(msg)
        self.db.upsert_docker(task_id, status="running")
        timings["total"] = round(time.time() - t0, 2)
        return {"deploy": self.get(task_id), "timings": timings}

    def get(self, task_id: int) -> Optional[dict]:
        for c in self.list():
            if c["task_id"] == int(task_id):
                return c
        return None

    def stop(self, task_id: int) -> None:
        rec = self.db.get_docker(int(task_id))
        if (rec or {}).get("mode") == "process":
            self._proc_stop(rec)
            self.db.upsert_docker(int(task_id), status="stopped")
            return
        self._require()
        self._docker("stop", "-t", "3", container_name(task_id), timeout=30)
        self.db.upsert_docker(int(task_id), status="stopped")

    def start(self, task_id: int) -> dict:
        """启动已停止的部署; 容器已不存在就按原参数重新部署。"""
        task_id = int(task_id)
        rec = self.db.get_docker(task_id)
        if (rec or {}).get("mode") == "process":
            return self.deploy(task_id, "process", _ttl_left_choice(rec or {}),
                               (rec or {}).get("bind") or "127.0.0.1", "512m", (rec or {}).get("port") or 0)
        self._require()
        code, _, _ = self._run(["start", container_name(task_id)], timeout=30)
        if code != 0:
            if not rec:
                raise DeployError("没有这个部署")
            return self.deploy(task_id, rec.get("mode") or "fast",
                               _ttl_left_choice(rec), rec.get("bind") or "127.0.0.1",
                               rec.get("mem_limit") or "512m", rec.get("port") or 0)
        if rec and not self._health(rec["port"], HEALTH_TIMEOUT_S):
            self.db.upsert_docker(task_id, status="failed",
                                  last_error="启动后健康检查没有通过")
            raise DeployError("启动后健康检查没有通过, 请看日志")
        self.db.upsert_docker(task_id, status="running", last_error="")
        return {"deploy": self.get(task_id)}

    def restart(self, task_id: int) -> dict:
        rec = self.db.get_docker(int(task_id))
        if (rec or {}).get("mode") == "process":
            return self.start(task_id)
        self._require()
        self._docker("restart", "-t", "3", container_name(task_id), timeout=40)
        rec = self.db.get_docker(int(task_id))
        if rec and not self._health(rec["port"], HEALTH_TIMEOUT_S):
            raise DeployError("重启后健康检查没有通过, 请看日志")
        self.db.upsert_docker(int(task_id), status="running", last_error="")
        return {"deploy": self.get(task_id)}

    def destroy(self, task_id: int) -> dict:
        """一键销毁(幂等): 删容器 → 删独立镜像 → 删登记释放端口。克隆任务和导出包不动。"""
        task_id = int(task_id)
        removed = {"container": False, "image": False, "port": 0}
        with self._lock:
            rec = self.db.get_docker(task_id) or {}
            self._proc_stop(rec)
            if rec.get("mode") != "process" and self.status()["available"]:
                code, _, _ = self._run(["rm", "-f", container_name(task_id)], timeout=30)
                removed["container"] = code == 0
                code, _, _ = self._run(["rmi", "-f", site_image(task_id)], timeout=60)
                removed["image"] = code == 0
            removed["port"] = rec.get("port", 0)
            self.db.delete_docker(task_id)
        return removed

    def extend(self, task_id: int, hours: int) -> dict:
        hours = int(hours)
        if hours not in TTL_CHOICES:
            raise DeployError("只能延长到 1 小时 / 24 小时 / 7 天 / 不自动")
        rec = self.db.get_docker(int(task_id))
        if not rec:
            raise DeployError("没有这个部署")
        self.db.upsert_docker(int(task_id),
                              expires_at=_fmt(_now() + timedelta(hours=hours)) if hours else "")
        return {"deploy": self.get(task_id)}

    def batch(self, action: str, task_ids=None) -> int:
        items = self.list()
        if task_ids:
            wanted = {int(t) for t in task_ids}
            items = [c for c in items if c["task_id"] in wanted]
        n = 0
        for c in items:
            try:
                if action in ("stop", "stop_all") and c["status"] == "running":
                    self.stop(c["task_id"]); n += 1
                elif action == "restart" and c["status"] == "running":
                    self.restart(c["task_id"]); n += 1
                elif action == "start" and c["status"] in ("stopped", "missing", "failed"):
                    self.start(c["task_id"]); n += 1
                elif action == "destroy":
                    self.destroy(c["task_id"]); n += 1
                elif action == "cleanup_stopped" and c["status"] in ("stopped", "missing", "failed"):
                    self.destroy(c["task_id"]); n += 1
            except DeployError:
                continue
        return n

    # ------------------------------------------------------------------ 后台: 对账与自动销毁
    def reap_expired(self) -> "builtins.list[int]":
        gone = []
        now = _now()
        for rec in self.db.list_docker():
            exp = _parse(rec.get("expires_at", ""))
            if exp and exp <= now:
                self.destroy(rec["task_id"])
                gone.append(rec["task_id"])
        return gone

    def reconcile(self) -> dict:
        """平台启动时: 登记里有、容器没了 → 标 missing; 容器在跑 → 标 running。"""
        fixed = 0
        for rec in self.db.list_docker():
            if rec.get("mode") == "process":
                want = "running" if self._proc_live(rec)["state"] == "running" else "stopped"
                if rec.get("status") not in (want, "failed"):
                    self.db.upsert_docker(rec["task_id"], status=want); fixed += 1
        if not self.status(force=True)["available"]:
            return {"skipped": True, "fixed": fixed}
        live = self._inspect_managed()
        for rec in self.db.list_docker():
            if rec.get("mode") == "process":
                continue
            c = live.get(rec["task_id"])
            want = "missing" if c is None else ("running" if c["state"] == "running" else "stopped")
            if rec.get("status") != want and rec.get("status") != "failed":
                self.db.upsert_docker(rec["task_id"], status=want); fixed += 1
        return {"fixed": fixed, "orphans": [t for t in live if not self.db.get_docker(t)]}

    def start_background(self, interval_s: int = 60) -> None:
        if self._reaper and self._reaper.is_alive():
            return

        def loop():
            try:
                self.reconcile()
            except Exception:  # noqa: BLE001 — 后台线程不得因 docker 抖动退出
                pass
            self.prewarm()
            while True:
                time.sleep(interval_s)
                try:
                    self.reap_expired()
                except Exception:  # noqa: BLE001
                    pass

        self._reaper = threading.Thread(target=loop, name="deploy-reaper", daemon=True)
        self._reaper.start()


# ---------------------------------------------------------------------- helpers
def _ttl_left_choice(rec: dict) -> int:
    exp = _parse(rec.get("expires_at", ""))
    if not exp:
        return 0
    left_h = (exp - _now()).total_seconds() / 3600
    for h in (1, 24, 168):
        if left_h <= h:
            return h
    return 168


_ROOT = Path(__file__).resolve().parents[1]


def _spawn_process(app_dir: Path, bind: str, port: int, log: Path) -> int:
    with open(log, "w", encoding="utf-8") as fh:
        p = subprocess.Popen(  # noqa: S603 — 参数固定, 无 shell
            [sys.executable, "-m", "core.deploy_runner", str(app_dir.resolve()), bind, str(int(port))],
            cwd=str(_ROOT), stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    return p.pid


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    try:   # 防 PID 复用: 必须是我们的启动器
        out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="], capture_output=True,
                             text=True, timeout=3).stdout
        return "deploy_runner" in out and "<defunct>" not in out
    except (OSError, subprocess.SubprocessError):
        return True


def _kill_process(pid: int) -> None:
    try:
        os.killpg(int(pid), signal.SIGTERM)
    except (OSError, ValueError):
        return
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            os.waitpid(int(pid), os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    with_suppress = (OSError, ValueError)
    try:
        os.killpg(int(pid), signal.SIGKILL)
    except with_suppress:
        pass


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) != 0


def _wait_health(port: int, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{int(port)}/__healthz"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:  # noqa: S310 — 固定本机地址
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.15)
    return False


def _humanize(err: str) -> str:
    e = (err or "").strip()
    low = e.lower()
    if "port is already allocated" in low or "address already in use" in low:
        return "端口被占用了, 请换一个端口或稍后重试"
    if "cannot connect to the docker daemon" in low or "docker.sock" in low:
        return "连不上 Docker, 请确认 Docker 已启动"
    if "no space left" in low:
        return "磁盘空间不足, 请先清理已停止的部署或导出包"
    if "pull access denied" in low or "manifest unknown" in low or "tls handshake" in low:
        return "拉取基础镜像失败, 请检查网络后重试"
    if "pip install" in low or "could not find a version" in low or "no matching distribution" in low:
        return "准备运行时镜像时安装依赖失败(通常是网络或代理问题), 请检查网络后重试"
    if "no such container" in low:
        return "容器已不存在"
    return e[-600:] or "Docker 操作失败"


def runtime_dockerfile() -> str:
    return RUNTIME_DOCKERFILE


__all__ = ["DockerDeployer", "DeployError", "container_name", "site_image",
           "RUNTIME_IMAGE", "PORT_RANGE", "TTL_CHOICES", "MODES", "BINDS", "MEM_LIMITS"]
