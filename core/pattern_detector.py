"""
框架模式识别器(W1): JS 字节 + 渲染 DOM 双指纹 → 模式判定。

- 指纹规则与 103 站普查(engine_bench/pattern_census.json)同源, 保证口径一致;
- 采集期在静态资源落盘后调用, 识别结果写入 site.json 并路由到
  core/strategy_packs 的模式策略包 —— 一类一策, 严禁 per-site 特判;
- 证据只取源站真实内容: 剔除引擎注入的 interaction-sim 脚本,
  JS 字节证据优先(URL 本地化会抹掉库名, 字节不会撒谎)。
"""
import re
from pathlib import Path

# 引擎注入物(不属于源站指纹)
_INJECT_RE = re.compile(r'<script data-clone-preview[^>]*>.*?</script>', re.S)

# JS 字节级指纹(库标识字符串藏在 chunk 里, 本地化不影响)
_JS_FP = {
    "Vue":        [rb'Vue\.js v\d', rb'@vue/', rb'__vue__'],
    "VueMaterial": [rb'vue-material'],
    "React":      [rb'react-dom', rb'react\.production', rb'ReactDOM'],
    "Next":       [rb'__NEXT_DATA__', rb'next/dist'],
    "Nuxt":       [rb'__NUXT__', rb'nuxt'],
    "Angular":    [rb'zone\.js', rb'ng-version', rb'@angular'],
    "jQuery":     [rb'jQuery JavaScript Library v', rb'jquery'],
    "Webpack":    [rb'webpackJsonp', rb'__webpack_require__'],
    "Vite":       [rb'__vite__', rb'vite/preload'],
}

# 渲染 DOM 指纹(框架运行时留在 DOM 上的痕迹, 本地化后依然存在)
_DOM_FP = {
    "Vue":        [r'data-v-[0-9a-f]{8}', r'data-v-app'],
    "VueMaterial": [r'md-theme-default'],
    "React":      [r'data-reactroot', r'id="___gatsby"'],
    # SSR 框架挂载点/资源路径: 渲染 DOM 里的结构性痕迹(JS 字节被拆包时的第二证据)
    "Next":       [r'id="__next"', r'/_next/static/', r'id="__NEXT_DATA__"'],
    "Nuxt":       [r'id="__nuxt"', r'/_nuxt/', r'data-n-head'],
    "Angular":    [r'ng-version', r'_nghost-[a-z0-9-]+', r'_ngcontent-[a-z0-9-]+'],
    "SSR水合":     [r'__INITIAL_STATE__'],
    "iframe门户":  [r'<iframe[ >]'],
    "视频":        [r'<video[ >]'],
    "Geetest":    [r'static\.geetest\.com|initGeetest'],
}

# ---- 快速匹配器(与 re.search(p, data, re.I) 逐值等价) ----------------------
# 旧实现对每个文件 × 每条指纹做 re.I 全量扫描: IGNORECASE 使 sre 无法用字面前缀
# 快速查找, 2MB × 20 条 ≈ 130ms/文件。新实现先把数据转小写一次, 纯字面指纹用
# `in`(C 层子串查找), 其余用小写化后的模式(无 re.I, 恢复前缀加速)。
#  - bytes: re.I 只做 ASCII 大小写折叠, bytes.lower() 也只改 ASCII, 严格等价;
#  - str: Unicode re.I 有 4 个非 ASCII 字符会与 ASCII 字母互配(İ ı ſ K, 其
#    str.lower() 也会产出 ASCII), 数据含其中任一字符时整体回落旧路径。
_UNICODE_FOLD_TRAPS = frozenset("\u0130\u0131\u017f\u212a")


def _literal_of(pattern):
    """模式若只由字面量组成则返回该字面量(同类型), 否则 None。"""
    try:
        items = list(re._parser.parse(pattern))
    except Exception:   # noqa: BLE001  私有解析器不可用/解析失败 → 当正则处理
        return None
    if not items or any(op is not re._constants.LITERAL for op, _ in items):
        return None
    codes = [v for _, v in items]
    return bytes(codes) if isinstance(pattern, bytes) else "".join(map(chr, codes))


def _lowered_matcher(pattern):
    """返回 fn(lowered_data) -> bool; 无法安全小写化时返回 None。"""
    lit = _literal_of(pattern)
    if lit is not None:
        lit = lit.lower()
        return lambda data: lit in data
    upper_escape = rb"\\[A-Z]" if isinstance(pattern, bytes) else r"\\[A-Z]"
    if re.search(upper_escape, pattern):   # \D \S \W \B 等小写化会变义
        return None
    try:
        rx = re.compile(pattern.lower())
    except re.error:
        return None
    return lambda data: rx.search(data) is not None


def _build(fp):
    table = {}
    for name, pats in fp.items():
        compiled = [re.compile(p, re.I) for p in pats]
        fast = [_lowered_matcher(p) for p in pats]
        table[name] = (compiled, fast if all(f is not None for f in fast) else None)
    return table


def _hit(entry, data, lowered):
    compiled, fast = entry
    if fast is not None and lowered is not None:
        return any(f(lowered) for f in fast)
    return any(rx.search(data) for rx in compiled)


_JS_SCAN_BUDGET = 12_000_000   # 单任务 JS 字节扫描上限(框架标识可能藏在任意 chunk)
_JS_PER_FILE = 2_000_000


def _scan(workspace: Path, html: str = None) -> tuple:
    """返回 (全部命中, JS 字节命中, DOM 命中)。

    html: 采集期渲染 DOM(权威证据)。旧版只读工作区 index.html —— 但识别在
    本地化之前执行, 新任务此时 index.html 尚不存在(重建时则是上一轮的陈旧
    文件; 目录型 URL 时是跳转桩), DOM 指纹实际从未参与首次识别。"""
    js_hits, dom_hits = set(), set()
    total = 0
    jsdir = workspace / 'static' / 'js'
    if jsdir.is_dir():
        # 大文件优先: 框架标识集中在 vendor/framework chunk, 预算内先扫它们
        files = []
        for js in jsdir.glob('*.js'):
            try:
                files.append((js.stat().st_size, js.name, js))
            except OSError:
                continue
        for _size, _name, js in sorted(files, key=lambda t: (-t[0], t[1])):
            if total > _JS_SCAN_BUDGET or len(js_hits) == len(_JS_FP):
                break
            try:
                data = js.read_bytes()[:_JS_PER_FILE]
            except OSError:
                continue
            total += len(data)
            lowered = data.lower()
            for name, entry in _JS_TABLE.items():
                if name not in js_hits and _hit(entry, data, lowered):
                    js_hits.add(name)
    if html is None:
        idx = workspace / 'index.html'
        if idx.is_file():
            try:
                html = idx.read_text(encoding='utf-8', errors='replace')
            except OSError:
                html = ''
    html = _INJECT_RE.sub('', html or '')
    lowered = None if any(ch in html for ch in _UNICODE_FOLD_TRAPS) else html.lower()
    for name, entry in _DOM_TABLE.items():
        if _hit(entry, html, lowered):
            dom_hits.add(name)
    return js_hits | dom_hits, js_hits, dom_hits


_JS_TABLE = _build(_JS_FP)
_DOM_TABLE = _build(_DOM_FP)


def classify(hits) -> str:
    """指纹集合 → 模式名(与普查口径一致)。"""
    s = set(hits)
    # SPA 字节证据优先于极验(现代登录页也常挂 Geetest)
    if "Next" in s:
        return "M-Next SSR"
    if "Nuxt" in s:
        return "M-Nuxt SSR"
    if "VueMaterial" in s:
        return "M-Vue SPA(Material)"
    if "Vue" in s or "Vite" in s:
        return "M-Vue/Vite SPA"
    if "React" in s:
        return "M-React SPA"
    if "Angular" in s:
        return "M-Angular SPA"
    if "Webpack" in s or "SSR水合" in s:
        return "M-Webpack水合MPA"
    if "Geetest" in s:
        return "M-AUTH 传统登录页(jQuery+极验)"
    if "jQuery" in s:
        return "M-jQuery传统MPA" + ("+iframe门户" if "iframe门户" in s else "")
    return "M-STATIC 纯静态"


_FRAMEWORKS = {"Vue", "VueMaterial", "React", "Next", "Nuxt", "Angular", "Vite", "Webpack"}

# 模式 → 决定它的关键证据(用于置信度: 关键证据被 JS+DOM 双源印证则更可信)
_KEY_EVIDENCE = {
    "M-Next SSR": {"Next"}, "M-Nuxt SSR": {"Nuxt"},
    "M-Vue SPA(Material)": {"VueMaterial"}, "M-Vue/Vite SPA": {"Vue", "Vite"},
    "M-React SPA": {"React"}, "M-Angular SPA": {"Angular"},
    "M-Webpack水合MPA": {"Webpack", "SSR水合"},
    "M-AUTH 传统登录页(jQuery+极验)": {"Geetest"},
}


def detect(workspace: Path, html: str = None) -> dict:
    """识别框架模式。返回 {pattern, features, confidence, evidence}。

    html 传入采集期渲染 DOM 时以其为 DOM 证据(推荐); 缺省回落工作区 index.html。"""
    hits, js_hits, dom_hits = _scan(workspace, html)
    pattern = classify(hits)
    key = _KEY_EVIDENCE.get(pattern, set())
    if key & js_hits and key & dom_hits:
        confidence = 0.97          # 关键证据 JS 字节 + 渲染 DOM 双源印证
    elif hits & _FRAMEWORKS:
        confidence = 0.9
    elif "jQuery" in hits:
        confidence = 0.75
    else:
        confidence = 0.6   # 纯静态判定的置信度最低(可能是指纹未覆盖)
    return {"pattern": pattern, "features": sorted(hits), "confidence": confidence,
            "evidence": {"js": sorted(js_hits), "dom": sorted(dom_hits)}}
