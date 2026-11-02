# Clonjurer-Engine

> **通用 Web 克隆与还原度评估引擎内核** —— 从 Clonjurer 平台抽离的纯引擎能力：
> 一次活体采集，双轨交付（保真轨 MHTML + 交互轨 Flask 工程），三通道诚实评分，AI 视觉闭环修复。
> 本仓库**只含引擎与算法 + 精简前端 + 性能基准**，不含部署编排、远程部署、短链、表单埋点等运营功能。

---

## 1. 这是什么

Clonjurer-Engine 对一个目标页面执行**一次活体渲染采集**，产出两个独立交付物，并对还原度做可复现的量化评分；当还原度不足时，用多模态大模型"看两张图出补丁"迭代修复。

| 轨道 | 资产 | 作用 |
|---|---|---|
| **保真轨** | `capture.mhtml` —— Chromium 渲染期快照（默认禁脚本、脚本剥离） | 像素级还原基准，结构保真**由构造保证**（确定性，回放不跑站点 JS） |
| **交互轨** | `flask_app/` —— 资源本地化 + 接口 Mock + 状态恢复的可运行 Flask 工程 | 可部署、可点击的独立站点 |

任务还原度 = **max(保真轨, 交互轨)**：两条路都能保真时取更优者，诚实不粉饰。

## 2. 引擎设计（DOM 权威双轨制）

核心押注：**采集期的活体渲染结果本身就是交付物**，站点 JS 的离线重执行降级为可选增强。这避免了"让源站 JS 在离线环境重演出原站渲染"这一历史上约 70% 失败的根源（水合重渲染清空 DOM、数据接口离线失败渲染空态、时变/竞态渲染分叉等）。

```
采集期(一次采集, 两个交付物):
  ① 活体渲染 → MHTML 快照(脚本剥离)         = 保真轨资产
  ② DOM 序列化 + 资源本地化 + Flask 工程生成 = 交互轨资产
评分期:
  保真轨: 渲染 MHTML vs 原站截图 → 三通道相似度(确定性, 不跑站点 JS)
  交互轨: 本地回放截图 vs 原站截图 → 同口径三通道
修复期(可选):
  还原度 < 阈值 → AI 视觉闭环: 多模态模型看"原站图 vs 克隆图"产出 CSS/DOM 补丁, 迭代重评
```

## 3. 核心能力

### 3.1 采集与本地化
- **框架模式识别器**（`core/pattern_detector.py`）：JS 字节指纹 + 渲染 DOM 指纹 → 10 类架构模式 → 策略路由。
- **智能下载器**（`core/smart_downloader.py`）：直连 requests 优先，失败降级到同会话 Playwright，TLS 指纹受限站也能取到资源。
- **CAS 内容寻址本地化**：全局资源去重池 + 哈希 token 匹配 + CSS `url()` 本地化改写；资产名字镜像索引可在源站 chunk 失效时补全依赖链。
- **URL/JS/CSS 重写算法**（`core/cloner_engine.py`）：
  - 上千条 URL 替换对用**前缀树正则**一次匹配（"最左起点取最长命中"，与扁平交替语义一致，编译结果按表缓存）；
  - `top/parent.location` 导航逃逸改写用**字面锚点扫描**替代全量正则回溯；
  - 严格形态绝对 URL 走 `urljoin`/`urlsplit` 快速路径。

### 3.2 还原度评分（诚实相似度 v3，`compute_visual_similarity`）
等比缩放到 512 宽 + 纵向 512 分段比较，高度不一致时短图补底色（缺失/多余区域真实失分）：
- **结构通道（0.5）**：分段块级 NCC（归一化互相关）。
- **边缘通道（0.3）**：分段边缘 IoU，numpy 向量化复刻 PIL `FIND_EDGES` + `MaxFilter`（整数卷积 + 阈值，容忍 1px 位移）。
- **文本通道（0.2）**：F2 指纹（召回 + 精确，多余错误文案也扣分），CJK 二元组整数编码 + numpy 去重求交。
- 差异热力图 `diff_preview.png` 并行生成。

### 3.3 像素校准（`core/pixel_calibrator.py`）
采集期固化 computed style（字体族/字号/行高/字距）与格子级（中位色、均匀度）统计，回放期覆盖注入并逐格差分，定位版面/字体漂移。

### 3.4 AI 视觉闭环修复（`core/ai_vision_repair.py`）
多模态模型对"原站截图 vs 克隆截图"出结构化补丁（CSS/DOM），按预算阶梯迭代，重渲染重评分直到达标或预算耗尽。`core/ai_mock_generator.py` 为离线接口生成智能 Mock 响应。

### 3.5 运行时
- `core/browser_pool.py`：Playwright 浏览器池；`core/scheduler.py`：批量任务调度（并发）。
- `app.py`：Flask 平台（提交 URL、任务列表/详情、截图/评分/差异预览、AI 修复、沙盒预览、调度控制、AI Key 设置、导出）。
- 空库自动进入开放模式（无登录摩擦），`core/security.py` 负责会话密钥与 AI Key 的加解密。

## 4. 性能基准（`engine_bench/perf/`）

只度量引擎纯计算路径（评分 / 本地化重写 / 模式识别 / 校准），无需浏览器和网络，与冻结基线（优化前代码）在同进程同夹具交替运行，逐项带**逐值等价正确性闸门**。

```bash
.venv/bin/python -m unittest discover tests      # 等价/正确性(差分模糊测试)
.venv/bin/python engine_bench/perf/bench.py      # 速度基准 + 0–100 评分
.venv/bin/python engine_bench/perf/loop.py --label "..."   # 闸门 + 跑分 + 记账
```

评分口径：单项得分 `100 × clamp(ln 提速 / ln 3, 0, 1)`（1x=0，1.73x=50，≥3x=100），按权重加权；任一项输出与基线不一致记 0 分，单测不过总分 0。详见 `engine_bench/perf/README.md`。

## 5. 快速开始

```bash
python3 -m venv .venv                      # 需 Python 3.13+
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium

CLONER_PORT=8897 .venv/bin/python app.py   # 浏览器打开 http://127.0.0.1:8897
```

生成产物（每任务 `workspaces/<task_id>/`）：`capture.mhtml`（保真轨）、`index.html` + `flask_app/`（交互轨）、`static/`（本地化资源）、`fidelity_preview.png` / `cloned_preview.png` / `diff_preview.png`、`similarity_detail.json`（三通道评分明细）。

## 6. 目录结构

```
├── app.py                   # Flask 平台(引擎 API + 页面; 已移除部署/埋点/短链/Docker 路由)
├── config.py / models.py    # 配置 / SQLite 数据层
├── core/
│   ├── cloner_engine.py     # 引擎主体(采集/本地化/评分/修复循环/双轨)
│   ├── pattern_detector.py  # 框架模式识别(10 模式)
│   ├── pixel_calibrator.py  # 像素校准
│   ├── ai_vision_repair.py  # AI 视觉闭环修复
│   ├── ai_mock_generator.py # 智能接口 Mock 生成
│   ├── flask_generator.py   # 交互轨 Flask 工程生成
│   ├── smart_downloader.py  # 智能下载器(直连 + 浏览器降级)
│   ├── browser_pool.py      # Playwright 浏览器池
│   ├── scheduler.py         # 批量任务调度
│   ├── security.py          # 会话密钥 / AI Key 加解密
│   └── strategy_packs/      # 模式级策略包
├── templates/index.html     # 精简前端(引擎 UI: 提交/任务/结果/AI 修复/日志)
├── static_vendor/           # 前端自托管依赖(Vue/Tailwind/Lucide/字体)
├── engine_bench/perf/       # 速度基准 + 冻结基线 + 等价测试
└── tests/                   # 单元/等价测试
```

## 7. 与完整平台的差异

本仓库是**引擎内核**，相对完整 Clonjurer 平台**移除了运营类功能**：Docker 容器编排、远程一键部署、短链管理、表单埋点追踪（及其路由、界面入口与生成工程中的注入）。保留 `security.py` 作为基础设施。目的：专注引擎算法的**效率与完成度**迭代，保持代码中性、聚焦。

## 8. 合规说明

本引擎仅用于**授权场景**下的站点镜像与还原（自有内容存档 / 授权测试）。不得用于侵犯第三方版权或绕过访问控制的用途。
