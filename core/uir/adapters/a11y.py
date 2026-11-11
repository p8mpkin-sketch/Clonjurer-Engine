"""可访问性树 → UIR(L2 动态 / L3 桌面输入, ROADMAP_95.md §3.1, DESIGN.md §9.7/§9.8)。

一个适配器 + 三个采集后端。后端只负责把操作系统公开的无障碍树 dump 成统一树 JSON,
映射到 UIR 的逻辑全部在 `tree_to_uir`(纯函数, 确定性, 不依赖设备):

  | 后端    | 采集                                         | 依赖                                   |
  |---------|----------------------------------------------|----------------------------------------|
  | android | `adb shell uiautomator dump` → XML           | adb + 已授权设备/模拟器                |
  | macos   | AX API(pyobjc AXUIElement)按 pid 递归        | pyobjc-framework-ApplicationServices + 辅助功能权限 |
  | windows | UI Automation(`uiautomation` 或 `pywinauto`) | Windows 机器                            |

统一树 JSON(夹具与后端输出同一格式):
  {"platform": "android|macos|windows", "app": "...", "scale": 2.75,   # 物理像素 / 逻辑单位
   "window": [x, y, w, h],                                              # 物理像素, 根窗口
   "root": {"role": "<平台原生角色/类名>", "name": "", "value": "", "desc": "", "id": "",
            "bounds": [x, y, w, h], "clickable": bool, "editable": bool, "password": bool,
            "checked": bool|None, "enabled": bool, "children": [...]}}

硬边界(DESIGN.md §9.1): 只读操作系统公开的无障碍接口, 只对用户自有/授权应用做运行期采集;
不注入、不 hook、不反编译、不读取进程内存或安装包代码。实时采集要求调用方显式声明授权
(req.meta["authorized"] = True), 否则拒绝。树里只取外观与交互意图(角色/文本/位置/可点击),
不取任何业务数据以外的内部实现。

映射规则(全部通用, 无 per-app 特判):
  - 角色: 三个平台的原生角色归一到同一 UIR role 闭集(Android 类名复用 apk_static 映射表);
  - 几何: bounds / scale → 逻辑单位; 子节点裁剪到父节点可见区域, 零面积/不可见节点丢弃;
  - 冗余包装: 无名、无交互、与唯一子节点同框的容器折叠;
  - 布局: 子节点在纵轴两两不重叠 → flex 列(margin 承载间距); 横轴不重叠 → flex 行; 否则 stack + 绝对定位;
  - 文本字号: 单行按框高, 多行按「面积 / 字符数」反解, 钳制到 [10, 32];
  - 可选参考截图: 采样背景色 / 文字色, 小图标与图片区域裁剪为内联 PNG(有总量上限)。
"""
import base64
import io
import json
import math
import re
import shutil
import subprocess
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from core.uir.model import UIR, Node, Tokens
from core.uir.registry import register_input, GenRequest, BuildContext

try:  # Android 类名映射复用 L2 静态适配器(同一 role 枚举)
    from core.uir.adapters.apk_static import _ROLE as _ANDROID_STATIC_ROLE
except ImportError:  # pragma: no cover
    _ANDROID_STATIC_ROLE = {}

PLATFORMS = ("android", "macos", "windows")

# ---------------------------------------------------------------------------
# 角色归一
# ---------------------------------------------------------------------------
_ANDROID_ROLE = dict(_ANDROID_STATIC_ROLE)
_ANDROID_ROLE.update({
    "TextView": "text", "CheckedTextView": "text", "EditText": "input", "AutoCompleteTextView": "input",
    "Button": "button", "ImageButton": "button", "ToggleButton": "button", "CompoundButton": "button",
    "Switch": "button", "SwitchCompat": "button", "CheckBox": "button", "RadioButton": "button",
    "ImageView": "image", "RecyclerView": "list", "ListView": "list", "GridView": "list",
    "ScrollView": "box", "HorizontalScrollView": "box", "NestedScrollView": "box", "WebView": "box",
    "Toolbar": "header", "ActionBar": "header", "TabWidget": "nav", "TabLayout": "nav",
    "BottomNavigationView": "nav", "NavigationBarView": "nav", "ProgressBar": "badge", "SeekBar": "input",
    "Spinner": "button", "RatingBar": "badge", "View": "box",
})

_AX_ROLE = {
    "AXStaticText": "text", "AXHeading": "text", "AXTextField": "input", "AXTextArea": "input",
    "AXSecureTextField": "input", "AXSearchField": "input", "AXComboBox": "input",
    "AXButton": "button", "AXPopUpButton": "button", "AXMenuButton": "button", "AXCheckBox": "button",
    "AXRadioButton": "button", "AXDisclosureTriangle": "button", "AXSwitch": "button", "AXToggle": "button",
    "AXLink": "link", "AXImage": "image", "AXList": "list", "AXTable": "table", "AXOutline": "list",
    "AXCollection": "list", "AXRow": "list_item", "AXCell": "list_item", "AXToolbar": "header",
    "AXTabGroup": "nav", "AXMenuBar": "nav", "AXSplitter": "divider", "AXProgressIndicator": "badge",
    "AXBusyIndicator": "badge", "AXLevelIndicator": "badge", "AXSlider": "input", "AXIncrementor": "button",
    "AXWindow": "box", "AXSheet": "overlay", "AXDrawer": "overlay", "AXPopover": "overlay",
    "AXGroup": "box", "AXScrollArea": "box", "AXSplitGroup": "box", "AXLayoutArea": "box",
    "AXWebArea": "box", "AXRadioGroup": "box", "AXMenu": "overlay", "AXMenuItem": "list_item",
    "AXMenuBarItem": "button", "AXColumn": "box", "AXValueIndicator": "badge", "AXScrollBar": "box",
}
_UIA_ROLE = {
    "Text": "text", "Edit": "input", "Document": "box", "ComboBox": "input", "Spinner": "input",
    "Button": "button", "SplitButton": "button", "CheckBox": "button", "RadioButton": "button",
    "Hyperlink": "link", "Image": "image", "List": "list", "DataGrid": "table", "Table": "table",
    "Tree": "list", "ListItem": "list_item", "TreeItem": "list_item", "DataItem": "list_item",
    "MenuBar": "nav", "Menu": "overlay", "MenuItem": "list_item", "ToolBar": "header", "TitleBar": "header",
    "Tab": "nav", "TabItem": "button", "StatusBar": "footer", "Separator": "divider", "ProgressBar": "badge",
    "Slider": "input", "Window": "box", "Pane": "box", "Group": "box", "Custom": "box", "Header": "header",
    "HeaderItem": "text", "ScrollBar": "box", "Thumb": "box", "ToolTip": "overlay", "AppBar": "header",
    "Calendar": "box", "SemanticZoom": "box",
}
# 勾选/单选/开关类控件的原生角色名: 采集后端没给出勾选状态时也按图形控件处理(方框/圆点是画出来的, 不是文字)
_TOGGLE_RAW = {"CheckBox", "RadioButton", "Switch", "SwitchCompat", "ToggleButton", "AXCheckBox", "AXRadioButton", "AXSwitch"}
# 进度条/评分条/电量条: 画出来的量表, 可访问名(如"Progress")并不显示在屏幕上
_GAUGE_RAW = {"ProgressBar", "RatingBar", "AXProgressIndicator", "AXLevelIndicator", "AXBusyIndicator"}
# 不参与界面重建的系统装饰(滚动条/窗口按钮等): 渲染出来只会是噪声
_SKIP_RAW = {"AXScrollBar", "AXValueIndicator", "ScrollBar", "Thumb", "AXGrowArea"}


def _android_short(cls: str) -> str:
    return (cls or "").rsplit(".", 1)[-1].split("$")[-1]


def normalize_role(platform: str, raw: str, node: dict = None) -> str:
    """平台原生角色/类名 → UIR role(闭集)。未知类名按交互属性兜底, 不猜语义。"""
    node = node or {}
    if platform == "android":
        short = _android_short(raw)
        role = _ANDROID_ROLE.get(short)
        if role is None:
            # 自定义控件按后缀/属性兜底(MaterialButton/xxxTextView/...), Compose 视图常为 View + 属性
            for suffix, r in (("Button", "button"), ("TextView", "text"), ("EditText", "input"),
                              ("ImageView", "image"), ("RecyclerView", "list"), ("Layout", "box")):
                if short.endswith(suffix):
                    role = r
                    break
        if role is None:
            role = "input" if node.get("editable") else "box"
    elif platform == "macos":
        role = _AX_ROLE.get(raw, "box")
    elif platform == "windows":
        role = _UIA_ROLE.get(re.sub(r"Control$", "", raw or ""), "box")
    else:
        role = "box"
    if node.get("editable") and role in ("box", "text"):
        role = "input"
    return role


# ---------------------------------------------------------------------------
# 后端 1: Android uiautomator
# ---------------------------------------------------------------------------
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def _flag(v) -> bool:
    return str(v).lower() == "true"


def parse_uiautomator_xml(xml_text, density_dpi: int = None, app: str = "") -> dict:
    """`uiautomator dump` 的 XML → 统一树 JSON。density_dpi(如 440)→ scale = dpi/160。"""
    root_el = ET.fromstring(xml_text if isinstance(xml_text, (bytes, str)) else str(xml_text))
    scale = round(density_dpi / 160.0, 4) if density_dpi else 1.0

    def conv(el) -> dict:
        a = el.attrib
        m = _BOUNDS_RE.match(a.get("bounds", ""))
        x1, y1, x2, y2 = (int(g) for g in m.groups()) if m else (0, 0, 0, 0)
        cls = a.get("class", "")
        return {
            "role": cls, "name": a.get("text", ""), "value": "", "desc": a.get("content-desc", ""),
            "id": a.get("resource-id", ""), "bounds": [x1, y1, x2 - x1, y2 - y1],
            "clickable": _flag(a.get("clickable")) or _flag(a.get("long-clickable")),
            "editable": _android_short(cls) in ("EditText", "AutoCompleteTextView") or cls.endswith("EditText"),
            "password": _flag(a.get("password")),
            "checked": _flag(a.get("checked")) if _flag(a.get("checkable")) else None,
            "enabled": a.get("enabled", "true") != "false", "package": a.get("package", ""),
            "children": [conv(c) for c in el if c.tag == "node"],
        }

    tops = [conv(c) for c in root_el if c.tag == "node"]
    if not tops:
        raise ValueError("uiautomator dump 不含 node")
    # 多窗口(状态栏/输入法/弹窗)时取面积最大的应用窗口为根, 其余作为浮层并入(保留可见遮挡)
    tops.sort(key=lambda n: -(n["bounds"][2] * n["bounds"][3]))
    main = tops[0]
    for extra in tops[1:]:
        if extra.get("package") in ("com.android.systemui",):
            continue
        main["children"].append(extra)
    x, y, w, h = main["bounds"]
    return {"platform": "android", "app": app or main.get("package", ""), "scale": scale,
            "window": [x, y, w, h], "root": main}


def _adb(serial, *args, adb=None, timeout=30, binary=False):
    exe = adb or shutil.which("adb")
    if not exe:
        raise FileNotFoundError("缺 adb(Android SDK platform-tools)")
    cmd = [exe] + (["-s", serial] if serial else []) + list(args)
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} 失败: {p.stderr.decode('utf-8', 'replace')[:300]}")
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def capture_android(serial: str, out_dir: Path = None, adb: str = None, screenshot: bool = True) -> dict:
    """授权设备/模拟器上的当前前台界面 → 统一树 JSON(+ 可选截图路径 tree["screenshot"])。
    只调用 uiautomator dump / wm density / screencap 三个公开命令。"""
    remote = "/sdcard/uir_a11y_dump.xml"
    last = ""
    for _ in range(3):  # 界面在动画中 dump 偶发 "could not get idle state", 重试
        last = _adb(serial, "shell", "uiautomator", "dump", remote, adb=adb, timeout=40)
        if "dumped to" in last.lower():
            break
        time.sleep(1.0)
    xml = _adb(serial, "exec-out", "cat", remote, adb=adb, binary=True)
    _adb(serial, "shell", "rm", "-f", remote, adb=adb)
    m = re.search(r"(\d+)\s*$", _adb(serial, "shell", "wm", "density", adb=adb).strip().splitlines()[-1])
    dpi = int(m.group(1)) if m else None
    focus = _adb(serial, "shell", "dumpsys", "window", "displays", adb=adb, timeout=20)
    fm = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([^/\s}]+)", focus)
    tree = parse_uiautomator_xml(xml, dpi, app=fm.group(1) if fm else "")
    if screenshot and out_dir:
        png = _adb(serial, "exec-out", "screencap", "-p", adb=adb, binary=True, timeout=30)
        if png[:8] == b"\x89PNG\r\n\x1a\n":
            p = Path(out_dir) / "a11y_reference.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(png)
            tree["screenshot"] = str(p)
    return tree


# ---------------------------------------------------------------------------
# 后端 2: macOS AX(pyobjc)
# ---------------------------------------------------------------------------
_AX_ATTRS = ("AXRole", "AXSubrole", "AXTitle", "AXValue", "AXDescription", "AXHelp", "AXPlaceholderValue",
             "AXIdentifier", "AXEnabled", "AXPosition", "AXSize", "AXChildren", "AXRoleDescription")


def walk_ax(elem, get_attr, max_depth: int = 40, max_nodes: int = 4000) -> dict:
    """通用 AX 递归: get_attr(elem, name) → 值(位置/尺寸已转成 (x,y)/(w,h) 元组)。
    把取值函数注入, 使递归逻辑可用假元素做确定性测试。"""
    budget = [max_nodes]

    def text(v):
        if isinstance(v, str):
            return v
        return "" if v is None or isinstance(v, (list, tuple, dict)) else str(v)

    def rec(e, depth):
        budget[0] -= 1
        role = get_attr(e, "AXRole") or ""
        pos = get_attr(e, "AXPosition") or (0, 0)
        size = get_attr(e, "AXSize") or (0, 0)
        value = get_attr(e, "AXValue")
        sub = get_attr(e, "AXSubrole") or ""
        node = {
            "role": role, "subrole": sub, "name": text(get_attr(e, "AXTitle")),
            "value": text(value) if not isinstance(value, bool) else "",
            "desc": text(get_attr(e, "AXDescription")) or text(get_attr(e, "AXPlaceholderValue")),
            "id": text(get_attr(e, "AXIdentifier")),
            "bounds": [float(pos[0]), float(pos[1]), float(size[0]), float(size[1])],
            "clickable": role in ("AXButton", "AXLink", "AXCheckBox", "AXRadioButton", "AXPopUpButton",
                                  "AXMenuButton", "AXMenuItem", "AXTabButton", "AXDisclosureTriangle"),
            "editable": role in ("AXTextField", "AXTextArea", "AXSecureTextField", "AXSearchField", "AXComboBox"),
            "password": role == "AXSecureTextField" or sub == "AXSecureTextField",
            "checked": bool(value) if role in ("AXCheckBox", "AXRadioButton") and isinstance(value, (int, bool)) else None,
            "enabled": get_attr(e, "AXEnabled") is not False, "children": [],
        }
        if depth < max_depth:
            for c in get_attr(e, "AXChildren") or []:
                if budget[0] <= 0:
                    break
                node["children"].append(rec(c, depth + 1))
        return node

    return rec(elem, 0)


def _ax_getter():
    """pyobjc 版取值函数; 缺依赖抛 ImportError, 未授权抛 PermissionError。"""
    import ApplicationServices as AS  # noqa: N813  (pyobjc-framework-ApplicationServices)

    if not AS.AXIsProcessTrusted():
        raise PermissionError("macOS 未授予辅助功能权限: 系统设置 → 隐私与安全性 → 辅助功能, 打开当前宿主进程")

    def get(e, name):
        err, v = AS.AXUIElementCopyAttributeValue(e, name, None)
        if err != 0 or v is None:
            return None
        if name in ("AXPosition", "AXSize"):
            typ = AS.kAXValueCGPointType if name == "AXPosition" else AS.kAXValueCGSizeType
            ok, st = AS.AXValueGetValue(v, typ, None)
            if not ok:
                return None
            return (st.x, st.y) if name == "AXPosition" else (st.width, st.height)
        if name == "AXChildren":
            return list(v)
        if isinstance(v, (str, bool, int, float)):
            return v
        try:
            return str(v)
        except Exception:  # pragma: no cover
            return None

    return AS, get


def capture_macos(pid: int, window_index: int = 0, out_dir: Path = None, screenshot: bool = True,
                  max_nodes: int = 4000) -> dict:
    """按 pid 读取应用第 window_index 个窗口的 AX 树 → 统一树 JSON。坐标为屏幕点(逻辑单位, scale=1)。"""
    AS, get = _ax_getter()
    app = AS.AXUIElementCreateApplication(int(pid))
    wins = get(app, "AXWindows") or []
    if not wins:
        err, v = AS.AXUIElementCopyAttributeValue(app, "AXWindows", None)
        wins = list(v or [])
    if not wins:
        raise RuntimeError(f"pid {pid} 无可访问窗口(应用未运行/最小化/无窗口)")
    win = wins[min(window_index, len(wins) - 1)]
    root = walk_ax(win, get, max_nodes=max_nodes)
    x, y, w, h = root["bounds"]
    title = get(app, "AXTitle") or ""
    tree = {"platform": "macos", "app": title, "scale": 1.0, "window": [x, y, w, h], "root": root}
    if screenshot and out_dir and shutil.which("screencapture") and w > 0 and h > 0:
        p = Path(out_dir) / "a11y_reference.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["screencapture", "-x", "-R", f"{int(x)},{int(y)},{int(w)},{int(h)}", str(p)],
                           capture_output=True, timeout=20)
        if r.returncode == 0 and p.exists() and p.stat().st_size > 0:
            tree["screenshot"] = str(p)
            tree["screenshot_scale"] = _png_scale(p, w)
    return tree


def _png_scale(png_path: Path, logical_w: float) -> float:
    try:
        from PIL import Image
        with Image.open(png_path) as im:
            return round(im.size[0] / float(logical_w), 4) if logical_w else 1.0
    except Exception:
        return 1.0


# ---------------------------------------------------------------------------
# 后端 3: Windows UI Automation
# ---------------------------------------------------------------------------
def parse_uia(elem: dict, scale: float = 1.0, app: str = "") -> dict:
    """UIA 元素字典(ControlType / Name / Value / AutomationId / BoundingRectangle[l,t,r,b] /
    IsEnabled / IsPassword / Children) → 统一树 JSON。capture_windows 与夹具共用。"""
    def conv(e):
        ct = re.sub(r"Control$", "", str(e.get("ControlType") or e.get("ControlTypeName") or "Pane"))
        l, t, r, b = (list(e.get("BoundingRectangle") or [0, 0, 0, 0]) + [0, 0, 0, 0])[:4]
        toggle = e.get("ToggleState")
        return {
            "role": ct, "name": str(e.get("Name") or ""), "value": str(e.get("Value") or ""),
            "desc": str(e.get("HelpText") or ""), "id": str(e.get("AutomationId") or ""),
            "bounds": [l, t, max(0, r - l), max(0, b - t)],
            "clickable": ct in ("Button", "SplitButton", "Hyperlink", "MenuItem", "TabItem", "CheckBox",
                                "RadioButton", "ListItem", "TreeItem") or bool(e.get("IsInvokable")),
            "editable": ct in ("Edit", "ComboBox") and not e.get("IsReadOnly", False),
            "password": bool(e.get("IsPassword")),
            "checked": (toggle in (1, "On", True)) if toggle is not None else None,
            "enabled": e.get("IsEnabled", True) is not False,
            "children": [conv(c) for c in (e.get("Children") or [])],
        }

    root = conv(elem)
    return {"platform": "windows", "app": app or root["name"], "scale": float(scale or 1.0),
            "window": list(root["bounds"]), "root": root}


_WIN_CONSOLE_CLASSES = ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PseudoConsoleWindow")


def capture_windows(pid: int = None, title: str = None, max_depth: int = 40, max_nodes: int = 4000) -> dict:
    """Windows 实时采集(需 Windows + `uiautomation` 包)。在非 Windows 上抛 OSError。"""
    import sys
    if not sys.platform.startswith("win"):
        raise OSError("Windows UIA 后端只能在 Windows 上运行; 其他平台请用 parse_uia 处理导出的树 JSON")
    import uiautomation as auto  # pragma: no cover  (Windows only)

    win = None
    if pid:  # pragma: no cover
        # uiautomation 的查找条件里没有进程号, 自己在顶层窗口里按 ProcessId 找; 控制台宿主窗口不算应用窗口
        for c in auto.GetRootControl().GetChildren():
            if c.ProcessId == pid and c.ClassName not in _WIN_CONSOLE_CLASSES and c.BoundingRectangle.width() > 0:
                win = c
                break
    if win is None:  # pragma: no cover
        if not title:
            raise LookupError(f"进程 {pid} 没有顶层应用窗口")
        win = auto.WindowControl(searchDepth=1, Name=title)
    budget = [max_nodes]

    def rec(c, depth):  # pragma: no cover
        budget[0] -= 1
        r = c.BoundingRectangle
        d = {"ControlType": c.ControlTypeName, "Name": c.Name, "AutomationId": c.AutomationId,
             "BoundingRectangle": [r.left, r.top, r.right, r.bottom], "IsEnabled": c.IsEnabled,
             "IsPassword": getattr(c, "IsPassword", False), "Children": []}
        try:
            vp = c.GetValuePattern()
            d["Value"] = vp.Value
        except Exception:
            pass
        if depth < max_depth:
            for ch in c.GetChildren():
                if budget[0] <= 0:
                    break
                d["Children"].append(rec(ch, depth + 1))
        return d

    scale = 1.0
    top = rec(win, 0)  # pragma: no cover
    try:  # pragma: no cover
        import ctypes
        from ctypes import wintypes
        scale = ctypes.windll.user32.GetDpiForWindow(win.NativeWindowHandle) / 96.0
        # 窗口矩形含左右下各约 7px 的不可见缩放边(截图上是窗口后面的桌面): 窗口范围取 DWM 的可见边框矩形
        vis = wintypes.RECT()
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(win.NativeWindowHandle), 9,
                                                      ctypes.byref(vis), ctypes.sizeof(vis)) == 0:
            l, t, r, b = top["BoundingRectangle"]
            if l <= vis.left < vis.right <= r and t <= vis.top < vis.bottom <= b:
                top["BoundingRectangle"] = [vis.left, vis.top, vis.right, vis.bottom]
    except Exception:
        pass
    return parse_uia(top, scale=scale)  # pragma: no cover


# ---------------------------------------------------------------------------
# 统一树 → UIR(纯函数)
# ---------------------------------------------------------------------------
_TOL = 2  # 逻辑单位: 判定重叠/同框的容差


class _Sampler:
    """参考截图采样(可选)。坐标为逻辑单位, 内部乘 px_scale 换到截图像素。"""

    def __init__(self, png_path, px_scale: float, origin=(0, 0), embed_budget: int = 4_000_000):
        from PIL import Image
        import numpy as np
        self.np = np
        with Image.open(png_path) as im:
            self.img = im.convert("RGB")
        self.arr = np.asarray(self.img)
        self.s = float(px_scale or 1.0)
        self.ox, self.oy = origin
        self.budget = embed_budget
        self.core_stroke = False   # 子像素渲染(ClearType)平台: 笔画粗细只数"更接近字色"的核心像素, 见 ink()

    def _crop(self, x, y, w, h):
        s = self.s
        H, W = self.arr.shape[:2]
        x1, y1 = max(0, int(round((x - self.ox) * s))), max(0, int(round((y - self.oy) * s)))
        x2, y2 = min(W, int(round((x - self.ox + w) * s))), min(H, int(round((y - self.oy + h) * s)))
        if x2 <= x1 or y2 <= y1:
            return None
        return self.arr[y1:y2, x1:x2]

    @staticmethod
    def _hex(c) -> str:
        return "#%02x%02x%02x" % tuple(int(v) for v in c)

    def _mode(self, px):
        np = self.np
        q = (px.reshape(-1, 3) >> 3).astype(np.int32)        # 5 bit 量化, 抗锯齿不抖
        keys = q[:, 0] * 1024 + q[:, 1] * 32 + q[:, 2]
        vals, counts = np.unique(keys, return_counts=True)
        order = np.argsort(-counts, kind="stable")
        return vals, counts, order, keys

    def bg(self, x, y, w, h):
        """框内边缘一圈的众数色(容器背景)。"""
        a = self._crop(x, y, w, h)
        if a is None or a.size == 0:
            return None
        np = self.np
        ring = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]]) if min(a.shape[:2]) > 2 else a.reshape(-1, 3)
        vals, counts, order, keys = self._mode(ring)
        top = vals[order[0]]
        sel = ring.reshape(-1, 3)[keys == top]
        return self._hex(sel.mean(axis=0).round())

    def bg_border(self, x, y, w, h, thin_only: bool = False):
        """(底色, 边框色或 None)。边缘一圈的众数色与内部主色不同、且内部主色占内部面积 ≥50% 时, 边缘那圈是描边
        (带边框的按钮/输入框/分组框): 底色取内部主色; 描边颜色一致(占一圈 ≥60%)才当作边框返回。"""
        ring_hex = self.bg(x, y, w, h)
        a = self._crop(x, y, w, h)
        if ring_hex is None or a is None or min(a.shape[:2]) < 10:
            return ring_hex, None
        np = self.np
        rc = np.array([int(ring_hex[i:i + 2], 16) for i in (1, 3, 5)])
        ring = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]]).astype(np.int32)
        share = float((np.abs(ring - rc).sum(axis=1) <= 24).mean())
        if thin_only:
            # 容器: 只认"细线框"(分组框/面板/编辑区的边线) —— 往里 3 个截图像素的那一圈换了主色, 且边线颜色在那一圈里
            # 几乎不出现; 底色取里面那一圈的主色。顶部色带、侧栏这类成片的异色区域不是描边, 底色照旧按边缘取
            k = 3
            if min(a.shape[:2]) < 6 * k + 4:
                # 矮条容器(表头行/工具条): 边缘一圈大半是上下两条边线, 底色按整块面积的主色取
                vals, counts, order, keys = self._mode(a[::1, ::2].reshape(-1, 3))
                dom = a[::1, ::2].reshape(-1, 3)[keys == vals[order[0]]].mean(axis=0)
                if counts[order[0]] >= 0.5 * len(keys) and np.abs(dom - rc).sum() > 40:
                    return self._hex(dom.round()), None
                return ring_hex, None
            if share < 0.4:
                return ring_hex, None
            r2 = np.concatenate([a[k, k:-k], a[-1 - k, k:-k], a[k:-k, k], a[k:-k, -1 - k]])
            vals, counts, order, keys = self._mode(r2)
            c2 = r2[keys == vals[order[0]]].mean(axis=0)
            if np.abs(c2 - rc).sum() <= 40 or counts[order[0]] < 0.5 * len(r2):
                return ring_hex, None
            if float((np.abs(r2.astype(np.int32) - rc).sum(axis=1) <= 24).mean()) > 0.12:
                return ring_hex, None
            return self._hex(c2.round()), (ring_hex if share >= 0.6 else None)
        m = max(2, int(min(a.shape[:2]) * 0.08))
        inner = a[m:-m, m:-m][::2, ::2].reshape(-1, 3)
        if inner.size == 0:
            return ring_hex, None
        vals, counts, order, keys = self._mode(inner)
        if counts[order[0]] < 0.5 * len(inner):
            return ring_hex, None
        dom = inner[keys == vals[order[0]]].mean(axis=0)
        if np.abs(dom - rc).sum() <= 40:
            return ring_hex, None
        if share < 0.6:
            # 立体边(上/左亮、下/右暗, 两侧平均色明显不同): 一圈颜色不统一, 但整圈都不是内部底色 → 边框取这一圈的平均色
            far = np.abs(ring - dom).sum(axis=1) >= 45
            tl = np.concatenate([a[0], a[:, 0]]).astype(np.int32).mean(axis=0)
            br = np.concatenate([a[-1], a[:, -1]]).astype(np.int32).mean(axis=0)
            bevel = far.mean() >= 0.8 and float(np.abs(tl - br).sum()) >= 60
            return self._hex(dom.round()), (self._hex(ring[far].mean(axis=0).round()) if bevel else None)
        return self._hex(dom.round()), ring_hex

    def fg(self, x, y, w, h, bg_hex):
        """框内与背景色差最大的显著颜色(文字色)。"""
        a = self._crop(x, y, w, h)
        if a is None or a.size == 0:
            return None
        np = self.np
        px = a.reshape(-1, 3).astype(np.int32)
        bgc = self._bg_vec(bg_hex, px)
        dist = np.abs(px - bgc).sum(axis=1)
        if dist.max() < 60:
            return None
        far = px[dist >= max(60, int(dist.max() * 0.7))]
        return self._hex(far.mean(axis=0).round())

    def _bg_vec(self, bg_hex, px):
        np = self.np
        if isinstance(bg_hex, str) and re.match(r"^#[0-9a-fA-F]{6}$", bg_hex):
            return np.array([int(bg_hex[i:i + 2], 16) for i in (1, 3, 5)])
        q = (px >> 3)
        keys = q[:, 0] * 1024 + q[:, 1] * 32 + q[:, 2]
        vals, counts = np.unique(keys, return_counts=True)
        return px[keys == vals[int(np.argmax(counts))]].mean(axis=0)

    def fill_rect(self, x, y, w, h, parent_bg):
        """控件框内的实心填充块(如带阴影内缩的按钮): 返回 (颜色, [x, y, w, h]) 逻辑单位, 无则 None。
        条件: 与父背景不同的主色覆盖 ≥35% 面积, 且其行/列密度 >50% 的范围构成矩形。"""
        a = self._crop(x, y, w, h)
        if a is None or min(a.shape[:2]) < 6:
            return None
        np = self.np
        px = a.astype(np.int32)
        pb = self._bg_vec(parent_bg, px.reshape(-1, 3))
        diff = np.abs(px - pb).sum(axis=2) > 30
        if diff.mean() < 0.35:
            return None
        vals, counts, order, keys = self._mode(px[diff])
        dom = px[diff][keys == vals[order[0]]].mean(axis=0)
        mask = np.abs(px - dom).sum(axis=2) <= 24
        if mask.mean() < 0.35:
            return None
        rows, cols = np.where(mask.mean(axis=1) > 0.5)[0], np.where(mask.mean(axis=0) > 0.5)[0]
        if rows.size == 0 or cols.size == 0:
            return None
        k = self.s
        rect = [x + cols[0] / k, y + rows[0] / k, (cols[-1] - cols[0] + 1) / k, (rows[-1] - rows[0] + 1) / k]
        return self._hex(dom.round()), [round(float(v), 2) for v in rect]

    def hlines(self, x, y, w, h, bg_hex):
        """带内的细横线(分割线; 系统常在列表装饰层绘制, 不在无障碍树里): [(y, 厚, x0, 宽, 色)] 逻辑单位。
        条件: 该行与背景不同的像素覆盖 ≥60% 宽, 连续 ≤3 物理像素, 上下相邻行覆盖 <30%(不是色块边缘)。"""
        a = self._crop(x, y, w, h)
        if a is None or a.shape[0] < 1 or a.shape[1] < 8:
            return []
        np = self.np
        px = a.astype(np.int32)
        bgc = self._bg_vec(bg_hex, px.reshape(-1, 3))
        mask = np.abs(px - bgc).sum(axis=2) > 12
        cov = mask.mean(axis=1)
        out, r, H = [], 0, mask.shape[0]
        k = self.s
        while r < H:
            if cov[r] < 0.6:
                r += 1
                continue
            e = r
            while e + 1 < H and cov[e + 1] >= 0.6:
                e += 1
            thin = (e - r + 1) <= max(3, int(round(k)))
            above = cov[r - 1] if r > 0 else 0.0
            below = cov[e + 1] if e + 1 < H else 0.0
            if thin and above < 0.3 and below < 0.3:
                band = mask[r:e + 1].any(axis=0)
                cols = np.where(band)[0]
                color = px[r:e + 1][mask[r:e + 1]].mean(axis=0)
                out.append((round(float(y + r / k), 2), round(max(1.0, (e - r + 1) / k), 2),
                            round(float(x + cols[0] / k), 2), round(float((cols[-1] - cols[0] + 1) / k), 2),
                            self._hex(color.round())))
            r = e + 1
        return out

    def ink(self, x, y, w, h, bg_hex):
        """文字墨迹度量(逻辑单位): {top, bottom, left, right, lines, line_h, pitch}; 无墨迹返回 None。
        用于从截图反解字号与文本在框内的实际位置(控件框通常比字形松)。"""
        a = self._crop(x, y, w, h)
        if a is None or a.size == 0 or min(a.shape[:2]) < 3:
            return None
        np = self.np
        px = a.astype(np.int32)
        bgc = self._bg_vec(bg_hex, px.reshape(-1, 3))
        dist = np.abs(px - bgc).sum(axis=2)
        mask = dist >= 90
        # 去掉贯穿整框的横/竖线(分割线、边框), 它们不是字形
        mask[mask.mean(axis=1) > 0.85, :] = False
        mask[:, mask.mean(axis=0) > 0.85] = False
        # 框边一圈(2 个截图像素)不算墨迹: 控件框贴着卡片/对话框边缘时, 那里是圆角/阴影/描边的抗锯齿像素,
        # 断断续续不构成"贯穿线", 却把墨迹左/上边界拉到框边(文字内边距被算成 0)
        if min(mask.shape) > 12:
            mask[:2, :] = False
            mask[-2:, :] = False
            mask[:, :2] = False
            mask[:, -2:] = False
        rows, cols = np.where(mask.any(axis=1))[0], np.where(mask.any(axis=0))[0]
        if rows.size == 0 or mask.sum() < 4:
            return None
        # 行内墨迹游程 = 文本行; 游程间空白 < 行高 15% 视为同一行(如 i 的点、中文偏旁)
        runs, start, prev = [], rows[0], rows[0]
        for r in rows[1:]:
            if r - prev > 1:
                runs.append((start, prev))
                start = r
            prev = r
        runs.append((start, prev))
        merged = [list(runs[0])]
        for s, e in runs[1:]:
            if s - merged[-1][1] <= max(2, 0.15 * (merged[-1][1] - merged[-1][0] + 1)):
                merged[-1][1] = e
            else:
                merged.append([s, e])
        # 过滤噪声行(高度 < 最大行 35%)
        hmax = max(e - s + 1 for s, e in merged)
        merged = [m for m in merged if (m[1] - m[0] + 1) >= 0.35 * hmax]
        k = self.s
        line_h = float(np.median([e - s + 1 for s, e in merged])) / k
        pitch = (float(np.median([merged[i][0] - merged[i - 1][0] for i in range(1, len(merged))])) / k
                 if len(merged) > 1 else 0.0)
        # 笔画粗细: 墨迹横向游程的平均长度(像素 → 逻辑单位), 用于区分常规/加粗
        r0, r1 = merged[0][0], merged[-1][1] + 1
        sub = mask[r0:r1]
        if self.core_stroke:
            # 子像素渲染在字形两侧各带一列彩边, 小字号下彩边比笔画本身还宽: 只数色差过半的核心像素
            sub = sub & (dist[r0:r1] >= 0.5 * float(dist[mask].max()))
        sub = sub.astype(np.int8)
        starts = int((np.diff(sub, axis=1, prepend=0) == 1).sum())
        stroke = (float(sub.sum()) / starts / k) if starts else 0.0
        top, bottom = (merged[0][0]) / k, (merged[-1][1] + 1) / k
        # 各行墨迹左缘的离散度: 多行文本左缘对齐(≈0)= 左对齐段落, 参差 = 居中/右对齐
        lefts = []
        for s0, e0 in merged:
            cc = np.where(mask[s0:e0 + 1].any(axis=0))[0]
            if cc.size:
                lefts.append(float(cc[0]) / k)
        spread = (max(lefts) - min(lefts)) if len(lefts) > 1 else 0.0
        return {"top": y + top, "bottom": y + bottom, "left": x + cols[0] / k,
                "right": x + (cols[-1] + 1) / k, "lines": len(merged), "line_h": line_h, "pitch": pitch,
                "stroke": stroke, "left_spread": spread}

    def icon_split(self, x, y, w, h, bg_hex, units: float):
        """带文字的控件框里是否还画着一个图标(工具栏按钮的"图标在上/字在下"、列表项的"图标在左/字在右")。
        返回 (图标框, 文字框)(逻辑单位)或 None。units = 文字按字宽折算的 em 数(判断剩下的墨迹像不像这段字)。
        判据: 墨迹分成两团, 一团近似方形且明显比另一团(文字行)高, 另一团的宽度与这段文字相称。"""
        a = self._crop(x, y, w, h)
        if a is None or min(a.shape[:2]) < 10 or units <= 0:
            return None
        np = self.np
        px = a.astype(np.int32)
        mask = np.abs(px - self._bg_vec(bg_hex, px.reshape(-1, 3))).sum(axis=2) >= 90
        mask[mask.mean(axis=1) > 0.85, :] = False
        mask[:, mask.mean(axis=0) > 0.85] = False
        mask[:2, :] = mask[-2:, :] = False
        mask[:, :2] = mask[:, -2:] = False
        k = self.s

        def runs(flags, min_gap):
            idx = np.where(flags)[0]
            if idx.size == 0:
                return []
            out, start, prev = [], idx[0], idx[0]
            for i in idx[1:]:
                if i - prev > min_gap:
                    out.append((int(start), int(prev) + 1))
                    start = i
                prev = i
            out.append((int(start), int(prev) + 1))
            return out

        def extent(sub, axis):
            idx = np.where(sub.any(axis=axis))[0]
            return (int(idx[0]), int(idx[-1]) + 1) if idx.size else None

        def texty(tw, th):
            # 这团墨迹像不像这段单行文字: 字号 ≈ 墨迹高/0.72, 宽度在估计值的 0.5~1.8 倍内
            est = units * (th / 0.72)
            return th >= 5 * k and 0.5 * est <= tw <= 1.8 * est

        def other_color(m_icon, m_text):
            # 图标与文字不是一种颜色(彩色图标 + 单色字): 两团墨迹的平均色相差明显
            if not m_icon.any() or not m_text.any():
                return False
            return float(np.abs(px[m_icon].mean(axis=0) - px[m_text].mean(axis=0)).sum()) >= 90

        def is_icon(ih, iw, th, m_icon, m_text):
            if not (ih >= 8 * k and 0.55 * ih <= iw <= 1.7 * ih):
                return False
            return (ih >= 10 * k and ih >= 1.25 * th) or other_color(m_icon, m_text)

        rows = runs(mask.any(axis=1), max(1, int(2 * k)))
        if len(rows) >= 2:
            (t0, t1), up = rows[-1], (rows[0][0], rows[-2][1])
            ce, te = extent(mask[up[0]:up[1]], 0), extent(mask[t0:t1], 0)
            if ce and te:
                ih, iw, th, tw = up[1] - up[0], ce[1] - ce[0], t1 - t0, te[1] - te[0]
                mi, mt = mask.copy(), mask.copy()
                mi[up[1]:, :] = False
                mt[:t0, :] = False
                if is_icon(ih, iw, th, mi, mt) and texty(tw, th):
                    cut = (up[1] + t0) / 2.0 / k
                    return [x, y, w, cut], [x, y + cut, w, h - cut]
        cols = runs(mask.any(axis=0), 0)
        if len(cols) >= 2:
            c0, c1 = cols[0]
            re_, rest = extent(mask[:, c0:c1], 1), extent(mask[:, cols[1][0]:], 1)
            if re_ and rest:
                ih, iw, th, tw = re_[1] - re_[0], c1 - c0, rest[1] - rest[0], cols[-1][1] - cols[1][0]
                gap = cols[1][0] - c1
                mi, mt = mask.copy(), mask.copy()
                mi[:, c1:] = False
                mt[:, :cols[1][0]] = False
                if is_icon(ih, iw, th, mi, mt) and gap >= max(2 * k, 0.15 * ih) and texty(tw, th):
                    cut = (c1 + cols[1][0]) / 2.0 / k
                    return [x, y, cut, h], [x + cut, y, w - cut, h]
        return None

    def frame_line(self, x, y, w, h, bg_hex):
        """窗口最外一圈像素是不是一条边线(与窗口底色明显不同的像素占一圈 ≥70%): 返回边线的平均色, 否则 None。
        桌面窗口的 1px 边线常是半透明的(混着后面的桌面), 颜色不均匀, 但整圈都比底色深/浅。"""
        a = self._crop(x, y, w, h)
        if a is None or min(a.shape[:2]) < 40 or not isinstance(bg_hex, str) or not bg_hex.startswith("#"):
            return None
        np = self.np
        ring = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]]).astype(np.int32)
        inner = np.concatenate([a[2], a[-3], a[:, 2], a[:, -3]]).astype(np.int32)
        bgc = np.array([int(bg_hex[i:i + 2], 16) for i in (1, 3, 5)])
        far = np.abs(ring - bgc).sum(axis=1) >= 45
        if far.mean() < 0.7 or (np.abs(inner - bgc).sum(axis=1) >= 45).mean() > 0.4:
            return None
        return self._hex(ring[far].mean(axis=0).round())

    def dominant(self, x, y, w, h):
        """框内整块面积的众数色。顶层窗口用它定底色: 窗口矩形常含不可见的缩放边/阴影(边缘一圈是桌面像素),
        按"边缘一圈"取色会把桌面色当成窗口背景。"""
        a = self._crop(x, y, w, h)
        if a is None or a.size == 0:
            return None
        np = self.np
        if min(a.shape[:2]) > 2:
            # 边缘一圈颜色一致(占 ≥50%)时它就是窗口自己的边/遮罩(对话框四周的半透明遮罩), 照旧用边缘色
            ring = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
            rv, rc, ro, rk = self._mode(ring)
            if rc[ro[0]] >= 0.5 * len(ring):
                return self._hex(ring.reshape(-1, 3)[rk == rv[ro[0]]].mean(axis=0).round())
        px = a[::2, ::2].reshape(-1, 3)
        vals, counts, order, keys = self._mode(px)
        return self._hex(px[keys == vals[order[0]]].mean(axis=0).round())

    def detail(self, x, y, w, h, bg_hex=None) -> float:
        """框内与背景色明显不同的像素占比(0~1): 判断一个没有文字/子节点的叶子框里是否画了东西。"""
        a = self._crop(x, y, w, h)
        if a is None or a.size == 0 or min(a.shape[:2]) < 3:
            return 0.0
        np = self.np
        px = a.reshape(-1, 3).astype(np.int32)
        bgc = self._bg_vec(bg_hex, px)
        return float((np.abs(px - bgc).sum(axis=1) >= 60).mean())

    def embed(self, x, y, w, h):
        a = self._crop(x, y, w, h)
        if a is None or a.size == 0:
            return None
        from PIL import Image
        buf = io.BytesIO()
        Image.fromarray(a).save(buf, format="PNG", optimize=False)
        data = buf.getvalue()
        if len(data) > 300_000 or len(data) > self.budget:
            return None
        self.budget -= len(data)
        return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _label(n: dict) -> str:
    """可访问名(含屏幕上不可见的描述), 用于判定节点是否有语义。"""
    for k in ("name", "value", "desc"):
        v = (n.get(k) or "").strip()
        if v:
            return v
    return ""


def _visible_text(n: dict) -> str:
    """屏幕上实际显示的文字: name/value。desc(content-desc/AXDescription/HelpText)只是读屏文案, 不渲染。"""
    for k in ("name", "value"):
        v = (n.get(k) or "").strip()
        if v:
            return v
    return ""


def _intersect(b, c):
    x1, y1 = max(b[0], c[0]), max(b[1], c[1])
    x2, y2 = min(b[0] + b[2], c[0] + c[2]), min(b[1] + b[3], c[1] + c[3])
    return [x1, y1, max(0.0, x2 - x1), max(0.0, y2 - y1)]


def _same_box(a, b) -> bool:
    return all(abs(a[i] - b[i]) <= _TOL for i in range(4))


def _prepare(tree: dict) -> dict:
    """逻辑坐标 + 裁剪 + 去不可见 + 折叠冗余包装。返回新树(不改入参)。"""
    platform = tree.get("platform", "")
    scale = float(tree.get("scale") or 1.0)
    win = [v / scale for v in (tree.get("window") or tree["root"]["bounds"])]

    def rec(n, clip, depth):
        if n.get("role") in _SKIP_RAW:
            return None
        b = [float(v) / scale for v in (n.get("bounds") or [0, 0, 0, 0])]
        vis = _intersect(b, clip)
        if vis[2] < 1 or vis[3] < 1:
            return None
        kids = [k for k in (rec(c, vis, depth + 1) for c in (n.get("children") or [])) if k]
        out = {k: v for k, v in n.items() if k != "children"}
        out["bounds"] = [round(v, 2) for v in vis]
        out["uir_role"] = normalize_role(platform, n.get("role", ""), n)
        out["children"] = kids
        # 折叠: 无名、不可交互、普通容器, 唯一子节点与自身同框 → 用子节点替代
        if (len(kids) == 1 and out["uir_role"] == "box" and not _label(out) and not out.get("clickable")
                and _same_box(out["bounds"], kids[0]["bounds"])):
            return kids[0]
        return out

    root = rec(tree["root"], win, 0)
    if root is None:
        raise ValueError("可访问性树在窗口内无可见节点")
    return {"platform": platform, "app": tree.get("app", ""), "window": win, "root": root}


def _mark_graphic_leaves(root: dict, win=None) -> int:
    """给"独占一块屏幕区域的无文字叶子"打 _excl 标记(自绘视图/系统栏/图形控件的候选)。

    独占 = 没有任何其他叶子节点与它的框重叠超过自身面积的 5%。透明触摸层、拖放目标、遮罩这类叶子
    盖在别的内容上面, 不独占 —— 它们框内的像素是别的节点画的, 不能当成它自己的图形。"""
    nodes = []

    def walk(n, path):
        nodes.append((n, path))
        for i, c in enumerate(n.get("children") or []):
            walk(c, path + (i,))

    walk(root, ())
    marked = 0
    for n, path in nodes:
        if n.get("children") or _visible_text(n):
            continue
        b = n["bounds"]
        area = b[2] * b[3]
        if area < 256:
            continue
        excl = True
        for m, mp in nodes:
            if m is n or m.get("children"):     # 只与其他叶子比: 容器本身不画内容(它的背景由采色还原)
                continue
            it = _intersect(b, m["bounds"])
            if it[2] * it[3] > 0.05 * area:
                excl = False
                break
        if excl:
            n["_excl"] = True
            marked += 1
    return marked


def _text_units(text: str) -> float:
    cjk = sum(1 for ch in text if ord(ch) > 0x2E80)
    return cjk + (len(text) - cjk) * 0.55


_TEXTY_ROLES = ("text", "button", "link", "list_item", "box")


def _refine_leaves(root: dict, sampler: "_Sampler") -> None:
    """建树前的两步叶子修整(纯几何 + 截图证据, 不看应用):
    1. 同级里框住了后面兄弟的文字叶子(表格行的首列文本框常报整行宽), 宽度收到最近的那个兄弟左缘 ——
       否则量墨迹时把别的列的字也算进来, 版式也被判成叠放;
    2. 文字控件框里另画着图标的(见 _Sampler.icon_split), 拆成 图标 + 文字 两个子节点。"""
    def walk(n):
        kids = n.get("children") or []
        for a in kids:
            if a.get("children") or not _visible_text(a):
                continue
            ax, ay, aw, ah = a["bounds"]
            lim = None
            for b in kids:
                if b is a:
                    continue
                bx, by, bw, bh = b["bounds"]
                if (bx >= ax + 8 and bx + bw <= ax + aw + _TOL and by >= ay - _TOL and by + bh <= ay + ah + _TOL):
                    lim = bx if lim is None else min(lim, bx)
            if lim is not None:
                a["bounds"] = [ax, ay, round(lim - ax, 2), ah]
        for i, a in enumerate(kids):
            text = _visible_text(a)
            if (sampler is None or a.get("children") or not text or "\n" in text or a.get("editable")
                    or a.get("uir_role") not in _TEXTY_ROLES or a.get("checked") is not None
                    or re.sub(r"Control$", "", str(a.get("role") or "")).rsplit(".", 1)[-1] in _TOGGLE_RAW):
                continue
            x, y, w, h = a["bounds"]
            sp = sampler.icon_split(x, y, w, h, sampler.bg(x, y, w, h), _text_units(text))
            if not sp:
                continue
            icon = {"role": "", "uir_role": "image", "name": "", "value": "", "desc": "", "bounds": [round(v, 2) for v in sp[0]],
                    "children": [], "_excl": True}
            label = dict(a, bounds=[round(v, 2) for v in sp[1]], children=[], id="", clickable=False, uir_role="text")
            wrap = dict(a, name="", value="", children=[icon, label])
            if wrap.get("uir_role") == "text":
                wrap["uir_role"] = "box"
            kids[i] = wrap
        for c in n.get("children") or []:
            if c.get("children") and not c.get("_split"):
                walk(c)

    walk(root)


def _add_window_title(root: dict, platform: str) -> None:
    """窗口标题: 桌面平台的标题文字只在窗口节点的名字里, 标题栏子树里没有对应的文本节点 —— 在标题栏空着的
    那一段(左侧系统菜单与右侧窗口按钮之间)补一个文本叶子。截图上那里没有字时, 建树阶段会按"无墨迹"丢掉。"""
    title = (root.get("name") or "").strip()
    if platform not in ("windows", "macos") or not title:
        return
    for tb in root.get("children") or []:
        raw = re.sub(r"Control$", "", str(tb.get("role") or ""))
        if raw != "TitleBar":
            continue
        def has_text(n):
            return any(_visible_text(c) and c.get("uir_role") == "text" or has_text(c) for c in n.get("children") or [])
        if has_text(tb):
            return
        x, y, w, h = tb["bounds"]
        mid = x + w / 2
        left = max([c["bounds"][0] + c["bounds"][2] for c in tb["children"] if c["bounds"][0] + c["bounds"][2] <= mid] + [x])
        right = min([c["bounds"][0] for c in tb["children"] if c["bounds"][0] >= mid] + [x + w])
        if right - left >= 40:
            tb["children"].append({"role": "Text", "uir_role": "text", "name": title, "value": "", "desc": "", "id": "",
                                   "bounds": [round(left, 2), y, round(right - left, 2), h], "children": []})
        return


def _infer_axis(kids: list) -> str:
    """'col' / 'row' / 'stack'。纵向两两不重叠优先(界面主流是竖排)。"""
    if len(kids) <= 1:
        return "col"
    ys = sorted(kids, key=lambda k: (k["bounds"][1], k["bounds"][0]))
    if all(ys[i]["bounds"][1] >= ys[i - 1]["bounds"][1] + ys[i - 1]["bounds"][3] - _TOL for i in range(1, len(ys))):
        return "col"
    xs = sorted(kids, key=lambda k: (k["bounds"][0], k["bounds"][1]))
    if all(xs[i]["bounds"][0] >= xs[i - 1]["bounds"][0] + xs[i - 1]["bounds"][2] - _TOL for i in range(1, len(xs))):
        return "row"
    return "stack"


# 控件框高 → 字号的平台系数(TextView 含 includeFontPadding; AX/UIA 文本框约 1.2 行高)
_BOX_FONT_RATIO = {"android": 0.73, "macos": 0.8, "windows": 0.75}


def _font_size(text: str, w: float, h: float, ratio: float = 0.73) -> int:
    """无截图时的字号估计: 单行按框高 × 平台系数, 多行按「面积 / 字符数」反解; 松框(高 > 2 行)封顶。"""
    if not text:
        return 14
    cjk = sum(1 for ch in text if ord(ch) > 0x2E80)
    units = cjk + (len(text) - cjk) * 0.55            # 以字号为单位的单行宽度
    single = h * ratio
    if units * min(single, 20) > w * 1.05 and h >= 34:  # 多行: lines * s * 1.3 ≈ h, lines ≈ units*s/w
        s = min(math.sqrt(max(1.0, w * h) / (max(1.0, units) * 1.3)), single)
    else:
        s = single if h < 34 else min(single, 20)
    return int(max(10, min(32, round(s))))


def _asc_desc(text: str):
    """一行字形墨迹的升部/降部(以字号为单位, 纯排版规则): 中日韩 ≈(0.80, 0.08);
    拉丁 = 升部(大写/数字/bdfhklt ≈0.72, 否则 x 高 ≈0.53) + 降部(gjpqy 等 ≈0.21)。"""
    if any(ord(ch) > 0x2E80 for ch in text):
        return 0.80, 0.08
    asc = 0.72 if re.search(r"[A-Z0-9bdfhklt\"'()/|\[\]{}!?&#%$@]", text) else 0.53
    desc = 0.21 if re.search(r"[gjpqy(),;\[\]{}|@]", text) else 0.0
    return asc, desc


def _ink_em(text: str) -> float:
    asc, desc = _asc_desc(text)
    return asc + desc


def _font_from_ink(ink: dict, box_size: int, text: str = "", tight: bool = True, hi: float = 1.3) -> int:
    """墨迹反解字号; 紧框(框高 < 34)单行且框与墨迹一致时信框(控件框由字号决定, 更稳)。"""
    if ink["lines"] > 1 and ink["pitch"]:
        s_ink = ink["pitch"] / 1.3
    else:
        s_ink = ink["line_h"] / _ink_em(text)
    if tight and ink["lines"] == 1 and s_ink * 0.8 <= box_size <= s_ink * hi:
        return box_size
    return int(max(9, min(48, round(s_ink))))


class _Builder:
    def __init__(self, sampler: "_Sampler" = None, platform: str = ""):
        self.sampler = sampler
        self.ratio = _BOX_FONT_RATIO.get(platform, 0.75)
        # 横向笔画游程/字号 的加粗判定线: Windows 的 ClearType 彩边不计入笔画(_Sampler.core_stroke), 常规体约 0.10~0.12em
        # 桌面控件的文本框比字形松(24 高的标签里是 12 号字), 框高与墨迹相差 15% 以上就信墨迹
        self.tight_hi = 1.0 if platform == "windows" else (1.15 if platform == "macos" else 1.3)
        self.bold_k = 0.17 if platform == "windows" else 0.175
        # Windows 的同级子窗口按 Z 序枚举(最上面的排最前), 与绘制顺序相反; 其余平台树序 ≈ 绘制顺序
        self.z_top_first = platform == "windows"
        font = {"android": {"family": "Roboto", "fallback": "'Noto Sans SC','PingFang SC',system-ui,sans-serif"},
                "macos": {"family": "-apple-system", "fallback": "'SF Pro Text','PingFang SC',system-ui,sans-serif"},
                "windows": {"family": "Segoe UI", "fallback": "Tahoma,Arial,'Microsoft YaHei',system-ui,sans-serif",
                            "metric_local": ["Arial", "Liberation Sans", "Helvetica"]}}
        self.tokens = Tokens(
            color={"c0": "#ffffff", "c1": "#1f2937", "c2": "#2563eb", "c3": "#6b7280"},
            text_style={"t0": {"size": 14, "weight": 400, "line": 1.35, "color": "c1"}},
            space={"s0": 0, "s1": 4, "s2": 8, "s3": 12, "s4": 16, "s5": 24},
            font={"f0": font.get(platform, {"family": "system-ui", "fallback": "sans-serif"})})
        self._ts_keys = {}
        self.stats = {"nodes_in": 0, "stack": 0, "row": 0, "col": 0, "embedded": 0, "dividers": 0}

    def text_style(self, size: int, color: str, h: float, weight: int = 400, align: str = "", line=None) -> str:
        if line is None:
            line = round(h / size, 3) if size and h and h < size * 1.9 else 1.3
        key = (size, color or "c1", line, weight, align)
        if key not in self._ts_keys:
            name = f"ta{len(self._ts_keys)}"
            st = {"size": size, "weight": weight, "line": line, "color": color or "c1"}
            if align:
                st["align"] = align
            self.tokens.text_style[name] = st
            self._ts_keys[key] = name
        return self._ts_keys[key]

    def build(self, n: dict, parent_bg: str, opaque: bool = False) -> Node:
        x, y, w, h = n["bounds"]
        role = n["uir_role"]
        kids = n["children"]
        if (self.sampler and not kids and not n.get("_inner") and _visible_text(n)
                and role in ("text", "button", "link", "badge", "box", "list_item")):
            fill = self.sampler.fill_rect(x, y, w, h, parent_bg)
            # 实心块窄得放不下这段文字(最小可读字号下的宽度)= 它是图标的色块, 不是按钮的底
            if fill and not _same_box(fill[1], n["bounds"]) and fill[1][2] >= _text_units(_visible_text(n)) * 8:
                # 实心块比控件框小(Material 按钮的阴影内缩等): 外层透明框占位, 内层按实心块几何重建
                inner = dict(n, bounds=fill[1], _inner=True)
                return self.build({"role": "", "uir_role": "box", "bounds": n["bounds"], "children": [inner],
                                   "_inner": True}, parent_bg, opaque)
        self.stats["nodes_in"] += 1
        framed = False
        label = _label(n)
        box = {"w": round(w, 1), "h": round(h, 1)}
        content, intents, layout, text_ref = {}, [], {}, ""
        bg = None
        if self.sampler and role not in ("text", "image", "icon", "divider"):
            border = None
            if n.get("_window"):
                bg = self.sampler.dominant(x, y, w, h)
                if n.get("_frame") and bg:
                    border = self.sampler.frame_line(x, y, w, h, bg)
            else:
                bg, border = self.sampler.bg_border(x, y, w, h, thin_only=bool(kids))
                framed = bg is not None and bg != self.sampler.bg(x, y, w, h)
            if bg and (bg != parent_bg or opaque):
                box["bg"] = bg
            if border:
                box["bg"] = bg
                box["border"] = {"w": 1, "color": border}
        eff_bg = box.get("bg") or parent_bg

        if role in ("button", "link") and kids:
            # 带子节点的可点击容器: 渲染为容器 + 点击意图(按钮内嵌块级子节点不可靠)
            role = "list_item" if n.get("role") in ("AXRow", "AXCell", "ListItem") else "box"
            intents = [{"on": "click", "do": "navigate"}]
        text = re.sub(r"\r\n?", "\n", _visible_text(n))
        if role in ("box", "list_item") and not kids and text:
            role = "button" if n.get("clickable") else "text"
        if role in ("text", "button", "link", "badge") and not kids and not text:
            # 只有读屏描述、没有可见文字的可点击/文本控件 = 图标(搜索、更多、关闭…)
            if role in ("button", "link") or n.get("clickable"):
                intents = [{"on": "click", "do": "navigate"}]
            role = "image"
        raw_short = re.sub(r"Control$", "", str(n.get("role") or "")).rsplit(".", 1)[-1]
        toggle_like = n.get("checked") is not None or raw_short in _TOGGLE_RAW
        if self.sampler and not kids and (
                (role == "input" and not n.get("editable")) or (toggle_like and role == "button")
                or (role == "badge" and raw_short in _GAUGE_RAW)):
            # 滑块/进度/开关/复选框是图形控件: 有截图时按图裁剪重建外观, 意图保留
            intents = [{"on": "click", "do": "toggle"}] if toggle_like else [{"on": "change", "do": "input"}]
            role = "image"
        if (role in ("box", "list_item") and not kids and not text and n.get("_excl") and self.sampler
                and self.sampler.budget > 0 and w >= 16 and h >= 16
                and self.sampler.detail(x, y, w, h, eff_bg) >= 0.01):
            # 没有文字、没有子节点、独占一块区域, 截图上却画了东西: 自绘视图(日历网格/图表/画布)、系统栏、
            # 无障碍树没展开的图形控件 —— 树里没有可重建的结构, 有截图时与图片同样按图裁剪内嵌
            if n.get("clickable"):
                intents = [{"on": "click", "do": "navigate"}]
            role = "image"
            box.pop("bg", None)
        elif (role in ("box", "list_item") and not kids and not text and not n.get("_excl") and self.sampler
                and box.get("bg") and w >= 16 and h >= 16 and self.sampler.detail(x, y, w, h, box["bg"]) >= 0.01):
            # 与别的叶子重叠的空叶子(拖放目标/触摸层/占位层), 截图上这块区域里能看到别的内容 → 它是透明的,
            # 不上底色(否则按树序叠在上面, 把下面的标签页图标/按钮盖成一块纯色)
            box.pop("bg", None)
        ink = None
        if role in ("text", "button", "link", "badge") and self.sampler:
            # 带边框的控件(按钮的立体边、默认按钮的粗边与虚线焦点框)在框内缩一圈再量墨迹, 边框线不当字形
            gi = min(5.0, h * 0.2, w * 0.2) if (framed and not kids and h >= 16) else 0.0
            ink = self.sampler.ink(x + gi, y + gi, w - 2 * gi, h - 2 * gi, eff_bg)
            if not ink:
                # 截图上该框内没有任何字形像素: 文字被遮挡/裁掉(折叠面板、屏外抽屉), 不渲染文字
                text, role = "", "box"
                intents = [{"on": "click", "do": "navigate"}] if n.get("clickable") else []
            elif (role in ("button", "link") and not kids and ink["lines"] == 1 and len(text) >= 3
                  and self.sampler.budget > 0):
                # 可访问名没有画在屏幕上: 墨迹宽度远小于这段文字在任何可读字号下的宽度(窗口的最小化/关闭按钮、
                # 工具栏图标按钮, 名字只给读屏用) → 是图标, 按图裁剪
                cjk = sum(1 for ch in text if ord(ch) > 0x2E80)
                units = cjk + (len(text) - cjk) * 0.55
                s_ink = max(9.0, ink["line_h"] / _ink_em(text))
                if (ink["right"] - ink["left"]) < 0.62 * units * s_ink:
                    intents = [{"on": "click", "do": "navigate"}]
                    role, ink = "image", None
        if role in ("text", "button", "link", "badge"):
            fg = self.sampler.fg(x, y, w, h, eff_bg) if self.sampler else None
            size = _font_size(text, w, h, self.ratio)
            align, line, pad, weight = ("center" if role == "button" else ""), None, None, 400
            if ink:
                symbolic = not re.search(r"[0-9A-Za-z\u2e80-\u9fff\uac00-\ud7af]", text)
                if not symbolic:
                    size = _font_from_ink(ink, size, text, tight=h < 34, hi=self.tight_hi)
                # 横向墨迹游程 / 字号: 常规体实测 0.13~0.17em, 中粗/粗体 ≥0.18em(含抗锯齿); 字号过小时被抗锯齿淹没, 不判
                if size >= 12 and ink["stroke"] >= self.bold_k * size and not symbolic:
                    weight = 600
                line = 1.3 if ink["lines"] > 1 else 1.25
                if "\n" in text and ink["lines"] > 1 and ink["pitch"] and 1.05 <= ink["pitch"] / size <= 2.2:
                    line = round(ink["pitch"] / size, 3)     # 硬换行的多行文本(代码/日志): 行距照实测行间距
                lh = size * line
                n_lines = max(1, ink["lines"])
                if n_lines == 1:
                    # 墨迹中线 = 行框中线 + 字号 × (0.36 - (升部 - 降部)/2): 按字符升/降部修正(大写/数字墨迹居中于行框)
                    asc, desc = _asc_desc(text)
                    top = (ink["top"] + ink["bottom"]) / 2 - lh / 2 - size * (0.36 + (desc - asc) / 2) - y
                else:
                    top = ink["top"] - y - (lh - ink["line_h"]) / 2
                lgap, rgap = ink["left"] - x, x + w - ink["right"]
                right = 0.0
                if n_lines > 1 and lgap > 4 and ink.get("left_spread", 9.0) <= 2.0:
                    # 多行且各行左缘对齐 = 左对齐段落(左右留白相近只是第二行恰好排满): 左内边距照墨迹, 右内边距取
                    # 不超过左边的那部分留白 —— 折行宽度与原文一致, 不会因为占满整框而少折一行
                    align, left = "", max(0.0, lgap - 0.5)
                    right = max(0.0, min(rgap, lgap) - 0.5)
                elif lgap > 4 and abs(lgap - rgap) <= max(3.0, 0.08 * w):
                    align, left = "center", 0.0
                else:
                    align, left = ("left" if role == "button" else ""), max(0.0, lgap - 0.5)
                top = max(0.0, top)
                # 下内边距补齐到框高: 行框恰好占满内容区, 按钮等默认垂直居中的元素不再二次居中
                bottom = max(0.0, h - top - lh * n_lines)
                pad = [round(float(top), 1), round(float(right), 1), round(float(bottom), 1), round(float(left), 1)]
                if symbolic:
                    box["_symbolic"] = True
            text_ref = self.text_style(size, fg or "c1", h, weight=weight, align=align, line=line)
            content = {"text": text}
            if pad and any(pad):
                box["pad"] = pad
            if role == "button":
                chk = n.get("checked")
                intents = [{"on": "click", "do": "toggle" if chk is not None else
                            ("submit-form" if re.search(r"登录|提交|确定|保存|发送|submit|login|sign|save|ok\b", text, re.I)
                             else "navigate")}]
                if chk is not None:
                    content["checked"] = bool(chk)  # type: ignore[assignment]
            elif role == "link":
                intents = [{"on": "click", "do": "navigate"}]
            if (ink and ink["lines"] == 1) or (not ink and h < size * 1.9):
                box["overflow"] = "hidden"
                if align != "center":   # 左对齐("" 或按钮的显式 "left")
                    # 回落字体比原平台字体宽时不截断: 单行文本框右侧放宽 15%, 由负右外边距抵消, 不推挤兄弟节点
                    extra = round(min(w * 0.15 + 4, 80), 1)
                    box["w"] = round(w + extra, 1)
                    box["_slack"] = extra
        elif role == "input":
            fg = self.sampler.fg(x, y, w, h, eff_bg) if self.sampler else None
            content = {"placeholder": (n.get("desc") or "") if not n.get("value") else "",
                       "text": n.get("value") or n.get("name") or "",
                       "type": "password" if n.get("password") else "text"}
            isize = _font_size(content["text"] or content["placeholder"] or "字", w, min(h, 30), self.ratio)
            iink = None
            if self.sampler and content["text"]:
                # 量墨迹的范围: 组合框取里面的编辑区子节点(不含下拉按钮), 否则内缩 3 个单位避开边框线
                ek = next((k for k in kids if k.get("uir_role") == "input"), None)
                ix, iy, iw, ih = ek["bounds"] if ek else ((x + 3, y + 3, w - 6, h - 6) if min(w, h) >= 14 else (x, y, w, h))
                iink = self.sampler.ink(ix, iy, iw, ih, eff_bg)
            content["text"] = re.sub(r"\r\n?", "\n", content["text"])
            multi = "\n" in content["text"].strip() and h >= 3 * isize
            box["pad"] = [0, 8, 0, 8]
            iline = 0.0
            if iink and iink["lines"] == 1:
                isize = _font_from_ink(iink, isize, content["text"], tight=False)
                box["pad"] = [0, 4, 0, round(max(0.0, min(16.0, iink["left"] - x - 1.5)), 1)]
            elif iink and multi and iink["lines"] > 1 and iink["pitch"] > 0:
                # 多行编辑区: 字号按行墨迹高、行距按行间距, 内边距照首行墨迹位置(文本从左上角起排, 不垂直居中)
                isize = max(9, min(40, int(round(iink["pitch"] / 1.25))))
                iline = round(iink["pitch"] / isize, 3)
                lh = isize * (iline or 1.3)
                box["pad"] = [round(max(0.0, iink["top"] - y - (lh - iink["line_h"]) / 2), 1), 4, 0,
                              round(max(0.0, iink["left"] - x - 0.5), 1)]
            if multi:
                content["multiline"] = True  # type: ignore[assignment]
            text_ref = self.text_style(isize, fg or "c1", 0, line=iline or None)
            intents = [{"on": "input", "do": "input"}]
        elif role == "image":
            content = {"alt": label or "图片"}
            box.setdefault("pad", [0])
            if self.sampler:
                src = self.sampler.embed(x, y, w, h)
                if src:
                    content["src"] = src
                    self.stats["embedded"] += 1
        if role in ("list", "list_item", "nav", "header", "footer", "box", "card", "overlay", "table", "form"):
            box.setdefault("pad", [0])            # 抵消 ul 等标签的默认内边距, 位置完全由 margin 承载
            if role == "list_item" and not kids:
                role = "box"
        node = Node(role=role, layout=layout, box=box, content=content, intents=intents, text_ref=text_ref)
        if n.get("id"):
            node.content.setdefault("ref", n["id"])

        if kids and role not in ("text", "image", "input", "divider", "badge"):
            axis = _infer_axis(kids)
            self.stats[axis] += 1
            if box.get("border"):
                # 带边线的容器: 子节点从边线内侧起排(边线占 1 个单位), 原点内移, 子节点位置不随边线偏移
                x, y = x + 1, y + 1
            if axis == "stack":
                node.layout = {"mode": "stack"}
                # 叠放保持树序(≈ 绘制顺序, 后者在上); 有内容的叠放子树背景不透明(抽屉/弹层), 否则下层内容透出;
                # 无内容的叶子框(拖放目标、触摸层)多为透明, 只在颜色确实不同于父级时才上色
                for k in (list(reversed(kids)) if self.z_top_first else kids):
                    c = self.build(k, eff_bg, opaque=bool(k["children"]))
                    c.box.pop("_slack", None)
                    c.box["position"] = {"top": round(k["bounds"][1] - y, 1), "left": round(k["bounds"][0] - x, 1)}
                    node.children.append(c)
            else:
                node.layout = {"mode": "flex", "direction": axis, "align": "start"}
                main = 1 if axis == "col" else 0
                cross = 0 if axis == "col" else 1
                order = sorted(kids, key=lambda k: (k["bounds"][main], k["bounds"][cross]))
                if axis == "col" and self.sampler and len(order) > 1:
                    order = self._with_dividers(order, x, y, w, h, eff_bg)
                cursor = (y if axis == "col" else x)
                for k in order:
                    c = k["_node"] if "_node" in k else self.build(k, eff_bg)
                    kb = k["bounds"]
                    lead = max(-_TOL, round(kb[main] - cursor, 1))
                    off = max(0.0, round(kb[cross] - (x if axis == "col" else y), 1))
                    slack = -c.box.pop("_slack", 0) or 0
                    c.box["margin"] = [lead, slack, 0, off] if axis == "col" else [off, slack, 0, lead]
                    cursor = max(cursor, kb[main] + kb[main + 2])
                    node.children.append(c)
            self._fix_symbolic(node)
            if role in ("button", "link"):
                node.content.pop("text", None)
        return node

    def _with_dividers(self, order: list, x, y, w, h, bg) -> list:
        """在相邻子节点交界带(±2)与首尾带里找分割线, 作为细色条节点插入竖排流。"""
        bands, prev_bottom = [], y
        for k in order:
            kb = k["bounds"]
            bands.append((prev_bottom - 2, kb[1] + 2))
            prev_bottom = kb[1] + kb[3]
        bands.append((prev_bottom - 2, y + h))
        seen, out = set(), list(order)
        for b0, b1 in bands:
            b0, b1 = max(y, b0), min(y + h, b1)
            if b1 - b0 < 1:
                continue
            for ly, th, lx, lw, col in self.sampler.hlines(x, b0, w, b1 - b0, bg):
                key = round(ly)
                if key in seen or any(abs(key - s2) <= 1 for s2 in seen):
                    continue
                seen.add(key)
                self.stats["dividers"] = self.stats.get("dividers", 0) + 1
                dn = Node(role="divider", box={"w": lw, "h": th, "bg": col, "pad": [0]})
                out.append({"bounds": [lx, ly, lw, th], "children": [], "_node": dn})
        return sorted(out, key=lambda k: (k["bounds"][1], k["bounds"][0]))

    def _fix_symbolic(self, node: Node):
        """纯符号文本(÷ − = · 等)的墨迹高度与字号无稳定比例: 取同级文字的字号中位数。"""
        sym = [c for c in node.children if c.box.pop("_symbolic", False)]
        sizes = sorted(self.tokens.text_style[c.text_ref]["size"] for c in node.children
                       if c.text_ref and c not in sym)
        if not sym or not sizes:
            return
        med = sizes[len(sizes) // 2]
        for c in sym:
            st = dict(self.tokens.text_style[c.text_ref])
            old_lh = st["size"] * st["line"]
            pad = c.box.get("pad") or [0, 0, 0, 0]
            centre = pad[0] + old_lh / 2
            new_lh = med * st["line"]
            top = max(0.0, centre - new_lh / 2)
            c.box["pad"] = [round(top, 1), 0, round(max(0.0, float(c.box["h"]) - top - new_lh), 1), pad[3]]
            c.text_ref = self.text_style(med, st["color"], 0, weight=st["weight"], align=st.get("align", ""),
                                         line=st["line"])


def tree_to_uir(tree: dict, screenshot=None, embed_images: bool = True) -> UIR:
    """统一树 JSON(+ 可选参考截图)→ UIR。确定性: 同输入逐字节同输出。"""
    if tree.get("platform") not in PLATFORMS:
        raise ValueError(f"未知平台: {tree.get('platform')!r}; 可用: {PLATFORMS}")
    prep = _prepare(tree)
    sampler = None
    shot = screenshot or tree.get("screenshot")
    if shot and Path(shot).exists():
        try:
            # 截图像素 / 逻辑单位: Android/Windows 截图是物理像素(= scale), macOS screencapture 带 Retina 倍率
            px = float(tree.get("screenshot_scale") or tree.get("scale") or 1.0)
            wx, wy = prep["window"][0], prep["window"][1]
            origin = (wx, wy) if tree.get("platform") == "macos" else (0, 0)
            sampler = _Sampler(shot, px, origin=origin, embed_budget=4_000_000 if embed_images else 0)
        except Exception:
            sampler = None
    _add_window_title(prep["root"], prep["platform"])
    if sampler is not None:
        sampler.core_stroke = prep["platform"] == "windows"
        _refine_leaves(prep["root"], sampler)
        _mark_graphic_leaves(prep["root"])
    b = _Builder(sampler, prep["platform"])
    wx, wy, ww, wh = prep["window"]
    root_bg = sampler.dominant(wx, wy, ww, wh) if sampler else "c0"
    rootn = prep["root"]
    cur = rootn
    while cur is not None:                       # 与窗口同框的最外层包装都按整块面积取底色
        if not _same_box(cur["bounds"], rootn["bounds"]):
            break
        cur["_window"] = True
        cur["_frame"] = cur is rootn
        cur = cur["children"][0] if len(cur["children"]) == 1 else None
    node = b.build(rootn, root_bg or "c0")
    node.box.pop("_slack", None)
    for nd in node.walk():                      # 无同级可参照的符号文本保留墨迹估计
        nd.box.pop("_symbolic", None)
    node.box["bg"] = node.box.get("bg") or root_bg or "c0"
    # 根节点相对窗口原点
    rx, ry = rootn["bounds"][0] - wx, rootn["bounds"][1] - wy
    if rx or ry:
        node.box["margin"] = [round(ry, 1), 0, 0, round(rx, 1)]
    vw, vh = int(round(ww)), int(round(wh))
    report = {"platform": prep["platform"], "nodes_in": _count(tree["root"]), "nodes_kept": b.stats["nodes_in"],
              "layout": {k: b.stats[k] for k in ("col", "row", "stack")}, "images_embedded": b.stats["embedded"], "dividers": b.stats["dividers"],
              "sampled_colors": sampler is not None, "scale": tree.get("scale", 1.0)}
    meta = {"source_kind": "a11y", "title": prep.get("app", ""), "lang": "zh",
            "viewport": {"w": vw, "h": vh}, "report": report}
    if prep["platform"] in ("android", "ios") and int(tree.get("window", [0, 0])[1]) == 0:
        # 窗口从屏幕顶边开始(含状态栏区域): 采到的像素与坐标都是全屏的, 原生目标应全面屏绘制(不再让出状态栏)
        meta["edge_to_edge"] = True
    return UIR(root=node, tokens=b.tokens, meta=meta).normalize()


def _count(n: dict) -> int:
    return 1 + sum(_count(c) for c in (n.get("children") or []))


def load_tree(path) -> dict:
    """树 JSON / uiautomator XML 文件 → 统一树 JSON(按内容嗅探格式)。"""
    p = Path(path)
    data = p.read_bytes()
    head = data.lstrip()[:200]
    if head.startswith(b"<"):
        return parse_uiautomator_xml(data)
    obj = json.loads(data.decode("utf-8"))
    if "root" in obj and "platform" in obj:
        return obj
    if "ControlType" in obj or "BoundingRectangle" in obj:
        return parse_uia(obj)
    raise ValueError("无法识别的树格式(需统一树 JSON / uiautomator XML / UIA 元素 JSON)")


# ---------------------------------------------------------------------------
@register_input("a11y")
class A11yAdapter:
    """GenRequest:
      - source_path: 已导出的树(统一树 JSON / uiautomator XML / UIA JSON), image_path 可给参考截图; 或
      - meta = {"backend": "android", "serial": "emulator-5554", "authorized": True}
               {"backend": "macos", "pid": 1234, "authorized": True}
               {"backend": "windows", "pid": 1234 | "title": "...", "authorized": True}
    """

    def to_uir(self, req: GenRequest, ctx: BuildContext) -> UIR:
        meta = req.meta or {}
        shot = req.image_path
        if req.source_path:
            tree = load_tree(req.source_path)
        else:
            backend = meta.get("backend")
            if not meta.get("authorized"):
                raise PermissionError("a11y 实时采集只用于自有/授权应用: 需显式 meta.authorized=True")
            if backend == "android":
                tree = capture_android(meta.get("serial"), out_dir=ctx.ws, adb=meta.get("adb"))
            elif backend == "macos":
                tree = capture_macos(int(meta["pid"]), int(meta.get("window", 0)), out_dir=ctx.ws)
            elif backend == "windows":
                tree = capture_windows(pid=meta.get("pid"), title=meta.get("title"))
            else:
                raise ValueError(f"未知 a11y 后端: {backend!r}; 可用: {PLATFORMS}")
            try:
                Path(ctx.ws).mkdir(parents=True, exist_ok=True)
                (Path(ctx.ws) / "a11y_tree.json").write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        uir = tree_to_uir(tree, shot)
        rep = uir.meta["report"]
        ctx.log("UIR", f"a11y({rep['platform']})→UIR: 树 {rep['nodes_in']} 节点 → 保留 {rep['nodes_kept']}, "
                       f"布局 列/行/叠 {rep['layout']['col']}/{rep['layout']['row']}/{rep['layout']['stack']}, "
                       f"截图采色 {'是' if rep['sampled_colors'] else '否'}, 内嵌图 {rep['images_embedded']}")
        return uir
