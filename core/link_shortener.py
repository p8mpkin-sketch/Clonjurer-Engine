"""短链服务层: 本站随机短码为主(不依赖外网, 每次点击都计数), 公共缩链服务可选; 结果经 models 方法落库。

本站短码: 每条短链都有一个 8 位随机短码(不可枚举), /s/<短码> 302 到目标并计一次点击。
  service=local 只生成本站短码; 选公共服务时, 公共短链指向本站 /s/<短码>(点击同样计数),
  平台对外地址是本机/内网(公共服务连不回来)时才退回直接缩目标地址, 并在 extras 里注明。

适配四家公共服务, 每家一个生成函数: 成功返回 (短链, extras), 失败抛 ShortenError。
  - tinyurl : GET  https://tinyurl.com/api-create.php?url=…      纯文本短链
  - clckru  : GET  https://clck.ru/--?url=…                      纯文本短链
  - lnkua   : POST https://lnk.ua/api/v1/link/create (Bearer public) JSON {"result": {"lnk", "key", "qr"}}
  - urlasia : POST https://url.asia/api/shorten                   JSON 或纯文本(不收纯 IP 目标)

外呼统一 15 秒超时; 响应只按纯文本 / JSON 解析, 解析出的短链必须是该服务自家域名下的
http(s) 绝对地址, 否则视为失败 —— 服务返回的 HTML 错误页 / 脚本片段不会被当成短链拼进库或页面。

两种生成方式:
  - shorten(prefer=<服务>)  指定服务, 失败自动顺延下一个可用服务; 记录里注明请求的服务与实际用的服务。
  - shorten_multi()         依次对每家可用服务各生成一条; 每家的成功 / 失败都各落一行。
失败行的 short_url 为空串, extras.ok = False, extras.error 为原因; 成功行 extras.ok = True。
"""
from __future__ import annotations

import ipaddress
import json
import secrets
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit

import requests

TIMEOUT = 15            # 所有外呼统一超时(秒)
MAX_URL_LEN = 2048
PREFLIGHT_TTL = 60      # 可用性预检结果缓存(秒), 免得每次打开页面都去敲四家服务
USER_AGENT = "Clonjurer-LinkShortener/1.0"
LOCAL = "local"         # 本站短码
CODE_LEN = 8            # 62^8 ≈ 2.2e14, 随机生成, 不可枚举
_CODE_ALPHABET = string.ascii_letters + string.digits

Extras = dict[str, Any]


class ShortenError(Exception):
    """单家服务生成失败(网络错误 / 非 2xx / 响应不是合法短链)。"""


class InvalidTarget(ValueError):
    """目标地址不合法(非 http(s) 绝对地址等)。"""


# ---------------------------------------------------------------------------
# 目标地址校验
# ---------------------------------------------------------------------------
def validate_target(url: Any) -> str:
    """目标必须是 http(s) 绝对地址; 返回去掉首尾空白后的地址, 不合法抛 InvalidTarget。"""
    if not isinstance(url, str):
        raise InvalidTarget("目标地址必须是字符串")
    url = url.strip()
    if not url:
        raise InvalidTarget("目标地址不能为空")
    if len(url) > MAX_URL_LEN:
        raise InvalidTarget(f"目标地址过长(上限 {MAX_URL_LEN} 字符)")
    if any(ch.isspace() for ch in url) or any(ord(ch) < 0x20 for ch in url):
        raise InvalidTarget("目标地址不能包含空白或控制字符")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - 非法端口在这里抛 ValueError
    except ValueError as exc:
        raise InvalidTarget(f"目标地址无法解析: {exc}") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise InvalidTarget("目标地址必须以 http:// 或 https:// 开头")
    if not host:
        raise InvalidTarget("目标地址缺少主机名")
    return url


def new_code() -> str:
    """本站短码: 8 位大小写字母 + 数字。纯数字的码留给老的 /s/<编号> 链接, 不发。"""
    while True:
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LEN))
        if not code.isdigit():
            return code


def valid_code(code: Any) -> bool:
    return isinstance(code, str) and 4 <= len(code) <= 32 and all(c in _CODE_ALPHABET for c in code)


def local_path(code: str) -> str:
    return f"/s/{code}"


def base_is_public(base_url: str) -> bool:
    """平台对外地址公共缩链服务能不能连回来: 本机名 / 回环 / 内网 / 链路本地 IP 视为不能。"""
    try:
        host = (urlsplit(base_url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host or host == "localhost" or host.endswith(".localhost") or host.endswith(".local") \
            or "." not in host and ":" not in host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified)


def host_is_ip(url: str) -> bool:
    """目标 host 是否为纯 IP(v4 / v6)。"""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 外呼与响应解析
# ---------------------------------------------------------------------------
def _request(method: str, url: str, **kwargs: Any) -> requests.Response:
    headers = {"User-Agent": USER_AGENT, **(kwargs.pop("headers", None) or {})}
    try:
        return requests.request(method, url, timeout=TIMEOUT, headers=headers, **kwargs)
    except requests.Timeout:
        raise ShortenError(f"请求超时({TIMEOUT}s)") from None
    except requests.RequestException as exc:
        raise ShortenError(f"网络错误: {type(exc).__name__}") from None


def _snippet(text: str, n: int = 120) -> str:
    """错误信息里引用的响应片段: 压成一行、截断, 只作留痕文本(前端按文本渲染)。"""
    return " ".join((text or "").split())[:n]


def _clean_short(value: Any, hosts: tuple[str, ...]) -> str:
    """服务返回的短链必须是自家域名下的 http(s) 绝对地址, 不含空白 / 引号 / 尖括号。"""
    if not isinstance(value, str):
        raise ShortenError("响应里没有短链")
    value = value.strip()
    if not value or len(value) > 300 or any(ch.isspace() or ch in "<>\"'`" for ch in value):
        raise ShortenError(f"响应不是合法短链: {_snippet(value)}")
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
    except ValueError:
        raise ShortenError(f"响应不是合法短链: {_snippet(value)}") from None
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise ShortenError(f"响应不是合法短链: {_snippet(value)}")
    if not any(host == h or host.endswith("." + h) for h in hosts):
        raise ShortenError(f"响应短链不属于该服务({host})")
    return value


_JSON_KEYS = ("lnk", "short_url", "shortUrl", "shorturl", "short_link", "shortLink",
              "short", "link", "url")


def _json_short(data: Any) -> Optional[str]:
    """在 JSON 响应里找短链字段(兼容 result / data 包一层)。"""
    if isinstance(data, str):
        return data
    if not isinstance(data, dict):
        return None
    for key in _JSON_KEYS:
        if isinstance(data.get(key), str):
            return data[key]
    for wrap in ("result", "data"):
        inner = data.get(wrap)
        if isinstance(inner, (dict, str)):
            found = _json_short(inner)
            if found:
                return found
    return None


def _json_error(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("error", "message", "msg", "errors", "detail"):
            if data.get(key):
                val = data[key]
                return _snippet(val if isinstance(val, str) else json.dumps(val, ensure_ascii=False))
    return ""


def parse_response(resp: requests.Response, hosts: tuple[str, ...]) -> tuple[str, Extras]:
    """按纯文本或 JSON 解析服务响应 -> (短链, extras)。非 2xx / 无短链抛 ShortenError。"""
    text = resp.text or ""
    data: Any = None
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
    if not 200 <= resp.status_code < 300:
        reason = _json_error(data) or _snippet(text) or resp.reason or ""
        raise ShortenError(f"HTTP {resp.status_code}" + (f": {reason}" if reason else ""))
    if data is not None:
        short = _json_short(data)
        if not short:
            raise ShortenError(_json_error(data) or f"响应里没有短链: {_snippet(text)}")
        extras: Extras = {}
        inner = data.get("result") if isinstance(data, dict) else None
        if isinstance(inner, dict):
            for key in ("key", "qr"):
                if isinstance(inner.get(key), str):
                    extras[key] = inner[key]
        return _clean_short(short, hosts), extras
    return _clean_short(text, hosts), {}


# ---------------------------------------------------------------------------
# 四家服务
# ---------------------------------------------------------------------------
def gen_tinyurl(target: str) -> tuple[str, Extras]:
    resp = _request("GET", "https://tinyurl.com/api-create.php", params={"url": target})
    return parse_response(resp, ("tinyurl.com",))


def gen_clckru(target: str) -> tuple[str, Extras]:
    resp = _request("GET", "https://clck.ru/--", params={"url": target})
    return parse_response(resp, ("clck.ru",))


def gen_lnkua(target: str) -> tuple[str, Extras]:
    resp = _request("POST", "https://lnk.ua/api/v1/link/create", data={"link": target},
                    headers={"Authorization": "Bearer public", "Accept": "application/json"})
    return parse_response(resp, ("lnk.ua",))


def gen_urlasia(target: str) -> tuple[str, Extras]:
    if host_is_ip(target):  # 双保险: 选服务时已排除, 直接调用也不发出去
        raise ShortenError("url.asia 不接受纯 IP 目标")
    resp = _request("POST", "https://url.asia/api/shorten", data={"url": target},
                    headers={"Accept": "application/json"})
    return parse_response(resp, ("url.asia",))


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    home: str               # 预检地址
    supports_ip: bool       # 是否接受纯 IP 目标
    create: Callable[[str], tuple[str, Extras]]


PROVIDERS: dict[str, Provider] = {
    "tinyurl": Provider("tinyurl", "TinyURL", "https://tinyurl.com/", True, gen_tinyurl),
    "clckru": Provider("clckru", "clck.ru", "https://clck.ru/", True, gen_clckru),
    "lnkua": Provider("lnkua", "lnk.ua", "https://lnk.ua/", True, gen_lnkua),
    "urlasia": Provider("urlasia", "url.asia", "https://url.asia/", False, gen_urlasia),
}
ORDER = tuple(PROVIDERS)  # 公共服务: 失败顺延的顺序
SERVICE_CHOICES = (LOCAL, "all") + ORDER   # local = 只要本站短码(默认); all = 本站 + 每家公共服务各一条


def normalize_service(value: Any, default: str = LOCAL) -> str:
    """服务名归一: 接受 key 或展示名(clck.ru / lnk.ua / url.asia / 本站); 未知抛 ValueError。"""
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    alias = {"clck.ru": "clckru", "lnk.ua": "lnkua", "url.asia": "urlasia", "auto": "all",
             "本站": LOCAL, "site": LOCAL}
    raw = alias.get(raw, raw)
    if raw not in SERVICE_CHOICES:
        raise ValueError(f"未知短链服务 {value!r}, 可选: {', '.join(SERVICE_CHOICES)}")
    return raw


def usable_services(target: str) -> list[str]:
    """该目标可用的服务(按 ORDER): 纯 IP 目标排除不收 IP 的服务。"""
    ip = host_is_ip(target)
    return [k for k in ORDER if PROVIDERS[k].supports_ip or not ip]


def skipped_services(target: str) -> list[dict]:
    if not host_is_ip(target):
        return []
    return [{"service": k, "ok": False, "skipped": True, "error": f"{PROVIDERS[k].name} 不接受纯 IP 目标"}
            for k in ORDER if not PROVIDERS[k].supports_ip]


def fallback_chain(target: str, prefer: str) -> list[str]:
    """指定服务优先, 之后按 ORDER 顺延其余可用服务; prefer 不可用(如 IP 目标选了 url.asia)则从头顺延。"""
    usable = usable_services(target)
    if prefer in usable:
        i = usable.index(prefer)
        return usable[i:] + usable[:i]
    return usable


def _try(service: str, target: str) -> tuple[Optional[str], Extras, str]:
    try:
        short, extras = PROVIDERS[service].create(target)
        return short, extras, ""
    except ShortenError as exc:
        return None, {}, str(exc) or "生成失败"
    except Exception as exc:  # noqa: BLE001 - 单家服务的任何意外都只算这一家失败, 不影响顺延
        return None, {}, f"内部错误: {type(exc).__name__}"


# ---------------------------------------------------------------------------
# 生成入口(落库)
# ---------------------------------------------------------------------------
def _public_source(target: str, code: str, base_url: str) -> tuple[str, bool]:
    """交给公共服务缩的地址: 平台对外地址可达时缩本站 /s/<短码>(点击计数), 否则直接缩目标。"""
    if base_url and base_is_public(base_url):
        return base_url.rstrip("/") + local_path(code), True
    return target, False


def _create(db: Any, task_id, target: str, service: str, short: str, label: str, record: Extras,
            code: str, link_task_id, workspace_id) -> int:
    return db.create_short_link(task_id, target, service, short, label, record, link_task_id=link_task_id,
                                workspace_id=workspace_id, code=code)


def _shorten_local(db: Any, target: str, *, label: str, task_id, link_task_id, workspace_id) -> dict:
    code = new_code()
    record = {"ok": True, "requested": LOCAL, "attempts": [{"service": LOCAL, "ok": True}]}
    link_id = _create(db, task_id, target, LOCAL, local_path(code), label, record, code,
                      link_task_id, workspace_id)
    return {"ok": True, "link_id": link_id, "service": LOCAL, "short_url": local_path(code), "code": code,
            "requested": LOCAL, "fallback": False, "attempts": record["attempts"]}


def shorten(db: Any, target_url: str, prefer: str = LOCAL, *, label: str = "",
            task_id: Optional[int] = None, link_task_id: Optional[int] = None,
            workspace_id: Optional[int] = None, base_url: str = "") -> dict:
    """生成一条短链。prefer=local(默认)只发本站短码, 不外呼; prefer=<公共服务> 先试该家, 失败按 ORDER
    顺延; prefer=all 从第一家公共服务开始顺延。每行都带本站短码, 公共短链(平台可达时)指向它。

    成功落一行(service = 实际用的服务, extras.requested / fallback / attempts / via_local 留痕);
    公共服务全部失败也落一行(short_url 为空, extras.ok=False)。返回 {ok, link_id, service, short_url, attempts}。"""
    target = validate_target(target_url)
    prefer = normalize_service(prefer)
    if prefer == LOCAL:
        return _shorten_local(db, target, label=label, task_id=task_id, link_task_id=link_task_id,
                              workspace_id=workspace_id)
    code = new_code()
    source, via_local = _public_source(target, code, base_url)
    chain = fallback_chain(source, prefer)
    attempts: list[dict] = []
    if prefer != "all" and prefer not in chain:  # 如 IP 目标指定了 url.asia: 记一笔跳过, 顺延其余服务
        attempts.append({"service": prefer, "ok": False, "skipped": True,
                         "error": f"{PROVIDERS[prefer].name} 不接受纯 IP 目标"})
    for service in chain:
        short, extras, error = _try(service, source)
        if short is None:
            attempts.append({"service": service, "ok": False, "error": error})
            continue
        attempts.append({"service": service, "ok": True})
        record = {**extras, "ok": True, "requested": prefer, "via_local": via_local,
                  "fallback": prefer != "all" and service != prefer, "attempts": attempts}
        link_id = _create(db, task_id, target, service, short, label, record, code, link_task_id, workspace_id)
        return {"ok": True, "link_id": link_id, "service": service, "short_url": short, "code": code,
                "requested": prefer, "fallback": record["fallback"], "attempts": attempts}
    error = "全部服务生成失败" if chain else "没有可用的短链服务"
    record = {"ok": False, "requested": prefer, "error": error, "attempts": attempts}
    link_id = _create(db, task_id, target, prefer, "", label, record, code, link_task_id, workspace_id)
    return {"ok": False, "link_id": link_id, "service": prefer, "short_url": "",
            "requested": prefer, "error": error, "attempts": attempts}


def shorten_multi(db: Any, target_url: str, *, label: str = "", task_id: Optional[int] = None,
                  link_task_id: Optional[int] = None, skip: Iterable[str] = (),
                  workspace_id: Optional[int] = None, base_url: str = "") -> dict:
    """prefer=all: 先发一条本站短码, 再依次对每家可用公共服务各生成一条; 每家成功 / 失败各落一行。

    skip: 不再重复生成的服务(如任务里该服务已有成功短链)。返回 {results, created, failed}。"""
    target = validate_target(target_url)
    skip = set(skip)
    results: list[dict] = []
    if LOCAL in skip:
        results.append({"service": LOCAL, "ok": True, "skipped": True, "error": "已有本站短码, 未重复生成"})
    else:
        one = _shorten_local(db, target, label=label, task_id=task_id, link_task_id=link_task_id,
                             workspace_id=workspace_id)
        results.append({"service": LOCAL, "ok": True, "short_url": one["short_url"], "link_id": one["link_id"]})
    results.extend(skipped_services(target))
    for service in usable_services(target):
        if service in skip:
            results.append({"service": service, "ok": True, "skipped": True, "error": "已有成功短链, 未重复生成"})
            continue
        code = new_code()
        source, via_local = _public_source(target, code, base_url)
        short, extras, error = _try(service, source)
        if short is None:
            record = {"ok": False, "requested": "all", "error": error}
            link_id = _create(db, task_id, target, service, "", label, record, code, link_task_id, workspace_id)
            results.append({"service": service, "ok": False, "error": error, "link_id": link_id})
        else:
            record = {**extras, "ok": True, "requested": "all", "via_local": via_local}
            link_id = _create(db, task_id, target, service, short, label, record, code, link_task_id, workspace_id)
            results.append({"service": service, "ok": True, "short_url": short, "link_id": link_id})
    created = sum(1 for r in results if r["ok"] and not r.get("skipped"))
    failed = sum(1 for r in results if not r["ok"] and not r.get("skipped"))
    return {"results": results, "created": created, "failed": failed}


def generate_for_task(db: Any, task_row_id: int, base_url: str = "") -> dict:
    """按短链任务的偏好服务生成: 先清掉该任务上一轮的失败记录, 再生成, 最后刷新任务状态。

    service=all 时已成功过的服务(含本站短码)不重复生成; 其余每次生成一条新短链(公共服务失败顺延)。"""
    task = db.get_short_task(task_row_id)
    if task is None:
        raise LookupError(f"短链任务 #{task_row_id} 不存在")
    db.delete_failed_links_of_short_task(task_row_id)
    label = task.get("name") or ""
    clone_id = task.get("clone_task_id")
    wid = task.get("workspace_id")
    try:
        if (task.get("service") or LOCAL) == "all":
            done = {l["service"] for l in db.list_links_of_short_task(task_row_id) if l.get("short_url")}
            res = shorten_multi(db, task["target_url"], label=label, task_id=clone_id,
                                link_task_id=task_row_id, skip=done, workspace_id=wid, base_url=base_url)
        else:
            one = shorten(db, task["target_url"], task["service"], label=label, task_id=clone_id,
                          link_task_id=task_row_id, workspace_id=wid, base_url=base_url)
            res = {"results": [one], "created": int(one["ok"]), "failed": int(not one["ok"])}
    except InvalidTarget as exc:
        res = {"results": [], "created": 0, "failed": 0, "error": str(exc)}
        db.update_short_task(task_row_id, status="failed")
        return res
    db.refresh_short_task_status(task_row_id)
    return res


# ---------------------------------------------------------------------------
# 可用性预检
# ---------------------------------------------------------------------------
_preflight_lock = threading.Lock()
_preflight_cache: dict[str, Any] = {"at": 0.0, "items": None}


def _probe(p: Provider) -> dict:
    out = {"key": p.key, "name": p.name, "home": p.home, "supports_ip": p.supports_ip,
           "available": False, "status": None, "latency_ms": None, "error": ""}
    t0 = time.monotonic()
    try:
        resp = _request("GET", p.home, allow_redirects=False)
        out["status"] = resp.status_code
        out["available"] = resp.status_code < 500
        if not out["available"]:
            out["error"] = f"HTTP {resp.status_code}"
    except ShortenError as exc:
        out["error"] = str(exc)
    out["latency_ms"] = int((time.monotonic() - t0) * 1000)
    return out


def preflight(force: bool = False) -> list[dict]:
    """四家服务的可用性预检(并发探测首页, 能连上且非 5xx 视为可用); 结果缓存 PREFLIGHT_TTL 秒。"""
    with _preflight_lock:
        cached = _preflight_cache["items"]
        if not force and cached is not None and time.monotonic() - _preflight_cache["at"] < PREFLIGHT_TTL:
            return [dict(x) for x in cached]
    with ThreadPoolExecutor(max_workers=len(PROVIDERS)) as ex:
        items = list(ex.map(_probe, PROVIDERS.values()))
    with _preflight_lock:
        _preflight_cache.update(at=time.monotonic(), items=items)
    return [dict(x) for x in items]


def provider_meta() -> list[dict]:
    """不外呼的公共服务清单(名称 / 是否收 IP), 给表单下拉用(本站短码不在其中, 总是可用)。"""
    return [{"key": p.key, "name": p.name, "supports_ip": p.supports_ip} for p in PROVIDERS.values()]
