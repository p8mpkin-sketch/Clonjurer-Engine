"""
V4 自适应反爬与智能回源下载器。

设计哲学(与"让大模型猜 URL"的路线相反):
  活体渲染阶段浏览器已经成功加载过全部资源 —— 需要的不是猜测, 而是把
  同一个 BrowserContext 的合法会话(Cookie / UA / 指纹)复用到补抓请求上。

两级策略:
  L1  requests 直连(快, 线程池并发友好);
  L2  L1 返回 403/404/HTML 拦截页时, 降级为 Playwright APIRequestContext
      (与克隆渲染共享同一 BrowserContext, 继承真实会话 Cookie 与 UA)。
  若 L2 仍失败则如实上报, 交由上游"活体会话请求差集"与"渲染驱动回补"
  循环处理; 本模块不做任何 URL 幻觉式推测。

另提供 URL 路径参数规范化(;jsessionid=... 等)供引擎全局使用:
  /css/xx.css;jsessionid=14_abc  ->  扩展名 .css 而非 ".css;jsessionid=..."
"""
import time
from concurrent.futures import ThreadPoolExecutor, wait as _wait_futures
from urllib.parse import urlsplit

import requests

import config

# 视为"被源站拒绝"从而触发浏览器降级的 HTTP 状态码
_BROWSER_FALLBACK_STATUS = {401, 403, 404, 405, 406, 429, 451, 500, 502, 503}
# 批量会话补抓里值得再走浏览器会话串行重试的状态: 风控/鉴权/限流类(指纹或会话不同才被拒)。
# 404/410/5xx 在带同一会话 Cookie 的直连下仍失败, 浏览器会话几乎从不救回 —— 只对活体
# 确实加载成功过的 URL 才再试(那说明 404 是给非浏览器客户端的拒绝页)
_SESSION_RETRY_STATUS = {401, 403, 405, 406, 412, 429, 451, 503}


def strip_path_params(path: str) -> str:
    """剥离 URL path 中的矩阵参数(;jsessionid=... / ;v=123 等)。"""
    return (path or "").split(";", 1)[0]


def clean_ext(url: str) -> str:
    """基于剥离路径参数后的 path 提取小写扩展名(含点)。"""
    import os
    path = strip_path_params(urlsplit(url).path)
    return os.path.splitext(path)[1].lower()


class SmartDownloader:
    """单任务共用的自适应下载器(requests -> 同会话 Playwright 降级)。"""

    def __init__(self, base_headers: dict, log=None):
        self.headers = dict(base_headers or {})
        self._log = log or (lambda *_args: None)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self.headers.get("User-Agent") or config.DEFAULT_USER_AGENT,
        })
        self.browser_fallbacks = 0  # 统计: 触发浏览器降级的次数
        self.browser_rescues = 0    # 统计: 降级后成功救回的资源数

    # ------------------------------------------------------------------
    def fetch_direct(self, url: str, referer: str = "") -> dict:
        """L1: requests 直连。返回统一结构, 失败不含异常。"""
        out = {"url": url, "ok": False, "content": b"", "content_type": "",
               "error": "", "ext": ".bin", "is_css": False, "via": "requests"}
        headers = dict(self.headers)
        if referer:
            headers["Referer"] = referer
        try:
            resp = self._session.get(url, headers=headers,
                                     timeout=getattr(config, "REQUEST_CONNECT_READ", config.REQUEST_TIMEOUT))
        except requests.RequestException as exc:
            out["error"] = type(exc).__name__
            return out
        if resp.status_code >= 400:
            out["error"] = f"HTTP {resp.status_code}"
            out["status"] = resp.status_code
            return out
        content = resp.content or b""
        if not content:
            out["error"] = "空响应体"
            return out
        if len(content) > config.MAX_ASSET_BYTES:
            out["error"] = f"oversize ({len(content)} bytes)"
            return out
        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        out.update(ok=True, content=content, content_type=content_type,
                   status=resp.status_code, via="requests")
        return out

    # ------------------------------------------------------------------
    def fetch_via_browser(self, url: str, api_request_ctx, referer: str = "") -> dict:
        """L2: 通过 Playwright APIRequestContext 复用活体会话补抓。

        api_request_ctx: browser_context.request (与页面渲染共享 Cookie/UA)。
        """
        out = {"url": url, "ok": False, "content": b"", "content_type": "",
               "error": "", "ext": ".bin", "is_css": False, "via": "browser"}
        if api_request_ctx is None:
            out["error"] = "无可用浏览器会话"
            return out
        headers = {"Referer": referer or url.rsplit("/", 1)[0] + "/",
                   "Accept": "*/*"}
        try:
            resp = api_request_ctx.get(url, headers=headers,
                                       timeout=int(config.REQUEST_TIMEOUT * 1000),
                                       fail_on_status_code=False)
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"[:200]
            return out
        if resp.status >= 400:
            out["error"] = f"HTTP {resp.status}"
            return out
        try:
            content = resp.body()
        except Exception as exc:
            out["error"] = f"body 读取失败: {type(exc).__name__}"
            return out
        if not content:
            out["error"] = "空响应体"
            return out
        if len(content) > config.MAX_ASSET_BYTES:
            out["error"] = f"oversize ({len(content)} bytes)"
            return out
        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        out.update(ok=True, content=content, content_type=content_type,
                   via="browser")
        return out

    # ------------------------------------------------------------------
    def fetch_session_many(self, urls, api_request_ctx, referer: str = "", cookies=None,
                           deadline: float = None, browser_ok=None, workers: int = 12) -> dict:
        """批量会话补抓: 先带活体会话 Cookie 并发直连, 只把被风控/鉴权拒绝的再串行走浏览器会话。

        旧路径逐个 fetch_via_browser(Playwright 同步 API 只能串行, 单个 20s 超时): 活体差集/Mock 收割/
        渲染缺失回补/下载降级四处各自串行, 实测单站白等 10~30s(aliyun 活体回补 29s、douban 降级 26s)。
        并发直连带上同一 BrowserContext 的 Cookie 与 UA, 覆盖绝大多数「需要会话」的情形; 浏览器会话
        只留给指纹类拒绝(403/429 等)。deadline(monotonic 秒)到点后未完成的直连放弃, 串行重试也不再发起。
        browser_ok(url, out) -> bool: 直连失败后是否值得浏览器会话重试(默认按 _SESSION_RETRY_STATUS)。
        返回 {url: out}; 时限内没拿到结果的 URL 给 error="会话补抓时限"。"""
        urls = list(dict.fromkeys(u for u in urls if u))
        res = {}
        if not urls:
            return res
        sess = requests.Session()
        sess.headers.update(self._session.headers)
        for c in cookies or []:
            try:
                sess.cookies.set(c["name"], c.get("value") or "", domain=c.get("domain") or "",
                                 path=c.get("path") or "/")
            except Exception:   # noqa: BLE001 畸形 Cookie 跳过
                continue

        def one(url):
            out = {"url": url, "ok": False, "content": b"", "content_type": "",
                   "error": "", "ext": ".bin", "is_css": False, "via": "session"}
            headers = dict(self.headers)
            headers["Referer"] = referer or url.rsplit("/", 1)[0] + "/"
            headers.setdefault("Accept", "*/*")
            try:
                resp = sess.get(url, headers=headers,
                                timeout=getattr(config, "REQUEST_CONNECT_READ", config.REQUEST_TIMEOUT))
            except requests.RequestException as exc:
                out["error"] = type(exc).__name__
                return out
            if resp.status_code >= 400:
                out["error"] = f"HTTP {resp.status_code}"
                out["status"] = resp.status_code
                return out
            content = resp.content or b""
            if not content:
                out["error"] = "空响应体"
                return out
            if len(content) > config.MAX_ASSET_BYTES:
                out["error"] = f"oversize ({len(content)} bytes)"
                return out
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            out.update(ok=True, content=content, content_type=ctype, status=resp.status_code)
            return out

        ex = ThreadPoolExecutor(max_workers=max(1, min(workers, len(urls))), thread_name_prefix="sess-dl")
        try:
            futs = {ex.submit(one, u): u for u in urls}
            left = None if deadline is None else max(0.2, deadline - time.monotonic())
            done, _ = _wait_futures(list(futs), timeout=left)
            for f in done:
                try:
                    res[futs[f]] = f.result()
                except Exception as exc:   # noqa: BLE001
                    res[futs[f]] = {"url": futs[f], "ok": False, "error": type(exc).__name__}
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        if browser_ok is None:
            browser_ok = lambda _u, o: o.get("status") in _SESSION_RETRY_STATUS  # noqa: E731
        for url in urls:
            out = res.get(url)
            if out is not None and (out.get("ok") or not browser_ok(url, out)):
                continue
            if api_request_ctx is None or (deadline is not None and time.monotonic() > deadline):
                if out is None:
                    res[url] = {"url": url, "ok": False, "error": "会话补抓时限", "content": b""}
                continue
            self.browser_fallbacks += 1
            retry = self.fetch_via_browser(url, api_request_ctx, referer=referer)
            if retry.get("ok"):
                self.browser_rescues += 1
            res[url] = retry
        return res

    # ------------------------------------------------------------------
    def fetch(self, url: str, referer: str = "", api_request_ctx=None) -> dict:
        """完整两级策略。L1 失败且提供浏览器会话时自动降级。"""
        out = self.fetch_direct(url, referer=referer)
        if out["ok"]:
            return out
        status = out.get("status")
        needs_fallback = status in _BROWSER_FALLBACK_STATUS or "Client Error" in (out.get("error") or "")
        if not needs_fallback or api_request_ctx is None:
            return out
        self.browser_fallbacks += 1
        self._log("SMART-DL", f"直连被拒({out['error']}), 浏览器会话降级重试: {url[:110]}")
        retry = self.fetch_via_browser(url, api_request_ctx, referer=referer)
        if retry["ok"]:
            self.browser_rescues += 1
            self._log("SMART-DL", f"浏览器会话救回: {url[:110]}")
        return retry
