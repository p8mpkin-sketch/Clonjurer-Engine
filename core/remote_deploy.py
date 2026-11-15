"""远程发布: 把已生成的站点工程包经 SSH 发布到用户自己的服务器上长期运行。

与本机部署(core/deploy.py: Docker / 进程, 用于快速预览)并存互补, 互不调用。

设计要点:
- 服务器档案: 名称 / 主机 / SSH 端口 / 用户名 / 口令或私钥。凭据只以 core/security.py 的 Fernet
  密文落库(remote_deploys.password_enc); 日志与 API 返回永不含凭据原文(present() 只回掩码)。
- 主机密钥 TOFU: 首次连接记录服务器公钥(remote_deploys.host_key), 之后公钥不一致立即中止,
  且在发送口令/密钥之前中止 —— 不把凭据交给冒充的主机。不读本机 known_hosts。
- 打包与导出 ZIP 同源: 取 flask_generator 产出的导出包(缺失时由调用方按导出逻辑重建),
  在本机把顶层目录改名为 app/ 再上传; 只上传、只解压平台自己产出的包。
- 远端命令一律固定模板(build_commands): 只插入由档案 ID 派生的路径 / 服务标识与校验过的整数端口,
  名称、备注、主机等自由文本永不进入命令行; 上传走 SFTP。
- 每次 SSH 连接、每条命令、SFTP 传输与健康检查都有超时; 失败置 failed 并保留日志。
- 远端布局(由 ID 派生): ~/clonjurer_deploy/rd_<id>/{app.zip, app/, venv/, runtime.log,
  clonjurer_rd_<id>.pid}; 服务标识 clonjurer_rd_<id>。
- paramiko 懒加载(_paramiko): 不装 paramiko 也能导入本模块; 测试把它整体替换成假实现。
"""
from __future__ import annotations

import hashlib
import base64
import io
import ipaddress
import posixpath
import re
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

REMOTE_BASE = "clonjurer_deploy"
SERVICE_PREFIX = "clonjurer_rd_"
MASK = "••••••••"                 # 凭据脱敏: 前 0 位 + 掩码
AUTH_TYPES = ("password", "key")
SSH_TIMEOUT = 15                  # TCP 连接 / 握手 / 认证, 秒
CMD_TIMEOUTS = {
    "mkdir": 30, "extract": 180, "deps": 900, "stop": 30, "start": 30,
    "local_health": 15, "status": 20, "logs": 20, "purge": 120,
}
UPLOAD_TIMEOUT = 600
HEALTH_TIMEOUT_S = 45.0
LOG_TAIL_MAX = 2000

# 发布流水各步的进度区间(百分比): 进入该步时取下界, 步内按完成比例推进到上界
STAGES: tuple = (
    ("package", "打包工程", 5, 20),
    ("connect", "连接服务器", 18, 18),
    ("upload", "上传工程包", 24, 42),
    ("extract", "解压工程", 46, 46),
    ("deps", "安装依赖", 52, 78),
    ("launch", "启动服务", 82, 82),
    ("health", "健康检查", 90, 100),
)
_STAGE_MAP = {k: (label, lo, hi) for k, label, lo, hi in STAGES}

_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,31}$")


class RemoteDeployError(RuntimeError):
    """面向用户的错误(message 是可直接展示的中文, 不含凭据)。"""


class HostKeyChanged(RemoteDeployError):
    """服务器公钥与首次连接时记录的不一致(TOFU 拒绝)。"""


# ---------------------------------------------------------------------------
# 纯函数: 校验 / 派生 / 命令模板 / 进度映射 / 脱敏 / 主机密钥
# ---------------------------------------------------------------------------
def validate_port(value: Any, what: str = "端口") -> int:
    """整数且 1~65535; 布尔值、小数、带符号或非数字字符串一律拒绝。"""
    if isinstance(value, bool):
        raise RemoteDeployError(f"{what}必须是 1~65535 的整数")
    if isinstance(value, int):
        port = value
    elif isinstance(value, str) and value.strip().isdigit():
        port = int(value.strip())
    else:
        raise RemoteDeployError(f"{what}必须是 1~65535 的整数")
    if not 1 <= port <= 65535:
        raise RemoteDeployError(f"{what}必须是 1~65535 的整数")
    return port


def validate_host(value: Any) -> str:
    """主机: 域名 / IPv4 / IPv6(可带方括号)。只会交给 paramiko 与平台侧 HTTP 探活, 不进远端命令。"""
    host = str(value or "").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or len(host) > 253:
        raise RemoteDeployError("请填写服务器地址(IP 或域名)")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if not _HOST_RE.match(host) or ".." in host:
        raise RemoteDeployError("服务器地址只能是 IP 或域名(字母、数字、点、短横线)")
    return host.lower()


def validate_username(value: Any) -> str:
    user = str(value or "").strip() or "root"
    if not _USER_RE.match(user):
        raise RemoteDeployError("SSH 用户名只能包含字母、数字、点、下划线、短横线(最长 32 位)")
    return user


def remote_dir(dep_id: int) -> str:
    """远端目录(相对登录用户家目录): clonjurer_deploy/rd_<id>。"""
    return f"{REMOTE_BASE}/rd_{int(dep_id)}"


def service_name(dep_id: int) -> str:
    return f"{SERVICE_PREFIX}{int(dep_id)}"


def build_commands(dep_id: int, port: Any) -> dict:
    """全部远端命令的固定模板。只插入 int(dep_id) 派生的路径 / 服务标识与校验过的整数端口。"""
    i = int(dep_id)
    p = validate_port(port, "服务端口")
    d = f"~/{remote_dir(i)}"
    svc = service_name(i)
    pid = f"{d}/{svc}.pid"
    # 只认我们自己起的进程: pid 文件里的进程命令行须含 app.py, 防 PID 被系统复用后误杀
    alive = f'pid=$(cat {pid} 2>/dev/null); [ -n "$pid" ] && grep -q app.py /proc/$pid/cmdline 2>/dev/null'
    return {
        "mkdir": f"mkdir -p {d}",
        "extract": (f"cd {d} && rm -rf app.new && python3 -c "
                    "'import zipfile; zipfile.ZipFile(\"app.zip\").extractall(\"app.new\")'"
                    " && rm -rf app && mv app.new/app app && rm -rf app.new app.zip"),
        "deps": (f"cd {d} && (test -x venv/bin/python || python3 -m venv venv) && "
                 "venv/bin/python -m pip install -q --disable-pip-version-check -r app/requirements.txt"),
        "stop": (f"if {alive}; then kill $pid; sleep 1; kill -9 $pid 2>/dev/null; fi; "
                 f"rm -f {pid}; echo stopped"),
        "start": (f"cd {d}/app && {{ CLONJURER_SERVICE={svc} PORT={p} nohup ../venv/bin/python app.py {p}"
                  f" > {d}/runtime.log 2>&1 < /dev/null & echo $! > {pid}; }} && echo started"),
        "local_health": ("python3 -c 'import urllib.request; "
                         f"urllib.request.urlopen(\"http://127.0.0.1:{p}/__healthz\", timeout=3)'"),
        "status": (f"if [ ! -d {d} ]; then echo absent; elif {alive}; then echo running; "
                   "else echo stopped; fi"),
        "purge": f"rm -rf {d}",
    }


def runtime_log_command(dep_id: int, lines: Any = 200) -> str:
    try:
        n = int(lines)
    except (TypeError, ValueError):
        n = 200
    n = max(1, min(LOG_TAIL_MAX, n))
    return f"tail -n {n} ~/{remote_dir(int(dep_id))}/runtime.log 2>/dev/null; exit 0"


def stage_label(stage: str) -> str:
    return _STAGE_MAP[stage][0]


def progress_for(stage: str, frac: float = 0.0) -> int:
    """某步完成 frac(0~1) 时的总进度。"""
    _label, lo, hi = _STAGE_MAP[stage]
    frac = max(0.0, min(1.0, float(frac)))
    return int(round(lo + (hi - lo) * frac))


def mask_secret(secret: Optional[str]) -> str:
    return MASK if secret else ""


def scrub(text: str, *secrets: Optional[str]) -> str:
    """日志落库前抹掉凭据原文(远端输出或异常信息里万一回显了口令)。"""
    out = str(text or "")
    for s in secrets:
        if s:
            out = out.replace(s, MASK)
            for line in s.splitlines():   # 私钥按行也抹
                if len(line.strip()) >= 8:
                    out = out.replace(line.strip(), MASK)
    return out


def host_key_id(key: Any) -> str:
    """paramiko PKey -> 'ssh-ed25519 AAAA…'(入库比对用)。"""
    return f"{key.get_name()} {key.get_base64()}"


def fingerprint(host_key: str) -> str:
    """'类型 base64' -> 'SHA256:…'(与 ssh-keygen -lf 一致), 给人看。"""
    if not host_key:
        return ""
    try:
        blob = base64.b64decode(host_key.split()[-1])
    except (ValueError, IndexError):
        return host_key[:24]
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def verify_host_key(stored: str, presented: str) -> None:
    """TOFU: 有记录且与本次不一致 -> HostKeyChanged; 没有记录(首次)放行。"""
    if stored and presented != stored:
        raise HostKeyChanged(
            f"服务器主机密钥与首次连接时记录的不一致(记录 {fingerprint(stored)}, 本次 {fingerprint(presented)})。"
            "可能是服务器重装过系统, 也可能遭遇中间人攻击, 已中止本次操作且未发送凭据。"
            "确认服务器确实换过密钥后, 在档案编辑里勾选「重置主机指纹」再试")


def repack_export_zip(src: Path, dst: Path) -> Path:
    """导出 ZIP(顶层 clone_site_<task>/)换成顶层 app/, 内容逐字节不变; 拒绝越界路径。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        names = zin.namelist()
        tops = {n.split("/", 1)[0] for n in names if n}
        if len(tops) != 1:
            raise RemoteDeployError("导出包结构异常(应只有一个顶层目录), 请重新生成工程")
        for info in zin.infolist():
            rest = info.filename.split("/", 1)[1] if "/" in info.filename else ""
            if not rest:
                continue
            parts = posixpath.normpath(rest).split("/")
            if rest.startswith("/") or ".." in parts:
                raise RemoteDeployError("导出包含越界路径, 已拒绝")
            zout.writestr("app/" + rest, zin.read(info.filename))
    tmp.replace(dst)
    return dst


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 — 平台侧探活, 地址已校验
            return 200 <= r.status < 400
    except Exception:  # noqa: BLE001
        return False


def _paramiko():
    try:
        import paramiko
    except ImportError as e:  # pragma: no cover - requirements 已含 paramiko
        raise RemoteDeployError("平台未安装 paramiko, 无法使用远程发布(pip install paramiko)") from e
    return paramiko


def humanize(exc: BaseException) -> str:
    """把 paramiko / socket 异常翻译成中文(按类名判断, 不依赖 paramiko 已安装)。"""
    if isinstance(exc, RemoteDeployError):
        return str(exc)
    name = type(exc).__name__
    if name == "AuthenticationException" or name == "BadAuthenticationType":
        return "认证失败: 用户名、口令或私钥不对"
    if name == "PasswordRequiredException":
        return "私钥带口令保护, 暂不支持, 请改用无口令私钥或口令登录"
    if name in ("timeout", "TimeoutError") or "timed out" in str(exc).lower():
        return f"连接超时({SSH_TIMEOUT}s): 请检查地址、SSH 端口与服务器安全组"
    if name == "gaierror":
        return "解析不了这个主机名, 请检查服务器地址"
    if name in ("NoValidConnectionsError", "ConnectionRefusedError"):
        return "服务器拒绝连接: SSH 端口不对或 sshd 未运行"
    if name in ("ConnectionResetError", "EOFError"):
        return "连接被服务器中断"
    if name == "SSHException":
        return f"SSH 握手失败: {str(exc)[:200]}"
    return f"{name}: {str(exc)[:200]}"


class _TofuPolicy:
    """paramiko 的 MissingHostKeyPolicy: 不加载 known_hosts, 每次都走这里做 TOFU 比对。

    paramiko 在认证之前调用 missing_host_key, 抛异常即中止连接 —— 凭据不会发给不认识的主机。"""

    def __init__(self, stored: str):
        self.stored = stored or ""
        self.seen = ""

    def missing_host_key(self, client, hostname, key):  # noqa: D401 — paramiko 接口
        presented = host_key_id(key)
        verify_host_key(self.stored, presented)
        self.seen = presented


def _load_pkey(pm, text: str):
    last: Optional[BaseException] = None
    for cls_name in ("Ed25519Key", "ECDSAKey", "RSAKey"):
        cls = getattr(pm, cls_name, None)
        if cls is None:
            continue
        try:
            return cls.from_private_key(io.StringIO(text))
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "PasswordRequiredException":
                raise
            last = e
    raise RemoteDeployError("私钥格式无法识别(支持 OpenSSH / PEM 格式的 Ed25519、ECDSA、RSA)") from last


def ssh_connect(host: str, port: int, username: str, secret: str, auth_type: str = "password",
                stored_host_key: str = ""):
    """建立 SSH 连接(TOFU + 超时)。返回 (client, 本次看到的主机公钥)。"""
    pm = _paramiko()
    client = pm.SSHClient()
    policy = _TofuPolicy(stored_host_key)
    client.set_missing_host_key_policy(policy)
    kw: dict = dict(hostname=host, port=int(port), username=username, timeout=SSH_TIMEOUT,
                    banner_timeout=SSH_TIMEOUT, auth_timeout=SSH_TIMEOUT,
                    allow_agent=False, look_for_keys=False)
    if auth_type == "key":
        kw["pkey"] = _load_pkey(pm, secret)
    else:
        kw["password"] = secret
    try:
        client.connect(**kw)
    except BaseException:
        client.close()
        raise
    return client, (policy.seen or stored_host_key)


def run_command(client, command: str, timeout: float,
                on_tick: Optional[Callable[[float], None]] = None) -> tuple:
    """执行一条固定模板命令, 返回 (退出码, 合并输出)。超时关通道并抛 RemoteDeployError。"""
    _stdin, stdout, _stderr = client.exec_command(command, timeout=timeout, get_pty=False)
    chan = stdout.channel
    chan.set_combine_stderr(True)
    buf = bytearray()
    t0 = time.monotonic()
    while True:
        while chan.recv_ready():
            buf.extend(chan.recv(65536))
        if chan.exit_status_ready():
            break
        elapsed = time.monotonic() - t0
        if elapsed > timeout:
            chan.close()
            raise RemoteDeployError(f"远端命令执行超时({int(timeout)}s)")
        if on_tick:
            on_tick(elapsed / timeout)
        time.sleep(0.2)
    while chan.recv_ready():
        buf.extend(chan.recv(65536))
    return chan.recv_exit_status(), buf.decode("utf-8", errors="replace")


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def service_url(host: str, port: int) -> str:
    return f"http://{_url_host(host)}:{int(port)}/"


def _hint_for(output: str) -> str:
    low = (output or "").lower()
    if "ensurepip" in low or "no module named venv" in low:
        return "服务器缺少 python3-venv, 请先在服务器上安装(Debian/Ubuntu: apt install -y python3-venv)"
    if "python3: not found" in low or "python3: command not found" in low:
        return "服务器上没有 python3, 请先安装 Python 3.9 及以上"
    if "no space left" in low:
        return "服务器磁盘空间不足"
    if "could not find a version" in low or "no matching distribution" in low or \
            "connectionerror" in low or "temporary failure in name resolution" in low or \
            "network is unreachable" in low:
        return "安装依赖失败, 通常是服务器访问 PyPI 的网络问题"
    if "address already in use" in low:
        return "服务端口已被服务器上的其他程序占用, 请换一个端口"
    return ""


# ---------------------------------------------------------------------------
# 服务层
# ---------------------------------------------------------------------------
class RemoteDeployer:
    """remote_deploys 档案的发布与生命周期。

    package_fn(clone_task_id) -> 导出 ZIP 路径(与「下载 ZIP」同源, 缺失时由调用方重建)。
    decrypt_fn(密文) -> 凭据原文(core/security.decrypt_value)。"""

    def __init__(self, db, package_fn: Callable[[int], Path], decrypt_fn: Callable[[str], str],
                 work_dir: Path):
        self.db = db
        self.package_fn = package_fn
        self.decrypt_fn = decrypt_fn
        self.work_dir = Path(work_dir)
        self._running: set = set()
        self._last_progress: dict = {}
        self._lock = threading.Lock()

    # ----- 展示 -----
    @staticmethod
    def present(rec: dict, log_tail: int = 0) -> dict:
        """API 返回: 去掉凭据密文, 只给掩码; 可附日志尾部。"""
        out = {k: v for k, v in rec.items() if k not in ("password_enc", "log")}
        has = bool(rec.get("password_enc"))
        out["has_secret"] = has
        out["password_masked"] = MASK if has else ""
        out["auth_type"] = rec.get("auth_type") or "password"
        out["remote_dir"] = "~/" + remote_dir(rec["id"])
        out["service"] = service_name(rec["id"])
        out["host_fingerprint"] = fingerprint(rec.get("host_key") or "")
        out.pop("host_key", None)
        if log_tail:
            lines = (rec.get("log") or "").splitlines()
            out["deploy_log"] = "\n".join(lines[-log_tail:])
        return out

    def is_busy(self, dep_id: int) -> bool:
        return dep_id in self._running

    def recover_after_restart(self) -> int:
        return self.db.fail_stale_remote_deploys()

    # ----- 内部 -----
    def _rec(self, dep_id: int) -> dict:
        rec = self.db.get_remote_deploy(dep_id)
        if rec is None:
            raise RemoteDeployError("远程发布档案不存在")
        return rec

    def _secret(self, rec: dict) -> str:
        secret = self.decrypt_fn(rec.get("password_enc") or "")
        if not secret:
            raise RemoteDeployError("档案没有可用的口令/私钥(未填写或平台密钥已更换), 请编辑档案重新填写")
        return secret

    def _log(self, dep_id: int, line: str, secret: str = "") -> None:
        self.db.append_remote_log(dep_id, scrub(line, secret))

    def _connect(self, rec: dict, secret: str):
        client, seen = ssh_connect(rec["host"], rec["ssh_port"], rec["username"], secret,
                                   rec.get("auth_type") or "password", rec.get("host_key") or "")
        if seen and not rec.get("host_key"):
            self.db.update_remote_deploy(rec["id"], host_key=seen)
            self._log(rec["id"], f"首次连接, 已记录服务器主机指纹 {fingerprint(seen)}(TOFU)")
        return client

    # ----- 连接测试 -----
    @staticmethod
    def test_connection(host: Any, ssh_port: Any, username: Any, secret: str, auth_type: str = "password",
                        stored_host_key: str = "") -> dict:
        """只验证能否登录成功, 不执行任何远端命令。"""
        try:
            h = validate_host(host)
            p = validate_port(ssh_port, "SSH 端口")
            u = validate_username(username)
            if auth_type not in AUTH_TYPES:
                raise RemoteDeployError("认证方式只能是 password / key")
            if not secret:
                raise RemoteDeployError("请填写口令或私钥")
            t0 = time.monotonic()
            client, seen = ssh_connect(h, p, u, secret, auth_type, stored_host_key)
            client.close()
            return {"ok": True, "latency_ms": int((time.monotonic() - t0) * 1000),
                    "fingerprint": fingerprint(seen)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": scrub(humanize(e), secret),
                    "host_key_changed": isinstance(e, HostKeyChanged)}

    # ----- 发布 -----
    def begin_deploy(self, dep_id: int) -> None:
        """同步部分: 校验并把档案置为 deploying; 之后调用 run_deploy(在后台线程)。"""
        rec = self._rec(dep_id)
        self._secret(rec)
        if not rec.get("clone_task_id"):
            raise RemoteDeployError("档案没有关联克隆任务")
        validate_port(rec.get("remote_port"), "服务端口")
        with self._lock:
            if dep_id in self._running:
                raise RemoteDeployError("这个档案正在发布中")
            self._running.add(dep_id)
        self.db.update_remote_deploy(dep_id, status="deploying", progress=progress_for("package"),
                                     stage=stage_label("package"), log="", service_url="")
        self._log(dep_id, f"开始发布到 {rec['username']}@{rec['host']}:{rec['ssh_port']}"
                          f" -> ~/{remote_dir(dep_id)} (服务 {service_name(dep_id)}, 端口 {rec['remote_port']})")

    def start_deploy_thread(self, dep_id: int) -> threading.Thread:
        self.begin_deploy(dep_id)
        t = threading.Thread(target=self.run_deploy, args=(dep_id,), daemon=True, name=f"remote-deploy-{dep_id}")
        t.start()
        return t

    def _step(self, dep_id: int, stage: str, frac: float = 0.0, note: str = "") -> None:
        """写进度; 进度与阶段都没变时不落库(依赖安装等步骤每 0.2s 会回调一次)。"""
        pct, label = progress_for(stage, frac), stage_label(stage)
        if self._last_progress.get(dep_id) != (pct, label):
            self._last_progress[dep_id] = (pct, label)
            self.db.update_remote_deploy(dep_id, progress=pct, stage=label)
        if note:
            self._log(dep_id, note)

    def run_deploy(self, dep_id: int) -> None:
        secret = ""
        client = None
        try:
            rec = self._rec(dep_id)
            secret = self._secret(rec)
            port = validate_port(rec["remote_port"], "服务端口")
            cmds = build_commands(dep_id, port)

            # 1) 打包 5-20: 导出 ZIP(与下载同源) -> 顶层改名 app/
            self._step(dep_id, "package", 0.0, f"打包: 克隆任务 #{rec['clone_task_id']} 的导出工程")
            export_zip = Path(self.package_fn(int(rec["clone_task_id"])))
            self._step(dep_id, "package", 0.45)
            bundle = repack_export_zip(export_zip, self.work_dir / f"rd_{dep_id}.zip")
            size_kb = max(1, bundle.stat().st_size // 1024)
            self._step(dep_id, "package", 0.75, f"打包完成 {size_kb} KB")

            # 2) 连接 18
            self._step(dep_id, "connect", 0.0, f"连接 {rec['host']}:{rec['ssh_port']}(超时 {SSH_TIMEOUT}s)")
            client = self._connect(rec, secret)
            self._log(dep_id, "SSH 登录成功")
            self._exec(client, dep_id, "mkdir", cmds, secret)

            # 3) 上传 24-42(SFTP)
            self._step(dep_id, "upload", 0.0, "SFTP 上传工程包")
            self._upload(client, dep_id, bundle)
            self._step(dep_id, "upload", 1.0, "上传完成")

            # 4) 解压 46
            self._step(dep_id, "extract", 0.0, "远端解压到 app/")
            self._exec(client, dep_id, "extract", cmds, secret)

            # 5) 依赖安装 52-78
            self._step(dep_id, "deps", 0.0, "创建虚拟环境并安装依赖(首次可能需要数分钟)")
            self._exec(client, dep_id, "deps", cmds, secret,
                       on_tick=lambda f: self._step(dep_id, "deps", min(0.95, f * 4)))
            self._step(dep_id, "deps", 1.0, "依赖安装完成")

            # 6) 启动 82: 先停旧进程再起新进程
            self._step(dep_id, "launch", 0.0, f"启动服务 {service_name(dep_id)}(端口 {port})")
            self._exec(client, dep_id, "stop", cmds, secret)
            self._exec(client, dep_id, "start", cmds, secret)

            # 7) 健康检查 90-100: 远端本机探活 -> 平台侧 HTTP 轮询
            self._step(dep_id, "health", 0.0, "健康检查: 轮询 /__healthz")
            url = service_url(rec["host"], port)
            self._health(client, dep_id, url, port, cmds, secret)
            self.db.update_remote_deploy(dep_id, status="running", progress=100,
                                         stage="运行中", service_url=url)
            self._log(dep_id, f"部署完成, 服务地址 {url}")
        except Exception as e:  # noqa: BLE001 — 后台线程: 任何失败都落到档案状态上
            msg = scrub(humanize(e), secret)
            self.db.update_remote_deploy(dep_id, status="failed", stage="发布失败")
            self._log(dep_id, f"ERROR 部署失败: {msg}", secret)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
            with self._lock:
                self._running.discard(dep_id)
                self._last_progress.pop(dep_id, None)

    def _exec(self, client, dep_id: int, key: str, cmds: dict, secret: str,
              on_tick: Optional[Callable[[float], None]] = None) -> str:
        rc, out = run_command(client, cmds[key], CMD_TIMEOUTS[key], on_tick=on_tick)
        tail = "\n".join(out.strip().splitlines()[-30:])
        if tail and key in ("extract", "deps", "start"):
            self._log(dep_id, tail, secret)
        if rc != 0:
            hint = _hint_for(out)
            raise RemoteDeployError(f"步骤「{key}」失败(退出码 {rc})" + (f": {hint}" if hint else ""))
        return out

    def _upload(self, client, dep_id: int, bundle: Path) -> None:
        sftp = client.open_sftp()
        try:
            chan = sftp.get_channel()
            if chan is not None:
                chan.settimeout(UPLOAD_TIMEOUT)
            home = sftp.normalize(".")
            target = posixpath.join(home, remote_dir(dep_id), "app.zip")
            last = [-1]

            def cb(done: int, total: int) -> None:
                frac = done / total if total else 1.0
                bucket = int(frac * 10)
                if bucket != last[0]:
                    last[0] = bucket
                    self._step(dep_id, "upload", frac)
            sftp.put(str(bundle), target, callback=cb)
        finally:
            sftp.close()

    def _health(self, client, dep_id: int, url: str, port: int, cmds: dict, secret: str) -> None:
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        local_ok = False
        while time.monotonic() < deadline:
            rc, _out = run_command(client, cmds["local_health"], CMD_TIMEOUTS["local_health"])
            if rc == 0:
                local_ok = True
                break
            self._step(dep_id, "health", min(0.5, 1 - (deadline - time.monotonic()) / HEALTH_TIMEOUT_S))
            time.sleep(1.0)
        if not local_ok:
            _rc, logs = run_command(client, runtime_log_command(dep_id, 30), CMD_TIMEOUTS["logs"])
            if logs.strip():
                self._log(dep_id, "运行日志尾部:\n" + logs.strip(), secret)
            hint = _hint_for(logs)
            raise RemoteDeployError(f"服务在 {int(HEALTH_TIMEOUT_S)}s 内没有响应健康检查" + (f": {hint}" if hint else ""))
        self._step(dep_id, "health", 0.6, "远端本机健康检查通过")
        for i in range(10):   # 平台侧 HTTP 轮询: 验证从外部能访问到
            if _http_ok(url + "__healthz"):
                self._step(dep_id, "health", 1.0, f"外部访问 {url} 正常")
                return
            self._step(dep_id, "health", 0.6 + 0.04 * i)
            time.sleep(1.0)
        self._log(dep_id, f"WARNING 服务已在服务器上运行, 但从平台访问不到 {url}: "
                          f"请在服务器防火墙/云安全组放行 TCP {port} 端口")

    # ----- 生命周期 -----
    def _guard_idle(self, dep_id: int) -> None:
        if dep_id in self._running:
            raise RemoteDeployError("这个档案正在发布中, 请等发布结束")

    def _session(self, dep_id: int, fn: Callable[[Any, dict, dict, str], Any]) -> Any:
        rec = self._rec(dep_id)
        secret = self._secret(rec)
        try:
            client = self._connect(rec, secret)
        except Exception as e:  # noqa: BLE001
            raise RemoteDeployError(scrub(humanize(e), secret)) from None
        try:
            return fn(client, rec, build_commands(dep_id, rec["remote_port"]), secret)
        except RemoteDeployError:
            raise
        except Exception as e:  # noqa: BLE001
            raise RemoteDeployError(scrub(humanize(e), secret)) from None
        finally:
            client.close()

    def start(self, dep_id: int) -> dict:
        self._guard_idle(dep_id)

        def go(client, rec, cmds, secret):
            st = run_command(client, cmds["status"], CMD_TIMEOUTS["status"])[1].strip()
            if st == "absent":
                raise RemoteDeployError("服务器上还没有这个工程, 请先发布")
            if st != "running":
                self._exec(client, dep_id, "start", cmds, secret)
            ok = False
            for _ in range(10):
                if run_command(client, cmds["local_health"], CMD_TIMEOUTS["local_health"])[0] == 0:
                    ok = True
                    break
                time.sleep(1.0)
            url = service_url(rec["host"], rec["remote_port"])
            if ok:
                self.db.update_remote_deploy(dep_id, status="running", stage="运行中", service_url=url)
                self._log(dep_id, f"已启动服务 {service_name(dep_id)}")
            else:
                self.db.update_remote_deploy(dep_id, status="failed", stage="启动失败")
                self._log(dep_id, "ERROR 启动后健康检查未通过, 请查看运行日志")
            return {"ok": ok, "status": "running" if ok else "failed", "service_url": url if ok else ""}
        return self._session(dep_id, go)

    def stop(self, dep_id: int) -> dict:
        self._guard_idle(dep_id)

        def go(client, rec, cmds, secret):
            self._exec(client, dep_id, "stop", cmds, secret)
            self.db.update_remote_deploy(dep_id, status="stopped", stage="已停止")
            self._log(dep_id, f"已停止服务 {service_name(dep_id)}")
            return {"ok": True, "status": "stopped"}
        return self._session(dep_id, go)

    def teardown(self, dep_id: int, purge: bool = False) -> dict:
        """停服务(不存在也算成功), 可选删除远端目录。幂等。"""
        self._guard_idle(dep_id)

        def go(client, rec, cmds, secret):
            self._exec(client, dep_id, "stop", cmds, secret)
            if purge:
                self._exec(client, dep_id, "purge", cmds, secret)
            return {"ok": True, "purged": bool(purge)}
        return self._session(dep_id, go)

    def runtime_logs(self, dep_id: int, lines: Any = 200) -> str:
        def go(client, rec, cmds, secret):
            return scrub(run_command(client, runtime_log_command(dep_id, lines), CMD_TIMEOUTS["logs"])[1], secret)
        return self._session(dep_id, go)

    def probe(self, dep_id: int) -> dict:
        """存活探测: 远端进程 + 远端本机健康检查 + 平台侧 HTTP。不改档案状态。"""
        def go(client, rec, cmds, secret):
            proc = run_command(client, cmds["status"], CMD_TIMEOUTS["status"])[1].strip() or "unknown"
            healthy = proc == "running" and \
                run_command(client, cmds["local_health"], CMD_TIMEOUTS["local_health"])[0] == 0
            url = service_url(rec["host"], rec["remote_port"])
            return {"process": proc, "healthy": healthy,
                    "reachable": healthy and _http_ok(url + "__healthz"), "service_url": url}
        return self._session(dep_id, go)


__all__ = ["RemoteDeployer", "RemoteDeployError", "HostKeyChanged", "STAGES", "MASK", "AUTH_TYPES",
           "build_commands", "runtime_log_command", "remote_dir", "service_name", "progress_for",
           "stage_label", "mask_secret", "scrub", "verify_host_key", "fingerprint", "host_key_id",
           "validate_port", "validate_host", "validate_username", "repack_export_zip", "humanize",
           "ssh_connect", "run_command", "service_url"]
