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


_EXT_SHAPE_RE = re.compile(r"^\.[a-z0-9]{1,10}$")


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


class ClonerEngine:
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
        try:
            phone_url, phone_html = self._probe_phone_edition(ctx_headers)
        except Exception as exc:   # noqa: BLE001
            self.log("RENDER", f"手机版探测失败, 跳过: {str(exc)[:120]}")
            return None
        separate, why = separate_phone_edition(
            getattr(self, "_main_final_url", self._target), self._main_dom_html,
            phone_url, phone_html, float(getattr(config, "MOBILE_EDITION_SIM", 0.45)))
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

    # ------------------------------------------------------------------
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
            assets.add(origin.rstrip("/") + "/favicon.ico")
            results = self._materialize(assets, origin, ctx=ctx)
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

            repair_started = time.time()
            prev_sig, prev_score = None, None   # 收敛判定: 上一轮缺失数与分数
            stale_prev = False                   # 上一轮回补是否也未涨分
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
                if not failed_local and not remote_missing:
                    break
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
                new, shadows = self._failed_backfill(ctx, failed_local,
                                                    remote_missing, results, origin)
                self._phase("修复回补下载", tp)
                if not new and not shadows:
                    break
                results.update(new)

            # 修复轮不保证单调(回补进来的埋点/广告资源可能让克隆页更偏离原站):
            # 末轮低于最优轮时恢复最优轮产物(页面/新增文件/截图/校准样式), 交付与分数一致
            if best is not None and detail is not None and \
                    detail["score"] < best["detail"]["score"] - 0.05:
                self._restore_round(best, results)
                self.log("RENDER", f"修复轮回退({detail['score']}→最优 {best['detail']['score']}), "
                                   f"已恢复第 {best['round'] + 1} 轮产物")
                detail = best["detail"]
                score = detail["score"]

            if want_score and detail is not None:
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
        zip_path = flask_generator.generate_project(
            self.task_id, origin, self.ws, pages_map,
            mock_entries, bool(self.cfg.get("proxy_fallback", True)),
            tracker_hook="",
            tracker_cfg=bake_cfg,
        )
        self.log("BUILD", f"Flask 工程已生成: {zip_path.name}")

        stats = self._collect_stats()
        stats["api"] = len(mock_entries)
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
        return detect_blocked_page(orig, captured)

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
            if self.cfg.get("viewport_reload", True):
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
                    ClonerEngine._start_settle_narrow(p2)
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
                        [ClonerEngine.AUTO_SCROLL_JS, [int(budget * 1000), 54000]])

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
                p2.evaluate(ClonerEngine.AUTO_SCROLL_JS, [int(budget * 1000), 54000])
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
            diff = sorted(p for p, v in main.items() if p and p in other
                          and other[p][0] == v[0] and other[p][3] != v[3])
            top = []
            for p in diff:
                if not any(p.startswith(q + ">") for q in top):
                    top.append(p)
            if not top or len(top) > config.VIEWPORT_VARIANT_MAX or tag not in pages:
                continue
            ClonerEngine._settle_narrow_page(pages[tag])
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
            inflight[id(req)] = time.monotonic()
            last[0] = time.monotonic()

        def _on_done(req):
            inflight.pop(id(req), None)

        page.on("request", _on_req)
        page.on("requestfinished", _on_done)
        page.on("requestfailed", _on_done)
        try:
            with suppress(Exception):
                page.wait_for_load_state("load", timeout=cap_ms)
            deadline = time.monotonic() + cap_ms / 1000
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
                self._full_shot(page, self.ws / "original_preview.png", "原站")
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
        activated = _safe_eval(FORM_ACTIVATE_JS)
        if activated:
            self.log("RENDER", f"表单激活: focus/blur {activated} 个输入控件")
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
        if path_ext and not _EXT_SHAPE_RE.match(path_ext):
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
        for url in sorted(assets, key=lambda u: _asset_priority(u, live)):
            if url.startswith(("http://", "https://")) and url not in seeded:
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
                        for raw, absolute in refs:
                            if absolute not in seen:
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
                for job in failed_for_browser[:30]:
                    if len(results) >= config.MAX_ASSETS_PER_TASK:
                        break
                    out = self.downloader.fetch_via_browser(
                        job["url"], ctx.request, referer=job.get("referer") or origin + "/")
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
        for url, raw in candidates.items():
            if len(results) >= config.MAX_ASSETS_PER_TASK:
                break
            pre = self._live_bodies.get(url)
            out = dict(pre) if pre is not None else self.downloader.fetch_via_browser(
                url, ctx.request, referer=self._target)
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
        # 浏览器会话自有熔断(requests 被拒的主机浏览器可能仍可达, 不沿用下载阶段熔断):
        # 串行回补里同一主机网络层连续失败即跳过余下, 免逐个空等超时
        b_fail = {}
        # 已取回响应体的不占回补名额(本地读取, 不走网络); 名额只限需要真回源的
        missing.sort(key=lambda u: u not in self._live_bodies)
        # 没有现成响应体的: 信标不回源; 第三方先并发直连预取(同 _failed_backfill), 直连拿不到的再串行走会话
        missing = [u for u in missing if u in self._live_bodies or not beacon_like(u)]
        net_urls = [u for u in missing if u not in self._live_bodies][:config.LIVE_HARVEST_MAX]
        third = {u for u in net_urls if urlsplit(u).netloc != page_netloc}
        prefetched = {}
        if third:
            ex = ThreadPoolExecutor(max_workers=8, thread_name_prefix="live-prefetch")
            try:
                futs = {ex.submit(self.downloader.fetch_direct, u, self._target): u for u in third}
                done, _ = _wait_futures(list(futs), timeout=config.BACKFILL_BUDGET)
                for f in done:
                    with suppress(Exception):
                        prefetched[futs[f]] = f.result()
            finally:
                ex.shutdown(wait=False, cancel_futures=True)
        net_left = config.LIVE_HARVEST_MAX
        cap = config.MAX_ASSETS_PER_TASK + config.RENDER_ASSET_RESERVE
        for url in missing:
            if len(results) >= cap:
                break
            if url not in self._live_bodies:
                if net_left <= 0:
                    break
                net_left -= 1
            host = urlsplit(url).hostname or ""
            if b_fail.get(host, 0) >= config.HOST_FAIL_TRIP:
                continue
            pre = self._live_bodies.get(url)
            if pre is None and url in third:
                got = prefetched.get(url)
                if got is not None and got.get("ok"):
                    pre = got
                elif got is not None and "HTTP 4" not in str(got.get("error")):
                    continue       # 直连超时/连不上: 会话也救不回, 不串行空等
            out = dict(pre) if pre is not None else self.downloader.fetch_via_browser(
                url, ctx.request, referer=self._target)
            out = self._normalize_out(out)
            if not out["ok"]:
                if not str(out.get("error") or "").startswith(("HTTP", "空响应体", "oversize",
                                                               "content-type")):
                    b_fail[host] = b_fail.get(host, 0) + 1
                continue
            info = self._store(out)
            results[out["url"]] = info
            added += 1
            if out["is_css"]:
                text = out["content"].decode("utf-8", "replace")
                for raw, absolute in parse_css_refs(text, out["url"]):
                    if absolute in results or absolute in self._live_seen:
                        continue
                    pre = self._live_bodies.get(absolute)
                    child = dict(pre) if pre is not None else self.downloader.fetch_via_browser(
                        absolute, ctx.request, referer=out["url"])
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

        def _fetch(url):
            if url in dead or time.monotonic() > deadline:
                return None
            out = self._normalize_out(self.downloader.fetch_via_browser(
                url, ctx.request, referer=self._target))
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
        pre = {}
        third = [u for u in todo if urlsplit(u).netloc != page_netloc and u not in dead]
        if third:
            ex = ThreadPoolExecutor(max_workers=8, thread_name_prefix="backfill")
            try:
                futs = {ex.submit(self.downloader.fetch_direct, u, self._target): u for u in third}
                done, _ = _wait_futures(list(futs), timeout=max(0.5, deadline - time.monotonic()))
                for f in done:
                    with suppress(Exception):
                        pre[futs[f]] = self._normalize_out(f.result())
            finally:
                ex.shutdown(wait=False, cancel_futures=True)   # 没回来的直连不等, 不拖住本轮
        for url in todo:
            if len(results) + len(new) >= config.MAX_ASSETS_PER_TASK + config.RENDER_ASSET_RESERVE:
                break
            out = pre.get(url)
            if out is not None and not out["ok"] and "HTTP 4" not in str(out.get("error")):
                dead.add(url)          # 第三方直连超时/连不上: 浏览器会话也救不回, 不再串行重试
                continue
            if out is None or not out["ok"]:
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
            remote = [u for u in self._live_seen if urlsplit(u).path == path]
            for cand in (origin.rstrip("/") + path,
                         origin.rstrip("/") + "/assets/" + base):
                if cand not in remote:
                    remote.append(cand)
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
            return _utf8_charset(_inject_responsive(localized))

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
            localized = _utf8_charset(_inject_responsive(localized))
            (self.ws / f"iframe_{i + 1}.html").write_text(
                localized, encoding="utf-8")
            pages_map[f"/iframe_{i + 1}.html"] = f"iframe_{i + 1}.html"
        for i, (_src, html, _text) in enumerate(frame_records):
            localized = _BASE_TAG_RE.sub(
                "", rewrite_js_navigation(apply_replacements(html, html_pairs)))
            localized = scope_bake_css(localized, self._band_floor_of(localized))
            localized = self._inject_after_head(localized, sim_script)
            localized = _utf8_charset(_inject_responsive(localized))
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
            # W4 状态注入(模式策略包): 采集期终态 Cookie(含 httpOnly)/Storage
            # 以沙盒原点(127.0.0.1)回放, 让运行时的登录态/配置态判断与源站一致。
            # HTML 注入版(inject_state_restore)无法恢复 httpOnly —— 此处补全。
            if self._strategy_pack.state_replay:
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

            def _fidelity_guard(route):
                try:
                    if route.request.url.startswith(("file:", "data:", "blob:", "cid:")):
                        route.continue_()
                    else:
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
                fpage = ctx.new_page()
                try:
                    # 离线守卫: 归档内资源由 Chromium 直接供给(不经网络层),
                    # 归档外的引用一律中止 —— 保真轨必须确定且不回源
                    fpage.route("**/*", _fidelity_guard)
                    fpage.goto('file://' + str(self.ws / 'capture.mhtml'),
                               wait_until='load', timeout=30000)
                    with suppress(Exception):
                        self._settle(fpage, cap_ms=2000)
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
                    # A1 完整版: 像素差分 → 元素映射 → CSS 覆盖合成(确定性校准闭环)。
                    # 网格差分定位色差格(原站侧纯色格才取), elementFromPoint 映射到
                    # 克隆 DOM 元素, 以原站格色生成覆盖 —— 注入后重渲染重评分一轮。
                    if detail is not None and (detail.get('score') or 0) < 99 \
                            and self._calibrations < 2 and failed_local \
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
