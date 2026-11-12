# 手机端适配分类测试集

真实站点跑分回答的是「这 20 个站怎么样」, 这里回答的是「每一种手机端适配机制引擎处理得怎么样」。
每个用例是一张只含一种机制的合成页面(`case_site.py`), 起在本机, 不依赖外网, 结果可复现。

```
python engine_bench/mobile_cases/run.py                    # 全部用例 × 6 视口, 约 6 分钟
python engine_bench/mobile_cases/run.py --only timed,lazy  # 只跑某几类 / 某几个用例
python engine_bench/mobile_cases/run.py --gate 95          # 低于门槛退出码 1(CI: .github/workflows/mobile-cases.yml)
```

| 类别 | 用例 | 考的机制 |
|---|---|---|
| 响应式(对照) | responsive_grid / responsive_nav | 纯 CSS 媒体查询, 应当满分, 用来确认口径本身没问题 |
| 按 UA 分发 | ua_server_template / ua_redirect / ua_client_branch | 服务端按 UA 换模板、跳转手机域路径、前端按 UA 换结构 |
| 手机专有组件 | structure_swap | 窄屏渲染另一套 DOM(表单只留邮箱、列表换紧凑版) |
| 脚本重跑 | rerun_list_trim / rerun_block_drop / rerun_append / rerun_state / rerun_measure_probe | 克隆里脚本再跑一次: 接口过期删列表、删整块、重复追加、按存储换状态、隐藏测量哨兵 |
| 懒渲染/滚动进场 | lazy_reveal / lazy_images / lazy_sections | 进视口才显示且可回退、懒加载图、滚到才渲染的区块 |
| 定时状态 | timed_carousel / timed_accordion | 定时轮播(无 id)、进视口后轮流展开的折叠卡 |
| 弹层/滚动锁 | overlay_modal | 全屏弹窗 + body 滚动锁 |
| 吸顶/固定 | pinned_layout | sticky/fixed 头尾 |
| 脚本量宽/rem/canvas | measured_inline / measured_rem / measured_canvas | 加载时量宽写内联 px、rem 基准随屏宽、按容器宽画的 canvas |
| 无 viewport 老页 | legacy_fixed | 980 定宽表格布局 |
| 按宽换图 | media_picture | picture/srcset |
| 脚本排版 | jslayout_masonry / jslayout_masonry_once / jslayout_slider / jslayout_clamp | 瀑布流绝对定位(监听/不监听 resize)、量宽轮播(slide 宽=容器 px)、按宽截断文字 |
| 设备探测 | device_flexible / device_touch / device_matchmedia / device_vhfix / device_container / device_hscroll | 脚本按 DPR 写 viewport + rem、触屏探测加类、matchMedia 分支、innerHeight 首屏、容器查询、横滑条滚到当前项 |

加用例: 在 `case_site.py` 写一个 `case_xxx(ctx)` 返回 HTML, 登记进 `CASES`。用例必须是一种**机制**, 不是某个站的复刻。
暂时修不了的登记进 `KNOWN_GAPS`(照常出分, 不计门槛), 修好后删掉。
