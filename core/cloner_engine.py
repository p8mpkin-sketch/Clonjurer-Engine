"""
V4 工业级高还原提取引擎(同步线程模型) —— AI 视觉闭环与自适应修复版。

V4 强化(工程修复优先, AI 兜底):
  - URL 路径参数规范化: ;jsessionid=... 等矩阵参数不再污染扩展名识别,
    CSS/JS 正确归位 static/css|js, 修复 stats 全部落入 misc 的问题;
  - 跨域 iframe 递归克隆: iframe[src](含跨域 SSO 组件)以顶级页面方式
    Playwright 渲染(天然绕过 X-Frame-Options), 固化为 frames/frame_N.html
    并重写引用, 深度与数量受控;
  - 活体会话请求差集回补: 渲染期浏览器成功加载过的全部资源与已下载清单
    做差集, 缺失部分用同一 BrowserContext(真实 Cookie/UA)补抓 —— 用观测
    代替猜测, 不依赖大模型猜 URL;
  - 渲染驱动修复循环: 本地渲染 -> 收集 404/外联请求 -> 回源补抓 ->
    重新本地化 -> 再渲染, 最多 RENDER_REPAIR_ROUNDS 轮;
  - JS 内容本地化重写: 运行时由 JS 字符串注入的资源(动态背景图等)
    在下载后的 .js 文件内做精确 URL 字面量替换;
  - 诚实相似度 v2: 块级归一化互相关(NCC) + 边缘图 IoU + 可见文本召回率
    三通道加权, 子指标过低输出告警, 杜绝"白底页面恒高分";
  - 本地评分渲染强制离线(路由拦截外网请求), 分数不被远程资源污染;
  - AI 视觉闭环(门控): 诚实评分低于阈值且配置了 API Key 时, 由
    ai_vision_repair 对比双图生成 CSS/HTML 补丁并迭代复评;
  - 智能 Mock: 表单 action/字段解析生成可交互 Mock 路由(可选 LLM 润色)。

执行管线:
  DOM渲染(懒加载+滚动+表单激活) -> DEEP_FREEZE(CSSOM/Canvas/表单/动画显形)
  -> 多页 BFS + 同源 iframe DOM 提取 + 跨域 iframe 顶级渲染固化
  -> XHR/Fetch 流量录制 -> CAS 内容寻址并发下载(requests->浏览器会话降级)
  -> 活体差集回补 -> HTML/CSS/JS 本地化重写 + SPA 路径伪装注入
  -> 渲染驱动修复循环(404 回补) -> 诚实相似度三通道评分 + 差异热力图
  -> 智能 Mock 合并 -> [门控] AI 视觉闭环修复 -> Flask 工程生成打包
"""
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
    CANVAS_PIN_JS,
    CV_CANVAS_DRIFT_JS,
    HIDDEN_ROOTS_JS,
    IMG_SRC_PIN_JS,
    ROUNDTRIP_PIN_JS,
    SCROLL_LOCK_STRIP_JS,
    STATE_PHASE_RESET_JS,
    TRANSITION_FINISH_JS,
    VISIBLE_PATHS_JS,
    AD_RECTS_JS,
    INTERSTITIAL_HIDE_JS,
    ROOT_FONT_APPLY_JS,
    FLUID_READ_JS,
    FLUID_APPLY_JS,
    ROOT_FONT_READ_JS,
    INTERSTITIAL_RESTORE_JS,
    FONT_BAKE_NOSIZE_JS,
    SCROLL_TOP_JS,
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
    WAIT_SHEETS_JS,
    CSSOM_LINK_DEDUPE_JS,
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
    _drop_slider_tracks,
    _id_misaligned_parents,
    _root_inline_varies,
    _source_id_classes,
    _source_sibling_groups,
    _subtree_sig,
    CONTENT_KEEP_GUARD_JS,
    SCROLL_UNLOCK_GUARD_JS,
    relax_font_display,
    beacon_like,
    neutralize_deploy_hazards,
    deploy_hazard_audit,
    page_codepoints,
    _STYLE_BLOCK_RE,
    fontface_swap_for_remote,
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
    apply_url_replacements,
    band_wrap,
    capture_band_floor,
    compute_visual_similarity,
    detect_blocked_page,
    layout_fix_css,
    os_ext,
    parse_css_refs,
    rewrite_js_navigation,
    rewrite_module_imports,
    scope_bake_css,
    scope_viewport_css,
    slugify,
    text_f_score,
    text_overlap_score,
    write_diff_preview,
)
from core.clone.support import _fluid_elements, _is_global_reset_rule, _is_transient_hide_rule, _regroup_css_rules  # noqa: F401


from core.clone.support import (  # noqa: E402  手机版(按 UA 分发)
    PHONE_EDITION_FILE, inject_edition_router, merge_static_tree, separate_phone_edition,
)


class _EditionDB:
    """手机版子克隆用的库代理: 任务状态/进度/完成计数归主克隆, 子克隆只读与记 CAS。"""

    def __init__(self, db):
        self._db = db

    def update_task(self, *_a, **_k):
        return None

    def increment_completed(self, *_a, **_k):
        return None

    def __getattr__(self, name):
        return getattr(self._db, name)


from core.clone.capture import CaptureMixin  # noqa: E402
from core.clone.download import DownloadMixin  # noqa: E402
from core.clone.localize import LocalizeMixin  # noqa: E402
from core.clone.scoring import ScoringMixin  # noqa: E402


class ClonerEngine(CaptureMixin, DownloadMixin, LocalizeMixin, ScoringMixin):
    """单任务克隆执行器(V4 同步线程模型), 由调度器工作线程驱动。"""

    def __init__(self, task_id, db, bus, pool):
        self.task_id = task_id
        self.db = db
        self.bus = bus
        self.pool = pool
        self.task = None
        self.cfg = {}
        self.ws: Path = config.WORKSPACE_ROOT / str(task_id)
        self._target = ""
        self._mock_raw = []
        self._dl_headers = {}
        self.downloader = None           # SmartDownloader, run() 中构造
        self._live_seen = {}             # 活体渲染期成功加载的资源 URL -> resource_type
        self._live_resp = {}             # 活体 200 响应(页面关闭前取响应体)
        self._live_bodies = {}           # URL -> 下载结果结构(浏览器已加载的字节, 免重下)
        self._live_body_bytes = 0
        self._raw_refs = {}              # 原始引用字符串 -> 绝对 URL(本地化替换用)
        self._page_dir = "/"             # 目标页所在目录(目录型 URL, 相对路径基准)
        self._page_frag = ""             # 目标 URL 的 hash 路由(SPA #/login)
        self._pattern_info = {"pattern": "DEFAULT", "features": [], "confidence": 0.0}
        self._strategy_pack = strategy_packs.for_pattern("DEFAULT")
        self._fidelity_score = None           # N1 保真轨评分(双轨取优用)
        self._static_score = None            # 静态版交付物实测分(保真轨明显领先时才做)
        self._fidelity_rendered = False      # 保真轨 MHTML 已渲染(每任务一次)
        self._layout_calibrated = False      # 布局校准已尝试(每任务一次)
        self._backfill_dead = set()          # 修复回补回源失败的 URL(跨轮负缓存)
        self._dead_urls = set()              # 下载期确认 HTTP 4xx/5xx 的资源 URL(交付侧秒 404 用)
        self._ff_needed, self._ff_skip = set(), set()   # @font-face 下载计划: 必下 / 证明不会被请求
        self._ff_fallbacks = {}              # 首选字体 URL -> 兜底 URL 列表(首选下不来时补下)
        self._prefetch_futs = {}             # 回补预取: URL -> Future(CAS 期间并发直连)
        self._prefetch_pool = None
        self._prefetch_hits = 0
        self._ff_cps = None                  # 页面可能出现的字符码位(已排序); None = 不按 unicode-range 剔除
        self._calibrations = 0               # A1 像素校准执行次数
        self._phase_acc = {}                 # 子阶段累计耗时(秒): 提速定位用
        self._calib_css = ""                 # A1 校准覆盖(跨修复轮持久, _localize 注入)
        self._band_floor = 0                 # 采集视口断点区间下界(视口作用域固化用)
        self._captured_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        self._net_tape = []              # VCR 网络磁带(W2): XHR/fetch 全量文本响应
        self._tape_seen = set()          # (method, path, query) 去重
        self._tape_bytes = 0
        self._stat_accum = {"css": 0, "js": 0, "img": 0, "fonts": 0,
                            "media": 0, "misc": 0, "bytes": 0}
        self._timeline = []              # [(stage, 单调时钟)] 阶段切换记录(端到端度量)
        self._edition_child = False      # 手机版子克隆(见 _maybe_mobile_edition): 不打包、不落任务状态
        self._log_prefix = ""
        self._main_dom_html = ""         # 采集期主页面 DOM(手机版探测比对用)

    # ------------------------------------------------------------------
    def log(self, stage: str, message: str):
        message = getattr(self, "_log_prefix", "") + message
        self.bus.publish(self.task_id, stage, message)
        # 任务日志落盘(工作区 engine.log): 内存环形队列只留最近几百条, 事后归因
        # (语料跑分的低分站)要能翻到采集期的每一步
        ws = getattr(self, "ws", None)
        if ws is not None:
            with suppress(OSError):
                with open(ws / "engine.log", "a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%H:%M:%S')} [{stage}] {message}\n")

    def _set(self, progress=None, stage=None, **extra):
        fields = dict(extra)
        if progress is not None:
            fields["progress"] = int(progress)
        if stage is not None:
            fields["stage"] = stage
            if not self._timeline or self._timeline[-1][0] != stage:
                self._timeline.append((stage, time.monotonic()))
        self.db.update_task(self.task_id, **fields)

    def _phase(self, name: str, t0: float) -> float:
        """子阶段计时: 把 t0 起的耗时累加到 name, 返回当前时刻供下一段接力。"""
        now = time.monotonic()
        acc = getattr(self, "_phase_acc", None)
        if acc is not None:
            acc[name] = acc.get(name, 0.0) + (now - t0)
        return now

    def _phase_times(self) -> dict:
        return {k: round(v, 1) for k, v in getattr(self, "_phase_acc", {}).items()}

    def _stage_times(self) -> dict:
        """阶段耗时(秒, 1 位小数), 按出现顺序; 真实语料跑分用它定位时间去哪了。"""
        out = {}
        now = time.monotonic()
        for i, (stage, t0) in enumerate(self._timeline):
            t1 = self._timeline[i + 1][1] if i + 1 < len(self._timeline) else now
            out[stage] = round(out.get(stage, 0.0) + (t1 - t0), 1)
        return out

    # ------------------------------------------------------------------
    def run(self):
        self.task = self.db.get_task(self.task_id)
        if self.task is None:
            return
        batch = self.db.get_batch(self.task["batch_id"])
        try:
            self.cfg = json.loads(batch.get("config_json") or "{}")
        except ValueError:
            self.cfg = {}
        parts = self.bind_target(self.task["target_url"])
        origin = f"{parts.scheme}://{parts.netloc}"
        domain = self.task["domain"] or parts.netloc
        self._prepare_workspace()

        self._set(progress=5, stage="DOM渲染", status="running",
                  workspace_path=str(self.ws), error_msg="")
        self.log("RENDER", f"任务 #{self.task_id} 启动: {self._target}")
        started = time.time()
        try:
            self._execute(origin, domain)
        except Exception as exc:
            # 瞬时 Playwright 会话崩溃(页面重导航/浏览器回收/网络瞬断)
            # 与稳定失败不同源: 换新会话整链重跑一次, 偶发不判死
            if not isinstance(exc, TargetUnreachable) \
                    and not getattr(self, "_transient_retried", False) and any(
                    m in f"{type(exc).__name__}: {exc}" for m in _TRANSIENT_ERROR_MARKS):
                self.log("WARN", f"瞬时会话崩溃, 自动重试: {type(exc).__name__}: {exc}")
                # 整链重跑前重置运行态(磁带/引用表/校准/保真轨/统计累加器),
                # 防上一次半途采集的残留混入新会话的产物与评分
                self.__init__(self.task_id, self.db, self.bus, self.pool)
                self._transient_retried = True
                self._set(status="running", stage="重试渲染", progress=4, error_msg="")
                return self.run()
            self.log("ERROR", f"任务失败: {type(exc).__name__}: {exc}")
            # 失败任务同样落阶段耗时与失败类别: 语料跑分区分"网络不可达"与"引擎失败"
            fail_stats = {"stage_times": self._stage_times(),
                          "failure": "unreachable" if isinstance(exc, TargetUnreachable)
                          else "engine"}
            fail_stats["elapsed_s"] = round(sum(fail_stats["stage_times"].values()), 1)
            self._set(status="failed", error_msg=f"{type(exc).__name__}: {exc}"[:1500],
                      stats_json=json.dumps(fail_stats, ensure_ascii=False))
            return
        elapsed = time.time() - started
        self.log("INFO", f"任务 #{self.task_id} 完成, 耗时 {elapsed:.1f}s")

    def _prepare_workspace(self):
        """工作区目录 + 下载器(请求头随任务 UA/Cookie/Bearer)。"""
        for sub in ("css", "js", "img", "fonts", "media", "misc"):
            (self.ws / "static" / sub).mkdir(parents=True, exist_ok=True)
        (self.ws / "frames").mkdir(parents=True, exist_ok=True)
        self._dl_headers = {
            "User-Agent": self.cfg.get("user_agent") or config.DEFAULT_USER_AGENT,
            "Referer": self._target,
            "Accept": "*/*",
        }
        if self.cfg.get("cookie"):
            self._dl_headers["Cookie"] = self.cfg["cookie"]
        if self.cfg.get("bearer"):
            self._dl_headers["Authorization"] = f"Bearer {self.cfg['bearer']}"
        self.downloader = smart_downloader.SmartDownloader(self._dl_headers,
                                                           log=self.log)

    # ------------------------------------------------------------------
    def _start_phone_precheck(self, ua, ctx_headers):
        """后台线程以桌面/手机两种 UA 直连取目标页 HTML(与主采集并行, 不占浏览器): 服务端按 UA
        分发(跳转 / 下发不同模板)在这一步就能看出来, 绝大多数响应式站据此免掉浏览器探测。"""
        def work():
            res = {}
            for tag, agent in (("d", ua), ("p", config.PHONE_USER_AGENT)):
                try:
                    headers = dict(ctx_headers or {})
                    headers.update({"User-Agent": agent, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
                    r = requests.get(self._target, headers=headers, timeout=(4, 6), allow_redirects=True)
                    ctype = r.headers.get("Content-Type", "")
                    res[tag] = (r.url, r.text[:2_000_000]) if r.ok and "html" in ctype else None
                except Exception:   # noqa: BLE001 直连不通(风控/证书)就交给浏览器探测
                    res[tag] = None
            self._phone_precheck = res
        self._phone_precheck = None
        self._phone_precheck_thread = threading.Thread(target=work, daemon=True)
        self._phone_precheck_thread.start()

    def _probe_phone_edition(self, ua_headers):
        """手机 UA + 触屏仿真加载目标页, 返回 (落地地址, DOM)。只等到 load(封顶), 不滚动。"""
        budget = float(getattr(config, "MOBILE_EDITION_PROBE_S", 12.0))
        vp = config.VIEWPORTS["mobile"]
        with self.pool.context(viewport={"width": vp["width"], "height": vp["height"]},
                               user_agent=config.PHONE_USER_AGENT, extra_headers=ua_headers,
                               **config.PHONE_DEVICE) as ctx:
            page = ctx.new_page()
            try:
                page.goto(self._target, wait_until="domcontentloaded", timeout=int(budget * 1000))
                with suppress(Exception):   # 跳转在提交时已定, 骨架在 DOMContentLoaded 后基本成形: load 只短等
                    page.wait_for_load_state("load", timeout=2500)
                with suppress(Exception):
                    page.evaluate(CV_QUIET_JS, 400)
                return page.url or self._target, page.evaluate(OUTER_HTML_JS) or ""
            finally:
                with suppress(Exception):
                    page.close()

    def _maybe_mobile_edition(self, ua, ctx_headers, pages_map, mock_entries):
        """原站按手机 UA 分发另一套页面(m. 子域跳转 / 同址移动模板)时, 以真机仿真口径另克隆一份
        手机版(index.m.html, 资源并入同一 static/), 克隆入口按 UA 路由过去。响应式站不触发。"""
        if self._edition_child or self.cfg.get("device") == "phone" \
                or self.cfg.get("viewport", "desktop") == "mobile":
            return None
        if "iphone" in (ua or "").lower() or "android" in (ua or "").lower():
            return None   # 任务本身就用手机 UA 采集
        t0 = time.monotonic()
        thr = float(getattr(config, "MOBILE_EDITION_SIM", 0.45))
        th = getattr(self, "_phone_precheck_thread", None)
        if th is not None:
            th.join(timeout=8)
        pre = getattr(self, "_phone_precheck", None) or {}
        separate, why, phone_url = False, "", None
        if pre.get("d") and pre.get("p"):
            (d_url, d_html), (p_url, p_html) = pre["d"], pre["p"]
            separate, why = separate_phone_edition(d_url, d_html, p_url, p_html, thr)
            why = "直连比对: " + why
            phone_url = p_url
            # 同一份 HTML 且页内没有 UA 嗅探脚本: 不会在浏览器里再分流, 免浏览器探测
            if not separate and re.search(r"navigator\s*\.\s*userAgent", p_html):
                pre = {}
        if not (pre.get("d") and pre.get("p")):
            try:
                phone_url, phone_html = self._probe_phone_edition(ctx_headers)
            except Exception as exc:   # noqa: BLE001
                self.log("RENDER", f"手机版探测失败, 跳过: {str(exc)[:120]}")
                return None
            separate, why = separate_phone_edition(
                getattr(self, "_main_final_url", self._target), self._main_dom_html,
                phone_url, phone_html, thr)
            why = "浏览器比对: " + why
        self.log("RENDER", f"手机版探测({time.monotonic() - t0:.1f}s): {why}")
        if not separate:
            return None
        child = ClonerEngine(self.task_id, _EditionDB(self.db), self.bus, self.pool)
        child._edition_child = True
        child._log_prefix = "[手机版] "
        child.task = self.task
        child.ws = self.ws / "edition_m"
        child.cfg = dict(self.cfg, user_agent=config.PHONE_USER_AGENT, viewport="mobile",
                         device="phone", mode="single", max_pages=1, ai_vision_repair=False,
                         mobile_edition=False, viewport_probe=False)
        child.bind_target(phone_url)
        child._prepare_workspace()
        cparts = urlsplit(phone_url)
        child._execute(f"{cparts.scheme}://{cparts.netloc}", cparts.netloc)
        page_file = child._main_page_file()
        if not page_file.is_file():
            self.log("WARN", "手机版克隆未产出页面, 不接入")
            return None
        copied = merge_static_tree(child.ws / "static", self.ws / "static")
        # 手机版与主页面同目录(目录型目标页落在 page_dir 影子路径, 入口路由按相对路径跳)
        main_file = self._main_page_file()
        rel = main_file.parent.relative_to(self.ws).as_posix()
        m_rel = PHONE_EDITION_FILE if rel in ("", ".") else f"{rel}/{PHONE_EDITION_FILE}"
        (self.ws / m_rel).write_text(
            page_file.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        with suppress(Exception):
            extra = json.loads((child.ws / "mock_data.json").read_text(encoding="utf-8"))
            have = {json.dumps(e, sort_keys=True, ensure_ascii=False) for e in mock_entries}
            for e in extra:
                if json.dumps(e, sort_keys=True, ensure_ascii=False) not in have:
                    mock_entries.append(e)
            (self.ws / "mock_data.json").write_text(
                json.dumps(mock_entries, ensure_ascii=False, indent=2), encoding="utf-8")
        for f in (main_file, main_file.parent / "index.interactive.html"):
            if f.is_file():
                f.write_text(inject_edition_router(f.read_text(encoding="utf-8", errors="replace")),
                             encoding="utf-8")
        pages_map["/" + m_rel] = m_rel
        site = self.ws / "site.json"
        with suppress(Exception):
            meta = json.loads(site.read_text(encoding="utf-8"))
            meta["routes"] = pages_map
            meta["phone_edition"] = {"url": phone_url, "file": m_rel,
                                     "similarity": getattr(child, "_edition_score", None)}
            site.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        # 子克隆的冗余产物不随包(static 已并入; 保留日志/截图/评分便于归因)
        import shutil
        for junk in ("static", "frames", "flask_app", "capture.mhtml"):
            with suppress(OSError):
                tgt = child.ws / junk
                shutil.rmtree(tgt) if tgt.is_dir() else tgt.unlink()
        self._phone_edition = {"url": phone_url, "similarity": getattr(child, "_edition_score", None),
                               "elapsed_s": round(time.monotonic() - t0, 1), "assets_merged": copied}
        self.log("RENDER", f"手机版已接入: {PHONE_EDITION_FILE}(还原度 {self._phone_edition['similarity']}, "
                           f"并入资源 {copied} 个, 耗时 {self._phone_edition['elapsed_s']}s); 手机 UA 打开克隆自动切换")
        return self._phone_edition

    def bind_target(self, target_url: str):
        """绑定目标 URL: 目录型 URL(/apps/#/login)页面真实位置在目录下, 相对路径
        (webpack publicPath='' 懒加载 chunk)以此为基准; 记录 page_dir, 本地化时按
        原路径落位, 避免页面挪到根路径后相对引用全体漂移。返回 urlsplit 结果。
        手动 AI 修复等离线入口也要先绑定, 否则 _main_page_file 落错文件。"""
        self._target = target_url
        parts = urlsplit(target_url)
        target_path = parts.path or "/"
        self._page_dir = "/"
        if target_path.endswith("/") or "." not in target_path.rsplit("/", 1)[-1]:
            # 归一化: 目标路径是不可信输入, `..` 段会让影子页落盘逃出工作区
            self._page_dir = _normalize_page_dir(target_path)
        self._page_frag = ("#" + parts.fragment) if parts.fragment else ""
        return parts

    def _log_repl_blocked(self):
        """CLONER_URL_REPL_TRACE=1: 把本轮被左边界挡下的替换样本写进任务日志(远程归因用)。"""
        from core.clone import support as _sup
        if not _sup.URL_REPL_TRACE or not _sup.URL_REPL_BLOCKED:
            return
        for line in _sup.URL_REPL_BLOCKED[:60]:
            self.log("SMART-DL", f"左边界挡下: {line[:260]}")
        _sup.URL_REPL_BLOCKED.clear()

    # ------------------------------------------------------------------
    def _repair_loop(self, ctx, render_and_score, want_score, results, routes, origin, srcset_map,
                     pages, iframe_pages, frame_records, mock_entries, pages_map):
        """渲染驱动修复循环: 本地化 -> 离线渲染评分 -> 缺失回补 -> 重来; 返回 (detail, score)。
        收敛口径: 无缺失 / 缺失均非样式表字体 / 到顶 / 无进展 / 轮数或时间预算用尽; 末轮低于最优轮时恢复最优轮产物。"""
        detail, score = None, None
        repair_started = time.time()
        prev_sig, prev_score = None, None   # 收敛判定: 上一轮缺失数与分数
        stale_prev = False                   # 上一轮回补是否也未涨分
        prev_struct = None                   # 上一轮的结构性缺失(样式表/字体)路径集合
        best = None                          # 最优轮快照(修复轮回退时恢复)
        for round_i in range(max(1, config.RENDER_REPAIR_ROUNDS)
                             if want_score else 1):
            # P0-A 时间预算: 快站快修, 慢站限时收敛 —— 速度是引擎一等公民
            if time.time() - repair_started > config.REPAIR_TIME_BUDGET:
                self.log("RENDER", f"修复循环时间预算耗尽"
                         f"({config.REPAIR_TIME_BUDGET}s), 提前收敛")
                break
            self._set(progress=62 + round_i * 6,
                      stage="资源重写" if round_i == 0 else f"修复循环第{round_i + 1}轮")
            tp = time.monotonic()
            pairs = self._compose_pairs(results, routes, origin, srcset_map,
                                        iframe_pages, frame_records)
            self._localize(pages, iframe_pages, frame_records, pairs, pages_map)
            self._rewrite_js_files(results, pairs)
            self._rewrite_mock_bodies(mock_entries, pairs)
            self._phase("本地化重写", tp)
            self._log_repl_blocked()
            if not want_score:
                break
            detail, failed_local, remote_missing = render_and_score()
            self.db.touch_task(self.task_id)   # 心跳: 长渲染防误收割
            if detail is not None:
                score = detail["score"]
                self.log("SIMILARITY",
                         f"视觉还原度 {score}% (结构 {detail['structure']:.0%} / "
                         f"边缘 {detail['edge']:.0%} / 文本 {detail['text']:.0%})")
                for warning in detail.get("warnings", []):
                    self.log("WARN", f"[评分告警] {warning}")
                if best is None or score > best["detail"]["score"]:
                    best = self._snapshot_round(round_i, detail, results)
            if getattr(self, "_clone_render_failed", False) and getattr(config, "CLONE_FAIL_FAST", True):
                # 克隆页本身打不开(主文档到不了 DOMContentLoaded、整页截图超时): 回补资源治不了它, 再跑一轮只是
                # 再等两个 30s 超时(实测每轮 60~84s, 任务累计 260s+)。收敛, 交给保真轨出分 + 静态版交付
                self.log("RENDER", f"交互轨克隆页渲染失败, 修复循环第 {round_i + 1} 轮收敛(回补治不了打不开的页面)")
                break
            if not failed_local and not remote_missing:
                break
            if detail is not None and config.REPAIR_STRUCTURAL_ONLY and \
                    not self._structural_missing(failed_local, remote_missing):
                # 缺失只剩图片/脚本/接口/媒体: 活体回补已把浏览器加载过的都取回, 余下多是运行时
                # 才拼出的埋点/备用资源; 再回补 + 整页重渲染历史上几乎不涨分、还常回退
                self.log("RENDER", f"本轮缺失 {len(failed_local) + len(remote_missing)} 个均非样式表/字体,"
                                   f" 不再回补重渲染(交付与评分同为本轮产物)")
                break
            struct_now = {urlsplit(u).path for u in self._structural_missing(failed_local, remote_missing)}
            if detail is not None and config.REPAIR_STRUCTURAL_ONLY and \
                    getattr(config, "REPAIR_STRUCT_SAME_STOP", True) and \
                    prev_struct is not None and struct_now == prev_struct:
                # 只为结构性缺失才再渲染, 而上一轮回补后这批样式表/字体一个没补上(上游 404/鉴权/
                # 连不上): 再回补 + 整页重渲染只会重复同样结果(CI allbirds 第 2、3 轮各 40s, 缺失
                # 589→588、分数 91.7→91.7)
                self.log("RENDER", f"结构性缺失 {len(struct_now)} 个与上一轮相同(回补补不上),"
                                   f" 第 {round_i + 1} 轮收敛")
                break
            prev_struct = struct_now
            if detail is not None and detail["score"] >= config.REPAIR_STOP_SCORE:
                # 已到顶(缺失的多是结账/埋点等不可见请求): 再回补一轮只花 20s 回源 + 一次渲染
                self.log("RENDER", f"还原度 {detail['score']} 已达 {config.REPAIR_STOP_SCORE}, 不再回补")
                break
            # 无进展即停: 上一轮回补之后缺失数没减少、分数也没涨 —— 回补已无能为力
            # (缺失资源上游本就 404/鉴权, 或带时间戳/随机参数每轮都"不同"),
            # 再跑只是重复本地化+渲染(真实语料里 wordpress/runoob/wix 都是 6 轮
            # 分数一动不动)。比较缺失数而非集合: 埋点 URL 的随机参数让集合每轮都变
            missing_n = len(failed_local) + len(remote_missing)
            cur = detail["score"] if detail is not None else None
            no_gain = (prev_score is not None and cur is not None
                       and cur - prev_score < config.REPAIR_MIN_GAIN)
            # 连续两轮回补都不涨分: 缺失数虽在下降, 回补的是登录/埋点/接口这类
            # 不可见请求(真实语料 jd 三轮 77.0 不动, 缺失 17→11 仍在跑)
            stale = no_gain and stale_prev
            if prev_sig is not None and (stale or (missing_n >= prev_sig and (
                    cur is None or prev_score is None or no_gain))):
                self.log("RENDER", f"修复循环无进展(缺失 {prev_sig}→{missing_n}, "
                                   f"分数 {prev_score}→{cur}), 第 {round_i + 1} 轮收敛")
                break
            prev_sig, prev_score, stale_prev = missing_n, cur, no_gain
            if round_i >= config.RENDER_REPAIR_ROUNDS - 1:
                self.log("SMART-DL",
                         f"仍有 {len(failed_local) + len(remote_missing)} 个本地缺失资源"
                         f"(回补轮数已用尽): {sorted(failed_local | {urlsplit(u).path for u in remote_missing})[:6]}")
                break
            self.log("SMART-DL", f"本轮缺失 {missing_n}: "
                                 f"{sorted(failed_local)[:8]} / 外联 {len(remote_missing)}")
            tp = time.monotonic()
            if config.REPAIR_STRUCTURAL_ONLY and getattr(config, "REPAIR_NEED_STRUCT_GAIN", True):
                # 本轮之所以还要重渲染, 只因为有样式表/字体缺失: 先只回补它们。一个都没补回时再整页重渲染 +
                # 评分只会复现同一版面(CI allbirds 第 2 轮 65s, 缺失 463→537, 91.7→91.7), 直接收敛,
                # 交付与评分同为本轮产物; 补回了才连同其余缺失一起回补并重渲染
                s_local = set(self._structural_missing(failed_local, ()))
                s_remote = set(self._structural_missing((), remote_missing))
                new, shadows = self._failed_backfill(ctx, s_local, s_remote, results, origin)
                # 这次只回补了结构性缺失: 新增资源或任何落盘(含桶路径修复——池里已有同名字节, 按请求路径
                # 复制一份, 不记 _bf_shadow_paths)都算补回。CI techcrunch 并发时 2 个字体经桶路径修复补回,
                # 却被判"一个没补回"直接收敛, 交付带着缺字体的版面(56.9, 单跑重渲染后 90.9)
                if not (new or shadows or self._struct_recovered(new, getattr(self, "_bf_shadow_paths", ()))):
                    self._phase("修复回补下载", tp)
                    self.log("RENDER", f"结构性缺失 {len(s_local) + len(s_remote)} 个回补一个没补回,"
                                       f" 不再重渲染, 第 {round_i + 1} 轮收敛")
                    break
                results.update(new)
                rest_new, rest_sh = self._failed_backfill(ctx, set(failed_local) - s_local,
                                                          set(remote_missing) - s_remote, results, origin)
                new.update(rest_new)
                shadows += rest_sh
            else:
                new, shadows = self._failed_backfill(ctx, failed_local,
                                                    remote_missing, results, origin)
            self._phase("修复回补下载", tp)
            if not new and not shadows:
                break
            results.update(new)

        # 修复轮不保证单调(回补进来的埋点/广告资源可能让克隆页更偏离原站):
        # 末轮低于最优轮时恢复最优轮产物(页面/新增文件/截图/校准样式), 交付与分数一致
        if best is not None and (detail is None or
                                 detail["score"] < best["detail"]["score"] - 0.05):
            self._restore_round(best, results)
            self.log("RENDER", f"修复轮回退({detail['score'] if detail is not None else '渲染失败'}→最优 {best['detail']['score']}), "
                               f"已恢复第 {best['round'] + 1} 轮产物")
            detail = best["detail"]
            score = detail["score"]
        return detail, score

    def _ai_config(self):
        """任务级 AI 配置合并(> 环境变量 > config 默认); 无 Key 返回 None。"""
        base_url = (self.cfg.get("ai_base_url") or "").strip() or config.AI_API_BASE_URL
        api_key = (self.cfg.get("ai_api_key") or "").strip() or config.AI_API_KEY
        model = (self.cfg.get("ai_model") or "").strip() or config.AI_MODEL
        if not api_key:
            return None
        return {"base_url": base_url.rstrip("/"), "api_key": api_key, "model": model}

    # ------------------------------------------------------------------
    def _execute(self, origin: str, domain: str):
        exec_started = time.time()
        viewport = config.VIEWPORTS.get(self.cfg.get("viewport", "desktop"),
                                        config.VIEWPORTS["desktop"])
        ctx_headers = {}
        if self.cfg.get("cookie"):
            ctx_headers["Cookie"] = self.cfg["cookie"]
        if self.cfg.get("bearer"):
            ctx_headers["Authorization"] = f"Bearer {self.cfg['bearer']}"
        ua = self.cfg.get("user_agent") or config.DEFAULT_USER_AGENT
        self._ctx_headers = dict(ctx_headers)

        pages = []          # [(route, html)]
        iframe_pages = []   # [(src, html)] 同源 iframe 固化子页
        frame_records = []  # [(src, html)] 跨域 iframe 顶级渲染固化子页
        assets = set()      # 待下载绝对 URL
        srcset_map = {}     # 候选 URL -> 最优 URL(全部指向同一本地文件)
        original_text = ""

        def merge_page_assets(result):
            assets.update(result.get("urls", []))
            for raw, absolute in result.get("rawRefs", []) or []:
                if raw and absolute and len(raw) > 5:
                    self._raw_refs[raw] = absolute
            for raw in result.get("srcsets", []):
                best, candidates = _parse_best_srcset(raw, self._target)
                if best is None:
                    continue
                assets.add(best)
                for candidate in candidates:
                    if candidate != best:
                        srcset_map[candidate] = best

        pages_map = {}
        mock_entries = []
        score = None
        detail = None
        ai_report = None

        device = dict(config.PHONE_DEVICE) if self.cfg.get("device") == "phone" else {}
        if self.cfg.get("mobile_edition", config.MOBILE_EDITION) and not self._edition_child:
            self._start_phone_precheck(ua, ctx_headers)
        with self.pool.context(
            viewport={"width": viewport["width"], "height": viewport["height"]},
            user_agent=ua, extra_headers=ctx_headers, **device,
        ) as ctx:
            main = self._render_page(ctx, self._target, screenshot=True,
                                     collect_links=True)
            self._main_dom_html = main.get("html") or ""
            self._main_final_url = main.get("final_url") or self._target
            merge_page_assets(main)
            original_text = main.get("text") or ""
            (self.ws / "original_text.txt").write_text(original_text, encoding="utf-8")
            if main.get("state") is not None:
                # Cookie/Storage 终态快照: 克隆页启动时前置恢复, 版本守卫自洽
                (self.ws / "state_snapshot.json").write_text(
                    json.dumps(main["state"], ensure_ascii=False), encoding="utf-8")
            for frame in ([] if self._edition_child else main.get("iframes", [])):
                merge_page_assets(frame)
                iframe_pages.append((frame["src"], frame["html"]))
            if iframe_pages:
                self.log("RENDER", f"固化 {len(iframe_pages)} 个同源 iframe 子页面")
            pages.append(("/", main["html"]))

            # ---- 跨域 iframe 递归克隆(顶级页面渲染, 绕过 X-Frame-Options) ----
            # 手机版子克隆不做: frames/frame_N.html 与主克隆同名, 合并时会互相覆盖
            if not self._edition_child:
                self._collect_frame_pages(ctx, main, frame_records, merge_page_assets,
                                          depth=1, target_url=self._target)
            if frame_records:
                self.log("RENDER", f"固化 {len(frame_records)} 个跨域 iframe 子页面(frames/*.html)")
                # iframe 文本并入原站文本指纹(与克隆侧同源 iframe 采集口径一致)
                original_text += "\n" + "\n".join(
                    text for _src, _html, text in frame_records if text)
                (self.ws / "original_text.txt").write_text(original_text, encoding="utf-8")

            self._set(progress=22, stage="CSSOM提取")

            # ---- 同域名多页面 BFS 发现 ----
            if self.cfg.get("mode") == "crawler" and int(self.cfg.get("max_pages", 1)) > 1:
                max_pages = min(int(self.cfg.get("max_pages", 1)), config.MAX_PAGES_LIMIT)
                queue = deque(main["links"])
                visited = {"/"}
                guard = 0
                while queue and len(pages) < max_pages and guard < 80:
                    guard += 1
                    path = queue.popleft()
                    if path in visited:
                        continue
                    visited.add(path)
                    self.log("CRAWL", f"抓取子页面 {path}")
                    try:
                        sub = self._render_page(ctx, origin + path,
                                                screenshot=False, collect_links=False)
                    except Exception as exc:
                        self.log("WARN", f"子页面失败 {path}: {exc}")
                        continue
                    merge_page_assets(sub)
                    for frame in sub.get("iframes", []):
                        merge_page_assets(frame)
                        iframe_pages.append((frame["src"], frame["html"]))
                    self._collect_frame_pages(ctx, sub, frame_records,
                                              merge_page_assets, depth=1,
                                              target_url=self._target)
                    pages.append((path, sub["html"]))

            mock_entries = self._finalize_mocks()
            if self.cfg.get("mock_api", True):
                self.log("MOCK-API", f"共录制 {len(mock_entries)} 个 JSON 接口")
            # Mock 先落盘: 本地沙盒评分服务器需要据此响应克隆页的运行时接口调用
            (self.ws / "mock_data.json").write_text(
                json.dumps(mock_entries, ensure_ascii=False, indent=2),
                encoding="utf-8")
            # VCR 网络磁带落盘(W2): 沙盒按模式策略包开关决定是否回放
            if self._net_tape:
                (self.ws / "net_tape.json").write_text(
                    json.dumps(self._net_tape, ensure_ascii=False),
                    encoding="utf-8")
                self.log("VCR", f"网络磁带: 录制 {len(self._net_tape)} 条响应 "
                                f"({self._tape_bytes // 1024} KB)")

            # ---- CAS 并发下载 + CSS 递归解析(requests -> 浏览器会话降级) ----
            self._set(progress=30, stage="CAS并发下载")
            # 静态补扫(完整性网): 固化 HTML 里仍引用、却没进采集集合的媒体/样式(水合后期插入、
            # 视口外 loading=lazy 的 img 等, 浏览器侧采集与评分重放都碰不到它们)。全语料复盘:
            # gitee 首页 62 张 <img src=https://e-assets...> 从未尝试下载, 交付后逐张回源外联。
            swept = 0
            for _route, _html in pages:
                before_n = len(assets)
                merge_page_assets(_static_refs(_html, self._target))
                swept += len(assets) - before_n
            if swept:
                self.log("SMART-DL", f"静态补扫: 固化 HTML 里 {swept} 个未进采集集合的资源引用补入下载队列")
            assets.add(origin.rstrip("/") + "/favicon.ico")
            # @font-face 下载计划: 页面字符全集(含 Mock 接口响应) + 内联样式里的字体声明
            if getattr(config, "FONT_PLAN", True):
                with suppress(Exception):
                    texts = [h for _r, h in pages]
                    texts += [str(e.get("response_body") or "") for e in self._mock_raw
                              if isinstance(e, dict)]
                    self._ff_cps = page_codepoints(texts)
                    for _route, _html in pages:
                        for m in _STYLE_BLOCK_RE.finditer(_html):
                            self._font_plan_add(m.group(2), self._target)
            # 保真轨(MHTML 禁脚本回放)只依赖采集产物: 下载期间浏览器空闲, 先在后台页里加载起来,
            # 评分时直接截图(旧实现在评分阶段才加载 + 固定 2s 静置, 每站串行 1.5~3s)
            if self.cfg.get("similarity", True) and getattr(config, "FIDELITY_PRESTART", True):
                self._prestart_fidelity(ctx)
            n_pre = 0
            with suppress(Exception):
                n_pre = self._start_backfill_prefetch(ctx, assets, mock_entries, origin)
            results = self._materialize(assets, origin, ctx=ctx)
            if n_pre:
                self.log("SMART-DL", f"回补预取: {n_pre} 个活体差集/响应体资产在 CAS 期间并发回源")
            self.log("INFO", f"资源池落地完成: {len(results)} 个文件 "
                             f"(浏览器会话救回 {self.downloader.browser_rescues} 个)")

            # ---- 活体会话请求差集回补 ----
            self._set(progress=48, stage="活体回补")
            rescued = self._live_backfill(ctx, results, origin)
            if rescued:
                self.log("SMART-DL", f"活体差集回补: 追加 {rescued} 个浏览器实际加载过的资源")

            mirrored = self._mirror_runtime_paths(results)
            if mirrored:
                self.log("SMART-DL", f"运行时路径镜像: {mirrored} 个脚本拼址加载的资源按原路径落盘"
                                     f"(MSE 视频/场景/贴图, 字面替换改写不到)")

            # ---- Mock 响应体资产收割: API 下发的图片/资源是静态提取盲区 ----
            harvested = self._harvest_mock_assets(ctx, mock_entries, results, origin)
            if harvested:
                self.log("SMART-DL", f"Mock 资产收割: 录制响应体中追加下载 {harvested} 个资源")
            if self._prefetch_hits:
                self.log("SMART-DL", f"回补预取命中 {self._prefetch_hits} 个(CAS 期间已取回, 回补不再等回源)")

            # ---- 渲染驱动修复循环: 本地化 -> 离线渲染评分 -> 404 回补 -> 重来 ----
            routes = {route for route, _ in pages}
            want_score = self.cfg.get("similarity", True) and \
                (self.ws / "original_preview.png").is_file()
            # 框架模式识别(W1): JS 字节+DOM 双指纹 → 模式策略包(一类一策地基)。
            # 静态资源已落盘, 证据齐备; 识别失败回落 DEFAULT 不阻断主流程。
            try:
                # DOM 证据取采集期主页面渲染 DOM(此时 index.html 尚未本地化落盘)
                self._pattern_info = pattern_detector.detect(
                    self.ws, html=pages[0][1] if pages else None,
                    runtime=getattr(self, "_runtime_hits", None))
                self._strategy_pack = strategy_packs.for_pattern(
                    self._pattern_info["pattern"])
                self.log("PATTERN",
                         f"识别模式 {self._pattern_info['pattern']} "
                         f"(置信 {self._pattern_info['confidence']:.2f}, 特征: "
                         f"{','.join(self._pattern_info['features']) or '无'})")
            except Exception as exc:
                self._strategy_pack = strategy_packs.for_pattern("DEFAULT")
                self.log("WARN", f"模式识别失败, 使用默认策略包: {exc}")
            # 热力图延迟: 中间修复轮不写 diff_preview.png, 收敛后统一生成一次
            render_and_score = self._make_render_and_score(ctx, defer_diff=True)

            detail, score = self._repair_loop(
                ctx, render_and_score, want_score, results, routes, origin, srcset_map,
                pages, iframe_pages, frame_records, mock_entries, pages_map)

            fp_left = getattr(self, "_fid_page", None)   # 交互轨已够高、保真轨没用上的预加载页
            if fp_left is not None:
                self._fid_page = None
                with suppress(Exception):
                    fp_left.close()
            if want_score and (detail is not None or getattr(self, "_clone_render_failed", False)):
                if detail is not None:
                    self._write_final_diff()
                with suppress(Exception):
                    self._static_score = self._try_static_track(ctx, score)

            # ---- 智能 Mock(表单解析, 可选 LLM 润色) ----
            html_sources = []
            for name in ("index.html",):
                file = self.ws / name
                if file.is_file():
                    html_sources.append(file.read_text(encoding="utf-8", errors="replace"))
            for i in range(1, len(frame_records) + 1):
                file = self.ws / "frames" / f"frame_{i}.html"
                if file.is_file():
                    html_sources.append(file.read_text(encoding="utf-8", errors="replace"))
            smart_mocks = ai_mock_generator.generate_smart_mocks(
                html_sources, mock_entries, self._ai_config(), self.log)
            if smart_mocks:
                mock_entries = mock_entries + smart_mocks
                self.log("MOCK-API", f"智能表单 Mock: 追加 {len(smart_mocks)} 个路由")

            (self.ws / "mock_data.json").write_text(
                json.dumps(mock_entries, ensure_ascii=False, indent=2),
                encoding="utf-8")

            # ---- AI 视觉闭环(门控: 低分 + 已配置 Key + 模式策略包允许) ----
            ai_cfg = self._ai_config()
            if self.cfg.get("ai_vision_repair") and want_score and detail is not None \
                    and time.time() - exec_started > config.AI_TIME_BUDGET:
                self.log("AI-VISION", "任务时间预算已耗尽, 跳过 AI 修复轮(P0-A 速度调度)")
            elif self.cfg.get("ai_vision_repair") and want_score and detail is not None:
                if ai_cfg is None:
                    self.log("AI-VISION", "未配置大模型 API Key, 视觉闭环修复跳过"
                                         "(可在高阶配置填写或设置 CLONER_AI_KEY)")
                elif not self._strategy_pack.ai_vision_repair:
                    self.log("AI-VISION",
                             f"模式 {self._pattern_info['pattern']} 策略包豁免 AI 修复"
                             "(实证该模式下 AI 补丁中性偏负)")
                elif score is not None and score >= config.AI_REPAIR_TRIGGER_SCORE:
                    self.log("AI-VISION",
                             f"诚实评分 {score}% >= 门限 {config.AI_REPAIR_TRIGGER_SCORE:.0f}%, 无需 AI 修复")
                else:
                    self._set(progress=86, stage="AI视觉修复")
                    self.log("AI-VISION",
                             f"评分 {score}% 低于门限 {config.AI_REPAIR_TRIGGER_SCORE:.0f}%, "
                             f"启动大模型视觉闭环 (模型: {ai_cfg['model']})")
                    try:
                        ai_report = ai_vision_repair.run_vision_repair(
                            self.ws, ai_cfg, detail, self.log, render_and_score,
                            page_file=self._main_page_file(),
                            region_cells=self._region_cells())
                        if ai_report.get("final_score") is not None:
                            score = ai_report["final_score"]
                            detail = dict(detail or {})
                            detail["score"] = score
                        if any(r.get("score_after") is not None and not r.get("rolled_back")
                               for r in ai_report.get("rounds", [])):
                            self._write_final_diff()   # 产物被 AI 补丁改过, 热力图随之更新
                    except Exception as exc:
                        self.log("AI-VISION", f"视觉闭环异常: {type(exc).__name__}: {exc}")
                        ai_report = {"error": f"{type(exc).__name__}: {exc}"}

            if detail is not None:
                detail = dict(detail)
                if ai_report:
                    detail["ai_repair"] = {
                        k: ai_report.get(k) for k in ("rounds", "final_score", "error")
                        if ai_report.get(k) is not None}
                # N1 保真轨: MHTML 确定性渲染的相似度(与交互轨分数分开记账)
                if getattr(self, "_fidelity_score", None) is not None:
                    detail["fidelity_score"] = self._fidelity_score
                (self.ws / "similarity_detail.json").write_text(
                    json.dumps(detail, ensure_ascii=False, indent=2),
                    encoding="utf-8")

            site_meta = {
                "task_id": self.task_id, "domain": domain, "upstream": origin,
                "routes": pages_map,
                "proxy_enabled": bool(self.cfg.get("proxy_fallback", True)),
                "similarity": score,
                "similarity_detail": detail,
                "fidelity_score": getattr(self, "_fidelity_score", None),
                "frames": [f"/frames/frame_{i + 1}.html" for i in range(len(frame_records))],
                "pattern": getattr(self, "_pattern_info", {}).get("pattern", "DEFAULT"),
            }
            (self.ws / "site.json").write_text(
                json.dumps(site_meta, ensure_ascii=False, indent=2),
                encoding="utf-8")

        if self._edition_child:
            # 交付的是静态版时报静态版实测分(与主克隆 preview_track 同口径), 否则报两轨最优
            static = getattr(self, "_static_score", None)
            fid = getattr(self, "_fidelity_score", None)
            self._edition_score = static if isinstance(static, (int, float)) else max(
                [x for x in (score, fid) if isinstance(x, (int, float))], default=None)
            return
        # ---- 手机版: 原站按手机 UA 分发另一套页面时另克隆一份, 手机 UA 打开克隆自动切过去 ----
        if self.cfg.get("mobile_edition", config.MOBILE_EDITION):
            try:
                self._maybe_mobile_edition(ua, ctx_headers, pages_map, mock_entries)
            except Exception as exc:   # noqa: BLE001 手机版是增量交付, 失败不影响主克隆
                self.log("WARN", f"手机版克隆失败(不影响桌面版): {type(exc).__name__}: {str(exc)[:200]}")

        # ---- 生成 Flask 工程 + ZIP ----
        self._set(progress=92, stage="Flask构建打包")
        tracker_cfg = self.db.get_tracker_config(self.task_id)
        bake_cfg = None
        if tracker_cfg and tracker_cfg.get("enabled"):
            from core import security
            bake_cfg = {
                "token": security.decrypt_value(tracker_cfg.get("token", "")),
                "secret": security.decrypt_value(tracker_cfg.get("secret_enc", "")),
                "keyword": tracker_cfg.get("keyword", ""),
                "slots": tracker_cfg.get("slots") or [],
                "mask": bool(tracker_cfg.get("mask", True)),
            }
        # 交付侧已知死路径: 下载期/回补期源站明确 4xx/5xx 的同源路径, 生成应用对其秒 404,
        # 不再走反代兜底回源(favicon/缺图/计数接口等每次加载都白等一轮超时)
        # 远程字体 swap: 本地化后的样式表里仍引用未本地化字体(采集时 CDN 连不上)的 @font-face
        # 补 font-display:swap, 部署后文字不再隐形等字体超时(评分渲染拦截外联, 画面不变)
        swapped_css = 0
        for css_file in (self.ws / "static" / "css").glob("*.css") if (self.ws / "static" / "css").is_dir() else []:
            with suppress(OSError, UnicodeDecodeError):
                text = css_file.read_text(encoding="utf-8", errors="replace")
                new_text = fontface_swap_for_remote(text)
                if new_text != text:
                    css_file.write_text(new_text, encoding="utf-8")
                    swapped_css += 1
        if swapped_css:
            self.log("DEPLOY", f"远程字体 swap: {swapped_css} 个样式表的未本地化 @font-face 补 font-display:swap")
        page_netloc = urlsplit(origin).netloc.lower()
        dead_paths = sorted({
            urlsplit(u).path for u in (self._dead_urls | self._backfill_dead)
            if (urlsplit(u).netloc or "").lower() == page_netloc and urlsplit(u).path
        })
        with suppress(OSError):
            (self.ws / "known_dead.json").write_text(
                json.dumps(dead_paths, ensure_ascii=False), encoding="utf-8")
        zip_path = flask_generator.generate_project(
            self.task_id, origin, self.ws, pages_map,
            mock_entries, bool(self.cfg.get("proxy_fallback", True)),
            tracker_hook="",
            tracker_cfg=bake_cfg,
        )
        self.log("BUILD", f"Flask 工程已生成: {zip_path.name}")

        stats = self._collect_stats()
        stats["api"] = len(mock_entries)
        # 部署隐患审计(交付侧度量, 评分口径之外): 交付首页里仍会阻塞首屏的未本地化脚本/样式。
        # 此前评分只量克隆耗时与还原度, 离线渲染对非本地请求瞬时 abort, 交付产物却等满代理
        # 超时 —— 这里把交付侧也量进 stats, 语料跑分据此汇总, 不再有盲区。
        try:
            main_html = (self.ws / pages_map.get("/", "index.html")).read_text(
                encoding="utf-8", errors="replace")
            audit = deploy_hazard_audit(main_html, urlsplit(origin).netloc, self.ws)
            audit["dead_paths"] = len(dead_paths)
            stats["deploy_audit"] = audit
            if audit["blocking_scripts"] or audit["blocking_styles"]:
                self.log("DEPLOY", f"部署隐患: 阻塞脚本 {audit['blocking_scripts']} / "
                                   f"阻塞样式 {audit['blocking_styles']} 仍未本地化")
            else:
                self.log("DEPLOY", f"部署审计通过: 未本地化引用 {audit['uncaptured_refs']} 个均已去阻塞, "
                                   f"已知死路径 {len(dead_paths)} 条交付侧秒 404")
        except Exception as exc:   # noqa: BLE001 审计不阻断交付
            self.log("DEPLOY", f"部署审计跳过: {type(exc).__name__}: {exc}")
        if detail is not None:
            stats["similarity_detail"] = detail
        # 端到端度量: 识别结果与阶段耗时随任务落库, 真实语料跑分直接读取, 不翻日志
        stats["pattern"] = self._pattern_info.get("pattern", "DEFAULT")
        stats["pattern_confidence"] = round(float(self._pattern_info.get("confidence", 0.0) or 0.0), 2)
        stats["pattern_features"] = list(self._pattern_info.get("features", []) or [])
        stats["calibrations"] = self._calibrations
        if getattr(self, "_phone_edition", None):
            stats["phone_edition"] = self._phone_edition
        stats["stage_times"] = self._stage_times()
        stats["phase_times"] = self._phase_times()
        stats["elapsed_s"] = round(sum(stats["stage_times"].values()), 1)
        # 双轨取优: 引擎能力 = max(交互轨重演, 保真轨 MHTML 确定性渲染)。
        # 两轨是同一采集的两个交付物, 取更能保真者作为任务还原度。
        fidelity = getattr(self, "_fidelity_score", None)
        # 双轨明细恒记账: 头条分数取优的同时, 两轨原值都可追溯(诚实口径)
        stats["interactive_score"] = score if isinstance(score, (int, float)) else None
        stats["fidelity_score"] = fidelity if isinstance(fidelity, (int, float)) else None
        stats["dominant_track"] = "interactive"
        static_score = getattr(self, "_static_score", None)
        if isinstance(static_score, (int, float)):
            # 预览/导出默认给的是静态版: 记下它的实测分, 跑分据此报"所见即所得"口径
            stats["static_score"] = static_score
            stats["preview_track"] = "static"
        stats["score_baseline"] = "pristine"   # 基准截图取自 DOM 改动前(v4.5 起)
        blocked = self._detect_blocked()
        if blocked:
            stats["baseline_blocked"] = blocked
            self.log("WARN", f"环境受阻: {blocked}; 本任务分数不反映引擎能力")
        if isinstance(fidelity, (int, float)) and \
                (not isinstance(score, (int, float)) or fidelity > score):
            score = fidelity
            stats["dominant_track"] = "fidelity"
        self._set(status="completed", progress=100, stage="完成",
                  similarity_score=score, stats_json=json.dumps(stats, ensure_ascii=False),
                  zip_path=str(zip_path))
        self.db.increment_completed(self.task["batch_id"])

    def _detect_blocked(self) -> str:
        def _read(name):
            f = self.ws / name
            try:
                return f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
            except OSError:
                return ""
        orig = _read("original_text.txt")
        if not orig:
            return ""
        # 看采集快照(保真轨文本 = 采集时刻的 DOM): 快照里已是拦截页才算环境受阻。
        # 只有交互轨回放变成拦截页(回放时活体请求被风控)是交付物缺陷, 不在此豁免;
        # 没有保真轨时退回交互轨文本
        captured = _read("fidelity_text.txt") or _read("cloned_text.txt")
        title = ""
        m = re.search(r"<title[^>]*>([^<]{1,200})</title>", getattr(self, "_main_dom_html", "") or "", re.I)
        if m:
            title = _html.unescape(m.group(1))
        return detect_blocked_page(orig, captured, title)

    # ------------------------------------------------------------------
    @staticmethod
    def _inject_spa_disguise(html: str, route: str) -> str:
        script = SPA_DISGUISE_TEMPLATE.replace(
            "__ORIG_PATH_JSON__", _safe_json(route or "/")
        )
        match = re.search(r"<head[^>]*>", html, re.I)
        if match:
            insert_at = match.end()
            return html[:insert_at] + script + html[insert_at:]
        return script + html

    # ------------------------------------------------------------------
    def _collect_stats(self) -> dict:
        """以任务工作空间真实文件为准统计; 固化产物(HTML/Mock)也计入总体积。"""
        stats = {"css": 0, "js": 0, "img": 0, "font": 0, "media": 0,
                 "misc": 0, "total_bytes": 0, "assets_total": 0}
        folder_to_bucket = {"css": "css", "js": "js", "img": "img",
                            "fonts": "font", "media": "media", "misc": "misc"}
        static_root = self.ws / "static"
        if static_root.is_dir():
            for folder in static_root.iterdir():
                bucket = folder_to_bucket.get(folder.name)
                if bucket is None or not folder.is_dir():
                    continue
                for file in folder.rglob("*"):
                    if file.is_file():
                        stats[bucket] += 1
                        stats["assets_total"] += 1
                        stats["total_bytes"] += file.stat().st_size

        # 内联固化产物(index/子页/iframe/frames 页/mock/元数据)计入总大小
        for entry in self.ws.rglob("*"):
            if entry.is_file() and entry.suffix in (".html", ".json") \
                    and "flask_app" not in entry.parts \
                    and "static" not in entry.parts:
                with suppress(OSError):
                    stats["total_bytes"] += entry.stat().st_size

        # 防御: 若文件扫描为零但下载期确实落盘过(极端权限场景), 回退累计器
        if stats["assets_total"] == 0 and self._stat_accum["bytes"] > 0:
            stats["css"] = self._stat_accum["css"]
            stats["js"] = self._stat_accum["js"]
            stats["img"] = self._stat_accum["img"]
            stats["font"] = self._stat_accum["fonts"]
            stats["media"] = self._stat_accum["media"]
            stats["misc"] = self._stat_accum["misc"]
            stats["assets_total"] = (stats["css"] + stats["js"] + stats["img"]
                                    + stats["font"] + stats["media"] + stats["misc"])
            stats["total_bytes"] = self._stat_accum["bytes"]

        stats["total_bytes_h"] = _fmt_size(stats["total_bytes"])
        return stats
