# Clonjurer-Engine

高保真网站克隆引擎内核 —— 活体采集、双轨保真评分、AI 视觉修复，并附带可复现的性能基准与逐值等价测试。

本仓库是从完整 Clonjurer 平台中抽取出的「**引擎算法内核 + 测试前端**」，专门用于持续训练引擎的**效率（速度）**与**完成度（还原度）**。
---

## 它是什么 / 用途

把一个网页在离线环境里**高保真复现**（结构 + 视觉 + 文本三个维度），并给出可解释的还原度评分；**离线存档 / 页面复现 / 还原度回归测试**以及引擎算法本身的研究与调优。

典型用法：提交一个 URL → 引擎采集并生成离线工程 → 对照原站截图打出还原度分 → 需要时用 AI 视觉修复补齐样式/布局差异 → 在基准上量化本轮改动对速度与还原度的影响。

## 核心能力

- **采集渲染**：Playwright 活体渲染 + 自动滚动 + 水合稳定等待；资产抓取与本地化重写。
- **还原度评分（诚实相似度 v3）**：
  - 图像通道：fit-to-512 归一后做块级归一化互相关（NCC）+ 边缘 IoU；
  - 文本通道：F2 指纹（CJK 二元组 + 拉丁词，集合交并）；
  - 差异热力图：逐像素差异可视化；
  - 双轨：保真轨（MHTML 快照）与交互轨（Flask 工程）分别评分、任务取优。
- **本地化改写**：前缀树（trie）正则替换表、导航逃逸锚点改写、CSS/srcset 解析、严格 URL 快速路径。
- **框架模式识别**：对 JS/DOM 做小写化 + 字面量/正则指纹，识别前端框架与特征。
- **像素校准**：网格级均值/方差差分（numpy 向量化，行内 float32 精确累加）。
- **AI 视觉修复**：对照原站截图，自动生成并注入 CSS/HTML 补丁修补还原差异。
- **性能工程**：可复现的 0–100 速度评分 + 与冻结基线**逐值等价**的差分模糊测试。

## 架构一览

```
提交 URL
   │
   ▼
[采集] Playwright 渲染 → 自动滚动 → 稳定等待 → 资产下载(smart_downloader)
   │
   ▼
[本地化] HTML/JS/CSS 改写(cloner_engine) → 生成离线 Flask 工程(flask_generator)
   │
   ├──▶ [保真轨] 渲染快照 ─┐
   │                        ├─▶ [评分] 图像 NCC + 边缘 IoU + 文本 F2 + 差异热力图
   └──▶ [交互轨] Flask 工程 ┘            ↑
                                         │
                       [像素校准] 网格差分(pixel_calibrator)
                                         │
                                         ▼
                       [AI 视觉修复] 对照原站截图补丁(ai_vision_repair)
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

## 测试与基准（训练效率 / 完成度的量具）

```bash
# 与冻结基线逐值等价的差分模糊测试（正确性闸门）
python -m unittest discover tests

# 0–100 速度评分 + 逐项正确性闸门 + 记账
python engine_bench/perf/loop.py --label "本轮改动说明"
python engine_bench/perf/bench.py --only score_long   # 单项调试
```

0–100 口径：每项得分 `100 × clamp(ln 提速 / ln 3, 0, 1)`（1x=0、1.73x=50、3x=100），按权重加权平均；任一项输出与冻结基线不一致则该项记 0。详见 [`DESIGN.md`](DESIGN.md) 与 `engine_bench/perf/README.md`。

## 目录结构

```
app.py                     精简版服务端(只保留引擎相关路由 + 前端)
templates/index.html       测试前端
config.py  models.py       配置与数据模型
core/
  cloner_engine.py         采集/本地化/评分主引擎
  flask_generator.py       离线 Flask 工程生成
  browser_pool.py          Playwright 浏览器池
  smart_downloader.py      资产下载
  pattern_detector.py      框架模式识别
  pixel_calibrator.py      像素校准
  ai_mock_generator.py     离线 Mock 响应生成(引擎依赖)
  ai_vision_repair.py      AI 视觉修复
  scheduler.py             任务调度
  strategy_packs/          站点策略包
engine_bench/perf/         速度基准 + 冻结基线 + 等价夹具
tests/test_perf_equivalence.py  差分等价测试
```

## 依赖与可复现性

`requirements.txt` 将 `pillow` / `numpy` **钉版**（`pillow~=13.0.0`、`numpy~=2.5.0`）。原因：还原度/速度的正确性闸门以「与冻结基线逐字节一致」为判据，而评分里部分步骤复刻了 PIL 的重采样行为，其字节输出随 Pillow 版本会变；钉版给逐值闸门一个稳定地基，避免升级底层库悄悄引入还原度回退。详见 `DESIGN.md` 的「可复现性」一节。

## 范围

本仓库只保留**通用引擎算法 + 本地测试前端**；完整平台中的**运营类**功能不在本仓库范围内。

