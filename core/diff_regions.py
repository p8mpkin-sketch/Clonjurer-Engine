"""差异区域: 原站(原图)截图 vs 克隆(产出)截图 → 一句结论 + 差别集中的区域框。

确定性计算, 不调模型: 两图按同一宽度缩小, 逐像素比较(三通道最大差 > 阈值算「不同」), 按网格聚合,
相邻的不同格子连成一个区域, 取面积最大的若干个。坐标用相对原图的比例(0~1), 前端叠在差异图上画框。
"""
from __future__ import annotations

import json
from pathlib import Path

PIX_THR = 40           # 单像素三通道最大差超过它算「不同」
CELL = 8               # 网格边长(缩小后的像素)
CELL_FRAC = 0.12       # 格子里不同像素占比超过它, 这格算「有差别」
WORK_W = 256           # 比较用的宽度
MAX_REGIONS = 8
NEAR_SAME = 1.0        # 差别面积 < 1% 视为几乎一致


def compute(orig: Path, clone: Path) -> dict:
    import numpy as np
    from PIL import Image
    with Image.open(orig) as a0, Image.open(clone) as b0:
        a0, b0 = a0.convert("RGB"), b0.convert("RGB")
        k = WORK_W / a0.size[0]
        ha = max(1, round(a0.size[1] * k))
        a = np.asarray(a0.resize((WORK_W, ha)), dtype=np.int16)
        hb = max(1, round(b0.size[1] * WORK_W / b0.size[0]))
        b = np.asarray(b0.resize((WORK_W, hb)), dtype=np.int16)
    h = min(ha, hb)
    if h < CELL:
        return {"area_pct": 0.0, "regions": [], "verdict": "截图太小, 无法比较"}
    diff = np.abs(a[:h] - b[:h]).max(axis=2) > PIX_THR
    area = float(diff.mean()) * 100
    # 克隆比原图短/长的部分整块算不同(内容缺失或多出)
    tail = abs(ha - hb) / max(ha, hb) * 100
    gh, gw = h // CELL, WORK_W // CELL
    cells = diff[:gh * CELL, :gw * CELL].reshape(gh, CELL, gw, CELL).mean(axis=(1, 3)) > CELL_FRAC
    # 膨胀一格, 把挨得很近的零散差别连成一片
    grown = cells.copy()
    grown[1:] |= cells[:-1]; grown[:-1] |= cells[1:]; grown[:, 1:] |= cells[:, :-1]; grown[:, :-1] |= cells[:, 1:]
    seen = np.zeros_like(grown, dtype=bool)
    regions = []
    for y in range(gh):
        for x in range(gw):
            if not grown[y, x] or seen[y, x]:
                continue
            stack, ys, xs, hits = [(y, x)], [], [], 0
            seen[y, x] = True
            while stack:
                cy, cx = stack.pop()
                ys.append(cy); xs.append(cx); hits += int(cells[cy, cx])
                for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                    if 0 <= ny < gh and 0 <= nx < gw and grown[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if hits < 2:                       # 一两个格子的零星噪点(抗锯齿、光标)不算
                continue
            y0, y1, x0, x1 = min(ys), max(ys) + 1, min(xs), max(xs) + 1
            regions.append({"x": round(x0 * CELL / WORK_W, 4), "y": round(y0 * CELL / ha, 4),
                            "w": round((x1 - x0) * CELL / WORK_W, 4), "h": round((y1 - y0) * CELL / ha, 4),
                            "weight": hits})
    regions.sort(key=lambda r: -r["weight"])
    regions = regions[:MAX_REGIONS]
    regions.sort(key=lambda r: (r["y"], r["x"]))
    total = area * h / ha + tail
    if total < NEAR_SAME and not regions:
        verdict = "与原图几乎一致"
    elif not regions:
        verdict = f"整体有细微差别(约 {total:.1f}% 面积)"
    else:
        verdict = f"差别集中在 {len(regions)} 处, 约占 {total:.1f}% 面积"
    if tail >= 3:
        verdict += f"; 页面长度相差 {tail:.0f}%"
    return {"area_pct": round(total, 2), "regions": regions, "verdict": verdict}


def for_workspace(ws: Path) -> dict | None:
    """读缓存或计算: 原图 original_preview.png vs 产出 cloned_preview.png。缺图返回 None。"""
    orig = ws / "original_preview.png"
    clone = next((ws / n for n in ("cloned_preview.png", "static_preview.png", "fidelity_preview.png") if (ws / n).is_file()), None)
    if not orig.is_file() or clone is None:
        return None
    key = f"{orig.stat().st_mtime}:{clone.stat().st_mtime}:v1"
    cache = ws / "diff_regions.json"
    try:
        hit = json.loads(cache.read_text(encoding="utf-8"))
        if hit.get("key") == key:
            return hit["data"]
    except (OSError, ValueError, KeyError):
        pass
    data = compute(orig, clone)
    try:
        cache.write_text(json.dumps({"key": key, "data": data}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return data
