"""截图的确定性版面测量: 横向分区(背景色带)、各区块的高度/背景色/内容左右边界、主色。

用途: 截图→代码时把原图量出来的数字作为硬约束交给模型; 每轮产出用同一方法量一遍, 与原图逐区块比较,
差得多的(高度、背景色、内容宽度)写成具体的修复目标。纯图像统计, 与页面内容无关。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

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
    out: list[dict[str, Any]] = []
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
    cards = _cards(a, out)
    cols = _columns(a, W, H)
    # 主色: 全图量化色按面积取前 6
    vals, cnt = np.unique(code[::4, ::4], return_counts=True)
    order = cnt.argsort()[::-1][:6]
    palette = [_hex(((int(vals[i]) >> 16) & 255, (int(vals[i]) >> 8) & 255, int(vals[i]) & 255)) for i in order]
    return {"width": W, "height": H, "bands": out, "palette": palette, "cards": cards, "columns": cols}


def describe(m: dict, cards: bool = False) -> str:
    """给写代码模型看的测量摘要(CSS 像素)。"""
    lines = [f"画面宽 {m['width']}px; 主色(按面积): {', '.join(m['palette'])}"]
    cols = m.get("columns") or []
    if len(cols) > 1:
        lines.append("纵向分界(贯穿整页大部分高度的竖向边线/底色交界, 如侧边栏边线、主内容区左右边缘; 各栏横向范围请按此还原):")
        for c in cols:
            lines.append(f"  x {c['x0']}~{c['x1']} (宽 {c['w']}px), 背景 {c['bg']}")
    lines.append("从上到下的区块(背景色带):")
    for i, b in enumerate(m["bands"], 1):
        span = f"内容横向 {b['x0']}~{b['x1']}px" if b["x1"] > b["x0"] else "无明显内容"
        lines.append(f"  {i}. y {b['y0']}~{b['y1']} (高 {b['h']}px), 背景 {b['bg']}, {span}")
    card_list = (m.get("cards") or []) if cards else []
    if card_list:
        lines.append("卡片/面板(与所在区块背景色不同的矩形块; x,y 为左上角):")
        for i, c in enumerate(card_list, 1):
            lines.append(f"  卡{i}: x {c['x']} y {c['y']}, 宽 {c['w']} 高 {c['h']}, 背景 {c['bg']}")
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
    rc, oc0 = ref.get("columns") or [], out.get("columns") or []
    if len(rc) > 1 and len(probs) < limit:
        if len(oc0) != len(rc):
            probs.append(f"纵向分栏: 原图 {len(rc)} 栏(" + ", ".join(f"宽 {c['w']}px" for c in rc) + f"), 产出 {len(oc0)} 栏")
        else:
            for i, (r, o) in enumerate(zip(rc, oc0), 1):
                if abs(r["w"] - o["w"]) > max(16, r["w"] * 0.1):
                    probs.append(f"第 {i} 栏宽度: 原图 {r['w']}px(x {r['x0']}~{r['x1']}), 产出 {o['w']}px")
                    break
    # 卡片: 原图每张较大的卡片, 在产出里找重叠最多的一张, 位置/尺寸差得多就报
    oc = out.get("cards") or []
    for i, r in enumerate((ref.get("cards") or [])[:12], 1):
        if len(probs) >= limit:
            break
        if r["w"] * r["h"] < 120 * 80:
            continue
        best, bi = None, 0.0
        for o in oc:
            ix = max(0, min(r["x"] + r["w"], o["x"] + o["w"]) - max(r["x"], o["x"]))
            iy = max(0, min(r["y"] + r["h"], o["y"] + o["h"]) - max(r["y"], o["y"]))
            u = r["w"] * r["h"] + o["w"] * o["h"] - ix * iy
            iou = ix * iy / u if u else 0
            if iou > bi:
                best, bi = o, iou
        if best is None or bi < 0.15:
            probs.append(f"卡{i}(原图 x {r['x']} y {r['y']} 宽 {r['w']} 高 {r['h']}, 背景 {r['bg']}) 在产出的对应位置找不到")
        elif bi < 0.6:
            probs.append(f"卡{i} 位置/尺寸: 原图 x {r['x']} y {r['y']} 宽 {r['w']} 高 {r['h']}, "
                         f"产出 x {best['x']} y {best['y']} 宽 {best['w']} 高 {best['h']}")
    return probs[:limit]


def _rgb(h: str):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _cards(a, bands, k: int = 3, max_cards: int = 24) -> list:
    """卡片/面板: 被边缘(描边、阴影、底色变化)围起来、外形接近矩形的区域(缩小 k 倍计算)。
    卡片常常只靠 1px 描边或阴影和背景区分, 底色几乎一样, 所以按「边缘围出的区域」找而不是按颜色找。
    返回 [{x,y,w,h,bg}](原图像素), 从上到下、从左到右排序。"""
    import numpy as np
    from collections import deque
    g = a[::k, ::k].astype(np.int32)
    gray = (g[..., 0] * 299 + g[..., 1] * 587 + g[..., 2] * 114) // 1000
    H2, W2 = gray.shape
    edge = np.zeros_like(gray, dtype=bool)
    edge[:, 1:] |= np.abs(gray[:, 1:] - gray[:, :-1]) > 6
    edge[1:, :] |= np.abs(gray[1:, :] - gray[:-1, :]) > 6
    free = ~edge
    seen = np.zeros_like(free)
    out: list[dict[str, Any]] = []
    min_w, min_h = 80 // k, 50 // k
    for y in range(0, H2, 3):
        for x in range(0, W2, 3):
            if not free[y, x] or seen[y, x]:
                continue
            q = deque([(y, x)])
            seen[y, x] = True
            n, y0, y1, x0, x1 = 0, y, y, x, x
            while q:
                cy, cx = q.popleft(); n += 1
                if cy < y0: y0 = cy
                if cy > y1: y1 = cy
                if cx < x0: x0 = cx
                if cx > x1: x1 = cx
                for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                    if 0 <= ny < H2 and 0 <= nx < W2 and free[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        q.append((ny, nx))
            bw, bh = x1 - x0 + 1, y1 - y0 + 1
            if bw < min_w or bh < min_h or bw > W2 * 0.92 or bh > H2 * 0.9:
                continue                          # 太小(文字间隙)或几乎整屏(区块背景本身)
            # 外形接近矩形: 外接框四条边上多数像素属于这块区域; 内部被文字/图标挖掉一部分没关系
            m = np.zeros((bh, bw), dtype=bool)
            sub = seen[y0:y1 + 1, x0:x1 + 1] & free[y0:y1 + 1, x0:x1 + 1]
            m |= sub
            rim = np.concatenate([m[1, :], m[-2, :], m[:, 1], m[:, -2]]) if bh > 3 and bw > 3 else np.array([0])
            if rim.mean() < 0.75 or n < bw * bh * 0.45:
                continue
            col = g[y0 + bh // 2, x0 + 2]
            out.append({"x": x0 * k, "y": y0 * k, "w": bw * k, "h": bh * k, "bg": "#%02x%02x%02x" % tuple(int(c) for c in col)})
    out.sort(key=lambda c: (c["y"] // 20, c["x"]))
    return out[:max_cards]

def _columns(a, W: int, H: int) -> list:
    """纵向分栏: 找贯穿页面大部分高度的竖向分界(左右两侧颜色在 ≥70% 的行里都不同, 例如侧边栏的边线或底色交界),
    按分界切成几栏。典型是后台侧边栏、文档站左目录。没有分栏返回 []。"""
    import numpy as np
    step = max(1, H // 400)
    sub = a[::step].astype(np.int32)
    d = np.abs(sub[:, 2:] - sub[:, :-2]).max(axis=2) > 3          # x 与 x+2 之间颜色不同
    frac = d.mean(axis=0)
    xs = [x + 1 for x in range(len(frac)) if frac[x] >= 0.7]
    edges = []
    for x in xs:                                                    # 相邻几像素算同一条分界
        if edges and x - edges[-1] <= 4:
            continue
        edges.append(x)
    edges = [x for x in edges if 0.06 * W <= x <= 0.94 * W]
    if not edges or len(edges) > 3:
        return []
    cuts = [0] + edges + [W]
    out = []
    for x0, x1 in zip(cuts, cuts[1:]):
        if x1 - x0 < 40:
            continue
        mid = sub[:, x0 + 2:x1 - 2].reshape(-1, 3)
        bg = np.median(mid, axis=0)
        out.append({"x0": x0, "x1": x1, "w": x1 - x0, "bg": _hex(bg)})
    if len(out) < 2 or not any(0.08 * W <= c["w"] <= 0.4 * W for c in (out[0], out[-1])):
        return []
    return out
