"""InputAdapter 内置实现包: 导入即注册。"""
from core.uir.adapters import description, vision, web_dom  # noqa: F401

try:  # D3: 授权 APK 静态适配器(缺失不影响 L1)
    from core.uir.adapters import apk_static  # noqa: F401
except ImportError:  # pragma: no cover
    pass

try:  # L2 动态 / L3 桌面: 可访问性树适配器(实时采集后端按需导入, 缺依赖不影响其余)
    from core.uir.adapters import a11y  # noqa: F401
except ImportError:  # pragma: no cover
    pass
