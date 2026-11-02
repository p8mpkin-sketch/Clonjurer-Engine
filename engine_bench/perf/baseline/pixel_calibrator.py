"""
A1 完整版: 像素差分 → 元素映射 → CSS 覆盖合成(确定性校准循环)。

三步:
  ①网格差分: 原站/克隆截图分格比对, 定位色差最大的区域(带均匀性守卫:
    仅取原站侧近似纯色的格子——纯色区域才适合确定性上色, 图案/内容格跳过);
  ②元素映射: 在回放页对每个差分格中心做 elementFromPoint, 取元素路径
    (tag#id.class×2 层级), 纯 DOM 证据, 无任何站点名特判;
  ③CSS 合成: 以原站格子色为目标生成 path{background-color} 覆盖, 注入
    克隆页后重渲染重评分 —— 校准闭环。
"""
from PIL import Image, ImageStat

GRID_X, GRID_Y = 24, 14
CELL_UNIFORM_VAR = 60      # 格子内方差低于此 = 近纯色区域
MAX_CELL_DIST = 26         # 色差超过此值视为 divergence
TOP_CELLS = 8              # 每轮最多校准的格子数


def _cell_stats(im, box):
    """格子中位色 + 均匀度(0=完全均匀)。box 越界自动钳制。

    ImageStat 在 C 层统计(旧版逐像素 Python 累加, 336 格 × 2 图为校准主耗时)。"""
    w, h = im.size
    x0, y0, x1, y1 = max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3])
    if x1 <= x0 or y1 <= y0:
        return (0, 0, 0), 999
    st = ImageStat.Stat(im.crop((x0, y0, x1, y1)))
    var = sum(st.var[:3]) / 3
    return tuple(int(m) for m in st.mean[:3]), max(var, 0.0) ** 0.5


def divergent_cells(orig_path, clone_path):
    """网格差分: 返回 [(cx_ratio, cy_ratio, orig_rgb, dist)], 差异降序。

    守卫: 仅取原站侧近纯色格子(均匀度 < CELL_UNIFORM_VAR)——
    内容密集格的差异多来自图片/文案时变, 确定性上色会破坏真实内容。"""
    a = Image.open(orig_path).convert('RGB')
    b = Image.open(clone_path).convert('RGB')
    if a.size != b.size:
        b = b.resize(a.size)
    w, h = a.size
    if w < GRID_X * 20 or h < GRID_Y * 20:
        return []
    cw, ch = w // GRID_X, h // GRID_Y
    out = []
    for gy in range(GRID_Y):
        for gx in range(GRID_X):
            box = (gx * cw, gy * ch, (gx + 1) * cw, (gy + 1) * ch)
            ca, va = _cell_stats(a, box)
            if va > CELL_UNIFORM_VAR:
                continue   # 原站格非纯色(内容/图), 跳过
            cb, _ = _cell_stats(b, box)
            d = sum((ca[i] - cb[i]) ** 2 for i in range(3)) ** 0.5
            if d > MAX_CELL_DIST:
                out.append((round((gx + 0.5) / GRID_X, 4),
                            round((gy + 0.5) / GRID_Y, 4), ca, round(d, 1)))
    out.sort(key=lambda x: -x[3])
    return out[:TOP_CELLS]


ELEMENT_PROBE_JS = """(cells) => {
    const out = [];
    for (const [cx, cy, rgb] of cells) {
        const x = Math.round(cx * document.documentElement.scrollWidth);
        const y = Math.round(cy * document.documentElement.scrollHeight);
        window.scrollTo(0, Math.max(0, y - innerHeight / 2));
        const el = document.elementFromPoint(
            Math.min(x, innerWidth - 2), y - window.scrollY);
        if (!el) { out.push(null); continue; }
        // 自底向上收集 3 层, 再反转为"祖先 > 子"顺序(旧实现未反转, 选择器
        // 方向颠倒几乎从不命中); 用子代组合符 + nth-of-type 精确定位,
        // 避免 tag.class 后代链误伤同类元素; 标识符经 CSS.escape(不可信输入)
        const esc = (v) => (window.CSS && CSS.escape) ? CSS.escape(v)
                                                       : String(v).replace(/[^\\w-]/g, '');
        const path = [];
        let e = el, guard = 0;
        while (e && e !== document.body && e !== document.documentElement && guard < 3) {
            let sel = e.tagName.toLowerCase();
            if (e.id) {
                sel += '#' + esc(e.id);
                path.push(sel);
                break;   // id 已唯一锚定, 无需继续上溯
            }
            if (e.className && typeof e.className === 'string' && e.className.trim()) {
                sel += e.className.trim().split(/\\s+/).slice(0, 2)
                        .map(c => '.' + esc(c)).join('');
            }
            const parent = e.parentElement;
            if (parent) {
                let idx = 1, sib = e;
                while ((sib = sib.previousElementSibling)) {
                    if (sib.tagName === e.tagName) idx += 1;
                }
                sel += ':nth-of-type(' + idx + ')';
            }
            path.push(sel);
            e = parent; guard += 1;
        }
        out.push({selector: path.reverse().join(' > '), rgb});
    }
    return out;
}"""


def element_css(page, cells):
    """在回放页做元素映射, 返回 CSS 覆盖规则文本。"""
    # 注意: evaluate 的 arg 原样作为唯一参数传入(旧版 `([cells]) =>` 解构
    # 只取到第一个格子, 随即在数字上迭代抛 TypeError —— 校准从未生效)
    payload = [[c[0], c[1], list(c[2])] for c in cells]
    mapped = page.evaluate(ELEMENT_PROBE_JS, payload) or []
    rules = []
    for m in mapped:
        if not m or not m.get('selector'):
            continue
        sel = m['selector']
        # 纵深防御: 选择器来自目标站 DOM, 禁止可闭合 <style> 或注入规则的字符
        if any(ch in sel.replace(' > ', '') for ch in '<>{};'):
            continue
        r, g, b = (max(0, min(255, int(v))) for v in m['rgb'])
        rules.append(f"{sel}{{background-color:rgb({r},{g},{b})!important}}")
    return '\n'.join(dict.fromkeys(rules))
