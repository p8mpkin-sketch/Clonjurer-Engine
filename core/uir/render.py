"""生成产物的渲染 + 截图 + 评分(复用 browser_pool 与 compute_visual_similarity)。

口径与克隆侧完全一致: 整页截图 → compute_visual_similarity(参考图, 产物图)。
截图输入: 参考图 = 输入截图, 无原文文本 → 评分 = 0.6×结构 + 0.4×边缘(v3 自动重分配)。
描述输入: 无参考图 → 退化为「描述覆盖度」自评(文本 F2 + 结构自洽), 报分单列。
"""
import re
import time
from contextlib import suppress
from pathlib import Path

import config
from core import cloner_engine as _ce
from core import pixel_calibrator

_TAG_RE = re.compile(r"<[^>]+>")
# 描述里的指令/功能词(不是页面内容, 不应要求出现在页面上)
_DESC_STOP = re.compile(r"(一个|一张|一篇|一款|页面|网页|界面|站点|网站|主题|风格|品牌叫|叫做|名为|标题|底部|顶部|左侧|右侧|中间|上方|下方|"
                        r"有|和|与|及|带|含|包含|个|张|块|条|项|列|栏|的|了|是|在|用|做|要|请|生成|设计|制作|布局|样式|排版|"
                        r"颜色|色|蓝色|红色|绿色|紫色|橙色|黑色|白色|深色|浅色|暗色|亮色|蓝|红|绿|紫|橙|黑|白|"
                        r"登录页|落地页|仪表盘|列表页|文章页|详情页|首页|官网|后台|"
                        r"[,，。:：;；、()（）「」“”\"'!！?？\-—\s]+)")


def _content_terms(desc: str) -> str:
    """去掉指令词/版式词/标点, 留下应出现在页面上的内容词(品牌、标题、字段、业务名词)。"""
    return _DESC_STOP.sub(" ", desc or "").strip()
_WS_RE = re.compile(r"\s+")


def visible_text_of(html_text: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_text, flags=re.S | re.I)
    t = _TAG_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def screenshot_dir(pool, ws: Path, entry: Path, out_png: Path, viewport: dict, log=None,
                   settle_ms: int = 350) -> dict:
    """用 Playwright 把本地产物整页截图到 out_png; 返回 {w,h,elapsed}。"""
    vp = {"width": int(viewport.get("width") or 1440), "height": int(viewport.get("height") or 900)}
    t0 = time.monotonic()
    with pool.context(viewport=vp) as ctx:
        page = ctx.new_page()
        page.set_default_timeout(config.SHOT_TIMEOUT)
        page.goto(entry.resolve().as_uri(), wait_until="load")
        with suppress(Exception):
            page.evaluate("() => document.fonts && document.fonts.ready")
        page.wait_for_timeout(settle_ms)
        dims = page.evaluate("() => [document.documentElement.clientWidth || innerWidth, "
                             "Math.max(document.documentElement.scrollHeight, document.body.scrollHeight, innerHeight)]")
        w = max(1, int(dims[0]))
        cap = min(int(_ce.SCORE_MAX_HEIGHT * w / _ce.SCORE_WIDTH), _ce.SHOT_MAX_TEXTURE)
        if int(dims[1]) > cap:
            page.screenshot(path=str(out_png), full_page=True,
                            clip={"x": 0, "y": 0, "width": w, "height": cap}, timeout=config.SHOT_TIMEOUT)
        else:
            page.screenshot(path=str(out_png), full_page=True, timeout=config.SHOT_TIMEOUT)
        page.close()
    return {"w": w, "h": int(dims[1]), "elapsed": round(time.monotonic() - t0, 2)}


def score_against_reference(ref_png: Path, out_png: Path, diff_png: Path = None,
                            ref_text: str = "", out_text: str = "") -> dict:
    """与克隆侧同一函数、同一口径。"""
    return _ce.compute_visual_similarity(ref_png, out_png, diff_png,
                                         orig_text=ref_text or "", clone_text=out_text or "")


def region_hints(ref_png: Path, out_png: Path, detail: dict, top: int = 8) -> list:
    """像素差分格 → 文本线索(复用 pixel_calibrator.divergent_cells, 同 AI 视觉修复)。"""
    cells = []
    with suppress(Exception):
        cells = pixel_calibrator.divergent_cells(ref_png, out_png) or []
    lines = []
    ratio = (detail or {}).get("height_ratio")
    if isinstance(ratio, (int, float)) and ratio < 0.95:
        lines.append(f"整页高度比 {ratio:.0%}: 存在整块区域缺失/多余")
    for cx, cy, rgb, dist in cells[:top]:
        r, g, b = (int(v) for v in rgb)
        lines.append(f"横 {cx:.0%}/纵 {cy:.0%}: 参考图该区域为 rgb({r},{g},{b}), 产物色差 {dist:.0f}")
    return lines


def description_self_score(description: str, out_text: str, uir_stats: dict) -> dict:
    """无参考图时的退化评分: 描述关键词 F2 覆盖 + 结构自洽(节点数/角色多样性)。

    口径与像素评分不同, 报分必须单列(DESIGN.md §9.6)。"""
    n_a, n_b, inter = _ce._token_counts(_content_terms(description or ""), out_text or "")
    if n_a > 0:
        _f, recall, precision = _ce._f_from_counts(n_a, n_b, inter)
        text = recall          # 自评只问「描述里的内容词是否都出现在页面上」, 页面多出的版式文案不扣分
    else:
        text = recall = precision = 1.0
    nodes = int(uir_stats.get("nodes") or 0)
    roles = len(uir_stats.get("roles") or {})
    structure = min(1.0, nodes / 12.0) * 0.5 + min(1.0, roles / 6.0) * 0.5   # 12 节点 / 6 种角色视为完整页
    score = 100.0 * (0.5 * text + 0.5 * structure)
    return {"score": round(score, 1), "text": round(text, 3), "text_recall": round(recall, 3),
            "text_precision": round(precision, 3), "structure": round(structure, 3),
            "scoring": "desc-self-v1", "warnings": []}
