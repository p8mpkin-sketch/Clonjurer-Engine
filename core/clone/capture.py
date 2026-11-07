"""ClonerEngine 采集: 活体渲染、截图、懒加载滚动、多视口/窄屏首载对照、活体响应采集(自 cloner_engine.py 按职责拆出的 mixin, 方法体未改)。"""
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


class CaptureMixin:

    def _full_shot(self, page, path, label="截图", _fresh=False):
        """整页截图 + 第三方广告位遮罩(评分口径, 见 AD_RECTS_JS)。"""
        if label == "原站" and config.SCORE_MASK_ADS:
            self._ad_rects = []
            with suppress(Exception):
                self._ad_rects = page.evaluate(AD_RECTS_JS) or []
        self._full_shot_raw(page, path, label, _fresh)
        rects = getattr(self, "_ad_rects", None)
        if rects and label in ("原站", "克隆页", "保真轨"):
            with suppress(Exception):
                _mask_rects(path, rects)


    def _full_shot_raw(self, page, path, label="截图", _fresh=False):
        """整页截图, 三级退化:
        1) 整页截图(SHOT_TIMEOUT);
        2) 限高裁剪截图: 高度取评分覆盖区(SCORE_MAX_HEIGHT 等比换算)与 GPU 纹理上限的较小值;
        3) 视口分块拼接: 逐屏滚动截视口再拼(超高/滚动驱动页 captureScreenshot 整体失败时)。
        评分只取等比缩放后前 SCORE_MAX_HEIGHT 像素, 2/3 覆盖评分全部区域, 口径不变。
        超出评分覆盖区的超长页直接走限高截图: 尾部不计分, 整页截图在超长页上慢一个
        数量级(41000px 合成页 4.6s → 0.6s), 且超大 PNG 拖慢后续评分/校准解码。"""
        dims_js = ("() => [document.documentElement.clientWidth || innerWidth, "
                   "Math.max(document.documentElement.scrollHeight, "
                   "document.body ? document.body.scrollHeight : 0, innerHeight), "
                   "innerHeight]")
        dims = None
        with suppress(Exception):
            dims = page.evaluate(dims_js)
        if dims:
            w = max(1, int(dims[0]))
            cap = min(int(SCORE_MAX_HEIGHT * w / SCORE_WIDTH), SHOT_MAX_TEXTURE)
            if int(dims[1]) > cap:
                try:
                    page.screenshot(path=str(path), full_page=True,
                                    clip={"x": 0, "y": 0, "width": w, "height": cap},
                                    timeout=config.SHOT_TIMEOUT)
                    self.log("RENDER", f"{label}超长({int(dims[1])}px > 评分区 {cap}px), "
                                       f"限高截图")
                    return
                except Exception as exc:
                    self.log("WARN", f"{label}限高截图失败, 改走整页截图: {str(exc)[:160]}")
        try:
            page.screenshot(path=str(path), full_page=True, timeout=config.SHOT_TIMEOUT)
            return
        except Exception as exc:
            self.log("WARN", f"{label}整页截图失败, 退化为限高截图: {str(exc)[:160]}")
        dims = page.evaluate(dims_js)
        w = max(1, int(dims[0]))
        cap = min(int(SCORE_MAX_HEIGHT * w / SCORE_WIDTH), SHOT_MAX_TEXTURE)
        h = max(1, min(int(dims[1]), cap))
        try:
            page.screenshot(path=str(path), full_page=True, animations="disabled",
                            clip={"x": 0, "y": 0, "width": w, "height": h},
                            timeout=config.SHOT_TIMEOUT)
            return
        except Exception as exc:
            self.log("WARN", f"{label}限高截图失败, 退化为视口分块拼接: {str(exc)[:160]}")
        view_h = int(dims[2]) if len(dims) > 2 and dims[2] else 0
        try:
            self._tiled_shot(page, path, w, h, max(1, view_h))
        except Exception:
            # 连视口截图都失败: 记录页面形态(超大元素/超大变换常致 GPU 回读失败)供归因
            with suppress(Exception):
                diag = page.evaluate(SHOT_DIAG_JS)
                self.log("WARN", f"{label}截图全部失败, 页面形态: {json.dumps(diag, ensure_ascii=False)[:600]}")
            # 本地页(沙盒/MHTML)可在独立的禁 GPU 浏览器里重开重截: 同一浏览器的 GPU 进程
            # 被前面的重 WebGL 页拖垮后, 该浏览器内所有截图都会 Unable to capture
            url = page.url or ""
            if _fresh or not url.startswith(("http://127.0.0.1", "file://")):
                raise
            self._fresh_browser_shot(page, url, path, label)


    def _inline_blob_urls(self, page, html: str) -> str:
        """blob: URL 只在原页面会话内有效(运行时生成的纹理/图片/字体, 常见于 JS 生成的
        点阵、渐变贴图), 离线回放与 MHTML 都解析不了 → 采集时在原页取字节内联为 data:。"""
        blobs = list(dict.fromkeys(BLOB_URL_RE.findall(html)))[:BLOB_INLINE_MAX]
        if not blobs:
            return html
        try:
            data = page.evaluate(BLOB_FETCH_JS, blobs) or {}
        except Exception as exc:
            self.log("WARN", f"blob: 资源内联失败: {exc}")
            return html
        for u, d in data.items():
            if d:
                html = html.replace(u, d)
        self.log("RENDER", f"blob: 资源内联 {len(data)}/{len(blobs)} 个(运行时生成, 离线不可解析)")
        return html


    def _fresh_browser_shot(self, page, url, path, label):
        browser = page.context.browser.browser_type.launch(
            headless=True, args=["--disable-gpu", "--disable-dev-shm-usage"])
        try:
            fresh = browser.new_page(viewport=page.viewport_size)
            fresh.set_default_timeout(30000)
            # 与沙盒评分同口径: 只放行本地源与内联协议, 外联一律中止(不回源)
            parts = urlsplit(url)
            local = (f"{parts.scheme}://{parts.netloc}" if parts.netloc else "file:",
                     "data:", "blob:", "cid:", "file:")

            def _guard(route):
                with suppress(Exception):
                    if route.request.url.startswith(local):
                        route.continue_()
                    else:
                        route.abort()
            fresh.route("**/*", _guard)
            fresh.goto(url, wait_until="load", timeout=30000)
            with suppress(Exception):
                self._settle(fresh)
            self._full_shot(fresh, path, label, _fresh=True)
            self.log("RENDER", f"{label}截图改在独立浏览器(禁 GPU)完成")
        finally:
            with suppress(Exception):
                browser.close()


    @staticmethod
    def _tiled_shot(page, path, width, height, view_h):
        """逐屏滚动截视口并按实际滚动位置拼接(滚不动即到底)。"""
        canvas = Image.new("RGB", (width, height), "white")
        y = 0
        while y < height:
            actual = page.evaluate(f"() => {{ window.scrollTo(0, {y}); return window.scrollY; }}")
            page.wait_for_timeout(120)
            tile = Image.open(io.BytesIO(page.screenshot(full_page=False,
                                                          timeout=config.SHOT_TIMEOUT)))
            tile = tile.convert("RGB")
            top = int(actual) if isinstance(actual, (int, float)) else y
            canvas.paste(tile, (0, top))
            if top + tile.height >= height or top < y:
                break
            y = top + view_h
        with suppress(Exception):
            page.evaluate("() => window.scrollTo(0, 0)")
        canvas.save(str(path))


    def _settle(self, page, cap_ms: int = None, quiet_ms: int = None):
        """截图前缓释: DOM 静置即走(MutationObserver), settle_ms 只是上限。
        旧实现固定睡满 settle_ms(2.5s), 每轮渲染/校准/保真轨都付一次。"""
        cap = int(self._strategy_pack.settle_ms if cap_ms is None else cap_ms)
        quiet = int(self._strategy_pack.settle_quiet_ms if quiet_ms is None else quiet_ms)
        if quiet <= 0 or quiet >= cap:
            page.wait_for_timeout(cap)
            return
        started = time.time()
        try:
            installed = bool(page.evaluate(SETTLE_INSTALL_JS, {"quiet": quiet}))
        except Exception:
            installed = False
        if not installed:
            page.wait_for_timeout(cap)
            return
        try:
            # DOM 连续 quiet ms 无变动且图片/字体就绪即返回; 超时 = 等满上限(旧行为)。
            # 脚本被禁用的页面(MHTML)定时器不触发, 谓词只评估一次 → 走超时, 等满上限。
            page.wait_for_function(SETTLE_READY_JS, timeout=cap, polling=50)
        except Exception as exc:
            if type(exc).__name__ != "TimeoutError":
                remain = cap - int((time.time() - started) * 1000)
                if remain > 0:
                    page.wait_for_timeout(remain)
        finally:
            with suppress(Exception):
                page.evaluate(SETTLE_CLEANUP_JS)


    def _probe_viewports(self, page):
        """活体页依次缩到平板/手机宽再还原, 记录各视口下的内联样式(见 CV_RECORD_JS)。
        只在桌面宽采集时做; 还原后等脚本静置, 基准截图在此之后, 口径不受影响。"""
        vp = page.viewport_size or {}
        width, height = vp.get("width") or 0, vp.get("height") or 0
        if width < 1024 or not self.cfg.get("viewport_probe", True):
            return
        t0 = time.monotonic()
        try:
            n = page.evaluate(CV_RECORD_JS, "d")
            for tag, w in (("t", 768), ("m", 390)):
                page.set_viewport_size({"width": w, "height": height})
                page.evaluate(CV_QUIET_JS, 250)
                page.evaluate(CV_RECORD_JS, tag)
            page.set_viewport_size({"width": width, "height": height})
            page.evaluate(CV_QUIET_JS, 300)
            page.evaluate("window.scrollTo(0, 0)")
            page.evaluate(CV_RECORD_JS, "b")
            self.log("RENDER", f"多视口内联采集: {n} 个元素 × 平板/手机视口 {time.monotonic() - t0:.1f}s")
            if self.cfg.get("viewport_reload", True) and not getattr(self, "_cv_pages", None):
                self._start_narrow_loads(page, height)
        except Exception as exc:
            with suppress(Exception):
                page.set_viewport_size({"width": width, "height": height})
            with suppress(Exception):
                page.evaluate("window.__cvRec = null")
            self.log("WARN", f"多视口内联采集失败: {exc}")


    def _start_narrow_loads(self, page, height):
        """同上下文另开平板/手机宽的页面重新加载目标页(只等到响应提交, 与主页后续采集并行):
        只在加载时按宽度写一次的内联样式, 缩放采不到, 要靠窄屏首载对照(见 CV_EMIT_JS)。"""
        self._cv_pages = []
        for tag, w in (("t", 768), ("m", 390)):
            p2 = None
            try:
                p2 = page.context.new_page()
                p2.set_viewport_size({"width": w, "height": height})
                p2.goto(page.url, wait_until="commit", timeout=10000)
                self._cv_pages.append((tag, p2))
            except Exception as exc:
                with suppress(Exception):
                    if p2 is not None:
                        p2.close()
                self.log("WARN", f"窄屏首载对照({w}px)启动失败: {str(exc)[:120]}")


    def _collect_narrow_loads(self, budget_s=8.0):
        """收回窄屏首载页的内联样式表并关页; 预算内没加载完的放弃。"""
        loads, t0 = {}, time.monotonic()
        for tag, p2 in getattr(self, "_cv_pages", []) or []:
            try:
                left = max(0.5, budget_s - (time.monotonic() - t0))
                p2.wait_for_load_state("load", timeout=int(left * 1000))
                p2.evaluate(CV_QUIET_JS, 300)
                loads[tag] = p2.evaluate(CV_PATHS_JS) or {}
                if self.cfg.get("viewport_variants", True):
                    CaptureMixin._start_settle_narrow(p2)
            except Exception:
                with suppress(Exception):
                    p2.close()
        return loads


    def _close_narrow_loads(self):
        for _tag, p2 in getattr(self, "_cv_pages", []) or []:
            with suppress(Exception):
                p2.close()
        self._cv_pages = []


    @staticmethod
    def _start_settle_narrow(p2):
        """非阻塞地在窄屏首载页里发起平滑滚动(页内 Promise 挂在 window.__cvSettle): 平板/手机两页
        同时滚, 主页同时做 CV_EMIT 等步骤; 此前两页依次阻塞滚动, 有变体的站固化阶段多等 8~10s。"""
        budget = max(1.0, float(getattr(config, "VIEWPORT_VARIANT_SETTLE", 4.0)))
        with suppress(Exception):
            p2.evaluate("([src, args]) => { window.__cvSettle = (0, eval)(src)(args).catch(() => 0); return 1; }",
                        [CaptureMixin.AUTO_SCROLL_JS, [int(budget * 1000), 54000]])


    @staticmethod
    def _settle_narrow_page(p2):
        """窄屏首载页在抽取变体子树前与主页同口径「到位」: 平滑滚到底再回顶(触发进视口才起播的
        入场动画 / 懒加载, 否则抽到的是 opacity:0 的初始态, 克隆窄屏整块空白), 再把动画相位写成
        负 animation-delay(ANIM_PHASE_JS, 与主页基准后同一步)。时限内做不完就按当前态抽取。
        滚动已由 _start_settle_narrow 提前发起时这里只等它结束。"""
        budget = max(1.0, float(getattr(config, "VIEWPORT_VARIANT_SETTLE", 4.0)))
        with suppress(Exception):
            started = p2.evaluate("() => !!window.__cvSettle")
            if started:
                p2.evaluate("() => window.__cvSettle")
            else:
                p2.evaluate(CaptureMixin.AUTO_SCROLL_JS, [int(budget * 1000), 54000])
            p2.wait_for_timeout(400)
            p2.evaluate("window.scrollTo(0, 0)")
            p2.evaluate(CV_QUIET_JS, 300)
        with suppress(Exception):
            p2.evaluate(ANIM_PHASE_JS)


    def _inject_dom_variants(self, page, loads):
        """窄屏 DOM 变体(见 ALT_EXTRACT_JS): 结构不同的最上层子树把窄屏版并列插入主页。"""
        if not loads or not self.cfg.get("viewport_variants", True):
            return 0
        main = page.evaluate(CV_PATHS_JS) or {}
        pages = dict(getattr(self, "_cv_pages", []) or [])
        variants = {}                      # path -> {tag: html}
        for tag, other in loads.items():
            diff = sorted(p for p, v in main.items() if p and not p.startswith("@") and p in other
                          and other[p][0] == v[0] and other[p][3] != v[3])
            top = []
            for p in diff:
                if not any(p.startswith(q + ">") for q in top):
                    top.append(p)
            if not top or len(top) > config.VIEWPORT_VARIANT_MAX or tag not in pages:
                continue
            CaptureMixin._settle_narrow_page(pages[tag])
            with suppress(Exception):
                htmls = pages[tag].evaluate(ALT_EXTRACT_JS, top) or {}
                sizes = page.evaluate(SUBTREE_SIZE_JS, list(htmls))
                for p, (h, alt_text, alt_n) in htmls.items():
                    main_text, main_n = sizes.get(p, (0, 0))
                    # 窄屏版明显"更空": 多半是异步内容(赞助位/推荐流)还没到, 不是换了结构
                    if alt_text < 0.5 * main_text or alt_n < 0.3 * main_n:
                        continue
                    variants.setdefault(p, {})[tag] = h
        if not variants:
            return 0
        total = sum(len(h) for v in variants.values() for h in v.values())
        if total > config.VIEWPORT_VARIANT_BYTES:
            self.log("RENDER", f"窄屏 DOM 变体 {len(variants)} 处共 {total // 1024}KB 超上限, 不注入")
            return 0
        items = []
        for p, v in variants.items():
            alts = []
            if len(v) == 2 and v.get("t") == v.get("m"):
                alts.append({"bands": "t m", "html": v["t"]})
            else:
                alts += [{"bands": t, "html": h} for t, h in v.items()]
            own = " ".join(["d"] + [t for t in ("t", "m") if t not in v])
            items.append({"path": p, "own": own, "alts": alts})
        css_main = set(page.evaluate(STYLE_TEXTS_JS) or [])
        extra = []
        for tag in {t for v in variants.values() for t in v}:
            with suppress(Exception):
                extra += [t for t in (pages[tag].evaluate(STYLE_TEXTS_JS) or []) if t not in css_main]
        css = "\n".join(dict.fromkeys(extra))[:config.VIEWPORT_VARIANT_BYTES]
        if re.search(r"</style", css, re.I):
            css = ""
        n = page.evaluate(ALT_INJECT_JS, {"items": items, "css": css})
        if n:
            self.log("RENDER", f"窄屏 DOM 变体: {len(items)} 处子树结构随视口不同, 并列插入 {n} 个窄屏版"
                               f"({total // 1024}KB, 附窄屏运行时样式 {len(css) // 1024}KB)")
        return n


    def _bg_freeze_css(self):
        """A1-lite.2 背景色权威固化: 原站截图四边条带采样主背景色。

        纯色(方差低)→固化为 html,body !important 覆盖, 防克隆侧系统色/
        CSS 变量解析漂移; 渐变/图案背景(方差高)不固化, 避免误伤。"""
        png = self.ws / 'original_preview.png'
        if not png.is_file():
            return None
        try:
            im = Image.open(png).convert('RGB')
            w, h = im.size
            strips = [im.crop(box).resize((1, 1)).getpixel((0, 0)) for box in
                      ((0, 0, w, 12), (0, h - 12, w, h), (0, 0, 12, h), (w - 12, 0, w, h))]
            flat = [c for s in strips for c in s]
            mean = sum(flat) / len(flat)
            var = sum((v - mean) ** 2 for v in flat) / len(flat)
            if var > 40:
                return None
            r, g, b = (int(round(sum(s[i] for s in strips) / len(strips))) for i in range(3))
            return (f'<style data-clone-freeze="bg">html,body{{'
                    f'background-color:rgb({r},{g},{b})!important}}</style>')
        except Exception:
            return None


    # ------------------------------------------------------------------
    def _freeze_media_from_baseline(self, page):
        """空白 Canvas / 无帧视频: 从纯净基准截图裁出所在区域回填(见 MEDIA_SHOT_JS)。"""
        try:
            info = page.evaluate(MEDIA_SHOT_JS)
        except Exception as exc:
            self.log("WARN", f"媒体快照探测失败: {exc}")
            return
        shots = info.get("shots") or []
        crops = {}
        if shots:
            try:
                with Image.open(self.ws / "original_preview.png") as im:
                    im = im.convert("RGB")
                    scale = im.width / float(info.get("docW") or im.width)
                    for sh in shots[:config.MEDIA_SHOT_MAX]:
                        box = (int(sh["x"] * scale), int(sh["y"] * scale),
                               int((sh["x"] + sh["w"]) * scale), int((sh["y"] + sh["h"]) * scale))
                        box = (max(0, box[0]), max(0, box[1]),
                               min(im.width, box[2]), min(im.height, box[3]))
                        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
                            continue
                        buf = io.BytesIO()
                        im.crop(box).save(buf, format="JPEG", quality=85)
                        crops[str(sh["n"])] = ("data:image/jpeg;base64,"
                                               + base64.b64encode(buf.getvalue()).decode("ascii"))
            except Exception as exc:
                self.log("WARN", f"媒体快照裁图失败: {exc}")
        try:
            applied = page.evaluate(MEDIA_SHOT_APPLY_JS, crops)
        except Exception as exc:
            self.log("WARN", f"媒体快照回填失败: {exc}")
            applied = 0
        if applied or info.get("posters"):
            self.log("RENDER", f"媒体快照: {info.get('posters', 0)} 个视频取当前帧作封面, "
                               f"{applied} 个空白 Canvas/无帧视频从基准截图回填")


    @staticmethod
    def _wait_quiet(page, cap_ms: int) -> bool:
        """渲染相关请求静置: QUIET_MS 内无新请求且在途请求都已完成或已挂起超过 STALE_MS。

        Playwright 的 networkidle 要求 500ms 内零连接, 埋点轮询/长连接/长轮询站点永远
        等满上限(huawei/jd 每站白等 9s)。挂起久的请求(长轮询、流式、卡死的第三方)不影响
        首屏渲染, 不再计入; 新请求还在冒出说明页面仍在装配, 照常等。"""
        quiet_s, stale_s = config.NAV_QUIET_MS / 1000, config.NAV_STALE_MS / 1000
        inflight, last = {}, [time.monotonic()]
        skip = ("websocket", "eventsource", "media", "manifest", "other")

        def _on_req(req):
            if req.resource_type in skip:
                return
            if beacon_like(req.url):
                return   # 埋点/统计信标与渲染无关, 轮询类信标曾让首导航每站等满上限
            inflight[id(req)] = time.monotonic()
            last[0] = time.monotonic()

        def _on_done(req):
            inflight.pop(id(req), None)

        page.on("request", _on_req)
        page.on("requestfinished", _on_done)
        page.on("requestfailed", _on_done)
        try:
            # load 与静置共用一个上限(旧实现各给满 cap: load 拖满 9s 的慢站再白等 9s 静置, 首导航最长 18s)
            deadline = time.monotonic() + cap_ms / 1000
            with suppress(Exception):
                page.wait_for_load_state("load", timeout=cap_ms)
            while time.monotonic() < deadline:
                now = time.monotonic()
                live = sum(1 for t in inflight.values() if now - t < stale_s)
                if not live and now - last[0] >= quiet_s:
                    return True
                page.wait_for_timeout(100)
            return False
        finally:
            for ev, fn in (("request", _on_req), ("requestfinished", _on_done),
                           ("requestfailed", _on_done)):
                with suppress(Exception):
                    page.remove_listener(ev, fn)


    @staticmethod
    def _probe_unreachable(ctx, url: str):
        """网络层可达性探测: 返回不可达原因, 可达(任何 HTTP 状态)返回 None。"""
        try:
            resp = ctx.request.get(url, timeout=config.REACHABILITY_PROBE_TIMEOUT,
                                   max_redirects=5, fail_on_status_code=False)
            with suppress(Exception):
                resp.dispose()
            return None
        except Exception as exc:
            msg = str(exc)
            m = re.search(r"(net::ERR_[A-Z_]+|ERR_[A-Z_]+|Timeout \d+ms exceeded|"
                          r"getaddrinfo \w+|ECONNREFUSED|ECONNRESET|ETIMEDOUT|EHOSTUNREACH|ENETUNREACH)", msg)
            return m.group(1) if m else f"{type(exc).__name__}: {msg[:80]}"


    def _render_page(self, ctx, url: str, screenshot: bool, collect_links: bool):
        page = ctx.new_page()
        rt = self.cfg.get("render_timeout")  # 批次级页面加载超时(秒), 0/缺省 = 平台默认
        try:
            page.set_default_timeout(int(float(rt) * 1000) if rt else config.PAGELOAD_TIMEOUT)
        except (TypeError, ValueError):
            page.set_default_timeout(config.PAGELOAD_TIMEOUT)
        if self.cfg.get("mock_api", True):
            page.on("response", self._on_response)
        page.on("response", self._on_asset_response)  # 活体资源采集(恒开)
        self.log("RENDER", f"导航 {url}")
        t_nav, t_nav_m = time.time(), time.monotonic()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=config.PAGELOAD_TIMEOUT)
        except Exception as exc:
            # 首导航瞬时失败(超时/连接抖动): 先用轻量请求探测网络层可达性 ——
            # 不可达(DNS/连接/超时)直接判失败, 不再白等第二个 45s 导航;
            # 可达才重试导航, 防慢站一票判死
            if any(m in str(exc) for m in ("Timeout", "ERR_", "net::")):
                reason = self._probe_unreachable(ctx, url)
                if reason:
                    self.log("RENDER", f"首导航失败且目标网络层不可达({reason}), 快速失败")
                    raise TargetUnreachable(f"目标不可达(网络层): {reason}; 首导航: "
                                            f"{type(exc).__name__}: {str(exc)[:200]}") from exc
                self.log("RENDER", f"首导航失败({type(exc).__name__}), 目标可达, 重试一次")
                page.goto(url, wait_until="domcontentloaded", timeout=config.PAGELOAD_TIMEOUT)
            else:
                raise
        if screenshot and getattr(config, "VIEWPORT_RELOAD_EARLY", True):
            # 窄屏首载对照页提前到主页导航提交后就发起: 与主页静置/懒加载滚动/多视口采集并行加载,
            # 固化阶段收回时多已加载完(旧实现在多视口采集后才发起, 固化阶段要等它们 load + 到位滚动)
            vp = page.viewport_size or {}
            if (vp.get("width") or 0) >= 1024 and self.cfg.get("viewport_probe", True) \
                    and self.cfg.get("viewport_reload", True):
                self._start_narrow_loads(page, vp.get("height") or 0)
        if not self._wait_quiet(page, config.NETWORK_IDLE_TIMEOUT):
            self.log("RENDER", "网络静置等待超时, 继续后续固化流程")
        self.log("RENDER", f"页面就绪 {time.time() - t_nav:.1f}s")
        if screenshot:
            with suppress(Exception):
                rt = page.evaluate(RUNTIME_FP_JS) or []
                self._runtime_hits = set(rt)
                if rt:
                    self.log("PATTERN", f"活体运行时证据: {', '.join(rt)}")
        tp = self._phase("采集-导航", t_nav_m)
        self._auto_settle_update_prompt(page)
        # 采集期主框架导航冻结: wikipedia 等站在固化序列中途自导航
        # (重定向/落地切换)会撕裂 evaluate 会话("Execution context
        # destroyed")—— abort 后续主框架导航, 按当前已渲染 DOM 采集
        def _freeze_main_nav(route):
            try:
                req = route.request
                if (req.frame == page.main_frame and req.is_navigation_request()
                        and req.url != url):
                    with suppress(Exception):
                        route.abort()
                    return
                with suppress(Exception):
                    route.continue_()
            except Exception:
                with suppress(Exception):
                    route.abort()
        with suppress(Exception):
            page.route("**/*", _freeze_main_nav)

        def _safe_eval(script, tries=3):
            """导航撕裂感知 evaluate: 等待重试而非整链报废。"""
            last = None
            for attempt in range(tries):
                try:
                    return page.evaluate(script)
                except Exception as exc:
                    last = exc
                    if "Execution context" not in str(exc) and "Target closed" not in str(exc):
                        raise
                    with suppress(Exception):
                        page.wait_for_load_state("load", timeout=8000)
                    page.wait_for_timeout(800)
            raise last

        lazy_count = _safe_eval(LAZY_JS)
        if lazy_count:
            self.log("RENDER", f"触发 {lazy_count} 个懒加载资源")
        self._auto_scroll(page)
        self.log("RENDER", f"懒加载与滚动完成 {time.time() - t_nav:.1f}s")
        tp = self._phase("采集-懒加载滚动", tp)
        if screenshot:
            self._probe_viewports(page)
            tp = self._phase("采集-多视口内联", tp)
        # 评分基准(纯净原站): 必须在任何引擎 DOM 改动(表单激活/DEEP_FREEZE/
        # 水合剥离/字体烘焙)之前截取 —— 否则评分比较的是"克隆 vs 引擎改过的
        # 原站", 引擎自身引入的失真被基准同步吸收, 分数系统性偏乐观。
        baseline_text = None
        layout_stable = []
        if screenshot:
            with suppress(OSError):   # 重建/重试: 清掉上一轮基准, 防口径混用
                (self.ws / "original_preview.png").unlink()
            layout_pre = None
            with suppress(Exception):
                layout_pre = _safe_eval(f"() => ({LAYOUT_MAP_JS})({LAYOUT_MAP_MAX})")
            # 图片未到齐就截基准, 克隆(资源池更全)反被扣分: cn.vuejs 赞助商图床慢时
            # 基准里整区空白格子而克隆是满的(-15 分)。可见图仍在加载就再等一会(封顶)
            with suppress(Exception):
                # 与克隆侧同口径: 克隆截图前懒加载图全部转 eager(见 _load_clone), 基准不转
                # 就是"克隆满、基准空"(mi.com 轮播页尾整块空白 -5 分)
                td = time.monotonic()
                page.evaluate(BASELINE_EAGER_JS, config.BASELINE_EAGER_MAX)
                pending = page.evaluate(IMG_PENDING_JS)
                if pending:
                    t_img = time.monotonic()
                    # 有进展才继续等: 连续 STALL 内一张都没到齐即放行(坏图床/永不加载的图不空等到上限)
                    deadline = t_img + config.BASELINE_IMG_WAIT_MS / 1000
                    last_n, last_t = pending, t_img
                    while time.monotonic() < deadline:
                        page.wait_for_timeout(250)
                        try:
                            n = page.evaluate(IMG_PENDING_JS)
                        except Exception:
                            break
                        now = time.monotonic()
                        if not n:
                            break
                        if n < last_n:
                            last_n, last_t = n, now
                        elif now - last_t >= config.BASELINE_IMG_STALL_MS / 1000:
                            break
                    self.log("RENDER", f"基准截图前等待 {pending} 张加载中的图片 "
                                       f"{time.monotonic() - t_img:.1f}s")
            with suppress(Exception):
                frz = _safe_eval(TIME_FREEZE_JS)
                if frz.get("anims"):
                    self.log("RENDER", f"时间冻结: 暂停 {frz['anims']} 个动画, "
                                       f"清空定时器 {frz.get('timers', 0)} 个")
            t_frz = time.monotonic()
            # 冻结后再等一轮可见图: 上面等图期间轮播定时器还在翻页, 冻结那一刻露出的往往
            # 是刚翻到、图还没到的一页(mi.com 基准整屏空白)。冻结后版面不再动, 等的就是截图里那几张
            with suppress(Exception):
                pending = page.evaluate(IMG_PENDING_JS)
                if pending:
                    t_img = time.monotonic()
                    deadline = t_img + config.BASELINE_IMG_FROZEN_WAIT_MS / 1000
                    while time.monotonic() < deadline and page.evaluate(IMG_PENDING_JS):
                        page.wait_for_timeout(200)
                    self.log("RENDER", f"冻结后补等 {pending} 张可见图 {time.monotonic() - t_img:.1f}s")
            with suppress(Exception):
                left = config.BASELINE_TWEEN_SETTLE_MS / 1000 - (time.monotonic() - t_frz)
                if left > 0:
                    page.wait_for_timeout(int(left * 1000))
                _safe_eval(RAF_FREEZE_JS)
            try:
                td = self._phase("细分·基准等图与冻结", td)
                self._full_shot(page, self.ws / "original_preview.png", "原站")
                with suppress(NameError):
                    td = self._phase("细分·基准整页截图", td)
                baseline_text = _safe_eval(TEXT_EXTRACT_JS)
                with suppress(Exception):
                    page.evaluate(MARK_LOADED_IMGS_JS)
                self.log("RENDER", "原站纯净基准快照已保存(DOM 改动前)")
            except Exception as exc:
                self.log("WARN", f"原站截图失败: {exc}")
            with suppress(Exception):
                n_wa = _safe_eval(WAAPI_BAKE_JS)
                if n_wa:
                    self.log("RENDER", f"脚本动画烘焙: {n_wa} 个 WAAPI 动画按截图时刻写成内联样式")
            with suppress(Exception):
                n_ph = _safe_eval(ANIM_PHASE_JS)
                if n_ph:
                    self.log("RENDER", f"动画相位固化: {n_ph} 个元素按截图时刻起播")
        if screenshot and layout_pre and (self.ws / "original_preview.png").is_file():
            # 截图前后各采一次, 只留尺寸未变的块: 动画/逐帧增长的块真实基准高度不可知,
            # 拿它校准只会把克隆拉向另一个时刻
            with suppress(Exception):
                post = {e[0]: e[2] for e in _safe_eval(
                    f"() => ({LAYOUT_MAP_JS})({LAYOUT_MAP_MAX})")}
                stable = [e for e in layout_pre if abs(post.get(e[0], -99) - e[2]) <= 1]
                layout_stable = stable
                (self.ws / "layout_map.json").write_text(
                    json.dumps(stable, ensure_ascii=False), encoding="utf-8")
        tp = self._phase("采集-基准截图", tp)
        if screenshot and (self.ws / "original_preview.png").is_file():
            self._freeze_media_from_baseline(page)
        # 触屏(手机版)不做 focus/blur 激活: 移动页常把搜索框聚焦做成全屏搜索态(返回箭头 + 历史面板),
        # blur 后也不退回, 冻结进克隆(手机版首屏 logo/搜索框整块变样)
        activated = 0 if self.cfg.get("device") == "phone" else _safe_eval(FORM_ACTIVATE_JS)
        if activated:
            self.log("RENDER", f"表单激活: focus/blur {activated} 个输入控件")
        with suppress(Exception):
            td = time.monotonic()
            ws_ = page.evaluate(WAIT_SHEETS_JS, 4000) or {}
            if ws_.get("ms", 0) > 50 or ws_.get("pending") or ws_.get("fetched"):
                self.log("RENDER", f"冻结前等样式表: {ws_.get('ms')}ms, 直取补副本 {ws_.get('fetched', 0)} 张,"
                                   f" 仍未到 {ws_.get('pending', 0)} 张")
        counts = _safe_eval(DEEP_FREEZE_JS)
        if layout_stable:
            # DEEP_FREEZE 已停掉定时器: 此后页面脚本不再改 DOM, 回撤结果能留到序列化
            with suppress(Exception):
                n_rev = page.evaluate(COLLAPSE_REVERT_JS, [e[0] for e in layout_stable if e[2] > 0])
                if n_rev:
                    self.log("RENDER", f"基准后塌陷回撤: {n_rev} 个块在基准截图后被脚本收起, 按基准保留占位")
        self.log("CSSOM",
                 f"固化 {counts.get('css', 0)} 组运行时样式 / "
                 f"{counts.get('canvas', 0)} 个 Canvas / "
                 f"{counts.get('revealed', 0)} 个动画元素显形 / "
                 f"{counts.get('crossOriginSheets', 0)} 个跨域样式表")
        # W7: 水合剥离决策(模式策略包 + 活体早识别)。
        # SSR/水合模式的静态 DOM 是权威渲染(采集期已完整), 而水合重渲染
        # 会用客户端数据替换它(实证: tailwind marquee 87→44 退化链)。
        # 活体探测 Next/Nuxt 家族 → 剥离水合脚本, 保静态 DOM 到序列化。
        strip_hydration = bool(self.cfg.get("disable_hydration"))
        if not strip_hydration:
            try:
                fam = _safe_eval("""() => ({
                    next: !!(window.__next_f || document.querySelector('script[src*="/_next/"]')),
                    nuxt: !!(window.__NUXT__ || document.querySelector('script[src*="/_nuxt/"]'))
                })""")
                strip_hydration = bool(fam.get("next") or fam.get("nuxt"))
            except Exception:
                strip_hydration = False
        if strip_hydration:
            removed = _safe_eval(REMOVE_SCRIPTS_JS)
            self.log("RENDER", f"水合剥离(静态 DOM 权威): 已剥离 {removed} 个水合脚本")
        # A2 字体度量固化(W7): 活体 computed font 采样为 !important 覆盖,
        # 序列化进 HTML/MHTML —— 回放文本度量与采集态一致。
        try:
            baked = _safe_eval(FONT_BAKE_JS)
            if baked:
                self.log("RENDER", f"字体度量固化: {baked} 个元素逐元素烘焙(差分继承)")
        except Exception:
            pass
        with suppress(Exception):
            fl = _safe_eval(INLINE_FLUID_JS) or {}
            if any(fl.values()):
                self.log("RENDER", f"内联尺寸流式化: 全宽 {fl.get('width', 0)} / 轨道 {fl.get('track', 0)}"
                                   f" / 位移 {fl.get('shift', 0)}(采集视口下不变, 换视口随容器伸缩)")
        with suppress(Exception):
            td = self._phase("细分·冻结与字体烘焙", td)
            loads = self._collect_narrow_loads()
            try:
                cv = page.evaluate(CV_EMIT_JS, loads) or {}
                if cv.get("els") or cv.get("cls"):
                    self.log("RENDER", f"多视口内联样式: {cv['els']} 个元素, {cv.get('vw', 0)} 个按视口宽比例改写为 vw,"
                                       f" {cv.get('banded', 0)} 个移入分视口样式表(其中窄屏首载补采 {cv.get('reload', 0)})"
                                       f"; 分视口 class {cv.get('cls', 0)} 个")
                try:
                    self._inject_dom_variants(page, loads)
                except Exception as exc:
                    self.log("WARN", f"窄屏 DOM 变体注入失败: {str(exc)[:160]}")
            finally:
                self._close_narrow_loads()
        # N1 保真轨: MHTML 快照(采集期活体渲染的权威固化)。
        # Chromium 对 MHTML 的回放默认禁脚本 —— 确定性由构造保证。
        if screenshot:
            try:
                td = self._phase("细分·窄屏收回与变体", td)
                cdp = ctx.new_cdp_session(page)
                snap = cdp.send('Page.captureSnapshot', {'format': 'mhtml'})
                (self.ws / 'capture.mhtml').write_text(snap['data'], encoding='utf-8')
                self.log("RENDER", f"保真轨 MHTML 快照: {len(snap['data']) // 1024} KB")
            except Exception as exc:
                self.log("WARN", f"MHTML 快照失败: {exc}")
            finally:
                with suppress(Exception):
                    cdp.detach()
        self.db.touch_task(self.task_id)   # 采集心跳: 长下载阶段防误收割
        if screenshot:
            with suppress(Exception):
                probe = _safe_eval(PAGE_PROBE_JS)
                self.log("RENDER", "版面特征: " + ", ".join(f"{k}={v}" for k, v in probe.items()))
        hidden_frames = 0
        with suppress(Exception):
            hidden_frames = int(_safe_eval(MARK_HIDDEN_FRAMES_JS) or 0)
        html = None
        try:
            td = self._phase("细分·MHTML快照", td)
            shadow = _safe_eval(SHADOW_SERIALIZE_JS)
            if shadow and shadow.get("html"):
                html = shadow["html"]
                self.log("RENDER", f"Shadow DOM 声明式序列化: {shadow['roots']} 个开放影子根"
                                   f"(物化 {shadow['adopted']} 组构造样式表)")
        except Exception as exc:
            self.log("WARN", f"Shadow DOM 序列化失败, 回退 outerHTML: {exc}")
        if html is None:
            html = _safe_eval(OUTER_HTML_JS)
        html = self._inline_blob_urls(page, html)
        # N1.1 采集退化守卫: 序列化过薄(<5KB)说明页面在采集窗口内被
        # 重定向/挂载失败清空(ibm 类 React 壳) —— 重新导航再序列化一次
        if len(html) < 5000:
            self.log("RENDER", f"序列化过薄({len(html)}B), 判定采集退化, 重导航重试")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=config.PAGELOAD_TIMEOUT)
                with suppress(Exception):
                    page.wait_for_load_state("networkidle", timeout=config.NETWORK_IDLE_TIMEOUT)
                page.wait_for_timeout(2500)
                retried = _safe_eval(OUTER_HTML_JS)
                self.log("RENDER", f"重试后序列化: {len(retried)}B")
                # 只接受更完整的结果: 小体量合法页面重导航后反而丢失采集期固化
                if len(retried) > len(html):
                    html = retried
            except Exception as exc:
                self.log("WARN", f"退化重试失败: {exc}")
        if hidden_frames:
            html, n_blank = _blank_hidden_frames(html)
            if n_blank:
                self.log("RENDER", f"不可见 iframe 置空: {n_blank} 个(埋点/像素沙盒, 克隆里不再加载活体)")
        text = _safe_eval(TEXT_EXTRACT_JS)
        if screenshot and baseline_text is not None:
            # 原站文本指纹与基准截图同口径(改动前), 采集期显形内容不计入
            text = baseline_text
        if screenshot and not (self.ws / "original_preview.png").is_file():
            # 基准截图失败时的兜底(改动后截图, 记日志以便账本区分口径)
            try:
                self._full_shot(page, self.ws / "original_preview.png", "原站")
                self.log("WARN", "原站基准快照回退为改动后截图(口径偏乐观)")
            except Exception as exc:
                self.log("WARN", f"原站截图失败: {exc}")
        with suppress(NameError):
            self._phase("细分·序列化", td)
        tp = self._phase("采集-固化", tp)
        collected = _safe_eval(COLLECT_ASSETS_JS)
        iframes = _safe_eval(IFRAME_EXTRACT_JS)
        iframe_srcs = _safe_eval(IFRAME_SRCS_JS)
        # 活体子 frame 的 DOM(含跨域 OOPIF, Playwright 不受同源限制): 供顶级导航被
        # 弹回宿主时兜底固化 —— 登录/SSO 组件只有嵌在宿主里才渲染自身
        live_frames = {}
        if iframe_srcs:
            for fr in page.frames[1:1 + config.MAX_FRAME_PAGES * 2]:
                with suppress(Exception):
                    if fr.url.startswith(("http://", "https://")):
                        live_frames[fr.url] = fr.content()
        links = _safe_eval(COLLECT_LINKS_JS) if collect_links else []
        if collect_links:
            self.log("CRAWL", f"发现 {len(links)} 个同域候选链接")
        state = None
        if screenshot:  # 仅主页面快照 Cookie/Storage 终态
            try:
                storage = page.evaluate(STORAGE_SNAPSHOT_JS)
                state = {"cookies": ctx.cookies(), "local": storage.get("local", {}),
                         "session": storage.get("session", {})}
            except Exception as exc:
                self.log("WARN", f"状态快照失败: {exc}")
        with suppress(Exception):
            self._harvest_live_bodies()
        self._phase("采集-收尾", tp)
        final_url = url
        with suppress(Exception):
            final_url = page.url or url
        with suppress(Exception):
            page.close()
        return {"final_url": final_url,
                "html": html, "text": text, "urls": collected["urls"],
                "srcsets": collected["srcsets"], "rawRefs": collected["rawRefs"],
                "links": links, "iframes": iframes, "iframe_srcs": iframe_srcs,
                "live_frames": live_frames,
                "state": state}


    def _auto_settle_update_prompt(self, page, max_rounds: int = 2):
        """活体渲染期主动走完"版本升级弹层"流程。

        首访弹升级框并自动 reload 的 SPA(灰度发布体系), 必须在冻结前点击
        "立即自动更新"让浏览器拿到新版页面 —— 冻结的是升级完成后的自洽终态;
        克隆侧再配合 state_snapshot 恢复 Cookie, 版本守卫判定通过不再弹框。
        """
        for _round in range(max_rounds):
            try:
                probe = page.evaluate(UPDATE_PROMPT_PROBE_JS)
            except Exception:
                return
            if not probe or not probe.get("found"):
                return
            self.log("RENDER",
                     f"检测到版本升级弹层({(probe.get('text') or '')[:50]}…), "
                     f"自动执行更新流程")
            try:
                clicked = page.evaluate(UPDATE_PROMPT_CLICK_JS)
            except Exception:
                clicked = None
            if not clicked:
                self.log("WARN", "升级弹层未找到可点击的更新按钮, 继续原流程")
                return
            try:
                page.wait_for_load_state("domcontentloaded",
                                         timeout=config.PAGELOAD_TIMEOUT)
                page.wait_for_load_state("networkidle",
                                         timeout=config.NETWORK_IDLE_TIMEOUT)
            except Exception:
                pass
            page.wait_for_timeout(1500)


    # 页内平滑滚动: 400px 一步、每步等两帧(rAF, 后台页 rAF 不触发时 100ms 兜底)再 60ms, 不在中途
    # 长驻留(驻留期间视频起播解码, 负载高时掉帧同样触发降级; 实测 400px/两帧连续滚动在
    # 机器高负载下仍保持增强版)。没赶上加载的媒体由修复循环按克隆实际请求回补。
    # 旧实现每步 scrollTo 跳 900px 再等 320ms: 苹果类页面的性能守卫把大跨度跳滚判成掉帧, 整页降级成非增强版(html 改
    # no-enhanced、滚动擦洗视频容器被移除), 采集到的是降级后的 DOM —— 克隆版面塌陷
    AUTO_SCROLL_JS = r"""async ([budgetMs, maxPx]) => {
      const t0 = performance.now();
      const frame = () => new Promise(r => { let done = false;
        const fin = () => { if (!done) { done = true; r(); } };
        requestAnimationFrame(() => requestAnimationFrame(fin)); setTimeout(fin, 100); });
      let y = 0, H = document.documentElement.scrollHeight;
      while (y < H && y < maxPx) {
        y += 400;
        window.scrollTo(0, y);
        await frame();
        await new Promise(r => setTimeout(r, 60));
        H = Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0);
        if (performance.now() - t0 > budgetMs) return {y: y, h: H, timeout: true};
      }
      return {y: y, h: H, timeout: false};
    }"""


    def _auto_scroll(self, page):
        """平滑滚动到底再回顶, 触发 IntersectionObserver / 无限加载(见 AUTO_SCROLL_JS)。
        总时长受 AUTO_SCROLL_BUDGET 约束(页内计时, 一次往返)。"""
        if not config.AUTO_SCROLL_SMOOTH:
            return self._auto_scroll_stepped(page)
        try:
            res = page.evaluate(self.AUTO_SCROLL_JS,
                                [int(config.AUTO_SCROLL_BUDGET * 1000), 54000]) or {}
            if res.get("timeout"):
                self.log("RENDER", f"自动滚动达到时限({config.AUTO_SCROLL_BUDGET}s), "
                                   f"停在 {res.get('y')}/{res.get('h')}px")
            page.wait_for_timeout(600)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(400)
        except Exception as exc:
            self.log("WARN", f"自动滚动中断: {exc}")


    def _auto_scroll_stepped(self, page):
        """分步滚动到底再回顶, 触发 IntersectionObserver / 无限加载。
        总时长受 AUTO_SCROLL_BUDGET 约束 —— 主线程被
        WebGL/滚动动画占满的重页(CI 软件渲染下单次 evaluate 可达数秒)不再拖满 60 步。"""
        deadline = time.monotonic() + config.AUTO_SCROLL_BUDGET
        try:
            step = 900
            position = 0
            for _ in range(60):
                position += step
                # 滚动 + 等待 + 取高合并为一次往返(重页上每次 evaluate 都要排主线程)
                height = page.evaluate(
                    "async p => { window.scrollTo(0, p); "
                    "await new Promise(r => setTimeout(r, 320)); "
                    "return document.documentElement.scrollHeight; }", position)
                if position >= height:
                    break
                if time.monotonic() > deadline:
                    self.log("RENDER", f"自动滚动达到时限({config.AUTO_SCROLL_BUDGET}s), "
                                       f"停在 {position}/{height}px")
                    break
            page.wait_for_timeout(600)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(400)
        except Exception as exc:
            self.log("WARN", f"自动滚动中断: {exc}")


    # ------------------------------------------------------------------
    def _collect_frame_pages(self, ctx, page_result, out_frames, merge_assets,
                             depth: int, target_url: str):
        """iframe 递归固化: 未被同源 DOM 提取覆盖的 iframe 以顶级页面渲染。

        跨域 iframe(如 CAS/SSO 登录组件)的 contentDocument 不可读, 但以
        顶级页面导航时不受 X-Frame-Options 约束, 可完整固化。
        """
        if depth > config.MAX_FRAME_DEPTH or len(out_frames) >= config.MAX_FRAME_PAGES:
            return
        extracted_srcs = {frame.get("src") for frame in page_result.get("iframes", [])}
        seen_srcs = {src for src, _html, _text in out_frames} | {target_url}
        for src in page_result.get("iframe_srcs", []) or []:
            if len(out_frames) >= config.MAX_FRAME_PAGES:
                return
            if src in extracted_srcs or src in seen_srcs:
                continue  # 同源已 DOM 提取 / 自引用 / 已固化
            try:
                sub = self._render_page(ctx, src, screenshot=False,
                                        collect_links=False)
            except Exception as exc:
                self.log("WARN", f"iframe 固化失败 {src[:100]}: {exc}")
                continue
            if _frame_bounced(src, sub.get("final_url") or src, target_url):
                # 登录/SSO 组件发现自己不在 iframe 里会按 ReturnUrl 跳回宿主页: 固化下来的
                # 是宿主首页而非组件(jd 登录弹层里塞进了一个缩小的京东首页)。
                # 改取宿主活体页里该 frame 的 DOM; 取不到则保留原 src 不固化。
                live_html = _match_live_frame(src, page_result.get("live_frames") or {})
                if live_html:
                    # 活体 frame DOM 没经过采集脚本, 它引用的样式/图片从未进资源池, 离线
                    # 回放全是外链(jd 登录框图标/二维码/样式全丢)。按 frame 地址静态提取引用补进去
                    with suppress(Exception):
                        merge_assets(_static_refs(live_html, src))
                    out_frames.append((src, live_html, ""))
                    self.log("RENDER", f"iframe 顶级导航被重定向, 改用宿主内活体 frame DOM "
                                       f"固化: {src[:100]}")
                else:
                    self.log("WARN", f"iframe 顶级导航被重定向({src[:80]} → "
                                     f"{(sub.get('final_url') or '')[:80]}), 不固化")
                continue
            merge_assets(sub)
            out_frames.append((src, sub["html"], sub.get("text") or ""))
            self.log("RENDER", f"跨域 iframe 已固化为本地子页: {src[:100]}")
            self._collect_frame_pages(ctx, sub, out_frames, merge_assets,
                                      depth + 1, target_url)


    # ------------------------------------------------------------------
    def _on_response(self, response):
        try:
            if not self.cfg.get("mock_api", True):
                return
            request = response.request
            if request.method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                return
            if request.resource_type not in ("xhr", "fetch"):
                return
            content_type = response.headers.get("content-type", "")
            parts = urlsplit(response.url)
            body = None
            # VCR 网络磁带(W3 安全协商版): 录制 JSON 数据接口 + x-component
            # RSC 导航载荷。回放侧由请求方 Accept 协商精确供给 —— 文档导航
            # (Accept text/html 开头)永不命中, RSC 载荷仅供给自带
            # Accept: text/x-component 的客户端路由 fetch(见 _tape_for);
            # 流式(event-stream)/分段(multipart)协议不录。
            tape_wanted = (config.TAPE_RECORD and response.status < 400
                           and ("json" in content_type or "x-component" in content_type)
                           and "event-stream" not in content_type
                           and "multipart" not in content_type)
            if tape_wanted:
                try:
                    body = response.text()
                except Exception:
                    body = None
                if body and len(body) <= config.MAX_TAPE_BODY_BYTES \
                        and self._tape_bytes < config.MAX_TAPE_TOTAL_BYTES:
                    key = (request.method, parts.path or "/", parts.query)
                    if key not in self._tape_seen:
                        self._tape_seen.add(key)
                        self._net_tape.append({
                            "path": parts.path or "/",
                            "method": request.method,
                            "query": parts.query,
                            "status": response.status,
                            "content_type": content_type or "application/json",
                            "response_body": body,
                        })
                        self._tape_bytes += len(body)
            if "json" not in content_type:
                return
            body = body if body is not None else response.text()
            if not body or len(body) > config.MAX_MOCK_BODY_BYTES:
                return
            self._mock_raw.append({
                "path": parts.path or "/",
                "method": request.method,
                "query": parts.query,
                "status": response.status,
                "response_body": body,
            })
            self.log("MOCK-API",
                     f"录制 {request.method} {parts.path} ({response.status})")
        except Exception:
            return


    def _on_asset_response(self, response):
        """活体资源采集: 浏览器渲染期真实成功加载的静态资源清单。"""
        try:
            if response.status >= 400:
                return
            request = response.request
            rtype = request.resource_type
            if rtype in ("fetch", "xhr") and response.url.startswith(("http://", "https://")):
                ctype = ""
                with suppress(Exception):
                    ctype = response.headers.get("content-type", "")
                if _is_runtime_asset(response.url, ctype):
                    # 只登记, 不进活体响应体复用: fetch 取的媒体可能是永不结束的流(直播/长连接),
                    # 收尾时 resp.body() 会永久等待, 整个任务卡在采集阶段; 交给回补按网络限时下载
                    self._live_seen.setdefault(response.url, "runtime")
                return
            if rtype not in ASSET_RESOURCE_TYPES:
                return
            self._live_seen[response.url] = rtype
            if response.status == 200 and request.resource_type in _LIVE_BODY_TYPES \
                    and len(self._live_resp) < config.LIVE_BODY_MAX \
                    and response.url.startswith(("http://", "https://")):
                self._live_resp.setdefault(response.url, response)
        except Exception:
            return


    def _harvest_live_bodies(self):
        """页面关闭前把浏览器已加载资源的响应体取回(CDP 本地读取, 毫秒级)。

        CAS 下载与活体回补原先把浏览器刚加载过的资源再从网络各下一遍: 慢站 CAS 20~30s、
        活体回补串行逐个 fetch 再 20~45s(jd/techcrunch)。字节就是浏览器实际渲染用的那份
        (含 Accept 协商出的 webp/avif), 比重下更忠实; 取不到(已被逐出/重定向)的照常走网络。"""
        t0, n = time.monotonic(), 0
        pending, self._live_resp = self._live_resp, {}
        for url, resp in pending.items():
            if url in self._live_bodies or self._live_body_bytes >= config.LIVE_BODY_BYTES:
                continue
            try:
                body = resp.body()
            except Exception:
                continue
            if not body or len(body) > config.MAX_ASSET_BYTES:
                continue
            ctype = ""
            with suppress(Exception):
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            self._live_bodies[url] = {"url": url, "ok": True, "content": body,
                                      "content_type": ctype, "error": "", "ext": ".bin",
                                      "is_css": False, "status": 200, "via": "live"}
            self._live_body_bytes += len(body)
            n += 1
        if n:
            self.log("SMART-DL", f"活体响应体复用: 取回 {n} 个已加载资源 "
                                 f"({self._live_body_bytes // 1024} KB, {time.monotonic() - t0:.1f}s)")


    def _finalize_mocks(self):
        entries, seen = [], set()
        for entry in self._mock_raw:
            key = (entry["method"], entry["path"], entry["query"])
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
            if len(entries) >= config.MAX_MOCK_ENTRIES:
                break
        return entries
