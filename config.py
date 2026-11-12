"""
Enterprise Web Cloner & Flask App Generator - 全局配置中心.

集中管理路径、并发参数、视口预设、资源分类与各类上限值,
所有模块统一从此处导入配置, 保证全平台行为一致。
"""
import os
import pathlib

# ---------------------------------------------------------------------------
# 路径布局
# ---------------------------------------------------------------------------
BASE_DIR = pathlib.Path(__file__).resolve().parent
WORKSPACE_ROOT = BASE_DIR / "workspaces"       # 每个任务一个独立工作空间
CACHE_ROOT = BASE_DIR / "cache_cas"            # 全局 CAS(内容寻址)静态资源池
EXPORTS_DIR = BASE_DIR / "exports"             # 生成的 Flask 工程 ZIP 输出目录
DB_PATH = BASE_DIR / "cloner.db"               # SQLite 主数据库(WAL 模式)


def ensure_dirs():
    """创建运行期所需的全部目录(幂等)。"""
    for directory in (WORKSPACE_ROOT, CACHE_ROOT, EXPORTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


ensure_dirs()

# ---------------------------------------------------------------------------
# 并发与调度
# ---------------------------------------------------------------------------
MAX_CONCURRENT_CONTEXTS = 4        # 常驻 Chromium 最大并发 BrowserContext 槽位
DOWNLOAD_WORKERS = 16              # CAS 并发下载线程数
LOG_RING_SIZE = 5000               # SSE 日志环形队列容量

# ---------------------------------------------------------------------------
# 任务约束
# ---------------------------------------------------------------------------
MAX_URLS_PER_BATCH = 50            # 单批次最多提交站点数
MAX_PAGES_LIMIT = 20               # 同域名爬虫模式下最大子页面数
REQUEST_TIMEOUT = 20               # 单个上游资源请求超时(秒, 浏览器会话降级为总时长)
REQUEST_CONNECT_READ = (6, 10)     # requests 直连: (连接, 读空闲) 超时; 读超时按无数据间隔计, 不限大文件总时长
PAGELOAD_TIMEOUT = 45_000          # Playwright 页面导航超时(毫秒)
REACHABILITY_PROBE_TIMEOUT = 12_000  # 首导航失败后的网络层可达性探测上限(毫秒)
AUTO_SCROLL_SMOOTH = False       # 采集滚动改页内平滑连续滚动(400px/两帧): 防苹果类性能守卫把跳滚判成掉帧降级;
                                 # 机器高负载下页面加载期本身就会降级, 语料未回归前默认关(见 DESIGN 动态形态一节)
AUTO_SCROLL_BUDGET = 25          # 采集期自动滚动总时限(秒)
SHOT_TIMEOUT = 30_000             # 单次截图上限(毫秒), 超时走限高退化
NETWORK_IDLE_TIMEOUT = 9_000       # networkidle 等待上限(毫秒)
NAV_QUIET_MS = 500                # 采集导航静置: 这么久没有新请求
NAV_STALE_MS = 2_500              # 在途超过这么久的请求(长轮询/流式/卡死第三方)不再等
MAX_ASSET_BYTES = 64 * 1024 * 1024     # 单个静态资源体积上限(64MB)
BASELINE_IMG_WAIT_MS = 8000       # 基准截图前等待加载中可见图片的上限
CLONE_VIDEO_WAIT_MS = 1500        # 克隆截图前等首屏 autoplay 视频出帧的上限
CLONE_IMG_WAIT_MS = 2000          # 克隆截图前等懒加载图到齐的上限(资源都在本地, 通常百毫秒级)
BASELINE_IMG_FROZEN_WAIT_MS = 5000  # 时间冻结后补等可见图的上限(版面已静止)
BASELINE_TWEEN_SETTLE_MS = 800    # 时间冻结后让在飞的 JS 补间走完再停 rAF
SCORE_MASK_ADS = True             # 评分时两侧第三方广告位遮成中灰(竞价创意随机, 不计分)
BASELINE_EAGER_MAX = 40           # 基准截图前转 eager 的懒加载图上限(文档序)
BASELINE_IMG_STALL_MS = 4000      # 等待期间连续这么久无一张到齐即放行
HOST_FAIL_TRIP = 3                # 单主机网络层失败(SSL/超时/连接)达此次数即熔断
MAX_ASSETS_PER_TASK = 400          # 单任务最大静态资源条目数
CLONE_SCROLL_MAX_MS = 30000        # 克隆页懒加载滚动的完成时限(超时即判页面脚本卡死)
STATIC_KEEP_FEED = True         # 交互轨评分滚动见页高增长(下拉流)时不把预览/导出换成静态版(静态版流冻结)
STATIC_FEED_MIN_INTER = 50      # 交互轨低于此分(塌了)时仍允许换静态版
CLONE_RESPONSIVE_MS = 8000         # 克隆页主线程看门狗: 加载后这么久执行不了一行脚本即判卡死
RENDER_ASSET_RESERVE = 300         # 名额外预留给"浏览器实际加载过/克隆渲染实际请求"的资源:
                                   # 静态提取先把名额用满(苹果 @font-face 每字重 woff/woff2/ttf 三份,
                                   # 上百个从不加载的字体), 首屏视频这类真用到的反被挤掉
MAX_MOCK_ENTRIES = 300             # 单任务最大录制 API 条目数
MAX_MOCK_BODY_BYTES = 2 * 1024 * 1024  # 单个录制响应体上限(2MB)
MAX_TAPE_BODY_BYTES = 512 * 1024       # VCR 磁带单条响应体上限(512KB)
MAX_TAPE_TOTAL_BYTES = 16 * 1024 * 1024  # VCR 磁带总体积上限(16MB)
TAPE_RECORD = True                      # 采集期磁带录制开关(A/B 实验用)
# 接口录制带"请求指纹"(查询参数 + 请求体摊平的键值对), 回放时在同方法同路径的多条录制里挑最像的一条。
# 门户类页面全页区块打同一个 POST 接口、靠请求体里的键区分时, 只按路径回放会让所有区块拿到同一份响应
API_REQ_SIG = True
# 随机数确定性重放: 采集页与克隆页用同一套按脚本分流的种子发生器替换 Math.random(见 js.SEED_RANDOM_JS)。
# 随机推荐位/随机轮播起始相位在克隆里脚本重跑时抽到与采集时相同的一版; 0 = 关
REPLAY_SEED_RANDOM = True
REPLAY_SEED = 20261111
# 时钟锚定(随种子脚本一起装, 见 js.CLOCK_ANCHOR_JS): 采集各页与克隆回放的 Date 都从采集开始那一刻起走; 0 = 关
REPLAY_CLOCK = True
NARROW_PAIR_TAGS = ("m", "t")   # 哪些窄屏段再开一张 0.92 倍宽的独立首载对照页(确认"内容更少"是该宽度本来如此, 而非没加载完)
# 采集期长定时器改过的周期性状态(轮播帧等)序列化前退回初始值: 克隆里脚本从初始帧起转, 不会与"采集时已转过一格"
# 的 DOM 叠成两格(合成用例 drift_rotator 51→100)。只退 class 词、不动内联样式(TIMER_JOURNAL_STYLE=0): 连内联样式
# 一起退时, 引擎自己改写过位移的轮播被回退盖掉(国内语料 mi 首页探针 100/100/69.5 → 65.5/54.4/54.7); 只退 class 时
# 同一站 99.9/100/69.5 无回退(2026-11-11 本机 A/B)。CLONER_CFG_TIMER_JOURNAL=0 可整体关掉
TIMER_JOURNAL = True
TIMER_JOURNAL_STYLE = False   # 1 = 回退时连内联样式一起退(A/B 用)
# 当前项(class)在兄弟间迁移的同一次定时器回调里, 条目/容器/容器上三层以内的位移与显隐样式一并退回(循环轮播只步进
# 一次时, 轨道 translate 没有周期性证据, 但当前项已被退回: 不一起退就是 class 指第 1 张、位移停在第 2 张)
TIMER_JOURNAL_LINKED = True
API_VARIANTS_PER_PATH = 48              # 同一 方法+路径 最多保留多少份不同请求体的录制
PROXY_CACHE_MAX_BYTES = 8 * 1024 * 1024   # 反向代理兜底单响应缓存上限(8MB)
# 交付产物反代兜底超时(秒): 部署机离原站远/原站慢时, 旧值 20s 会把阻塞请求拖成
# 20s 首屏。兜底只为补未捕获的零星资源, 连不上就快速失败, 不该让访客陪等。
PROXY_FALLBACK_CONNECT_TIMEOUT = 3.05
PROXY_FALLBACK_READ_TIMEOUT = 6

# ---------------------------------------------------------------------------
# 设备视口预设
# ---------------------------------------------------------------------------
VIEWPORTS = {
    "desktop": {"width": 1920, "height": 1080, "device_scale_factor": 1},
    "laptop": {"width": 1440, "height": 900, "device_scale_factor": 1},
    "mobile": {"width": 390, "height": 844, "device_scale_factor": 2},
}

# 手机版(按 UA 分发的站): 采集时的真机仿真口径。iOS Safari UA + 触屏 + 视口 meta 生效
PHONE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1"
)
PHONE_DEVICE = {"is_mobile": True, "has_touch": True}
MOBILE_EDITION = True            # 探测原站是否按手机 UA 分发另一套页面, 是则另克隆一份手机版
MOBILE_EDITION_SIM = 0.45        # 手机 UA 页与桌面页 class/标签集合 Jaccard 低于此值视为另一套页面
MOBILE_EDITION_PROBE_S = 12.0    # 探测页加载上限(秒)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# 静态资源分类映射(扩展名 -> 工作空间子目录)
# ---------------------------------------------------------------------------
EXT_CATEGORIES = {
    ".css": "css",
    ".js": "js", ".mjs": "js", ".cjs": "js",
    ".woff": "fonts", ".woff2": "fonts", ".ttf": "fonts",
    ".otf": "fonts", ".eot": "fonts",
    ".png": "img", ".jpg": "img", ".jpeg": "img", ".gif": "img",
    ".webp": "img", ".svg": "img", ".ico": "img", ".avif": "img", ".bmp": "img",
    ".mp4": "media", ".webm": "media", ".mov": "media",
    ".mp3": "media", ".wav": "media", ".ogg": "media", ".m4a": "media",
}

CONTENT_TYPE_FALLBACK = {
    "text/css": ".css",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "font/woff2": ".woff2",
    "font/woff": ".woff",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "image/x-icon": ".ico",
    "video/mp4": ".mp4",
    "audio/mpeg": ".mp3",
}

# 前端用于展示统计的四类资源桶
STAT_BUCKETS = ("css", "js", "img", "font", "api")

# ---------------------------------------------------------------------------
# V4 工程修复参数(Tier 0, 无 AI 参与)
# ---------------------------------------------------------------------------
MEDIA_SHOT_MAX = 24           # 单页最多从基准截图回填的空白 Canvas/无帧视频数
MAX_FRAME_PAGES = 6            # 单任务最多递归克隆的跨域 iframe 子页面数
MAX_FRAME_DEPTH = 2            # iframe 递归深度上限(主页面为第 0 层)
RENDER_REPAIR_ROUNDS = 6       # 视觉修复迭代轮数上限
REPAIR_MIN_GAIN = 0.5          # 修复循环收敛判定: 缺失集合不变且分数涨幅低于此值即停
FIDELITY_SKIP_ABOVE = 97.0    # 交互轨达到此分时不渲染保真轨(头条取优不受影响)
VIEWPORT_VARIANT_MAX = 40      # 窄屏 DOM 变体: 结构不同的子树超过此数视为内容随机(推荐流), 不注入
VIEWPORT_VARIANT_BYTES = 3_000_000   # 窄屏变体 HTML / 附带样式各自上限
VARIANT_FLUID = True           # 窄屏首载页抽取变体前: 0.92 倍宽对照, 随宽同比的内联 px/img 尺寸属性改写为 vw
VARIANT_FLUID_RESTORE = True   # 随宽对照(0.92/1.128 倍宽来回)后把内联 style/class 复原成首载态再抽变体
NARROW_PHASE_RESET = True        # 窄屏首载页抽变体前也做状态类相位复位(滚动后才展开/轮换的同类组件回到源码 HTML 的首载分配)
LATE_PHASE_RESET = True          # 序列化前复查一次状态类相位复位(首次复位后被页面脚本/重渲染写回的)
VARIANT_ID_ALIGN = True          # 窄屏变体: 容器子项标签序列相同但带 id 的子项只在一侧存在 → 变体根提到容器
VARIANT_ID_ALIGN_MAX = 12        # 上述容器数超过此值视为 id 不稳定(随机 id), 整体不用
CONTENT_KEEP_GROUP = True        # 内容守卫: 同一父元素下成批同类兄弟(横向列表/轮播条目)被脚本重跑删掉、幸存不到 1/3 → 按原顺序放回
VARIANT_PAIR_CONFIRM = True      # 窄屏变体比主页内容少/表单控件少时: 0.92 倍宽对照页(另一次加载)该子树结构相同 → 不算未水合, 采用
BASELINE_PHASE_RESET = True      # 基准截图前也做状态类相位复位(基准与克隆同为源码相位, 布局校准不再把收起项撑回展开高度)
PHASE_RESET_SIBLINGS = True      # 状态类相位复位也认没有 id 的同类兄弟组(按源码里父元素路径 + 子元素标签序列对上才用)
CANVAS_FILL_BANDS = True         # 固化 canvas 撑满容器且宽高比各段恒定 → 平板/手机段写 width:100%+height:auto, 不钉首载宽度的 px
VARIANT_CANVAS_CANON = False     # 结构对比时把已固化的 canvas 按 CANVAS 记(不再给 canvas 父元素出窄屏变体)。默认关: vite 手机 98→60, 窄屏变体里的 canvas 才是该宽度的真状态
VARIANT_CANVAS_FILL = True       # 窄屏变体里脚本写了内联 px 宽高且撑满父元素的 canvas → width:100%+height:auto, 不钉首载宽度的 px
ROOT_INLINE_VARIES_NOSIZE = True  # 根字号(html/body 内联 px)被脚本按屏宽改写时, 桌面宽采集的字体烘焙不烘字号/行高/字距(交回 rem 规则)
NARROW_UA_SPLIT = True           # 手机 UA 首载页之外再开一张桌面 UA 390 页: 只在手机 UA 下才换结构的变体只给触屏显示
NARROW_UA_CLS = True             # 手机 UA 首载页与桌面 UA 同宽页同路径 class 不同(桌面 UA 同宽与主页相同)→ 只给触屏手机段换 class(wix 手机卡片默认收起)
NARROW_PHONE_UA = True           # 窄屏首载对照的手机宽(390)页用手机 UA + 触屏仿真单独上下文加载(与评分真机口径一致)
NARROW_FLUID_PAIR = True         # 手机首载页旁再开一张 0.92 倍宽首载页, 认出只在加载时按宽度写一次的内联尺寸
MAIN_SETTLE_MS = 4000          # 主页懒加载滚动后等 DOM「到位」(文字量/有源图片数/控件数连续不变)的上限(毫秒):
                               # 晚水合的表单/标签页组件(wix 首屏邮箱表单)抽早了克隆整块缺失
VIEWPORT_VARIANT_SETTLE = 4.0  # 窄屏首载页抽取变体前平滑滚动的时限(秒): 触发进视口才起播的入场动画
STATIC_TRACK_MARGIN = 5.0     # 保真轨领先交互轨达此分差时, 试做"静态版"(剥离应用脚本)交付物
                              # 并实测; 静态版也领先交互轨此分差才设为默认预览/导出
VIEWPORT_RELOAD_EARLY = True   # 窄屏首载对照页在主页导航提交后即发起(与主页采集并行), 而非多视口采集之后
VIEWPORT_RELOAD_EARLY_MIN_CPUS = 8  # 核数低于此值不提前: CI(4 核)实测两页并行渲染抢 CPU, 主页懒加载/多视口采集反慢,
                                    # 语料均值 46s -> 57s(固化 148 -> 198s, 多视口内联 56 -> 104s 合计)
FIDELITY_PRESTART = False      # 保真轨 MHTML 在下载阶段后台页预加载。默认关: 预加载页常驻到评分结束(交互轨够高时根本用不上),
                               # CI 4 核同时段对照: 开 均耗时 73s / 关 50.6s(基线 60.6s), 页内 CSS 动画/视频持续抢 CPU
CALIB_INPLACE_GATE = True      # 像素校准先在活页就地注入预评, 不升即不做整页重渲染
SESSION_FETCH_BUDGET = 12      # 下载阶段浏览器会话降级(批量并发直连 + 风控类串行重试)的总时限(秒)
BACKFILL_BUDGET = 20          # 单轮渲染缺失回补的回源时限(秒)
REPAIR_STRUCTURAL_ONLY = True  # 修复循环只为「结构性缺失」(样式表/字体)再渲染一轮: 真实任务日志 97 例多轮修复里
                               # 涨分 >1 的只有 2 例(且随后被回退), 平均每例多花 15s; 图片/脚本/接口缺失补回后不再重渲染评分
REPAIR_STOP_SCORE = 99.5      # 修复循环: 还原度达到此值即停(缺失的只剩不可见请求时不再回补)
REPAIR_TIME_BUDGET = 300       # 修复循环时间预算(秒): 快站快修, 慢站限时收敛(P0-A 速度调度)
AI_TIME_BUDGET = 900           # 含 AI 修复的任务时间预算(秒): 超预算跳过 AI 轮       # 渲染驱动回补循环轮数(本地渲染->收集404->回源补抓);
                               # Vite/webpack 动态 chunk 是"entry->视图chunk->懒路由->
                               # 图片"多层依赖链, 每轮只能剥一层, 浅轮数补不到深层资源
LIVE_BODY_MAX = 1500          # 活体响应体复用: 单任务最多留存的浏览器已加载资源数
LIVE_BODY_BYTES = 256 * 1024 * 1024  # 活体响应体复用: 单任务留存总字节上限
LIVE_HARVEST_MAX = 60          # 活体会话请求差集回补的单轮最大补抓数
LIVE_BODY_HARVEST_BUDGET = 8   # 页面关闭前取回已加载响应体的总时限(秒): 小的先取, 超时剩余交 CAS 并发下载
LIVE_BODY_SINGLE_MAX = 512 * 1024  # 单个响应体超过此大小不经 CDP 取回(慢链路上一次 body() 可阻塞数秒), 交 CAS
JS_REWRITE_MAX_BYTES = 8 * 1024 * 1024  # 参与本地化重写的单个 JS 文件体积上限

# ---------------------------------------------------------------------------
# V4 大模型(OpenAI 兼容)配置
#
# 优先级: 任务级(UI 提交的 ai_api_key/ai_model/ai_base_url)
#         > 环境变量(CLONER_AI_KEY / CLONER_AI_MODEL / CLONER_AI_BASE_URL)
#         > 此处默认值。
# 视觉闭环修复要求模型具备多模态(图像理解)能力; 未配置 Key 时相关功能
# 自动跳过, 不影响确定性克隆管线。
# ---------------------------------------------------------------------------
AI_API_BASE_URL = os.environ.get("CLONER_AI_BASE_URL", "https://api.deepseek.com/v1")
AI_API_KEY = os.environ.get("CLONER_AI_KEY", "")
AI_MODEL = os.environ.get("CLONER_AI_MODEL", "deepseek-v4.1")
AI_REQUEST_TIMEOUT = 90        # 单次大模型请求超时(秒)
AI_CALL_WALL_LIMIT = 180       # 单次 chat_json 墙钟上限(含重试/升档; 上游 keep-alive 时读超时不触发)
AI_PATCH_MAX_TOKENS = 8192     # AI 补丁补全预算基档(推理模型的思考 token 计入, 耗尽时自动×2 重试)
AI_REPAIR_TRIGGER_SCORE = 85.0  # 视觉闭环触发门限: 诚实评分低于此值才调用大模型
AI_REPAIR_TARGET_SCORE = 90.0   # 修复循环的目标分: 达标即停
AI_REPAIR_MAX_ROUNDS = 2       # 视觉闭环最大迭代轮数
AI_VISION_MAX_IMAGE_EDGE = 1280  # 送入大模型前截图最长边压缩上限(像素)
AI_MOCK_MAX_FORMS = 10         # 智能 Mock 最多生成的表单路由数

# ---------------------------------------------------------------------------
# V4 仿真交互层(克隆页前端行为模拟)
# ---------------------------------------------------------------------------
INTERACTION_SIM = True             # 默认启用: 登录点击提示繁忙 / 验证码 60s 倒计时
SIM_LOGIN_FAIL_MESSAGES = (        # 登录类点击随机提示池
    "网络繁忙，请稍后重试",
    "系统繁忙，请稍后再试",
    "网络异常，请稍后重试",
)
SIM_SMS_SENT_MESSAGE = "验证码已发送，请注意查收"
SIM_SMS_COUNTDOWN_SECONDS = 60     # 验证码按钮倒计时秒数

# 主服务监听(可用环境变量覆盖, 便于同机并行起多个独立实例, 如 CLONER_PORT=8898)
HOST = os.environ.get("CLONER_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLONER_PORT", "8899"))

# 平台对外可达地址(导出工程内埋点引擎的上报回传地址;
# 留空则用导出时的宿主机局域网推断, 建议部署时显式配置)
PLATFORM_PUBLIC_URL = os.environ.get("CLONER_PUBLIC_URL", f"http://127.0.0.1:{PORT}")

# 审计日志保留天数(超过自动清理; 0 = 永久保留)
AUDIT_RETENTION_DAYS = int(os.environ.get("CLONER_AUDIT_RETENTION_DAYS", "90"))

# @font-face 下载计划: 只下浏览器会请求的字体(src 首个受支持格式 + 页面有对应字符的 unicode-range 切片)
FONT_PLAN = os.environ.get("CLONER_FONT_PLAN", "1") != "0"
# 回补预取: 活体差集回补 / Mock 资产收割的回源在 CAS 下载期间并发发起
BACKFILL_PREFETCH = os.environ.get("CLONER_BACKFILL_PREFETCH", "1") != "0"
# 首导航静置: 导航前挂请求追踪, load 只被挂起请求拖住时不再等满上限
NAV_STALE_GATE = os.environ.get("CLONER_NAV_STALE_GATE", "1") != "0"
# 修复循环: 结构性缺失(样式表/字体)与上一轮完全相同(回补补不上)即收敛
REPAIR_STRUCT_SAME_STOP = os.environ.get("CLONER_REPAIR_STRUCT_SAME_STOP", "1") != "0"
REPAIR_NEED_STRUCT_GAIN = os.environ.get("CLONER_REPAIR_NEED_STRUCT_GAIN", "1") != "0"  # 回补没补回样式表/字体就不再重渲染
# 无超时的 CDP/关页调用设时限(页面渲染进程卡住时任务不再永久挂起)
NARROW_CLOSE_TIMEOUT_S = float(os.environ.get("CLONER_NARROW_CLOSE_TIMEOUT_S", "15"))
# 干净环境下 163/sohu(7.5MB/3.7MB)快照 0.1~0.2s; 引擎里偶发 90s 不返回。首次 45s 超时后
# 先探测哪些 frame 卡住(进日志), 再 Page.stopLoading 取消挂起的导航/请求重试一次(MHTML_RETRY_TIMEOUT_S)
MHTML_SNAPSHOT_TIMEOUT_S = float(os.environ.get("CLONER_MHTML_SNAPSHOT_TIMEOUT_S", "45"))
MHTML_RETRY_TIMEOUT_S = float(os.environ.get("CLONER_MHTML_RETRY_TIMEOUT_S", "30"))
# 序列化过薄的重导航重试: 就绪时无脚本/子框架/meta 刷新的纯静态小页不重试(没有东西能清空它)
THIN_STATIC_NO_RETRY = os.environ.get("CLONER_THIN_STATIC_NO_RETRY", "1") != "0"
# 布局校准就地预评: 尺寸覆盖先注入活页重截图评分, 不升即不做整页重渲染(与 CALIB_INPLACE_GATE 同口径)
LAYOUT_CALIB_INPLACE_GATE = os.environ.get("CLONER_LAYOUT_CALIB_INPLACE_GATE", "1") != "0"
# 第 8 轮(速度/部署首屏)总开关: CLONER_SPEED8=0 一次关掉本轮全部改动, 供 CI 同时段 A/B
SPEED8 = os.environ.get("CLONER_SPEED8", "1") != "0"
# 交付首屏: 前 N 屏以外、原站加载过的图改原生懒加载(部署后 load 不等页尾图片; 评分截图前转回 eager)
DEPLOY_DEFER_IMG = SPEED8 and os.environ.get("CLONER_DEPLOY_DEFER_IMG", "1") != "0"
DEPLOY_DEFER_SCREENS = float(os.environ.get("CLONER_DEPLOY_DEFER_SCREENS", "2"))
# 交付首屏: HTML 里(脚本块以外)解码后 ≥8KB 的图片/字体 data: URI 外置为 /static/ 文件
DEPLOY_EXTERNALIZE_DATA_URI = SPEED8 and os.environ.get("CLONER_DEPLOY_EXTERNALIZE_DATA_URI", "1") != "0"
# 交付首屏: 原图宽 > 2.5 倍显示宽、只被 <img src> 引用的大图(≥300KB)缩到 max(2×显示宽, 1600px)
DEPLOY_SHRINK_IMG = SPEED8 and os.environ.get("CLONER_DEPLOY_SHRINK_IMG", "1") != "0"
# 一屏放得下的页面采集滚动不再固定等 1s(没有真的滚动, 没有进视口事件)
SHORT_PAGE_NO_SCROLL_WAIT = SPEED8 and os.environ.get("CLONER_SHORT_PAGE_NO_SCROLL_WAIT", "1") != "0"
# 按 CPU 定并发克隆数: 每个克隆同时开主页 + 窄屏对照页 + 保真轨页(3~5 个渲染进程), 4 核机上并发 4 个
# 互相抢 CPU, 单站各阶段都被拉长(CI 4 核同批 20 站: 重页滚动/截图/评分加载 2~3 倍于单跑)。
# 每个克隆至少留 2 个核; 8 核以上不变(上限仍是 MAX_CONCURRENT_CONTEXTS)。CLONER_CFG_MAX_CONCURRENT_CONTEXTS 可覆盖
# 默认关(=1 开): CI 4 核同时段 A/B, 并发 2/3/4 的单站均耗时 61.5/80.1/90.3s, 但整批墙钟 635/576/478s,
# 少并发让单站变快却拖慢整批吞吐; 单用户一次只克隆一个站时并发数不起作用。适合"交互式要单站快"的场景手动开。
# 交付首屏: srcset 候选全落到同一本地文件(按宽出图 CDN 去查询串后同名)时去掉 srcset, 用 src
DEPLOY_SRCSET_DEDUP = SPEED8 and os.environ.get("CLONER_DEPLOY_SRCSET_DEDUP", "1") != "0"
# 窄屏首载页的静置/到位/随宽等待改为各页同时起、逐页取结果(原逐页串行等满)
NARROW_PARALLEL_WAITS = SPEED8 and os.environ.get("CLONER_NARROW_PARALLEL_WAITS", "1") != "0"
# 评分整页截图用 JPEG 编码(=1 开)。默认关: CI 同时段二分里像素级一致的克隆(angular.dev 原 100.0)
# 在 JPEG 下稳定掉到 97.5(结构 0.984 / 边缘 0.942), q100 也一样; 而 CI 上整页截图省时每站不到 1s
SHOT_JPEG = SPEED8 and os.environ.get("CLONER_SHOT_JPEG", "0") == "1"
SHOT_JPEG_QUALITY = int(os.environ.get("CLONER_SHOT_JPEG_QUALITY", "92"))
CPU_AWARE_CONCURRENCY = SPEED8 and os.environ.get("CLONER_CPU_AWARE_CONCURRENCY", "0") == "1"
if CPU_AWARE_CONCURRENCY:
    MAX_CONCURRENT_CONTEXTS = max(1, min(MAX_CONCURRENT_CONTEXTS, (os.cpu_count() or 4) // 2))

# 第二批合成用例的通用修法(2026-11-11 合并线)
# 排在脚本之后的 viewport meta(脚本运行时写的)序列化后挪到第一个脚本前, 克隆里脚本照常改写它
VIEWPORT_META_HOIST = True
# <html>/<body> 的分视口 class 也按 UA 拆分: 手机 UA 首载页变了、桌面 UA 同宽页没变 → 只给触屏手机段
ROOT_CLS_UA_SPLIT = True
# 桌面 UA 的窄屏首载页(平板段/手机桌面 UA 对照)开在全新上下文: 不继承主页的 cookie/localStorage(首访状态)
NARROW_FRESH_CTX = True
# 窄屏首载页(尤其手机 UA)跳到另一个文档(m. 子域/下载引导页)时不作变体来源、其资源不进活体登记; 手机段改用桌面 UA 同宽页
NARROW_SAME_DOC = True
# 窄屏变体运行时只把当前段生效的那份子树留在 DOM(隐藏的备选兄弟仍参与 :nth-child 计数, 奇偶样式整体错位)
VARIANT_DOM_SWAP = True
# 交互轨克隆页打不开时: 修复循环立即收敛, 静态版按交互轨 0 分参与比较(预览/导出不给打不开的页)
CLONE_FAIL_FAST = True
# 评分整页截图前剥掉弹层写回 html/body 的锁滚动声明(两侧同口径; 交付 HTML 本来就剥)
SCORE_UNLOCK_SHOT = True
# 窄屏阶段提速: 0.92 倍宽对照页在收回时读完即关; 每张窄屏页抽完变体即关; 不稳定页的随宽采样不再等稳定
NARROW_PAIR_EARLY_CLOSE = True
NARROW_PAGE_EARLY_CLOSE = True
FLUID_SKIP_UNSTABLE_WAIT = True
# 内容守卫: 被删块原位出现了删除后才插入的同标签同类新块(水合失配后客户端重渲染, 数据少所以更矮)= 换了一份, 不放回
CONTENT_KEEP_NEW_TWIN = True
# 手机 UA 窄屏首载页启动失败时, 同宽桌面 UA 页接管手机段
NARROW_MDESK_FALLBACK = True
# 克隆不写回「站点每次首访自己写」的存储键(主页自然首载就有, 另一全新上下文首访同值): 写回会让克隆脚本按回访者
# 渲染(apple iphone 顶栏 ribbon=下次显示第几条, 平板/手机 81/99.8→42.7/49.8)
STATE_SKIP_SELF_WRITTEN = True
# 内容守卫的新插入观察器在守卫脚本执行时就挂上(只计解析完之后的插入): defer/module 脚本阶段的先插后删也认得出
CONTENT_KEEP_EARLY_ADD = True
# 拦截页识别看主页面标题(「验证码_xx」「安全验证」+ 正文很短): 风控页正文是登录框, 正文话术认不出
BLOCK_TITLE_CHECK = True

# ---- 任务稳定性(调度器) ----
MIN_FREE_DISK_MB = 1024          # 工作区所在卷剩余低于此值时暂停领取新任务(0 关闭); 一轮国内 17 站约占 2GB
TASK_STALE_S = 1800              # 本进程正在执行的任务心跳(updated_at)停了这么久判卡死 → failed
TASK_ORPHAN_S = 120              # 不在本进程执行名单里的 running 任务(工作线程已死)停了这么久即判 failed

# ---------------------------------------------------------------------------
# svg 的 width/height 属性被内联样式覆盖时不当作分视口尺寸(挂载时按宽写一次属性的插画); 0 关(A/B 用)
SVG_ATTR_INLINE_SKIP = True

# 常量环境覆盖(A/B 用): CLONER_CFG_<常量名>=值 覆盖本模块同名的 bool/int/float/str 常量。
# 例 CLONER_CFG_NARROW_PHONE_UA=0 CLONER_CFG_MAIN_SETTLE_MS=0。CI 的 engine_env 输入就是一串这样的赋值,
# 同一分支不改代码就能对照开关; 类型随原值, 解析失败忽略。
# ---------------------------------------------------------------------------
def _apply_env_overrides():
    g = globals()
    for k, v in os.environ.items():
        if not k.startswith("CLONER_CFG_"):
            continue
        name = k[len("CLONER_CFG_"):]
        if name not in g or name.startswith("_"):
            continue
        cur = g[name]
        try:
            if isinstance(cur, bool):
                g[name] = v.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, int):
                g[name] = int(v)
            elif isinstance(cur, float):
                g[name] = float(v)
            elif isinstance(cur, str):
                g[name] = v
        except ValueError:
            pass


_apply_env_overrides()
