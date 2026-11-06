"""OutputTarget 内置实现包: 导入即注册。"""
from core.uir.targets import html_css  # noqa: F401

try:  # D3: Flutter 最小目标(缺失不影响 Web 路线)
    from core.uir.targets import flutter  # noqa: F401
except ImportError:  # pragma: no cover
    pass

try:  # L3: Electron 桌面目标(复用 html_css, 缺失不影响 Web 路线)
    from core.uir.targets import electron  # noqa: F401
except ImportError:  # pragma: no cover
    pass
