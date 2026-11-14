"""通用 UI 重建引擎(UIR)配置。

独立于 config.py: 新能力以新模块进入, 不改动共享配置文件。
所有项可用环境变量覆盖(CLONER_GEN_*), 与克隆侧 CLONER_* 约定一致。
"""
import os

import config

# 生成任务在 clone_tasks.target_url 里的标识前缀(调度器据此分流, 不碰 cloner_engine)
TARGET_URL_PREFIX = "uir:generate:"

# 输入暂存(上传截图先落盘, 任务建好后再拷入工作空间)
GEN_INPUTS_DIR = config.BASE_DIR / "gen_inputs"

# 质量循环: 复用克隆侧「快任务快停 / 慢任务限时收敛」语义
GENERATE_TIME_BUDGET = int(os.environ.get("CLONER_GEN_TIME_BUDGET", "240"))   # 秒
GENERATE_TARGET_SCORE = float(os.environ.get("CLONER_GEN_TARGET_SCORE", "85"))  # ≥ 即停
GENERATE_MAX_ROUNDS = int(os.environ.get("CLONER_GEN_MAX_ROUNDS", "4"))
# 截图 + 模型: 读图(参考文本)与骨架填充的墙钟上限(秒), 两者并行; 到点收回多少用多少
GENERATE_READ_BUDGET = float(os.environ.get("CLONER_GEN_READ_BUDGET", "100"))
GENERATE_FILL_ROUNDS = int(os.environ.get("CLONER_GEN_FILL_ROUNDS", "3"))      # 骨架填充候选数(取评分最高)
GENERATE_FILL_WORKERS = int(os.environ.get("CLONER_GEN_FILL_WORKERS", "64"))   # 填充并发请求数
GENERATE_FILL_BUDGET = float(os.environ.get("CLONER_GEN_FILL_BUDGET", "100"))

# 输入上限
GEN_MAX_IMAGE_BYTES = 12 * 1024 * 1024
GEN_MAX_DESC_CHARS = 4000

# 无参考图(描述输入)时的默认渲染视口
GEN_DEFAULT_VIEWPORT = {"width": 1440, "height": 900}

# 确定性视觉适配器(Tier 0)参数
SEG_MIN_REGION_AREA = 0.0015      # 区域最小面积占比(低于此并入父块)
SEG_COLOR_QUANT = 24              # 色彩量化步长(每通道)
SEG_MAX_DEPTH = 4                 # 递归切分深度上限
SEG_MAX_NODES = 400               # 单页 UIR 节点上限
PALETTE_MAX = 8                   # 令牌色板最多颜色数

# 版面分析 v2: 从输入截图裁切回填为图片的面积上限(占整页比例; 0 = 不回填, 用于消融对照)
SEG_PIC_CAP = float(os.environ.get("CLONER_GEN_PIC_CAP", "0.6"))

# ---------------------------------------------------------------------------
# 代码直写轨(文字描述 → 模型直接写可运行网页 → 浏览器体检 → 多模态验收 → 定向修复), 见 core/uir/codegen.py
# ---------------------------------------------------------------------------
# 仓库外的 Key 文件(KEY=VALUE, 只取 CLONER_AI_*): 服务进程没带环境变量启动时, 生成侧仍能拿到模型。置空关闭。
GEN_AI_ENV_FILE = os.environ.get("CLONER_AI_ENV_FILE", "~/.clonjurer_ai.env")
CODE_TARGET_SCORE = float(os.environ.get("CLONER_GEN_CODE_TARGET", "85"))     # 需求符合度 ≥ 且无硬伤即停
CODE_MIN_DESIGN = float(os.environ.get("CLONER_GEN_CODE_MIN_DESIGN", "0.75"))    # 设计分(0~1)低于此值不算达标, 继续按问题清单打磨
CODE_MAX_REPAIRS = int(os.environ.get("CLONER_GEN_CODE_REPAIRS", "2"))        # 首版之后最多修几轮
CODE_MAX_TOKENS = int(os.environ.get("CLONER_GEN_CODE_TOKENS", "16384"))      # 写代码/修复的补全预算基档
# 写代码/修复是否保留模型思考。默认关: 本机实测(同一批 7 条需求)开思考写一页 130~180s、总输出 token 近两倍,
# 均分只高 1 分且有一条因超时失败; 关思考 6~28s 写完, 人工比对截图质量相当
CODE_THINKING = os.environ.get("CLONER_GEN_CODE_THINKING", "0") not in ("0", "false", "")
CODE_PLAN_WALL = float(os.environ.get("CLONER_GEN_PLAN_WALL", "60"))          # 各步墙钟上限(秒)
CODE_WRITE_WALL = float(os.environ.get("CLONER_GEN_WRITE_WALL", "180"))
CODE_REVIEW_WALL = float(os.environ.get("CLONER_GEN_REVIEW_WALL", "75"))
CODE_MIN_REPAIR_LEFT = 45.0       # 剩余预算低于此值不再开修复轮(一轮修复 + 体检 + 验收放不下)
CODE_MAX_HTML_CHARS = 400_000     # 模型产出上限(超出视为异常)
# 体检: 连拍帧间隔(毫秒, 互质以免与动效周期同相)、帧差判定阈值、交互探测上限
PROBE_FRAME_GAPS_MS = (230, 410, 570)
PROBE_DIFF_THRESHOLD = 14         # 灰度差超过此值的像素计为「变了」
PROBE_MOTION_EPS = 0.0004         # 变化像素占比低于此值视为静止
PROBE_MAX_INTERACT = 8
PROBE_REVEAL_STEPS = 14           # 整页截图前逐屏滚动的最多步数(触发滚动渐显 / 懒加载)
PROBE_LINGER_CLICKS = 4            # 静止页面上前几次点击之后多等一会儿, 看画面是否自己继续变
PROBE_LINGER_MS = 1150
PROBE_POST_GAPS_MS = (450, 1150)  # 操作完之后再连拍的间隔: 第二档跨过 1 秒, 能看到按秒走的计时
PROBE_MOBILE_VIEWPORT = {"width": 390, "height": 844}
PROBE_SHORT_VIEWPORT = {"width": 1280, "height": 600}   # 矮窗口(查文字重叠)


def use_code_track(kind: str, target: str, spec: dict = None) -> bool:
    """文字描述 → 网页 走代码直写轨; spec.track="uir" 可强制回到 UIR 原型轨(基准回环 / 无模型的离线测试用)。"""
    return kind == "description" and target == "html_css" and (spec or {}).get("track") != "uir"
