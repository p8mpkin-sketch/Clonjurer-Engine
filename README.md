# Clonjurer-Engine

通用 UI 重建引擎 —— 从网址、截图、文字描述、授权 APK 或无障碍树出发，重建可运行的界面，并用同一套像素评分给出可解释的还原度。产物可以是网站工程、桌面应用（.app / .exe）或 Android 应用（APK）。

本仓库是从完整 Clonjurer 平台中抽取出的「**引擎算法内核 + 测试前端**」，专门用于持续训练引擎的**效率（速度）**与**完成度（还原度）**。

---

## 它是什么 / 用途

- **Web 克隆**：把一个网页在离线环境里**高保真复现**（结构 + 视觉 + 文本），桌面 / 平板 / 手机三视口分别评分，可用于离线存档、页面复现与还原度回归测试。
- **L1 界面生成**：截图或文字描述 → 可运行网站，带评分闭环迭代。
- **L2 / L3 应用重建**：自有或授权 APK 的公开资源、Android / macOS / Windows 无障碍树 → 跨平台界面（HTML/CSS、Flutter、Electron）。
- **交付形态**：任何产出网站工程的任务都可以选择打包成桌面应用或 Android 应用。

典型用法：提交一个 URL（或截图 / 描述）→ 引擎采集或生成离线工程 → 对照参考图打出还原度分 → 需要时用 AI 视觉修复补齐差异 → 按交付形态导出 ZIP / 安装包 → 在基准上量化本轮改动对速度与还原度的影响。

**授权边界**：只重建界面的外观与交互意图。APK 只读 Manifest / res / resources.arsc 等公开资源，不触碰 DEX/so、不脱壳、不绕过任何保护机制；无障碍树只在运行期授权采集。

## 核心能力

- **采集渲染**：Playwright 活体渲染 + 自动滚动 + DOM 静置检测（MutationObserver）；资产流式下载与本地化重写；三视口采集，窄屏 DOM 变体与分视口 class 按视口切换。
- **还原度评分（诚实相似度 v3）**：
  - 图像通道：fit-to-512 归一后做块级归一化互相关（NCC）+ 边缘 IoU；
  - 文本通道：F2 指纹（CJK 二元组 + 拉丁词，集合交并）；
  - 差异热力图：逐像素差异可视化；
  - 双轨：保真轨（MHTML 快照）与交互轨（Flask 工程）分别评分、任务取优；保真轨领先时自动出静态版。
  - 同一评分函数也是 L1/L2/L3 生成质量的奖励函数。
- **本地化改写**：前缀树（trie）正则替换表、导航逃逸锚点改写、CSS/srcset 解析、严格 URL 快速路径。
- **框架模式识别**：React / Vue / Svelte / Astro / Remix / Solid / Qwik / Ember / WordPress / Shopify / Wix 等指纹，置信度随证据数量与来源变化。
- **像素校准**：网格级均值/方差差分（numpy 向量化），与评分共享一次解码。
- **AI 视觉修复**：对照原站截图生成 CSS/HTML 补丁；补丁经消毒，复评不提升即整体回滚。
- **UIR 中间表示**：一切输入归一化为 UIR，一切输出从 UIR 渲染；新能力只以 InputAdapter / OutputTarget 注册进入，Web 侧零回归。
  - 输入适配器（`core/uir/adapters/`）：`web_dom`、`vision`（截图）、`description`（描述）、`layout`（截图版面分析）、`apk_static`、`a11y`（Android uiautomator 已实测；macOS AX / Windows UIA 目前只有夹具测试）。
  - 输出目标（`core/uir/targets/`）：`html_css`、`flutter`、`electron`。
- **交付形态**（`core/app_shell.py` + `core/uir/toolchain.py`）：Android 原生 Activity + 系统 WebView 壳（不走 Flutter/Gradle，构建数秒，example.com 约 25KB）；桌面 Go + 系统 WebView（macOS WKWebView / Windows WebView2，站点 go:embed 进单文件，约 2.5MB）；有签名 secret 时正式签名，否则回退 debug / ad-hoc / 不签并标注「未签名」。
- **性能工程**：可复现的 0–100 速度评分 + 与冻结基线**逐值等价**的差分模糊测试。

## 架构一览

```
 网址            截图 / 描述        授权 APK        无障碍树
   │                 │                 │                │
   ▼                 ▼                 ▼                ▼
[Web 克隆管线]   [vision/description] [apk_static]    [a11y]      ← InputAdapter
 采集 → 本地化        └────────┬────────┴────────────────┘
 → Flask 工程                  ▼
   │                         UIR
   │                           │
   │                ┌──────────┼───────────┐
   │                ▼          ▼           ▼
   │            html_css    flutter     electron                 ← OutputTarget
   │                │
   ├────────────────┴──▶ [评分 v3] NCC + 边缘 IoU + 文本 F2 + 差异热力图
   │                         ↑   └─▶ 低于目标分则带差异线索迭代
   │              [像素校准] / [AI 视觉修复]
   ▼
[交付形态] 网站工程 ZIP │ 桌面应用 .app/.exe │ Android APK
```

## 快速开始

```bash
pip install -r requirements.txt
python -m playwright install chromium          # 首次需要
python app.py                                   # 默认 127.0.0.1:8899
# 可用环境变量换端口并行多实例：
CLONER_PORT=8897 python app.py
```

打开浏览器访问 `http://127.0.0.1:8899/`：提交一个 URL → 等待克隆完成 → 查看原站/克隆/差异截图与还原度评分 → 需要时点「AI 修复」。数据库为空时为开放模式，无需登录。

- **交付形态**：「新建克隆」与「界面生成」里的「交付形态」卡片可选 网站工程（默认）/ 桌面应用 / Android 应用；安装包在任务详情里下载。构建 APK 需要本机 Android SDK，桌面包需要 Go。
- **界面生成**：`POST /api/v4/generate`，`kind=screenshot|description`，可选 `target`（默认 `html_css`）、`viewport`、`target_score` / `max_rounds` / `time_budget`；报告见 `/api/v4/generate/<task_id>/report`。
- **AI 配置**：读图、填充与视觉修复走 OpenAI 兼容接口，用环境变量 `CLONER_AI_KEY` / `CLONER_AI_BASE_URL` / `CLONER_AI_MODEL` 或管理设置页配置。Key 不要写进仓库。没有 Key 时 L1 退回确定性版面分析。

## 测试与基准（训练效率 / 完成度的量具）

```bash
# 与冻结基线逐值等价的差分模糊测试（正确性闸门）
python -m unittest discover tests

# 0–100 速度评分 + 逐项正确性闸门 + 记账
python engine_bench/perf/loop.py --label "本轮改动说明"
python engine_bench/perf/bench.py --only score_long   # 单项调试

# 真实站点语料（需运行中的实例；holdout 组只测不调）
python3 engine_bench/real_sites/run_corpus.py --base http://127.0.0.1:8897
python3 engine_bench/real_sites/run_corpus.py --base http://127.0.0.1:8897 --tag cn   # 国内站，需本机网络

# L1 截图/描述生成用例、a11y 适配器用例
python engine_bench/gen_cases/run_gen_cases.py [--holdout]
python engine_bench/a11y_cases/run_a11y.py [tune|holdout]
```

CI（`.github/workflows/`）：`engine-gate.yml` 在 PR 与 main 上跑逐值等价测试与基准正确性闸门；`corpus.yml` 改动 `core/` 的 PR 自动跑国际站语料、每晚全量一次，只做度量留档。`tests/test_no_site_special_case.py` 守卫「禁止 per-site 特判」：语料域名不得出现在 `core/`。

0–100 口径：每项得分 `100 × clamp(ln 提速 / ln 3, 0, 1)`（1x=0、1.73x=50、3x=100），按权重加权平均；任一项输出与冻结基线不一致则该项记 0。详见 [`DESIGN.md`](DESIGN.md) 与 `engine_bench/perf/README.md`。路线图与门槛见 [`ROADMAP_95.md`](ROADMAP_95.md)，最近一次独立评分见 [`SCORECARD_20261106.md`](SCORECARD_20261106.md)。

## 目录结构

```
app.py                     精简版服务端(引擎相关路由 + 前端)
templates/index.html       测试前端
config.py  models.py       配置与数据模型
core/
  cloner_engine.py         采集/本地化/评分主引擎
  clone/                   从主引擎拆出的模块(注入脚本、辅助函数)
  flask_generator.py       离线 Flask 工程生成
  app_shell.py             交付形态: 网站工程装进桌面 / Android 应用壳
  shell_templates/         应用壳模板(desktop_go)
  browser_pool.py          Playwright 浏览器池
  smart_downloader.py      资产下载
  pattern_detector.py      框架模式识别
  pixel_calibrator.py      像素校准
  ai_mock_generator.py     离线 Mock 响应生成(引擎依赖)
  ai_vision_repair.py      AI 视觉修复
  scheduler.py             任务调度(克隆 / 生成任务分流, 完成后按交付形态打包)
  security.py              安全基座
  strategy_packs/          按模式类的策略包
  uir/
    model.py  schema.json  UIR 中间表示与 JSON Schema
    registry.py            InputAdapter / OutputTarget 发现式注册
    generate.py            生成任务引擎(评分闭环迭代)
    llm.py  settings.py    模型调用(墙钟上限、关思考回退)与生成参数
    render.py              渲染与截图
    toolchain.py           APK / 桌面构建与签名
    adapters/              web_dom / vision / description / layout / apk_static / a11y
    targets/               html_css / flutter / electron
engine_bench/
  perf/                    速度基准 + 冻结基线 + 等价夹具
  real_sites/              真实站点语料(含 holdout)与跑分脚本
  gen_cases/               L1 生成用例
  a11y_cases/              无障碍树用例(tune / holdout)
tests/                     单元与离线端到端测试(浏览器相关用例缺依赖时跳过)
```

## 依赖与可复现性

`requirements.txt` 将 `pillow` / `numpy` **钉版**（`pillow~=13.0.0`、`numpy~=2.5.0`）。原因：还原度/速度的正确性闸门以「与冻结基线逐字节一致」为判据，而评分里部分步骤复刻了 PIL 的重采样行为，其字节输出随 Pillow 版本会变；钉版给逐值闸门一个稳定地基，避免升级底层库悄悄引入还原度回退。详见 `DESIGN.md` 的「可复现性」一节。

## 范围

本仓库只保留**通用引擎算法 + 本地测试前端**；完整平台中的**运营类**功能不在本仓库范围内。改进必须是通用算法，严禁按站点或按应用的特判。

