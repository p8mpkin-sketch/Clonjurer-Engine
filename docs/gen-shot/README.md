# 截图 → 代码直写轨(第 1 版)

- 诊断(旧链路): `diag/DIAG.md`, `diag/577_sheet.jpg`
- 新链路实测: `r2/`(4 个 tuned + 4 个留出, 每张图: 去外框的原图 | 原图宽度产出 | 平板 768 | 手机 390), 跑分表 `r2/REPORT.md`
- `s577_with_hint_cart.jpg`: #577 原图 + 补充要求「配色保持不变, 右上角加一个购物车按钮」在真实界面(隔离实例)提交的产出
- `ui_paste_hint.jpg`: 新的截图输入区(⌘V 粘贴 / 拖放 / 选择, 补充要求输入框)

口径 shot-fit-v1 = 40% 原图相似度(原图宽度下, 与克隆侧同一函数) + 30% 清单达成 + 15% 三端健康度 + 15% 设计。
像素相似度对几像素的错位很敏感, 视觉上已经很接近的页面也只有 40~65, 分数用于同一口径下的前后对比。
复跑: `CLONER_BENCH_DATA=<主仓库> python engine_bench/gen_cases/run_shot_cases.py --group all --workers 4`
