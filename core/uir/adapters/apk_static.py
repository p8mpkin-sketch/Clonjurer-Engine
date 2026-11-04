"""授权 APK / 布局资源 → UIR(L2 静态路线, DESIGN.md §9.7)。

输入两种形态(都是用户自有/授权的公开资源):
  1) 已编译 APK(zip): 只读 AndroidManifest.xml / res/layout*/ *.xml(二进制 AXML) / resources.arsc
     (字符串资源) / res/drawable*|mipmap*(图片文件名);
  2) Android 工程资源目录或其 zip: res/layout/*.xml 为明文 XML, res/values/strings.xml 明文。

硬边界(只做公开资源解析, 不做任何绕过保护的尝试):
  - 从不读取 classes*.dex / lib/*.so / assets 内任何文件, 不解密、不脱壳、不 hook;
  - 布局不存在或不是合法 AXML/XML → 报告 static_unavailable=True 并给出「需运行期授权采集
    (uiautomator 界面树)」建议, 到此为止。
控件映射(Android → UIR role): LinearLayout→box(flex, 按 orientation), FrameLayout/Constraint/
Relative→box(stack), ScrollView→box, TextView→text, EditText→input, Button→button, ImageView→image,
RecyclerView/ListView/GridView→list, Toolbar/AppBar→header, CardView→card, Switch/CheckBox→button。
"""
import io
import re
import struct
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from core.uir.model import UIR, Node, Tokens
from core.uir.registry import register_input, GenRequest, BuildContext

FORBIDDEN_PREFIXES = ("classes", "lib/", "assets/", "META-INF/")   # 从不读取


# ---------------------------------------------------------------------------
# 二进制 AXML / ARSC 只读解析(仅字符串池 + 元素/属性, 够还原布局树)
# ---------------------------------------------------------------------------
_RES_STRING_POOL = 0x0001
_RES_XML = 0x0003
_RES_XML_START_ELEMENT = 0x0102
_RES_XML_END_ELEMENT = 0x0103
_RES_XML_RESOURCE_MAP = 0x0180
_RES_TABLE = 0x0002
_RES_TABLE_PACKAGE = 0x0200
_RES_TABLE_TYPE = 0x0201
_RES_TABLE_TYPE_SPEC = 0x0202
_TYPE_REFERENCE, _TYPE_STRING, _TYPE_INT_DEC, _TYPE_INT_HEX, _TYPE_INT_BOOLEAN = 0x01, 0x03, 0x10, 0x11, 0x12
_TYPE_DIMENSION, _TYPE_INT_COLOR_ARGB8, _TYPE_INT_COLOR_RGB8 = 0x05, 0x1c, 0x1e
_DIM_UNITS = ("px", "dp", "sp", "pt", "in", "mm")


def _read_string_pool(buf: bytes, off: int) -> list:
    typ, hsize, size = struct.unpack_from("<HHI", buf, off)
    if typ != _RES_STRING_POOL:
        return []
    n_str, _n_sty, flags, str_start, _sty_start = struct.unpack_from("<IIIII", buf, off + 8)
    utf8 = bool(flags & (1 << 8))
    offsets = struct.unpack_from(f"<{n_str}I", buf, off + 28)
    base = off + str_start
    out = []
    for o in offsets:
        p = base + o
        try:
            if utf8:
                # 两个变长长度: 字符数, 字节数
                c = buf[p]; p += 2 if c & 0x80 else 1
                b = buf[p]; ln = ((b & 0x7F) << 8 | buf[p + 1]) if b & 0x80 else b
                p += 2 if b & 0x80 else 1
                out.append(buf[p:p + ln].decode("utf-8", "replace"))
            else:
                ln = struct.unpack_from("<H", buf, p)[0]; p += 2
                if ln & 0x8000:
                    ln = ((ln & 0x7FFF) << 16) | struct.unpack_from("<H", buf, p)[0]; p += 2
                out.append(buf[p:p + ln * 2].decode("utf-16-le", "replace"))
        except (IndexError, struct.error):
            out.append("")
    return out


def _typed_value(t: int, d: int, strings: list):
    if t == _TYPE_STRING:
        return strings[d] if 0 <= d < len(strings) else ""
    if t == _TYPE_REFERENCE:
        return f"@0x{d:08x}"
    if t in (_TYPE_INT_DEC, _TYPE_INT_HEX):
        return d
    if t == _TYPE_INT_BOOLEAN:
        return bool(d)
    if t == _TYPE_DIMENSION:
        val = (d >> 8) / (1 << ((d >> 4) & 3) * 0)  # 整数部分(忽略定点小数位, 布局足够)
        unit = _DIM_UNITS[d & 0xF] if (d & 0xF) < len(_DIM_UNITS) else "px"
        return f"{int((d >> 8))}{unit}"
    if t in (_TYPE_INT_COLOR_ARGB8, _TYPE_INT_COLOR_RGB8):
        return f"#{d & 0xFFFFFF:06x}"
    return d


def parse_axml(buf: bytes) -> ET.Element:
    """二进制 AXML → ElementTree(属性名去命名空间, 值已类型化为字符串)。非法则抛 ValueError。"""
    if len(buf) < 8 or struct.unpack_from("<H", buf, 0)[0] != _RES_XML:
        raise ValueError("不是 AXML")
    strings, off, size = [], 8, len(buf)
    stack, root = [], None
    while off + 8 <= size:
        typ, hsize, csize = struct.unpack_from("<HHI", buf, off)
        if csize < 8:
            break
        if typ == _RES_STRING_POOL:
            strings = _read_string_pool(buf, off)
        elif typ == _RES_XML_START_ELEMENT:
            _ns, name_i, _as, _asz, n_attr = struct.unpack_from("<IIHHH", buf, off + 16)
            name = strings[name_i] if name_i < len(strings) else "view"
            el = ET.Element(name.split(".")[-1])
            p = off + hsize + 20
            for _ in range(n_attr):
                _ans, an, raw, _tsz, _r, tt, td = struct.unpack_from("<IIIHBBI", buf, p)
                aname = strings[an] if an < len(strings) else f"attr{an}"
                val = strings[raw] if 0 <= raw < len(strings) and tt == _TYPE_STRING else _typed_value(tt, td, strings)
                el.set(aname, str(val))
                p += 20
            if stack:
                stack[-1].append(el)
            else:
                root = el
            stack.append(el)
        elif typ == _RES_XML_END_ELEMENT:
            if stack:
                stack.pop()
        off += csize
    if root is None:
        raise ValueError("AXML 无元素")
    return root


def parse_arsc_strings(buf: bytes) -> dict:
    """resources.arsc → {资源 id: 字符串值}(仅 string 类型, 用于 @string 引用解析)。失败返回 {}。"""
    out = {}
    try:
        if struct.unpack_from("<H", buf, 0)[0] != _RES_TABLE:
            return out
        off = struct.unpack_from("<H", buf, 2)[0]
        global_strings = []
        while off + 8 <= len(buf):
            typ, hsize, csize = struct.unpack_from("<HHI", buf, off)
            if typ == _RES_STRING_POOL and not global_strings:
                global_strings = _read_string_pool(buf, off)
            elif typ == _RES_TABLE_PACKAGE:
                pkg_id = struct.unpack_from("<I", buf, off + 8)[0]
                type_strings_off = struct.unpack_from("<I", buf, off + 268)[0]
                type_names = _read_string_pool(buf, off + type_strings_off)
                p = off + hsize
                end = off + csize
                while p + 8 <= end:
                    t2, h2, c2 = struct.unpack_from("<HHI", buf, p)
                    if t2 == _RES_TABLE_TYPE:
                        tid = buf[p + 8]
                        tname = type_names[tid - 1] if 0 < tid <= len(type_names) else ""
                        if tname == "string":
                            n_ent, ent_start = struct.unpack_from("<II", buf, p + 12)
                            offs = struct.unpack_from(f"<{n_ent}I", buf, p + h2)
                            for i, eo in enumerate(offs):
                                if eo == 0xFFFFFFFF:
                                    continue
                                e = p + ent_start + eo
                                esize, eflags = struct.unpack_from("<HH", buf, e)
                                if eflags & 1:   # complex
                                    continue
                                _sz, _r, vt, vd = struct.unpack_from("<HBBI", buf, e + 8)
                                if vt == _TYPE_STRING and vd < len(global_strings):
                                    rid = (pkg_id << 24) | (tid << 16) | i
                                    out.setdefault(rid, global_strings[vd])
                    p += max(c2, 8)
            off += max(csize, 8)
    except (struct.error, IndexError):
        pass
    return out


# ---------------------------------------------------------------------------
# Android 控件 → UIR
# ---------------------------------------------------------------------------
_ROLE = {
    "TextView": "text", "AppCompatTextView": "text", "MaterialTextView": "text",
    "EditText": "input", "AppCompatEditText": "input", "TextInputEditText": "input",
    "Button": "button", "AppCompatButton": "button", "MaterialButton": "button", "ImageButton": "button",
    "FloatingActionButton": "button", "Switch": "button", "CheckBox": "button", "RadioButton": "button",
    "ImageView": "image", "AppCompatImageView": "image", "ShapeableImageView": "image",
    "RecyclerView": "list", "ListView": "list", "GridView": "list", "ViewPager": "list", "ViewPager2": "list",
    "Toolbar": "header", "MaterialToolbar": "header", "AppBarLayout": "header", "BottomNavigationView": "nav",
    "TabLayout": "nav", "NavigationView": "nav", "CardView": "card", "MaterialCardView": "card",
    "View": "divider", "ProgressBar": "badge", "Chip": "badge",
}
_DIM_RE = re.compile(r"^(-?\d+(?:\.\d+)?)(dp|dip|sp|px|pt)?$")


def _dim(v):
    if v in (None, ""):
        return None
    if v in ("match_parent", "fill_parent", "-1"):
        return "100%"
    if v in ("wrap_content", "-2"):
        return "auto"
    m = _DIM_RE.match(str(v))
    return int(float(m.group(1))) if m else None


def _color(v, strings: dict):
    if not v:
        return None
    v = str(v)
    if v.startswith("#"):
        return "#" + v[-6:] if len(v) in (7, 9) else v
    return None


class _Ctx:
    def __init__(self, strings: dict, drawables: set):
        self.strings, self.drawables, self.tokens = strings, drawables, Tokens(
            color={"c0": "#ffffff", "c1": "#212121", "c2": "#6200ee", "c3": "#757575", "c4": "#f5f5f5"},
            text_style={"t0": {"size": 14, "weight": 400, "line": 1.4, "color": "c1"},
                        "t1": {"size": 20, "weight": 600, "line": 1.2, "color": "c1"},
                        "t4": {"size": 14, "weight": 600, "line": 1.2, "color": "c0", "align": "center"}},
            space={"s0": 0, "s1": 4, "s2": 8, "s3": 12, "s4": 16, "s5": 24})
        self.unresolved = 0

    def text(self, v):
        if not v:
            return ""
        v = str(v)
        m = re.match(r"^@(?:string/)?(?:0x)?([0-9a-fA-F]{8})$", v)
        if m:
            s = self.strings.get(int(m.group(1), 16))
            if s is None:
                self.unresolved += 1
                return ""
            return s
        if v.startswith("@string/"):
            s = self.strings.get(v[8:])
            if s is None:
                self.unresolved += 1
                return ""
            return s
        return v


def view_to_node(el: ET.Element, ctx: _Ctx) -> Node:
    tag = el.tag.split(".")[-1].split("}")[-1]
    a = {k.split("}")[-1]: v for k, v in el.attrib.items()}
    role = _ROLE.get(tag, "box")
    if tag.endswith("Layout") or tag in ("ScrollView", "NestedScrollView", "HorizontalScrollView", "FrameLayout",
                                          "LinearLayoutCompat", "CoordinatorLayout", "DrawerLayout"):
        role = "box" if role == "box" else role
    layout, box, content, intents = {}, {}, {}, []
    if tag in ("LinearLayout", "LinearLayoutCompat", "TableLayout", "TableRow"):
        layout = {"mode": "flex", "direction": "row" if a.get("orientation") in ("horizontal", "0") or tag == "TableRow" else "col"}
    elif tag in ("FrameLayout", "ConstraintLayout", "RelativeLayout", "CoordinatorLayout", "MotionLayout"):
        layout = {"mode": "flex", "direction": "col"}
    elif tag in ("GridLayout", "GridView"):
        layout = {"mode": "grid", "cols": int(a.get("columnCount") or a.get("numColumns") or 2)}
    elif role in ("list", "card", "header", "nav"):
        layout = {"mode": "flex", "direction": "row" if a.get("orientation") in ("horizontal", "0") or role == "nav" else "col"}
    w, h = _dim(a.get("layout_width")), _dim(a.get("layout_height"))
    if w is not None:
        box["w"] = w
    if h is not None and h != "auto":
        box["h"] = h
    pad = [_dim(a.get(k)) for k in ("paddingTop", "paddingEnd", "paddingBottom", "paddingStart")]
    if a.get("padding"):
        pad = [_dim(a["padding"])] * 4
    if any(p is not None for p in pad):
        box["pad"] = [p if isinstance(p, int) else 0 for p in pad]
    mg = [_dim(a.get(k)) for k in ("layout_marginTop", "layout_marginEnd", "layout_marginBottom", "layout_marginStart")]
    if a.get("layout_margin"):
        mg = [_dim(a["layout_margin"])] * 4
    if any(m is not None for m in mg):
        box["margin"] = [m if isinstance(m, int) else 0 for m in mg]
    bg = _color(a.get("background"), ctx.strings)
    if bg:
        box["bg"] = bg
    if a.get("cardCornerRadius") or a.get("radius"):
        box["radius"] = _dim(a.get("cardCornerRadius") or a.get("radius")) or 8
    if a.get("layout_weight"):
        box["grow"] = 1
    text_ref = ""
    if role in ("text", "button", "input", "badge"):
        txt = ctx.text(a.get("text"))
        if role == "input":
            content = {"placeholder": ctx.text(a.get("hint")), "text": txt,
                       "type": "password" if "Password" in (a.get("inputType") or "") else "text"}
            intents = [{"on": "input", "do": "input"}]
        else:
            content = {"text": txt}
            if role == "button":
                intents = [{"on": "click", "do": "submit-form" if re.search(r"登录|提交|确定|submit|login|sign", txt, re.I) else "navigate"}]
        size = _dim(a.get("textSize"))
        color = _color(a.get("textColor"), ctx.strings)
        if size or color:
            key = f"ta{len(ctx.tokens.text_style)}"
            ctx.tokens.text_style[key] = {"size": size or 14, "weight": 600 if "bold" in (a.get("textStyle") or "") else 400,
                                          "line": 1.3, "color": color or ("c0" if role == "button" else "c1")}
            text_ref = key
        else:
            text_ref = "t4" if role == "button" else "t0"
        if role == "button" and "bg" not in box:
            box["bg"] = "c2"
            box.setdefault("pad", [10, 16, 10, 16])
            box.setdefault("radius", 4)
    elif role == "image":
        src = a.get("src") or a.get("srcCompat") or ""
        name = src.split("/")[-1] if src else ""
        content = {"alt": name or "图片"}
        if name and any(d.rsplit(".", 1)[0] == name for d in ctx.drawables):
            content["drawable"] = name
    elif role == "divider":
        if (h is not None and isinstance(h, int) and h <= 2) or (w is not None and isinstance(w, int) and w <= 2):
            role = "divider"
        else:
            role = "box"
    node = Node(role=role, layout=layout, box=box, content=content, intents=intents, text_ref=text_ref)
    for child in list(el):
        if child.tag.split("}")[-1] in ("requestFocus", "tag", "include", "merge"):
            continue
        node.children.append(view_to_node(child, ctx))
    return node


# ---------------------------------------------------------------------------
class ApkSource:
    """统一读取 APK zip / 工程目录 / 工程 zip 的公开资源。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.zip = zipfile.ZipFile(self.path) if self.path.is_file() else None
        self.names = ([n for n in self.zip.namelist()] if self.zip
                      else [str(p.relative_to(self.path)) for p in self.path.rglob("*") if p.is_file()])
        self.report = {"source": self.path.name, "kind": "apk" if self.zip and "AndroidManifest.xml" in self.names else "project",
                       "skipped_protected_sections": [p for p in FORBIDDEN_PREFIXES if any(n.startswith(p) for n in self.names)]}

    def read(self, name: str) -> bytes:
        if name.startswith(FORBIDDEN_PREFIXES):
            raise PermissionError(f"边界: 不读取 {name}")
        return self.zip.read(name) if self.zip else (self.path / name).read_bytes()

    def layouts(self) -> list:
        return sorted(n for n in self.names if re.match(r"^res/layout[^/]*/[^/]+\.xml$", n))

    def drawables(self) -> set:
        return {n.rsplit("/", 1)[-1] for n in self.names if re.match(r"^res/(drawable|mipmap)[^/]*/", n)}

    def strings(self) -> dict:
        out = {}
        if "resources.arsc" in self.names:
            out.update(parse_arsc_strings(self.read("resources.arsc")))
        for n in self.names:
            if re.match(r"^res/values[^/]*/strings\.xml$", n):
                try:
                    root = ET.fromstring(self.read(n))
                    for s in root.iter("string"):
                        if s.get("name") and s.text:
                            out.setdefault(s.get("name"), s.text.strip())
                except ET.ParseError:
                    pass
        return out

    def manifest(self) -> dict:
        if "AndroidManifest.xml" not in self.names:
            return {}
        raw = self.read("AndroidManifest.xml")
        try:
            root = parse_axml(raw) if raw[:2] == b"\x03\x00" else ET.fromstring(raw)
        except (ValueError, ET.ParseError):
            return {"error": "manifest 不可解析"}
        a = {k.split("}")[-1]: v for k, v in root.attrib.items()}
        app = root.find("application")
        acts = [{k.split("}")[-1]: v for k, v in x.attrib.items()}.get("name", "") for x in (app.iter("activity") if app is not None else [])]
        return {"package": a.get("package", ""), "label": ({k.split("}")[-1]: v for k, v in app.attrib.items()}.get("label", "") if app is not None else ""),
                "activities": acts[:50]}

    def parse_layout(self, name: str) -> ET.Element:
        raw = self.read(name)
        if raw[:2] == b"\x03\x00":
            return parse_axml(raw)
        return ET.fromstring(raw)


def pick_layout(names: list, preferred: str = "") -> str:
    """选主界面布局: 显式指定 > activity_main > activity_* > 第一个。"""
    if preferred and preferred in names:
        return preferred
    for pat in (r"/activity_main\.xml$", r"/activity_[^/]+\.xml$", r"/fragment_[^/]+\.xml$", r"/main\.xml$"):
        for n in names:
            if re.search(pat, n):
                return n
    return names[0] if names else ""


def from_apk(path, layout_name: str = "") -> UIR:
    src = ApkSource(path)
    layouts = src.layouts()
    report = dict(src.report)
    report["layouts"] = len(layouts)
    manifest = src.manifest()
    report["manifest"] = manifest
    if not layouts:
        report["static_unavailable"] = True
        report["advice"] = ("未发现 res/layout*/ 布局资源(资源可能被加固/加密, 或界面由代码/Compose 构建)。"
                            "按边界不做任何绕过; 请改用授权设备上的运行期界面树采集(uiautomator dump → a11y 适配器)。")
        root = Node(role="box", layout={"mode": "flex", "direction": "col"}, box={"pad": [24]},
                    children=[Node(role="text", text_ref="t1", content={"text": "静态路线不可用"}),
                              Node(role="text", text_ref="t0", content={"text": report["advice"]})])
        ctx = _Ctx({}, set())
        return UIR(root=root, tokens=ctx.tokens, meta={"source_kind": "apk_static", "report": report, "title": manifest.get("label", "")}).normalize()
    chosen = pick_layout(layouts, layout_name)
    strings = src.strings()
    ctx = _Ctx(strings, src.drawables())
    try:
        tree = src.parse_layout(chosen)
    except (ValueError, ET.ParseError) as exc:
        report["static_unavailable"] = True
        report["advice"] = f"布局 {chosen} 不是合法 AXML/XML({exc}); 不做绕过, 建议运行期授权采集。"
        root = Node(role="box", children=[Node(role="text", text_ref="t0", content={"text": report["advice"]})])
        return UIR(root=root, tokens=ctx.tokens, meta={"source_kind": "apk_static", "report": report}).normalize()
    root = view_to_node(tree, ctx)
    root.box.setdefault("w", "100%")
    root.box.setdefault("min_h", "100vh")
    root.box.setdefault("bg", "c0")
    report.update({"layout": chosen, "strings": len(strings), "unresolved_refs": ctx.unresolved,
                   "all_layouts": layouts[:200]})
    title = ctx.text(manifest.get("label")) or manifest.get("package", "") or src.path.stem
    return UIR(root=root, tokens=ctx.tokens,
               meta={"source_kind": "apk_static", "title": title, "lang": "zh", "report": report,
                     "viewport": {"w": 390, "h": 844}}).normalize()


@register_input("apk_static")
class ApkStaticAdapter:
    def to_uir(self, req: GenRequest, ctx: BuildContext) -> UIR:
        uir = from_apk(req.source_path, req.meta.get("layout", ""))
        rep = uir.meta.get("report", {})
        if rep.get("static_unavailable"):
            ctx.log("UIR", f"APK 静态路线不可用: {rep.get('advice')}")
        else:
            ctx.log("UIR", f"APK→UIR: {rep.get('layout')} → {uir.root.count()} 节点, 字符串 {rep.get('strings')}, "
                           f"未解析引用 {rep.get('unresolved_refs')}; 跳过受保护段 {rep.get('skipped_protected_sections')}")
        return uir
