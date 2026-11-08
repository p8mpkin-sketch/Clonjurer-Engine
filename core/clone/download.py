"""ClonerEngine 资源落地: CAS 并发下载、会话补抓、活体差集/Mock 收割/渲染缺失回补(自 cloner_engine.py 按职责拆出的 mixin, 方法体未改)。"""
# flake8: noqa  —— 导入表与 cloner_engine.py 一致, 便于方法原样搬迁
import base64
import hashlib
import html as _html
import io
import json
import math
import os
import posixpath
import re
import struct
import threading
import time
import zlib
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as _wait_futures
from contextlib import suppress
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

try:  # 评分向量化(可选依赖): 缺失时回落纯 Python 实现, 结果一致
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None

import config
from core import ai_mock_generator, ai_vision_repair, flask_generator, smart_downloader
from core import pattern_detector, strategy_packs
from core import pixel_calibrator

from core.clone.js import (  # noqa: F401  纯搬迁后的再导出, 外部 import 路径不变
    AD_RECTS_JS,
    ALT_EXTRACT_JS,
    ALT_INJECT_JS,
    ANIM_ALIGN_JS,
    ANIM_PHASE_JS,
    BASELINE_EAGER_JS,
    BLOB_FETCH_JS,
    CLONE_IMG_DONE_JS,
    CLONE_IMG_EAGER_JS,
    COLLAPSED_PROBE_JS,
    COLLAPSE_REVERT_JS,
    COLLECT_ASSETS_JS,
    COLLECT_LINKS_JS,
    CV_ATTR_FN,
    CV_EMIT_JS,
    CV_PATHS_JS,
    CV_PATH_FN,
    CV_QUIET_JS,
    CV_RECORD_JS,
    DEEP_FREEZE_JS,
    WAIT_SHEETS_JS,
    FONT_BAKE_JS,
    FORM_ACTIVATE_JS,
    IFRAME_EXTRACT_JS,
    IFRAME_SRCS_JS,
    IMG_PENDING_JS,
    INLINE_FLUID_JS,
    LAYOUT_MAP_JS,
    LAZY_JS,
    MARK_HIDDEN_FRAMES_JS,
    MARK_LOADED_IMGS_JS,
    MEDIA_SHOT_APPLY_JS,
    MEDIA_SHOT_JS,
    OUTER_HTML_JS,
    PAGE_PROBE_JS,
    RAF_FREEZE_JS,
    REMOVE_SCRIPTS_JS,
    RESPONSIVE_CSS,
    RUNTIME_FP_JS,
    SCOPED_BAKE_FN,
    SCORE_TIMER_GUARD_JS,
    SETTLE_CLEANUP_JS,
    SETTLE_INSTALL_JS,
    SETTLE_READY_JS,
    SHADOW_SERIALIZE_JS,
    SHOT_DIAG_JS,
    STORAGE_SNAPSHOT_JS,
    STYLE_TEXTS_JS,
    SUBTREE_SIZE_JS,
    TEXT_EXTRACT_JS,
    TIME_FREEZE_JS,
    TRACKER_OBSERVE_JS,
    UPDATE_PROMPT_CLICK_JS,
    UPDATE_PROMPT_PROBE_JS,
    VIDEO_FRAME_READY_JS,
    VIDEO_SEEK_DONE_JS,
    VIDEO_SEEK_JS,
    WAAPI_BAKE_JS,
)
from core.clone.support import (  # noqa: F401  纯搬迁后的再导出, 外部 import 路径不变
    beacon_like,
    ASSET_RESOURCE_TYPES,
    ASSET_SKIP_PREFIXES,
    BLOB_INLINE_MAX,
    BLOB_URL_RE,
    CSS_IMAGE_SET_RE,
    CSS_IMPORT_RE,
    CSS_URL_RE,
    CloneUnresponsive,
    FIRST_SEGMENT_WEIGHT,
    HTML_INTERCEPT_TYPES,
    INTERACTION_SIM_TEMPLATE,
    REPLAY_DEDUPE_JS,
    JS_NAV_GUARD_SCRIPT,
    JS_NAV_OPEN_RE,
    JS_PARENT_LOC_RE,
    JS_TOP_ASSIGN_RE,
    JS_TOP_HREF_RE,
    LAYOUT_FIX_MAX,
    LAYOUT_MAP_MAX,
    PROTECTED_CATEGORIES,
    RESPONSIVE_META,
    RUNTIME_ASSET_CTYPES,
    RUNTIME_ASSET_EXT_RE,
    SCORE_MAX_HEIGHT,
    SCORE_SEGMENT,
    SCORE_WIDTH,
    SHOT_MAX_TEXTURE,
    SMS_SIM_SHIM_SCRIPT,
    SPA_DISGUISE_TEMPLATE,
    STATE_RESTORE_TEMPLATE,
    TargetUnreachable,
    _BAKE_STYLE_RE,
    _BASE_TAG_RE,
    _BLOCK_MAX_CHARS,
    _BLOCK_STRONG,
    _BLOCK_WEAK,
    _CSS_FOLD_TRAPS,
    _CV_BAND_RE,
    _DIFF_BAND_MIN_ROWS,
    _DIFF_DIM_LUT,
    _DIFF_MASK_LUT,
    _FIT_CACHE,
    _FIT_CACHE_LOCK,
    _FIT_CACHE_MAX,
    _FIT_HINTS,
    _GENERIC_NAME_RE,
    _HIDDEN_FRAME_TAG_RE,
    _LINK_MEDIA_RE,
    _LIVE_BODY_TYPES,
    _LocalPreviewServer,
    _MEDIA_PRELUDE_RE,
    _META_CHARSET_RE,
    _PRIO_EXT,
    _REF_PREP,
    _REF_PREP_LOCK,
    _REF_PREP_MAX,
    _REPL_CACHE,
    _SCORE_POOL,
    _SCORE_POOL_LOCK,
    _SCRIPT_TAG_RE,
    _SRCSET_URL_RE,
    _STATIC_ATTR_RE,
    _STRICT_ABS_URL_RE,
    _SandboxHandler,
    _TAG_SRC_RE,
    _TRANSIENT_ERROR_MARKS,
    _VIEWPORT_META_RE,
    _WIDTH_FEAT_RE,
    _WORD_BYTES,
    _WORD_TABLE,
    _anchored_sub,
    _assemble_diff,
    _asset_priority,
    _blank_hidden_frames,
    _block_ncc,
    _block_ncc_arr,
    _block_ncc_np,
    _block_ncc_py,
    _ci_finditer,
    _css_lowered,
    _decode_rgb,
    _diff_band_deflate,
    _diff_image,
    _distinctive_asset_name,
    _edge_iou,
    _edge_iou_from,
    _edge_masks,
    _f_from_counts,
    _fit_padded,
    _fit_width,
    _fmt_size,
    _frame_bounced,
    _html_escaped_pairs,
    _inject_responsive,
    _is_runtime_asset,
    _load_fit_pair,
    _mask_rects,
    _match_live_frame,
    _media_breakpoints,
    _ncc_blocks,
    _ncc_finish,
    _ncc_finish_segments,
    _ncc_prep,
    _ncc_prep_segments,
    _normalize_page_dir,
    _pad_to,
    _parse_best_srcset,
    _png_bytes,
    _png_chunk,
    _ref_prep,
    _replacement_matcher,
    _safe_json,
    _save_png_fast,
    _score_pool,
    _seg_f64,
    _sha1,
    _short_text,
    _split_scheme_netloc_path,
    _src_pad_heights,
    _srcset_entries,
    _stat_hint,
    _static_refs,
    _strip_hydration_html,
    _sub_filtered,
    _text_len,
    _text_tokens,
    _token_counts,
    _token_sig,
    _trie_regex,
    _urljoin,
    _utf8_charset,
    _write_diff,
    apply_replacements,
    band_wrap,
    capture_band_floor,
    compute_visual_similarity,
    detect_blocked_page,
    layout_fix_css,
    os_ext,
    parse_css_refs,
    fontface_plan,
    rewrite_js_navigation,
    scope_bake_css,
    scope_viewport_css,
    slugify,
    text_f_score,
    text_overlap_score,
    write_diff_preview,
)



from core.clone.support import (  # noqa: E402  手机版(按 UA 分发)
    PHONE_EDITION_FILE, inject_edition_router, merge_static_tree, separate_phone_edition,
)


class DownloadMixin:

    # ------------------------------------------------------------------
    def _normalize_out(self, out: dict) -> dict:
        """统一规范化下载结果: 清洗扩展名 + 防污染校验。"""
        if not out.get("ok"):
            return out
        content_type = out.get("content_type") or ""
        path_ext = os_ext(urlsplit(out["url"]).path)
        # 路径末段里的点不一定是扩展名(base64/签名令牌 ".hcuwe%2fql...%3d"): 不像扩展名的按
        # content-type 回落。否则本地文件名带 %2f/%3d, 服务端解码后找不到文件, 图片 404 退回
        # width/height 属性尺寸, 撑破窄屏栅格(手机横向溢出)
        # (只认带 % 或超长的: 更宽的形状规则曾让 cgnpc 窄屏样式表的 CSSOM 副本丢失, 见 PR 说明)
        if path_ext and ("%" in path_ext or len(path_ext) > 12):
            path_ext = ""
        if not path_ext:
            path_ext = config.CONTENT_TYPE_FALLBACK.get(content_type, ".bin")
        category = config.EXT_CATEGORIES.get(path_ext, "")
        if category in PROTECTED_CATEGORIES and content_type in HTML_INTERCEPT_TYPES:
            out = dict(out)
            out["ok"] = False
            out["error"] = f"content-type 拦截页 ({content_type})"
            return out
        # 内容级防毒: SPA fallback 可能伪装 content-type(或缺失), 对受保护
        # 类别(js/css 等)直接看字节 —— 真实 JS/CSS 不会以 <!DOCTYPE/<html 开头
        if category in PROTECTED_CATEGORIES and out.get("content"):
            head = bytes(out["content"][:512]).lstrip()
            if head[:9] == b"<!DOCTYPE" or head[:5].lower() == b"<html":
                out = dict(out)
                out["ok"] = False
                out["error"] = "SPA fallback HTML(内容级拦截)"
                return out
        out = dict(out)
        out["ext"] = path_ext if path_ext.startswith(".") else ".bin"
        out["is_css"] = ("css" in content_type or path_ext == ".css")
        return out


    # ------------------------------------------------------------------
    def _font_plan_add(self, css_text: str, base_url: str):
        """累积 @font-face 下载计划(见 support.fontface_plan): 任一样式表把某 URL 当首选即必下。"""
        if not getattr(config, "FONT_PLAN", True):
            return
        with suppress(Exception):
            needed, skip, fb_of = fontface_plan(css_text, base_url, self._ff_cps)
            self._ff_needed |= needed
            self._ff_skip |= skip
            for fb, prim in fb_of.items():
                lst = self._ff_fallbacks.setdefault(prim, [])
                if fb not in lst:
                    lst.append(fb)

    def _materialize(self, assets: set, origin: str, ctx=None, results: dict = None):
        """CAS 并发下载(L1 requests 线程池) + 失败 URL 浏览器会话降级(L2)
        + CSS 深度递归。results 可跨调用累积(回补循环复用)。"""
        results = results if results is not None else {}
        jobs = deque()
        # 样式表独立队列、永远先发: 样式内引用(活体字体切片可达数百个)插队时只能排在
        # 剩余样式表之后 —— mi 曾被首个 CSS 的 300+ 字体切片吃光名额, 主样式表没进队列
        css_jobs = deque()
        page_netloc = urlsplit(self._target).netloc
        seeded = set()
        for url in results:
            seeded.update(self._variants(url, page_netloc))
        # 名额优先级: 样式/字体/图片/媒体先排, 脚本最后(单任务条目上限下, 字母序曾让
        # 数百个同源 JS 分包吃光名额而图片 0 张; 且应用脚本多半会被水合剥离)
        live = getattr(self, "_live_seen", None) or {}
        font_skipped = [0]

        def _font_unused(u):
            # @font-face 计划判定浏览器不会请求的字体(兜底格式/字符不在页面里的切片): 不排队。
            # 不进 seen —— 之后别的样式表若把它当首选, 照常排队
            if u in live or u not in self._ff_skip or u in self._ff_needed:
                return False
            font_skipped[0] += 1
            return True

        for url in sorted(assets, key=lambda u: _asset_priority(u, live)):
            if url.startswith(("http://", "https://")) and url not in seeded:
                if _font_unused(url):
                    continue
                (css_jobs if _asset_priority(url)[1] == 0 else jobs).append(
                    {"url": url, "referer": origin + "/"})
        seen = set(seeded)
        css_sources, css_refs = {}, {}
        failed_for_browser = []
        total, downloaded = len(jobs) + len(css_jobs), 0
        executor = ThreadPoolExecutor(max_workers=config.DOWNLOAD_WORKERS)
        in_flight = {}   # future -> job
        max_inflight = config.DOWNLOAD_WORKERS * 2

        # 主机 -> 网络层失败次数(无 HTTP 状态: SSL/超时/连接错误); 任务级, 跨回补调用累积
        host_fail = self.__dict__.setdefault("_host_fail", {})
        tripped = self.__dict__.setdefault("_host_tripped", set())

        def _pump():
            # 流式调度: 队列里有活、在飞的不满就提交; 不再按 32 个一批等最慢者
            while (css_jobs or jobs) and len(in_flight) < max_inflight:
                job = (css_jobs or jobs).popleft()
                if job["url"] in seen:
                    continue
                if (urlsplit(job["url"]).hostname or "") in tripped:
                    seen.add(job["url"])
                    continue   # 熔断: 该主机网络层已连续失败, 余下请求不再空等超时
                seen.add(job["url"])
                in_flight[executor.submit(self._fetch_asset, job)] = job

        try:
            _pump()
            while in_flight and len(results) < config.MAX_ASSETS_PER_TASK:
                done, _pending = _wait_futures(list(in_flight), return_when=FIRST_COMPLETED)
                for fut in done:
                    job = in_flight.pop(fut, None)
                    if job is None:
                        continue   # 已被主机熔断放弃
                    if len(results) >= config.MAX_ASSETS_PER_TASK:
                        continue
                    try:
                        out = fut.result()
                    except Exception as exc:   # noqa: BLE001 下载线程兜底
                        out = {"url": job["url"], "ok": False, "error": type(exc).__name__}
                    out = self._normalize_out(out)
                    if not out["ok"]:
                        h0 = urlsplit(job["url"]).hostname or ""
                        if (not out.get("status") and not job.get("retry") and h0 not in tripped
                                and str(out.get("error") or "").endswith(("SSLError", "ConnectionError",
                                                                          "ChunkedEncodingError"))):
                            # 网络层瞬时错误(SSL EOF/连接重置)重排一次: 这类失败在代理/跨境链路上
                            # 是随机的, 不重试的话该资源这一轮就缺失(kotlinlang 平板图 SSLError 后
                            # 未本地化, 克隆里 <source> 指向 404, 平板分 95→51)
                            seen.discard(job["url"])
                            jobs.append(dict(job, retry=1))
                            _pump()
                            continue
                        self.log("WARN", f"跳过 {job['url']}: {out['error']}")
                        # 首选字体下不来: 浏览器会往 src 列表后面走, 兜底份补进队列
                        for fb in self._ff_fallbacks.get(job["url"], ()):
                            self._ff_needed.add(fb)
                            if fb not in seen:
                                jobs.appendleft({"url": fb, "referer": job.get("referer") or origin + "/"})
                                total += 1
                        if out.get("status"):
                            self._dead_urls.add(job["url"])   # 源站明确 4xx/5xx: 交付侧不再回源
                        if out.get("status") and ctx is not None:
                            failed_for_browser.append(job)
                        elif not out.get("status") and str(out.get("error") or "").endswith(
                                ("Error", "Timeout", "TimeoutError")):
                            h = urlsplit(job["url"]).hostname or ""
                            host_fail[h] = host_fail.get(h, 0) + 1
                            if host_fail[h] >= config.HOST_FAIL_TRIP and h not in tripped:
                                tripped.add(h)
                                # 已在飞的同主机请求也不再等(否则仍要逐个等满读超时)
                                dropped = 0
                                for f2, j2 in list(in_flight.items()):
                                    if (urlsplit(j2["url"]).hostname or "") == h:
                                        f2.cancel()
                                        in_flight.pop(f2, None)
                                        dropped += 1
                                self.log("SMART-DL", f"主机熔断: {h} 网络层连续失败 "
                                                     f"{host_fail[h]} 次, 余下请求跳过"
                                                     f"(放弃在飞 {dropped} 个)")
                        continue
                    info = self._store(out)
                    results[out["url"]] = info
                    downloaded += 1
                    if downloaded % 8 == 0 or not (jobs or css_jobs):
                        done_ratio = downloaded / max(total, 1)
                        self._set(progress=min(58, 30 + int(24 * done_ratio)))
                    tag = "CAS-HIT" if info["hit"] else "CAS-MISS"
                    self.log(tag, f"{info['cat']}/{info['name']} "
                                  f"({_fmt_size(info['size'])}) <- {out['url'][:110]}")
                    if out["is_css"]:
                        text = out["content"].decode("utf-8", "replace")
                        css_sources[out["url"]] = text
                        refs = parse_css_refs(text, out["url"])
                        css_refs[out["url"]] = refs
                        self._font_plan_add(text, out["url"])
                        for raw, absolute in refs:
                            if absolute not in seen and not _font_unused(absolute):
                                # 样式内引用: 活体加载过的或图片插队; 未用到的字体切片排队尾
                                job2 = {"url": absolute, "referer": out["url"]}
                                pr = _asset_priority(absolute, live)
                                if pr[1] == 0:
                                    css_jobs.append(job2)   # @import
                                elif pr[0] == 0 or pr[1] <= 1:
                                    jobs.appendleft(job2)
                                else:
                                    jobs.append(job2)
                                total += 1
                _pump()

            # L2: requests 被拒的资源用浏览器会话逐个降级重试(同线程, Playwright 线程亲和)
            if ctx is not None and failed_for_browser:
                # 批量: 带会话 Cookie 并发直连, 风控类拒绝才串行走浏览器会话(旧实现 30 个逐个串行)
                batch = failed_for_browser[:30]
                got = self._session_fetch(ctx, [j["url"] for j in batch], referer=origin + "/")
                for job in batch:
                    if len(results) >= config.MAX_ASSETS_PER_TASK:
                        break
                    out = got.get(job["url"])
                    if out is None:
                        continue
                    out = self._normalize_out(out)
                    if not out["ok"]:
                        continue
                    info = self._store(out)
                    results[out["url"]] = info
                    self.log("SMART-DL", f"浏览器会话救回 {info['cat']}/{info['name']}"
                                         f" <- {out['url'][:110]}")
                    if out["is_css"]:
                        text = out["content"].decode("utf-8", "replace")
                        css_sources[out["url"]] = text
                        css_refs[out["url"]] = parse_css_refs(text, out["url"])
        finally:
            # 熔断放弃的在飞请求不阻塞收尾(工作线程超时后自行结束, 结果丢弃)
            executor.shutdown(wait=not tripped, cancel_futures=True)
        if font_skipped[0]:
            self.log("SMART-DL", f"字体按需: {font_skipped[0]} 个 @font-face 兜底格式/页面无对应字符的切片"
                                 f"浏览器不会请求, 不下载(交付里保持原地址)")

        # CSS 内部引用本地化(@import 与 url()/mask/image-set 引用改写为本地路径)
        for css_url, text in css_sources.items():
            info = results.get(css_url)
            if info is None:
                continue
            rewritten = text
            for raw, absolute in css_refs.get(css_url, []):
                child = results.get(absolute)
                if child is None:
                    # 引用 URL 与实际抓取 URL 形态不同(如 /assets/name.hash8.ext
                    # 被浏览器以另一路径抓走): 按内容哈希 token 在池内找同字节
                    child = self._pool_match(absolute, results)
                if child is not None:
                    rewritten = rewritten.replace(raw, child["local"])
            targets = [info["abs_path"]]
            rel = (info.get("local") or "").lstrip("/")
            if rel:
                alias = self.ws / rel
                if alias != info["abs_path"]:
                    targets.append(alias)
            for target in targets:
                with suppress(OSError):
                    target.write_text(rewritten, encoding="utf-8")
        return results


    _STRUCT_EXT = (".css", ".woff2", ".woff", ".ttf", ".otf", ".eot")


    @classmethod
    def _structural_missing(cls, failed_local, remote_missing) -> list:
        """渲染缺失里的结构性资源(样式表/字体): 只有它们补回后才可能大幅改变版面。"""
        out = [p for p in failed_local or () if urlsplit(p).path.lower().endswith(cls._STRUCT_EXT)]
        out += [u for u in remote_missing or () if not beacon_like(u)
                and urlsplit(u).path.lower().endswith(cls._STRUCT_EXT)]
        return out


    def _session_fetch(self, ctx, urls, referer: str = None, budget: float = None) -> dict:
        """批量会话补抓(见 SmartDownloader.fetch_session_many): 带活体 Cookie 并发直连, 风控类拒绝
        才串行走浏览器会话。活体期浏览器确实加载成功过的 URL 直连失败(含同源网络层错误)也再试会话:
        那是对非浏览器客户端的拒绝, 不是资源不存在。"""
        cookies = []
        if ctx is not None:
            with suppress(Exception):
                cookies = ctx.cookies()
        live = getattr(self, "_live_seen", None) or {}
        page_host = urlsplit(self._target).hostname or ""

        def browser_ok(url, out):
            st = out.get("status")
            if st in smart_downloader._SESSION_RETRY_STATUS:
                return True
            if url in live:
                return st is not None or (urlsplit(url).hostname or "") == page_host
            return False

        deadline = time.monotonic() + float(budget if budget is not None else config.SESSION_FETCH_BUDGET)
        return self.downloader.fetch_session_many(
            urls, ctx.request if ctx is not None else None, referer=referer or self._target,
            cookies=cookies, deadline=deadline, browser_ok=browser_ok)


    def _fetch_asset(self, job: dict) -> dict:
        """L1 直连下载(线程池内执行, 禁止触碰 Playwright 对象)。"""
        pre = self._live_bodies.get(job["url"])
        if pre is not None:
            return dict(pre)
        out = self.downloader.fetch_direct(job["url"],
                                           referer=job.get("referer") or self._target)
        return out


    def _store(self, out: dict) -> dict:
        """写入全局 CAS 池并落到任务工作空间(命中同样计入统计)。"""
        content = out["content"]
        sha = hashlib.sha256(content).hexdigest()
        ext = out["ext"] if out["ext"] in config.EXT_CATEGORIES or out["ext"] else ".bin"
        cat = config.EXT_CATEGORIES.get(ext, "misc")
        name = sha[:16] + ext
        cache_file = config.CACHE_ROOT / sha[:2] / sha[2:4] / (sha + ext)
        hit = cache_file.is_file()
        if not hit:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(content)
        self.db.record_cas(sha, len(content), ext)
        local = f"/static/{cat}/{name}"
        dst = self.ws / "static" / cat / name
        if not dst.is_file():
            dst.write_bytes(content)

        # CAS 命中统计修复: 无论 miss/hit, 落入任务工作空间的文件都计入分类与体积
        self._stat_accum[cat if cat in self._stat_accum else "misc"] += 1
        self._stat_accum["bytes"] += len(content)

        alias_local = self._entry_alias(out, content, sha)

        return {"url": out["url"], "sha": sha, "cat": cat, "name": name,
                "local": alias_local or local, "abs_path": dst, "size": len(content),
                "ext": ext, "hit": hit}


    # 入口 bundle 原名特征(app.<hash>.js / chunk-vendors.<hash>.js 等)
    ENTRY_ALIAS_RE = re.compile(
        r"^(?:app|main|bundle|runtime|vendor|chunk-vendors|chunk-[A-Za-z0-9_]+)"
        r"[A-Za-z0-9_.\-]*\.(?:js|mjs|cjs|css)$", re.I)


    def _entry_alias(self, out: dict, content: bytes, sha: str):
        """入口 bundle 原名副本: 保留 app.<hash>.js 等构建命名特征。

        部分 SPA 版本守卫从页面 HTML 正则提取 app.<hash>.js 的 hash 与
        Cookie 记录比对 —— CAS 纯 sha 命名会破坏该自检导致"检测到新版本"
        弹层常驻。以原文件名再落一份副本并让本地引用指向它, 自检即可通过。
        返回别名本地路径; 不适用返回 None。
        """
        orig_name = Path(smart_downloader.strip_path_params(
            urlsplit(out["url"]).path)).name
        if len(orig_name) > 80 or not self.ENTRY_ALIAS_RE.match(orig_name):
            return None
        ext = os.path.splitext(orig_name)[1].lower()
        cat = config.EXT_CATEGORIES.get(ext)
        if cat not in ("js", "css"):
            return None
        alias = self.ws / "static" / cat / orig_name
        if alias.is_file():
            try:
                if alias.read_bytes() == content:
                    return f"/static/{cat}/{orig_name}"
            except OSError:
                pass
            stem, suffix = os.path.splitext(orig_name)
            alias = self.ws / "static" / cat / f"{stem}.{sha[:6]}{suffix}"
            if alias.is_file():
                return None
        try:
            alias.write_bytes(content)
        except OSError:
            return None
        return f"/static/{cat}/{alias.name}"


    # ------------------------------------------------------------------
    MOCK_ASSET_URL_RE = re.compile(
        r"""https?://[^"'\\\s,)]+?\.(?:png|jpe?g|gif|svg|webp|ico|bmp|css|mjs|js|woff2?|ttf|otf|eot|mp4|webm)(?:\?[^"'\\\s,)]*)?"""
        r"""|/(?![/#])[\w\-./@~]+?\.(?:png|jpe?g|gif|svg|webp|ico|bmp|css|mjs|js|woff2?|ttf|otf|eot|mp4|webm)(?:\?[\w\-./=&%]*)?""",
        re.I,
    )


    def _harvest_mock_assets(self, ctx, mock_entries: list, results: dict,
                             origin: str) -> int:
        """扫描录制响应体中的资产 URL 并下载(静态提取的盲区)。

        背景图/Logo/头像常由 API JSON 下发(如 getHomeBackground), 原站渲染
        由浏览器动态加载 —— 静态 DOM 提取看不到, 克隆页会因背景缺失而
        白底白字。此处把响应体里出现的 URL 全部下载, 并登记 raw 引用供
        Mock 体本地化重写。
        """
        page_netloc = urlsplit(self._target).netloc
        candidates: dict = {}  # url -> raw
        for entry in mock_entries:
            body = entry.get("response_body") or ""
            if len(body) > 512 * 1024:
                continue
            for match in self.MOCK_ASSET_URL_RE.finditer(body):
                raw = match.group(0)
                if raw.startswith(("http://", "https://")):
                    absolute = raw
                else:
                    absolute = urljoin(origin.rstrip("/") + "/", raw)
                if not absolute.startswith(("http://", "https://")):
                    continue
                if absolute in results:
                    continue
                if any(v in results for v in self._variants(absolute, page_netloc)):
                    continue
                candidates[absolute] = raw
                if len(candidates) >= 30:
                    break
            if len(candidates) >= 30:
                break
        added = 0
        need = [u for u in candidates if u not in self._live_bodies]
        fetched = self._session_fetch(ctx, need, budget=config.BACKFILL_BUDGET) if need else {}
        for url, raw in candidates.items():
            if len(results) >= config.MAX_ASSETS_PER_TASK:
                break
            pre = self._live_bodies.get(url)
            out = dict(pre) if pre is not None else fetched.get(url)
            if out is None:
                continue
            out = self._normalize_out(out)
            if not out["ok"]:
                continue
            info = self._store(out)
            results[url] = info
            self._raw_refs[raw] = url  # 响应体里的原始写法 -> 绝对地址
            added += 1
        return added


    def _rewrite_mock_bodies(self, mock_entries: list, pairs: list):
        """Mock 响应体本地化(资产 URL 改写为本地路径)并回写 mock_data.json。"""
        changed = False
        for entry in mock_entries:
            body = entry.get("response_body") or ""
            if "http" not in body and "/picupload" not in body and "." not in body:
                continue
            rewritten = apply_replacements(body, pairs)
            if rewritten != body:
                entry["response_body"] = rewritten
                changed = True
        if changed:
            (self.ws / "mock_data.json").write_text(
                json.dumps(mock_entries, ensure_ascii=False, indent=2),
                encoding="utf-8")


    # ------------------------------------------------------------------
    def _live_backfill(self, ctx, results: dict, origin: str) -> int:
        """活体差集回补: 渲染期浏览器成功加载、但静态提取遗漏的资源。"""
        page_netloc = urlsplit(self._target).netloc
        missing = []
        for url in self._live_seen:
            if url in results:
                continue
            if any(v in results for v in self._variants(url, page_netloc)):
                continue
            missing.append(url)
        added = 0
        # 已取回响应体的不占回补名额(本地读取, 不走网络); 名额只限需要真回源的
        missing.sort(key=lambda u: u not in self._live_bodies)
        # 没有现成响应体的: 信标不回源; 其余一次批量会话补抓(并发直连带 Cookie, 风控类再走会话),
        # 旧实现同源 URL 逐个串行走浏览器会话(aliyun 活体回补 29s)
        missing = [u for u in missing if u in self._live_bodies or not beacon_like(u)]
        net_urls = [u for u in missing if u not in self._live_bodies][:config.LIVE_HARVEST_MAX]
        net_set = set(net_urls)
        missing = [u for u in missing if u in self._live_bodies or u in net_set]
        fetched = self._session_fetch(ctx, net_urls, budget=config.BACKFILL_BUDGET) if net_urls else {}
        cap = config.MAX_ASSETS_PER_TASK + config.RENDER_ASSET_RESERVE
        css_children = []
        for url in missing:
            if len(results) >= cap:
                break
            pre = self._live_bodies.get(url)
            out = dict(pre) if pre is not None else fetched.get(url)
            if out is None:
                continue
            out = self._normalize_out(out)
            if not out["ok"]:
                continue
            info = self._store(out)
            results[out["url"]] = info
            added += 1
            if out["is_css"]:
                text = out["content"].decode("utf-8", "replace")
                for raw, absolute in parse_css_refs(text, out["url"]):
                    if absolute in results or absolute in self._live_seen:
                        continue
                    css_children.append((absolute, out["url"]))
        if css_children:
            need = [a for a, _r in dict(css_children).items() if a not in self._live_bodies]
            got = self._session_fetch(ctx, need, budget=config.BACKFILL_BUDGET / 2) if need else {}
            for absolute, _ref in dict(css_children).items():
                if absolute in results:
                    continue
                pre = self._live_bodies.get(absolute)
                child = dict(pre) if pre is not None else got.get(absolute)
                if child is None:
                    continue
                child = self._normalize_out(child)
                if child["ok"]:
                    results[child["url"]] = self._store(child)
        return added


    def _mirror_runtime_paths(self, results: dict) -> int:
        """脚本运行时拼出地址加载的资源(fetch/xhr 取的媒体分段、WebGL 场景、视频)按原站
        路径镜像落盘。这些地址不以字面量出现在 HTML/CSS/JS 里, 本地化替换改写不到, 克隆
        里脚本照原路径请求只能 404; 同源的落到工作空间同路径, 沙盒与交付工程都直接命中。"""
        host = urlsplit(self._target).netloc
        n = 0
        for url, kind in list(self._live_seen.items()):
            # script: 脚本按拼出的路径动态插入的库(苹果播放器按 /ac/libs/hls.js/<ver>/hls.js
            # 现拼地址加载): 缺了它播放器在克隆里 Promise 自旋, 整页主线程卡死
            if kind not in ("runtime", "media", "script"):
                continue
            parts = urlsplit(url)
            if parts.netloc != host:
                continue
            info = results.get(url)
            if info is None:
                continue
            rel = parts.path.lstrip("/")
            if not rel or rel.endswith("/") or rel.startswith(("static/", "frames/")) \
                    or ".." in rel.split("/"):
                continue
            target = self.ws / rel
            if target.is_file():
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(info["abs_path"], target)
                except OSError:
                    target.write_bytes(info["abs_path"].read_bytes())
                n += 1
            except OSError:
                continue
        return n


    _HASH_TOKEN_RE = re.compile(r"(?:^|\.)([0-9a-f]{8,16})(?:\.[A-Za-z0-9]{1,6})?$")


    def _pool_match(self, absolute: str, results: dict):
        """按内容哈希 token 在资源池找同字节文件。

        站点构建产物的文件名普遍携带内容哈希(Vite: name.hash8.ext; 本站实测
        hash 即 sha256 前缀, 与 CAS 池 sha[:16] 命名同源), 引用路径与实际抓取
        路径形态不同时, 以哈希 token 求同存异。
        """
        base = urlsplit(absolute).path.rsplit("/", 1)[-1]
        if base in (None, "", "/"):
            return None
        exact = next((i for i in results.values()
                      if i["abs_path"].name == base), None)
        if exact is not None:
            return exact
        m = self._HASH_TOKEN_RE.search(base)
        if not m:
            return None
        token = m.group(1)
        return next((i for i in results.values()
                     if i["abs_path"].name.startswith(token)
                     or f".{token}." in i["abs_path"].name), None)


    def _failed_backfill(self, ctx, failed_local: set, remote_missing: set,
                         results: dict, origin: str):
        """渲染驱动回补: 本地 404 路径与被拦截的外联 URL 回源补抓。

        返回 (新增资源表, 影子落盘数)。对已在资源池中的 URL(活体回补已抓过,
        但运行时拼接的相对路径无法字面量替换)也按失败路径落影子副本,
        让静态服务器按原路径直接命中。
        """
        page_netloc = urlsplit(self._target).netloc
        new = {}
        shadows = 0
        # 时限 + 跨轮负缓存: 埋点/广告类外联 URL 动辄几十个, 逐个 20s 超时串行回源曾让
        # 单站修复回补耗时 180s(techcrunch); 回源失败的 URL 后续轮次不再重试
        deadline = time.monotonic() + config.BACKFILL_BUDGET
        dead = self._backfill_dead
        skipped_beacons = 0

        fetched = {}   # 本轮批量会话补抓的结果(并发直连带 Cookie, 风控类再走会话)

        def _prefetch(urls):
            want = [u for u in dict.fromkeys(urls) if u not in dead and u not in fetched]
            left = deadline - time.monotonic()
            if not want or left <= 0.2:
                return
            for u, out in self._session_fetch(ctx, want, budget=min(left, 10.0)).items():
                fetched[u] = self._normalize_out(out)

        def _fetch(url):
            if url in dead:
                return None
            out = fetched.get(url)
            if out is None:
                if time.monotonic() > deadline:
                    return None
                _prefetch([url])
                out = fetched.get(url)
                if out is None:
                    return None
            if not out["ok"]:
                dead.add(url)
                if urlsplit(url).netloc == page_netloc:
                    self.log("SMART-DL", f"回源失败({str(out.get('error'))[:60]}): {url[:110]}")
            return out

        def write_shadow(url: str, content: bytes) -> bool:
            nonlocal shadows
            shadow_rel = urlsplit(url).path.lstrip("/")
            if not shadow_rel or shadow_rel.startswith(("static/", "frames/")):
                return False
            shadow = self.ws / shadow_rel
            with suppress(OSError):
                shadow.parent.mkdir(parents=True, exist_ok=True)
                shadow.write_bytes(content)
            shadows += 1
            self.db.record_asset_name(
                shadow_rel.rsplit("/", 1)[-1],
                hashlib.sha256(content).hexdigest(), len(content))
            return True

        todo = []
        for url in remote_missing:
            if not url.startswith(("http://", "https://")):
                continue
            if url in results or any(v in results
                                     for v in self._variants(url, page_netloc)):
                continue
            if beacon_like(url):
                skipped_beacons += 1
                continue
            todo.append(url)
        # 第三方外联先并发直连预取(CDN 图片/字体/脚本不依赖会话 Cookie): 此前逐个走浏览器会话串行回源,
        # 一个慢请求就吃掉整轮 20s 时限。直连失败的再按原路径走浏览器会话; 同源 URL 一律走会话。
        # 外联缺失与本地 404 的回源候选一次批量并发补抓(旧实现: 第三方并发直连, 同源逐个串行走
        # 浏览器会话, 本地 404 的候选路径也逐个串行 —— apple 一条不存在的 hls.js 候选白等 10s)
        remote_cands = {}
        for path in sorted(failed_local):
            if path.startswith(("/preview/", "/frames/", "/api/")):
                continue
            base = path.rsplit("/", 1)[-1]
            cands = [u for u in self._live_seen if urlsplit(u).path == path]
            for cand in (origin.rstrip("/") + path, origin.rstrip("/") + "/assets/" + base):
                if cand not in cands:
                    cands.append(cand)
            remote_cands[path] = cands
        _prefetch(todo + [u for cands in remote_cands.values() for u in cands
                          if u not in results and not any(
                              v in results for v in self._variants(u, page_netloc))])
        for url in todo:
            if len(results) + len(new) >= config.MAX_ASSETS_PER_TASK + config.RENDER_ASSET_RESERVE:
                break
            out = _fetch(url)
            if out is not None and out["ok"]:
                new[out["url"]] = self._store(out)
                self.log("SMART-DL", f"渲染缺失回补: {new[out['url']]['cat']}"
                                     f"/{new[out['url']]['name']} <- {out['url'][:110]}")

        for path in sorted(failed_local):
            if path.startswith(("/preview/", "/frames/", "/api/")):
                continue
            # 动态 import 的解析基准错位: 引用改写为相对路径后, 运行时按
            # 模块/文档目录解析出的路径可能没有文件, 而字节其实已在资源池
            # (按原站路径镜像落盘) —— 按文件名(均为内容哈希命名)在池内
            # 找同名字节, 直接落到请求路径; 池内没有则按候选路径回源:
            # 原路径 + /assets/<同名>(Vite 默认 chunk 目录)。注意很多 SPA
            # 对未知路径返回 200 的 HTML fallback, 盲信状态码会拿 HTML 当
            # JS —— 由下方 DOCTYPE 防毒兜底
            base = path.rsplit("/", 1)[-1]
            target = self.ws / path.lstrip("/")
            stored = next((i for i in results.values()
                           if i["abs_path"].name == base), None)
            if stored is None:
                # Vite name.hash8.ext 类文件名: 池内是 sha 命名, 按哈希 token 匹配
                stored = self._pool_match(path, results)
            if stored is not None:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(stored["abs_path"].read_bytes())
                    shadows += 1
                    self.db.record_asset_name(
                        base, hashlib.sha256(stored["abs_path"].read_bytes()).hexdigest(),
                        stored["abs_path"].stat().st_size)
                    self.log("SMART-DL",
                             f"桶路径修复: {path} <- 资源池 {base}")
                except OSError:
                    pass
                continue
            # 全局资产名索引: 同名文件曾被任何任务抓过(CAS 在池) —— 直接取
            # 字节落盘。源站失效(302/下线/毒化)时这是唯一救赎路径。
            known_sha = self.db.find_asset_name(base) if _distinctive_asset_name(base) else None
            if known_sha:
                src = None
                with suppress(OSError):
                    cas_row = self.db.cas_ext(known_sha)
                    src = (config.CACHE_ROOT / known_sha[:2] / known_sha[2:4]
                           / (known_sha + (cas_row or "")))
                if src is not None and src.is_file():
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(src.read_bytes())
                        shadows += 1
                        self.log("SMART-DL",
                                 f"名字索引回补: {path} <- CAS {known_sha[:12]}")
                        continue
                    except OSError:
                        pass
            remote = remote_cands.get(path) or []
            for url in remote:
                already = None
                if url in results:
                    already = results[url]
                else:
                    already = next((results[v] for v in self._variants(url, page_netloc)
                                    if v in results), None)
                if already is not None:
                    # 已在资源池: 落到请求路径(运行时相对 URL 的救赎路径)
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(already["abs_path"].read_bytes())
                        shadows += 1
                        self.db.record_asset_name(base, already["sha"],
                                                  already.get("size") or 0)
                    except OSError:
                        pass
                    break
                if len(results) + len(new) >= config.MAX_ASSETS_PER_TASK + config.RENDER_ASSET_RESERVE:
                    break
                out = _fetch(url)
                if out is None:
                    continue
                # SPA fallback 防毒: 未知路径常返回 200 的 HTML 壳,
                # 存成 .js/.css 会让运行时解析崩溃且污染资源池
                head = (out.get("content") or b"")[:512].lstrip()
                if out["ok"] and (head[:9] == b"<!DOCTYPE"
                                  or head[:5].lower() == b"<html"):
                    out["ok"] = False
                if not out["ok"]:
                    continue
                info = self._store(out)
                new[out["url"]] = info
                write_shadow(out["url"], out["content"])
                self.db.record_asset_name(
                    base, info["sha"], len(out["content"]))
                # 抓到的字节必须同时落到请求路径, 否则下一轮渲染依旧 404
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(out["content"])
                    shadows += 1
                except OSError:
                    pass
                self.log("SMART-DL", f"渲染缺失回补: {info['cat']}/{info['name']}"
                                     f" <- {out['url'][:110]}")
                break
        if shadows:
            self.log("SMART-DL", f"影子落盘 {shadows} 个运行时相对路径资源")
        if skipped_beacons:
            self.log("SMART-DL", f"跳过 {skipped_beacons} 个埋点/广告信标形态的外联(不影响画面, 不回源)")
        if time.monotonic() > deadline:
            self.log("SMART-DL", f"修复回补达到时限({config.BACKFILL_BUDGET}s), 余下缺失本轮跳过")
        return new, shadows
