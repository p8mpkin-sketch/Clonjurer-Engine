"""
框架模式识别器(W1): JS 字节 + 渲染 DOM 双指纹 → 模式判定。

- 指纹规则来自站点普查经验(普查原始数据不随仓库分发), 一类一策;
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
    # 第二批框架/CMS(W1.1): 字面量均取框架运行时/构建产物的专有标识, 避免单词级误触
    "SvelteKit":  [rb'__sveltekit', rb'@sveltejs/kit', rb'/_app/immutable/'],
    "Svelte":     [rb'svelte/internal', rb'__svelte', rb'svelte-'],
    "Astro":      [rb'astro-island', rb'/_astro/', rb'astro:'],
    "Remix":      [rb'__remixContext', rb'__remixManifest', rb'@remix-run/'],
    "Solid":      [rb'solid-js', rb'_\$HY'],
    "Qwik":       [rb'@builder\.io/qwik', rb'qwikloader'],
    "Ember":      [rb'ember-source', rb'Ember\.VERSION', rb'@ember/'],
    "WordPress":  [rb'/wp-content/', rb'/wp-includes/'],
    "Shopify":    [rb'cdn\.shopify\.com', rb'Shopify\.theme'],
    "Wix":        [rb'parastorage\.com', rb'wixstatic\.com'],
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
    "SvelteKit":  [r'data-sveltekit-', r'/_app/immutable/'],
    "Svelte":     [r'class="[^"]*\bsvelte-[a-z0-9]{4,}'],
    "Astro":      [r'<astro-island', r'data-astro-cid-', r'/_astro/'],
    "Remix":      [r'__remixContext', r'data-remix-'],
    "Solid":      [r'data-hk="'],
    "Qwik":       [r'q:container', r'type="qwik/json"'],
    "Ember":      [r'class="ember-application', r'id="ember\d+"'],
    "WordPress":  [r'/wp-content/', r'/wp-includes/', r'content="WordPress'],
    "Shopify":    [r'cdn\.shopify\.com', r'Shopify\.theme'],
    "Wix":        [r'parastorage\.com', r'wixstatic\.com'],
}
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
            for name, pats in _JS_FP.items():
                if name not in js_hits and any(re.search(p, data, re.I) for p in pats):
                    js_hits.add(name)
    if html is None:
        idx = workspace / 'index.html'
        if idx.is_file():
            try:
                html = idx.read_text(encoding='utf-8', errors='replace')
            except OSError:
                html = ''
    html = _INJECT_RE.sub('', html or '')
    for name, pats in _DOM_FP.items():
        if any(re.search(p, html, re.I) for p in pats):
            dom_hits.add(name)
    return js_hits | dom_hits, js_hits, dom_hits


def classify(hits) -> str:
    """指纹集合 → 模式名(与普查口径一致)。

    判定次序 = 证据的专有性: SSR/元框架(Next/Nuxt/Remix/SvelteKit/Astro/Qwik)
    先于其宿主 UI 库, UI 库(Vue/React/Solid/Angular/Ember)先于打包器(Vite/
    Webpack), 打包器先于 CMS/jQuery。Vite 只是构建器证据: 有 React 时判 React,
    仅在没有任何 UI 库证据时才按 Vue 生态先验回落到 Vue/Vite。"""
    s = set(hits)
    # SPA 字节证据优先于极验(现代登录页也常挂 Geetest)
    if "Next" in s:
        return "M-Next SSR"
    if "Nuxt" in s:
        return "M-Nuxt SSR"
    if "Remix" in s:
        return "M-Remix SSR"
    if "SvelteKit" in s:
        return "M-SvelteKit SSR"
    if "Astro" in s:
        return "M-Astro 静态岛"
    if "Qwik" in s:
        return "M-Qwik 可恢复"
    if "VueMaterial" in s:
        return "M-Vue SPA(Material)"
    if "Vue" in s:
        return "M-Vue/Vite SPA"
    if "React" in s:
        return "M-React SPA"
    if "Svelte" in s:
        return "M-Svelte SPA"
    if "Solid" in s:
        return "M-Solid SPA"
    if "Angular" in s:
        return "M-Angular SPA"
    if "Ember" in s:
        return "M-Ember SPA"
    if "Vite" in s:
        return "M-Vue/Vite SPA"
    if "WordPress" in s:
        return "M-WordPress CMS"
    if "Shopify" in s:
        return "M-Shopify CMS"
    if "Wix" in s:
        return "M-Wix CMS"
    if "Webpack" in s or "SSR水合" in s:
        return "M-Webpack水合MPA"
    if "Geetest" in s:
        return "M-AUTH 传统登录页(jQuery+极验)"
    if "jQuery" in s:
        return "M-jQuery传统MPA" + ("+iframe门户" if "iframe门户" in s else "")
    return "M-STATIC 纯静态"


_FRAMEWORKS = {"Vue", "VueMaterial", "React", "Next", "Nuxt", "Angular", "Vite", "Webpack",
               "SvelteKit", "Svelte", "Astro", "Remix", "Solid", "Qwik", "Ember"}
_CMS = {"WordPress", "Shopify", "Wix"}

# 模式 → 决定它的关键证据(用于置信度: 关键证据被 JS+DOM 双源印证则更可信)
_KEY_EVIDENCE = {
    "M-Next SSR": {"Next"}, "M-Nuxt SSR": {"Nuxt"},
    "M-Remix SSR": {"Remix"}, "M-SvelteKit SSR": {"SvelteKit"},
    "M-Astro 静态岛": {"Astro"}, "M-Qwik 可恢复": {"Qwik"},
    "M-Vue SPA(Material)": {"VueMaterial"}, "M-Vue/Vite SPA": {"Vue", "Vite"},
    "M-React SPA": {"React"}, "M-Svelte SPA": {"Svelte"}, "M-Solid SPA": {"Solid"},
    "M-Angular SPA": {"Angular"}, "M-Ember SPA": {"Ember"},
    "M-WordPress CMS": {"WordPress"}, "M-Shopify CMS": {"Shopify"}, "M-Wix CMS": {"Wix"},
    "M-Webpack水合MPA": {"Webpack", "SSR水合"},
    "M-AUTH 传统登录页(jQuery+极验)": {"Geetest"},
}


def _confidence(pattern: str, hits: set, js_hits: set, dom_hits: set) -> float:
    """置信度随证据数量与来源数单调上升, 而非四档常量。

    - 关键证据 JS 字节 + 渲染 DOM 双源印证: 0.97;
    - 框架/CMS 单源: 0.85 起, 每多一条旁证 +0.02, 封顶 0.93;
    - jQuery: 0.75; 纯静态: 0.6(无 JS 证据、仅凭 DOM 判静态时 0.5——
      很可能只是指纹未覆盖)。"""
    key = _KEY_EVIDENCE.get(pattern, set())
    if key & js_hits and key & dom_hits:
        return 0.97
    if hits & _FRAMEWORKS or hits & _CMS:
        extra = max(0, len(hits & (_FRAMEWORKS | _CMS)) - 1)
        return round(min(0.93, 0.85 + 0.02 * extra), 2)
    if "jQuery" in hits:
        return 0.75
    return 0.6 if js_hits or dom_hits else 0.5


def detect(workspace: Path, html: str = None) -> dict:
    """识别框架模式。返回 {pattern, features, confidence, evidence}。

    html 传入采集期渲染 DOM 时以其为 DOM 证据(推荐); 缺省回落工作区 index.html。"""
    hits, js_hits, dom_hits = _scan(workspace, html)
    pattern = classify(hits)
    confidence = _confidence(pattern, hits, js_hits, dom_hits)
    return {"pattern": pattern, "features": sorted(hits), "confidence": confidence,
            "evidence": {"js": sorted(js_hits), "dom": sorted(dom_hits)}}
