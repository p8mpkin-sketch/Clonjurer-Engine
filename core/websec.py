"""Web 层安全基座: 与 Flask 路由解耦的纯函数与小型内存状态。

- Host 白名单(防 DNS 重绑定): 监听在本机回环地址时, 只接受 localhost / 127.0.0.1 / [::1]
  与显式配置的主机名; 监听 0.0.0.0 等对外地址时默认不限制(可用 CLONER_ALLOWED_HOSTS 收紧)。
- 同源校验(防 CSRF): 改动类请求带 Origin / Referer 时, 主机必须是本站或白名单;
  浏览器跨站发起的 POST 一定带 Origin, 命令行等非浏览器客户端两者都不带, 照常放行。
- 来源 IP: 只有直连对端是回环/白名单代理时才采信 X-Forwarded-For, 防伪造审计 IP 与绕过限流。
- 登录失败计数(按来源 IP 的滑动窗口), 供验证码与限流判断。
"""
from __future__ import annotations

import ipaddress
import threading
import time
from collections import deque
from urllib.parse import urlsplit

LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}


def split_host(value: str) -> str:
    """'Host' 头 / netloc -> 小写主机名(去端口, IPv6 去方括号)。"""
    v = (value or "").strip().lower()
    if not v:
        return ""
    if v.startswith("["):
        end = v.find("]")
        return v[1:end] if end > 0 else v
    if v.count(":") == 1:
        return v.split(":", 1)[0]
    return v


def is_loopback_bind(bind_host: str) -> bool:
    h = split_host(bind_host)
    if h in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def parse_list(raw: str) -> set:
    return {split_host(x) for x in (raw or "").replace(";", ",").split(",") if x.strip()}


def host_allowed(host_header: str, bind_host: str, extra: set) -> bool:
    host = split_host(host_header)
    if not host:
        return False
    if extra and host in extra:
        return True
    if is_loopback_bind(bind_host):
        if host in LOOPBACK_NAMES:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
    return not extra  # 对外监听: 未配置白名单则不限制


def same_origin_ok(origin: str, referer: str, request_host: str, trusted: set) -> bool:
    """改动类请求的来源校验。Origin 优先; 没有 Origin 时看 Referer; 两者都没有放行(非浏览器)。
    只比较主机名+端口(不比协议): TLS 由反向代理终结时后端看到的是 http。"""
    own = (request_host or "").strip().lower()
    if origin:
        if origin == "null":  # 沙箱 iframe / data: 页面
            return False
        netloc = urlsplit(origin).netloc.lower()
    elif referer:
        netloc = urlsplit(referer).netloc.lower()
    else:
        return True
    if not netloc:
        return False
    return netloc == own or netloc in trusted


def origin_netlocs(raw: str) -> set:
    """'https://a.example.com, http://10.0.0.5:8899' -> {'a.example.com', '10.0.0.5:8899'}。
    按「主机:端口」精确比对: 预览端口与平台端口主机相同, 不能只比主机名。"""
    out = set()
    for item in (raw or "").replace(";", ",").split(","):
        item = item.strip().lower()
        if not item:
            continue
        out.add(urlsplit(item if "//" in item else "//" + item).netloc)
    return {x for x in out if x}


def client_ip(remote_addr: str, forwarded_for: str, trusted_proxies: set) -> str:
    """直连对端是回环地址或在代理白名单里, 才取 X-Forwarded-For 最右侧的非代理地址。"""
    remote = (remote_addr or "").strip()
    if not forwarded_for or not _is_trusted_proxy(remote, trusted_proxies):
        return remote or "local"
    hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    for hop in reversed(hops):  # 最右边是离我们最近的一跳; 跳过同样受信的代理
        if not _is_trusted_proxy(hop, trusted_proxies):
            return hop[:64]
    return (hops[0] if hops else remote)[:64] or "local"


def _is_trusted_proxy(addr: str, trusted: set) -> bool:
    if addr in trusted:
        return True
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


_METADATA_HOSTS = {"metadata", "metadata.google.internal", "metadata.goog", "instance-data",
                   "instance-data.ec2.internal", "metadata.azure.internal"}


def forbidden_target(host: str) -> str:
    """克隆/外呼目标的底线拦截: 云厂商元数据地址、链路本地、未指定与组播地址。
    返回拒绝原因, 空串表示放行。内网地址(10.x / 192.168.x 等)照常允许: 克隆内网站点是正常用法。"""
    import socket
    h = split_host(host).rstrip(".")
    if not h:
        return "缺少主机名"
    if h in _METADATA_HOSTS:
        return "不允许访问云服务器元数据地址"
    ip = None
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        # 浏览器认的 0xa9.0xfe.0xa9.0xfe / 2852039166 这类写法也要识别出来
        if all(c in "0123456789abcdefx." for c in h) and any(c.isdigit() for c in h):
            try:
                ip = ipaddress.ip_address(socket.inet_aton(h))
            except OSError:
                ip = None
    if ip is None:
        return ""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved \
            or str(ip) in ("fd00:ec2::254",):
        return "不允许访问链路本地/元数据等特殊地址"
    return ""


class FailureCounter:
    """按 key(来源 IP)记失败时间戳, 滑动窗口计数。进程内存, 重启清零即可。"""

    def __init__(self, window_s: int, max_keys: int = 20000):
        self.window_s = window_s
        self.max_keys = max_keys
        self._hits: dict = {}
        self._lock = threading.Lock()

    def _trim(self, q: deque, now: float) -> None:
        while q and q[0] <= now - self.window_s:
            q.popleft()

    def add(self, key: str) -> int:
        now = time.time()
        with self._lock:
            if len(self._hits) >= self.max_keys and key not in self._hits:
                for k in list(self._hits)[: self.max_keys // 10]:
                    self._hits.pop(k, None)
            q = self._hits.setdefault(key, deque())
            self._trim(q, now)
            q.append(now)
            return len(q)

    def count(self, key: str) -> int:
        now = time.time()
        with self._lock:
            q = self._hits.get(key)
            if not q:
                return 0
            self._trim(q, now)
            return len(q)

    def retry_after(self, key: str) -> int:
        with self._lock:
            q = self._hits.get(key)
            return max(1, int(q[0] + self.window_s - time.time())) if q else 0

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
