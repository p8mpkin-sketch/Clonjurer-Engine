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
