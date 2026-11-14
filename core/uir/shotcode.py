"""界面截图 → 代码直写轨。

与文字需求轨(codegen)共用「写页面 → 体检 → 验收 → 修复」循环, 不同的几步在 ShotProfile 里:

1. 预处理(确定性 + 一次快速看图):
   - 设备像素比: 截图像素宽 ≠ 网页 CSS 宽(Retina 2 倍、手机 3 倍)。按宽高比与像素宽估计, 原图缩到 CSS 尺寸后再用。
   - 浏览器 / 系统外框: 看图模型判断有没有外框; 有则放大顶部窄条再问一次正文起点, 并在附近找整行像素突变的位置校准, 裁掉。
   - 位图素材: 看图模型列出照片、插画、头像、logo 图形这类代码画不出来的区域; 每个框在原分辨率上吸附到附近最强的
     边缘后裁出, 存成 assets/ 下的文件交给写页面的模型引用。文字、按钮、卡片、图标一律用代码写。
2. 读图任务书: 看图模型把截图读成结构化任务书(布局分区、尺寸、配色、字号、全部可见文字、组件、平板/手机怎么重排、
   逐条验收清单)。用户的补充提示进任务书, 对应条目 must=true, 与截图冲突时以提示为准。
3. 写页面 / 修复: 模型同时看原图、任务书、素材清单, 写单文件响应式网页。
4. 评分 shot-fit-v1: 在原图 CSS 宽度渲染与原图比像素/结构/文字相似度(与克隆侧同一函数) + 清单达成 + 三端(桌面 /
   平板 768 / 手机 390)健康度 + 看图设计分。验收时把原图与产出并排给模型看。

没有针对任何具体页面的规则; 所有阈值只依赖图像统计量。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from core.uir import codegen, llm, probe, render, settings

SCORING = "shot-fit-v1"
TABLET = {"width": 768, "height": 1024}

FRAME_SYSTEM = "你是界面截图分析师。只输出一个 JSON 对象, 不要 markdown, 不要解释。"
FRAME_USER = (
    "这是一张界面截图, 尺寸 {w}×{h} 像素。\n"
    "1) has_chrome: 截图里是否带有浏览器或操作系统的外框(浏览器标签栏/地址栏/书签栏、窗口标题栏、系统任务栏或 Dock、"
    "手机状态栏不算外框)。\n"
    "2) content_box: 网页/应用本身的区域 [x0,y0,x1,y1](像素, 相对整张截图); 没有外框就是整张图。\n"
    "3) images: 正文里所有用代码画不出来的位图区域 —— 照片、商品图、插画、头像、带图形的 logo、复杂的徽章插图。"
    "纯文字、按钮、色块、线条、简单图标(箭头、搜索、铃铛这类单色图标)不算。每个给 {{\"what\":\"简述\",\"box\":[x0,y0,x1,y1]}}。"
    "框要贴着图片本身的边缘, 不要把旁边的文字框进去。\n"
    "输出 {{\"has_chrome\":true,\"content_box\":[...],\"images\":[...]}}"
)
CHROME_USER = (
    "这是一张截图最上面的一条(尺寸 {w}×{h} 像素), 上面画了几条带编号的红色横线。截图带有浏览器外框: 标签栏、带网址的地址栏、"
    "书签栏、扩展图标; 外框下面才是网页本身 —— 网页自己的 logo、导航菜单、搜索框、登录按钮都属于网页, 不是外框。"
    "哪一条红线正好是浏览器外框的下沿(线上方是外框, 线下方是网页)? 都不是就回答 0。输出 {{\"line\":编号}}"
)

READ_SYSTEM = (
    "你是资深前端架构师兼视觉设计师。给你一张界面截图(已去掉浏览器外框, 按 CSS 像素缩放), 可能还有用户的补充要求。"
    "你要把截图读成一份任务书, 让工程师不看截图也能把页面按原样写出来, 让验收员能逐条核对。"
    "只输出一个 JSON 对象, 不要 markdown, 不要解释。\n"
    "字段:\n"
    "- title: 页面标题\n"
    "- lang: 界面文字的语言代码(如 zh-CN / en)\n"
    "- summary: 一两句话说明这是什么页面; 用户有补充要求时说明你打算怎么结合\n"
    "- kind: 页面性质(如 电商列表、后台、落地页、表单、移动端应用页)\n"
    "- design: {palette: 主要颜色的十六进制值并注明用途(背景、主色、强调色、文字、边框), typography: 字体风格(衬线/无衬线/等宽)"
    "与各级字号字重(以 CSS 像素计), radius: 圆角, shadow: 阴影/描边风格, spacing: 主要间距}\n"
    "- layout: 从上到下逐个区块描述: 名称、位置与尺寸(CSS 像素)、背景、内部怎么排(几列网格、左右分栏、卡片尺寸与间距)\n"
    "- texts: 截图里所有可见文字, 按阅读顺序, 一个元素一条, 原样照抄(不翻译、不改写、不省略; 数字和单位也照抄)\n"
    "- components: 关键组件清单(导航、搜索框、按钮、卡片、标签、列表、侧栏卡片…), 每个写清外观细节\n"
    "- responsive: 平板(768px)和手机(390px)宽度下怎么重排(列数怎么减、侧栏放到哪、导航怎么收起), 不改变内容\n"
    "- checklist: 8~14 条验收项, 每条 {id, text, type, must}。type 取 content(有哪些内容、文字是否齐全) / visual(布局、"
    "尺寸、配色、圆角阴影这类外观) / interaction(截图里看得出的可交互控件有反应)。must=true 给: 整体布局结构、"
    "主要区块、关键文字内容、用户补充要求里的每一条; 细节还原 must=false。每条都要能对照截图直接核对。\n"
    "规则: 用户补充要求与截图冲突时以补充要求为准, 其余照截图; 不要编造截图里没有的大块内容。"
)

WRITE_SYSTEM = (
    "你是顶尖的前端工程师, 擅长把设计稿一比一还原成代码。给你: 界面截图(原图, 按 CSS 像素)、读图任务书、"
    "可用的图片素材清单, 可能还有用户补充要求。写出一个完整的单文件网页, 在桌面宽度下尽可能与截图一致, 在平板和手机宽度下合理重排。\n"
    "硬性要求:\n"
    "1. 只输出一个完整的 HTML 文档, 从 <!DOCTYPE html> 开始, 到 </html> 结束。不要解释文字, 不要 markdown 代码围栏。\n"
    "2. 所有 CSS 和 JavaScript 内联。禁止外部资源(CDN、外链字体、外网图片)。图片只能用素材清单里给出的相对路径"
    "(如 assets/img_01.jpg), 用 <img> 或 background-image 放到截图里对应的位置, 按素材尺寸设好宽高比, 用 object-fit: cover。"
    "素材清单里没有的图形: 简单图标用内联 SVG 画, 头像占位用色块 + 首字。不要用 emoji 代替图标。\n"
    "3. 文字一律用真实文本(不要做成图片), 内容与截图逐字一致; 字号、字重、颜色、行高照截图。\n"
    "4. 布局照任务书的分区与尺寸还原: 用 flex / grid 搭结构, 宽度用 max-width + 百分比/fr, 不要整页绝对定位, 不要写死整页宽度。"
    "桌面宽度(截图宽度)下各区块的位置、尺寸、间距、圆角、阴影、描边、背景色要与截图吻合: 素材清单里的坐标和尺寸就是该图片在原图里的"
    "精确位置, 桌面宽度下图片框的宽高要与之一致(误差不超过 8px), 并据此推出所在卡片、网格列宽和间距; "
    "内容区的最大宽度、左右留白、各栏宽度、通栏高度按截图量出的数值写, 不要凭感觉放大或缩小。\n"
    "5. 自适应: 写 viewport meta; 768px 宽和 390px 宽下按任务书的 responsive 重排(减列、侧栏下移、导航收起为可展开的菜单), "
    "任何宽度都不能出现横向滚动条, 文字不能互相重叠或被裁掉。\n"
    "6. 截图里看得出是可交互的控件(按钮、标签页、输入框、菜单)要有真实的悬停和点击反馈; 标签页能切换, 菜单能展开。"
    "代码不能抛出运行时异常; 不用 alert / confirm / prompt。\n"
    "7. 用户补充要求优先: 与截图冲突的地方按补充要求做, 其余照截图。"
)
_REPAIR_HEAD = (
    "\n\n你现在的任务是修复一个已有页面, 让它更接近截图并解决问题。会给你: 原图、当前页面在同一宽度下的截图、任务书、"
    "当前页面完整代码、问题清单(来自真实浏览器测量和对照验收)。逐条解决问题, 保留已经正确的部分。"
)
REPAIR_SYSTEM = WRITE_SYSTEM + _REPAIR_HEAD + (
    "\n输出方式(二选一, 不要输出任何解释):\n"
    "A. 改动集中在局部时优先用修改块 —— 只输出若干个修改块, 每块严格按下面的格式:\n"
    "<<<<<<< SEARCH\n(当前代码里要被替换的原文: 逐字符照抄, 包括缩进和换行; 取连续的几行, 足以在文件中唯一定位)\n"
    "=======\n(替换后的内容; 留空表示删除)\n>>>>>>> REPLACE\n"
    "修改块之间互不重叠。\n"
    "B. 需要大面积重写时, 输出修复后的完整 HTML 文档(从 <!DOCTYPE html> 到 </html>)。"
)
REPAIR_FULL_SYSTEM = WRITE_SYSTEM + _REPAIR_HEAD + "输出修复后的完整 HTML 文档(不是补丁)。"

REVIEW_SYSTEM = (
    "你是严格的界面还原验收员。给你: 原始截图(设计稿)、按它写出的页面在同一宽度下的截图、平板和手机宽度下的截图、"
    "验收清单、自动测量数据(像素相似度、溢出、重叠、报错)。逐条判断验收项, 评价还原质量。只输出一个 JSON 对象。\n"
    "规则:\n"
    "1. 只依据截图里实际看到的内容下结论, 每条 evidence 写具体依据(哪个区块、差在哪)。\n"
    "2. verdict: yes(与原图一致) / partial(大体一致但有明显差异) / no(缺失或明显不同) / unsure(截图无法判断)。\n"
    "3. 测量数据是确定性的, 与观感冲突时以测量为准。\n"
    "4. design.score 0~10: 以「资深前端照这张设计稿手工还原」为 9 分参照。布局错位、区块缺失、配色明显不同、文字缺失或错字、"
    "图片放错位置、平板/手机下错乱或横向滚动, 都要扣分并写进 issues。\n"
    "5. issues 按严重程度列出最需要改的差异(最多 8 条), 每条写清位置(哪个区块)、现象(problem)和修改方向(fix, 尽量给出具体的"
    "尺寸、颜色值、列数)。\n"
    "输出格式: {\"items\":[{\"id\":\"c1\",\"verdict\":\"yes|partial|no|unsure\",\"evidence\":\"...\"}],"
    "\"design\":{\"score\":0,\"comment\":\"...\"},"
    "\"issues\":[{\"severity\":\"high|mid|low\",\"problem\":\"...\",\"fix\":\"...\"}],\"summary\":\"一句话总评\"}"
)


# ---------------------------------------------------------------------------
# 预处理: 设备像素比 / 外框 / 素材
# ---------------------------------------------------------------------------
def estimate_dpr(w: int, h: int) -> int:
    """截图像素尺寸 → 设备像素比。竖图按手机(CSS 宽约 360~430); 横图: 2200px 以下 1 倍, 4400px 以下 2 倍, 再大 3 倍。"""
    if h > w * 1.25 and w >= 600:
        return max(1, min(4, round(w / 400)))
    if w < 2200:
        return 1
    return 2 if w < 4400 else 3


def _gray(img):
    import numpy as np
    return np.asarray(img.convert("L"), dtype=np.float32)


def chrome_candidates(img, frac: float = 0.18) -> list:
    """顶部 frac 高度内横贯整行的分界线(相邻两行有明显差异的列占 60% 以上), 相邻行合并为一条, 返回 y 列表(CSS 像素)。
    浏览器外框下沿是这样一条线; 网页自己的通栏边界也是, 由看图模型在编号线里选。"""
    import numpy as np
    g = _gray(img)
    lim = max(2, int(g.shape[0] * frac))
    d = np.abs(g[1:lim] - g[:lim - 1])
    cov = (d > 10).mean(axis=1)
    ys = [y + 1 for y in range(len(cov)) if cov[y] > 0.6 and y + 1 >= 12]
    out = []
    for y in ys:
        if out and y - out[-1][-1] <= 3:
            out[-1].append(y)
        else:
            out.append([y])
    return [grp[-1] for grp in out][:8]


def content_fraction(img) -> float:
    """裁出的小图里与其边框中位色明显不同的像素占比。框准了的头像/图标占比高; 框偏了只裁到背景和一条边的占比低。"""
    import numpy as np
    a = np.asarray(img.convert("RGB"), dtype=np.int16)
    if a.shape[0] < 3 or a.shape[1] < 3:
        return 0.0
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    bg = np.median(border, axis=0)
    return float((np.abs(a - bg).max(axis=2) > 30).mean())


def snap_box(gray, box, frac: float = 0.12, min_r: int = 6, min_snap_scale: float = 1.0):
    """把模型给的近似框吸附到附近最强的边缘: 每条边在 ±max(min_r, 边长×frac) 内找跨该边灰度差最大的线。
    找不到明显的边(最大值不到该范围中位数的 2 倍)就保持原位。坐标与 gray 同一像素系。"""
    import numpy as np
    h, w = gray.shape
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    x0, x1 = sorted((max(0, min(w - 1, x0)), max(0, min(w, x1))))
    y0, y1 = sorted((max(0, min(h - 1, y0)), max(0, min(h, y1))))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    if min(x1 - x0, y1 - y0) < 60 * min_snap_scale:  # 小图(头像、图标)周围边缘密集, 吸附反而容易跑偏
        return [x0, y0, x1, y1]
    rx, ry = max(min_r, int((x1 - x0) * frac)), max(min_r, int((y1 - y0) * frac))

    def best(profile_fn, v, r, lo_lim, hi_lim):
        lo, hi = max(lo_lim + 1, v - r), min(hi_lim - 1, v + r)
        if hi <= lo:
            return v
        vals = np.array([profile_fn(c) for c in range(lo, hi + 1)])
        k = int(np.argmax(vals))
        return lo + k if vals[k] >= 2 * max(0.5, float(np.median(vals))) else v
    ys, xs = slice(y0, y1), slice(x0, x1)
    nx0 = best(lambda c: float(np.abs(gray[ys, c] - gray[ys, c - 1]).mean()), x0, rx, 0, w)
    nx1 = best(lambda c: float(np.abs(gray[ys, c] - gray[ys, c - 1]).mean()), x1, rx, 0, w)
    ny0 = best(lambda c: float(np.abs(gray[c, xs] - gray[c - 1, xs]).mean()), y0, ry, 0, h)
    ny1 = best(lambda c: float(np.abs(gray[c, xs] - gray[c - 1, xs]).mean()), y1, ry, 0, h)
    if nx1 - nx0 < 8 or ny1 - ny0 < 8:
        return [x0, y0, x1, y1]
    a0, a1 = (x1 - x0) * (y1 - y0), (nx1 - nx0) * (ny1 - ny0)
    if abs(a1 - a0) > 0.35 * a0:               # 吸附把框改得面目全非, 多半是吸到了别的元素的边上
        return [x0, y0, x1, y1]
    return [nx0, ny0, nx1, ny1]


def _box(v, w, h):
    try:
        x0, y0, x1, y1 = [float(t) for t in v][:4]
    except (TypeError, ValueError):
        return None
    x0, x1 = sorted((max(0.0, min(w, x0)), max(0.0, min(w, x1))))
    y0, y1 = sorted((max(0.0, min(h, y0)), max(0.0, min(h, y1))))
    return [x0, y0, x1, y1] if x1 - x0 >= 4 and y1 - y0 >= 4 else None


def _acc(total: dict, part: dict):
    """把一次调用的 usage 累加进 total(llm.chat_json 的 usage_out 是覆盖写)。"""
    if total is None:
        return
    for k in ("prompt_tokens", "completion_tokens"):
        total[k] = int(total.get(k) or 0) + int(part.get(k) or 0)


def prepare(src: Path, ws: Path, ai_cfg: dict, deadline: float, log, usage: dict = None) -> dict:
    """原始截图 → 参考图(CSS 像素、去外框) + 位图素材。返回 {ref, css_w, css_h, dpr, crop, assets:[...]}。

    ref 写到 ws/original_preview.png(评分与界面对比都以它为准), 素材写到 ws/assets/。看图失败时退化为
    「不裁外框、没有素材」, 不中断生成。"""
    from PIL import Image
    with Image.open(src) as im:
        orig = im.convert("RGB")
    W, H = orig.size
    dpr = estimate_dpr(W, H)
    css = orig.resize((max(1, round(W / dpr)), max(1, round(H / dpr))), Image.LANCZOS) if dpr > 1 else orig.copy()
    cw, ch = css.size
    tmp = ws / "_shot_css.png"
    css.save(tmp)
    frame, images = {}, []
    try:
        parts = llm.image_parts(tmp, 1280, max_tiles=2, tile_h=1600)[0]
        frame = llm.chat_json(ai_cfg, FRAME_SYSTEM, FRAME_USER.format(w=cw, h=ch), parts=parts, temperature=0,
                              max_tokens=4096, thinking=False, json_mode=True, usage_out=(u := {}),
                              deadline=min(deadline, time.monotonic() + 60))
        _acc(usage, u)
        # 模型看到的是按宽缩放到 ≤1280 的图, 坐标按它说的尺寸系换回 CSS 像素(它被告知的是 CSS 尺寸, 多数照此回答)
        images = [i for i in (frame.get("images") or []) if isinstance(i, dict)]
    except Exception as exc:
        log("GEN", f"截图预读失败, 不裁外框、不提取素材: {type(exc).__name__}: {str(exc)[:120]}")
    crop = [0, 0, cw, ch]
    if frame.get("has_chrome"):
        cb = _box(frame.get("content_box"), cw, ch) or [0, 0, cw, ch]
        top = int(cb[1])
        cands = chrome_candidates(css)
        if cands:
            try:                                         # 编号线选择题: 看图模型直接报坐标误差几十像素, 选线可靠得多
                from PIL import ImageDraw
                strip_h = min(ch, max(cands) + 40)
                sw = min(1600, cw * 2)
                k = sw / cw
                strip = css.crop((0, 0, cw, strip_h)).resize((sw, max(1, round(strip_h * k))), Image.LANCZOS)
                d = ImageDraw.Draw(strip)
                for n, y in enumerate(cands, 1):
                    yy = round(y * k)
                    d.line([(0, yy), (sw, yy)], fill=(230, 0, 0), width=2)
                    d.rectangle([4, yy - 13, 30, yy + 1], fill=(230, 0, 0))
                    d.text((10, yy - 13), str(n), fill=(255, 255, 255))
                sp = ws / "_shot_strip.png"
                strip.save(sp)
                obj = llm.chat_json(ai_cfg, FRAME_SYSTEM, CHROME_USER.format(w=strip.size[0], h=strip.size[1]),
                                    parts=[llm._image_part(sp, 1600)], temperature=0, max_tokens=256, thinking=False,
                                    json_mode=True, usage_out=(u := {}), deadline=min(deadline, time.monotonic() + 40))
                _acc(usage, u)
                n = int(obj.get("line") or 0)
                if 1 <= n <= len(cands):
                    top = cands[n - 1]
            except Exception:
                pass
        bottom = int(cb[3]) if cb[3] > top + ch * 0.4 else ch
        left, right = int(cb[0]), int(cb[2])
        if right - left < cw * 0.6:                     # 左右外框很少见, 模型给得离谱时不采信
            left, right = 0, cw
        crop = [left, top, right, bottom]
        if (right - left) * (bottom - top) < 0.45 * cw * ch:
            crop = [0, 0, cw, ch]
    ref = css.crop(tuple(crop))
    ref_path = ws / "original_preview.png"
    ref.save(ref_path)
    if crop != [0, 0, cw, ch]:
        log("GEN", f"去掉截图外框: 正文区域 x {crop[0]}~{crop[2]}, y {crop[1]}~{crop[3]}(CSS 像素)")
    # ---- 素材 ----
    adir = ws / "assets"
    shutil.rmtree(adir, ignore_errors=True)
    assets = []
    gray_full = _gray(orig)
    for it in images[:40]:
        b = _box(it.get("box"), cw, ch)
        if not b:
            continue
        b = snap_box(gray_full, [v * dpr for v in b], min_r=6 * dpr, min_snap_scale=dpr)  # 在原分辨率上吸附边缘, 裁图也用原分辨率
        if not b:
            continue
        x0, y0, x1, y1 = b
        bw, bh = (x1 - x0) / dpr, (y1 - y0) / dpr
        cx0, cy0 = x0 / dpr - crop[0], y0 / dpr - crop[1]
        if bw < 16 or bh < 16 or bw * bh > 0.6 * ref.size[0] * ref.size[1]:
            continue
        if cx0 + bw <= 0 or cy0 + bh <= 0 or cx0 >= ref.size[0] or cy0 >= ref.size[1]:
            continue                                      # 在外框里(浏览器扩展图标之类)
        piece = orig.crop((int(x0), int(y0), int(x1), int(y1)))
        if min(bw, bh) < 48 and content_fraction(piece) < 0.45:
            continue                                      # 小图框偏了(只裁到背景和半个头像): 不如让页面画占位
        adir.mkdir(parents=True, exist_ok=True)
        name = f"img_{len(assets) + 1:02d}.jpg"
        piece.save(adir / name, quality=88)
        assets.append({"file": f"assets/{name}", "what": str(it.get("what") or "")[:60],
                       "x": round(cx0), "y": round(cy0), "w": round(bw), "h": round(bh)})
    for f in (tmp, ws / "_shot_strip.png"):
        f.unlink(missing_ok=True)
    if assets:
        log("GEN", f"提取位图素材 {len(assets)} 个: " + "; ".join(f"{a['what']} {a['w']}×{a['h']}" for a in assets[:8]))
    return {"ref": str(ref_path), "css_w": ref.size[0], "css_h": ref.size[1], "dpr": dpr, "crop": crop,
            "src_size": [W, H], "assets": assets}


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------
def _assets_text(assets: list) -> str:
    if not assets:
        return "(没有可用的图片素材; 截图里的图片区域用与原图主色相近的色块或渐变占位)"
    return "\n".join(f"- {a['file']}: {a['what']}, 原图位置 x={a['x']} y={a['y']}, 尺寸 {a['w']}×{a['h']}" for a in assets)


def _ref_parts(ref: Path, label: str = "原始截图(设计稿)") -> list:
    tiles = llm.image_parts(ref, 1280, max_tiles=3, tile_h=1500)[0]
    out = []
    for k, part in enumerate(tiles):
        out.append({"type": "text", "text": label + (f", 从上到下第 {k + 1}/{len(tiles)} 段" if len(tiles) > 1 else "") + ":"})
        out.append(part)
    return out


class ShotProfile(codegen.Profile):
    plan_stage = "读取截图"
    review_stage = "对照原图验收"
    score_label = "还原度"

    def __init__(self, ws: Path, src: Path, hint: str, log):
        self.ws, self.src, self.hint, self.log = ws, src, (hint or "").strip(), log
        self.prep: dict = {}
        self._brief_texts: list = []
        self._last_rep: dict = {}

    # ---- 读图 ----
    def plan(self, ai_cfg, desc, deadline, usage):
        pre = {}
        self.prep = prepare(self.src, self.ws, ai_cfg, deadline, self.log, pre)
        ref = Path(self.prep["ref"])
        user = (f"截图尺寸 {self.prep['css_w']}×{self.prep['css_h']}(CSS 像素)。"
                + (f"\n用户补充要求: {self.hint}" if self.hint else "\n用户没有补充要求, 照截图还原。")
                + f"\n可用的图片素材(已从截图裁出):\n{_assets_text(self.prep['assets'])}")
        def norm(o):
            b = codegen.normalize_plan(o, self.hint or "还原截图界面")
            for k in ("layout", "texts", "components", "responsive"):
                v = o.get(k)
                if v not in (None, "", [], {}):
                    b[k] = v if isinstance(v, (str, list)) else json.dumps(v, ensure_ascii=False)
            return b
        brief = codegen._json_call(ai_cfg, READ_SYSTEM, user, deadline, norm, usage, parts=_ref_parts(ref),
                                   temperature=0.2, max_tokens=8192)
        if usage is not None:
            _acc(usage, pre)
        return self._decorate(brief)

    def _decorate(self, brief):
        texts = brief.get("texts")
        self._brief_texts = texts if isinstance(texts, list) else ([texts] if texts else [])
        brief["source"] = {k: self.prep.get(k) for k in ("css_w", "css_h", "dpr", "crop", "src_size")}
        brief["assets"] = self.prep.get("assets") or []
        if self.hint:
            brief["hint"] = self.hint
        return brief

    def fallback(self, desc):
        if not self.prep:
            raise codegen.CodegenError("截图预处理失败")
        b = codegen.fallback_plan(self.hint or "照截图还原界面")
        b["checklist"] = [{"id": "c1", "text": "页面的布局、内容和配色与截图一致", "type": "visual", "must": True}]
        return self._decorate(b)

    # ---- 写 / 修 ----
    def _brief_text(self, brief):
        keys = ("title", "lang", "summary", "kind", "design", "layout", "texts", "components", "responsive")
        slim = {k: brief.get(k) for k in keys if brief.get(k) not in (None, "", [], {})}
        slim["checklist"] = [{k: i[k] for k in ("id", "text", "type", "must")} for i in brief["checklist"]]
        return json.dumps(slim, ensure_ascii=False, indent=1)

    def _head(self, brief):
        src = brief.get("source") or {}
        return (f"截图宽 {src.get('css_w')} × 高 {src.get('css_h')}(CSS 像素; 这是桌面版的设计宽度)。\n"
                + (f"用户补充要求(优先): {self.hint}\n" if self.hint else "")
                + f"\n读图任务书:\n{self._brief_text(brief)}\n\n可用的图片素材(路径相对页面):\n{_assets_text(brief.get('assets'))}\n")

    def write(self, ai_cfg, desc, brief, deadline, note="", on_delta=None):
        user = self._head(brief) + (f"\n{note}\n" if note else "") + "\n现在输出完整的 HTML 文档。"
        return codegen._code_call(ai_cfg, WRITE_SYSTEM, user, deadline, 0.4,
                                  parts=_ref_parts(Path(self.prep["ref"])), on_delta=on_delta)

    def repair(self, ai_cfg, desc, brief, html, problems, deadline, shot=None, stalled=False, on_delta=None):
        user = (self._head(brief) + "\n需要解决的问题:\n" + "\n".join(f"{i + 1}. {p}" for i, p in enumerate(problems))
                + ("\n\n上一次修复没有带来改善, 换一种实现思路。" if stalled else "")
                + f"\n\n当前页面完整代码:\n{html}\n\n现在输出"
                + ("修复后的完整 HTML 文档。" if stalled else "修改块(或修复后的完整 HTML 文档)。"))
        parts = _ref_parts(Path(self.prep["ref"]))
        fid = (self._last_rep.get("shots") or {}).get("fid")
        if fid and Path(fid).is_file():
            parts += _ref_parts(Path(fid), "当前页面在同一宽度下的截图")
        if stalled:
            new, meta = codegen._code_call(ai_cfg, REPAIR_FULL_SYSTEM, user, deadline, 0.6, parts=parts, on_delta=on_delta)
            return new, dict(meta, mode="full")
        out = llm.chat_text(ai_cfg, REPAIR_SYSTEM, user, parts=parts, temperature=0.3, max_tokens=settings.CODE_MAX_TOKENS,
                            deadline=deadline, thinking=settings.CODE_THINKING, complete=codegen._repair_complete,
                            on_delta=on_delta)
        meta = {"elapsed": out["elapsed"], "usage": out.get("usage") or {}}
        text = out["text"]
        if codegen._PATCH_RE.search(text):
            new, ok, bad = codegen.apply_patches(html, text)
            if not ok or new == html:
                raise codegen.CodegenError(f"修改块无法应用到当前代码(共 {ok + bad} 块)")
            return new, dict(meta, mode="patch", patches=ok, patches_failed=bad)
        new, complete = codegen.extract_html(text)
        if not complete:
            raise codegen.CodegenError("修复输出不完整")
        return new, dict(meta, mode="full")

    # ---- 附加测量: 原图宽度下的还原度 + 平板宽度 ----
    def after_probe(self, pool, ws, rep, rdir, log):
        src = self.prep
        ref = Path(src["ref"])
        fid = rdir / "fid.png"
        render.screenshot_dir(pool, ws, ws / "index.html", fid, {"width": src["css_w"], "height": min(src["css_h"], 1200)})
        # 只比与原图等高的部分(页面比截图长是正常的: 截图只截了首屏或一部分)
        from PIL import Image
        with Image.open(fid) as im:
            if im.size[1] > src["css_h"]:
                im.crop((0, 0, im.size[0], src["css_h"])).save(fid)
        ref_text = "\n".join(str(t) for t in self._brief_texts)
        out_text = rep.get("text") or ""
        sim = render.score_against_reference(ref, fid, rdir / "diff.png", ref_text=ref_text, out_text=out_text)
        rep["fidelity"] = {k: sim.get(k) for k in ("score", "structure", "edge", "text", "text_recall", "height_ratio")}
        rep["fidelity"]["hints"] = render.region_hints(ref, fid, sim, top=6)
        rep.setdefault("shots", {})["fid"] = str(fid)
        if (rdir / "diff.png").is_file():
            rep["shots"]["diff"] = str(rdir / "diff.png")
        # 平板
        tab = rdir / "tablet.png"
        with pool.context(viewport=dict(TABLET)) as ctx:
            page = ctx.new_page()
            page.route("**/*", lambda r: r.abort() if probe._remote(r.request.url) else r.continue_())
            page.goto((ws / "index.html").resolve().as_uri(), wait_until="load")
            page.wait_for_timeout(500)
            dims = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
            page.screenshot(path=str(tab))
            page.close()
        rep["tablet"] = {"scroll_w": int(dims[0]), "client_w": int(dims[1]), "overflow_x": int(dims[0]) > int(dims[1]) + 1}
        rep["shots"]["tablet"] = str(tab)
        self._last_rep = rep
        log("SIMILARITY", f"原图宽度 {src['css_w']}px 下与原图相似度 {sim.get('score')}(结构 {sim.get('structure')}, "
                          f"边缘 {sim.get('edge')}, 文字 {sim.get('text')}); 平板 768 横向溢出 {'是' if rep['tablet']['overflow_x'] else '否'}")

    def defects(self, rep):
        out = probe.hard_defects(rep)
        tab = rep.get("tablet") or {}
        if tab.get("overflow_x"):
            out.append(f"平板宽度(768px)下出现横向滚动条(内容宽 {tab.get('scroll_w')}px)")
        return out

    # ---- 验收 ----
    def review(self, ai_cfg, desc, brief, rep, html, deadline, usage):
        fidm = rep.get("fidelity") or {}
        tab = rep.get("tablet") or {}
        head = ("验收清单:\n" + "\n".join(f"- {i['id']} [{i['type']}{' · 必须' if i['must'] else ''}] {i['text']}" for i in brief["checklist"])
                + (f"\n\n用户补充要求: {self.hint}" if self.hint else "")
                + f"\n\n自动测量:\n- 原图宽度下与原图的像素/结构相似度 {fidm.get('score')}(结构 {fidm.get('structure')}, 边缘 {fidm.get('edge')}, "
                  f"文字重合 {fidm.get('text')})\n- 差异最大的区域: " + ("; ".join(fidm.get("hints") or []) or "无")
                + f"\n- 平板 768px 横向溢出 {'是' if tab.get('overflow_x') else '否'}\n" + codegen.probe_digest(rep) + "\n")
        parts = _ref_parts(Path(self.prep["ref"]))
        shots = rep.get("shots") or {}
        if shots.get("fid") and Path(shots["fid"]).is_file():
            parts += _ref_parts(Path(shots["fid"]), "按设计稿写出的页面, 同一宽度")
        if shots.get("tablet") and Path(shots["tablet"]).is_file():
            parts += [{"type": "text", "text": "平板宽度 768px 首屏:"}, llm._image_part(Path(shots["tablet"]), 768)]
        if shots.get("mobile") and Path(shots["mobile"]).is_file():
            parts += [{"type": "text", "text": "手机宽度 390px 首屏:"}, llm._image_part(Path(shots["mobile"]), 390)]
        norm = lambda o: codegen.normalize_review(o, brief)  # noqa: E731
        out = codegen._json_call(ai_cfg, REVIEW_SYSTEM, head + "\n下面是截图。", deadline, norm, usage, parts=parts,
                                 temperature=0.1, max_tokens=4096)
        return dict(out, mode="vision", fallback="")

    # ---- 评分 ----
    def score(self, brief, rev, rep):
        d = codegen.score(brief, rev, rep)
        fidm = rep.get("fidelity") or {}
        fid = (fidm.get("score") or 0) / 100.0
        tab = rep.get("tablet") or {}
        f = d["facts"]
        alive = not f["frozen"]
        health = (0.25 * (alive and not f["errors"]) + 0.15 * (alive and not f["blank"]) + 0.1 * (not f["external"])
                  + 0.15 * (not f["overflow_desktop"]) + 0.15 * (not tab.get("overflow_x")) + 0.15 * (not f["overflow_mobile"])
                  + 0.05 * (not f["overlap"]))
        raw = 100.0 * (0.4 * fid + 0.3 * d["coverage"] + 0.15 * health + 0.15 * d["design"])
        caps = []
        if f["errors"] or f["blank"] or f["frozen"]:
            caps.append(("运行报错 / 空白 / 卡死", 40.0))
        if any(i["must"] and i["verdict"] == "no" for i in d["items"]):
            caps.append(("有必须项未满足", 60.0))
        if rev is None:
            caps.append(("未经模型验收", 70.0))
        final = min([raw] + [c for _, c in caps])
        d.update(score=round(final, 1), scoring=SCORING, raw=round(raw, 1), health=round(health, 3),
                 fidelity=fidm, caps=[{"reason": r, "max": c} for r, c in caps if raw > c])
        return d

    def problems(self, defects, detail, rep):
        out = codegen.problem_list(defects, detail, limit=12)
        hints = ((rep or {}).get("fidelity") or {}).get("hints") or []
        if hints:
            out.append("[像素对比] 与原图差异最大的区域: " + "; ".join(hints[:5]))
        return out[:14]

    def preview_png(self, rep, rdir):
        fid = (rep.get("shots") or {}).get("fid")
        return Path(fid) if fid and Path(fid).is_file() else rdir / "full.png"


def run(src: Path, hint: str, ws: Path, pool, ai_cfg: dict, judge_cfg: dict, log, stage,
        deadline: float, target_score: float, max_rounds: int, on_brief=None) -> dict:
    prof = ShotProfile(ws, src, hint, log)
    out = codegen.run(hint or "照截图还原界面", ws, pool, ai_cfg, judge_cfg, dict(settings.GEN_DEFAULT_VIEWPORT), log, stage,
                      deadline, target_score, max_rounds, profile=prof, on_brief=on_brief)
    out["prep"] = {k: prof.prep.get(k) for k in ("css_w", "css_h", "dpr", "crop", "src_size")}
    out["assets"] = prof.prep.get("assets") or []
    return out
