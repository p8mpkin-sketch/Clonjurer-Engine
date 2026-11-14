"""截图的确定性版面测量: 横向分区(背景色带)、各区块的高度/背景色/内容左右边界、主色。

用途: 截图→代码时把原图量出来的数字作为硬约束交给模型; 每轮产出用同一方法量一遍, 与原图逐区块比较,
差得多的(高度、背景色、内容宽度)写成具体的修复目标。纯图像统计, 与页面内容无关。
"""
from __future__ import annotations

from pathlib import Path

Q = 16              # 颜色量化步长(抗锯齿/JPEG 噪声不拆色)
ROW_DOM = 0.55      # 一行里占比超过它的颜色算这行的背景
MIN_BAND = 28       # 区块最小高度(px), 更矮的并入相邻区块
MAX_BANDS = 14


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)


def _dist(a, b) -> float:
    return sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)) ** 0.5


def measure(path: Path, max_h: int = 2400) -> dict:
    import numpy as np
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        W, H = im.size
        if H > max_h:
            im = im.crop((0, 0, W, max_h))
            H = max_h
        a = np.asarray(im, dtype=np.int32)
    q = (a // Q) * Q + Q // 2
    code = q[..., 0] * 65536 + q[..., 1] * 256 + q[..., 2]
    # 每行的主色与占比
    row_bg, row_dom = [], []
    for y in range(H):
        vals, cnt = np.unique(code[y], return_counts=True)
        k = int(cnt.argmax())
        c = int(vals[k])
        row_bg.append(((c >> 16) & 255, (c >> 8) & 255, c & 255))
        row_dom.append(cnt[k] / W)
    # 分区: 主色明显变化(且新主色持续)的位置切开
    cuts = [0]
    cur = row_bg[0]
    for y in range(1, H):
        if row_dom[y] >= ROW_DOM and _dist(row_bg[y], cur) > 40:
            ahead = [row_bg[t] for t in range(y, min(H, y + MIN_BAND)) if row_dom[t] >= ROW_DOM]
            if len(ahead) >= MIN_BAND * 0.3 and sum(_dist(c, row_bg[y]) < 30 for c in ahead) >= len(ahead) * 0.7:
                cuts.append(y)
                cur = row_bg[y]
        elif row_dom[y] >= ROW_DOM and _dist(row_bg[y], cur) <= 40:
            cur = row_bg[y]
    cuts.append(H)
    bands = []
    for y0, y1 in zip(cuts, cuts[1:]):
        if bands and y1 - y0 < MIN_BAND:
            bands[-1]["y1"] = y1
            continue
        bands.append({"y0": y0, "y1": y1})
    if len(bands) > 1 and bands[0]["y1"] - bands[0]["y0"] < MIN_BAND:
        bands[1]["y0"] = 0
        bands.pop(0)
    out = []
    for b in bands[:MAX_BANDS]:
        y0, y1 = b["y0"], b["y1"]
        # 区块背景 = 区块里「背景行」(主色占比高的行)的主色众数; 整块都是内容(图片墙)时退回全块众数
        rows = [row_bg[y] for y in range(y0, y1) if row_dom[y] >= ROW_DOM]
        if rows:
            from collections import Counter
            bg = Counter(rows).most_common(1)[0][0]
        else:
            seg = code[y0:y1]
            vals, cnt = np.unique(seg, return_counts=True)
            c = int(vals[cnt.argmax()])
            bg = ((c >> 16) & 255, (c >> 8) & 255, c & 255)
        diff = (np.abs(a[y0:y1] - np.array(bg)).max(axis=2) > 36)
        cols = np.nonzero(diff.mean(axis=0) > 0.02)[0]
        x0, x1 = (int(cols[0]), int(cols[-1]) + 1) if len(cols) else (0, 0)
        out.append({"y0": y0, "y1": y1, "h": y1 - y0, "bg": _hex(bg), "x0": x0, "x1": x1,
                    "ink": round(float(diff.mean()), 3)})
    # 主色: 全图量化色按面积取前 6
    vals, cnt = np.unique(code[::4, ::4], return_counts=True)
    order = cnt.argsort()[::-1][:6]
    palette = [_hex(((int(vals[i]) >> 16) & 255, (int(vals[i]) >> 8) & 255, int(vals[i]) & 255)) for i in order]
    return {"width": W, "height": H, "bands": out, "palette": palette}


def describe(m: dict) -> str:
    """给写代码模型看的测量摘要(CSS 像素)。"""
    lines = [f"画面宽 {m['width']}px; 主色(按面积): {', '.join(m['palette'])}", "从上到下的区块(背景色带):"]
    for i, b in enumerate(m["bands"], 1):
        span = f"内容横向 {b['x0']}~{b['x1']}px" if b["x1"] > b["x0"] else "无明显内容"
        lines.append(f"  {i}. y {b['y0']}~{b['y1']} (高 {b['h']}px), 背景 {b['bg']}, {span}")
    return "\n".join(lines)


def compare(ref: dict, out: dict, limit: int = 6) -> list:
    """逐区块对比原图与产出, 返回差得多的项(中文, 写进修复目标)。区块按顺序配对, 数量不同单独说明。"""
    probs = []
    rb, ob = ref["bands"], out["bands"]
    if abs(len(rb) - len(ob)) >= 2:
        probs.append(f"区块数量不同: 原图从上到下 {len(rb)} 个背景色带, 产出 {len(ob)} 个(检查是否漏了或多出整块区域)")
    for i, (r, o) in enumerate(zip(rb, ob), 1):
        if len(probs) >= limit:
            break
        if _dist(_rgb(r["bg"]), _rgb(o["bg"])) > 45:
            probs.append(f"第 {i} 区块背景色: 原图 {r['bg']}, 产出 {o['bg']}")
        if abs(r["h"] - o["h"]) > max(24, r["h"] * 0.18):
            probs.append(f"第 {i} 区块高度: 原图 {r['h']}px, 产出 {o['h']}px")
        rw, ow = r["x1"] - r["x0"], o["x1"] - o["x0"]
        if rw > 80 and ow > 0 and abs(rw - ow) > max(40, rw * 0.12):
            probs.append(f"第 {i} 区块内容宽度: 原图 {r['x0']}~{r['x1']}px(宽 {rw}), 产出 {o['x0']}~{o['x1']}px(宽 {ow})")
    return probs[:limit]


def _rgb(h: str):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
