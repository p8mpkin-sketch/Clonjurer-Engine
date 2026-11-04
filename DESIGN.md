# Clonjurer-Engine · 设计与核心能力

本文说明引擎的整体设计、每个算法子系统的原理，以及「效率 / 完成度」的量化口径。目标读者是要继续优化这套引擎的人。

---

## 1. 总体设计：一次采集，双轨交付

引擎的核心主张是 **DOM 权威双轨制**：采集期对页面做一次活体渲染，产出两个交付物，评分时分别度量、取优。

- **保真轨**：渲染后的快照（结构由构造保证，确定性强），度量「长得像不像」。
- **交互轨**：本地化后的离线 Flask 工程，度量「能不能用」。

这样避免了「让站点 JS 在离线重执行出原站渲染」这条高失败率路径——还原度由采集期的渲染结果与确定性本地化保证，而不是赌离线重放。

管线分段（`core/cloner_engine.py` 的 `run()` → `_execute()`）：

```
采集渲染 → 资产下载/本地化 → 离线工程生成 → 截图 → 还原度评分 → 像素校准 → (可选) AI 视觉修复
```

真实端到端耗时的大头是浏览器渲染/滚动/稳定等待、全页截图、资产下载与（启用时的）AI 修复；纯计算算法（评分/改写/识别/校准）只占一小片。因此「训练效率」既要看微基准，也要看端到端。

### 1.1 采集期固化的三条不变量（真实语料 2026-11-02 归因）

- **样式表的媒体条件随内联保留**：CSSOM 序列化把每张 `document.styleSheets` 内联成 `<style data-cssom>` 时，`media` 非 `all` 的表包进 `@media … {}`，`disabled` 的表跳过。否则打印样式（常见 `.header,.footer{display:none!important}`）在屏幕生效，页眉页脚整块消失（wordpress.org 实证 28 分，两轨同病）。
- **构造样式表要单独序列化**：`document.adoptedStyleSheets`（CSS-in-JS 运行时 / Vite 注入）不在 `document.styleSheets`，outerHTML 与 MHTML 都带不走；按序追加为 `<style data-cssom="adopted">`（文档级采纳表级联在普通表之后）。remix.run 实证：两轨整页无样式 34 分。
- **水合剥离在落盘前兜底**：活体阶段只能早识别 Next/Nuxt；模式识别发生在资源落盘之后，策略包 `disable_hydration` 由 `_localize` 的 `finalize` 用 `_strip_hydration_html` 落地（JSON 数据脚本与 `data-clone-*` 注入物保留）。solidjs.com / emberjs.com 实证：离线水合把整页再渲染一遍，克隆页长度翻倍。

## 2. 还原度评分（诚实相似度 v3）

对「原站参考图」与「克隆截图」两张图打分，分三条通道，可解释、可复算。

### 2.1 图像预处理
- 两张图各自等比缩放到固定宽 512（`_fit_width` = 单次 `PIL.Image.resize`）。
  - **重要教训**：曾把这步手工拆成「横向分带 + 纵向分带」并行提速，假设与 PIL 单次 resize 逐字节相同；但 PIL 会按长宽比在内部自选两趟先后次序且随版本变化，超长页上偏差可达 8/通道，破坏逐值等价闸门。已回退单次 resize（见 §6）。
- 未命中缓存的图并行解码；参考图侧的中间量（NCC 去均值块、边缘掩码、缩放结果）按**文件内容 sha1** 缓存，修复循环里每任务评分 4–8 次而参考图不变，命中即省去整页 PNG 解码。

### 2.2 结构通道
- **块级归一化互相关（NCC）**：512 宽灰度图切 32px 块，逐块做去均值归一化互相关；双平坦且相近记高分，一方有结构一方平坦记低分。
- **边缘 IoU**：numpy 复刻 PIL `FIND_EDGES`（1px 外框保留原值）+ `MaxFilter`（边缘复制扩边）的边界语义后，对边缘掩码求交并比。

### 2.3 文本通道（F2 指纹）
- 只计算 |A|、|B|、|A∩B| 三个数：CJK 二元组编码成整数后用 numpy 去重求交，拉丁词用字节翻译表分词；原站文本只算一次。
- 以 β=2 的 F 值（偏召回）衡量文本覆盖。

### 2.4 差异热力图
- numpy 生成逐像素差异图，分带后分片 deflate 拼成一个合法 zlib 流（pigz 同款做法）直接写 PNG。此图是**诊断用**，不参与分数。

### 2.5 双轨取优
保真轨与交互轨各自算一套 structure/edge/text，任务分取两轨最大值；原因与边界情况见各模块注释。

## 3. 本地化改写

把采集到的 HTML/JS/CSS 中指向原站的引用改写为本地资源。

- **前缀树正则替换表**（`_trie_regex` / `_replacement_matcher`）：上千条 URL 变体不再用扁平交替 `k1|k2|...` 逐个试配，而是编译成前缀树正则，每个位置只沿一条字符路径前进；语义仍是「最左起点取最长命中」。编译结果按替换表缓存，多页面/多 JS 共用。
- **导航逃逸改写**（`_anchored_sub` / `rewrite_js_navigation`）：`top.location`/`parent.location` 等以交替开头、无法用前缀加速；改为先 `str.find` 定位字面锚点、只在锚点处匹配，与 `re.sub` 的「最左、非重叠」语义一致。
- **CSS / srcset**：三条引用正则改锚点扫描；已收录引用先查重，免重复 `urljoin`。
- **URL 快速路径**：严格形态的绝对 http(s) URL（小写 scheme、ASCII、无 `; #`、空白控制字符）走轻量 `urlsplit`，其余回落 `urllib`。

## 4. 框架模式识别（`pattern_detector.py`）

- 指纹两批：第一批 Vue/React/Next/Nuxt/Angular/jQuery/Webpack/Vite；第二批（W1.1）SvelteKit/Svelte/Astro/Remix/Solid/Qwik/Ember 与 WordPress/Shopify/Wix，每个模式在 `strategy_packs` 里有对应策略包。
- 判定次序 = 证据的专有性：SSR/元框架 → CMS/建站平台 → UI 库 → 打包器 → jQuery。CMS 的证据是页面自身资源路径（`/wp-content/`、Shopify 文件 CDN、parastorage），决定资源布局，先于其上的 UI 库（Shopify 主题带 Vue、Wix 自家站是 React，真实语料里都曾被 UI 库抢先判错）。Vite 只是打包器证据：有 React 时判 React，仅在没有任何 UI 库证据时才按 Vue 生态先验回落。
- 指纹只取运行时/构建产物的专有标识，**不用裸词**：`nuxt`、`svelte-`、`solid-js`、`astro:`、`jquery`、`@angular` 这类会被任何文档站正文命中（真实语料里 vitejs.dev / cn.vuejs.org / emberjs.com / techcrunch 全被判成 Nuxt，识别命中 10/26）；改为 `/_nuxt/`、`svelte-h[a-z0-9]{4,}`、`solid-js/web`、`astro:page-load`、`jquery.min.js`、`@angular/core`。
- 置信度随证据数量与来源数单调上升：关键证据 JS+DOM 双源 0.97；框架/CMS 单源 0.85 起每多一条旁证 +0.02（封顶 0.93）；jQuery 0.75；纯静态 0.6（完全无证据 0.5）。
- JS 字节与渲染 DOM 各转一次小写；纯字面量指纹用 `in`（C 层子串查找），其余走小写化后的正则，避免 `re.I` 全量扫描。
- 边界：Unicode 下 `re.I` 会让 İ、ı、ſ、K 与 ASCII 字母互配，DOM 含这些字符时整体回落旧路径（`_UNICODE_FOLD_TRAPS`）。

## 5. 像素校准（`pixel_calibrator.py`）

- 把图切成网格（如 336 格），一次向量化求每格均值/方差：行内用 float32 累加（每行部分和 < 2²⁴，结果精确），跨行用 float64，套 ImageStat 同一公式，结果逐位一致。
- 参考图格子统计按内容哈希缓存；原站侧全是内容格时克隆图不必解码。
- 克隆图格子统计由评分阶段顺带预热（`compute_visual_similarity(prime_calibration=True)` → `pixel_calibrator.prime_grid`，同一 sha1 键），校准阶段不再对同一张图二次整页解码；预热结果与校准自行解码逐值一致（`CalibrationPrimeEquivalence`）。

## 6. 效率与可复现性

### 6.1 0–100 速度评分
- 基线：`engine_bench/perf/baseline/` 冻结的优化前代码，与当前代码在同一进程、同一夹具上交替运行，机器噪声对两边相同；每项取多次运行最小耗时。
- 单项：`100 × clamp(ln 提速 / ln 3, 0, 1)`（1x=0、1.73x=50、3x=100）。
- 总分：按权重加权平均（权重按真实任务中的调用频次与耗时占比）。
- **正确性闸门**：任一项输出与基线不一致则该项记 0；单元测试不过则总分 0。

### 6.2 逐值等价测试
`tests/test_perf_equivalence.py` 把每个被优化的函数与冻结基线做差分模糊测试（导航改写、替换表、CSS、URL 快速路径、随机尺寸/模式的截图评分明细与热力图像素、校准、模式识别等），覆盖 Unicode 折叠陷阱、超长键、同路径改写后缓存失效、热力图 PNG 严格校验等边界。

### 6.3 底层库钉版（关键）
逐值闸门以「与冻结基线逐字节一致」为判据，而部分优化复刻了 PIL/numpy 的**浮点内部行为**，其字节输出随库版本会变。因此：
- `requirements.txt` 钉 `pillow~=13.0.0` / `numpy~=2.5.0`；
- 原则：复刻**整数/确定性**算法安全（边缘 IoU 整数卷积、文本整数编码、校准 float32 精确累加均对实时 PIL 零像素差）；复刻**库浮点内部字节输出**脆弱——要么不做，要么钉死库版本。
- 升级库时作为独立变更：先升版 → 跑 `loop.py` → 若某项逐值漂移，重新冻结 `baseline/` 或放宽该项为容差判据。

### 6.4 已知地板与端到端优化
- 截图评分剩余耗时以克隆图 PNG 解码为主（单图不可再分块并行），长页评分已接近「解码 + 单次缩放」地板。
- 端到端层面（微基准不体现）已做：
  - **自适应缓释**：沙盒渲染/校准重渲染/保真轨截图前用 `MutationObserver` 等 DOM 静置（连续 `settle_quiet_ms`=500ms 无变动且图片已 complete 即走），`settle_ms` 只是上限；恒定动画的页面退化为等满上限，与旧固定等待一致。
  - **下载流式调度**：`_materialize` 用 `FIRST_COMPLETED` 流式提交/收割，CSS 递归发现的新引用立即入队，慢资源不再阻塞整批（旧实现按 32 个一批 `executor.map`，每批等最慢者）。
  - **校准免二次解码**：见 §5。
  - **热力图延迟**：修复循环的中间轮不写 `diff_preview.png`，收敛后 `write_diff_preview` 用缓存的缩放结果生成一次（像素与同步生成逐值一致，`DeferredDiffEquivalence`）。

## 7. AI 视觉修复（`ai_vision_repair.py`）

对照原站截图与克隆截图，调用多模态模型生成针对性的 CSS/HTML 补丁并注入离线工程，再重打包、重评分，形成「评分→修复→重评分」的闭环，用来提升还原度「完成度」。无 API Key 时该功能自动跳过，不影响其余管线。

- **区域线索**：提示词附带像素差分（`pixel_calibrator.divergent_cells`）定位的差异格（位置百分比、原站色、色差）、高度比与差异热力图（第 3 张图），要求模型只修列出的区域。
- **回滚守卫**：每轮补丁前快照页面/`ai_patch.css`/截图/文本，复评不高于补丁前即整体恢复——与像素校准同一套「必须净提升」语义，坏补丁不会留在产物里。
- **补丁消毒**：剥离外链 `url()`、`@import`、`expression()`、`<script>`、内联事件与外链引用；离线产物不得回源。
- 补丁落到真实被渲染的页面（目录型 URL 时是 `page_dir` 下的影子页，而非根目录的跳转桩）。

## 8. 并发与安全边界

- 评分使用进程级小线程池：缩放结果最多 4 张、参考图中间量最多 2 份、校准统计最多 8 份，键都是文件内容 sha1；所有池任务都是叶子任务，调度器多 worker 并发评分不会死锁。
- 本仓库仅保留通用引擎算法与本地测试能力，完整平台中的运营类功能不在本仓库范围内。

---

## 9. 通用 UI 重建引擎(L1/L2/L3)

本章记录引擎从「Web 界面重建」演进为「通用 UI 重建」的架构基线。核心主张:**一切输入归一化为统一中间表示 UIR,一切输出目标从 UIR 渲染**;新能力只能以 InputAdapter / OutputTarget 形式进入,不另起炉灶,Web 侧能力零回归。

### 9.1 定位与授权边界

- **能力分层**:L1 截图/描述 → 可运行网站(生成式);L2 自有/授权 APK 的公开资源 → 跨平台应用(RN/Flutter);L3 桌面应用界面重建(Electron/Tauri,仅设计)。
- **授权边界(硬约束)**:只重建界面「外观 + 交互意图」。L2 仅解析**自有/授权**安装包的**公开资源**(布局 XML / 图片 / 文案),**不触碰 DEX/so、不脱壳、不绕过任何保护机制**,不提取第三方程序的内部实现代码。遇加固/资源加密包即判「静态不可用」,退回「需运行期授权采集」建议,不做破解尝试。
- **工程铁律(延续克隆侧)**:严禁 per-site/per-app 特判;确定性算法优先、LLM 兜底;评分 v3 口径不变,直接泛化为生成质量奖励函数;engine_bench 语料+留出集跑分不得下降。

### 9.2 三个关键取舍

1. **组件粒度 = 语义组件为主 + `box` 原语兜底(混合)**。语义节点(nav/hero/card/list/button/input…)做主干,保留能表达任意容器的 `box` 原语做逃生舱。Web 适配器可整树退化为 `box`→保证零回归;视觉侧只给得出区域+角色→用语义节点。评分 v3 是像素级奖励,粒度不必对齐源 DOM,只要渲染像素对得上——粒度自由度由此解锁。
2. **样式表达 = 全局设计令牌表 + 每节点「类型化样式集(引用令牌 + 原值覆盖)」双层**。令牌(色板/字阶/间距/圆角/阴影)从输入量化抽取;节点样式是一组类型化属性(布局/盒/文本),默认引用令牌、允许原值覆盖。Web 适配器填原值→零回归;视觉适配器量化成令牌;Flutter 目标直接读类型化属性。这是同时伺候「Web 不回归」与「跨目标可渲染」的唯一表示。
3. **交互建模 = 声明式「交互意图」闭集,按目标各自渲染**。组件挂一个小词表的交互意图(navigate/toggle/open-overlay/submit-form/input/countdown/carousel…),不含业务逻辑、不搬源 JS。每个 OutputTarget 把意图翻成自己的惯用法(Web 复用现成 `INTERACTION_SIM` 仿真层;Flutter 出 onPressed 桩)。与引擎「不赌离线重放 JS」哲学一致,且可从视觉推断。

### 9.3 UIR JSON Schema(权威定义)

顶层:
```jsonc
{
  "uir_version": "1",
  "meta": {
    "source_kind": "web_dom|vision|description|apk_static|a11y",
    "viewport": {"w": 1920, "h": 1080},
    "title": "", "lang": "zh"
  },
  "tokens": {
    "color":      {"c0": "#0b5fff", "c1": "#111827", "...": "..."},
    "font":       {"f0": {"family": "Inter", "fallback": "sans-serif"}},
    "text_style": {"t0": {"size": 16, "weight": 400, "line": 1.5, "color": "c1", "font": "f0", "align": "left"}},
    "space":      {"s0": 0, "s1": 4, "s2": 8, "s3": 12, "s4": 16, "s5": 24, "s6": 32},
    "radius":     {"r0": 0, "r1": 4, "r2": 8, "r3": 9999},
    "shadow":     {"sh0": "0 1px 2px rgba(0,0,0,.08)"}
  },
  "root": { /* node */ }
}
```

节点(node):
```jsonc
{
  "id": "n1",
  "role": "box|text|image|icon|button|input|nav|list|list_item|card|header|footer|hero|divider|overlay",
  "layout": {"mode": "flow|flex|grid|stack|absolute", "direction": "row|col",
             "justify": "start|center|end|between", "align": "start|center|end|stretch",
             "gap": "s2", "wrap": false, "cols": 3},
  "box": {"w": "100%|px|auto", "h": "auto", "pad": ["s2","s3","s2","s3"], "margin": ["s0"],
          "bg": "c0", "radius": "r1", "border": {"w": 1, "color": "c1", "style": "solid"},
          "shadow": "sh0", "opacity": 1.0,
          "position": {"top": "px", "left": "px", "z": 10}  /* 仅 absolute */},
  "text_ref": "t0",
  "content": {"text": "登录", "src": "assets/img/hero.png", "alt": "", "icon": "search", "placeholder": "手机号"},
  "intents": [{"on": "click|submit|input|change", "do": "navigate|toggle|open-overlay|submit-form|countdown|carousel-advance|none",
               "target": "n9", "meta": {"seconds": 60}}],
  "children": [ /* node, ... */ ],
  "raw": {"tag": "div", "attrs": {"class": "x"}, "css": "/* 计算样式串 */"}
}
```

字段语义:
- **tokens**:全局去重的设计原子。节点样式字段(`bg`/`color`/`gap`/`radius`/`shadow`/`text_ref`)默认是令牌键(字符串引用);也可直接给原值(如 `"bg": "#fff"`、`"gap": 8`),渲染器两者都认——这就是「引用令牌 + 原值覆盖」双层。
- **role**:语义角色,决定默认渲染惯用法与可用交互意图;`box` 是无语义容器(万能逃生舱)。
- **layout / box**:类型化、目标无关的布局与盒模型;各 OutputTarget 自行翻译(Web→flexbox/grid;Flutter→Row/Column/Stack/Container)。
- **intents**:交互意图闭集;无逻辑,只声明「控件用途」,由目标提供罐装安全行为。
- **raw**:可选逃生舱。Web 适配器把克隆产物的 `tag/attrs/计算样式串` 原样塞入;HtmlCss 目标见 `raw` 即原样回吐 → 逐字节等价 → 零回归。非 Web 目标忽略 `raw`,只读类型化字段。

校验:`core/uir/schema.json` 为权威 JSON Schema,`core/uir/model.py` 提供 dataclass 与 `validate()`(轻量自校验,不引第三方)。

### 9.4 现有克隆 DOM → UIR 映射(证明 Web 平滑迁入)

- Web 适配器 `web_dom.from_clone_workspace(ws) -> UIR` **不做语义识别**:把克隆产物(渲染后快照 DOM + CSSOM 固化样式)逐节点转 `role:"box"/"text"/"image"`,样式整串塞进 `raw.css`,标签与属性进 `raw.tag/attrs`。令牌表可空(或仅做无损色板抽取)。
- `HtmlCssOutputTarget` 对带 `raw` 的节点原样回吐 `tag+attrs+css` → 产出与当前克隆直出**逐字节等价**。
- **零回归闸门(可测试)**:同一克隆任务走两条路——「克隆直出 HTML」与「克隆→UIR→HtmlCss」——对同一对(原站图,产物图)用 `compute_visual_similarity` 打分必须**逐值一致**。此闸门进 `tests/`,保证 UIR 迁入不动 Web 分数。
- 语义上卷(把 `box` 树归并为 nav/card/list)是**可选增强层**,仅生成侧(vision)启用,不改克隆直出路径,不进 `cloner_engine.py` 主流程。Web 适配器作为旁路模块存在,供迁移测试与未来统一导出。

### 9.5 InputAdapter / OutputTarget 接口规范与挂载点

```python
# core/uir/registry.py
class InputAdapter(Protocol):
    kind: str                                   # "vision"/"description"/"web_dom"/"apk_static"/"a11y"
    def to_uir(self, req: GenRequest, ctx: BuildContext) -> UIR: ...

class OutputTarget(Protocol):
    name: str                                   # "html_css"/"flutter"/...
    shootable: bool                             # 产物能否被 Playwright 截图参与像素评分
    def render(self, uir: UIR, out_dir: Path, ctx: BuildContext) -> RenderResult: ...

@register_input("vision") / @register_output("html_css")   # 发现式注册, 新增能力零侵入
```

- **BuildContext**:时间预算 `(start, deadline)`、AI 配置(任务级 > 环境 > 默认,与克隆侧同源)、日志总线 `log(tag, msg)`、工作空间路径、评分函数句柄 `score(orig, clone, ...)`。
- **三处挂载点**:
  1. **调度器**(唯一侵入现有文件处,且极小):生成任务用 sentinel——`batch config.mode == "generate"` 或 `target_url` 以 `uir:` 前缀标识。`scheduler._worker` 一处分支:生成任务 → `generate.run_generation_task(task, db, bus, pool)`;克隆任务 → `ClonerEngine`(原样)。**不碰 `cloner_engine.py`**。
  2. **时间预算**:复用 `REPAIR_TIME_BUDGET`/`AI_TIME_BUDGET` 同款阶梯;新增 `GENERATE_TIME_BUDGET` / `GENERATE_TARGET_SCORE`(≥85 即停)/ `GENERATE_MAX_ROUNDS`。快任务快停,慢任务限时收敛。
  3. **评分闭环**:复用 `compute_visual_similarity`(口径与克隆侧**完全一致**)与 `pixel_calibrator.divergent_cells` 区域线索(同 AI vision repair),把差异喂回下一轮生成 prompt。

### 9.6 L1 数据流(生成式「截图/描述转代码」)

```
输入(截图 bytes / 文字描述)
  → VisionInputAdapter(多模态理解, 复用 OpenAI 兼容通道与 max_tokens 预算阶梯)
  → UIR
  → HtmlCssOutputTarget(UIR → HTML/CSS, 沿用 [[ ]] 定界符与内联 SVG 图标约定)
  → Playwright 渲染 + 全页截图(复用 browser_pool)
  → compute_visual_similarity(与参考图打分)
  → 低于 GENERATE_TARGET_SCORE 则带差异线索迭代(≤ GENERATE_MAX_ROUNDS 或预算用尽)
  → 产物接入现有预览(/preview/<task>)与导出(zip)
```

- **参考图口径**:截图输入时,参考图 = 输入截图(与克隆侧「原站图」同位);描述输入时无参考图,评分退化为「结构自洽 + 文本覆盖(描述关键词 F2)」,**报分单列**,不混入截图用例的保真均分。
- **API**:`POST /api/v4/generate`(多部分或 JSON:`kind=screenshot|description`,附图或文本,可选 AI 覆盖)→ 建生成任务 → 走调度器 → 产物链路。
- **前端**:生成入口沿用既有 UI 约定(`[[ ]]` 定界符、内联 SVG 图标)。

### 9.7 L2 可行性(移动端,授权 APK)

- **静态路线(D3 骨架先做)**:授权 APK 即标准 zip。`ApkStaticInputAdapter` 用 `zipfile` 读取并解析**公开资源**:`AndroidManifest.xml`(主题/屏幕/label)、`res/layout/*.xml`(控件树:LinearLayout/ConstraintLayout/TextView/ImageView/Button…)、`res/drawable*|mipmap*`(图片)、`res/values/strings.xml`(文案)→ UIR。Android 控件/属性映射到 UIR role/layout/box。
  - **严格边界**:只读公开资源;**不碰 `classes*.dex`、`lib/*.so`,不脱壳,不解密**。二进制 AXML(编译后的 XML)用轻量只读解析(自研 AXML 解码或 `androguard` 只读模式,择一,避免重依赖)。资源被加密/加固 → 判「静态不可用」,输出「建议动态授权采集」,不做破解。
- **动态路线(可行性,非本次实现)**:授权设备上 `uiautomator dump` 得 view hierarchy(类名/文本/bounds/资源 id)→ `A11yInputAdapter`(与桌面 a11y 同一适配器族)→ UIR。需真机/模拟器 + ADB,属**运行期授权采集**,不涉及静态破解。
- **OutputTarget(Flutter)**:UIR → Flutter Widget 树(Scaffold/Column/Row/Container/Text/Image/ElevatedButton…)。D3 只做最小版:L1 级简单页能产出**可读** Widget 代码;构建/CI/签名列为缺口(见 9.10)。

### 9.8 L3 路线设计(桌面端,仅设计)

- **采集**:读授权桌面应用的操作系统可访问性树(macOS AX API / Windows UI Automation / Linux AT-SPI):角色、标题、bounds、值。a11y 树是 OS 提供的**公开无障碍接口**,非反编译。→ `A11yInputAdapter` → UIR。
- **输出**:`ElectronOutputTarget`(Chromium + HTML,与 HtmlCss 目标高度复用:UIR→HTML 直接装进 Electron 壳)或 `TauriOutputTarget`(系统 WebView + Rust 壳,体积小但需 Rust 工具链)。
- **边界**:仅重建外观与交互意图,不反编译目标程序。

### 9.9 依赖 / 工期 / 风险

| 能力 | 关键依赖 | 预估工期 | 主要风险 |
|------|----------|----------|----------|
| L1 截图/描述转代码 | 多模态模型(OpenAI 兼容,已有通道);Playwright(已有) | 本次 D2 | 模型质量决定上限;无 API Key 时整体不可用(自动跳过,与 AI 修复同) |
| L2 静态 APK → UIR | `zipfile`(标准库)+ AXML 只读解析 | D3 骨架 + 后续 | 加固/资源加密包静态不可用(按边界退回,不破解);控件→UIR 映射覆盖度 |
| L2 Flutter 产物 | Flutter SDK + Android SDK + 签名链(本仓库不含) | 骨架本次,构建后续 | 工具链/CI/签名是独立工程缺口 |
| L2 动态路线 | 真机/模拟器 + ADB + uiautomator | 仅可行性 | 运行期授权采集,需设备 |
| L3 桌面 | OS a11y API + Electron/Tauri | 仅设计 | 各 OS a11y 差异;Tauri 需 Rust |

### 9.10 落地缺口清单(诚实声明)

- **L1**:多模态模型质量是上限;无 API Key 时 L1 不可用(自动跳过,确定性克隆管线不受影响)。
- **L2**:APK 构建需 Flutter SDK + Android SDK + 签名链,本仓库不含工具链;`OutputTarget(Flutter)` 只产出可读 Widget 代码,构建/CI/签名列为缺口。加固包静态路线不可用(边界内行为,非缺陷)。
- **跨目标保真**:Flutter/桌面目标无法复用像素评分(需各自渲染器)。初期以「代码可读 + 可构建」为验收;像素评分(`compute_visual_similarity`)仍只覆盖 Web/Electron 的 HTML 渲染产物。
- **语料纪律**:生成能力建独立小语料 + 留出集(5 截图 + 5 描述起步),报分按「克隆/生成」「语料内/留出集」分组,沿用 `_summary_one` 外包分组模式。

### 9.11 D2/D3 落地实录与缺口(2026-11-04)

**已落地**(全部在 `core/uir/`, 调度器一处分流, 未改 `cloner_engine.py`):
- 输入适配器: `description`(五类通用页面原型 + LLM 回落)、`screenshot`(XY 版面切分 + 文字行字形代理 + 图片区裁切回填 + 多模态 LLM)、`web_dom`(克隆 DOM 直通, 零回归闸门逐字节相同)、`apk_static`(授权 APK zip 的二进制 AXML/ARSC 只读解析, 或工程 `res/layout` 明文 XML; **从不读取 classes*.dex / lib / assets**, 布局缺失或不可解析只报告 `static_unavailable` 并建议运行期授权采集)。
- 输出目标: `html_css`(可截图、可评分、接入预览/导出)、`flutter`(`lib/main.dart` + `theme.dart` + `pubspec.yaml`, 不可截图)。
- 实测(本机无大模型 Key, 全走确定性 Tier 0): 描述用例 5/5 ≥85(desc-self 自评均 88.6, 单列); 截图回环 5 例像素均 46.5, 真实站点留出集 8 例均 39.8(只看不调)——结构级确定性重建在 NCC/边缘像素口径上的诚实天花板, 不以合成用例继续调参。

**缺口清单**:
1. **L1 ≥85 截图目标依赖多模态 Tier 1**: 通道与回落已接好, 需配置多模态 Key 后在留出集上验证; 口径不变。
2. **L2 工具链**: Flutter SDK / Android SDK / 签名链(keystore, key.properties)不在仓库; `flutter analyze/build` 的 CI 作业未建; drawable 资源搬运与 `@drawable` 引用解析仅做文件名匹配。
3. **L2 覆盖度**: ConstraintLayout 约束 → 线性布局近似; `<include>/<merge>`、样式/主题(`@style`)、`dimens`、多密度资源未解析; Jetpack Compose 界面无布局 XML, 静态路线天然不可用(需动态 a11y 路线)。
4. **L2 动态路线 / L3**: `a11y` 适配器(uiautomator 界面树 / macOS AX / UIA)未实现, 仅设计。
5. **跨目标像素评分**: Flutter/桌面产物无渲染器接入, 仅「代码可读」验收。
6. **API**: `POST /api/v4/generate` 目前接受 screenshot/description; `apk_static` 走离线调用(`GenRequest.source_path`), 上传入口与授权声明留给平台侧。

### 9.12 真实构建实录: APK 与桌面二进制(2026-11-05)

**结论**: 引擎内已能从 UIR 产出**真实 APK** 与**真实桌面二进制**(macOS .app / Windows .exe)。构建是显式步骤 `core/uir/toolchain.py`, `render()` 仍是纯代码生成、不联网不调工具链; 缺工具链时返回 `{"ok": false, "gap": ...}` 而非抛异常。

**新增**:
- `OutputTarget("electron")`(`core/uir/targets/electron.py`): 复用 `html_css`, 同一份 index.html/app.css 原样装进 Electron 壳(逐字节相同, 有测试闸门), 因此桌面产物与 Web 同为 Chromium 渲染, `shootable=True`, 像素评分直接覆盖; `main.js` 安全默认(contextIsolation / sandbox / 无 nodeIntegration, 外链交系统浏览器, 不在应用内导航外站); `package.json` 钉死 electron 45.2.2 + @electron/packager 20.3.0; 窗口取 UIR 视口。
- `toolchain.build_apk()`: `flutter create`(只补 android/ 骨架, 不覆盖生成的 lib/) → `pub get` → `analyze` → `test` → `build apk`。
- `toolchain.build_desktop()`: `npm install` → `@electron/packager` 按平台打包; 交叉打包默认 x64。
- Flutter 目标随产物带 `test/widget_test.dart`(冒烟: 能构建出 Scaffold)与无 include 的 `analysis_options.yaml`: 否则 `flutter create` 模板测试引用不存在的 `MyApp`、分析配置引用未声明的 `flutter_lints`, analyze 误报 18 个错误。
- Flutter 目标两处通用缺陷修复(真机截图发现): ① 按钮的底色/圆角/内边距外包在 Container 装饰上, 渲染成「蓝色块里套一个默认按钮」, 现并入 `ElevatedButton.styleFrom`; ② 内边距 2/3 值未按 CSS 简写展开(`[12,24]` 被展开成上 12 其余 24), 现按 1/2/3/4 值规则展开。

**实测**(本机 macOS arm64, 样本 = 描述适配器 Tier 0 生成的登录页 UIR, 非第三方应用):

| 产物 | 大小 | 验证 |
|------|------|------|
| `app-release.apk`(arm64-v8a / armeabi-v7a / x86_64, minSdk 由 Flutter 模板定, compileSdk 36) | 47.5 MB(45.3 MiB) | analyze 无问题、flutter test 通过、apksigner 校验通过; 已在 API 25 arm64 模拟器安装启动, 截图与 Electron 渲染版式一致, 验证后已卸载 |
| macOS `Clonjurer.app`(arm64, 未签名) | 257 MB(目录) | 本机启动, 经 CDP 截图确认页面完整渲染 |
| Windows `Clonjurer.exe`(x64, PE32+ GUI) | 主程序 249 MB / 目录 357 MB | 在 macOS 上交叉打包; `file` 确认 PE32+ x86-64; **未在 Windows 实机运行** |

首次 APK 构建 245s(Gradle 冷启动拉依赖), 之后 17s; Electron 每平台约 20s。

**本机为此安装的工具链(均在仓库外)**: Flutter 3.47.9(`brew install --cask flutter`, 已关闭 flutter/dart 遥测); Android SDK `platforms;android-36` + `build-tools;36.0.0`(sdkmanager, 使用本机已接受的 android-sdk-license, 未接受新条款); 既有: Android cmdline-tools(`/opt/homebrew/share/android-commandlinetools`)、OpenJDK 21、Node 26 / npm 11。Electron 运行时在 `npm install` 时下载到生成工程的 node_modules。

**缺口(更新 §9.11 第 2/4/5 条)**:
1. **签名**: APK 用 Flutter 模板默认的 debug keystore 签名(release 构建亦然), 上架需自备 keystore + `key.properties`; macOS 未签名未公证(Gatekeeper 会拦截他人机器上的首次打开), Windows 无 Authenticode。
2. **安装器/体积**: 只出可执行目录, 未做 dmg / msi / nsis 安装器; APK 未按 ABI 拆分(`--split-per-abi` 可降到约 1/3); Electron 包体约 250–370 MB, 若在意体积需 Tauri(系统 WebView, 需 Rust 工具链, 未做)。
3. **CI**: 构建只在本机跑通, 没有 CI 作业; 首次构建需联网拉 Gradle/pub/npm 依赖。
4. **跨目标评分**: Electron 产物被像素评分覆盖(同一 HTML); Flutter 渲染仍无评分接入, 现阶段验收为 analyze + test + 可构建 + 模拟器截图人工核对。
5. **Windows 实机**: .exe 只验证了文件格式, 未在 Windows 上运行。
6. **L3 采集侧**: 桌面 a11y 输入适配器(macOS AX / UIA)仍未实现, 本次只落地输出侧。

**前端接入(同日)**: 生成面板「输出目标」新增 Electron, 新增「构建安装包」(选 Flutter 自动选 APK, 选 Electron 自动选桌面包); 生成任务 `spec.build=apk|desktop` 在打包后构建(不占生成预算), 产物放 `exports/`, 任务行出现 APK / macOS .app / Windows .exe 下载按钮(`GET /api/v4/generate/<id>/artifact/<i>`, 只放行报告登记且位于导出目录的文件); 失败显示「构建缺口」。隔离实例 8894 端到端: 浏览器提交「商品列表 + Flutter + APK」→ 42.6 MB APK → 下载 → 模拟器启动渲染正常。
自动冒烟测试在端到端里抓到并修复 Flutter 目标的通用布局缺陷: 无伸缩子项的横排改 `Wrap`(窄屏折行而非溢出); 网格改「等宽列 + 内容自适应行高 + 按最小列宽 140 减列」(固定 childAspectRatio 纵向溢出); `Expanded` 只由横排父级包裹(放进 Wrap/滚动竖排会抛 ParentData 异常); Android `0dp + layout_weight` 不再钉成宽 0; drawable 图片加 `errorBuilder` 回落占位。五类描述原型 + APK 夹具 + 实测 UIR 共 7 例 analyze / test 全过。
仍缺: Electron 任务的「沙盒预览」不指向壳内页面(产物可下载、评分正常); Flutter 版式仍是近似(如顶栏导航折行与 Web 不同)。

### 9.13 95 分路线图(2026-11-05)

2026-11-05 六维评分 67/100(Web 80 / L1 45 / L2·L3 50 / 速度效率 72 / 代码完整度 68 / 稳定 77)。从当前到各维 ≥95 的量化口径、技术路线、阶段闸门与所需资源见 `ROADMAP_95.md`; 本节不重复。约束不变: 禁 per-site 特判、留出集只测不调、Web 零回归。

**交付形态统一(同日, 按 cz 反馈重做)**: 安装包不再是「界面生成」的专属选项, 而是**任务级的交付形态**, 与现有 UI 同一套卡片样式:
- 「新建克隆」Hero 卡与「界面生成」面板都有同一组 `交付形态 DELIVERY` 三选一: 网站工程(Flask ZIP, 默认) / 桌面应用(macOS .app + Windows .exe) / Android 应用(APK)。批次配置键 `package`。
- 实现 `core/app_shell.py`: 把任务已有的网站工程(flask_app: templates/static/mock_data + site.json 路由表)装进应用壳, 壳内置只监听 127.0.0.1 的本地服务器, 语义移植自导出的 Flask 工程(页面路由 / 录制接口四级匹配 / 影子文件 / 反代兜底), 所以绝对路径资源与多页路由在应用里与网站一致。桌面 = Electron(Node http); Android = Flutter + webview_flutter(dart:io HttpServer 读单文件资源包 site.pack + 索引), 清单只对 127.0.0.1 放行明文 http。
- 调度器在任务完成后执行后置步骤 `Scheduler._package_if_requested`(不改 cloner_engine), 失败只记缺口不改评分; 产物 `exports/<id>_*.{apk,zip}`, 报告 `build_report.json`, 任务卡片在资产统计下方多一行「安装包」下载(统一接口 `GET /api/tasks/<id>/artifact/<i>`)。
- 「界面生成」面板: HTML/CSS + 桌面/Android 走同一应用壳; Flutter 原生控件 + Android 走 §9.12 的原生构建。Electron 原生目标保留在引擎/API 中, 不再单独出现在 UI(与应用壳重复)。
- 实测(8894 隔离实例, 浏览器操作新建克隆): example.com → 桌面应用: macOS 114MB zip / Windows x64 151MB zip(内含 PE32+ exe); example.com → Android 应用: 43MB APK, 模拟器启动显示克隆页; 另用一个已有门户克隆工作空间(27MB 资源)离线验证: APK 与 macOS 应用均正常加载 /static 绝对路径资源。
- 缺口: 应用壳不含平台埋点/表单上报(只打包已有产物); 反代兜底需联网; Android 壳首次构建约 1 分钟(依赖已缓存时); 未签名同 §9.12。

### 9.14 L1 截图 Tier 0 版面分析 v2 实录(2026-11-05)

`core/uir/adapters/layout.py`(确定性, 无 Key 可用, 单例 1~10s)。v1 在 512 宽缩略图上切分、叶块底色取模糊后的中位色, 标题/正文变成灰色实心条, 1px 边框消失。v2 全部在原分辨率上做, 规则均为通用版面先验(禁特判守卫 `tests/test_no_site_special_case.py` 已入 CI):
递归 XY 切分(行沟槽 1px、单行高度区块的列沟槽 ≥0.75×行高, 防止字间切碎; 平滑缓变线也算沟槽); 众数底色分层; 边框/圆角检测(允许外侧投影行); 叶块按前景行带 × 段切分, 段内按墨迹高度再分组(头像+小字); 文字段外圈同色实心矩形 → 按钮/标签底板; 文字行字号 = 墨迹高/0.98(Chromium 实测 0.92~1.07); 色彩丰富的段从输入截图裁切回填(整页面积 ≤60%, 单块 ≤35%, 扁长文字段门槛更高, 避免文字被当图片)。v1 保留为第 2 轮对照候选, 质量循环取高分者。

| 口径(Tier 0, 无 Key) | v1(REPORT_20261105_013923) | v2(REPORT_20261105_015300) | v2 不回填图片(消融, REPORT_20261105_015429) |
|---|---|---|---|
| 截图回环 5 例(tuned) | 46.5 | **87.8**(4/5 ≥85) | 87.2 |
| 真实截图留出集 8 例(只测不调) | 39.8 | **85.2**(6/8 ≥85) | 67.8 |
| 留出集耗时均值 | 4.4s | 4.2s | 6.3s |

留出集回填面积 1.6%~26%(均 ~12%); 不回填时仍有 67.8, 说明主体提升来自结构/文字行, 不是贴图。文字为确定性字形代理(非真实文案), 文本通道在无参考文本时不计分; 有 Key 时地板轮即为 v2, 模型轮须超过它才保留。骨架填充(skeleton_fill)仍用 v1 骨架(块数少, 便于模型逐块回答), 待有 Key 时验证切换。

**应用壳瘦身(同日, cz 反馈「太大」)**: 体积几乎全是运行时(Electron 自带 Chromium; Flutter 引擎按 3 个 ABI 各一份), 改为直接用系统 WebView:
- **Android**: 原生 Activity + 系统 WebView, 不再用 Flutter/Gradle。虚拟 https 源 `https://app.local`, 在 `shouldInterceptRequest` 中按导出 Flask 工程语义作答(不开端口, 不放行明文); 构建 `toolchain.build_android_shell`: aapt2 link → javac → d8 → zipalign → apksigner, 只需 Android SDK + JDK, 约 3 秒。代码 dex 约 11KB。
- **桌面**: Go + 系统 WebView(`core/shell_templates/desktop_go`; macOS WKWebView 经 cgo 本机构建, Windows WebView2 纯 Go 在 macOS 上交叉编译), 站点 `go:embed` 进单个可执行文件, 本地服务器只听 127.0.0.1(Info.plist 只放行本地网络)。构建 `toolchain.build_go_desktop`; 依赖版本钉死在模板 go.mod。Electron 壳代码保留为备选, 默认不用。
- 实测(同一 example.com 克隆): APK 43MB → **25KB**; macOS 114MB → **2.5MB**(zip); Windows 151MB → **2.9MB**(zip, exe 7.4MB)。27MB 资源的门户克隆: APK 24MB、macOS 34.5MB, 即「站点本身 + 极小壳」; 模拟器与本机窗口截图均正常渲染。
- 缺口: Windows 包需系统自带 WebView2 运行时(Win10/11 通常已带, 缺失时弹窗提示); Windows 包未在实机运行; 桌面壳外链在窗口内打开(未拦截到系统浏览器); Android 壳的上游反代只支持 GET/HEAD(WebView 拦截拿不到请求体); Linux 桌面壳未实现; 签名同前。

### 9.14 签名与三平台构建 CI(2026-11-05)

落地 ROADMAP_95.md §3.3 的第一版, 不改任何生成/克隆逻辑:
- **签名步骤**(`core/uir/toolchain.py`, 纯增量): `build_apk` / `build_desktop` 构建后自动签名并校验, 结果写回报告: `signing`(给人看的标签, 未签名一律以「未签名」开头)、`signed`(布尔)、`signing_detail`(状态/校验/证书)。
  - APK: 有 keystore 时 `apksigner sign`(口令走 `env:` 传参) → `apksigner verify --print-certs`; 无 keystore 保留 Flutter 模板的 debug 签名, 状态 `unsigned-debug`; 校验出的证书仍是 Android Debug 时不标已签名。
  - macOS: 有 `APPLE_SIGN_IDENTITY` 时 Developer ID + hardened runtime(带 Electron 所需 JIT entitlements), 再有 Apple 账号则 `notarytool` 公证 + `stapler` 装订; 否则 ad-hoc 签名(本机可运行, Gatekeeper 下需右键打开)。非 macOS 宿主不签。
  - Windows: 有 pfx 时 Windows 宿主用 `signtool`, 其他宿主用 `osslsigncode`(交叉打包); 时间戳服务不可达时退回无时间戳签名并标注; 自签名证书校验不过时标「证书链未受信任」。无证书不签。
  - 证书只从环境变量读(base64 解到 0600 临时文件, 用完即删), 口令在日志与返回的日志尾里打码; `CLONER_SIGN=0` 整体跳过。secret 名见 ROADMAP_95.md §8 末尾。
  - 「交付形态」应用壳(`core/app_shell.py`, 克隆任务的桌面/Android 包)同样经 `build_desktop` / `build_apk`, 因此自动得到同一套签名与标注, 无需另接。
  - 顺带: `npm install` 后若 Electron 二进制缺失(postinstall 被跳过/中断)显式补拉一次, 避免打出空壳。
- **CI**(`.github/workflows/build.yml`, `app-build`): 手动 + 每晚北京时间 03:00 + 只改构建链(toolchain / Flutter·Electron 目标 / 本 workflow)的 PR, 不进引擎 PR 闸门。样本 = `gen_cases/cases.json` 描述用例(确定性路线, 不调模型)。
  - android(ubuntu): Flutter → release APK → 签名校验 → android-emulator-runner(API 34)安装、启动、进程存活、`screencap`; 截图与参考 HTML 按 dp 视口比较, **跨渲染器只记录不设门槛**(L2 跨目标评分的第一个数据点)。
  - desktop(macos-latest arm64 / windows-latest x64): Electron 打包 → 签名 → **实机启动**: 带 `--remote-debugging-port` 启动产物, 经 CDP 截图, 与同一份 HTML 在 Playwright Chromium 下同视口渲染比较, 低于 `min_score`(默认 90)即红。Windows 这一步是 .exe 第一次在真实 Windows 上启动验证。
  - **壳可切换**: workflow 输入 `android_target` / `desktop_target`(默认 flutter / electron)取自 `engine_bench/build/ci_build.py` 的 `BUILD_TARGETS` 表。将来换原生 WebView 的 Android 壳或 Tauri 桌面壳 = 注册 OutputTarget + toolchain 里同签名的 build 函数 + 表里加一行, workflow 与签名都不用改(签名按产物类型 APK / .app / .exe 做, 与壳无关)。冒烟方式也随表: Chromium 内核的壳走 CDP 截图比分, 系统 WebView 类的壳退为「启动存活」(不出分)。
  - 冒烟比较时两侧都隐藏滚动条(桌面窗口的经典滚动条占 15px, 会让长页整体错位: 实测落地页 35.5 → 100.0)。5 个描述用例 linux 包冒烟均 100.0。
  - 产物按签名状态命名(`clonjurer-<用例>-<平台>-<架构>[-unsigned].zip/.apk`), 报告与截图上传, `summary` 作业把构建/签名/冒烟汇总进运行摘要。
- **本地验证(云端容器, Linux)**: render → `@electron/packager` linux-x64 + win32-x64 → Xvfb 下启动 linux 包经 CDP 截图, 与参考 HTML 比较 100.0; 用临时自签名 pfx 走 osslsigncode 给 win32 .exe 签名成功(容器内时间戳服务不可达, 退回无时间戳并如实标注)。未在本地验证: Flutter/APK 签名(容器无 Flutter/Android SDK, 由伪 apksigner 单测覆盖)、macOS codesign/公证、Windows 上 signtool 与实机启动 —— 都要等 CI 首跑。
- 缺口: 未签名时 macOS 会被 Gatekeeper 拦、Windows 会弹 SmartScreen; 仍无安装器(dmg/msi); APK 冒烟分数只记录; macOS / Windows runner 分钟数按 GitHub 计费倍率消耗(macOS 10×, Windows 2×)。
