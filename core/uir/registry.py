"""InputAdapter / OutputTarget 接口与发现式注册表(DESIGN.md §9.5)。

新增输入源或输出目标: 实现协议 + 装饰器注册, 不改调度器/引擎。
"""
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol, runtime_checkable

from core.uir.model import UIR


@dataclass
class GenRequest:
    """一次生成请求(来自 POST /api/v4/generate 或 bench)。"""
    kind: str                                   # "screenshot" | "description" | "web_dom" | "apk_static" | "a11y"
    image_path: Optional[Path] = None           # 截图输入(参考图)
    description: str = ""                       # 文字描述输入
    viewport: dict = field(default_factory=dict)  # {"width","height"}; 截图输入默认取图宽
    source_path: Optional[Path] = None          # web_dom: 克隆工作空间; apk_static: APK 路径; a11y: 导出的树文件
    hints: dict = field(default_factory=dict)   # 上一轮差异线索等(质量循环回喂)
    meta: dict = field(default_factory=dict)


@dataclass
class BuildContext:
    """适配器/目标共享的运行上下文: 时间预算 / AI 配置 / 日志 / 评分句柄。"""
    ws: Path
    log: Callable[[str, str], None]
    deadline: float                             # time.monotonic() 绝对时刻
    ai_cfg: Optional[dict] = None               # {"base_url","api_key","model"} 或 None(无 Key)
    score: Optional[Callable] = None            # compute_visual_similarity 句柄
    pool: object = None                         # SyncBrowserPool
    extra: dict = field(default_factory=dict)

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0


@dataclass
class RenderResult:
    entry_file: Path                            # 产物入口(如 ws/index.html 或 lib/main.dart)
    shootable: bool                             # 能否被 Playwright 截图参与像素评分
    assets: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@runtime_checkable
class InputAdapter(Protocol):
    kind: str

    def to_uir(self, req: GenRequest, ctx: BuildContext) -> UIR: ...


@runtime_checkable
class OutputTarget(Protocol):
    name: str
    shootable: bool

    def render(self, uir: UIR, out_dir: Path, ctx: BuildContext) -> RenderResult: ...


_INPUTS: dict = {}
_OUTPUTS: dict = {}


def register_input(kind: str):
    def deco(cls):
        cls.kind = kind
        _INPUTS[kind] = cls
        return cls
    return deco


def register_output(name: str):
    def deco(cls):
        cls.name = name
        _OUTPUTS[name] = cls
        return cls
    return deco


def get_input(kind: str):
    _ensure_loaded()
    if kind not in _INPUTS:
        raise KeyError(f"未知输入适配器: {kind!r}; 可用: {sorted(_INPUTS)}")
    return _INPUTS[kind]()


def get_output(name: str):
    _ensure_loaded()
    if name not in _OUTPUTS:
        raise KeyError(f"未知输出目标: {name!r}; 可用: {sorted(_OUTPUTS)}")
    return _OUTPUTS[name]()


def list_inputs() -> list:
    _ensure_loaded()
    return sorted(_INPUTS)


def list_outputs() -> list:
    _ensure_loaded()
    return sorted(_OUTPUTS)


_loaded = False


def _ensure_loaded():
    """首次访问时导入内置适配器/目标包, 触发装饰器注册。"""
    global _loaded
    if _loaded:
        return
    _loaded = True
    import importlib
    for mod in ("core.uir.adapters", "core.uir.targets"):
        importlib.import_module(mod)
