"""ClonerEngine 本地化: 引用替换表、页面/脚本改写、交互模拟注入(自 cloner_engine.py 按职责拆出的 mixin, 方法体未改)。"""
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
    neutralize_deploy_hazards,
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


class LocalizeMixin:

    # ------------------------------------------------------------------
    def _compose_pairs(self, results: dict, routes: set, origin: str,
                       srcset_map: dict, iframe_pages: list, frame_records: list):
        """构造 (原始引用 -> 本地路径) 替换表, 长字符串优先。"""
        pairs = {}
        page_netloc = urlsplit(self._target).netloc
        self._fill_variants_bulk(pairs, [(url, info["local"]) for url, info in results.items()],
                                 page_netloc)
        srcset_items = []
        for candidate, best in srcset_map.items():
            # 候选自身已落地(浏览器按 sizes 实际选中的那档, 活体复用/回补拿到)就用它自己:
            # 一律指向最大档时浏览器把 2x 大图缩下来, 锐度/重采样与原站那档不同
            if candidate in results:
                continue
            info = results.get(best)
            if info is not None:
                srcset_items.append((candidate, info["local"]))
        self._fill_variants_bulk(pairs, srcset_items, page_netloc)
        # 原始相对/带参引用字符串 -> 本地(覆盖 <style> url() 与相对路径引用)
        for raw, absolute in self._raw_refs.items():
            info = results.get(absolute)
            if info is not None and len(raw) > 5 and raw != absolute:
                pairs[raw] = info["local"]
        netloc = urlsplit(origin).netloc
        for route in routes:
            pairs[origin + route] = route
            pairs[f"//{netloc}{route}"] = route
            if route != "/":
                pairs[origin + route + "/"] = route + "/"
        # 同源 iframe 引用重写为本地静态子页
        for i, (src, _html) in enumerate(iframe_pages):
            local = f"/iframe_{i + 1}.html"
            for variant in self._variants(src, page_netloc):
                pairs[variant] = local
        # 跨域 iframe 引用重写为本地静态子页
        for i, (src, _html, _text) in enumerate(frame_records):
            local = f"/frames/frame_{i + 1}.html"
            for variant in self._variants(src, page_netloc):
                pairs[variant] = local
            # 同时登记原始引用串(iframe src 常带相对写法)
            for raw, absolute in self._raw_refs.items():
                if absolute == src and len(raw) > 5:
                    pairs[raw] = local
        # 按长度降序的稳定排序(reverse=True 仍保持稳定), 与旧 key=-len 逐项一致;
        # 内置 len 作 key 免去逐项 lambda 调用
        return [(k, pairs[k]) for k in sorted(pairs, key=len, reverse=True)]


    def _build_interaction_sim(self) -> str:
        """仿真交互层脚本: 登录点击提示(自定义/默认池) / 可选登录后跳转 / 验证码 60s 倒计时。"""
        if not self.cfg.get("interaction_sim", config.INTERACTION_SIM):
            return ""
        custom_msg = str(self.cfg.get("sim_login_message") or "").strip()
        redirect = str(self.cfg.get("sim_login_redirect") or "").strip()
        # 安全校验: 仅放行 http(s) 绝对地址与站内相对路径, 拒绝 javascript: 等注入向量
        if redirect and not re.match(r"^(https?://|/)", redirect, re.I):
            self.log("WARN", f"登录跳转地址被拒绝(仅支持 http(s):// 或 / 开头): {redirect[:80]}")
            redirect = ""
        fails = [custom_msg] if custom_msg else list(config.SIM_LOGIN_FAIL_MESSAGES)
        script = (INTERACTION_SIM_TEMPLATE
                  .replace("__SIM_FAILS__", json.dumps(fails, ensure_ascii=False))
                  .replace("__SIM_SMS_OK__",
                           json.dumps(config.SIM_SMS_SENT_MESSAGE,
                                      ensure_ascii=False))
                  .replace("__SIM_SECONDS__",
                           str(int(config.SIM_SMS_COUNTDOWN_SECONDS)))
                  .replace("__SIM_REDIRECT__",
                           json.dumps(redirect, ensure_ascii=False)))
        # W5 时间冻结(模式策略包): 回放统一到采集时刻, 消除时变内容的
        # 渲染分叉(倒计时/日期组件/限时横幅在回放时与采集态不一致)
        if getattr(self._strategy_pack, "freeze_time", False) and self._captured_at:
            freeze_js = (
                '<script data-clone-preview="time-freeze">(function(){try{'
                'var __cap=new Date("__CAPTURE_ISO__");'
                'if(!isNaN(__cap.getTime())){var _R=Date;'
                'function FD(){var a=arguments;switch(a.length){case 0:return new _R(__cap.getTime());'
                'case 1:return new _R(a[0]);case 2:return new _R(a[0],a[1]);case 3:return new _R(a[0],a[1],a[2]);'
                'case 4:return new _R(a[0],a[1],a[2],a[3]);case 5:return new _R(a[0],a[1],a[2],a[3],a[4]);'
                'case 6:return new _R(a[0],a[1],a[2],a[3],a[4],a[5]);default:return new _R(a[0],a[1],a[2],a[3],a[4],a[5],a[6]);}}'
                'FD.prototype=_R.prototype;FD.now=function(){return __cap.getTime()};'
                'FD.parse=_R.parse;FD.UTC=_R.UTC;window.Date=FD;'
                '}catch(e){}})();</script>')
            script = freeze_js.replace("__CAPTURE_ISO__", self._captured_at) + script
        return script


    @staticmethod
    def _inject_after_head(html: str, script: str) -> str:
        if not script:
            return html
        match = re.search(r"<head[^>]*>", html, re.I)
        insert_at = match.end() if match else 0
        return html[:insert_at] + script + html[insert_at:]


    def _main_page_file(self) -> Path:
        """主页面实际被渲染/交付的文件(目录型 URL 落在 page_dir 影子路径)。"""
        if self._page_dir != "/":
            return self.ws / self._page_dir.lstrip("/") / "index.html"
        return self.ws / "index.html"


    def _localize(self, pages: list, iframe_pages: list, frame_records: list,
                  pairs: list, pages_map: dict):
        """HTML 本地化重写 + 状态恢复/仿真交互注入 + 导航护栏 + SPA 伪装 + 子页落盘。"""
        sim_script = self._build_interaction_sim()
        if self.db.get_tracker_config(self.task_id) and \
                (self.db.get_tracker_config(self.task_id) or {}).get("enabled"):
            # 启用埋点的任务: 附加观察器胶水(交互层拦截前回调 __ft_observe 触发上报)
            sim_script += TRACKER_OBSERVE_JS

        def inject_state_restore(html: str) -> str:
            # 批次级开关: restore_state=False 时产物不携带任何原站会话材料
            if not self.cfg.get("restore_state", True):
                return html
            snapshot = self.ws / "state_snapshot.json"
            if not snapshot.is_file():
                return html
            try:
                payload = json.loads(snapshot.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return html
            slim = {
                "cookies": [{"name": c.get("name"), "value": c.get("value"),
                             "path": c.get("path") or "/"}
                            for c in payload.get("cookies", [])
                            # httpOnly Cookie 原站页面 JS 本就不可见(document.cookie
                            # 读不到), 烘焙进产物既无语义收益又扩散会话凭证 → 剔除;
                            # 沙盒评分侧的 httpOnly 回放走 ctx.add_cookies, 不受影响
                            if c.get("name") and not c.get("httpOnly")],
                "local": payload.get("local", {}),
                "session": payload.get("session", {}),
            }
            script = STATE_RESTORE_TEMPLATE.replace(
                "__STATE_JSON__", _safe_json(slim))
            return self._inject_after_head(html, script)

        bg_css = self._bg_freeze_css()

        strip_pack = bool(self._strategy_pack.disable_hydration)
        stripped_total = 0

        def finalize(html: str, route: str) -> str:
            nonlocal stripped_total
            # 模式策略包: 水合类站点以采集期静态 DOM 为权威, 落盘前剥离应用脚本
            # (活体阶段只对 Next/Nuxt 早识别剥离; 这里补齐识别后的整类开关)
            if strip_pack:
                html, n = _strip_hydration_html(html)
                stripped_total += n
            # <base href> 会让克隆页所有未改写的相对引用按原站指定的基准解析
            # (指向 CDN/原站即整类漂移); 剥离后恢复"按克隆页自身路径解析"
            # 的统一不变量 —— 与 page_dir 影子落盘机制配套
            localized = _BASE_TAG_RE.sub("", rewrite_js_navigation(html))
            floor = self._band_floor_of(localized)
            if route == "/":
                self._band_floor = floor
            localized = scope_bake_css(localized, floor)
            localized = scope_viewport_css(localized, floor, self._band_floor_of(localized, 768))
            if route == "/" and self._calib_css:
                localized = self._put_calib(localized, self._calib_css)
            localized = self._inject_spa_disguise(localized, route)
            localized = inject_state_restore(localized)
            if bg_css:
                localized = self._inject_after_head(localized, bg_css)
            localized = self._inject_after_head(localized, sim_script)
            localized = self._inject_after_head(localized, REPLAY_DEDUPE_JS)
            # 部署性能守卫: 未本地化的阻塞外链脚本(采集期已 404/跳过的计数/埋点、跨域图标)
            # 不得在本地部署时走反代兜底阻塞首屏 —— 删信标 / 其余转 async。
            localized = neutralize_deploy_hazards(
                localized, urlsplit(self._target).netloc)
            return _utf8_charset(_inject_responsive(localized, bool(self.cfg.get("responsive_enhance"))))

        html_pairs = _html_escaped_pairs(pairs)
        for route, html in pages:
            fname = "index.html" if route == "/" else f"p__{slugify(route)}.html"
            localized = finalize(apply_replacements(html, html_pairs), route)
            (self.ws / fname).write_text(localized, encoding="utf-8")
            pages_map[route] = fname
            if strip_pack and stripped_total and route == "/":
                self.log("RENDER", f"策略包水合剥离({self._strategy_pack.pattern}): "
                                   f"落盘前剥离 {stripped_total} 个应用脚本, 静态 DOM 权威")
            # 目录型目标页: 在原路径落一份副本(index.html), 使页面相对路径
            # (运行时拼接的懒加载 chunk 等)与原站解析一致
            if route == "/" and self._page_dir != "/":
                # 真实页面只落在原路径(相对路径基准); 根入口改为保 hash 的
                # 跳转桩 —— 容器根路径 / 工作台预览 /preview/<id>/ 打开时
                # 自动落到 page_dir, 懒加载 chunk 等相对引用不漂移。
                shadow = self.ws / self._page_dir.lstrip("/") / "index.html"
                shadow.parent.mkdir(parents=True, exist_ok=True)
                shadow.write_text(localized, encoding="utf-8")
                pages_map[self._page_dir] = f"{self._page_dir.lstrip('/')}index.html"
                pages_map[self._page_dir.rstrip("/")] = f"{self._page_dir.lstrip('/')}index.html"
                stub = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
                        "<script>(function(){var h=location.hash||'';"
                        "location.replace(" + _safe_json(self._page_dir.lstrip("/"))
                        + "+h);})();</script>"
                        "<noscript><meta http-equiv=\"refresh\" content=\"0;url="
                        + _html.escape(self._page_dir.lstrip("/"), quote=True)
                        + "\"></noscript>"
                        "</head><body></body></html>")
                (self.ws / "index.html").write_text(stub, encoding="utf-8")
        for i, (_src, html) in enumerate(iframe_pages):
            localized = _BASE_TAG_RE.sub(
                "", rewrite_js_navigation(apply_replacements(html, html_pairs)))
            localized = scope_bake_css(localized, self._band_floor_of(localized))
            localized = self._inject_after_head(localized, sim_script)
            localized = _utf8_charset(_inject_responsive(localized, bool(self.cfg.get("responsive_enhance"))))
            (self.ws / f"iframe_{i + 1}.html").write_text(
                localized, encoding="utf-8")
            pages_map[f"/iframe_{i + 1}.html"] = f"iframe_{i + 1}.html"
        for i, (_src, html, _text) in enumerate(frame_records):
            localized = _BASE_TAG_RE.sub(
                "", rewrite_js_navigation(apply_replacements(html, html_pairs)))
            localized = scope_bake_css(localized, self._band_floor_of(localized))
            localized = self._inject_after_head(localized, sim_script)
            localized = _utf8_charset(_inject_responsive(localized, bool(self.cfg.get("responsive_enhance"))))
            (self.ws / "frames" / f"frame_{i + 1}.html").write_text(
                localized, encoding="utf-8")
            pages_map[f"/frames/frame_{i + 1}.html"] = f"frames/frame_{i + 1}.html"


    def _rewrite_js_files(self, results: dict, pairs: list):
        """JS 内容本地化: URL 字面量替换 + 导航逃逸护栏改写(含原名副本)。"""
        for info in results.values():
            if info["ext"] not in (".js", ".mjs", ".cjs"):
                continue
            targets = [info["abs_path"]]
            rel = (info.get("local") or "").lstrip("/")
            if rel:
                alias = self.ws / rel
                if alias != info["abs_path"]:
                    targets.append(alias)
            for path in targets:
                try:
                    if not path.is_file() or path.stat().st_size > config.JS_REWRITE_MAX_BYTES:
                        continue
                    text = path.read_text(encoding="utf-8", errors="replace")
                    rewritten = rewrite_js_navigation(apply_replacements(text, pairs))
                    if rewritten != text:
                        path.write_text(rewritten, encoding="utf-8")
                except OSError:
                    continue


    @classmethod
    def _fill_variants_bulk(cls, pairs: dict, items: list, page_netloc: str):
        """对 items=[(url, local)] 逐个做 _fill_variants, 结果与逐个调用完全相同。

        整批校验(全部 C 层子串计数/扫描): 每个 URL 都以小写 http:// 或 https://
        开头、纯 ASCII、不含 urlsplit 会删除或另行处理的字符(\t \r \n # [ ])。
        此时 urlsplit 的切分完全确定: netloc 止于主机后首个 / 或 ?, path 止于
        ?, 各变体都是 url 的切片, 直接用 str.find 定位; 任一不满足则逐个走
        _fill_variants(其内部再按严格正则判定或回落 _variants)。"""
        if not items:
            return
        joined = "\n".join([u for u, _ in items])
        n_http = joined.count("\nhttp://") + joined.startswith("http://")
        n_https = joined.count("\nhttps://") + joined.startswith("https://")
        if not (n_http + n_https == len(items) == joined.count("\n") + 1
                and joined.isascii() and not any(ch in joined for ch in "\t\r#[]")):
            for url, local in items:
                cls._fill_variants(pairs, url, local, page_netloc)
            return
        for url, local in items:
            host_at = 8 if url[4] == "s" else 7          # https:// | http://
            slash = url.find("/", host_at)
            query = url.find("?", host_at)
            if slash < 0 or 0 <= query < slash:           # 无路径
                stem = url if query < 0 else url[:query]
                pairs[url] = local
                pairs[stem] = local
                bare = stem[host_at - 2:]
                if len(bare) > 5:
                    pairs[bare] = local
                if query >= 0 and query > host_at and len(url) - host_at + 2 > 5:
                    pairs[url[host_at - 2:]] = local
                continue
            stem = url if query < 0 else url[:query]      # scheme://netloc/path
            pairs[url] = local
            pairs[stem] = local
            bare = stem[host_at - 2:]                     # //netloc/path
            if len(bare) > 5:
                pairs[bare] = local
            same = url[host_at:slash] == page_netloc
            path = stem[slash:]
            if len(path) > 5 and same:
                pairs[path] = local
            if query >= 0 and slash > host_at:
                # 带查询串的协议相对/站内相对写法也要整串登记: 否则只剩去参的 stem 能命中,
                # 同一端点不同参数(/_next/image?url=A / ?url=B)被 stem 前缀改写成同一张图
                if len(url) - host_at + 2 > 5:
                    pairs[url[host_at - 2:]] = local
                if same and len(url) - slash > 5:
                    pairs[url[slash:]] = local


    @classmethod
    def _fill_variants(cls, pairs: dict, url: str, local: str, page_netloc: str):
        """pairs[v] = local 对 _variants(url) 的每个变体 —— _compose_pairs 热循环专用。

        严格形态绝对 URL(_STRICT_ABS_URL_RE)的变体都是 url 的切片: 去查询串 =
        url[:路径末], 去 scheme = 再去掉 "scheme:" 前缀, 免去 urlsplit 与两个临时
        set。替换表最终按长度降序稳定排序, 而同一 URL 的各变体长度两两不同
        (url ≥ 去查询串 > 去 scheme > 纯路径), 插入先后不影响最终顺序; 非严格
        形态回落 _variants, 输出逐项一致。"""
        m = _STRICT_ABS_URL_RE.match(url)
        if m is None:
            for variant in cls._variants(url, page_netloc):
                pairs[variant] = local
            return
        path = m.group(3)
        stem = url[:m.end(3) if path else m.end(2)]      # scheme://netloc/path
        pairs[url] = local
        pairs[stem] = local
        bare = stem[m.end(1) + 1:]                        # //netloc/path
        if len(bare) > 5:
            pairs[bare] = local
        if path and len(path) > 5 and m.group(2) == page_netloc:
            pairs[path] = local
        if len(url) > len(stem):                          # 带查询串: 整串的相对写法
            if len(url) - m.end(1) - 1 > 5:
                pairs[url[m.end(1) + 1:]] = local
            if path and m.group(2) == page_netloc and len(url) - m.start(3) > 5:
                pairs[url[m.start(3):]] = local


    @staticmethod
    def _variants(url: str, page_netloc: str):
        scheme, netloc, path = _split_scheme_netloc_path(url)
        candidates = {url, f"{scheme}://{netloc}{path}", f"//{netloc}{path}"}
        if netloc == page_netloc and path.startswith("/"):
            candidates.add(path)
        if "?" in url and netloc:
            rest = url[url.find("//") :] if "//" in url else ""
            if rest.startswith("//"):
                candidates.add(rest)
                if netloc == page_netloc and path.startswith("/"):
                    i = rest.find("/", 2)
                    if i > 0:
                        candidates.add(rest[i:])
        return {c for c in candidates
                if c and len(c) > 5 and c.startswith(("http", "//", "/"))}
