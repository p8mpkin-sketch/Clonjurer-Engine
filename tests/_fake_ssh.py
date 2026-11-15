"""远程发布测试用的假 paramiko: 不开 socket、不连真主机。

FakeWorld 里登记若干假服务器(主机名 -> 公钥 / 口令 / 命令处理), FakeParamiko 是可整体替换
core.remote_deploy._paramiko() 返回值的模块替身。connect 的顺序与真 paramiko 一致:
先把服务器公钥交给 MissingHostKeyPolicy(TOFU 在这里拒绝), 通过后才做口令认证 ——
所以测试能断言「指纹不符时凭据根本没发出去」。"""
from __future__ import annotations

import socket
from typing import Callable, Optional


class AuthenticationException(Exception):
    pass


class FakeKey:
    def __init__(self, b64: str, name: str = "ssh-ed25519"):
        self._b64, self._name = b64, name

    def get_name(self):
        return self._name

    def get_base64(self):
        return self._b64


class FakeChannel:
    def __init__(self, rc: int = 0, out: str = "", never_exits: bool = False):
        self._out = out.encode("utf-8")
        self.rc = rc
        self.never_exits = never_exits
        self.closed = False
        self.timeout = None

    def set_combine_stderr(self, v):
        pass

    def recv_ready(self):
        return bool(self._out)

    def recv(self, n):
        d, self._out = self._out[:n], self._out[n:]
        return d

    def exit_status_ready(self):
        return not self.never_exits

    def recv_exit_status(self):
        return self.rc

    def close(self):
        self.closed = True

    def settimeout(self, t):
        self.timeout = t


class FakeStream:
    def __init__(self, chan: FakeChannel):
        self.channel = chan

    def read(self):
        return b""


class FakeServer:
    def __init__(self, key: str, password: str, home: str = "/home/deploy"):
        self.key = key
        self.password = password
        self.home = home
        self.auth_attempts: list = []      # 每次真正送出的口令(TOFU 拒绝时不会出现)
        self.commands: list = []           # (命令, 超时)
        self.uploads: list = []            # (本地路径, 远端路径, 字节数)
        self.running = False
        self.present = False
        self.healthy = True
        self.fail: dict = {}               # 命令关键字 -> (退出码, 输出)
        self.echo = ""                     # 让依赖安装「回显」一段文本(用来测日志脱敏)
        self.hang: Optional[str] = None    # 命令关键字: 该命令永不结束(测超时)

    def handle(self, cmd: str):
        for key, res in self.fail.items():
            if key in cmd:
                return res
        if "mkdir -p" in cmd:
            self.present = True
            return 0, ""
        if "zipfile" in cmd:
            return 0, ""
        if "pip install" in cmd:
            return 0, "Successfully installed flask waitress\n" + self.echo
        if "nohup" in cmd:
            self.running = True
            return 0, "started\n"
        if "kill" in cmd and "echo stopped" in cmd:
            self.running = False
            return 0, "stopped\n"
        if "__healthz" in cmd:
            return (0, "") if (self.running and self.healthy) else (1, "URLError: Connection refused")
        if "echo absent" in cmd:
            return 0, ("absent" if not self.present else "running" if self.running else "stopped") + "\n"
        if cmd.startswith("tail -n"):
            return 0, " * Serving with waitress on http://0.0.0.0:8080\n"
        if cmd.startswith("rm -rf ~/clonjurer_deploy/"):
            self.present = False
            return 0, ""
        return 127, f"unexpected command: {cmd}"


class FakeWorld:
    def __init__(self):
        self.servers: dict = {}
        self.clients: list = []

    def add(self, host: str, key: str = "AAAAC3NzaC1lZDI1NTE5AAAAIOriginalKey", password: str = "s3cret-Pa55!"):
        srv = FakeServer(key, password)
        self.servers[host] = srv
        return srv

    def module(self):
        world = self

        class SFTP:
            def __init__(self, srv: FakeServer):
                self.srv = srv
                self.chan = FakeChannel()

            def get_channel(self):
                return self.chan

            def normalize(self, path):
                return self.srv.home

            def put(self, local, remote, callback: Callable = None):
                with open(local, "rb") as fh:
                    n = len(fh.read())
                if callback:
                    callback(n // 2, n)
                    callback(n, n)
                self.srv.uploads.append((local, remote, n))

            def close(self):
                pass

        class SSHClient:
            def __init__(self):
                self.policy = None
                self.srv: Optional[FakeServer] = None
                self.connect_kwargs: dict = {}
                self.loaded_system_keys = False
                self.closed = False
                world.clients.append(self)

            def load_system_host_keys(self, *a, **k):
                self.loaded_system_keys = True

            def set_missing_host_key_policy(self, policy):
                self.policy = policy

            def connect(self, hostname, port=22, username=None, password=None, pkey=None, **kw):
                self.connect_kwargs = dict(hostname=hostname, port=port, username=username, **kw)
                srv = world.servers.get(hostname)
                if srv is None:
                    raise socket.timeout("timed out")
                self.policy.missing_host_key(self, hostname, FakeKey(srv.key))
                srv.auth_attempts.append(password)
                if password != srv.password:
                    raise AuthenticationException("Authentication failed.")
                self.srv = srv

            def exec_command(self, cmd, timeout=None, get_pty=False):
                self.srv.commands.append((cmd, timeout))
                if self.srv.hang and self.srv.hang in cmd:
                    chan = FakeChannel(never_exits=True)
                else:
                    rc, out = self.srv.handle(cmd)
                    chan = FakeChannel(rc, out)
                return None, FakeStream(chan), FakeStream(chan)

            def open_sftp(self):
                return SFTP(self.srv)

            def close(self):
                self.closed = True

        class Module:
            pass
        mod = Module()
        mod.SSHClient = SSHClient
        mod.AuthenticationException = AuthenticationException
        return mod
