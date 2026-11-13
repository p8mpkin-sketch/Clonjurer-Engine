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

import json
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
MODES = ("fast", "image")
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
                 health: Optional[Callable[[int, float], bool]] = None):
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
                "hint": "" if ok else "没有检测到 Docker。请安装并启动 Docker Desktop(或 dockerd)后刷新。"}

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
            rows.append(self._present(rec, live.get(tid)))
        for tid, c in live.items():                # 有容器无登记: 未登记(可清理)
            if tid not in seen:
                rows.append(self._present({"task_id": tid, "port": 0, "image_tag": c["image"],
                                           "status": "orphan"}, c, orphan=True))
        return rows

    def _present(self, rec: dict, live: Optional[dict], orphan: bool = False) -> dict:
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
        }

    def summary(self) -> dict:
        st = self.status()
        items = self.list()
        return {"available": st["available"], "version": st["version"], "hint": st["hint"],
                "containers": items, "total": len(items),
                "running": sum(1 for c in items if c["status"] == "running"),
                "stopped": sum(1 for c in items if c["status"] in ("stopped", "failed", "missing")),
                "ports": sum(1 for c in items if c["status"] == "running" and c["port"]),
                "port_range": list(PORT_RANGE)}

    def stats(self) -> dict:
        if not self.status()["available"]:
            return {}
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
        if mode not in MODES:
            raise DeployError("启动方式只能是 极速(fast) 或 构建镜像(image)")
        if bind not in BINDS:
            raise DeployError("访问范围只能是 本机 或 局域网")
        if mem_limit not in MEM_LIMITS:
            raise DeployError("内存上限只能是 " + " / ".join(MEM_LIMITS))
        ttl_hours = int(ttl_hours)
        if ttl_hours not in TTL_CHOICES:
            raise DeployError("自动销毁只能是 1 小时 / 24 小时 / 7 天 / 不自动")
        self._require()
        task = self.get_task(task_id)
        if not task:
            raise DeployError("任务不存在")
        if task.get("status") not in ("completed", "partial"):
            raise DeployError("任务还没有完成, 完成后才能部署")
        timings = {}
        with self._lock:
            t0 = time.time()
            ws = Path(self.workspace_of(task))
            app_dir = ws / "flask_app"
            if not (app_dir / "app.py").is_file():
                self.regenerate(task, ws)
            if not (app_dir / "app.py").is_file():
                raise DeployError("生成工程失败, 请在任务详情里重新导出后再试")
            (app_dir / "cache").mkdir(exist_ok=True)   # tmpfs 挂载点(只读挂载下无法现建)
            timings["prepare"] = round(time.time() - t0, 2)

            t1 = time.time()
            self._docker("rm", "-f", container_name(task_id), check=False, timeout=30)
            port = self._alloc_port(task_id, int(port or 0))
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

    def get(self, task_id: int) -> Optional[dict]:
        for c in self.list():
            if c["task_id"] == int(task_id):
                return c
        return None

    def stop(self, task_id: int) -> None:
        self._require()
        self._docker("stop", "-t", "3", container_name(task_id), timeout=30)
        self.db.upsert_docker(int(task_id), status="stopped")

    def start(self, task_id: int) -> dict:
        """启动已停止的部署; 容器已不存在就按原参数重新部署。"""
        self._require()
        task_id = int(task_id)
        rec = self.db.get_docker(task_id)
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
            if self.status()["available"]:
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
    def reap_expired(self) -> list:
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
        if not self.status(force=True)["available"]:
            return {"skipped": True}
        live = self._inspect_managed()
        fixed = 0
        for rec in self.db.list_docker():
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
            while True:
                time.sleep(interval_s)
                try:
                    if self.status()["available"]:
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
