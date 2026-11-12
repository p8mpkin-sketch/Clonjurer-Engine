"""截图本地 OCR 文字层(确定性, 不依赖模型 Key)。

版面分析 v2 已能从截图里找到每一段文字的位置、字号、颜色, 但不知道写的是什么, 只能放字形代理;
真实文字以前只能靠多模态模型逐块回答(慢、会漏、会编)。本地 OCR 给出带坐标的文字行, 按几何
落进 v2 的文字段:

- 后端: rapidocr_onnxruntime(PP-OCR 中英模型, 跨平台 CPU, 同一输入输出确定); 未安装时整层关闭,
  行为与此前完全一致。可用 CLONER_GEN_OCR=0 关掉做消融。
- 分配: 每个 OCR 行按字符宽度先验(CJK 1 em / 其余 0.55 em / 空格 0.3 em)把行内字符摊到行框横向位置上,
  文字段拿走「字符中心落在自己横向范围内、且行框与自己竖向重叠过半」的字符。一行跨多段(导航、表格)、
  一段跨多行都成立; 没分到字符的段保留字形代理。
- 拟合: 真实文字的自然宽度与原墨迹宽度不一定相同(字体不同), 按同一先验估计自然宽度, 用 letter-spacing
  把差补到原段宽(封顶), 避免整行溢出被裁或明显偏短。
- 置信度低于阈值的行不用(宁可保留代理纹理, 不写错字)。

纯函数 apply_ocr(uir, lines) 可测; read_lines 带内容哈希缓存(质量循环/重跑同图只识别一次)。
"""
import hashlib
import os
import threading
from pathlib import Path

MIN_CONF = float(os.environ.get("CLONER_GEN_OCR_MIN_CONF", "0.5"))
TILE_H = 1600          # 长截图纵向分段识别(检测器内部会按长边缩放, 整张长图细字会丢)
TILE_OV = 120
_LOCK = threading.Lock()
_ENGINE = [None, False]   # (实例, 已尝试加载)
_CACHE = {}


def enabled() -> bool:
    return os.environ.get("CLONER_GEN_OCR", "1") not in ("0", "false", "off") and _engine() is not None


def _engine():
    with _LOCK:
        if not _ENGINE[1]:
            _ENGINE[1] = True
            try:
                from rapidocr_onnxruntime import RapidOCR   # type: ignore
                _ENGINE[0] = RapidOCR()
            except Exception:   # 未安装/模型缺失: 整层关闭
                _ENGINE[0] = None
        return _ENGINE[0]


def _ocr_array(eng, arr):
    with _LOCK:   # onnxruntime 会话本身可并发, 但 RapidOCR 包装对象内部有状态, 串行最稳
        res, _ = eng(arr)
    return res or []


def read_lines(image_path, max_h: int = 16000) -> list:
    """截图 → [{"x0","y0","x1","y1","text","conf"}](原图像素坐标, 按 y、x 排序)。无后端返回 []。"""
    eng = _engine()
    if eng is None:
        return []
    data = Path(image_path).read_bytes()
    key = hashlib.sha1(data).hexdigest()
    if key in _CACHE:
        return _CACHE[key]
    import numpy as np
    from PIL import Image
    with Image.open(image_path) as im:
        im = im.convert("RGB")
        W, H = im.size
        H = min(H, max_h)
        out, y = [], 0
        while y < H:
            y1 = min(H, y + TILE_H)
            arr = np.asarray(im.crop((0, y, W, y1)))
            for box, text, conf in _ocr_array(eng, arr):
                xs = [float(p[0]) for p in box]
                ys = [float(p[1]) + y for p in box]
                cy = (min(ys) + max(ys)) / 2
                # 重叠带里的行只由「中心所在」的那一段收录, 不重复
                if y > 0 and cy < y + TILE_OV / 2:
                    continue
                if y1 < H and cy >= y1 - TILE_OV / 2:
                    continue
                t = str(text).strip()
                if t:
                    out.append({"x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys),
                                "text": t, "conf": float(conf)})
            if y1 >= H:
                break
            y = y1 - TILE_OV
    out.sort(key=lambda r: (round(r["y0"] / 4), r["x0"]))
    if len(_CACHE) > 32:
        _CACHE.clear()
    _CACHE[key] = out
    return out


def _cw(ch: str) -> float:
    """字符宽度先验(em)。"""
    o = ord(ch)
    if ch == " ":
        return 0.3
    if 0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF or 0x3000 <= o <= 0x303F:
        return 1.0
    if ch in "ilI.,:;'|!`":
        return 0.3
    if ch in "mwMW@":
        return 0.85
    if ch.isupper() or ch.isdigit():
        return 0.62
    return 0.52


def natural_width(text: str, size: float) -> float:
    return sum(_cw(c) for c in text) * size


def _char_centers(line: dict) -> list:
    """把行框横向按字符宽度先验摊开 → [(字符, 中心 x)]。"""
    text = line["text"]
    ws = [_cw(c) for c in text]
    tot = sum(ws) or 1.0
    span = line["x1"] - line["x0"]
    out, acc = [], 0.0
    for c, w in zip(text, ws):
        out.append((c, line["x0"] + (acc + w / 2) / tot * span))
        acc += w
    return out


def text_slots(uir) -> list:
    """UIR 中全部文字段 → [(节点, 绝对 x0, y0, x1, y1)]; 绝对坐标 = 祖先 position 累加。"""
    slots = []

    def walk(node, ox, oy, top_level):
        pos = node.box.get("position") if isinstance(node.box.get("position"), dict) else {}
        x = ox + float(pos.get("left") or 0) if not top_level else ox
        y = oy + float(pos.get("top") or 0) if not top_level else oy
        if node.role == "text" and node.box.get("w") is not None and node.box.get("h") is not None:
            slots.append((node, x, y, x + float(node.box["w"]), y + float(node.box["h"])))
        for c in node.children:
            walk(c, x, y, False)

    walk(uir.root, 0.0, 0.0, True)
    return slots


def _font_size(uir, node) -> float:
    ts = uir.tokens.text_style.get(node.text_ref or "") if node.text_ref else None
    try:
        return float((ts or {}).get("size") or 14)
    except (TypeError, ValueError):
        return 14.0


def apply_ocr(uir, lines: list, min_conf: float = None) -> int:
    """把 OCR 行按几何分配进 UIR 的文字段(原位替换代理文本)。返回替换的段数。纯函数(就地改 uir)。"""
    min_conf = MIN_CONF if min_conf is None else float(min_conf)
    lines = [ln for ln in lines if ln.get("conf", 1.0) >= min_conf and ln.get("text")]
    if not lines:
        return 0
    slots = text_slots(uir)
    centers = [_char_centers(ln) for ln in lines]
    # 1) 每个文字段认一条 OCR 行: 竖向重叠过半、横向相交的行里取竖向重叠最多者
    owner = {}
    for si, (node, x0, y0, x1, y1) in enumerate(slots):
        h = max(1.0, y1 - y0)
        best = None
        for li, ln in enumerate(lines):
            ov = min(y1, ln["y1"]) - max(y0, ln["y0"])
            if ov <= 0 or ov < 0.5 * min(h, max(1.0, ln["y1"] - ln["y0"])):
                continue
            if ln["x1"] < x0 - 2 or ln["x0"] > x1 + 2:
                continue
            xo = min(x1, ln["x1"]) - max(x0, ln["x0"])
            if best is None or (ov, xo) > best[0]:
                best = ((ov, xo), li)
        if best is not None:
            owner.setdefault(best[1], []).append(si)
            # 同一行里横向并排的其它 OCR 行(两个相邻按钮/标签被识别成两行, 而版面把它们切成了一段): 也归这一段,
            # 否则第二行的字整行丢失
            bl = lines[best[1]]
            for li, ln in enumerate(lines):
                if li == best[1] or ln["x1"] < x0 - 2 or ln["x0"] > x1 + 2:
                    continue
                if ln["x0"] < bl["x1"] - 2 and ln["x1"] > bl["x0"] + 2:
                    continue   # 与主行横向重叠: 是上下行, 不是并排
                ov = min(y1, ln["y1"]) - max(y0, ln["y0"])
                if ov >= 0.5 * min(h, max(1.0, ln["y1"] - ln["y0"])):
                    owner.setdefault(li, []).append(si)
    # 2) 行内每个字符归「中心落在其横向范围内」的段; 落在段间空隙的字符归最近的段(距离 ≤ 1 行高),
    #    OCR 行框通常比墨迹略宽、字宽先验也有误差, 不归就会丢字
    got = {}
    for li, sis in owner.items():
        lh = max(8.0, lines[li]["y1"] - lines[li]["y0"])
        for c, cx in centers[li]:
            best = None
            for si in sis:
                _, x0, _, x1, _ = slots[si]
                d = 0.0 if x0 <= cx <= x1 else min(abs(cx - x0), abs(cx - x1))
                if best is None or d < best[0]:
                    best = (d, si)
            if best is not None and best[0] <= lh:
                got.setdefault(best[1], []).append((cx, li, c))
    filled = 0
    for si, chars in got.items():
        node, x0, y0, x1, y1 = slots[si]
        chars.sort(key=lambda t: t[0])
        parts, prev = [], None
        for _, li, c in chars:
            if prev is not None and li != prev:
                parts.append(" ")      # 并排的两条 OCR 行之间
            parts.append(c)
            prev = li
        text = "".join(parts).strip()
        if not text:
            continue
        node.content = dict(node.content or {}, text=text)
        node.content["ocr"] = 1   # 标记: 骨架填充不再覆盖(OCR 有坐标, 模型逐块回答没有)
        size = _font_size(uir, node)
        nat = natural_width(text, size)
        n = max(1, len(text))
        ls = max(-0.12 * size, min(0.6 * size, ((x1 - x0) - nat) / n))
        if abs(ls) >= 0.3:
            node.box["letter_spacing"] = round(ls, 2)
        filled += 1
    uir.meta["ocr_filled"] = filled
    uir.meta["ocr_lines"] = len(lines)
    return filled
