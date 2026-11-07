"""ClonerEngine 评分: 克隆离线渲染与双轨评分、像素/布局校准、保真轨、静态版、修复轮快照(自 cloner_engine.py 按职责拆出的 mixin, 方法体未改)。"""
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


class ScoringMixin:

    def _try_static_track(self, ctx, inter_score):
        """预览/导出与头条分对齐: 头条分取两轨最优, 但预览与导出交付的是交互轨页面 ——
        交互轨塌了(SPA 路由在预览路径下渲染成 404 页、离线水合清空 DOM、脚本报错)而
        保真轨高分时, 用户看到的与分数不符(36kr 引擎 100、预览 16)。
        保真轨明显领先时, 生成"静态版"(采集 DOM 剥离应用脚本, 引擎注入物与 JSON 数据
        保留, 与 MHTML 保真轨同语义但资源已本地化), 用同一评分实测; 静态版也明显领先才
        设为默认页, 交互版另存 <页>.interactive.html。返回静态版分数(未采用返回 None)。"""
        fid = getattr(self, "_fidelity_score", None)
        margin = config.STATIC_TRACK_MARGIN
        if not (isinstance(fid, (int, float)) and isinstance(inter_score, (int, float))) \
                or fid - inter_score < margin:
            return None
        main = self._main_page_file()
        original = self.ws / "original_preview.png"
        if not (main.is_file() and original.is_file()):
            return None
        html = main.read_text(encoding="utf-8", errors="replace")
        static, stripped = _strip_hydration_html(html)
        if not stripped:
            return None
        t0 = time.monotonic()
        main.write_text(static, encoding="utf-8")
        server = _LocalPreviewServer(self.ws, use_tape=self._strategy_pack.net_tape)
        server.start()
        base = f"http://127.0.0.1:{server.port}"
        page = ctx.new_page()
        s_detail = None
        try:
            page.set_default_timeout(30000)
            with suppress(Exception):
                page.add_init_script(SCORE_TIMER_GUARD_JS)

            def _guard(route):
                try:
                    if route.request.url.startswith(base):
                        route.continue_()
                    else:
                        route.abort()
                except Exception:
                    with suppress(Exception):
                        route.abort()
            page.route("**/*", _guard)
            self._load_clone(page, base)
            shot = self.ws / "static_preview.png"
            self._full_shot(page, shot, "静态版")
            text = page.evaluate(TEXT_EXTRACT_JS) or ""
            orig_txt = self.ws / "original_text.txt"
            s_detail = compute_visual_similarity(
                original, shot, orig_text=orig_txt.read_text(errors="replace") if orig_txt.is_file() else "",
                clone_text=text)
        except Exception as exc:
            self.log("WARN", f"静态版评分失败: {exc}")
        finally:
            with suppress(Exception):
                page.close()
            with suppress(Exception):
                server.stop()
            self._phase("评分-静态版", t0)
        s_score = (s_detail or {}).get("score")
        if isinstance(s_score, (int, float)) and s_score - inter_score >= margin:
            main.with_name(main.stem + ".interactive.html").write_text(html, encoding="utf-8")
            self.log("RENDER", f"静态版交付: 交互轨 {inter_score} / 保真轨 {fid} / 静态版 {s_score}"
                               f"(剥离 {stripped} 个应用脚本); 预览与导出默认静态版, 交互版另存"
                               f" {main.stem}.interactive.html")
            return s_score
        main.write_text(html, encoding="utf-8")
        self.log("RENDER", f"静态版未领先(交互轨 {inter_score} / 静态版 {s_score}), 保留交互版交付")
        return None


    @staticmethod
    def _fidelity_guard(route):
        """保真轨离线守卫: 归档内资源由 Chromium 直接供给(不经网络层), 归档外的引用一律中止。"""
        try:
            if route.request.url.startswith(("file:", "data:", "blob:", "cid:")):
                route.continue_()
            else:
                route.abort()
        except Exception:
            with suppress(Exception):
                route.abort()


    def _prestart_fidelity(self, ctx):
        """后台页非阻塞加载 capture.mhtml(只等到提交), 供 _render_fidelity 接手。"""
        mh = self.ws / "capture.mhtml"
        if self._fidelity_rendered or not mh.is_file() or getattr(self, "_fid_page", None) is not None:
            return
        fpage = None
        try:
            fpage = ctx.new_page()
            fpage.route("**/*", ScoringMixin._fidelity_guard)
            fpage.goto("file://" + str(mh), wait_until="commit", timeout=15000)
            self._fid_page, self._fid_t0 = fpage, time.monotonic()
        except Exception as exc:
            with suppress(Exception):
                if fpage is not None:
                    fpage.close()
            self._fid_page = None
            self.log("WARN", f"保真轨预加载失败(评分时再加载): {str(exc)[:120]}")


    @staticmethod
    def _settle_static(page, t0: float, min_age_ms: int = 2000, cap_ms: int = 2000):
        """禁脚本页(MHTML)截图前静置: 页内定时器不跑, DOM 静置探针等不到回调只会等满上限;
        这里改为页龄 ≥ min_age(让 CSS 入场动画走完, 与旧的 2s 静置同口径)且图片/字体就绪即走。"""
        end = time.monotonic() + cap_ms / 1000
        while True:
            age = (time.monotonic() - t0) * 1000
            ready = False
            with suppress(Exception):
                ready = bool(page.evaluate(
                    "() => document.readyState === 'complete' && (!document.fonts || document.fonts.status === 'loaded')"
                    " && Array.from(document.images).every(i => i.complete)"))
            if (ready and age >= min_age_ms) or time.monotonic() >= end:
                return
            page.wait_for_timeout(100)


    def _round_files(self):
        return {f for f in self.ws.rglob("*")
                if f.is_file() and "flask_app" not in f.relative_to(self.ws).parts}


    def _snapshot_round(self, round_i, detail, results):
        """修复轮快照: 工作区文件清单 + 全部 HTML 字节 + 截图/文本 + 校准样式 + 资源表。"""
        files = self._round_files()
        keep = {f: f.read_bytes() for f in files
                if f.suffix == ".html" or f.name in ("cloned_preview.png", "cloned_text.txt")}
        return {"round": round_i, "detail": dict(detail), "files": files, "bytes": keep,
                "calib": self._calib_css, "results": dict(results)}


    _KEEP_ON_RESTORE_RE = re.compile(
        r"\.(?:mp4|m4v|webm|mov|m4s|m4a|mp3|aac|ogg|ogv|png|jpe?g|gif|webp|avif|svg|"
        r"ktx2?|basis|hdr|exr|glb|gltf|drc|usdz|lsd|woff2?|ttf|otf)$", re.I)


    def _restore_round(self, snap, results):
        kept = 0
        for f in self._round_files() - snap["files"]:
            rel = f.relative_to(self.ws).parts
            # 后几轮按原路径回补的媒体/贴图/字体留下: 只有页面脚本按这条路径真去请求才会
            # 用到, 回退不该把它们删掉 —— 整页静态分在滚动叙事页上随视频帧/进场状态抖动
            # ±5 分, 按分数回退曾把 AirPods 页整组视频删光(克隆里视频全部 404)
            if rel and rel[0] not in ("static", "frames") and self._KEEP_ON_RESTORE_RE.search(f.name):
                kept += 1
                continue
            with suppress(OSError):
                f.unlink()
        if kept:
            self.log("RENDER", f"修复轮回退保留 {kept} 个按原路径回补的媒体/贴图/字体")
        for f, data in snap["bytes"].items():
            with suppress(OSError):
                f.write_bytes(data)
        self._calib_css = snap["calib"]
        results.clear()
        results.update(snap["results"])


    CLONE_SCROLL_JS = """async () => {
        await new Promise(res => {
            let y = 0;
            const step = () => {
                y += 800;
                window.scrollTo(0, y);
                if (y < document.body.scrollHeight) setTimeout(step, 60);
                else { window.scrollTo(0, 0); setTimeout(res, 250); }
            };
            step();
        });
    }"""


    def _load_clone(self, page, base, phases: bool = False):
        """克隆页统一加载序列: dcl 锚定 → 短 load → 网络静置封顶 → 滚动 → 缓释。

        首评与像素/布局校准重渲染共用, 保证重评分看到同样被懒加载/IntersectionObserver
        揭示的内容(此前校准重渲染不滚动, 懒挂载区块在重截图里缺失却仍可能"提分")。
        """
        tp = time.monotonic()
        page.goto(base + self._page_dir + self._page_frag,
                  wait_until="domcontentloaded", timeout=30000)
        with suppress(Exception):
            page.wait_for_load_state("load", timeout=self._strategy_pack.wait_load_timeout)
        # 主线程看门狗: page.evaluate 没有超时, 克隆页脚本一旦死循环(截断的库让播放器
        # Promise 自旋), 后面的滚动/截图 evaluate 会让整个任务永久挂起。wait_for_function
        # 的超时由驱动侧计时, 主线程卡死也能返回
        try:
            page.wait_for_function("true", timeout=config.CLONE_RESPONSIVE_MS)
        except Exception as exc:
            if "Timeout" in type(exc).__name__ or "Timeout" in str(exc):
                raise CloneUnresponsive(
                    f"克隆页主线程 {config.CLONE_RESPONSIVE_MS}ms 无响应(页面脚本死循环)") from exc
        if phases:
            # 网络静置只在首评等: 校准重渲染时资源已在浏览器缓存/本地, 轮询类页面
            # 每次都会白等满上限(滚动 + 缓释仍保留, 懒加载照样揭示)
            with suppress(Exception):
                page.wait_for_load_state(
                    "networkidle", timeout=self._strategy_pack.wait_networkidle_timeout)
            tp = self._phase("评分-克隆加载", tp)
        if self._strategy_pack.scroll_pass:
            # 懒加载触发: 分段滚动到底再回顶, 派发 IntersectionObserver
            # 不 await 页内滚动: 滚到某段触发页面脚本死循环时, 等它返回的 evaluate 会永久挂起;
            # 改为页内起跑 + 驱动侧限时轮询完成标记, 超时即判卡死(交互轨本轮作废)
            try:
                page.evaluate("(src) => { window.__cloneScrollDone = false;"
                              " (0, eval)('(' + src + ')')().then(() => { window.__cloneScrollDone = true; },"
                              " () => { window.__cloneScrollDone = true; }); }", self.CLONE_SCROLL_JS)
                page.wait_for_function("window.__cloneScrollDone === true",
                                       timeout=config.CLONE_SCROLL_MAX_MS, polling=200)
            except Exception as exc:
                if "Timeout" in type(exc).__name__ or "Timeout" in str(exc):
                    raise CloneUnresponsive(
                        f"克隆页滚动 {config.CLONE_SCROLL_MAX_MS}ms 未完成(页面脚本死循环)") from exc
        if phases:
            tp = self._phase("评分-克隆滚动", tp)
        self._settle(page)
        with suppress(Exception):
            if page.evaluate(CLONE_IMG_EAGER_JS) or not page.evaluate(CLONE_IMG_DONE_JS):
                page.wait_for_function(CLONE_IMG_DONE_JS, timeout=config.CLONE_IMG_WAIT_MS,
                                       polling=100)
        with suppress(Exception):
            if page.evaluate(VIDEO_SEEK_JS):
                page.wait_for_function(VIDEO_SEEK_DONE_JS, timeout=config.CLONE_VIDEO_WAIT_MS,
                                       polling=100)
        # 首屏 autoplay 视频解出第一帧前只显示 poster(常为低清占位), 原站基准里是真帧
        with suppress(Exception):
            page.wait_for_function(VIDEO_FRAME_READY_JS, timeout=config.CLONE_VIDEO_WAIT_MS,
                                   polling=100)
        with suppress(Exception):
            page.evaluate(ANIM_ALIGN_JS)
        # 克隆里 autofocus 的输入框会撑开 :focus-within 浮层(原站基准时焦点不在其上)
        with suppress(Exception):
            page.evaluate("() => { const a = document.activeElement;"
                          " if (a && a !== document.body && a.blur) a.blur(); }")
        if phases:
            self._phase("评分-克隆静置", tp)


    def _layout_calibrate(self, page, base, original, cloned, detail, clone_text,
                          orig_text, diff_path):
        """版面块图对齐 → 尺寸覆盖并入校准样式 → 重渲染重评分; 未净提升则整体回滚。"""
        layout_file = self.ws / "layout_map.json"
        idx_html = self._main_page_file()
        if not (layout_file.is_file() and idx_html.is_file()):
            return detail, clone_text
        orig_map = json.loads(layout_file.read_text(encoding="utf-8"))
        clone_map = page.evaluate(f"() => ({LAYOUT_MAP_JS})({LAYOUT_MAP_MAX})")
        css, n_bad = layout_fix_css(orig_map, clone_map)
        seen = {e[0] for e in clone_map or []}
        missing = [e[0] for e in orig_map if e[0] not in seen and e[2] >= 24]
        if missing:
            with suppress(Exception):
                shown = page.evaluate(COLLAPSED_PROBE_JS, missing) or []
                if shown:
                    css = "\n".join([r for r in [css] if r] + [
                        f"{k}{{display:{d}!important}}" for k, d in shown
                        if re.fullmatch(r"[a-z-]+", d)])
                    n_bad += len(shown)
                    self.log("RENDER", f"布局校准: {len(shown)} 个基准可见块在克隆里被脚本收起, 压回显示")
        if not css:
            return detail, clone_text
        prev_css, prev_detail, prev_text = self._calib_css, detail, clone_text
        prev_doc = idx_html.read_text(encoding="utf-8", errors="replace")
        prev_shot = cloned.read_bytes()
        merged = list(dict.fromkeys((self._calib_css + "\n" + css).split("\n")))
        self._calib_css = "\n".join(r for r in merged if r)
        style = self._calib_style(self._calib_css)
        if "data-clone-calib" in prev_doc:
            doc = re.sub(r"<style data-clone-calib>.*?</style>", lambda _m: style,
                         prev_doc, count=1, flags=re.S)
        else:
            doc = self._put_calib(prev_doc, self._calib_css)
        idx_html.write_text(doc, encoding="utf-8")
        self._load_clone(page, base)
        self._full_shot(page, cloned, "克隆页")
        clone_text = page.evaluate(TEXT_EXTRACT_JS)
        detail = compute_visual_similarity(
            original, cloned, diff_path=diff_path, orig_text=orig_text,
            clone_text=clone_text or "", prime_calibration=False)
        before, after = prev_detail.get("score") or 0, detail.get("score") or 0
        if after <= before:
            self.log("RENDER", f"布局校准未提升({before}→{after}, {n_bad} 块失配), 已回滚")
            self._calib_css = prev_css
            idx_html.write_text(prev_doc, encoding="utf-8")
            cloned.write_bytes(prev_shot)
            return prev_detail, prev_text
        (self.ws / "cloned_text.txt").write_text(clone_text or "", encoding="utf-8")
        self.log("RENDER", f"布局校准: {n_bad} 块高度失配 → {len(css.splitlines())} 条尺寸覆盖, "
                           f"评分 {before}→{after}")
        return detail, clone_text


    def _calib_inplace_score(self, page, css: str, original, orig_text: str):
        """把候选覆盖样式注入活页(不重载)截图评分后撤掉; 失败返回 None(按旧路径重渲染)。"""
        tmp = self.ws / "_calib_probe.png"
        try:
            page.evaluate("""(css) => { const s = document.createElement('style'); s.id = '__cl_calib_probe';
                s.textContent = css; document.head.appendChild(s); }""", band_wrap(css, self._band_floor))
            with suppress(Exception):
                page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
            self._full_shot(page, tmp, "校准预评")
            text = page.evaluate(TEXT_EXTRACT_JS) or ""
            d = compute_visual_similarity(original, tmp, orig_text=orig_text, clone_text=text,
                                          prime_calibration=False)
            return (d or {}).get("score")
        except Exception:
            return None
        finally:
            with suppress(Exception):
                page.evaluate("() => { const s = document.getElementById('__cl_calib_probe'); if (s) s.remove(); }")
            with suppress(OSError):
                tmp.unlink()


    def _calib_style(self, css: str) -> str:
        """校准覆盖是按采集视口测出来的尺寸/底色: 限定在采集视口断点区间内。"""
        return f'<style data-clone-calib>{band_wrap(css, self._band_floor)}</style>'


    def _put_calib(self, html: str, css: str) -> str:
        return self._inject_calib(html, css, self._band_floor)


    def _band_floor_of(self, html: str, width: int = 0) -> int:
        """页面(内联样式 + 本地化后的外链样式表)里不超过 width(默认采集宽度)的最大宽度断点。"""
        width = width or config.VIEWPORTS.get(self.cfg.get("viewport", "desktop"),
                                              config.VIEWPORTS["desktop"])["width"]
        texts = [html]
        root = self.ws.resolve()
        for href in re.findall(r"<link\b[^>]*?\bhref\s*=\s*[\"']([^\"'#?]+\.css)[\"'#?]", html, re.I)[:80]:
            if href.startswith(("http:", "https:", "//", "data:")):
                continue
            for cand in (self.ws / href.lstrip("/"),
                         self.ws / self._page_dir.lstrip("/") / href):
                with suppress(OSError, ValueError):
                    f = cand.resolve()
                    if str(f).startswith(str(root)) and f.is_file():
                        texts.append(f.read_text(encoding="utf-8", errors="replace")[:3_000_000])
                        break
        return capture_band_floor(texts, width)


    @staticmethod
    def _inject_calib(html: str, css: str, floor: int = 0) -> str:
        """校准覆盖置于 </head> 前(晚于站点样式, 层叠优先); floor>0 时限定在采集视口区间。"""
        style = f'<style data-clone-calib>{band_wrap(css, floor)}</style>'
        if re.search(r"</head>", html, re.I):
            return re.sub(r"</head>", lambda _m: style + "</head>", html, count=1, flags=re.I)
        return style + html


    # ------------------------------------------------------------------
    def _write_final_diff(self):
        """收敛后生成一次差异热力图(两图缩放结果已在缓存, 不再解码)。"""
        original, cloned = self.ws / "original_preview.png", self.ws / "cloned_preview.png"
        if original.is_file() and cloned.is_file():
            try:
                write_diff_preview(original, cloned, self.ws / "diff_preview.png")
            except Exception as exc:
                self.log("WARN", f"差异热力图生成失败: {exc}")


    def _region_cells(self):
        """AI 修复的区域线索: 像素差分定位的纯色区域色差格(失败返回 None)。"""
        original, cloned = self.ws / "original_preview.png", self.ws / "cloned_preview.png"
        try:
            return pixel_calibrator.divergent_cells(original, cloned)
        except Exception:
            return None


    def _apply_state_replay(self, ctx, page):
        """W4 状态注入(模式策略包): 采集期终态 Cookie(含 httpOnly)/Storage 以沙盒原点(127.0.0.1)回放,
        让运行时的登录态/配置态判断与源站一致(HTML 注入版 inject_state_restore 无法恢复 httpOnly)。"""
        if not self._strategy_pack.state_replay:
            return
        snapshot = self.ws / "state_snapshot.json"
        if snapshot.is_file():
            try:
                payload = json.loads(snapshot.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
            sandbox_cookies = []
            for c in payload.get("cookies", []):
                if not c.get("name"):
                    continue
                sandbox_cookies.append({
                    "name": c["name"], "value": c.get("value") or "",
                    "domain": "127.0.0.1", "path": c.get("path") or "/",
                    "httpOnly": bool(c.get("httpOnly")),
                    "secure": False, "sameSite": "Lax",
                })
            if sandbox_cookies:
                with suppress(Exception):
                    ctx.add_cookies(sandbox_cookies)
            seeds = {"local": payload.get("local", {}),
                     "session": payload.get("session", {})}
            blob = json.dumps(seeds, ensure_ascii=False)[:1_000_000]
            with suppress(Exception):
                page.add_init_script(
                    "try{var d=" + blob +
                    ";['local','session'].forEach(function(t){"
                    "var s=t==='local'?window.localStorage:window.sessionStorage;"
                    "var m=d[t]||{};for(var k in m){try{"
                    "if(s.getItem(k)===null)s.setItem(k,m[k]);"
                    "}catch(e){}}});}catch(e){}")


    def _pixel_calibrate(self, page, base, original, cloned, detail, clone_text, orig_text,
                         diff_path, failed_local, remote_missing, tp):
        """A1 像素校准一步(活页上做, 可能重渲染重评分); 返回 (detail, clone_text, tp)。"""
        # A1 完整版: 像素差分 → 元素映射 → CSS 覆盖合成(确定性校准闭环)。
        # 网格差分定位色差格(原站侧纯色格才取), elementFromPoint 映射到
        # 克隆 DOM 元素, 以原站格色生成覆盖 —— 注入后重渲染重评分一轮。
        if detail is not None and (detail.get('score') or 0) < 99 \
                and self._calibrations < 2 and failed_local \
                and (not config.REPAIR_STRUCTURAL_ONLY
                     or self._structural_missing(failed_local, remote_missing)) \
                and not getattr(self, "_calib_deferred", False):
            # 本轮还有本地资源 404/兜底壳: 差异格多半是待回补的图, 按缺图时的
            # 截图上色会在资源到齐后变成色块 —— 让给下一轮(只让一次: 源站本身
            # 就 404 的资源永远回补不上, 不能因此永不校准)
            self._calib_deferred = True
            self.log("RENDER", f"像素校准暂缓: {len(failed_local)} 个本地资源待回补")
        elif detail is not None and (detail.get('score') or 0) < 99 \
                and self._calibrations < 2:
            try:
                cal_cells = pixel_calibrator.divergent_cells(
                    original, cloned)
                if cal_cells:
                    big_gaps = [c0 for c0 in cal_cells if c0[3] > 200]
                    if len(big_gaps) >= 4:
                        self.log("RENDER", f"像素校准诊断: {len(big_gaps)} 格差距>200"
                                 f" = 大面积区域缺失(回放数据缺口, 归 W2/W3), "
                                 f"校准仅能补色不能补区域")
                        if len(big_gaps) * 2 >= len(cal_cells):
                            # 差异以区域缺失为主: 补色覆盖无从提分(语料里此情形
                            # 全部持平或回滚), 省掉一整次克隆重渲染
                            self.log("RENDER", "像素校准跳过: 差异以区域缺失为主")
                            self._calibrations = 2
                            cal_cells = []
                    cal_css = pixel_calibrator.element_css(page, cal_cells) \
                        if cal_cells else ""
                    if cal_css and config.CALIB_INPLACE_GATE:
                        # 就地预评: 覆盖样式直接注入当前活页重截图评分, 不升就不付整页重渲染
                        # (真实任务日志 58 次像素校准 44 次回滚, 每次白付一次 加载+滚动+静置)
                        pre = self._calib_inplace_score(page, cal_css, original, orig_text)
                        if pre is not None and pre <= (detail.get('score') or 0):
                            self.log("RENDER", f"像素校准就地预评未提升"
                                     f"({detail.get('score')}→{pre}), 跳过重渲染")
                            self._calibrations = 2
                            cal_css = ""
                    if cal_css:
                        # 累积(第二轮在已校准页上测得的是新增差异格)并持久化,
                        # 后续修复轮 _localize 重写页面时重新注入, 不被覆盖
                        prev_css, prev_detail = self._calib_css, detail
                        prev_shot = cloned.read_bytes()
                        prev_text = clone_text
                        merged = list(dict.fromkeys(
                            (self._calib_css + "\n" + cal_css).split("\n")))
                        self._calib_css = "\n".join(r for r in merged if r)
                        cal_css = self._calib_css
                        # 写入真实被渲染的页面: 目录型 URL 时 index.html 是跳转桩
                        idx_html = self._main_page_file()
                        if idx_html.is_file():
                            doc = idx_html.read_text(encoding='utf-8',
                                                     errors='replace')
                            prev_doc = doc
                            cal_style = self._calib_style(cal_css)
                            if 'data-clone-calib' in doc:
                                doc = re.sub(
                                    r'<style data-clone-calib>.*?</style>',
                                    lambda _m: cal_style, doc, count=1, flags=re.S)
                            else:
                                doc = self._put_calib(doc, cal_css)
                            idx_html.write_text(doc, encoding='utf-8')
                            self._calibrations += 1
                            self.log("RENDER", f"像素校准: {len(cal_cells)} "
                                     f"格差 → {len(cal_css.splitlines())} 条覆盖, 重评分")
                            self._load_clone(page, base)
                            self._full_shot(page, cloned, "克隆页")
                            clone_text = page.evaluate(TEXT_EXTRACT_JS)
                            (self.ws / 'cloned_text.txt').write_text(
                                clone_text or '', encoding='utf-8')
                            detail = compute_visual_similarity(
                                original, cloned, diff_path=diff_path,
                                orig_text=orig_text, clone_text=clone_text or '',
                                prime_calibration=self._calibrations < 2)
                            # 回滚守卫: 校准必须净提升, 否则撤销覆盖并恢复上一版
                            # 截图/文本/明细(色块覆盖误伤内容时不允许拉低分数)
                            # 持平同样回滚并停手: 无增益的覆盖只是风险, 后续修复轮再试也只白付一次重渲染
                            if (detail.get('score') or 0) <= (prev_detail.get('score') or 0):
                                self.log("RENDER", f"像素校准未提升"
                                         f"({prev_detail.get('score')}→{detail.get('score')}), 已回滚")
                                self._calib_css = prev_css
                                idx_html.write_text(prev_doc, encoding='utf-8')
                                cloned.write_bytes(prev_shot)
                                clone_text = prev_text
                                (self.ws / 'cloned_text.txt').write_text(
                                    clone_text or '', encoding='utf-8')
                                detail = prev_detail
                                self._calibrations = 2   # 不再尝试
            except Exception as exc:
                self.log('WARN', f'像素校准失败: {exc}')
            tp = self._phase("评分-像素校准", tp)
        return detail, clone_text, tp


    def _make_render_and_score(self, ctx, defer_diff: bool = False):
        """构造"本地离线渲染 + 三通道评分"闭包(AI 修复循环复用)。

        defer_diff: 不写交互轨热力图(由 _write_final_diff 在收敛后统一生成);
        手动 AI 修复等入口保持每次评分即写热力图。"""
        diff_path = None if defer_diff else self.ws / "diff_preview.png"

        def render_and_score():
            server = _LocalPreviewServer(
                self.ws, use_tape=self._strategy_pack.net_tape)
            server.start()
            base = f"http://127.0.0.1:{server.port}"
            failed_local, remote_missing = set(), set()
            page = ctx.new_page()
            page.set_default_timeout(30000)
            with suppress(Exception):
                page.add_init_script(SCORE_TIMER_GUARD_JS)
            self._apply_state_replay(ctx, page)

            def _route_guard(route):
                try:
                    if route.request.url.startswith(base):
                        route.continue_()
                    else:
                        remote_missing.add(route.request.url)
                        route.abort()
                except Exception:
                    with suppress(Exception):
                        route.abort()

            def _on_resp(resp):
                try:
                    rt = resp.request.resource_type
                    if rt not in ASSET_RESOURCE_TYPES and not (
                            rt in ("fetch", "xhr") and _is_runtime_asset(resp.url)):
                        return
                    if resp.status >= 400:
                        failed_local.add(urlsplit(resp.url).path or "/")
                        return
                    # SPA 兜底毒化感知: 静态资产拿到 200 的 text/html 是
                    # 兜底壳而非真实字节, 视同失败喂给回补(不修会以
                    # HTML 当 JS/CSS 解析崩溃且永不再重试)
                    if resp.status == 200 and resp.url.startswith(base) \
                            and resp.request.resource_type in (
                                "script", "stylesheet", "image", "font", "media"):
                        ctype = (resp.headers or {}).get("content-type", "")
                        if "text/html" in ctype:
                            failed_local.add(urlsplit(resp.url).path or "/")
                except Exception:
                    return

            def _on_fail(req):
                try:
                    if req.resource_type not in ASSET_RESOURCE_TYPES and not (
                            req.resource_type in ("fetch", "xhr") and _is_runtime_asset(req.url)):
                        return
                    url = req.url
                    if url.startswith(base):
                        # 媒体元素换源/暂停时主动取消在途请求(ERR_ABORTED), 文件其实在:
                        # 记成缺失只会让修复循环误判"缺失不降"而提前收敛
                        if req.resource_type == "media" and "ABORTED" in str(req.failure or ""):
                            return
                        failed_local.add(urlsplit(url).path or "/")
                except Exception:
                    return

            def _render_fidelity():
                """N1 保真轨: MHTML 确定性渲染(Chromium 对 MHTML 默认禁脚本), 与交互轨
                分开评分 —— 回放保真由构造保证。MHTML 不随修复循环变化 → 每任务一次。"""
                if self._fidelity_rendered or not (self.ws / 'capture.mhtml').is_file():
                    return
                self._fidelity_rendered = True
                with suppress(OSError):   # 防上一轮/重建残留截图被误评
                    (self.ws / 'fidelity_preview.png').unlink()
                t_fid = time.monotonic()
                fpage, t0 = getattr(self, "_fid_page", None), getattr(self, "_fid_t0", t_fid)
                self._fid_page = None
                try:
                    if fpage is not None:
                        # 下载期间已在后台加载(见 _prestart_fidelity); 后台标签页不出帧、图片不解码,
                        # 截图前切到前台再等几帧(否则偶发截到未绘制完的版面: 夹具上 93→52.9)
                        fpage.wait_for_load_state("load", timeout=30000)
                        with suppress(Exception):
                            fpage.bring_to_front()
                        # 禁脚本页里页内定时器/rAF 不跑, 不能 await 页内 Promise(会永久挂起); 驱动侧等两三帧
                        fpage.wait_for_timeout(150)
                    else:
                        fpage = ctx.new_page()
                        # 离线守卫: 归档内资源由 Chromium 直接供给(不经网络层),
                        # 归档外的引用一律中止 —— 保真轨必须确定且不回源
                        fpage.route("**/*", ScoringMixin._fidelity_guard)
                        t0 = time.monotonic()
                        fpage.goto('file://' + str(self.ws / 'capture.mhtml'),
                                   wait_until='load', timeout=30000)
                    with suppress(Exception):
                        self._settle_static(fpage, t0)
                    self._full_shot(fpage, self.ws / 'fidelity_preview.png', "保真轨")
                    f_text = fpage.evaluate(TEXT_EXTRACT_JS)
                    (self.ws / 'fidelity_text.txt').write_text(
                        f_text or '', encoding='utf-8')
                except Exception as exc:
                    self.log('WARN', f'保真轨渲染失败: {exc}')
                finally:
                    with suppress(Exception):
                        fpage.close()
                    self._phase("评分-保真轨渲染", t_fid)

            def _score_fidelity(original, orig_text, inter_score):
                """N1 保真轨评分: MHTML 确定性渲染 vs 原站(独立于交互轨, 每任务一次)。"""
                fid_png = self.ws / 'fidelity_preview.png'
                fid_txt = self.ws / 'fidelity_text.txt'
                if not fid_png.is_file() or self._fidelity_score is not None:
                    return
                t_sc = time.monotonic()
                try:
                    f_detail = compute_visual_similarity(
                        original, fid_png,
                        diff_path=self.ws / 'fidelity_diff.png',
                        orig_text=orig_text,
                        clone_text=fid_txt.read_text(errors='replace') if fid_txt.is_file() else '')
                    self._fidelity_score = f_detail.get('score')
                    self.log("RENDER", f"保真轨评分: {self._fidelity_score} (交互轨: {inter_score})")
                except Exception as exc:
                    self.log("WARN", f"保真轨评分失败: {exc}")
                self._phase("评分-计算", t_sc)

            try:
                page.route("**/*", _route_guard)
                page.on("response", _on_resp)
                page.on("requestfailed", _on_fail)
                # 混合等待: dcl 锚定(重型站 load 可能超时) → 短 load 等待
                # (重水合 SPA 需要资源齐) → 网络静置封顶 → 滚动 → 缓释
                # (W1 起参数由模式策略包供给, 值=现行全局行为)
                self._load_clone(page, base, phases=True)
                if not getattr(self, "_clone_probed", False):
                    # 与原站同一探针: 两侧样本对照即可归因装饰层在克隆侧丢失
                    self._clone_probed = True
                    with suppress(Exception):
                        cp = page.evaluate(PAGE_PROBE_JS)
                        self.log("RENDER", "克隆版面特征: " + ", ".join(
                            f"{k}={v}" for k, v in cp.items()))
                tp = time.monotonic()
                self._full_shot(page, self.ws / "cloned_preview.png", "克隆页")
                clone_text = page.evaluate(TEXT_EXTRACT_JS)
                (self.ws / "cloned_text.txt").write_text(clone_text or "",
                                                         encoding="utf-8")
                tp = self._phase("评分-克隆截图", tp)
            except CloneUnresponsive as exc:
                # 卡死的页面上截图也会挂起: 交互轨本轮作废, 由保真轨(MHTML 禁脚本)出分
                self.log("WARN", f"克隆页渲染失败: {exc}")
                with suppress(OSError):
                    (self.ws / "cloned_preview.png").unlink()
                clone_text = ""
            except Exception as exc:
                self.log("WARN", f"克隆页渲染失败: {exc}")
                with suppress(Exception):
                    self._full_shot(page, self.ws / "cloned_preview.png", "克隆页")
                clone_text = ""
            remote_missing.discard("")
            # 页面/沙盒在评分与 A1 校准全程保持存活(校准需在活页上 elementFromPoint
            # 并重导航重截图; 旧实现在此前已 close/stop, 校准恒抛异常从未生效)
            try:
                original = self.ws / "original_preview.png"
                cloned = self.ws / "cloned_preview.png"
                if not original.is_file():
                    return None, failed_local, remote_missing
                if not cloned.is_file():
                    # 交互轨截图失败: 保真轨仍可独立出分(头条分数双轨取优)
                    _render_fidelity()
                    with suppress(Exception):
                        _score_fidelity(original, (self.ws / "original_text.txt").read_text(
                            encoding="utf-8", errors="replace")
                            if (self.ws / "original_text.txt").is_file() else "", None)
                    return None, failed_local, remote_missing
                try:
                    orig_text = (self.ws / "original_text.txt").read_text(
                        encoding="utf-8", errors="replace") if (self.ws / "original_text.txt").is_file() else ""
                    tp = time.monotonic()
                    detail = compute_visual_similarity(
                        original, cloned, diff_path=diff_path,
                        orig_text=orig_text, clone_text=clone_text or "",
                        prime_calibration=self._calibrations < 2)
                    tp = self._phase("评分-计算", tp)
                    detail, clone_text, tp = self._pixel_calibrate(
                        page, base, original, cloned, detail, clone_text, orig_text,
                        diff_path, failed_local, remote_missing, tp)
                    # 布局校准: 版面块高度对齐原站, 消除累积错位(每任务一次, 不提升即回滚)
                    if detail is not None and (detail.get('score') or 0) < 99 \
                            and not self._layout_calibrated:
                        self._layout_calibrated = True
                        try:
                            detail, clone_text = self._layout_calibrate(
                                page, base, original, cloned, detail, clone_text,
                                orig_text, diff_path)
                        except Exception as exc:
                            self.log('WARN', f'布局校准失败: {exc}')
                        tp = self._phase("评分-布局校准", tp)
                    # N1 保真轨: 交互轨已近满分时保真轨无法改变头条分数(双轨取优), 跳过其渲染
                    if detail is None or (detail.get('score') or 0) < config.FIDELITY_SKIP_ABOVE:
                        _render_fidelity()
                        _score_fidelity(original, orig_text,
                                        detail.get('score') if detail is not None else None)
                    return detail, failed_local, remote_missing
                except Exception as exc:
                    self.log("WARN", f"评分计算失败: {exc}")
                    return None, failed_local, remote_missing
            finally:
                with suppress(Exception):
                    page.close()
                # W4 防跨站污染: cookie 按 domain 作用、无视端口 —— 不清理
                # 会泄漏给同 worker 后续任务的沙盒渲染(W4 首轮验收教训)
                with suppress(Exception):
                    ctx.clear_cookies()
                server.stop()

        return render_and_score
