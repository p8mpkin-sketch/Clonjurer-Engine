# 真实站点语料跑分

微基准(`engine_bench/perf/`)只度量纯计算路径; 这里度量引擎在真实站点上的**完成度**(还原度、识别命中)与**端到端耗时**, 是"一类一策"的回归面。

## 跑一轮

在能访问平台实例的机器上(实例默认 `http://127.0.0.1:8897`):

```bash
python3 engine_bench/real_sites/run_corpus.py                      # 全语料
python3 engine_bench/real_sites/run_corpus.py --tag cn             # 只跑国内直连站
python3 engine_bench/real_sites/run_corpus.py --sites https://a.com,https://b.com
python3 engine_bench/real_sites/run_corpus.py --user admin --password '...'   # 实例已开鉴权
```

产物在 `runs/`: `run_<ts>.json`(逐站明细)与 `REPORT_<ts>.md`(汇总表, 低分站排最上面, 带与上一轮的 Δ)。
`runs/` 已加入 `.gitignore`, 想留档的报告手动复制到本目录。

## 在 GitHub Actions 上跑

`.github/workflows/corpus.yml`: 改动 `core/`、`config.py` 或语料的 PR 自动跑全量; 也可在 Actions 页手动触发(可填 `tag`/`sites`), 每晚 02:30 全量一次。
runner 有国际网络, 报告直接写进运行摘要, 明细与实例日志作为 `corpus-report` 产物上传。不作门槛, 只做度量留档。

## 读报告

| 列 | 含义 |
|---|---|
| 期望/检出模式 | `corpus.json` 的 `expect` 先验 vs 引擎 `stats.pattern`; `expect` 核对后可修正, 它是假设不是真值 |
| 分 / 交互 / 保真 / 轨 | 任务 `similarity_score` = max(交互轨, 保真轨); 两轨原值与胜出轨都列出 |
| 耗时 / 最慢阶段 | 引擎 `stats.stage_times`(任务级阶段计时, 落库, 失败任务也有) ; 老实例无此字段时用轮询观测兜底(只计运行中, 不含排队) |
| 汇总: 交互轨均分 | 头条分取双轨最优; 交互轨是可交互的交付物, 单独给均分, 防保真轨掩盖交互轨塌陷 |
| 汇总: 网络不可达 / 引擎失败 | 引擎 `stats.failure`: 首导航失败后探测网络层不可达即快速失败(`TargetUnreachable`), 不算引擎缺陷 |

## 规则

- 低分站只能归因到**模式类**(识别错类 / 策略包缺项 / 本地化规则漏项 / 评分口径), 修复必须对整类生效, 严禁按域名特判。
- 评分权重与阈值的改动要先在语料上证明单调改善, 并重新冻结 `engine_bench/perf/baseline/`。
- 语料加站点时写清 `expect` 与 `tags`(`cn` = 国内直连可达, `global` = 需国际网络), 同一模式至少 2 站。
