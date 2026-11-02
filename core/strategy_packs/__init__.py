"""
模式策略包(W1 骨架): 每个框架模式一组机制级参数, 由引擎统一消费。

设计原则:
- 策略包是**模式级**的——同一模式所有站点共享同一组参数, 不存在 per-site 配置;
- W1 骨架阶段全部对齐现行全局行为(零回归保证), 后续 W2-VCR 磁带 / W3-SSR 回放 /
  W5-时间冻结 / W6-静态优先降级 逐模式点亮对应开关;
- 未知模式回落 DEFAULT(能力退化而非崩溃)。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyPack:
    pattern: str
    # --- 回放等待策略(评分渲染) ---
    wait_load_timeout: int = 15000        # dcl 后等待 load 事件的上限
    wait_networkidle_timeout: int = 6000  # 网络静置上限
    scroll_pass: bool = True              # 懒加载滚动触发
    settle_ms: int = 2500                 # 截图前缓释上限(DOM 静置即提前结束)
    settle_quiet_ms: int = 500            # DOM 连续静置多久视为稳定(0 = 退回固定等待)
    # --- 资源策略 ---
    backfill_aggressive: bool = True      # failed_local/名字索引回补(恒开, 留档)
    net_tape: bool = False                # W2: VCR 网络磁带回放(JS 驱动模式点亮)
    # --- 冻结策略(W5) ---
    freeze_time: bool = False             # Date/performance.now 固定采集时刻
    freeze_random: bool = False           # Math.random 种子化
    # --- 兜底策略 ---
    static_fallback: bool = True          # SPA 文档路径兜底(W4 已落地, 留档)
    dom_first: bool = False               # W6: JS 回放失败时采集 DOM 直出
    # --- AI 策略 ---
    ai_vision_repair: bool = True         # 模式级 AI 视觉修复开关
    # (M-Next SSR 关闭: 实证 RSC 站上 AI 补丁中性偏负——tailwind 87→44,
    #  redis 42.1→42.1 两轮零收益; 修复补丁对 hydration 页面破坏性强)
    # --- 状态策略(W4) ---
    state_replay: bool = False            # 采集期终态 Cookie(含 httpOnly)/Storage 沙盒回放
    # --- 静态 DOM 权威策略(W7) ---
    disable_hydration: bool = False       # 水合剥离: SSR 模式静态 DOM 为权威, 剥水合脚本防重渲染退化


DEFAULT = StrategyPack(pattern="DEFAULT")

# W2.1: 磁带子集化后重新点亮 —— 仅录 JSON 数据接口(录制+供给双侧排除
# text/x-component RSC 导航载荷/event-stream/multipart), 重走模式级验收。
_TAPE_ON = {"M-React SPA", "M-Webpack水合MPA", "M-Next SSR", "M-Nuxt SSR",
            "M-Vue/Vite SPA", "M-Vue SPA(Material)",
            "M-Remix SSR", "M-SvelteKit SSR", "M-Svelte SPA", "M-Solid SPA",
            "M-Ember SPA", "M-Qwik 可恢复"}
# 模式级 AI 修复豁免(实证: RSC/hydration 页面上 AI 补丁中性偏负)
_AI_OFF = {"M-Next SSR"}
# W4: 状态回放点亮 —— 动态模式的运行时登录态/配置态判断依赖 Storage/Cookie
_STATE_ON = {"M-React SPA", "M-Webpack水合MPA", "M-Next SSR", "M-Nuxt SSR",
             "M-Vue/Vite SPA", "M-Vue SPA(Material)",
             "M-Remix SSR", "M-SvelteKit SSR", "M-Svelte SPA", "M-Solid SPA",
             "M-Ember SPA", "M-Qwik 可恢复"}
# W7: 静态 DOM 权威(水合剥离) —— 采集期渲染 DOM 永远比离线水合完整:
# M-Next 实证: 水合重渲染把完整静态 marquee 退化成单卡列表(87→44);
# W6 扩展: 运行时 JS 重挂载清空 DOM 是 3-16 分空白站的共性根因(vivo 实证)。
# W1.1: Remix/SvelteKit 与 Next 同属"服务端已渲染完整 DOM + 客户端水合"; Astro 岛/
# Qwik 可恢复则以静态 DOM 为交付本体, 离线重执行只会把岛/容器重置为空。
_HYDRATION_STRIP = {"M-Next SSR", "M-React SPA", "M-Nuxt SSR", "M-Webpack水合MPA",
                    "M-Remix SSR", "M-SvelteKit SSR", "M-Astro 静态岛", "M-Qwik 可恢复",
                    "M-Wix CMS"}
# W5: 时间冻结点亮 —— 时变内容(倒计时/日期组件/限时横幅)回放对齐采集时刻。
_FREEZE_TIME = {"M-React SPA", "M-Webpack水合MPA", "M-Next SSR", "M-Nuxt SSR",
                "M-Vue/Vite SPA", "M-Vue SPA(Material)", "M-jQuery传统MPA",
                "M-jQuery传统MPA+iframe门户",
                "M-Remix SSR", "M-SvelteKit SSR", "M-Svelte SPA", "M-Solid SPA",
                "M-Ember SPA", "M-Qwik 可恢复", "M-Astro 静态岛",
                "M-WordPress CMS", "M-Shopify CMS", "M-Wix CMS"}
_PACKS = {
    name: StrategyPack(pattern=name, net_tape=name in _TAPE_ON,
                       ai_vision_repair=name not in _AI_OFF,
                       state_replay=name in _STATE_ON,
                       disable_hydration=name in _HYDRATION_STRIP,
                       freeze_time=name in _FREEZE_TIME)
    for name in (
        "M-STATIC 纯静态",
        "M-jQuery传统MPA",
        "M-jQuery传统MPA+iframe门户",
        "M-React SPA",
        "M-Webpack水合MPA",
        "M-Next SSR",
        "M-Nuxt SSR",
        "M-Vue/Vite SPA",
        "M-Vue SPA(Material)",
        "M-Angular SPA",
        "M-AUTH 传统登录页(jQuery+极验)",
        # W1.1 第二批(与 pattern_detector.classify 一一对应)
        "M-Remix SSR",
        "M-SvelteKit SSR",
        "M-Svelte SPA",
        "M-Solid SPA",
        "M-Ember SPA",
        "M-Astro 静态岛",
        "M-Qwik 可恢复",
        "M-WordPress CMS",
        "M-Shopify CMS",
        "M-Wix CMS",
    )
}


def for_pattern(pattern: str) -> StrategyPack:
    """模式名 → 策略包; 未注册模式回落 DEFAULT。"""
    return _PACKS.get(pattern, DEFAULT)
