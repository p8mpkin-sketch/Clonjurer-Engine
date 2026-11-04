"""界面截图 → UIR。

Tier 0(确定性, 始终可用): 递归 XY 切分版面分析——
  1) 色板: 量化取主色 → tokens.color;
  2) 切分: 沿背景色均匀的水平/垂直沟槽递归切块(深度/面积受限), 叶块 → 绝对定位 box;
  3) 文本带: 叶块内按行方差投影找出高频细纹理带(文字行), 以其水平跨度与前景色发出
     细长 box, 复现文字行的边缘与块内结构(评分 v3 的 NCC/边缘通道所需);
  产物按参考图原宽渲染, 像素位置对齐, 评分口径与克隆侧一致。
Tier 1(LLM, 有 Key 时): 多模态模型直接产出语义 UIR(真实文案/角色/布局), 校验失败回落 Tier 0;
  质量循环把像素差分线索回喂给下一轮。
"""
from pathlib import Path

import numpy as np
from PIL import Image

from core.uir import llm, settings
from core.uir.model import UIR, Node, Tokens
from core.uir.registry import register_input, GenRequest, BuildContext

WORK_W = 512
_FILL_CJK = "界面重建统一表示组件设计令牌内容交互渲染评分结构边缘文本通用算法生成布局色板字体间距"
_FILL_LAT = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor"


def _filler(n_ch: int, seed: int) -> str:
    """确定性字形纹理代理: 中英混排, 由区域位置播种, 同图两次结果相同。"""
    seed = int(seed) % 9973
    out = []
    i = 0
    while len("".join(out)) < n_ch:
        k = (seed + i * 7) % (len(_FILL_CJK) + len(_FILL_LAT.split()))
        if k < len(_FILL_CJK):
            out.append(_FILL_CJK[k])
        else:
            out.append(" " + _FILL_LAT.split()[k - len(_FILL_CJK)] + " ")
        i += 1
    return "".join(out)[:n_ch]


def _quant(arr: np.ndarray, step: int) -> np.ndarray:
    return (arr // step) * step + step // 2


def palette(img: np.ndarray, k: int) -> list:
    q = _quant(img.reshape(-1, 3).astype(np.int32), settings.SEG_COLOR_QUANT)
    keys = q[:, 0] * 65536 + q[:, 1] * 256 + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    order = np.argsort(-counts)[:k]
    out = []
    for i in order:
        v = int(vals[i])
        out.append(((v >> 16) & 255, (v >> 8) & 255, v & 255, int(counts[i])))
    return out


def _hex(rgb) -> str:
    r, g, b = (int(max(0, min(255, v))) for v in rgb[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def _nearest(tokens_rgb: list, rgb) -> int:
    d = [sum((a - b) ** 2 for a, b in zip(t, rgb)) for t in tokens_rgb]
    return int(np.argmin(d))


def _uniform_lines(region: np.ndarray, axis: int, tol: int):
    """沿 axis 的每条线(行 axis=0 / 列 axis=1)是否近乎纯色: 逐通道极差 ≤ tol。返回 (mask, 线色)。"""
    other = 1 - axis
    mx = region.max(axis=other).astype(np.int16)
    mn = region.min(axis=other).astype(np.int16)
    mask = ((mx - mn) <= tol).all(axis=1)
    color = region.mean(axis=other)
    return mask, color


def _gutter_runs(mask: np.ndarray, color: np.ndarray, min_run: int, tol: int) -> list:
    """均匀线的连续段(段内颜色相近) → [(start, end)], 长度 ≥ min_run。"""
    runs, start = [], None
    n = len(mask)
    for i in range(n + 1):
        ok = i < n and bool(mask[i]) and (start is None or np.abs(color[i] - color[start]).max() <= tol)
        if ok and start is None:
            start = i
        elif not ok and start is not None:
            if i - start >= min_run:
                runs.append((start, i))
            start = i if (i < n and bool(mask[i])) else None
    return runs


def _split(region: np.ndarray, tol: int, min_run: int):
    """沿沟槽更多的那条轴把区域切成内容段; 无沟槽返回 None。返回 (axis, [(a,b),...])。

    沟槽 = 任意颜色的近纯色行/列连续段(卡片网格的间隙、区块之间的留白、分割线两侧),
    不再要求与区域中位色一致 —— 中位色被卡片占满时, 间隙永远检不出(实测列表/仪表盘不切)。"""
    best = None
    for axis in (0, 1):
        size = region.shape[axis]
        mask, color = _uniform_lines(region, axis, tol)
        g = _gutter_runs(mask, color, min_run, tol)
        cuts, pos = [], 0
        for a, b in g:
            if a > pos:
                cuts.append((pos, a))
            pos = b
        if pos < size:
            cuts.append((pos, size))
        cuts = [(a, b) for a, b in cuts if b - a >= 2]
        if len(cuts) >= 2 and (best is None or len(cuts) > len(best[1])):
            best = (axis, cuts)
    return best


def _picture_like(region: np.ndarray, bands: list) -> bool:
    """图片/图表/插画样区域: 色彩丰富(量化后颜色数多)且不是由文字行带主导。

    这类区域用结构节点无法复现(渐变/照片/图表), 按克隆引擎「空白 Canvas 从基准截图回填」
    同款做法, 从用户自己的输入截图裁切为 image 资产。"""
    h, w = region.shape[:2]
    if h < 24 or w < 24:
        return False
    q = (region.reshape(-1, 3) // 32)
    colors = len(np.unique(q[:, 0] * 64 + q[:, 1] * 8 + q[:, 2]))
    band_cover = sum((b[1] - b[0]) for b in bands) / max(1, h)
    gray = region.astype(np.float32).mean(axis=2)
    bg = np.median(region.reshape(-1, 3), axis=0)
    bg_frac = float((np.abs(region.astype(np.int16) - bg.astype(np.int16)).max(axis=2) <= 24).mean())
    return colors >= 12 and gray.std() > 18 and band_cover < 0.45 and bg_frac < 0.70


def _text_bands(region: np.ndarray, bg: np.ndarray) -> list:
    """按行方差投影找文字行带: 返回 [(y0,y1,x0,x1, fg_rgb)] (区域内坐标)。"""
    h, w = region.shape[:2]
    if h < 6 or w < 12:
        return []
    gray = region.astype(np.float32).mean(axis=2)
    row_var = gray.var(axis=1)
    thr = max(60.0, float(np.percentile(row_var, 55)) * 1.2)
    active = row_var > thr
    bands, start = [], None
    for y, v in enumerate(active):
        if v and start is None:
            start = y
        elif not v and start is not None:
            if 2 <= y - start <= max(6, h // 3):
                bands.append((start, y))
            start = None
    if start is not None and 2 <= h - start <= max(6, h // 3):
        bands.append((start, h))
    out = []
    for y0, y1 in bands[:60]:
        strip = region[y0:y1]
        diff = np.abs(strip.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
        cols = np.where((diff > 40).any(axis=0))[0]
        if cols.size < 4:
            continue
        x0, x1 = int(cols[0]), int(cols[-1]) + 1
        if x1 - x0 < 6:
            continue
        fg_mask = diff > 40
        fg = strip[fg_mask].reshape(-1, 3).mean(axis=0) if fg_mask.any() else (bg * 0.3)
        out.append((y0, y1, x0, x1, fg))
    return out


def segment(image_path, max_nodes: int = None, max_depth: int = None, tol: int = None,
            min_area_ratio: float = None, min_run: int = None, asset_dir=None) -> UIR:
    """确定性: 截图 → 绝对定位 box 树(按参考图原宽排版)。

    tol: 沟槽判定的色差容差(越小切得越细); 质量循环逐轮收紧。
    底色一律用区域精确中位色(令牌表只做色板记录): 令牌量化会把 #f3f4f6 与 #ffffff
    合并成同一色, 卡片整块消失 → NCC 归零(实测登录页 19.7 分)。"""
    max_nodes = max_nodes or settings.SEG_MAX_NODES
    max_depth = max_depth or settings.SEG_MAX_DEPTH
    tol = int(tol or settings.SEG_COLOR_QUANT)
    min_area_ratio = float(min_area_ratio or settings.SEG_MIN_REGION_AREA)
    min_run = int(min_run or 6)
    asset_dir = Path(asset_dir) if asset_dir else None
    pics = [0]            # 已回填的图片数
    pic_area = [0.0]      # 已回填面积占比(封顶, 防止整页退化为一张图)
    with Image.open(image_path) as im:
        im = im.convert("RGB")
        W, H = im.size
        scale = W / WORK_W if W > WORK_W else 1.0
        work = im.resize((int(W / scale), int(H / scale)), Image.BILINEAR) if scale > 1 else im
        arr = np.asarray(work).astype(np.uint8)
        im_full = im.copy()
    pal = palette(arr, settings.PALETTE_MAX)
    tok_rgb = [p[:3] for p in pal]
    tokens = Tokens(color={f"c{i}": _hex(p) for i, p in enumerate(pal)},
                    space={"s0": 0, "s1": 4, "s2": 8, "s4": 16},
                    text_style={"t0": {"size": 14, "weight": 400, "line": 1.4, "color": "c1" if len(pal) > 1 else "c0"}})
    nodes, budget, sizes = [], [max_nodes], set()
    min_area = min_area_ratio * arr.shape[0] * arr.shape[1]
    page_area = float(arr.shape[0] * arr.shape[1])

    def S(v):  # 工作坐标 → 原图 px
        return int(round(v * scale))

    def emit_leaf(y0, y1, x0, x1, region):
        if budget[0] <= 0:
            return
        bg = np.median(region.reshape(-1, 3), axis=0)
        full = np.asarray(im_full.crop((S(x0), S(y0), S(x1), S(y1)))).astype(np.uint8)   # 原分辨率叶块
        bands = _text_bands(full, bg)                                                    # 坐标已是原图 px
        area_ratio = region.shape[0] * region.shape[1] / page_area
        if (asset_dir is not None and area_ratio <= 0.25 and pic_area[0] + area_ratio <= 0.40
                and pics[0] < 24 and _picture_like(region, bands)):
            asset_dir.mkdir(parents=True, exist_ok=True)
            name = f"region_{pics[0]:02d}.png"
            crop = im_full.crop((S(x0), S(y0), S(x1), S(y1)))
            crop.save(asset_dir / name, format="PNG")
            pics[0] += 1
            pic_area[0] += area_ratio
            nodes.append(Node(role="image", layout={"mode": "absolute"}, content={"src": f"static/img/{name}", "alt": "区域"},
                              box={"position": {"top": S(y0), "left": S(x0)}, "w": S(x1 - x0), "h": S(y1 - y0)}))
            budget[0] -= 1
            return
        children = []
        for by0, by1, bx0, bx1, fg in bands:
            if budget[0] <= 1:
                break
            h_px, w_px = int(by1 - by0), int(bx1 - bx0)
            if h_px >= 7 and w_px >= 24:
                # 文字行: 以字形纹理文本代理(字号≈行高), 复现笔画/字距的高频边缘, 比实心条更贴近 NCC/边缘通道
                size = max(8, int(h_px * 0.86))
                n_ch = max(2, int(w_px / (size * 0.62)))
                children.append(Node(role="text", layout={"mode": "absolute"},
                                     content={"text": _filler(n_ch, bx0 + by0)},
                                     box={"position": {"top": int(by0), "left": int(bx0)}, "w": w_px, "h": h_px,
                                          "color": _hex(fg), "overflow": "hidden"},
                                     text_ref=f"ts{size}"))
                sizes.add(size)
            else:
                children.append(Node(role="box", layout={"mode": "absolute"},
                                     box={"position": {"top": int(by0), "left": int(bx0)}, "w": w_px, "h": h_px,
                                          "bg": _hex(fg), "radius": 1}))
            budget[0] -= 1
        nodes.append(Node(role="box", layout={"mode": "stack"},
                          box={"position": {"top": S(y0), "left": S(x0)}, "w": S(x1 - x0), "h": S(y1 - y0),
                               "bg": _hex(bg), "overflow": "hidden"}, children=children))
        budget[0] -= 1

    def rec(y0, y1, x0, x1, depth):
        region = arr[y0:y1, x0:x1]
        if region.size == 0 or budget[0] <= 0:
            return
        area = (y1 - y0) * (x1 - x0)
        bg = np.median(region.reshape(-1, 3), axis=0)
        if depth >= max_depth or area < min_area * 4:
            emit_leaf(y0, y1, x0, x1, region)
            return
        # 大块中的子块也可能是图片: 先判一次, 是图直接回填不再切
        if asset_dir is not None and depth > 0 and area / page_area <= 0.25 and _picture_like(region, []):
            emit_leaf(y0, y1, x0, x1, region)
            return
        sp = _split(region, tol, min_run=max(2, min_run))
        if sp is None:
            emit_leaf(y0, y1, x0, x1, region)
            return
        axis, cuts = sp
        # 父块先铺底色(沟槽处的背景), 子块叠在上面
        nodes.append(Node(role="box", layout={"mode": "absolute"},
                          box={"position": {"top": S(y0), "left": S(x0)}, "w": S(x1 - x0), "h": S(y1 - y0), "bg": _hex(bg)}))
        budget[0] -= 1
        for a, b in cuts:
            if axis == 0:
                rec(y0 + a, y0 + b, x0, x1, depth + 1)
            else:
                rec(y0, y1, x0 + a, x0 + b, depth + 1)

    rec(0, arr.shape[0], 0, arr.shape[1], 0)
    for size in sizes:   # 文字代理的字阶令牌(行高驱动)
        tokens.text_style[f"ts{size}"] = {"size": size, "weight": 400, "line": 1.0}
    root_bg = _hex(np.median(arr.reshape(-1, 3), axis=0))
    root = Node(role="box", layout={"mode": "stack"},
                box={"w": W, "h": H, "bg": root_bg, "position": {"mode": "relative"}, "overflow": "hidden"}, children=nodes)
    uir = UIR(root=root, tokens=tokens,
              meta={"source_kind": "vision", "tier": "deterministic", "title": "Generated from screenshot",
                    "lang": "zh", "viewport": {"w": W, "h": H}, "palette": [p[:3] for p in pal],
                    "picture_regions": pics[0], "picture_area": round(pic_area[0], 3)})
    return uir.normalize()


_LLM_SYSTEM = (
    "你是 UI 逆向结构化专家。根据界面截图输出 UIR v1 JSON(统一 UI 中间表示), 尽量复现截图的版面、配色、"
    "文案与控件。只输出一个 JSON 对象, 不要 markdown。结构: {\"uir_version\":\"1\",\"meta\":{\"title\":..,"
    "\"lang\":..,\"viewport\":{\"w\":W,\"h\":H}},\"tokens\":{\"color\":{\"c0\":\"#hex\",..},\"text_style\":{\"t0\":"
    "{\"size\":px,\"weight\":400,\"color\":\"c1\"}},\"space\":{\"s1\":4,\"s2\":8,\"s4\":16,\"s6\":32}},\"root\":NODE}。"
    "NODE={\"role\":角色,\"layout\":{\"mode\":\"flex|grid\",\"direction\":\"row|col\",\"gap\":令牌,\"justify\":..,"
    "\"align\":..,\"cols\":n},\"box\":{\"pad\":[令牌..],\"bg\":令牌或#hex,\"radius\":px,\"w\":px或'100%',\"h\":px},"
    "\"text_ref\":文本样式令牌,\"content\":{\"text\":截图里的真实文字,\"placeholder\":..,\"icon\":名},"
    "\"intents\":[{\"on\":\"click\",\"do\":\"navigate|toggle|submit-form\"}],\"children\":[..]}。角色闭集: box,text,"
    "image,icon,button,input,link,nav,list,list_item,card,header,footer,hero,divider,badge,avatar,table,form,section。"
    "图片区域用 image 角色并给出 w/h, 不要外链。根节点宽度等于截图宽度 W。文字必须逐字抄录截图中可见文案。"
    "若用户附带了上一轮的像素差分线索, 优先修正这些区域的尺寸/颜色/缺失内容。"
)


@register_input("screenshot")
class ScreenshotAdapter:
    def to_uir(self, req: GenRequest, ctx: BuildContext) -> UIR:
        img = req.image_path
        with Image.open(img) as im:
            W, H = im.size
        if ctx.ai_cfg and not ctx.expired() and not req.hints.get("force_tier0"):
            try:
                ctx.log("UIR", f"截图→UIR: 调用多模态模型 ({ctx.ai_cfg['model']}), {W}×{H}")
                hint_txt = ""
                if req.hints.get("regions"):
                    hint_txt = "\n上一轮像素差分线索(参考图 vs 产物):\n- " + "\n- ".join(req.hints["regions"])
                    if req.hints.get("score") is not None:
                        hint_txt += f"\n上一轮评分 {req.hints['score']} (结构 {req.hints.get('structure')} / 边缘 {req.hints.get('edge')})"
                images = [img] + ([req.hints["last_render"]] if req.hints.get("last_render") else [])
                user = (f"截图尺寸 {W}×{H}。第 1 张图是目标界面截图" +
                        ("; 第 2 张图是上一轮生成产物的渲染截图(用于对比修正)" if len(images) > 1 else "") +
                        f"。{hint_txt}\n请输出 UIR JSON。")
                obj = llm.chat_json(ctx.ai_cfg, _LLM_SYSTEM, user, images=images)
                uir = UIR.from_dict(obj).normalize()
                errs = uir.validate()
                if errs:
                    raise ValueError("; ".join(errs[:3]))
                uir.meta.update({"source_kind": "vision", "tier": "llm", "viewport": {"w": W, "h": H}})
                if "w" not in uir.root.box:
                    uir.root.box["w"] = W
                return uir
            except Exception as exc:
                ctx.log("UIR", f"多模态 UIR 不可用, 回落确定性版面分析: {type(exc).__name__}: {str(exc)[:160]}")
        depth = int(req.hints.get("seg_depth") or settings.SEG_MAX_DEPTH)
        min_area = float(req.hints.get("seg_min_area") or settings.SEG_MIN_REGION_AREA)
        min_run = int(req.hints.get("seg_min_run") or 6)
        uir = segment(img, max_depth=depth, min_area_ratio=min_area, min_run=min_run,
                      asset_dir=(ctx.ws / "static" / "img") if ctx.ws else None)
        ctx.log("UIR", f"截图→UIR(确定性版面分析, 深度 {depth}, 最小面积 {min_area:.4f}, 沟槽 ≥{min_run}px, "
                       f"图片回填 {uir.meta.get('picture_regions')} 块/{uir.meta.get('picture_area')}): {uir.root.count()} 节点, "
                       f"色板 {len(uir.tokens.color)} 色, {W}×{H}")
        return uir
