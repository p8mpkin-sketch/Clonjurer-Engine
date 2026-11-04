# 通用引擎 95/100 路线图(2026-11-05)

起点: 2026-11-05 评分 **67/100**(口径见 §0)。目标: 六个维度各自 ≥95, 加权 ≥95。
本文只写「差距 → 技术路线 → 闸门 → 所需资源」, 实录与缺口仍记在 `DESIGN.md §9`。

三条不变约束(每个闸门都复核):
1. **禁 per-site 特判**: 任何以域名 / 站点指纹 / 用例 id 分支的代码一律不合入(CI 用 grep 守卫 `engine_bench` 语料里的域名不出现在 `core/`)。
2. **留出集只测不调**: `engine_bench/real_sites/corpus.json` 与 `gen_cases/cases.json` 的 holdout 组只报分; 调参只用 tuned 组; 每个阶段结束换一批新留出站(旧留出站并入 tuned)。
3. **Web 零回归**: `tests/test_uir_guards.py::WebZeroRegression` + 逐值等价 `test_perf_equivalence.py` + 真实语料 tuned 组均分不降 >0.5 分, 任一不满足即整批回退。

---

## 0. 口径: 每个维度「95 分 = 什么数字」

打分口径必须先钉死, 否则「到 95」没有可验收的含义。下表是本路线图的验收定义, 与 2026-11-05 评分表同权重。

| 维度(权重) | 当前 | 95 分的量化门槛(全部满足) | 差距要点 |
|---|---|---|---|
| Web 克隆(30%) | 80 | 留出集(≥12 站, 每阶段换新)桌面均分 ≥93 且最低 ≥80; 平板 768 ≥88、手机 390 ≥88; 框架识别 ≥95% 且无误判(可多标签); 交互轨与保真轨差 ≤5 | 平板/手机 70.7/72.0 → 88; 尾部 bilibili 61 → 80; 识别 jd 误判 |
| L1 截图/描述生成(15%) | 45 | 截图回环(已知真值) ≥90; **真实截图留出集 ≥75**(像素 v3 口径, 与克隆同函数); 描述用例改为**外部裁判**(非自评)≥85 占 5/5; 单例耗时 p90 ≤120s | 留出 33~41 → 75; 自评 → 外判; 300~616s → 120s |
| L2/L3 应用重建(15%) | 50 | 输入侧: 静态 APK + **动态 a11y(Android uiautomator / macOS AX / Windows UIA)** 三条通道可用; 输出侧 APK / .app / .exe 全部**签名**且 CI 产出; **跨目标像素评分**(Flutter 产物在模拟器截图 ↔ 原图)留出集 ≥70; 授权样本语料 ≥10 应用, 留出 ≥4 | a11y 未实现; 无签名/CI; 无跨目标评分; 样本只有 1 个登录页 |
| 速度效率(15%) | 72 | Web 留出集 p50 ≤20s、p90 ≤45s, **连续 3 轮**都达标; L1 p90 ≤120s; APK 热构建 ≤30s、Electron ≤20s; 速度评分(§6.1 基线对比) ≥85 | p90 59~92s 不稳; L1 超预算 2.5 倍 |
| 代码完整度(15%) | 68 | `cloner_engine.py` 拆成 ≤6 个模块, 单文件 ≤1500 行, 单函数 ≤120 行; `core/` 类型标注覆盖 ≥90%(mypy 通过); 单测覆盖 core/uir ≥85%、core 其余 ≥70%; CI 覆盖 Flutter/Electron 构建 + Windows 实机冒烟 | 6719 行单文件、5 个 >300 行函数; CI 只跑 Web |
| 稳定(10%) | 77 | 连续 3 轮全量跑分, 同站分差 ≤1.5; 任务失败率 <1%; L1/构建 0 次异常退出(只允许 gap 返回) | 轮间 p90 波动大; 未做多轮方差统计 |

> 为什么 L1 留出集门槛定 75 而不是 95: 像素 v3 是 NCC+边缘口径, 克隆侧**有 DOM**的留出集也只有 90.9。纯截图输入没有字体/布局真值, 75 已是「图片区裁切回填 + OCR 逐字文本 + 字体度量估计」能诚实达到的上限区间; 维度分 95 对应的是「这个任务在该口径下做到头」, 不是像素 95。

---

## 1. Web 克隆: 80 → 95

现状归因(来自 `REPORT_20261104_192827`): 桌面已 86~94; 掉分集中在 (a) 平板/手机视口 (b) 重 SPA 尾部 (c) 识别误判。

### 1.1 多视口: 三视口采集冻结(主因, +12 分)
- 现在是**桌面视口采集一次**, 再在 768/390 宽度下渲染克隆与活体原站对比(`_probe_viewports`, 只探针不采集)。媒体查询、`srcset`、JS 按宽度切换的 DOM 都没被采到, 平板/手机 44~50 分的站(mdn/gitee/github)全是这个原因。
- 路线: 采集期按 `config.VIEWPORTS` 三档各做一次「静置 → 冻结 DOM → 截图」, 得到三份冻结快照; 落盘时合成**一份** HTML: 共享资源 CAS 去重, 三份样式表按断点 `scope_viewport_css` 分段装载(该函数已存在, 当前只做占位换断点), 视口相关的 DOM 差异用 `data-vp` 属性 + 一段通用 CSS 显隐。不改写原站媒体查询, 不猜断点, 断点来自样式表自身。
- 代价: 采集耗时约 ×1.6, 用 §4 的并行抵消。
- 闸门: 平板/手机留出集 ≥85, 桌面不降。

### 1.2 尾部: SPA 懒加载与滚动触发(+3 分)
- bilibili 61.1、jd 保真 79: 首屏下方内容靠 IntersectionObserver / 滚动事件加载, 冻结时还是骨架屏。
- 路线: 静置前做**通用滚动预热**(分屏滚到底再回顶, 每屏等 `settle_quiet_ms`), 对 IntersectionObserver 做一次性「全部可见」回调(注入通用 polyfill, 不针对站点)。已有 `MutationObserver` 静置逻辑直接复用。
- 闸门: 留出集最低分 ≥80。

### 1.3 识别: 证据加权 + 多标签(+2 分)
- jd 实为 React 却判 Vue/Vite: 现在是「命中即定」。改成每条证据带权重(运行时全局对象 > 构建产物指纹 > 文件名), 允许输出主/次两标签, 报告对「主标签命中」计分。误判零容忍写进闸门。

### 1.4 两轨差距收敛
- 交互轨落后保真轨 >5 分的站, 原因是水合失败回落到静态。路线见 §1.2(预热后水合成功率提升), 另在交互轨注入「水合失败即不卸载 SSR DOM」的通用守卫。

---

## 2. L1 截图/描述生成: 45 → 95

### 2.1 真实截图: 33~41 → 75
现在的 skeleton_fill 已把几何交给版面分析、文字交给模型逐块填充, 剩余差距按像素口径分解为三项, 每项都有确定性解法, **模型只做它擅长的**:

| 差距来源 | 现状 | 路线 | 预期 |
|---|---|---|---|
| 文字位置/字号/字重 | 模型给文案, 字形用代理纹理或默认字体 | **本地 OCR**(RapidOCR/PaddleOCR, 离线, 无 Key 也能跑)给出逐行 bbox + 文本 → 字号 = 行高×经验比、字重由笔画密度估计、对齐由 bbox 与容器关系推; 模型只在 OCR 置信度低的块上复核 | +15~20 |
| 图片/图标区 | 已有图片区裁切回填 | 扩展到**所有非文字非纯色块**(图标、logo、渐变条), 裁切后以 `<img>` 原位回填, 保证像素级一致; 这是输入截图自身的内容, 不是外部资源 | +8~12 |
| 容器底色/边框/圆角/阴影 | 底色来自调色板量化 | 边缘检测后沿块边采样: 1px 边框颜色、圆角半径(角点曲率)、阴影(外侧渐变) → UIR box 属性 | +3~5 |
| 字体匹配 | 无 | 对 OCR 文本行做字体分类(衬线/无衬线/等宽 + 宽度度量), 从内置开源字体集选最近者; 不下载商业字体 | +2~4 |

- **更强视觉模型是否必要**: 必要但不是主因。目前模型只负责「读文案 + role」, deepseek-flash 的读图分辨率/长页能力已是瓶颈(切 6 段仍 2~3 分钟)。路线: `core/uir/llm.py` 已是 OpenAI 兼容通道, 增加**按任务选模型**(读图用带 bbox grounding 的多模态模型, 如 Qwen3-VL / Claude Opus 5.5 vision / GPT-5 级别; 填充 role 仍用便宜模型), 模型返回 bbox 时与 OCR 结果做 IoU 对齐, 二者冲突以 OCR 为准(确定性优先)。
- **差异驱动迭代**: 第 2 轮起用差异热力图(§2.4 已有)定位最差 3 个区域, 只对这些区域重新 OCR/裁切/填充, 不整页重来。
- 闸门: 留出 8 站 ≥75 且 0 次回落; 回环 ≥90。

### 2.2 耗时: 300~616s → p90 ≤120s
- 参考文本读图 6 段**串行**调模型, 这是耗时主体。改: 6 段并行(线程池, 与 CAS 下载同一套 `FIRST_COMPLETED` 调度); 读图结果按截图 sha1 缓存(同一截图第二轮零成本); OCR 本地化后读图只在 OCR 空白块上做, 段数通常降到 1~2。
- 预算从「总预算」改为「分阶段预算」(读图 ≤40s、填充 ≤30s/轮、渲染评分 ≤10s), 超时阶段降级而不是静默跳过(a719d67 的日志已在, 改成结构化字段进报告)。

### 2.3 描述用例: 自评 → 外部裁判
- 现 desc-self 是生成器自评, 不能计入可信分。改为两条外判:
  (a) tuned 组 5 例写**隐藏金标 HTML**(由人工定稿, 入库), 生成物渲染后与金标渲染图走像素 v3, 与截图用例同口径;
  (b) 留出描述用例用**另一家模型**按固定 rubric(元素齐全/布局关系/文案逐字)打分, 报告标 `desc-judge`, 裁判模型与生成模型不得同一家。
- 闸门: (a) ≥85 5/5, (b) ≥85 5/5。

---

## 3. L2/L3 应用重建: 50 → 95

### 3.1 输入侧: a11y 适配器族(`core/uir/adapters/a11y.py`, 一个适配器三个后端)
三个 OS 的可访问性树结构同构(role / name / value / bounds / children), 写一个 `A11yAdapter` + 三个采集后端, 后端只负责把树 dump 成统一 JSON:

| 后端 | 采集方式 | 依赖 | 边界 |
|---|---|---|---|
| Android | `adb shell uiautomator dump` → XML(class/text/bounds/resource-id) | adb + 授权设备/模拟器 | 运行期授权采集, 不碰 apk 内部; 解决 Compose 无布局 XML 的静态盲区 |
| macOS | AX API(`pyobjc` 的 `AXUIElement`), 对目标 pid 递归取 `AXRole/AXTitle/AXValue/AXFrame` | pyobjc-framework-ApplicationServices; 用户在系统设置授予辅助功能权限 | 只读; 不注入、不反编译 |
| Windows | UI Automation(`pywinauto` uia 后端 或 `comtypes` 直连) | Windows 机器/CI runner | 同上 |

树 → UIR 映射复用 `apk_static` 的控件映射表(TextView/Button/Image… 与 AXStaticText/AXButton/AXImage… 归一到同一 role 枚举)。
- 闸门: 三后端各有 ≥3 个授权样本应用(开源 / 自研)入语料, dump → UIR → html_css 渲染与原应用截图像素 v3 ≥70; `tests/test_uir_l3_guards.py` 加「树 JSON 夹具 → UIR 确定性」测试, 不依赖真机。

### 3.2 输出侧: 跨目标像素评分
- Flutter 产物目前只「可构建」。接入: `flutter test --update-goldens` 不用(字体差异大), 改为**模拟器截图**: `toolchain.build_apk` 后 `adb install` → `adb shell screencap` → 与原图走像素 v3(视口按设备 dp 换算)。Electron 已 shootable, 无需改。
- 这使 L2 第一次有真实分数, 并让 Flutter 目标的布局缺陷(eeec01a 修的 Wrap/网格/Expanded 类)能用分数而不是肉眼发现。
- 闸门: L2 留出 4 应用 Flutter 产物 ≥70; Electron 产物与 html_css 分数逐值相同(已有闸门)。

### 3.3 签名与 CI
| 产物 | 签名 | CI |
|---|---|---|
| APK | `key.properties` 走 CI secret(keystore base64), `apksigner verify` 入闸门; 本地无 keystore 时自动生成 debug 签名并在报告标 `unsigned-debug` | GitHub Actions ubuntu runner: Flutter + Android SDK 缓存, `analyze → test → build apk --release → apksigner verify → adb 模拟器冒烟截图`(emulator-runner) |
| macOS .app | `codesign --deep` + notarization(需 Apple Developer ID); 无证书时 ad-hoc 签名并标注 | macos runner: packager → codesign → `open` 冒烟 + CDP 截图 |
| Windows .exe | `signtool`(需代码签名证书, 可先 self-signed 标注) | windows runner: packager → signtool → 启动冒烟截图(**首次在 Windows 实机验证**) |

- 三个作业都是 `workflow_dispatch` + 夜间定时, 不进每次 push 的 `engine-gate`(构建 5~10 分钟)。
- 闸门: 三平台 CI 绿, 产物签名校验通过, 冒烟截图与 html_css 渲染像素 v3 ≥90(同一 HTML 应接近逐值)。
- **落地(2026-11-05)**: `.github/workflows/build.yml`(`app-build`, 手动 + 夜间 03:00)+ `core/uir/toolchain.py` 签名步骤 + `engine_bench/build/ci_build.py` 冒烟驱动, 详见 DESIGN.md §9.14。未配 secret 时照常出包, 文件名带 `-unsigned`。需要 cz 在仓库 Settings → Secrets and variables → Actions 添加的 secret 名见 §8 末尾。

### 3.4 L2 静态覆盖度补齐(配合 a11y 做兜底)
`<include>/<merge>`、`@style/@dimen` 解析、多密度资源选择、ConstraintLayout 约束 → Flex 近似的精度提升; 解析不了的一律 `static_unavailable` 走动态通道, 不猜。

---

## 4. 速度效率: 72 → 95

| 项 | 现状 | 路线 | 目标 |
|---|---|---|---|
| Web DOM 渲染阶段 | 163.com 41s、门户普遍 15~18s | 静置改为「分区静置」: 首屏静置满足即开始截图/CAS, 下方内容继续冻结; 三视口采集(§1.1)共用一个 page, 只切 viewport 不重载(`viewport_reload=False` 已有开关, 验证零回归后默认) | 阶段 p90 ≤15s |
| 资源重写 | apple 25.7s、bilibili 18.8s | 前缀树已做; 剩余是大 JS 文件逐个重写。按文件哈希缓存重写结果(CAS 内容相同即复用), 多 worker 进程池 | ≤8s |
| CAS 并发下载 | 15s | 已流式; 加 HTTP/2 连接复用、同域限流自适应 | ≤8s |
| 整体 p90 | 59~92s 波动 | 慢站识别后**提前降级**(超 45s 自动放弃交互轨保留保真轨) 而不是等满预算 | p90 ≤45s 三轮稳定 |
| L1 | 300~616s | §2.2 | p90 ≤120s |
| 构建 | APK 热 17s / Electron 20s | Gradle daemon 常驻 + pub 缓存; Electron node_modules 全局缓存 | APK ≤30s / Electron ≤20s(已达) |
| 速度评分 | 基线对比 | 新增项进 `engine_bench/perf/fixtures.py`(OCR、版面切分、a11y 树映射), 逐值闸门同规则 | ≥85 |

所有提速改动都走 `test_perf_equivalence.py` 逐值闸门, 复刻库浮点内部行为的一律不做(§6.3 教训)。

---

## 5. 代码完整度: 68 → 95

### 5.1 `cloner_engine.py` 拆分(零回归方式)
- 目标结构: `core/clone/{capture.py, freeze.py, rewrite.py, materialize.py, score.py, calibrate.py, engine.py}`, 每文件 ≤1500 行; 5 个 >300 行函数(`_normalize_page_dir` 764、`scope_viewport_css` 662、`_execute` 384、`_render_page` 330、`_make_render_and_score` 329)拆到 ≤120 行。
- 方法: **纯搬迁, 不改逻辑**, 每次搬一个模块就跑: 单测 110 + 逐值等价 + 真实语料 tuned 组(分数逐站相同, 不是均分相同)。`cloner_engine.py` 保留为薄 re-export 一版后删除。
- 闸门: 每个拆分 PR 的语料跑分逐站 Δ=0。

### 5.2 类型与测试
- `mypy --strict` 对 `core/uir` 与新 `core/clone` 通过; `coverage` 进 CI: core/uir ≥85%, core 其余 ≥70%。
- 新增测试: a11y 树夹具、OCR 后处理纯函数、三视口合成 HTML 的断点装载、签名校验 gap 路径、描述外判 rubric 解析。
- 禁特判守卫: CI grep `core/` 不含语料域名 / 用例 id。

### 5.3 CI 矩阵
`engine-gate`(每次 push, ≤5 分钟): 单测 + 逐值 + mypy + coverage + 禁特判 grep。
`corpus`(夜间): Web 三视口留出集、L1 留出集(需 Key secret)、三轮方差统计。
`build`(夜间 + 手动): §3.3 三平台构建签名冒烟。

---

## 6. 稳定: 77 → 95
- 跑分器加「连续 N 轮」模式, 报告输出逐站方差; 方差 >1.5 的站进「不稳定清单」, 归因到阶段(静置超时 / 下载抖动 / 原站活体变化), 活体变化导致的不计入稳定分(探针口径已有)。
- 失败分类: 网络不可达 / 环境受阻(拦截页) / 引擎异常 三类, 只有引擎异常计失败率, 目标 <1%。
- L1 与构建: 任何异常只能以 `{"ok": False, "gap": …}` 或报告 `错误` 列出现, 进程不退出(已有约定, 加测试覆盖全部异常路径)。

---

## 7. 阶段与闸门

| 阶段 | 内容 | 闸门(全部通过才进下一阶段) | 预估 |
|---|---|---|---|
| P0 口径与守卫(1 周) | 描述外判 + 金标; 跑分器多轮方差; 禁特判 CI grep; 换一批新留出站(Web 12 / L1 8) | 新口径基线报告入库; 守卫 CI 绿 | 分数不变, 只是可信 |
| P1 Web 多视口 + 尾部(2 周) | §1.1 三视口采集冻结; §1.2 滚动预热; §1.3 识别加权 | 平板/手机留出 ≥85; 最低 ≥80; 识别无误判; 桌面不降; p90 不升 >10% | Web 80→90 |
| P2 L1 确定性增强(3 周) | §2.1 OCR + 全块裁切 + 边框/圆角估计 + 字体匹配; §2.2 并行读图与分阶段预算 | 留出 ≥60(中间闸), 回环 ≥85, p90 ≤150s | L1 45→70 |
| P3 L2/L3 输入与评分(3 周) | §3.1 a11y 三后端; §3.2 模拟器截图评分; §3.4 静态覆盖 | 三后端夹具测试; L2 留出 ≥60; a11y 样本 ≥9 | L2/L3 50→75 |
| P4 签名 CI 与拆分(2 周, 与 P3 并行) | §3.3 三平台 CI; §5.1 拆分; §5.2 mypy/coverage | 三平台 CI 绿 + Windows 实机冒烟; 拆分逐站 Δ=0; coverage 达标 | 代码 68→90 |
| P5 冲顶(3 周) | §2.1 更强读图模型 + 差异驱动迭代; §4 提速; §6 三轮稳定 | 全部 §0 门槛 | 各维 ≥95 |

每阶段结束出一份与 2026-11-05 同口径的评分表(维度/权重不变), 看得到逐阶段的分数移动。

---

## 8. 需要 cz 提供的资源

| 资源 | 用途 | 阶段 | 没有时的退路 |
|---|---|---|---|
| 多模态模型 Key(支持 bbox grounding 的读图模型 1 个 + 便宜填充模型 1 个; 另一家模型 1 个做描述裁判) | §2.1 读图、§2.3 外判 | P0 起 | 本地 OCR 仍能把留出集推到 ~60; 外判退化为金标像素口径 |
| 模型费用预算 | 留出 8 站 × 3 轮 × 夜间, 估 <¥20/晚(切段并行后 token 不增) | P2 起 | 夜间跑分改为每周 |
| Android keystore(或授权我生成并由 cz 保管) | APK release 签名 | P4 | debug 签名并标注 |
| Apple Developer ID 证书 | macOS 签名/公证 | P4 | ad-hoc 签名, Gatekeeper 需右键打开 |
| Windows 代码签名证书 | .exe 签名 | P4 | self-signed 标注 |
| GitHub Actions: macOS + Windows runner 分钟数(或一台可 SSH 的 Windows 机器) | §3.3 CI 与 Windows 实机冒烟 | P4 | Windows 冒烟改为手动 |
| L2/L3 授权样本: ≥10 个可合法采集的应用(开源 Android 应用 / 自研 / 已授权桌面应用), 其中 ≥4 个只做留出 | §3 语料 | P3 | 用 Flutter/Electron 官方示例应用自建语料(分数可信度较低) |
| 一台可长期授予「辅助功能」权限的 macOS 与一台 Windows 机器 | a11y 采集 | P3 | macOS 本机已可; Windows 后端只做夹具测试 |

**签名 secret 名(只填名字对应的值, 不入库; 缺哪组哪个平台就退回未签名)**:
- Android: `ANDROID_KEYSTORE_BASE64`(keystore 文件 base64)、`ANDROID_KEYSTORE_PASSWORD`、`ANDROID_KEY_ALIAS`、`ANDROID_KEY_PASSWORD`(可省, 缺省同 keystore 口令)
- macOS 签名: `APPLE_CERT_P12_BASE64`(Developer ID Application 证书 .p12 的 base64)、`APPLE_CERT_PASSWORD`、`APPLE_SIGN_IDENTITY`(如 `Developer ID Application: 名字 (TEAMID)`)
- macOS 公证(可选, 需先有签名): `APPLE_ID`、`APPLE_APP_PASSWORD`(App 专用密码)、`APPLE_TEAM_ID`
- Windows: `WINDOWS_CERT_PFX_BASE64`(代码签名证书 .pfx 的 base64)、`WINDOWS_CERT_PASSWORD`

---

## 9. 诚实风险
- L1 真实截图像素口径的上限受字体与反锯齿影响, 75 是估计值; 若 P2 中间闸达不到 60, 调整为「结构+文本通道 ≥80, 像素单列」而不是改口径凑分。
- 三视口采集会让每站采集时间上升, §4 的提速要先于 P1 落地一半, 否则 p90 闸门会卡。
- 拆分 `cloner_engine.py` 风险最低但最耗时, 必须逐模块零回归, 不能一次性大改。
- 任何阶段若发现只能靠特判才能过闸门, 该闸门改成不过, 记入缺口清单。
