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
from core.clone.api_match import request_pairs
from core.clone.js import seed_random_js

from core.clone.js import (  # noqa: F401  纯搬迁后的再导出, 外部 import 路径不变
    CANVAS_PIN_JS,
    CV_CANVAS_DRIFT_JS,
    HIDDEN_ROOTS_JS,
    IMG_SRC_PIN_JS,
    ROUNDTRIP_PIN_JS,
    SCROLL_LOCK_STRIP_JS,
    STATE_PHASE_RESET_JS,
    TIMER_JOURNAL_JS,
    TIMER_JOURNAL_REWIND_JS,
    TRANSITION_FINISH_JS,
    VISIBLE_PATHS_JS,
    DEPLOY_DEFER_IMG_JS,
    AD_RECTS_JS,
    INTERSTITIAL_HIDE_JS,
    ROOT_FONT_APPLY_JS,
    FLUID_READ_JS,
    FLUID_APPLY_JS,
    ROOT_FONT_READ_JS,
    INTERSTITIAL_RESTORE_JS,
    FONT_BAKE_NOSIZE_JS,
    SCROLL_TOP_JS,
    NEST_FIX_JS,
    NARROW_STABLE_JS,
    DOM_COUNTS_JS,
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
    CSSOM_LINK_DEDUPE_JS,
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
    _same_document,
    _id_misaligned_parents,
    _root_inline_varies,
    _source_id_classes,
    _source_sibling_groups,
    _subtree_sig,
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
from core.clone.support import bounded_call, bounded_api_call, _fluid_elements, _is_global_reset_rule, _is_transient_hide_rule, _regroup_css_rules  # noqa: F401



from core.clone.support import (  # noqa: E402  手机版(按 UA 分发)
    PHONE_EDITION_FILE, inject_edition_router, merge_static_tree, separate_phone_edition,
)



def _shot_fmt() -> dict:
    """评分整页截图编码: SHOT_JPEG 开时用 JPEG(文件名仍 .png, 读取方按内容解码)。
    1280×16000 整页 PNG 编码 ~3s, JPEG q92 ~0.6s; 评分先等比缩到 512 宽, 两侧同为 JPEG 时
    分数与两侧同为 PNG 一致(合成页 5 组扰动逐一相同)。基准/克隆/保真轨/校准预评都走这里, 口径同源。"""
    if getattr(config, "SHOT_JPEG", False):
        return {"type": "jpeg", "quality": int(getattr(config, "SHOT_JPEG_QUALITY", 92))}
    return {}

class CaptureMixin:

    def _full_shot(self, page, path, label="截图", _fresh=False):
        """整页截图 + 第三方广告位遮罩(评分口径, 见 AD_RECTS_JS)。"""
        hidden = 0
        if config.SCORE_MASK_ADS and label in ("原站", "克隆页", "保真轨", "静态版"):
            with suppress(Exception):
                hidden = page.evaluate(INTERSTITIAL_HIDE_JS) or 0
                if hidden and label == "原站":
                    self.log("RENDER", f"评分截图: 插屏广告层 {hidden} 个两侧隐藏(不计分, 交付物保留)")
            if getattr(config, "SCORE_UNLOCK_SHOT", True):
                # 弹层打开时给 html/body 写的锁滚动声明(overflow:hidden; position:fixed; inset:0)在截图时仍在: 弹层藏了,
                # 页面却只渲染一屏, 整页截图首屏以下全空白 —— 基准与克隆(交付 HTML 已剥离锁)口径不一, 引擎自评被压到 55
                # (探针桌面 81~93)。滚动前已剥过一次, 弹层脚本之后又写回; 截图前再剥(交付 HTML 本来也要剥)
                with suppress(Exception):
                    n_lock = page.evaluate(SCROLL_LOCK_STRIP_JS) or 0
                    if n_lock and label == "原站":
                        self.log("RENDER", f"评分截图: 弹层写回的 {n_lock} 条锁滚动声明截图前剥离(否则整页截图首屏以下空白)")
        if label == "原站" and config.SCORE_MASK_ADS:
            self._ad_rects = []
            with suppress(Exception):
                self._ad_rects = page.evaluate(AD_RECTS_JS) or []
        try:
            self._full_shot_raw(page, path, label, _fresh)
        finally:
            if hidden:
                with suppress(Exception):
                    page.evaluate(INTERSTITIAL_RESTORE_JS)
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
                                    timeout=config.SHOT_TIMEOUT, **_shot_fmt())
                    self.log("RENDER", f"{label}超长({int(dims[1])}px > 评分区 {cap}px), "
                                       f"限高截图")
                    return
                except Exception as exc:
                    self.log("WARN", f"{label}限高截图失败, 改走整页截图: {str(exc)[:160]}")
        try:
            page.screenshot(path=str(path), full_page=True, timeout=config.SHOT_TIMEOUT, **_shot_fmt())
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
                            timeout=config.SHOT_TIMEOUT, **_shot_fmt())
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
            tile = Image.open(io.BytesIO(page.screenshot(full_page=False, **_shot_fmt(),
                                                          timeout=config.SHOT_TIMEOUT)))
            tile = tile.convert("RGB")
            top = int(actual) if isinstance(actual, (int, float)) else y
            canvas.paste(tile, (0, top))
            if top + tile.height >= height or top < y:
                break
            y = top + view_h
        with suppress(Exception):
            page.evaluate(SCROLL_TOP_JS)
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
            parts = []   # 分步耗时(缩放/静置/记录, 秒): 远程归因慢站在哪一步
            tq = time.monotonic()
            with suppress(Exception):
                page.evaluate(CANVAS_PIN_JS, "save")
                page.evaluate(IMG_SRC_PIN_JS, "save")
            n = page.evaluate(CV_RECORD_JS, "d")
            roots = [page.evaluate(ROOT_FONT_READ_JS)]
            parts.append(f"d 记录 {time.monotonic() - tq:.1f}")
            for tag, w in (("t", 768), ("m", 390), ("b", width)):
                t1 = time.monotonic()
                page.set_viewport_size({"width": w, "height": height})
                t2 = time.monotonic()
                page.evaluate(CV_QUIET_JS, 300 if tag == "b" else 250)
                t3 = time.monotonic()
                if tag == "b":
                    with suppress(Exception):
                        k = page.evaluate(IMG_SRC_PIN_JS, "restore")
                        if k:
                            self.log("RENDER", f"多视口内联采集: {k} 张图片缩放来回后地址被站点脚本清空, 已按缩放前还原")
                    # 图表/动画库的 canvas 在来回缩放后可能按还原瞬间(布局未稳)量到的盒子重设位图(vite 首页 638x339 → 548),
                    # 基准截图、布局校准、固化都会接着用这个错态: 尺寸与缩放前不同的 canvas 存在时再派发一次 resize 让库按稳定布局重算
                    with suppress(Exception):
                        for _ in range(2):
                            if not page.evaluate(CV_CANVAS_DRIFT_JS):
                                break
                            page.evaluate("window.dispatchEvent(new Event('resize'))")
                            page.evaluate(CV_QUIET_JS, 300)
                        drift = page.evaluate(CV_CANVAS_DRIFT_JS)
                        pinned = page.evaluate(CANVAS_PIN_JS, "restore")
                        if drift or pinned:
                            self.log("RENDER", f"多视口内联采集: {drift} 个 canvas 缩放来回后尺寸未回到原值, {pinned} 个按缩放前显示盒钉住")
                    page.evaluate(SCROLL_TOP_JS)
                page.evaluate(CV_RECORD_JS, tag)
                if tag != "b":
                    roots.append(page.evaluate(ROOT_FONT_READ_JS))
                else:
                    # 脚本按屏宽改写 <html>/<body> 内联字号(rem 基准; 桌面宽下常已封顶, 与宽不成正比): 桌面宽烘焙的 px 字号/行高
                    # 没有断点可限定区间时会在所有宽度压过 rem 规则 —— 窄屏下盒子按新基准缩了、字仍是桌面 px。认定后不烘字号度量
                    self._root_inline_varies = bool(config.ROOT_INLINE_VARIES_NOSIZE and _root_inline_varies(roots))
                    if self._root_inline_varies:
                        self.log("RENDER", "多视口内联采集: 根字号由脚本按屏宽改写, 字体烘焙不烘字号/行高/字距")
                parts.append(f"{tag} 缩放 {t2 - t1:.1f}/静置 {t3 - t2:.1f}/记录 {time.monotonic() - t3:.1f}")
            self.log("RENDER", f"多视口内联采集: {n} 个元素 × 平板/手机视口 {time.monotonic() - t0:.1f}s"
                               f"({'; '.join(parts)})")
            if self.cfg.get("viewport_reload", True) and not getattr(self, "_cv_pages", None):
                self._start_narrow_loads(page, height)
        except Exception as exc:
            with suppress(Exception):
                page.set_viewport_size({"width": width, "height": height})
            with suppress(Exception):
                page.evaluate("window.__cvRec = null")
            self.log("WARN", f"多视口内联采集失败: {exc}")


    def _phase_src(self, html):
        """状态类相位复位的源码对照: ({id: class}, 同类兄弟组)。同一份源码只解析一次。"""
        if not html:
            return {}, []
        memo = getattr(self, "_phase_src_memo", None)
        if memo is None or memo[0] is not html:
            grp = _source_sibling_groups(html) if getattr(config, "PHASE_RESET_SIBLINGS", True) else []
            memo = self._phase_src_memo = (html, _source_id_classes(html), grp)
        return memo[1], memo[2]

    def _probe_root_font(self, page):
        """rem 基准随视口宽成正比(<html>/<body> 内联 font-size = 宽/N)时, 冻结后改写成 vw(见
        ROOT_FONT_READ_JS)。证据: 0.92 倍宽另开页面重新加载, 计算字号同比变化(±1.5%)才认; 只认内联写的
        (样式表里的 vw/媒体查询本就随视口)。认定后字体烘焙不烘字号/行高/字距(FONT_BAKE_NOSIZE_JS)。"""
        self._root_fluid = {}
        self._fluid = None
        vp = page.viewport_size or {}
        width, height = vp.get("width") or 0, vp.get("height") or 0
        # 只在窄屏采集(手机版子克隆等)做: 桌面宽下按宽算的 rem 到了窄屏多半换公式/封顶, 改 vw 未必更近
        if not width or width >= 1024 or not self.cfg.get("viewport_probe_root", True):
            return
        # 另开同上下文页面在 0.92 倍宽重新加载来对照, 不缩放主页: 有的站 resize 处理整页重载(搜狐手机版),
        # 主页一旦被导航, 已滚动/懒加载的状态就丢了
        p2 = None
        try:
            a = page.evaluate(ROOT_FONT_READ_JS)
            fa = page.evaluate(FLUID_READ_JS) or {}
            w2 = int(round(width * 0.92))
            p2 = page.context.new_page()
            p2.set_viewport_size({"width": w2, "height": height})
            p2.goto(page.url, wait_until="domcontentloaded", timeout=10000)
            with suppress(Exception):
                p2.wait_for_load_state("load", timeout=4000)
            p2.evaluate(CV_QUIET_JS, 300)
            b = p2.evaluate(ROOT_FONT_READ_JS)
            fb = p2.evaluate(FLUID_READ_JS) or {}
        except Exception as exc:   # noqa: BLE001 探测失败不影响采集
            self.log("WARN", f"随宽同比探测失败: {str(exc)[:120]}")
            return
        finally:
            with suppress(Exception):
                if p2 is not None:
                    p2.close()
        for k, i in (("html", 1), ("body", 3)):
            f1, f2 = a[i + 1], b[i + 1]
            if a[i] and f1 > 0 and f2 > 0 and a[0] and b[0] and abs((f2 / f1) / (b[0] / a[0]) - 1) <= 0.015:
                self._root_fluid[k] = f1 / a[0] * 100
        if self._root_fluid:
            self.log("RENDER", "rem 基准随视口宽: " + ", ".join(f"{k} {v:.3f}vw" for k, v in self._root_fluid.items()))
        self._fluid = _fluid_elements(fa, fb, a[0] or width, b[0] or w2)
        if self._fluid and (self._fluid["paths"] or self._fluid["inline"]):
            self.log("RENDER", f"随宽同比: {len(self._fluid['paths'])} 个元素字号随视口宽(不烘字号),"
                               f" {sum(len(v) for v in self._fluid['inline'].values())} 个内联 px 改写为 vw"
                               f"(0.92 倍宽对照, 同比 ±1.5%)")

    def _narrow_desktop_page(self, page, w, height):
        """桌面 UA 的窄屏首载页。NARROW_FRESH_CTX: 开在全新上下文里(与手机页一样不带主页的 cookie/localStorage)。
        与主页同上下文时它拿到的是「第二次访问」的状态(按存储计数换标签页/关掉过的提示条/已同意的弹层),
        而原站对新访客(评分探针与真实首访都是全新上下文)显示的是首访状态 —— 平板段变体烘进了回访态(合成用例 rerun_state)。"""
        if not getattr(config, "NARROW_FRESH_CTX", True):
            return self._seed_page(page.context.new_page())
        ua = None
        with suppress(Exception):
            ua = page.evaluate("navigator.userAgent")
        hdrs = dict(getattr(self, "_ctx_headers", {}) or {})
        ctx = page.context.browser.new_context(viewport={"width": w, "height": height}, user_agent=ua or None,
                                               extra_http_headers=hdrs, ignore_https_errors=True)
        self._cv_extra_ctx.append(ctx)
        return self._seed_page(ctx.new_page())

    def _start_narrow_loads(self, page, height):
        """同上下文另开平板/手机宽的页面重新加载目标页(只等到响应提交, 与主页后续采集并行):
        只在加载时按宽度写一次的内联样式, 缩放采不到, 要靠窄屏首载对照(见 CV_EMIT_JS)。"""
        self._cv_pages = []
        self._cv_mdesk = None
        self._cv_extra_ctx = []
        self._cv_pair = {}
        self._cv_pair_read = {}
        self._cv_main_page = page
        for tag, w in (("t", 768), ("m", 390)):
            p2 = None
            try:
                if tag == "m" and getattr(config, "NARROW_PHONE_UA", True):
                    # 手机宽页按真机口径加载: 手机 UA + 触屏仿真, 单独上下文(UA 是上下文级)。服务端/脚本按 UA
                    # 或按 A/B 下发的首屏组件(wix 邮箱表单: 桌面 UA 采集 5 轮里 3 轮没有, 手机 UA 评分侧 8/8 有)
                    # 只在手机 UA 下才稳定出现, 桌面上下文里再开 390 宽页抽不到, 克隆手机端整块缺失
                    hdrs = dict(getattr(self, "_ctx_headers", {}) or {})
                    ctx2 = page.context.browser.new_context(
                        viewport={"width": w, "height": height}, user_agent=config.PHONE_USER_AGENT,
                        extra_http_headers=hdrs, ignore_https_errors=True, **config.PHONE_DEVICE)
                    self._cv_extra_ctx.append(ctx2)
                    p2 = self._seed_page(ctx2.new_page())
                    # 同宽再开一张桌面 UA 页(只读结构): 手机 UA 页换了结构而桌面 UA 同宽页没换的子树是按 UA 下发的,
                    # 不是按宽度; 这类变体只给触屏设备显示, 桌面浏览器窄窗口仍显示主页版(wix 手机 390 89→43)
                    if getattr(config, "NARROW_UA_SPLIT", True):
                        try:
                            p4 = self._narrow_desktop_page(page, w, height)
                            # 手机 UA 页跳到别的文档时它要顶替成手机段来源, 窄屏才加载的图同样要登记
                            p4.on("response", lambda r, pp=p4: self._on_narrow_response(r, pp))
                            p4.set_viewport_size({"width": w, "height": height})
                            p4.goto(page.url, wait_until="commit", timeout=10000)
                            self._cv_mdesk = p4
                        except Exception as exc:   # noqa: BLE001 可有可无
                            self.log("WARN", f"桌面 UA 窄屏对照页启动失败: {str(exc)[:120]}")
                else:
                    p2 = self._narrow_desktop_page(page, w, height)
                # 窄屏才显示/才加载的图(手机版首屏图、按断点换的 picture 源)桌面活体从没请求过, 不登记就
                # 进不了活体回补, 克隆在手机宽度下整块裂图(wix 手机首屏 ~40 分)
                p2.on("response", lambda r, pp=p2: self._on_narrow_response(r, pp))
                p2.set_viewport_size({"width": w, "height": height})
                p2.goto(page.url, wait_until="commit", timeout=10000)
                self._cv_pages.append((tag, p2))
                # 手机页旁再开一张 0.92 倍宽的首载对照页(同上下文): 只在加载时按宽度写一次的内联尺寸(wix 图片组件
                # 的 height px)缩放不会重写, 同页三点采样看不出; 两张不同宽度的首载页对照才认得出同比
                if tag in getattr(config, "NARROW_PAIR_TAGS", ("m",)) and getattr(config, "NARROW_FLUID_PAIR", True):
                    w2 = int(round(w * 0.92))
                    try:
                        p3 = self._seed_page(p2.context.new_page())
                        p3.on("response", lambda r, pp=p3: self._on_narrow_response(r, pp))
                        p3.set_viewport_size({"width": w2, "height": height})
                        p3.goto(page.url, wait_until="commit", timeout=10000)
                        self._cv_pair[tag] = (p3, w2)
                    except Exception as exc:   # noqa: BLE001 对照页可有可无
                        self.log("WARN", f"窄屏首载对照页({w2}px)启动失败: {str(exc)[:120]}")
            except Exception as exc:
                with suppress(Exception):
                    if p2 is not None:
                        p2.close()
                self.log("WARN", f"窄屏首载对照({w}px)启动失败: {str(exc)[:120]}")
                # 手机 UA 页没起来(冷启动上下文在高负载下 goto 等 commit 超时): 同宽桌面 UA 页已在加载, 接管手机段,
                # 否则整个手机段没有变体/分视口样式, 手机端整块用桌面结构(偶发手机 100→29)
                p4 = getattr(self, "_cv_mdesk", None) if tag == "m" else None
                if p4 is not None and getattr(config, "NARROW_MDESK_FALLBACK", True):
                    self._cv_mdesk = None
                    self._cv_pages.append((tag, p4))
                    self.log("RENDER", "手机段改用桌面 UA 同宽首载页")


    def _narrow_same_doc(self, p2):
        """窄屏首载页最终停在与主页同一文档(同主机同路径, 不计查询串/锚点/末尾斜杠)上。手机 UA 下 302/脚本跳到
        m. 子域或下载引导页的, 是另一个文档 —— 那由「手机版」子克隆处理, 不能按路径对齐当成同页的窄屏变体。"""
        if not getattr(config, "NARROW_SAME_DOC", True):
            return True
        try:
            mp = getattr(self, "_cv_main_page", None)
            return _same_document(p2.url, mp.url if mp is not None else "")
        except Exception:   # noqa: BLE001
            return True

    def _on_narrow_response(self, response, p2):
        """窄屏首载页的资源只在页面仍是主页同一文档时登记: 跳到手机站后加载的另一套应用包会被当成活体资源,
        先于框架识别落盘, 把识别从「水合 MPA(剥离脚本)」改判成 SPA(保留脚本), 克隆里广告加载器重建 iframe 卡死。"""
        if not self._narrow_same_doc(p2):
            return
        self._on_asset_response(response, bodies=False)

    def _collect_narrow_loads(self, budget_s=None):
        """收回窄屏首载页的内联样式表并关页; 预算内没加载完的放弃(记日志: 否则该站手机端没有变体/分视口
        样式却看不出原因)。预算随主页就绪耗时放大: 跨境慢站(CI 美国出口访问国内站就绪 11s+)固定 8s 两页都等不到,
        窄屏首载页一张没有, 手机端整块桌面横幅留在克隆里。"""
        if budget_s is None:
            budget_s = min(24.0, max(8.0, float(getattr(self, "_nav_ready_s", 0) or 0) * 1.5))
        loads, t0 = {}, time.monotonic()
        kept = []
        if getattr(config, "NARROW_PARALLEL_WAITS", False):
            # 各窄屏首载页(平板/手机/桌面 UA 同宽/0.92 倍宽对照)的「静置 300ms」原本逐页串行等, 忙页各等满 1.5s;
            # 先等全部页 load(浏览器里本就并行加载), 再同时在各页里起静置, 后面逐页读路径时只等各自的结果
            allp = [p for _t, p in (getattr(self, "_cv_pages", []) or [])]
            if getattr(self, "_cv_mdesk", None) is not None:
                allp.append(self._cv_mdesk)
            allp += [pp for pp, _w in (getattr(self, "_cv_pair", {}) or {}).values()]
            for pp in allp:
                with suppress(Exception):
                    left = max(0.5, budget_s - (time.monotonic() - t0))
                    pp.wait_for_load_state("load", timeout=int(left * 1000))
                    CaptureMixin._kick_quiet(pp)
        for tag, p2 in getattr(self, "_cv_pages", []) or []:
            try:
                left = max(0.5, budget_s - (time.monotonic() - t0))
                p2.wait_for_load_state("load", timeout=int(left * 1000))
                if not self._narrow_same_doc(p2):
                    # 跳到了另一个文档: 不拿它出变体/分视口 class/内联补采; 手机段退回桌面 UA 同宽对照页(同文档)
                    alt = getattr(self, "_cv_mdesk", None) if tag == "m" else None
                    self.log("RENDER", f"窄屏首载页 {tag} 跳到另一个文档({str(p2.url)[:100]}), 不作窄屏变体来源"
                                       + ("; 改用桌面 UA 同宽首载页" if alt is not None else ""))
                    with suppress(Exception):
                        p2.close()
                    pr = (getattr(self, "_cv_pair", {}) or {}).pop(tag, None)
                    if pr:
                        with suppress(Exception):
                            pr[0].close()
                    if alt is None:
                        continue
                    self._cv_mdesk = None
                    p2 = alt
                    left = max(0.5, budget_s - (time.monotonic() - t0))
                    p2.wait_for_load_state("load", timeout=int(left * 1000))
                    if not self._narrow_same_doc(p2):
                        with suppress(Exception):
                            p2.close()
                        continue
                kept.append((tag, p2))
                CaptureMixin._await_quiet(p2)
                loads[tag] = p2.evaluate(CV_PATHS_JS) or {}
                if self.cfg.get("viewport_variants", True):
                    CaptureMixin._start_settle_narrow(p2)
            except Exception as exc:
                self.log("WARN", f"窄屏首载页 {tag} 在 {budget_s:.0f}s 预算内未加载完, 放弃({type(exc).__name__}): "
                                 f"该宽度无首载对照/变体")
                with suppress(Exception):
                    p2.close()
        self._cv_pages = kept
        t_main = time.monotonic() - t0
        p4 = getattr(self, "_cv_mdesk", None)
        if p4 is not None and "m" in loads:
            try:
                left = max(0.5, budget_s - (time.monotonic() - t0))
                p4.wait_for_load_state("load", timeout=int(left * 1000))
                CaptureMixin._await_quiet(p4)
                loads["__mdesk"] = p4.evaluate(CV_PATHS_JS) or {}
            except Exception as exc:   # noqa: BLE001
                self.log("WARN", f"桌面 UA 窄屏对照页未加载完, 不分 UA({type(exc).__name__})")
        if p4 is not None:
            with suppress(Exception):
                p4.close()
            self._cv_mdesk = None
        for tag, (p3, w3) in list((getattr(self, "_cv_pair", {}) or {}).items()):
            try:
                left = max(0.5, budget_s - (time.monotonic() - t0))
                p3.wait_for_load_state("load", timeout=int(left * 1000))
                CaptureMixin._await_quiet(p3)
                # 对照页的内联样式按路径交给 CV_EMIT_JS: 手机段 px 与之同比(值比≈宽比)的写 vw
                if tag in loads:
                    loads.setdefault("__pair", {})[tag] = {"w": w3, "paths": p3.evaluate(CV_PATHS_JS) or {}}
                if getattr(config, "NARROW_PAIR_EARLY_CLOSE", True):
                    # 随宽对照要的读数现在就取, 然后关页: 这张页之后只剩这一次读, 留着它会在整个变体阶段
                    # 一直跑动画/定时器抢 CPU(4 核 runner 上同时开着 4 张窄屏页, remix 变体阶段 60s+)
                    with suppress(Exception):
                        p3.evaluate(NARROW_STABLE_JS, 1500)
                    fp = p3.evaluate(FLUID_READ_JS) or {}
                    self._cv_pair_read = dict(getattr(self, "_cv_pair_read", {}) or {})
                    self._cv_pair_read[tag] = (fp, w3)
                    self._grab_fresh_storage(p3)
                    with suppress(Exception):
                        p3.close()
                    self._cv_pair.pop(tag, None)
            except Exception:   # noqa: BLE001
                with suppress(Exception):
                    p3.close()
                self._cv_pair.pop(tag, None)
        if time.monotonic() - t0 >= 3:
            self.log("RENDER", f"窄屏首载页收回: 主两页 {t_main:.1f}s / 合计 {time.monotonic() - t0:.1f}s(预算 {budget_s:.0f}s)")
        return loads


    @staticmethod
    def _kick_settle_tail(p2):
        """页内起「等平滑滚动结束 → 400ms → 回顶 → 静置 300ms → 稳定(2.5s 封顶)」链(window.__cvTail, 结果为稳定 1/0)。"""
        with suppress(Exception):
            p2.evaluate("""([quiet, stab]) => {
                const Q = (0, eval)(quiet), S = (0, eval)(stab);
                window.__cvTail = (async () => {
                  try { if (window.__cvSettle) await window.__cvSettle; } catch (e) {}
                  await new Promise(r => setTimeout(r, 400));
                  window.scrollTo({top: 0, left: 0, behavior: 'instant'});
                  try { await Q(300); } catch (e) {}
                  try { return await S(2500); } catch (e) { return 1; }
                })();
                return 1; }""", [CV_QUIET_JS, NARROW_STABLE_JS])

    @staticmethod
    def _kick_quiet(p2):
        """在页里起一次静置等待(CV_QUIET 300ms), 不阻塞; 之后 _await_quiet 取结果。"""
        with suppress(Exception):
            p2.evaluate("([src]) => { window.__cvQ = (0, eval)(src)(300).catch(() => 0); return 1; }", [CV_QUIET_JS])

    @staticmethod
    def _await_quiet(p2):
        """等先前起的静置结束; 没起过(或已取过)就当场等一次, 与原逐页 CV_QUIET 同口径。"""
        try:
            done = p2.evaluate("async () => { const q = window.__cvQ; window.__cvQ = null; if (!q) return 0; await q; return 1; }")
        except Exception:   # noqa: BLE001
            done = 0
        if not done:
            p2.evaluate(CV_QUIET_JS, 300)

    def _mhtml_retry(self, page, cdp):
        """MHTML 快照首次超时: 探测卡住的 frame(document.readyState 1s 内不返回)写进日志,
        Page.stopLoading 取消挂起的导航/请求后重试一次; 仍超时则抛出, 走既有"快照失败"分支。"""
        t0 = time.monotonic()
        stuck, states = [], {}
        for fr in list(page.frames)[:80]:
            if time.monotonic() - t0 > 8:
                break
            try:
                st = bounded_api_call(fr, "evaluate", ("document.readyState",), 1.0)
                states[st] = states.get(st, 0) + 1
            except TimeoutError:
                stuck.append(fr.url[:120])
            except Exception:
                states["err"] = states.get("err", 0) + 1
        self.log("WARN", f"MHTML 快照 {config.MHTML_SNAPSHOT_TIMEOUT_S:.0f}s 未返回: frame {len(page.frames)} 个, "
                         f"状态 {states}, 无响应 {len(stuck)} 个 {stuck[:5]}; 停止加载后重试")
        self._diag_mark("保真轨 MHTML 快照重试")
        with suppress(Exception):
            bounded_api_call(cdp, "send", ('Page.stopLoading',), 5.0)
        return bounded_api_call(cdp, "send", ('Page.captureSnapshot', {'format': 'mhtml'}),
                                config.MHTML_RETRY_TIMEOUT_S)

    def _diag_mark(self, what: str):
        """只写文件不进日志总线的进度标记(engine.diag): 任务挂死时据最后一行定位卡点, 正常日志不变。"""
        with suppress(Exception):
            with open(self.ws / "engine.diag", "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {what}\n")

    def _grab_fresh_storage(self, p):
        """全新上下文(不与主页共用存储)的同文档窄屏首载页的存储, 关页前取: 用来认「站点每次首访都自己写」的键。"""
        if not getattr(config, "STATE_SKIP_SELF_WRITTEN", True) or p is None:
            return
        with suppress(Exception):
            mp = getattr(self, "_cv_main_page", None)
            if mp is None or p.context is mp.context or not self._narrow_same_doc(p):
                return
            st = p.evaluate(STORAGE_SNAPSHOT_JS)
            if st:
                self._fresh_storage = list(getattr(self, "_fresh_storage", None) or []) + [st]

    def _close_narrow_loads(self):
        for _tag, p2 in getattr(self, "_cv_pages", []) or []:
            self._grab_fresh_storage(p2)
            self._diag_mark(f"关窄屏首载页 {_tag}")
            try:
                bounded_api_call(p2, "close", (), config.NARROW_CLOSE_TIMEOUT_S)
            except TimeoutError as exc:
                self.log("WARN", f"窄屏首载页 {_tag} 关闭{exc}, 放弃等待(页面留给上下文关闭时回收)")
            except Exception as exc:   # noqa: BLE001 关不掉要留痕: 活页空转到任务结束会拖慢后续全部阶段
                if "closed" not in str(exc).lower():
                    self.log("WARN", f"窄屏首载页 {_tag} 关闭失败: {type(exc).__name__}: {str(exc)[:120]}")
        self._cv_pages = []
        for p3, _w in (getattr(self, "_cv_pair", {}) or {}).values():
            with suppress(Exception):
                p3.close()
        self._cv_pair = {}
        if getattr(self, "_cv_mdesk", None) is not None:
            self._grab_fresh_storage(self._cv_mdesk)
            with suppress(Exception):
                self._cv_mdesk.close()
            self._cv_mdesk = None
        for ctx2 in getattr(self, "_cv_extra_ctx", []) or []:
            with suppress(Exception):
                ctx2.close()
        self._cv_extra_ctx = []


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
        st = [time.monotonic()]
        tail = None
        with suppress(Exception):
            tail = p2.evaluate("async () => { const t = window.__cvTail; window.__cvTail = null; if (!t) return null; return await t; }")
        if tail is not None:
            st += [time.monotonic(), time.monotonic()]
            t_idle = time.monotonic()
            with suppress(Exception):
                p2.wait_for_load_state("networkidle", timeout=3000)
            st.append(time.monotonic())
            stable, counts = int(tail or 0), {}
            if time.monotonic() - t_idle > 0.3:
                # 网络真忙了一阵: 稳定判定要在静下来之后重测(原顺序是先网络静置再稳定)
                with suppress(Exception):
                    stable = p2.evaluate(NARROW_STABLE_JS, 2500)
            st.append(time.monotonic())
            with suppress(Exception):
                counts = p2.evaluate(DOM_COUNTS_JS) or {}
            with suppress(Exception):
                p2.evaluate(ANIM_PHASE_JS)
            st.append(time.monotonic())
            counts = dict(counts or {})
            counts["_steps"] = "并行链 " + " / ".join(f"{k} {b - a:.1f}s" for k, a, b in zip(
                ("链", "-", "网络静置", "稳定", "计数相位"), st, st[1:]))
            return bool(stable), counts
        with suppress(Exception):
            started = p2.evaluate("() => !!window.__cvSettle")
            if started:
                p2.evaluate("() => window.__cvSettle")
            else:
                p2.evaluate(CaptureMixin.AUTO_SCROLL_JS, [int(budget * 1000), 54000])
            st.append(time.monotonic())
            p2.wait_for_timeout(400)
            p2.evaluate(SCROLL_TOP_JS)
            p2.evaluate(CV_QUIET_JS, 300)
        while len(st) < 2:
            st.append(time.monotonic())
        st.append(time.monotonic())
        # 滚完再等页面"到位": 网络静默 + 文字量/已出 src 的图片数连续两次不变(水合晚到的表单、计数动画、
        # 懒加载图片组件写 src 都在这之后才稳定; 抽早了变体里只剩 alt 文字与空计数, wix 手机首屏 ~40 分)
        with suppress(Exception):
            p2.wait_for_load_state("networkidle", timeout=3000)
        st.append(time.monotonic())
        stable, counts = 1, {}
        with suppress(Exception):
            stable = p2.evaluate(NARROW_STABLE_JS, 2500)
        st.append(time.monotonic())
        with suppress(Exception):
            counts = p2.evaluate(DOM_COUNTS_JS) or {}
        with suppress(Exception):
            p2.evaluate(ANIM_PHASE_JS)
        st.append(time.monotonic())
        counts = dict(counts or {})
        counts["_steps"] = " / ".join(f"{k} {b - a:.1f}s" for k, a, b in zip(
            ("滚动", "回顶静置", "网络静置", "稳定", "计数相位"), st, st[1:]))
        return bool(stable), counts


    def _fluidize_narrow_page(self, p2, tag):
        """窄屏首载页抽取变体前的随宽同比: 页面脚本按加载宽度把几何写死成 px(内联 style / img 的 width·height 属性,
        wix 手机图 325x215), 变体只在 390 采一次, 到 360~440 宽就按 390 的尺寸渲染, 整页从该处起位移十几像素
        (iPhone Air 70 / Pro Max 59 / S24 50~96 双峰)。同页改到 0.92 倍宽再读一遍, 值比≈宽比(±1.5%)的 px 改写成
        vw 后再抽取(与主页 _probe_root_font 同口径, 见 _fluid_elements)。改宽触发整页重载的站: 两次读的路径对不上,
        自然什么都不改。"""
        if not getattr(config, "VARIANT_FLUID", True):
            return
        try:
            vp = p2.viewport_size or {}
            w1, h = int(vp.get("width") or 0), int(vp.get("height") or 800)
            if not w1:
                return
            _t0 = time.monotonic()
            fa = p2.evaluate(FLUID_READ_JS) or {}
            _t_read0 = time.monotonic() - _t0
            with suppress(Exception):
                p2.evaluate(CANVAS_PIN_JS, "save")
                p2.evaluate(IMG_SRC_PIN_JS, "save")
            rt_on = bool(getattr(config, "VARIANT_FLUID_RESTORE", True))
            if rt_on:
                with suppress(Exception):
                    p2.evaluate(ROUNDTRIP_PIN_JS, "save")
            samples = {}
            # 窄一档(0.92×)与宽一档(1.128×: 390→440 正好是最宽的主流手机)各读一次, 三点判直线/封顶/垫底
            # 到位等待已判定「到时限仍在变化」的页(常驻动画/轮播): 每次改宽后的稳定等待也必然等满, 三次白等 4.5s
            unstable = getattr(config, "FLUID_SKIP_UNSTABLE_WAIT", True) and getattr(self, "_cv_unstable", {}).get(tag)
            ft = {"读": 0.0, "改宽": 0.0, "静置": 0.0, "稳定": 0.0}
            _t = time.monotonic()
            for w in (int(round(w1 * 0.92)), int(round(w1 * 1.128)), w1):
                p2.set_viewport_size({"width": w, "height": h})
                p2.wait_for_timeout(250)
                _t, ft["改宽"] = time.monotonic(), ft["改宽"] + time.monotonic() - _t
                if not unstable and getattr(config, "NARROW_PARALLEL_WAITS", False):
                    # 静置(无样式/class/结构变动 300ms)与稳定(文字/图/控件计数连续不变)同时等, 取两者都到
                    with suppress(Exception):
                        p2.evaluate("([q, s]) => Promise.all([(0, eval)(q)(300), (0, eval)(s)(1500)])",
                                    [CV_QUIET_JS, NARROW_STABLE_JS])
                    _t, ft["静置"] = time.monotonic(), ft["静置"] + time.monotonic() - _t
                else:
                    with suppress(Exception):
                        p2.evaluate(CV_QUIET_JS, 300)
                    _t, ft["静置"] = time.monotonic(), ft["静置"] + time.monotonic() - _t
                if not unstable and not getattr(config, "NARROW_PARALLEL_WAITS", False):
                    with suppress(Exception):
                        p2.evaluate(NARROW_STABLE_JS, 1500)
                _t, ft["稳定"] = time.monotonic(), ft["稳定"] + time.monotonic() - _t
                if w != w1:
                    samples[w] = p2.evaluate(FLUID_READ_JS) or {}
                _t, ft["读"] = time.monotonic(), ft["读"] + time.monotonic() - _t
            # 先复原内联态, 再钉 canvas 显示盒(反过来会把刚钉上的 aspect-ratio 一并抹掉, vite 平板 98→64)
            if rt_on:
                with suppress(Exception):
                    k = p2.evaluate(ROUNDTRIP_PIN_JS, "restore")
                    if k:
                        self.log("RENDER", f"窄屏首载页 {tag} 随宽对照来回后 {k} 个元素的内联样式/class 没回到首载态, 已复原")
            with suppress(Exception):
                p2.evaluate(CANVAS_PIN_JS, "restore")
                p2.evaluate(IMG_SRC_PIN_JS, "restore")
            if sum(ft.values()) + _t_read0 >= 3:
                self.log("RENDER", f"窄屏首载页 {tag} 随宽分步: 首读 {_t_read0:.1f}s / "
                                   + " / ".join(f"{k} {v:.1f}s" for k, v in ft.items())
                                   + f"; 元素 {len(fa)}" + ("; 不稳定页免等" if unstable else ""))
            w2, w3 = int(round(w1 * 0.92)), int(round(w1 * 1.128))
            fb = samples.get(w2) or {}
            fl = _fluid_elements(fa, fb, w1, w2, fc=samples.get(w3) or None, w3=w3)
            # 另一张不同宽度的首载页对照: 只在加载时按宽度写一次的内联尺寸(缩放不重写, 上面三点看不出)
            pair_n = 0
            pair = (getattr(self, "_cv_pair", {}) or {}).get(tag)
            pread = (getattr(self, "_cv_pair_read", {}) or {}).get(tag)
            if pread:
                pair = (None, pread[1])
            if pair:
                p3, wp = pair
                try:
                    if pread:
                        fp = pread[0]
                    else:
                        with suppress(Exception):
                            p3.evaluate(CV_QUIET_JS, 300)
                        with suppress(Exception):
                            p3.evaluate(NARROW_STABLE_JS, 1500)
                        fp = p3.evaluate(FLUID_READ_JS) or {}
                    fl2 = _fluid_elements(fa, fp, w1, wp)
                    for path, props in fl2["inline"].items():
                        # canvas 位图尺寸由图表库在改宽时重设(不是"只在首载写一次"), 同页三点采样已覆盖; 两张首载页对照
                        # 只有窄侧两点, 往 420/440 外推会把位图/固化图拉大(vite iPhone Air 90→76)
                        if (fa.get(path) or [""])[0] == "CANVAS":
                            continue
                        cur = fl["inline"].setdefault(path, {})
                        for k, v in props.items():
                            if k not in cur:
                                cur[k] = v
                                pair_n += 1
                                if pair_n <= 12:
                                    self.log("RENDER", f"窄屏首载对照页同比 {tag}: {path[-90:]} {k} {(fa.get(path) or [0, 0, ''])[2][:80]!r} → {v}")
                    for path in fl2["paths"]:
                        if path not in fl["paths"]:
                            fl["paths"].append(path)
                            pair_n += 1
                except Exception as exc:   # noqa: BLE001
                    self.log("WARN", f"窄屏首载对照页({tag})读取失败: {str(exc)[:120]}")
            if fl["paths"] or fl["inline"]:
                dbg = [f"{pth[-70:]} {k}={v}" for pth, props in list(fl["inline"].items())[:40] for k, v in props.items()][:14]
                if dbg:
                    self.log("RENDER", f"窄屏首载页 {tag} 随宽同比明细: " + " | ".join(dbg)[:1800])
                res = p2.evaluate(FLUID_APPLY_JS, fl) or {}
                piece = sum(1 for v in fl["inline"].values() for x in v.values() if x.startswith(("min(", "max(")))
                self.log("RENDER", f"窄屏首载页 {tag} 随宽同比: {res.get('vw', 0)} 个内联 px/尺寸属性改写为 vw(其中 {piece} 个封顶/垫底),"
                                   f" {res.get('marked', 0)} 个元素字号随宽(0.92/1.128 倍宽对照, 同比 ±1.5%)"
                                   + (f"; 其中 {pair_n} 项只在首载时按宽度写一次, 由 {pair[1]}px 首载对照页认出" if pair_n else ""))
        except Exception as exc:   # noqa: BLE001 探测失败就按原样抽取
            self.log("WARN", f"窄屏首载页 {tag} 随宽同比探测失败: {str(exc)[:120]}")

    @staticmethod
    def _hidden_root_variants(self, page, p2, main, other, top):
        """桌面采集时整棵 display:none 的子树(手机版横幅/visible-xs 块), 窄屏首载页里可见, 且子树内任一元素的
        class 或内联 style 与窄屏页不同 → 作为变体根返回(不与已有结构变体重叠)。桌面上这些子树的状态来自后台
        脚本(轮播对不渲染的项翻页, 过渡事件不触发, 时间冻结截掉其兜底定时器 → 两项同时 active), 无法验证;
        窄屏页里它渲染着、已静置, 才是该视口的真实状态(cgnpc 手机端横幅下多出一整项, 49 分)。"""
        try:
            roots = page.evaluate(HIDDEN_ROOTS_JS) or []
            roots = [p for p in roots if p in other and p in main and not any(p == q or p.startswith(q + ">") for q in top)]
            if not roots:
                return []
            vis = set(p2.evaluate(VISIBLE_PATHS_JS, roots) or [])
        except Exception as exc:   # noqa: BLE001 诊断性增强, 失败不影响主流程
            self.log("WARN", f"桌面隐藏子树窄屏对照失败: {str(exc)[:120]}")
            return []
        out = []
        _dv = lambda st: sorted(m.group(1).lower() + ":" + m.group(2).strip().lower()
                                for m in re.finditer(r"(?:^|;)\s*(display|visibility)\s*:\s*([^;!]+)", st or ""))
        for p in roots:
            if p not in vis:
                continue
            # 窄屏页里靠根自身内联 display/visibility 才显示的(脚本轮换消息条/标签页当前项写的), 是时间状态不是视口
            # 差异: 换成窄屏版就和主页正显示的那一项同时可见(apple iphone 页平板横幅多出一行, 81→43)
            if _dv(other[p][2]) and _dv(other[p][2]) != _dv(main[p][2]):
                self.log("RENDER", f"桌面不渲染子树 {p[-80:]} 在窄屏页靠内联 {_dv(other[p][2])} 显示(脚本轮换态), 不换窄屏版")
                continue
            pre = p + ">"
            diff = (other[p][1] != main[p][1] or other[p][2] != main[p][2]) or any(
                k in other and (other[k][1] != v[1] or other[k][2] != v[2])
                for k, v in main.items() if k.startswith(pre))
            if diff and not any(p.startswith(q + ">") for q in out):
                out.append(p)
        return out

    def _inject_dom_variants(self, page, loads):
        """窄屏 DOM 变体(见 ALT_EXTRACT_JS): 结构不同的最上层子树把窄屏版并列插入主页。"""
        if not loads or not self.cfg.get("viewport_variants", True):
            return 0
        main = page.evaluate(CV_PATHS_JS) or {}
        pages = dict(getattr(self, "_cv_pages", []) or [])
        if getattr(config, "NARROW_PARALLEL_WAITS", False):
            # 各页「滚完 → 400ms → 回顶 → 静置 → 稳定」原本逐页阻塞等(平板等完才轮到手机), 改为先在每张页里同时起这条链,
            # 逐页处理时只取结果; 网络静置仍逐页等(之后若真等了才重测稳定)
            for p2 in pages.values():
                CaptureMixin._kick_settle_tail(p2)
        variants = {}                      # path -> {tag: html}
        hidden_all = set()
        style_pre = {}                     # tag -> STYLE_TEXTS_JS 结果(窄屏页提前关闭前取)
        for tag, other in loads.items():
            if tag.startswith("__"):
                continue
            diff = sorted(p for p, v in main.items() if p and not p.startswith("@") and p in other
                          and other[p][0] == v[0] and other[p][3] != v[3])
            # 子元素标签序列相同、但某个带 id 的子项只在一侧存在(另一侧整页都没有这个 id): 两页在这个容器里各有对方
            # 没有的组件, 按序号对齐会把不同组件配成一对, 变体只换其中一个、另一侧独有的兄弟丢掉(wix 手机首屏表单:
            # 原站多一个占第三行的 0 高兄弟, 克隆少一道行距, 其下整页上移 16px)。把变体根提到这个容器, 整个换成窄屏版。
            # id 每次加载都变的站(随机 id)会大面积命中, 超过上限就整体不用这条判据
            if getattr(config, "VARIANT_ID_ALIGN", True):
                lifted = _id_misaligned_parents(main, other)
                if lifted:
                    if len(lifted) > int(getattr(config, "VARIANT_ID_ALIGN_MAX", 12)):
                        self.log("RENDER", f"窄屏首载页 {tag}: 子项 id 错位的容器 {len(lifted)} 个超上限, 视为 id 不稳定, 不按 id 提升变体根")
                    else:
                        self.log("RENDER", f"窄屏首载页 {tag}: {len(lifted)} 个容器的子项 id 与主页错位(各有对方没有的组件), 变体根提到容器: "
                                           + "; ".join(q[-60:] for q in lifted[:4]))
                        diff = sorted(set(diff) | set(lifted))
            top = []
            for p in diff:
                if not any(p.startswith(q + ">") for q in top):
                    top.append(p)
            if tag not in pages:
                continue
            # 桌面不渲染、窄屏可见的子树: 类或内联样式与窄屏首载页不同就整棵换成窄屏版(状态只在窄屏页里可信)
            tk = [time.monotonic()]   # 分步耗时(远程归因「窄屏收回与变体」慢在哪一步)
            hidden_tops = CaptureMixin._hidden_root_variants(self, page, pages[tag], main, other, top)
            tk.append(time.monotonic())
            hidden_all.update(hidden_tops)
            top += hidden_tops
            if not top or len(top) > config.VIEWPORT_VARIANT_MAX:
                continue
            stable, counts = CaptureMixin._settle_narrow_page(pages[tag])
            self._cv_unstable = dict(getattr(self, "_cv_unstable", {}) or {})
            self._cv_unstable[tag] = not stable
            tk.append(time.monotonic())
            # 随宽对照只做手机段: 手机机型宽度 360~440 一档内连续变化, 平板段(768/820)两档之间断点换布局, 来回改宽
            # 反而让图表/轮播停在错态(vite 平板 98→64、iPad Air 87→77)
            if tag == "m":
                CaptureMixin._fluidize_narrow_page(self, pages[tag], tag)
            tk.append(time.monotonic())
            mc = getattr(self, "_main_counts", {}) or {}
            self.log("RENDER", f"窄屏首载页 {tag}: {'到位' if stable else '到时限仍在变化'}, 控件 {counts.get('ctrl', '?')}"
                               f"(主页 {mc.get('ctrl', '?')}) / 按钮 {counts.get('btn', '?')}(主页 {mc.get('btn', '?')})"
                               f" / 文字 {counts.get('text', '?')}(主页 {mc.get('text', '?')}); 结构不同子树 {len(top) - len(hidden_tops)}"
                               + (f", 桌面不渲染而窄屏状态不同的子树 {len(hidden_tops)}" if hidden_tops else "")
                               + (f"; 到位分步 {counts.get('_steps')}" if counts.get("_steps") else ""))
            # 窄屏首载页同样做状态类相位复位: 它滚到底再回顶后, 进视口才展开/自动轮换的同类组件(折叠卡、高亮卡)已不在
            # 源码 HTML 的首载分配上, 变体把那一刻的状态烘进交付页(主页做过复位, 变体没做: 手机端多展开一张卡, 其下整页错位)。
            # 源码用该页自己上下文再取一次(手机 UA 下发的 HTML 可能不同), 取不到退回主页源码
            CaptureMixin._timer_rewind(self, pages[tag], f"窄屏首载页 {tag}")
            if getattr(config, "NARROW_PHASE_RESET", True):
                with suppress(Exception):
                    src2 = None
                    with suppress(Exception):
                        r2 = pages[tag].request.get(pages[tag].url, timeout=6000)
                        src2 = r2.text() if r2 is not None and r2.ok else None
                    cls2, grp2 = CaptureMixin._phase_src(self, src2)
                    if not cls2 and not grp2:
                        cls2, grp2 = CaptureMixin._phase_src(self, getattr(self, "_src_html", None))
                    if cls2 or grp2:
                        ph = pages[tag].evaluate(STATE_PHASE_RESET_JS, [cls2, grp2]) or {}
                        if ph.get("els") or ph.get("cand"):
                            self.log("RENDER", f"窄屏首载页 {tag} 状态类相位复位: {ph.get('groups', 0)} 组 / {ph.get('els', 0)} 个实例复位成源码分配"
                                               f"({'本页源码' if src2 else '主页源码'}); 候选组 {ph.get('cand', 0)} {ph.get('dbg') or ''}"[:700])
            tk.append(time.monotonic())
            with suppress(Exception):
                fin = pages[tag].evaluate(TRANSITION_FINISH_JS) or 0
                if fin:
                    self.log("RENDER", f"窄屏首载页 {tag}: 抽取前 {fin} 个进行中的 CSS 过渡跳到终点(轮播翻页半途两项同时可见)")
            tk.append(time.monotonic())
            with suppress(Exception):
                htmls = pages[tag].evaluate(ALT_EXTRACT_JS, top) or {}
                tk.append(time.monotonic())
                self.log("RENDER", f"窄屏首载页 {tag} 分步耗时: " + " / ".join(
                    f"{k} {b - a:.1f}s" for k, a, b in zip(("隐藏子树", "到位", "随宽", "相位", "过渡", "抽取"), tk, tk[1:])))
                sizes = page.evaluate(SUBTREE_SIZE_JS, list(htmls))
                normed = sum((v[4] if len(v) > 4 else 0) for v in htmls.values())
                if normed:
                    self.log("RENDER", f"窄屏首载页 {tag}: {normed} 个互斥兄弟组(轮播项/标签页)当前项对调到第一项(与原站首载同相位)")
                for p, (h, alt_text, alt_n, *alt_rest) in htmls.items():
                    main_text, main_n, *main_rest = sizes.get(p, (0, 0, 0))
                    alt_ctrl = alt_rest[0] if alt_rest else 0
                    main_ctrl = main_rest[0] if main_rest else 0
                    # 窄屏版明显"更空": 多半是异步内容(赞助位/推荐流)还没到, 不是换了结构
                    # (桌面不渲染的子树 innerText 退化为 textContent, 含未激活项的文字, 不比文字量)
                    # 例外(两处判据共用): 同 UA 的 0.92 倍宽对照页是另一次独立加载, 它在该子树下的结构与本页逐节点相同
                    # → 两次加载一致, 是这个宽度本来就这样(手机版换成紧凑列表/表单只留邮箱), 不是内容没到或水合时序
                    pair = ((loads.get("__pair") or {}).get(tag) or {}).get("paths") or {}
                    confirmed = bool(config.VARIANT_PAIR_CONFIRM and pair
                                     and _subtree_sig(loads.get(tag) or {}, p) == _subtree_sig(pair, p))
                    if ((alt_text < 0.5 * main_text and p not in hidden_tops) or alt_n < 0.3 * main_n) and not confirmed:
                        self.log("RENDER", f"窄屏 DOM 变体: {p[:60]} 的 {tag} 版内容明显更少(文字 {alt_text}/{main_text}, 节点 {alt_n}/{main_n}), 视为未到位, 不用")
                        continue
                    # 窄屏版表单控件比主页少: 组件晚水合(wix 首屏邮箱表单在窄屏页里时有时无 → 手机端整块表单消失),
                    # 不是响应式换结构; 退回主页子树 + 分视口样式
                    if main_ctrl and alt_ctrl < main_ctrl:
                        if confirmed:
                            self.log("RENDER", f"窄屏 DOM 变体: {p[:60]} 的 {tag} 版表单控件 {alt_ctrl} < 主页 {main_ctrl}, "
                                               f"但对照页(另一次加载)结构相同, 采用窄屏版")
                        else:
                            self.log("RENDER", f"窄屏 DOM 变体: {p[:60]} 的 {tag} 版表单控件 {alt_ctrl} < 主页 {main_ctrl}, 视为未水合, 不用")
                            continue
                    if alt_ctrl > main_ctrl:
                        self.log("RENDER", f"窄屏 DOM 变体: {p[:60]} 的 {tag} 版表单控件 {alt_ctrl} > 主页 {main_ctrl}(主页该组件可能未水合), 采用窄屏版")
                    variants.setdefault(p, {})[tag] = h
            if getattr(config, "NARROW_PAGE_EARLY_CLOSE", True):
                # 这张窄屏页用完了: 它的运行时样式现在取(后面只用这一次), 然后关页, 别让它在下一张页的到位/随宽
                # 等待期间继续跑动画抢 CPU
                if any(tag in v for v in variants.values()):
                    with suppress(Exception):
                        style_pre[tag] = pages[tag].evaluate(STYLE_TEXTS_JS) or []
                CaptureMixin._grab_fresh_storage(self, pages[tag])
                with suppress(Exception):
                    pages[tag].close()
        if not variants:
            return 0
        total = sum(len(h) for v in variants.values() for h in v.values())
        if total > config.VIEWPORT_VARIANT_BYTES:
            self.log("RENDER", f"窄屏 DOM 变体 {len(variants)} 处共 {total // 1024}KB 超上限, 不注入")
            return 0
        # 只在手机 UA 下换结构的子树: 桌面 UA 同宽页该处子元素结构与主页相同(状态类隐藏子树除外)
        md = loads.get("__mdesk") or {}
        ua_only = {p for p, v in variants.items() if "m" in v and md and p not in hidden_all and p in md and p in main
                   and md[p][0] == main[p][0] and md[p][3] == main[p][3]
                   and (len(md[p]) <= 8 or len(main[p]) <= 8 or md[p][8] == main[p][8])}
        if ua_only:
            self.log("RENDER", f"窄屏 DOM 变体: {len(ua_only)} 处只在手机 UA 下换结构, 只给触屏显示(桌面浏览器窄窗口仍用主页版): "
                               + "; ".join(p[-60:] for p in list(ua_only)[:4]))
        items = []
        for p, v in variants.items():
            alts = []
            mt = "mt" if p in ua_only else "m"
            if len(v) == 2 and v.get("t") == v.get("m"):
                alts.append({"bands": "t " + mt, "html": v["t"]})
            else:
                alts += [{"bands": (mt if t == "m" else t), "html": h} for t, h in v.items()]
            own = " ".join(["d"] + [t for t in ("t", "m") if t not in v] + (["m", "mx"] if p in ua_only else []))
            items.append({"path": p, "own": own, "alts": alts})
        main_rows = page.evaluate(STYLE_TEXTS_JS) or []
        css_main = {(g, t) for g, t, *_ in main_rows}
        extra, by_sheet, hidden_skipped, reset_skipped = [], {}, 0, 0
        for tag in {t for v in variants.values() for t in v}:
            with suppress(Exception):
                rows = style_pre[tag] if tag in style_pre else (pages[tag].evaluate(STYLE_TEXTS_JS) or [])
                for g, t, *rest in rows:
                    if (g, t) not in css_main:
                        if _is_transient_hide_rule(t):
                            hidden_skipped += 1
                            continue
                        if _is_global_reset_rule(t):
                            reset_skipped += 1
                            continue
                        extra.append((g, t))
                        by_sheet[(tag, rest[0] if rest else "")] = by_sheet.get((tag, rest[0] if rest else ""), 0) + 1
        css = _regroup_css_rules(dict.fromkeys(extra))[:config.VIEWPORT_VARIANT_BYTES]
        if hidden_skipped or reset_skipped:
            self.log("RENDER", f"窄屏运行时样式: 跳过 {hidden_skipped} 条入场动画隐藏态规则(visibility/opacity/display/transform)"
                               f", {reset_skipped} 条标签级全局重置/@font-face(不注入手机段)")
        if extra:
            # 归因: 多出的规则来自窄屏页哪些样式表, 主页又有哪些表(定位"主页没有"是真窄屏专属还是采集/序列化差异)
            sheets_main = {}
            for _g, _t, *rest in main_rows:
                sheets_main[rest[0] if rest else ""] = sheets_main.get(rest[0] if rest else "", 0) + 1
            src = "; ".join(f"{tag}:{sid[:110]}×{n}" for (tag, sid), n in sorted(by_sheet.items(), key=lambda kv: -kv[1])[:4])
            mains = "; ".join(f"{sid[:110]}×{n}" for sid, n in sorted(sheets_main.items(), key=lambda kv: -kv[1])[:9])
            sample = "; ".join((g.replace("\x00", ">")[:40] + " | " + t[:70]) for g, t in extra[:2])
            self.log("RENDER", f"窄屏运行时样式: 主页没有的规则 {len(extra)} 条({len(css) // 1024}KB), 来源 [{src}]; 主页表 [{mains}]; 样例: {sample}")
        if re.search(r"</style", css, re.I):
            css = ""
        n = page.evaluate(ALT_INJECT_JS, {"items": items, "css": css})
        if n:
            fixed = 0
            with suppress(Exception):
                fixed = page.evaluate("window.__cvAltImgFixed || 0")
            self.log("RENDER", f"窄屏 DOM 变体: {len(items)} 处子树结构随视口不同, 并列插入 {n} 个窄屏版"
                               f"({total // 1024}KB, 附窄屏运行时样式 {len(css) // 1024}KB"
                               + (f", 补 {fixed} 张无 src 的图" if fixed else "") + ")")
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
    def _nav_tracker(page) -> dict:
        """导航前挂上的请求追踪(与 _wait_quiet 同口径): 从 goto 起就记在途请求与 load 事件,
        静置判定不再漏掉 DOMContentLoaded 之前发出的请求。"""
        skip = ("websocket", "eventsource", "media", "manifest", "other")
        tr = {"inflight": {}, "ignored": {}, "last": [time.monotonic()], "load": [None],
              "t0": time.monotonic()}

        def _on_req(req):
            if req.resource_type in skip or beacon_like(req.url):
                tr["ignored"][id(req)] = time.monotonic()   # 不计静置, 但会拖住 load
                return
            tr["inflight"][id(req)] = time.monotonic()
            tr["last"][0] = time.monotonic()

        def _on_done(req):
            tr["inflight"].pop(id(req), None)
            tr["ignored"].pop(id(req), None)

        def _on_load(*_a):
            if tr["load"][0] is None:
                tr["load"][0] = time.monotonic()

        tr["handlers"] = (("request", _on_req), ("requestfinished", _on_done),
                          ("requestfailed", _on_done), ("load", _on_load))
        for ev, fn in tr["handlers"]:
            page.on(ev, fn)
        return tr

    @staticmethod
    def _wait_quiet_tracked(page, cap_ms: int, tr: dict) -> bool:
        """静置等待(导航前已挂追踪): load 已触发 + 静置即走; load 未触发但在途的只剩挂起超过
        STALE_MS 的请求(慢第三方像素/长连接拖住 load 事件)且静置, 也放行 —— 旧实现先阻塞等 load
        (上限 9s)再判静置, 这类站每次白等到上限。首屏图没到齐由基准截图前的等图兜底。"""
        quiet_s, stale_s = config.NAV_QUIET_MS / 1000, config.NAV_STALE_MS / 1000
        deadline = time.monotonic() + cap_ms / 1000
        try:
            while time.monotonic() < deadline:
                now = time.monotonic()
                inflight = tr["inflight"]
                live = sum(1 for t in list(inflight.values()) if now - t < stale_s)
                if not live and now - tr["last"][0] >= quiet_s:
                    if tr["load"][0] is not None or inflight or tr["ignored"]:
                        return True   # load 已到, 或只剩挂起的请求/信标/长连接在拖 load
                    # 追踪表空却没等到 load: 可能是不追踪的类型(媒体/长连接)在拖, 退回旧口径再等 load
                    with suppress(Exception):
                        page.wait_for_load_state("load", timeout=max(1, int((deadline - now) * 1000)))
                    return True
                page.wait_for_timeout(100)
            return False
        finally:
            for ev, fn in tr.get("handlers", ()):
                with suppress(Exception):
                    page.remove_listener(ev, fn)

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


    def _capture_baseline(self, page, _safe_eval, screenshot: bool):
        """纯净基准: 等可见图 → 时间冻结 → 冻结后补等 → 整页截图 + 文本指纹 → 动画烘焙; 截图前后两次版面块图
        取尺寸未变的块(layout_map.json)。返回 (baseline_text, layout_stable)。"""
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
            # 基准也按源码相位: 定时器已清, 把轮换过的同类组件(轮播/折叠卡)先复位成源码 HTML 的分配再截。否则基准是
            # 「采集那一刻转到的第 k 项」、克隆是复位后的第一项, 布局校准会按基准把收起的那项撑回展开时的高度(一块空白)
            self._timer_rewind(page, "基准截图前")
            if getattr(config, "BASELINE_PHASE_RESET", True):
                with suppress(Exception):
                    src_cls, src_grp = self._phase_src(getattr(self, "_src_html", None))
                    if src_cls or src_grp:
                        ph = page.evaluate(STATE_PHASE_RESET_JS, [src_cls, src_grp]) or {}
                        if ph.get("els"):
                            self.log("RENDER", f"基准截图前状态类相位复位: {ph.get('groups', 0)} 组 / {ph.get('els', 0)} 个实例 {ph.get('dbg') or ''}"[:500])
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
        return baseline_text, layout_stable

    def _replay_clock_t0(self):
        """时钟锚点(毫秒): 本次采集第一张页面创建的时刻; 采集各页与克隆回放的 Date 都从这一刻起走。"""
        if not getattr(config, "REPLAY_CLOCK", True):
            return None
        if not getattr(self, "_clock_t0", None):
            self._clock_t0 = int(time.time() * 1000)
        return self._clock_t0

    def _seed_page(self, page):
        """采集侧页面装上种子随机数(与克隆页同一套, 见 js.SEED_RANDOM_JS); 须在导航前调用。"""
        if getattr(config, "REPLAY_SEED_RANDOM", True):
            with suppress(Exception):
                page.add_init_script(seed_random_js(getattr(config, "REPLAY_SEED", 0), t0_ms=self._replay_clock_t0()))
        if getattr(config, "TIMER_JOURNAL", True):
            with suppress(Exception):
                page.add_init_script(TIMER_JOURNAL_JS)
        return page

    def _timer_rewind(self, page, where: str):
        """长定时器改过的周期性状态(轮播帧/轮换横幅/进度条)退回初始值, 见 js.TIMER_JOURNAL_JS。"""
        if not getattr(config, "TIMER_JOURNAL", True):
            return
        with suppress(Exception):
            mode = 0 if where.startswith("序列化前") else 2 if getattr(config, "TIMER_JOURNAL_STYLE", False) \
                else 1 if getattr(config, "TIMER_JOURNAL_LINKED", True) else 0
            tj = page.evaluate(TIMER_JOURNAL_REWIND_JS, mode) or {}
            if tj.get("els"):
                self.log("RENDER", f"定时器改动回退({where}): {tj.get('els')} 个元素(class 词 {tj.get('cls', 0)} / 样式属性 {tj.get('sty', 0)})"
                                   f"退回首次被定时器改动前的值, 定时器共触发 {tj.get('fires')} 次 {tj.get('dbg') or ''}"[:500])

    def _render_page(self, ctx, url: str, screenshot: bool, collect_links: bool, is_frame: bool = False):
        page = self._seed_page(ctx.new_page())
        rt = self.cfg.get("render_timeout")  # 批次级页面加载超时(秒), 0/缺省 = 平台默认
        try:
            page.set_default_timeout(int(float(rt) * 1000) if rt else config.PAGELOAD_TIMEOUT)
        except (TypeError, ValueError):
            page.set_default_timeout(config.PAGELOAD_TIMEOUT)
        if self.cfg.get("mock_api", True):
            page.on("response", self._on_response)
        page.on("response", self._on_asset_response)  # 活体资源采集(恒开)
        self._nav_failed = []
        page.on("requestfailed", self._on_nav_request_failed)
        self.log("RENDER", f"导航 {url}")
        t_nav, t_nav_m = time.time(), time.monotonic()
        nav_tr = None
        if getattr(config, "NAV_STALE_GATE", True):
            with suppress(Exception):
                nav_tr = self._nav_tracker(page)
        self._src_html = None
        try:
            _resp = page.goto(url, wait_until="domcontentloaded", timeout=config.PAGELOAD_TIMEOUT)
            with suppress(Exception):
                self._src_html = _resp.text() if _resp is not None else None
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
        if screenshot and getattr(config, "VIEWPORT_RELOAD_EARLY", True) \
                and (os.cpu_count() or 1) >= getattr(config, "VIEWPORT_RELOAD_EARLY_MIN_CPUS", 8):
            # 窄屏首载对照页提前到主页导航提交后就发起: 与主页静置/懒加载滚动/多视口采集并行加载,
            # 固化阶段收回时多已加载完(旧实现在多视口采集后才发起, 固化阶段要等它们 load + 到位滚动)
            vp = page.viewport_size or {}
            if (vp.get("width") or 0) >= 1024 and self.cfg.get("viewport_probe", True) \
                    and self.cfg.get("viewport_reload", True):
                self._start_narrow_loads(page, vp.get("height") or 0)
        quiet_ok = (self._wait_quiet_tracked(page, config.NETWORK_IDLE_TIMEOUT, nav_tr) if nav_tr
                    else self._wait_quiet(page, config.NETWORK_IDLE_TIMEOUT))
        if not quiet_ok:
            self.log("RENDER", "网络静置等待超时, 继续后续固化流程")
        self._nav_ready_s = time.time() - t_nav
        self._storage_first = None
        self._fresh_storage = []
        if screenshot and getattr(config, "STATE_SKIP_SELF_WRITTEN", True):
            # 主页自然首载静置后(引擎点弹层/滚动之前)就有的存储键, 终态快照时与全新上下文窄屏首载页对比(见下)
            with suppress(Exception):
                self._storage_first = page.evaluate(STORAGE_SNAPSHOT_JS)
        if nav_tr:
            t_load = nav_tr["load"][0]
            hang = len(nav_tr["ignored"]) + sum(1 for t in nav_tr["inflight"].values()
                                                if time.monotonic() - t >= config.NAV_STALE_MS / 1000)
            self.log("RENDER", f"页面就绪 {self._nav_ready_s:.1f}s ("
                     + (f"load {t_load - nav_tr['t0']:.1f}s" if t_load else f"load 未触发, 挂起请求 {hang} 个")
                     + ")")
        else:
            self.log("RENDER", f"页面就绪 {self._nav_ready_s:.1f}s")
        if screenshot:
            with suppress(Exception):
                rt = page.evaluate(RUNTIME_FP_JS) or []
                self._runtime_hits = set(rt)
                if rt:
                    self.log("PATTERN", f"活体运行时证据: {', '.join(rt)}")
        tp = self._phase("采集-导航", t_nav_m)
        # 就绪时页面里的脚本数与同源以外的子资源: 序列化过薄时据此区分"本来就小的纯静态页"与"脚本壳被清空"
        n_scripts = n_els_ready = None
        with suppress(Exception):
            n_scripts, n_els_ready = page.evaluate(
                "() => [document.scripts.length + document.querySelectorAll('iframe,frame,meta[http-equiv=refresh i]').length,"
                " document.body ? document.body.getElementsByTagName('*').length : 0]")
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
        self._settle_main_page(page)
        tp = self._phase("采集-懒加载滚动", tp)
        if screenshot:
            self._probe_viewports(page)
            self._probe_root_font(page)
            tp = self._phase("采集-多视口内联", tp)
        # 评分基准(纯净原站): 必须在任何引擎 DOM 改动(表单激活/DEEP_FREEZE/
        # 水合剥离/字体烘焙)之前截取 —— 否则评分比较的是"克隆 vs 引擎改过的
        # 原站", 引擎自身引入的失真被基准同步吸收, 分数系统性偏乐观。
        baseline_text, layout_stable = self._capture_baseline(page, _safe_eval, screenshot)
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
        self._timer_rewind(page, "冻结后")
        with suppress(Exception):
            src_cls, src_grp = self._phase_src(getattr(self, "_src_html", None))
            if not src_cls and not src_grp:
                self.log("RENDER", f"状态类相位复位跳过: 源码 HTML {'未取到' if not getattr(self, '_src_html', None) else '无带 id 的 class、无同类兄弟组'}")
            else:
                ph = page.evaluate(STATE_PHASE_RESET_JS, [src_cls, src_grp]) or {}
                if ph.get("els") or ph.get("cand"):
                    self.log("RENDER", f"状态类相位复位: {ph.get('groups', 0)} 组同类组件的 open/active 状态在采集时已轮换, "
                                       f"{ph.get('els', 0)} 个实例复位成源码 HTML 的分配(克隆与原站同相位起步); 候选组 {ph.get('cand', 0)} {ph.get('dbg') or ''}"[:900])
        with suppress(Exception):
            # 定时器已停, 弹层脚本不会再把锁写回
            n_lock = page.evaluate(SCROLL_LOCK_STRIP_JS) or 0
            if n_lock:
                self.log("RENDER", f"滚动锁剥离: html/body 内联 style 上 {n_lock} 条弹层打开时写的锁滚动声明(overflow:hidden/position/inset)不进交付 HTML")
        if getattr(self, "_root_fluid", None):
            # 定时器已停, 页面脚本不会再改回 px
            with suppress(Exception):
                page.evaluate(ROOT_FONT_APPLY_JS, self._root_fluid)
        fl = getattr(self, "_fluid", None)
        if fl and (fl["paths"] or fl["inline"]):
            with suppress(Exception):
                page.evaluate(FLUID_APPLY_JS, fl)
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
                 f"{counts.get('crossOriginSheets', 0)} 个跨域样式表"
                 + (f" / 内联表原样 {counts.get('cssSkipped')} 组未重复序列化" if counts.get("cssSkipped") else ""))
        # 外链样式表的内联副本去重: 文件本身已随包, 运行时未改动的表其序列化副本是纯重复
        # (全语料均值 215KB/页, 占交付 HTML 37%)。浏览器自己解析比对, 不一致/取不到原文的保留。
        if counts.get("css"):
            dd = {}
            with suppress(Exception):
                dd = page.evaluate(CSSOM_LINK_DEDUPE_JS) or {}
            if dd.get("removed"):
                self.log("CSSOM", f"外链表内联副本去重: 移除 {dd['removed']}/{dd.get('checked', 0)} 块"
                                  f"({int(dd.get('bytes', 0)) // 1024} KB), 保留 {dd.get('kept', 0)} 块(运行时改过/取不到原文)")
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
            baked = _safe_eval(FONT_BAKE_NOSIZE_JS if (getattr(self, "_root_fluid", None) or getattr(self, "_root_inline_varies", False)) else FONT_BAKE_JS)
            if baked:
                self.log("RENDER", f"字体度量固化: {baked} 个元素逐元素烘焙(差分继承)")
        except Exception:
            pass
        with suppress(Exception):
            fl = _safe_eval(INLINE_FLUID_JS) or {}
            if any(fl.values()):
                self.log("RENDER", f"内联尺寸流式化: 全宽 {fl.get('width', 0)} / 轨道 {fl.get('track', 0)}"
                                   f" / 位移 {fl.get('shift', 0)}(采集视口下不变, 换视口随容器伸缩)")
        td = self._phase("细分·冻结与字体烘焙", td)
        nt = [time.monotonic()]   # 窄屏阶段分段耗时: 收回(等加载+读路径) / 多视口样式落地 / 变体 / 关页
        try:
            try:
                loads = self._collect_narrow_loads()
                nt.append(time.monotonic())
                emit_loads = loads if config.NARROW_UA_CLS else {k: v for k, v in loads.items() if k != "__mdesk"}
                cv = page.evaluate(CV_EMIT_JS if getattr(config, "CANVAS_FILL_BANDS", True)
                                   else CV_EMIT_JS.replace("const CVFILL = 1;", "const CVFILL = 0;"), emit_loads) or {}
                nt.append(time.monotonic())
                if cv.get("els") or cv.get("cls") or cv.get("srcFill") or cv.get("textFill"):
                    self.log("RENDER", f"多视口内联样式: {cv['els']} 个元素, {cv.get('vw', 0)} 个按视口宽比例改写为 vw"
                                       + (f"(另 {cv['pairvw']} 个手机段 px 由 0.92 倍宽首载对照认出同比)" if cv.get("pairvw") else "")
                                       + (f"(手机段两点仿射 {cv['affine']} 个)" if cv.get("affine") else "")
                                       + (f"(手机段内容高改 auto {cv['contentH']} 个)" if cv.get("contentH") else "") + ","
                                       f" {cv.get('banded', 0)} 个移入分视口样式表(其中窄屏首载补采 {cv.get('reload', 0)})"
                                       f"; 分视口 class {cv.get('cls', 0)} 个"
                                       + (f"(其中只随手机 UA 变、只给触屏 {cv['clsTouch']} 个)" if cv.get("clsTouch") else "")
                                       + (f"; 只随 UA 的状态词触屏上先拿掉 {cv['touchOff']} 个元素" if cv.get("touchOff") else "")
                                       + (f"; 桌面隐藏元素补窄屏 src {cv['srcFill']} 个" if cv.get("srcFill") else "")
                                       + (f", 补文本 {cv['textFill']} 个" if cv.get("textFill") else "")
                                       + (f"; 固化 canvas 按缩放前尺寸定盒 {cv['cvFix']} 个" if cv.get("cvFix") else ""))
                if cv.get("pairdbg"):
                    self.log("RENDER", f"手机段首载对照同比明细: {json.dumps(cv['pairdbg'], ensure_ascii=False)[:1500]}")
                if cv.get("cvdbg"):
                    self.log("RENDER", f"固化 canvas 分视口对照: {json.dumps(cv['cvdbg'], ensure_ascii=False)[:1500]}")
                try:
                    self._inject_dom_variants(page, loads)
                except Exception as exc:
                    self.log("WARN", f"窄屏 DOM 变体注入失败: {str(exc)[:160]}")
                nt.append(time.monotonic())
            finally:
                t_close = time.monotonic()
                self._close_narrow_loads()
                if time.monotonic() - t_close > 3:
                    self.log("RENDER", f"窄屏首载页关闭耗时 {time.monotonic() - t_close:.1f}s")
                nt.append(time.monotonic())
                if len(nt) == 5 and nt[-1] - nt[0] >= 5:
                    self.log("RENDER", "窄屏阶段分段: " + " / ".join(
                        f"{k} {b - a:.1f}s" for k, a, b in zip(("收回", "多视口样式", "变体", "关页"), nt, nt[1:])))
        except Exception as exc:   # noqa: BLE001 以前静默吞掉: 该站手机端没有分视口样式/变体却看不出原因(qwik 手机 41/100 双峰)
            self.log("WARN", f"多视口内联样式/窄屏变体整步失败: {type(exc).__name__}: {str(exc)[:200]}")
        # 序列化前再做一次状态类相位复位: 第一次复位之后页面仍活着(多视口样式/变体注入期间组件框架重渲染会按自己的
        # 状态把 class 写回去, 或进视口观察器又展开一张), 复位过的展开态回到交付 HTML 里(wix 手机多展开一张卡, 整页错位)
        self._timer_rewind(page, "序列化前复查")
        if getattr(config, "LATE_PHASE_RESET", True):
            with suppress(Exception):
                src_cls, src_grp = self._phase_src(getattr(self, "_src_html", None))
                if src_cls or src_grp:
                    ph = page.evaluate(STATE_PHASE_RESET_JS, [src_cls, src_grp]) or {}
                    if ph.get("els"):
                        self.log("RENDER", f"状态类相位复位(序列化前复查): 又有 {ph.get('els', 0)} 个实例偏离源码分配, 已再次复位 {ph.get('dbg') or ''}"[:600])
        # 非法嵌套(<p> 里的块级容器)序列化后会被解析器拆树: MHTML 与交付 HTML 之前改成等价 span(见 NEST_FIX_JS)
        if getattr(config, "NEST_FIX", True):
            with suppress(Exception):
                nf = _safe_eval(NEST_FIX_JS) or {}
                if nf.get("fixed") or nf.get("reverted") or nf.get("other"):
                    self.log("RENDER", f"非法嵌套修复: <p> 内块级容器 {nf.get('fixed', 0)} 个改等价 span"
                                       f"(样式有差异换回 {nf.get('reverted', 0)}; 其它拆树块 {nf.get('other', 0)} 个未动)")
        # N1 保真轨: MHTML 快照(采集期活体渲染的权威固化)。
        # Chromium 对 MHTML 的回放默认禁脚本 —— 确定性由构造保证。
        if screenshot:
            try:
                td = self._phase("细分·窄屏收回与变体", td)
                cdp = ctx.new_cdp_session(page)
                # 卡死归因: CDP 调用没有超时, 163/wix 偶发在窄屏变体之后整任务无日志挂死(疑在此处或关窄屏页)
                self._diag_mark("保真轨 MHTML 快照开始")
                try:
                    snap = bounded_api_call(cdp, "send", ('Page.captureSnapshot', {'format': 'mhtml'}),
                                            config.MHTML_SNAPSHOT_TIMEOUT_S)
                except TimeoutError:
                    snap = self._mhtml_retry(page, cdp)
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
        if screenshot and getattr(config, "DEPLOY_DEFER_IMG", True):
            # MHTML 已快照、基准已截: 此后只影响交付 HTML(部署首屏), 评分截图前会转回 eager
            with suppress(Exception):
                n_def = int(_safe_eval(f"() => ({DEPLOY_DEFER_IMG_JS})({config.DEPLOY_DEFER_SCREENS})") or 0)
                if n_def:
                    self.log("DEPLOY", f"首屏外图片延后: {n_def} 张改原生懒加载(部署后首屏不等页尾图片)")
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
        if len(html) < 5000 and n_scripts == 0 and getattr(config, "THIN_STATIC_NO_RETRY", True):
            # 就绪时页面没有任何脚本/子框架/meta 刷新: 没有东西能在采集窗口里清空或重定向它, 小就是它本来的
            # 大小(example.com 类文档页、无脚本的广告素材 iframe)。重导航只会拿到同一份 HTML, 白等 3~5s
            self.log("RENDER", f"序列化 {len(html)}B: 页面无脚本/子框架, 小体量即原貌, 不重导航")
        elif len(html) < 5000 and is_frame and getattr(config, "THIN_STATIC_NO_RETRY", True) \
                and self._not_degraded(page, n_els_ready):
            # 跨域 iframe 子页(登录框/订阅条/广告素材)本来就是小部件: 就绪以来元素没有减少即不是"被清空",
            # 不为它再整页导航一次(每个 3~5s, 单任务最多 6 个)
            self.log("RENDER", f"序列化 {len(html)}B: iframe 小部件自就绪以来未被清空, 不重导航")
        elif len(html) < 5000 and getattr(config, "THIN_STATIC_NO_RETRY", True) \
                and self._not_degraded(page, n_els_ready, min_text=200):
            # 带脚本的小页: 画面上有成段可见文字且元素自就绪以来没有减少 —— 不是挂载失败的空壳, 也没被清空;
            # 标记字节少只是页面本身小(example.com 现版 2.5KB / 917 字), 重导航拿不到更完整的结果
            self.log("RENDER", f"序列化 {len(html)}B: 页面有成段可见文字且自就绪以来未被清空, 不重导航")
        elif len(html) < 5000:
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
                first = getattr(self, "_storage_first", None) or {}
                fresh = getattr(self, "_fresh_storage", None) or []
                n_drop = 0
                for kind in ("local", "session"):
                    cur, f0 = storage.get(kind) or {}, first.get(kind) or {}
                    for k in list(cur):
                        # 主页自然首载时就写下、且另一个全新上下文的首访页写出同一个值: 站点每次首访都会自己写,
                        # 克隆的脚本也会再写一遍; 预先写回反而让脚本按回访者渲染(苹果顶栏 ribbon="下次显示第几条",
                        # 首访即写 2, 写回后克隆显示第 2 条长文案, 768 宽折两行整页下移)。随机分桶类键各上下文不同, 照旧写回
                        if k in f0 and any((fs.get(kind) or {}).get(k) == cur[k] for fs in fresh):
                            del cur[k]
                            n_drop += 1
                if n_drop:
                    self.log("RENDER", f"状态快照: {n_drop} 个存储键是站点每次首访自己写的(另一全新上下文首访同值), 不写回")
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


    def _on_nav_request_failed(self, req):
        """采集期主页失败请求(排除引擎/媒体主动取消): 晚水合组件的脚本块或接口没到, 克隆整块缺失时据此归因。"""
        try:
            fail = (req.failure or "") if isinstance(req.failure, str) else str(req.failure or "")
            if req.resource_type in ("media", "websocket", "eventsource", "other") or "ERR_ABORTED" in fail \
                    or "BLOCKED_BY_CLIENT" in fail:
                return
            lst = getattr(self, "_nav_failed", None)
            if lst is not None and len(lst) < 40:
                lst.append((req.resource_type, req.url[:160], fail[:60]))
        except Exception:
            pass

    @staticmethod
    def _not_degraded(page, n_els_ready, min_text: int = 0) -> bool:
        """序列化时元素数不少于就绪时的 60%(且就绪时非空): 采集窗口内没有被清空/跳走;
        min_text > 0 时还要求当前可见文字不少于这么多字符(排除从一开始就没挂载上的空壳)。"""
        if not n_els_ready:
            return False
        try:
            now, text = page.evaluate("() => [document.body ? document.body.getElementsByTagName('*').length : 0,"
                                      " document.body ? (document.body.innerText || '').trim().length : 0]")
        except Exception:
            return False
        return now >= 0.6 * n_els_ready and text >= min_text

    def _settle_main_page(self, page):
        """懒加载滚动后等主页 DOM「到位」: 文字量/有源图片数/控件数连续不变(与窄屏首载页同口径,
        NARROW_STABLE_JS)。晚水合的表单/标签页组件(wix 首屏邮箱表单、模板标签行)在 load 与滚动之后才出现,
        抽早了基准与克隆同时缺它, 评分看不出, 手机端整块表单消失(~40 分)。到位与否与控件数都写日志,
        远程归因"同一引擎一次好一次坏"。"""
        cap = int(getattr(config, "MAIN_SETTLE_MS", 4000) or 0)
        if cap <= 0:
            return
        t0 = time.monotonic()
        stable, counts = 1, {}
        with suppress(Exception):
            stable = page.evaluate(NARROW_STABLE_JS, cap)
        with suppress(Exception):
            counts = page.evaluate(DOM_COUNTS_JS) or {}
        failed = list(getattr(self, "_nav_failed", []) or [])
        self._main_counts = counts
        msg = (f"页面到位({time.monotonic() - t0:.1f}s): 文字 {counts.get('text', '?')} / 图 {counts.get('imgs', '?')}"
               f"({counts.get('imgsrc', '?')} 有源) / 控件 {counts.get('ctrl', '?')} / 按钮 {counts.get('btn', '?')}"
               f" / 元素 {counts.get('els', '?')}")
        if not stable:
            msg += "; DOM 到时限仍在变化(按当前态采集)"
        if failed:
            kinds = {}
            for rt, _u, _f in failed:
                kinds[rt] = kinds.get(rt, 0) + 1
            msg += "; 采集期失败请求 " + ", ".join(f"{k}×{v}" for k, v in kinds.items())
            msg += " 样例 " + " | ".join(f"{u[-70:]} {f}" for _rt, u, f in failed[:3])
        self.log("RENDER", msg)

    def _unlock_scroll(self, page, when: str) -> int:
        """滚动前/后剥掉 html/body 内联的锁滚动声明。自动弹层(短信订阅/优惠券/Cookie 墙)在采集窗口内打开时
        给 html/body 写 overflow:hidden; position:fixed|absolute; inset:0 锁住页面: 文档高度只剩一屏, 自动滚动
        原地不动, IntersectionObserver 驱动的懒加载/入场显形全都不触发, 首屏以下整段空白被当成"原站本来如此"
        序列化并当作基准(allbirds: 基准与克隆都只有一屏, 保真轨 98.5 虚高; 对真机 4628px 的原站只有 38 分)。"""
        try:
            n = page.evaluate(SCROLL_LOCK_STRIP_JS) or 0
        except Exception:
            return 0
        if n:
            self.log("RENDER", f"{when}解锁滚动: 弹层给 html/body 写的 {n} 条锁滚动声明已剥离, 页面恢复可滚")
        return n

    def _auto_scroll(self, page):
        """平滑滚动到底再回顶, 触发 IntersectionObserver / 无限加载(见 AUTO_SCROLL_JS)。
        总时长受 AUTO_SCROLL_BUDGET 约束(页内计时, 一次往返)。滚动前后各查一次滚动锁: 滚动途中弹出的
        层锁住页面时, 剥离后再滚一遍。"""
        self._unlock_scroll(page, "滚动前")
        self._auto_scroll_once(page)
        if self._unlock_scroll(page, "滚动后"):
            self._auto_scroll_once(page)

    def _auto_scroll_once(self, page):
        if not config.AUTO_SCROLL_SMOOTH:
            return self._auto_scroll_stepped(page)
        try:
            res = page.evaluate(self.AUTO_SCROLL_JS,
                                [int(config.AUTO_SCROLL_BUDGET * 1000), 54000]) or {}
            if res.get("timeout"):
                self.log("RENDER", f"自动滚动达到时限({config.AUTO_SCROLL_BUDGET}s), "
                                   f"停在 {res.get('y')}/{res.get('h')}px")
            page.wait_for_timeout(600)
            page.evaluate(SCROLL_TOP_JS)
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
            height = 0
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
            vh = int((page.viewport_size or {}).get("height") or 0)
            if getattr(config, "SHORT_PAGE_NO_SCROLL_WAIT", True) and vh and height <= vh:
                # 一屏放得下的页面没有真的滚动过: 没有进视口事件要等, 也不用回顶
                return
            page.wait_for_timeout(600)
            page.evaluate(SCROLL_TOP_JS)
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
                                        collect_links=False, is_frame=True)
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
            # 请求指纹(查询参数 + 请求体摊平的键值对): 同一接口靠请求体区分内容时, 回放按它挑对应的那份录制
            req_sig = []
            if getattr(config, "API_REQ_SIG", True):
                post = None
                if request.method != "GET":
                    with suppress(Exception):
                        post = request.post_data
                req_sig = request_pairs(parts.query, post or "")
            sig_key = tuple(req_sig) if request.method != "GET" else ()
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
                    key = (request.method, parts.path or "/", parts.query, sig_key)
                    pk = ("#variants", request.method, parts.path or "/")
                    n_var = sum(1 for k in self._tape_seen if k[:3] == pk) if sig_key else 0
                    if key not in self._tape_seen and n_var < getattr(config, "API_VARIANTS_PER_PATH", 48):
                        self._tape_seen.add(key)
                        if sig_key:
                            self._tape_seen.add(pk + (n_var,))
                        self._net_tape.append({
                            "path": parts.path or "/",
                            "method": request.method,
                            "query": parts.query,
                            "status": response.status,
                            "content_type": content_type or "application/json",
                            "response_body": body,
                            **({"req_sig": req_sig} if req_sig else {}),
                        })
                        self._tape_bytes += len(body)
            if "json" not in content_type:
                return
            body = body if body is not None else response.text()
            if not body or len(body) > config.MAX_MOCK_BODY_BYTES:
                return
            self._mock_raw.append({
                "path": parts.path or "/",
                "host": parts.netloc,
                "method": request.method,
                "query": parts.query,
                "status": response.status,
                "response_body": body,
                **({"req_sig": req_sig} if req_sig else {}),
            })
            self.log("MOCK-API",
                     f"录制 {request.method} {parts.path} ({response.status})")
        except Exception:
            return


    def _on_asset_response(self, response, bodies=True):
        """活体资源采集: 浏览器渲染期真实成功加载的静态资源清单。
        bodies=False 只登记 URL 不留响应对象(窄屏首载对照页先于收尾关闭, 响应体取不回, 交给回补下载)。"""
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
            if bodies and response.status == 200 and request.resource_type in _LIVE_BODY_TYPES \
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
        # resp.body() 会阻塞到该响应下载完: 慢链路上几张还在传的大图曾让这一步串行等 91s
        # (cgnpc 高负载复盘, 原本毫秒级)。先只取已传完的(timing.responseEnd>=0), 未传完的交给
        # 并发 CAS 下载(本就要跑); 再给整步一个时限, 超时剩余的也交给 CAS。
        def _finished(r):
            try:
                t = r.request.timing
                t = t() if callable(t) else t
                return (t or {}).get("responseEnd", -1) != -1
            except Exception:
                return True   # 拿不到时序: 按旧行为处理(多半是小资源)
        def _size(r):
            try:
                return int(r.headers.get("content-length") or 0)
            except Exception:
                return 0
        # 小的先取: 单次 body() 在慢链路/高负载下可能阻塞数秒且无法中途打断, 把大响应排到最后,
        # 时限一到整体止损; 超过 LIVE_BODY_SINGLE_MAX 的大文件一律不经 CDP 取, 交并发 CAS 下载
        ordered = sorted(pending.items(), key=lambda kv: (not _finished(kv[1]), _size(kv[1]) or 1 << 20))
        skipped_unfinished = skipped_budget = 0
        for url, resp in ordered:
            if url in self._live_bodies or self._live_body_bytes >= config.LIVE_BODY_BYTES:
                continue
            if time.monotonic() - t0 > config.LIVE_BODY_HARVEST_BUDGET:
                skipped_budget += 1
                continue
            if not _finished(resp) or _size(resp) > config.LIVE_BODY_SINGLE_MAX:
                skipped_unfinished += 1
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
        if n or skipped_unfinished or skipped_budget:
            extra = ""
            if skipped_unfinished or skipped_budget:
                extra = (f"; 跳过 {skipped_unfinished} 个未传完 / {skipped_budget} 个超时限, "
                         f"交并发 CAS 下载")
            self.log("SMART-DL", f"活体响应体复用: 取回 {n} 个已加载资源 "
                                 f"({self._live_body_bytes // 1024} KB, {time.monotonic() - t0:.1f}s){extra}")


    def _finalize_mocks(self):
        entries, seen, per_path = [], set(), {}
        for entry in self._mock_raw:
            # 非 GET 请求体不同算不同的录制(同一接口靠请求体区分内容); 同一接口最多留 API_VARIANTS_PER_PATH 份,
            # 防轮询/心跳类接口(请求体带时间戳, 每次都不同)挤占名额
            sig = tuple(entry.get("req_sig") or ()) if entry["method"] != "GET" else ()
            key = (entry["method"], entry["path"], entry["query"], sig)
            if key in seen:
                continue
            pk = (entry["method"], entry["path"])
            if sig and per_path.get(pk, 0) >= getattr(config, "API_VARIANTS_PER_PATH", 48):
                continue
            per_path[pk] = per_path.get(pk, 0) + 1
            seen.add(key)
            entries.append(entry)
            if len(entries) >= config.MAX_MOCK_ENTRIES:
                break
        return entries
