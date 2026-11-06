"""确定性版面分析 v2: 截图 → 绝对定位 UIR(原分辨率、底色分层、边框/圆角、文字行、图片回填)。

v1(`vision.segment`)在 512 宽缩略图上做 XY 切分, 叶块底色取缩略图中位色: 字形被模糊成灰团后,
标题/正文整块变成灰色实心条(回环用例结构分 0.4), 1px 边框在缩略图里消失, 深度上限 4 让卡片内容挤成一块。
v2 全部在原分辨率上做, 规则都是通用的版面先验, 不看站点:

1. **递归 XY 切分, 先行后列**: 「沟槽」= 逐通道极差 ≤ TOL 的整行/整列连续段(任意颜色)。行沟槽 1px 即可切
   (文字行之间总有空行); 列沟槽对单行高度的区块要求 ≥ 0.75×行高, 否则会在字间/词间把一行字切碎。
   与区块底色不同的沟槽段(分割线、色带、边框外的细线)原样发为色块。
2. **底色分层**: 区块底色 = 众数色(8 级量化取最多的桶再求精确均值, 不受文字笔画影响); 与父级不同才绘制。
3. **边框/圆角**: 区块四边外圈各自近乎纯色、彼此同色且异于内部底色 → 带边框的容器(1~2px), 圆角半径
   = 顶边从角点起到首个边框色像素的偏移; 之后在边框内侧继续切分(卡片内的多行文字不再挤成一块)。
4. **叶块**: 前景 = 与底色最大通道差 > FG_DIFF 的像素; 按前景行带 × 带内 ≥0.75 行高的空隙切成段, 逐段分类:
   细线(≤3px)→ 色块; 色彩丰富(量化色桶多)→ 从输入截图裁切回填为 image(用户自己的截图内容, 面积封顶);
   其余 → 文字行: 字号 ≈ 墨迹高 / 0.98(Chromium 实测), 粗细由横向笔画游程估计, 颜色取最深的那部分前景像素(抗锯齿边缘
   会把均值拉浅), 文本用确定性字形代理, nowrap + 定宽裁切, 保证墨迹覆盖到原行尾。

产出形状与 v1 相同(root.children = 平铺的区块, 叶块的 text 子节点 = 文字行), 骨架填充(merge_fill)可直接复用。
"""
from pathlib import Path

import numpy as np
from PIL import Image

from core.uir import settings
from core.uir.model import UIR, Node, Tokens

TOL = 10            # 色差容差(同色判定)
SMOOTH = 3          # 平滑线: 相邻像素逐通道差上限
GRAD_SPAN = 10      # 平滑线: 整线逐通道极差上限(缓变渐变)
FG_DIFF = 36        # 前景: 与区块底色的最大通道差
MAX_H = 16000       # 超长截图只分析前 MAX_H 像素(评分覆盖区之外不浪费)
_FILL_CJK = "界面重建统一表示组件设计令牌内容交互渲染评分结构边缘文本通用算法生成布局色板字体间距的了是在和有我们这个上来到时大地为子中你说生国年着就那要下以会可也出对能而于"
_FILL_LAT = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore"


def _hex(rgb) -> str:
    r, g, b = (int(max(0, min(255, round(float(v))))) for v in list(rgb)[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def _filler(n_ch: int, seed: int, latin: bool) -> str:
    seed = int(seed) % 9973
    if latin:
        words = _FILL_LAT.split()
        out, i = [], 0
        while len(" ".join(out)) < n_ch:
            out.append(words[(seed + i * 3) % len(words)])
            i += 1
        return " ".join(out)[:n_ch]
    return "".join(_FILL_CJK[(seed + i * 7) % len(_FILL_CJK)] for i in range(n_ch))


def mode_color(region: np.ndarray):
    """众数色: 8 级量化后取最多的桶, 返回 (该桶像素精确均值, 占比)。"""
    flat = region.reshape(-1, 3)
    if flat.shape[0] > 400000:
        flat = flat[:: flat.shape[0] // 400000 + 1]
    q = (flat >> 3).astype(np.int32)
    key = (q[:, 0] << 10) | (q[:, 1] << 5) | q[:, 2]
    cnt = np.bincount(key, minlength=32768)
    k = int(cnt.argmax())
    sel = flat[key == k]
    return sel.astype(np.float32).mean(axis=0), float(cnt[k]) / max(1, key.size)


def _cdiff(a, b) -> float:
    return float(np.abs(np.asarray(a, dtype=np.float32) - np.asarray(b, dtype=np.float32)).max())


def _uniform(region: np.ndarray, axis: int):
    """axis=0: 每一行是否「平滑」; axis=1: 每一列。返回 (mask, 线均色, 线首色, 线尾色)。

    平滑 = 相邻像素逐通道差 ≤ SMOOTH 且整线极差 ≤ GRAD_SPAN: 纯色线与缓变渐变线都算沟槽
    (渐变底的页面若只认纯色, 整页切不开, 按钮/卡片被当成彩色碎片裁切); 文字/图片线有陡变, 不算。"""
    other = 1 - axis
    r16 = region.astype(np.int16)
    step = np.abs(np.diff(r16, axis=other)).max(axis=other) if region.shape[other] > 1 else np.zeros((region.shape[axis], 3), np.int16)
    rng = r16.max(axis=other) - r16.min(axis=other)
    mask = (rng <= TOL).all(axis=1) | ((step <= SMOOTH).all(axis=1) & (rng <= GRAD_SPAN).all(axis=1))
    color = region.mean(axis=other)
    if axis == 0:
        first, last = region[:, 0].astype(np.float32), region[:, -1].astype(np.float32)
    else:
        first, last = region[0].astype(np.float32), region[-1].astype(np.float32)
    return mask, color, first, last


def _runs(mask: np.ndarray, color: np.ndarray, min_run: int, first=None, last=None) -> list:
    """平滑线的连续段(相邻线色差 ≤ TOL, 允许缓慢漂移) → [(a, b, 均色, 首端色, 尾端色)], 长度 ≥ min_run。"""
    out, n, i = [], len(mask), 0
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return out
    while i < idx.size:
        a = int(idx[i])
        j = i
        while (j + 1 < idx.size and idx[j + 1] == idx[j] + 1
               and np.abs(color[idx[j + 1]] - color[a]).max() <= TOL):
            j += 1
        b = int(idx[j]) + 1
        if b - a >= min_run or a == 0 or b == n:
            f = first[a:b].mean(axis=0) if first is not None else None
            l = last[a:b].mean(axis=0) if last is not None else None
            out.append((a, b, color[a:b].mean(axis=0), f, l))
        i = j + 1
    return out


def _pieces(size: int, runs: list) -> list:
    cuts, pos = [], 0
    for a, b, *_ in runs:
        if a > pos:
            cuts.append((pos, a))
        pos = max(pos, b)
    if pos < size:
        cuts.append((pos, size))
    return [(a, b) for a, b in cuts if b - a >= 1]


def _border(region: np.ndarray, bg):
    """外圈边框: 四边(各允许向内 0~3px, 外侧多出来的是投影)中段各自近纯色、彼此同色、异于内部底色
    → (宽, 色, 圆角, (上, 下, 左, 右) 内缩); 否则 None。"""
    h, w = region.shape[:2]
    if h < 14 or w < 14:
        return None

    def side(k, which):
        if which == 0:
            line = region[k, w // 6: w - w // 6]
        elif which == 1:
            line = region[h - 1 - k, w // 6: w - w // 6]
        elif which == 2:
            line = region[h // 6: h - h // 6, k]
        else:
            line = region[h // 6: h - h // 6, w - 1 - k]
        rng = line.max(axis=0).astype(np.int16) - line.min(axis=0).astype(np.int16)
        if (rng > TOL).any():
            return None
        c = line.astype(np.float32).mean(axis=0)
        return c if _cdiff(c, bg) > 6 else None

    cands = [[(k, c) for k in range(4) for c in [side(k, which)] if c is not None] for which in range(4)]
    if not all(cands):
        return None
    found = None
    for _, ref in sorted((kc for cs in cands for kc in cs), key=lambda kc: -_cdiff(kc[1], bg)):
        pick = [next(((k, c) for k, c in cs if _cdiff(c, ref) <= TOL), None) for cs in cands]
        if all(pick):
            found = pick
            break
    if found is None:
        return None
    col = np.mean([c for _, c in found], axis=0)
    t, b_, l, r = (k for k, _ in found)
    bw = 1
    inner = region[t + 1, w // 6: w - w // 6].astype(np.float32).mean(axis=0)
    if _cdiff(inner, col) <= TOL and _cdiff(inner, bg) > 6:
        bw = 2
    top = region[t, l: l + min(w // 2, 40)].astype(np.float32)
    near = np.flatnonzero(np.abs(top - col).max(axis=1) <= TOL)
    radius = int(near[0]) if near.size else 0
    return bw, col, min(radius, h // 2, w // 2), (t, b_, l, r)


def _corner_radius(region: np.ndarray, bg, parent_bg) -> int:
    """无边框的色块: 角点是父底色、顶边中点是本底色 → 圆角半径 = 顶边首个本底色像素偏移。"""
    h, w = region.shape[:2]
    if h < 8 or w < 8 or parent_bg is None:
        return 0
    c = region[0, 0].astype(np.float32)
    if _cdiff(c, bg) <= TOL or _cdiff(c, parent_bg) > 2 * TOL:
        return 0
    top = region[0, : min(w // 2, 40)].astype(np.float32)
    near = np.flatnonzero(np.abs(top - bg).max(axis=1) <= TOL)
    return min(int(near[0]) if near.size else 0, h // 2, w // 2)


def _color_bins(px: np.ndarray) -> int:
    if px.shape[0] < 64:
        return 0
    q = (px // 32).astype(np.int32)
    return int(np.unique(q[:, 0] * 64 + q[:, 1] * 8 + q[:, 2]).size)


def _colorful(px: np.ndarray, text_like: bool = False) -> bool:
    """前景像素是否像图片/彩色图标: 32 级量化色桶数多, 且不是单一色相的抗锯齿灰阶。

    text_like(扁长、行高级的段): 双色文字(黑+红标题)的抗锯齿也能凑出十几个色桶, 门槛抬高,
    避免把文字行当图片裁切。"""
    bins = _color_bins(px)
    if text_like:
        return bins >= 28
    chroma = (px.max(axis=1).astype(np.int16) - px.min(axis=1).astype(np.int16))
    return bins >= 16 or (bins >= 9 and float(np.percentile(chroma, 90)) > 60)


def _plate(R: np.ndarray, bg, y0, y1, x0, x1):
    """文字段外侧 3px 一圈若几乎都是同一种异于底色的颜色 → 沿该色向外扩到边, 得到按钮/标签底板。

    返回 (y0, y1, x0, x1, 色, 圆角) 或 None。深底上的浅按钮、白底上的灰标签、导航里的高亮项都是这一形态;
    底板与底色差异常小于前景阈值, 叶块前景检测看不见它。"""
    h, w = R.shape[:2]
    m = 3
    if y0 - m < 0 or x0 - m < 0 or y1 + m > h or x1 + m > w:
        return None
    ring = np.concatenate([R[y0 - m, x0 - m:x1 + m], R[y1 + m - 1, x0 - m:x1 + m],
                           R[y0 - m:y1 + m, x0 - m], R[y0 - m:y1 + m, x1 + m - 1]]).astype(np.float32)
    c = np.median(ring, axis=0)
    close = np.abs(ring - c).max(axis=1) <= TOL
    if close.mean() < 0.85 or _cdiff(c, bg) <= 4:
        return None

    def same(px):
        return float(np.abs(px.astype(np.float32) - c).max()) <= TOL

    my, mx = (y0 + y1) // 2, x0 - m
    l = x0 - m
    while l - 1 >= 0 and same(R[my, l - 1]):
        l -= 1
    r = x1 + m
    while r < w and same(R[my, r]):
        r += 1
    t = y0 - m
    while t - 1 >= 0 and same(R[t - 1, mx]):
        t -= 1
    b = y1 + m
    while b < h and same(R[b, mx]):
        b += 1
    if (b - t) > 4 * (y1 - y0) + 24 or (r - l) > 3 * (x1 - x0) + 80:
        return None   # 扩出去是大块底色而不是按钮
    rad = 0
    row = R[t, l:min(r, l + 24)].astype(np.float32)
    near = np.flatnonzero(np.abs(row - c).max(axis=1) <= TOL)
    if near.size:
        rad = int(near[0])
    return t, b, l, r, c, min(rad, (b - t) // 2)


def _split_mixed(fgb: np.ndarray, a: int, b: int) -> list:
    """段内按墨迹高度再分组: 头像/图标(高)紧挨着一行小字(矮)时间距常小于行高, 不拆就会把整段当成
    一行巨号字。列空隙切出字块, 相邻字块高度比 > 2.2 另起一组(中文字高 vs 拉丁 x 高约 2 倍, 不拆); 矮于 6px 的(标点/点号)跟随当前组。"""
    sub = fgb[:, a:b]
    cols = sub.any(axis=0)
    blobs, i, n = [], 0, cols.size
    while i < n:
        if not cols[i]:
            i += 1
            continue
        j = i
        while j < n and cols[j]:
            j += 1
        rows = np.flatnonzero(sub[:, i:j].any(axis=1))
        blobs.append((i, j, int(rows[-1] - rows[0] + 1)))
        i = j
    if len(blobs) <= 1:
        return [(a, b)]
    groups = [[blobs[0]]]
    for bl in blobs[1:]:
        gmax = max(x[2] for x in groups[-1])
        h = bl[2]
        if h >= 6 and (h > 2.2 * gmax or h * 2.2 < gmax):
            groups.append([bl])
        else:
            groups[-1].append(bl)
    return [(a + g[0][0], a + g[-1][1]) for g in groups]


def _bg_plane(R: np.ndarray, bg) -> np.ndarray:
    """叶块底色模型: 外圈像素拟合 a + b·x + c·y(逐通道); 外圈本就近纯色时直接用众数色。"""
    h, w = R.shape[:2]
    ring = np.concatenate([R[0], R[-1], R[:, 0], R[:, -1]]).astype(np.float32)
    if h < 4 or w < 4 or float((ring.max(axis=0) - ring.min(axis=0)).max()) <= TOL:
        return np.broadcast_to(np.asarray(bg, np.float32), R.shape)
    ys = np.concatenate([np.zeros(w), np.full(w, h - 1), np.arange(h), np.arange(h)])
    xs = np.concatenate([np.arange(w), np.arange(w), np.zeros(h), np.full(h, w - 1)])
    # 只拿接近众数色的外圈点拟合(外圈压到文字/图片的点不算)
    keep = np.abs(ring - np.asarray(bg, np.float32)).max(axis=1) <= GRAD_SPAN // 2
    if keep.sum() < 8:
        return np.broadcast_to(np.asarray(bg, np.float32), R.shape)
    M = np.stack([np.ones(int(keep.sum())), xs[keep], ys[keep]], axis=1)
    coef, *_ = np.linalg.lstsq(M, ring[keep], rcond=None)
    yy, xx = np.mgrid[0:h, 0:w]
    return (coef[0][None, None, :] + xx[..., None] * coef[1][None, None, :] + yy[..., None] * coef[2][None, None, :])


class _Seg:
    def __init__(self, arr: np.ndarray, im: Image.Image, asset_dir, max_nodes: int,
                 pic_cap: float = 0.6, crop_cap: float = 0.35):
        self.A, self.im = arr, im
        self.H, self.W = arr.shape[:2]
        self.page_area = float(self.H * self.W)
        self.asset_dir = Path(asset_dir) if asset_dir else None
        self.blocks, self.budget = [], max_nodes
        self.pics, self.pic_area = 0, 0.0
        self.pic_cap, self.crop_cap = pic_cap, crop_cap
        self.styles = {}

    # ------------------------------------------------------------------ 发射
    def _box(self, y0, x0, h, w, bg=None, border=None, radius=0, children=None):
        box = {"position": {"top": int(y0), "left": int(x0)}, "w": int(w), "h": int(h)}
        if bg is not None:
            box["bg"] = bg if isinstance(bg, str) else _hex(bg)
        if border is not None:
            box["border"] = {"w": int(border[0]), "color": _hex(border[1])}
        if radius:
            box["radius"] = int(radius)
        n = Node(role="box", layout={"mode": "absolute"}, box=box, children=children or [])
        self.blocks.append(n)
        self.budget -= 1 + len(children or [])
        return n

    def _crop(self, y0, x0, h, w) -> Node:
        name = f"region_{self.pics:03d}.png"
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self.im.crop((x0, y0, x0 + w, y0 + h)).save(self.asset_dir / name, format="PNG")
        self.pics += 1
        self.pic_area += h * w / self.page_area
        return Node(role="image", layout={"mode": "absolute"}, content={"src": f"static/img/{name}", "alt": "区域"},
                    box={"position": {"top": int(y0), "left": int(x0)}, "w": int(w), "h": int(h)})

    def _can_crop(self, h, w) -> bool:
        a = h * w / self.page_area
        return (self.asset_dir is not None and self.pic_cap > 0 and a <= self.crop_cap and self.pic_area + a <= self.pic_cap
                and self.pics < 160)

    def _text_style(self, size: int, weight: int, line: int) -> str:
        key = f"ts{size}w{weight // 100}l{line}"
        self.styles[key] = {"size": size, "weight": weight, "line": f"{line}px"}
        return key

    # ------------------------------------------------------------------ 递归
    def rec(self, y0, y1, x0, x1, parent_bg, depth=0):
        if self.budget <= 0 or y1 - y0 < 1 or x1 - x0 < 1:
            return
        R = self.A[y0:y1, x0:x1]
        h, w = R.shape[:2]
        bg, _ = mode_color(R)
        paint = parent_bg is None or _cdiff(bg, parent_bg) > 3
        if h >= 14 and w >= 14 and depth < 60:
            bd = _border(R, bg)
            if bd is not None:
                bw, (t, b_, l, r) = bd[0], bd[3]
                if paint and (t or b_ or l or r):   # 边框外侧的投影/留白: 先铺区块底色
                    self._box(y0, x0, h, w, bg=bg)
                self._box(y0 + t, x0 + l, h - t - b_, w - l - r, bg=bg, border=bd[:2], radius=bd[2])
                inset = bw + max(0, bd[2] // 3)
                self.rec(y0 + t + inset, y1 - b_ - inset, x0 + l + inset, x1 - r - inset, bg, depth + 1)
                return
        # 大块彩色区(照片/插画/渐变横幅): 整块回填, 不再切碎
        if h >= 48 and w >= 48 and self._can_crop(h, w):
            fg = np.abs(R.astype(np.int16) - bg.astype(np.int16)).max(axis=2) > FG_DIFF
            if fg.mean() > 0.85 and _colorful(R[fg][:: max(1, int(fg.sum()) // 20000 + 1)]):
                self.blocks.append(self._crop(y0, x0, h, w))
                self.budget -= 1
                return
        # 行切分(1px 沟槽即可)
        mask, color, c0, c1 = _uniform(R, 0)
        runs = _runs(mask, color, 1, c0, c1)
        pieces = _pieces(h, runs)
        if pieces and (len(pieces) > 1 or pieces[0] != (0, h)):
            if paint:
                self._box(y0, x0, h, w, bg=bg, radius=_corner_radius(R, bg, parent_bg))
            self._rules(runs, bg, axis=0, y0=y0, x0=x0, h=h, w=w)
            for a, b in pieces:
                self.rec(y0 + a, y0 + b, x0, x1, bg, depth + 1)
            return
        if not pieces:   # 整块纯色
            if paint:
                self._box(y0, x0, h, w, bg=bg, radius=_corner_radius(R, bg, parent_bg))
            return
        # 列切分: 单行高度的区块要求宽沟槽, 否则会在字间切碎
        min_run = max(6, int(round(0.75 * h))) if h <= 80 else 8
        mask, color, c0, c1 = _uniform(R, 1)
        runs = _runs(mask, color, min_run, c0, c1)
        pieces = _pieces(w, runs)
        if pieces and (len(pieces) > 1 or pieces[0] != (0, w)):
            if paint:
                self._box(y0, x0, h, w, bg=bg, radius=_corner_radius(R, bg, parent_bg))
            self._rules(runs, bg, axis=1, y0=y0, x0=x0, h=h, w=w)
            for a, b in pieces:
                self.rec(y0, y1, x0 + a, x0 + b, bg, depth + 1)
            return
        self.leaf(y0, x0, R, bg if paint else None, bg, _corner_radius(R, bg, parent_bg) if paint else 0)

    def _rules(self, runs, bg, axis, y0, x0, h, w):
        """异于区块底色的沟槽段发为色块; 段内首尾端色差明显 → 线性渐变(行段左→右, 列段上→下)。"""
        for a, b, col, f, l in runs:
            grad = f is not None and _cdiff(f, l) > 6
            if _cdiff(col, bg) <= 3 and not grad:
                continue
            fill = (f"linear-gradient({'90deg' if axis == 0 else '180deg'},{_hex(f)},{_hex(l)})" if grad else col)
            if axis == 0:
                self._box(y0 + a, x0, b - a, w, bg=fill)
            else:
                self._box(y0, x0 + a, h, b - a, bg=fill)

    # ------------------------------------------------------------------ 叶块
    def leaf(self, y0, x0, R, paint_bg, bg, radius):
        h, w = R.shape[:2]
        diff = np.abs(R.astype(np.float32) - _bg_plane(R, bg)).max(axis=2)
        fg = diff > FG_DIFF
        kids = []
        rows = np.flatnonzero(fg.any(axis=1))
        if rows.size:
            bands, s = [], rows[0]
            for i in range(1, rows.size + 1):
                if i == rows.size or rows[i] > rows[i - 1] + 1:
                    bands.append((int(s), int(rows[i - 1]) + 1))
                    if i < rows.size:
                        s = rows[i]
            for by0, by1 in bands:
                bh = by1 - by0
                cols = np.flatnonzero(fg[by0:by1].any(axis=0))
                gap = max(4, int(round(0.75 * bh)))
                segs, s = [], cols[0]
                for i in range(1, cols.size + 1):
                    if i == cols.size or cols[i] > cols[i - 1] + gap:
                        segs.append((int(s), int(cols[i - 1]) + 1))
                        if i < cols.size:
                            s = cols[i]
                segs = [p for a, b in segs for p in _split_mixed(fg[by0:by1], a, b)]
                for sx0, sx1 in segs:
                    k = self._segment(R, bg, fg[by0:by1, sx0:sx1], diff[by0:by1, sx0:sx1], by0, by1, sx0, sx1, y0, x0)
                    if k is not None:
                        kids.append(k)
                    if self.budget - len(kids) <= 2:
                        break
        if paint_bg is None and not kids:
            return
        self._box(y0, x0, h, w, bg=paint_bg, radius=radius, children=kids)

    def _segment(self, R, bg, fg, diff, by0, by1, sx0, sx1, y0, x0):
        sub = fg
        ys = np.flatnonzero(sub.any(axis=1))
        sy0, sy1 = by0 + int(ys[0]), by0 + int(ys[-1]) + 1
        bh, sw = sy1 - sy0, sx1 - sx0
        px = R[by0:by1, sx0:sx1][sub]
        if bh <= 3 or sw <= 3:     # 细线/点
            col = np.median(px, axis=0)
            return Node(role="box", layout={"mode": "absolute"},
                        box={"position": {"top": sy0, "left": sx0}, "w": sw, "h": bh, "bg": _hex(col)})
        colorful = _colorful(px, text_like=(bh <= 48 and sw >= 2.5 * bh))
        if (colorful or bh > 120) and self._can_crop(bh, sw):
            pad = 1
            ty0, tx0 = max(0, sy0 - pad), max(0, sx0 - pad)
            ty1, tx1 = min(R.shape[0], sy1 + pad), min(R.shape[1], sx1 + pad)
            n = self._crop(y0 + ty0, x0 + tx0, ty1 - ty0, tx1 - tx0)
            n.box["position"] = {"top": ty0, "left": tx0}
            return n
        # 文字行(若被一块异于叶块底色的实心矩形包着 → 先发按钮/标签底板)
        plate = _plate(R, bg, sy0, sy1, sx0, sx1)
        size = max(8, int(round(bh / 0.98)))   # 实测 Chromium 系统字体: 墨迹高 ≈ 0.92~1.07 × 字号
        strong = diff[sub]
        thr = np.percentile(strong, 60)
        col = np.median(px[strong >= thr], axis=0)
        # 横向笔画游程: 粗体笔画更宽
        runs_px = int(sub.sum())
        starts = int((sub[:, 1:] & ~sub[:, :-1]).sum() + sub[:, 0].sum())
        stroke = runs_px / max(1, starts)
        weight = 700 if stroke >= max(2.6, 0.13 * size) else 400
        pad = max(1, int(round(0.14 * size)))
        line = bh + 2 * pad
        # 拉丁/中文判定: 中文字形近方块, 列空隙少; 拉丁字母间 1px 空隙多
        colmask = sub.any(axis=0)
        gaps = float((~colmask).mean())
        latin = gaps > 0.22 and bh < 0.9 * size + 2
        n_ch = int(sw / (0.5 * size)) + 3 if latin else int(sw / size) + 2
        ref = self._text_style(size, weight, line)
        node = Node(role="text", layout={"mode": "absolute"}, text_ref=ref,
                    content={"text": _filler(n_ch, sx0 * 7 + sy0, latin)},
                    box={"position": {"top": sy0 - pad, "left": sx0}, "w": sw + 1, "h": line,
                         "color": _hex(col), "overflow": "hidden"})
        if plate is None:
            return node
        py0, py1, px0, px1, pcol, prad = plate
        node.box["position"] = {"top": sy0 - pad - py0, "left": sx0 - px0}
        return Node(role="box", layout={"mode": "absolute"},
                    box={"position": {"top": py0, "left": px0}, "w": px1 - px0, "h": py1 - py0,
                         "bg": _hex(pcol), "radius": prad}, children=[node])


def segment_v2(image_path, asset_dir=None, max_nodes: int = None) -> UIR:
    max_nodes = int(max_nodes or max(settings.SEG_MAX_NODES, 3000))
    with Image.open(image_path) as im:
        im = im.convert("RGB")
        W, H0 = im.size
        H = min(H0, MAX_H)
        if H < H0:
            im = im.crop((0, 0, W, H))
        im.load()
        arr = np.asarray(im).astype(np.uint8)
    seg = _Seg(arr, im, asset_dir, max_nodes, pic_cap=settings.SEG_PIC_CAP)
    page_bg, _ = mode_color(arr)
    seg.rec(0, H, 0, W, page_bg)
    tokens = Tokens(color={"c0": _hex(page_bg)}, space={"s0": 0, "s1": 4, "s2": 8, "s4": 16},
                    text_style={"t0": {"size": 14, "weight": 400, "line": 1.4}})
    tokens.text_style.update(seg.styles)
    root = Node(role="box", layout={"mode": "stack"},
                box={"w": W, "h": H0, "bg": _hex(page_bg), "position": {"mode": "relative"}, "overflow": "hidden"},
                children=seg.blocks)
    uir = UIR(root=root, tokens=tokens,
              meta={"source_kind": "vision", "tier": "deterministic", "seg": "v2", "title": "Generated from screenshot",
                    "lang": "zh", "viewport": {"w": W, "h": H0}, "picture_regions": seg.pics,
                    "picture_area": round(seg.pic_area, 3)})
    return uir.normalize()
