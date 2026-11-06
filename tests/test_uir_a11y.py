"""a11y 适配器族护栏(ROADMAP_95.md §3.1): 树 JSON 夹具 → UIR 确定性, 不依赖真机。

覆盖:
- 三后端的树解析: Android uiautomator XML、macOS AX 递归(假元素注入取值函数)、Windows UIA 元素 JSON;
- 树 → UIR: 角色归一、坐标换算(scale)、裁剪/折叠、列/行/叠布局推断、读屏描述不当可见文字、确定性;
- 参考截图采样: 背景色/文字色/墨迹行高(合成截图, 逐值断言);
- 适配器: 注册可发现、导出树文件输入、未声明授权拒绝实时采集、Android 采集只调用公开 adb 命令。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.uir.adapters import a11y  # noqa: E402
from core.uir.adapters.a11y import (A11yAdapter, load_tree, normalize_role, parse_uia,  # noqa: E402
                                    parse_uiautomator_xml, tree_to_uir, walk_ax)
from core.uir.registry import BuildContext, GenRequest, list_inputs  # noqa: E402
from core.uir.targets.html_css import HtmlCssTarget  # noqa: E402

# 420dpi(scale 2.625)设备上的一个设置类界面: 工具栏 + 列表(图标/双行文字/更多按钮) + 输入框 + 系统状态栏窗口
ANDROID_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
 <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="org.sample.app" content-desc=""
       checkable="false" checked="false" clickable="false" enabled="true" password="false" bounds="[0,0][1050,1785]">
  <node index="0" text="" resource-id="org.sample.app:id/toolbar" class="android.view.ViewGroup" package="org.sample.app"
        content-desc="" clickable="false" enabled="true" bounds="[0,63][1050,210]">
   <node index="0" text="设置" class="android.widget.TextView" package="org.sample.app" content-desc="" clickable="false"
         bounds="[42,100][210,171]"/>
   <node index="1" text="" class="android.widget.ImageButton" package="org.sample.app" content-desc="搜索" clickable="true"
         bounds="[903,63][1050,210]"/>
  </node>
  <node index="1" text="" resource-id="org.sample.app:id/list" class="androidx.recyclerview.widget.RecyclerView"
        package="org.sample.app" content-desc="" clickable="false" bounds="[0,210][1050,1785]">
   <node index="0" text="" class="android.widget.LinearLayout" package="org.sample.app" content-desc="" clickable="true"
         bounds="[0,210][1050,399]">
    <node index="0" text="" class="android.widget.ImageView" package="org.sample.app" content-desc="" clickable="false"
          bounds="[42,273][105,336]"/>
    <node index="1" text="" class="android.widget.RelativeLayout" package="org.sample.app" content-desc="" clickable="false"
          bounds="[189,252][900,357]">
     <node index="0" text="无线网络" class="android.widget.TextView" package="org.sample.app" clickable="false"
           bounds="[189,252][420,310]"/>
     <node index="1" text="已连接" class="android.widget.TextView" package="org.sample.app" clickable="false"
           bounds="[189,310][357,357]"/>
    </node>
   </node>
   <node index="1" text="" class="android.widget.EditText" package="org.sample.app" content-desc="请输入密码"
         clickable="true" password="true" bounds="[42,441][1008,567]"/>
   <node index="2" text="保存" class="com.google.android.material.button.MaterialButton" package="org.sample.app"
         content-desc="" clickable="true" bounds="[42,609][1008,735]"/>
   <node index="3" text="" class="android.widget.LinearLayout" package="org.sample.app" clickable="false"
         bounds="[0,3000][1050,3200]"/>
  </node>
 </node>
 <node index="1" text="" class="android.widget.FrameLayout" package="com.android.systemui" content-desc=""
       clickable="false" bounds="[0,0][1050,63]"/>
</hierarchy>
"""


def _walk(u):
    return list(u.root.walk())


class AndroidBackend(unittest.TestCase):
    def setUp(self):
        self.tree = parse_uiautomator_xml(ANDROID_XML, density_dpi=420)

    def test_parse(self):
        t = self.tree
        self.assertEqual(t["platform"], "android")
        self.assertEqual(t["scale"], 2.625)
        self.assertEqual(t["app"], "org.sample.app")
        self.assertEqual(t["window"], [0, 0, 1050, 1785])
        # 系统状态栏窗口不并入应用树
        self.assertEqual(len(t["root"]["children"]), 2)
        btn = t["root"]["children"][0]["children"][1]
        self.assertEqual(btn["bounds"], [903, 63, 147, 147])
        self.assertTrue(btn["clickable"])
        self.assertEqual(btn["desc"], "搜索")

    def test_to_uir(self):
        u = tree_to_uir(self.tree)
        self.assertEqual(u.validate(), [])
        self.assertEqual(u.meta["source_kind"], "a11y")
        self.assertEqual(u.meta["viewport"], {"w": 400, "h": 680})
        nodes = _walk(u)
        texts = [n.content.get("text") for n in nodes if n.role == "text"]
        self.assertEqual(texts, ["设置", "无线网络", "已连接"])
        # 只有读屏描述的 ImageButton = 图标按钮(带点击意图), 描述只进 alt 不当可见文字
        icon = [n for n in nodes if n.content.get("alt") == "搜索"]
        self.assertEqual(len(icon), 1)
        self.assertEqual(icon[0].role, "image")
        self.assertEqual(icon[0].intents, [{"on": "click", "do": "navigate"}])
        inp = [n for n in nodes if n.role == "input"][0]
        self.assertEqual(inp.content["type"], "password")
        self.assertEqual(inp.content["placeholder"], "请输入密码")
        btn = [n for n in nodes if n.role == "button"][0]
        self.assertEqual(btn.content["text"], "保存")
        self.assertEqual(btn.intents[0]["do"], "submit-form")
        self.assertEqual([n.role for n in nodes if n.role == "list"], ["list"])
        # 窗口外节点(y=3000)被裁掉
        self.assertFalse(any(n.box.get("h") == 76.2 for n in nodes))
        self.assertEqual(u.meta["report"]["nodes_in"], 13)

    def test_layout_geometry(self):
        u = tree_to_uir(self.tree)
        root = u.root
        self.assertEqual(root.layout, {"mode": "flex", "direction": "col", "align": "start"})
        toolbar, lst = root.children
        self.assertEqual(toolbar.layout["direction"], "row")
        self.assertEqual((toolbar.box["w"], toolbar.box["h"]), (400.0, 56.0))
        self.assertEqual(toolbar.box["margin"], [24.0, 0, 0, 0.0])      # 状态栏下方 63px / 2.625
        title, search = toolbar.children
        self.assertEqual(title.box["margin"][3], 16.0)                   # 42 / 2.625
        self.assertEqual(search.box["margin"][3], round(903 / 2.625 - 210 / 2.625, 1))
        row = lst.children[0]
        self.assertEqual(row.layout["direction"], "row")
        self.assertEqual([c.role for c in row.children], ["image", "box"])
        self.assertEqual(row.children[1].layout["direction"], "col")
        self.assertEqual(lst.box["pad"], [0])                           # 抵消 ul 默认内边距

    def test_deterministic(self):
        a = tree_to_uir(parse_uiautomator_xml(ANDROID_XML, 420)).to_json()
        b = tree_to_uir(parse_uiautomator_xml(ANDROID_XML, 420)).to_json()
        self.assertEqual(a, b)

    def test_renders_html(self):
        u = tree_to_uir(self.tree)
        with tempfile.TemporaryDirectory() as d:
            r = HtmlCssTarget().render(u, Path(d) / "w", None)
            html = r.entry_file.read_text(encoding="utf-8")
        for t in ("设置", "无线网络", "已连接", "保存", "请输入密码"):
            self.assertIn(t, html)


class FakeAX:
    def __init__(self, role, title="", value=None, desc="", pos=(0, 0), size=(0, 0), children=(), sub=""):
        self.a = {"AXRole": role, "AXTitle": title, "AXValue": value, "AXDescription": desc, "AXPosition": pos,
                  "AXSize": size, "AXChildren": list(children), "AXSubrole": sub, "AXEnabled": True}


def _fake_get(e, name):
    return e.a.get(name)


MAC_WIN = FakeAX("AXWindow", "偏好", pos=(100, 50), size=(600, 400), children=[
    FakeAX("AXToolbar", pos=(100, 50), size=(600, 52), children=[
        FakeAX("AXButton", "通用", pos=(110, 56), size=(64, 40)),
        FakeAX("AXButton", "账户", pos=(184, 56), size=(64, 40)),
    ]),
    FakeAX("AXGroup", pos=(100, 102), size=(600, 348), children=[
        FakeAX("AXStaticText", value="用户名", pos=(130, 130), size=(80, 17)),
        FakeAX("AXTextField", value="", desc="输入用户名", pos=(220, 126), size=(240, 24)),
        FakeAX("AXTextField", value="", pos=(220, 160), size=(240, 24), sub="AXSecureTextField"),
        FakeAX("AXCheckBox", "记住我", value=1, pos=(130, 200), size=(90, 18)),
        FakeAX("AXScrollBar", pos=(685, 102), size=(15, 348)),
        FakeAX("AXImage", desc="头像", pos=(560, 130), size=(64, 64)),
    ]),
])


class MacBackend(unittest.TestCase):
    def test_walk_ax(self):
        root = walk_ax(MAC_WIN, _fake_get)
        self.assertEqual(root["role"], "AXWindow")
        self.assertEqual(root["bounds"], [100.0, 50.0, 600.0, 400.0])
        grp = root["children"][1]
        self.assertTrue(grp["children"][1]["editable"])
        self.assertTrue(grp["children"][2]["password"])
        self.assertTrue(grp["children"][3]["checked"])

    def test_max_nodes_budget(self):
        root = walk_ax(MAC_WIN, _fake_get, max_nodes=3)
        self.assertLessEqual(sum(1 for _ in _iter(root)), 3)

    def test_to_uir(self):
        root = walk_ax(MAC_WIN, _fake_get)
        tree = {"platform": "macos", "app": "偏好", "scale": 1.0, "window": root["bounds"], "root": root}
        u = tree_to_uir(tree)
        self.assertEqual(u.validate(), [])
        self.assertEqual(u.meta["viewport"], {"w": 600, "h": 400})
        nodes = _walk(u)
        self.assertEqual(u.root.children[0].role, "header")
        self.assertEqual([n.content["text"] for n in nodes if n.role == "button"], ["通用", "账户", "记住我"])
        chk = [n for n in nodes if n.content.get("text") == "记住我"][0]
        self.assertEqual(chk.intents, [{"on": "click", "do": "toggle"}])
        self.assertEqual([n.content["type"] for n in nodes if n.role == "input"], ["text", "password"])
        self.assertFalse(any(n.box.get("w") == 15 for n in nodes))       # 滚动条不重建
        self.assertEqual(u.tokens.font["f0"]["family"], "-apple-system")
        # 窗口原点 (100,50) 归零: 根节点无外边距
        self.assertNotIn("margin", u.root.box)

    def test_capture_requires_pyobjc_or_permission(self):
        with mock.patch.object(a11y, "_ax_getter", side_effect=PermissionError("未授权")):
            with self.assertRaises(PermissionError):
                a11y.capture_macos(1)


def _iter(n):
    yield n
    for c in n.get("children", []):
        yield from _iter(c)


UIA = {"ControlType": "WindowControl", "Name": "记事本", "BoundingRectangle": [150, 75, 1350, 975], "Children": [
    {"ControlType": "TitleBar", "Name": "", "BoundingRectangle": [150, 75, 1350, 120], "Children": [
        {"ControlType": "Button", "Name": "关闭", "BoundingRectangle": [1281, 75, 1350, 120]}]},
    {"ControlType": "MenuBar", "Name": "", "BoundingRectangle": [150, 120, 1350, 156], "Children": [
        {"ControlType": "MenuItem", "Name": "文件", "BoundingRectangle": [156, 120, 210, 156]},
        {"ControlType": "MenuItem", "Name": "编辑", "BoundingRectangle": [210, 120, 264, 156]}]},
    {"ControlType": "Edit", "Name": "文本编辑器", "Value": "你好", "BoundingRectangle": [150, 156, 1350, 930]},
    {"ControlType": "StatusBar", "Name": "", "BoundingRectangle": [150, 930, 1350, 975], "Children": [
        {"ControlType": "Text", "Name": "第 1 行", "BoundingRectangle": [1050, 936, 1200, 969]}]},
]}


class WindowsBackend(unittest.TestCase):
    def test_parse_uia_scale(self):
        t = parse_uia(UIA, scale=1.5)
        self.assertEqual(t["platform"], "windows")
        self.assertEqual(t["root"]["bounds"], [150, 75, 1200, 900])
        u = tree_to_uir(t)
        self.assertEqual(u.validate(), [])
        self.assertEqual(u.meta["viewport"], {"w": 800, "h": 600})
        roles = [c.role for c in u.root.children]
        self.assertEqual(roles, ["header", "nav", "input", "footer"])
        nav = u.root.children[1]
        self.assertEqual(nav.layout["direction"], "row")
        self.assertEqual([c.role for c in nav.children], ["button", "button"])   # 叶子菜单项 = 可点击按钮
        self.assertEqual(u.root.children[2].content["text"], "你好")
        self.assertEqual(u.tokens.font["f0"]["family"], "Segoe UI")

    def test_load_tree_sniffs_formats(self):
        with tempfile.TemporaryDirectory() as d:
            p1, p2, p3 = Path(d, "a.xml"), Path(d, "b.json"), Path(d, "c.json")
            p1.write_text(ANDROID_XML, encoding="utf-8")
            p2.write_text(json.dumps(UIA, ensure_ascii=False), encoding="utf-8")
            p3.write_text(json.dumps(parse_uia(UIA)), encoding="utf-8")
            self.assertEqual(load_tree(p1)["platform"], "android")
            self.assertEqual(load_tree(p2)["platform"], "windows")
            self.assertEqual(load_tree(p3)["platform"], "windows")

    def test_live_capture_off_windows(self):
        if sys.platform.startswith("win"):
            self.skipTest("Windows 实机")
        with self.assertRaises(OSError):
            a11y.capture_windows(pid=1)


class LayoutInference(unittest.TestCase):
    def _tree(self, kids):
        return {"platform": "android", "scale": 1.0, "window": [0, 0, 300, 300],
                "root": {"role": "android.widget.FrameLayout", "bounds": [0, 0, 300, 300], "children": kids}}

    def test_overlap_becomes_stack(self):
        kids = [{"role": "android.widget.ImageView", "bounds": [0, 0, 300, 200], "children": []},
                {"role": "android.widget.TextView", "name": "标题", "bounds": [20, 150, 200, 30], "children": []}]
        u = tree_to_uir(self._tree(kids))
        self.assertEqual(u.root.layout, {"mode": "stack"})
        self.assertEqual(u.root.children[1].box["position"], {"top": 150.0, "left": 20.0})

    def test_redundant_wrapper_collapsed(self):
        inner = {"role": "android.widget.TextView", "name": "正文", "bounds": [10, 10, 100, 20], "children": []}
        wrap = {"role": "android.widget.FrameLayout", "bounds": [10, 10, 100, 20], "children": [inner]}
        u = tree_to_uir(self._tree([wrap]))
        self.assertEqual(u.root.children[0].role, "text")

    def test_role_normalization(self):
        self.assertEqual(normalize_role("android", "com.x.widget.FancyButton"), "button")
        self.assertEqual(normalize_role("android", "android.view.View", {"editable": True}), "input")
        self.assertEqual(normalize_role("macos", "AXStaticText"), "text")
        self.assertEqual(normalize_role("windows", "HyperlinkControl"), "link")
        self.assertEqual(normalize_role("windows", "Unknown"), "box")

    def test_unknown_platform(self):
        with self.assertRaises(ValueError):
            tree_to_uir({"platform": "ios", "root": {}})


class ScreenshotSampling(unittest.TestCase):
    """合成截图(2x 像素): 灰底卡片 + 深色「文字」墨迹块 → 背景色/文字色/字号/行内位置逐值确定。"""

    def test_colors_and_ink(self):
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as d:
            png = Path(d, "s.png")
            im = Image.new("RGB", (400, 300), "#ffffff")
            dr = ImageDraw.Draw(im)
            dr.rectangle([0, 0, 399, 99], fill="#eeeeee")               # 卡片 (0,0,200,50) 逻辑
            dr.rectangle([40, 36, 159, 61], fill="#222222")              # 墨迹: 逻辑 x20..80, y18..31 (h=13)
            im.save(png)
            tree = {"platform": "android", "scale": 2.0, "window": [0, 0, 400, 300],
                    "root": {"role": "android.widget.FrameLayout", "bounds": [0, 0, 400, 300], "children": [
                        {"role": "android.widget.LinearLayout", "bounds": [0, 0, 400, 100], "children": [
                            {"role": "android.widget.TextView", "name": "Hello", "bounds": [0, 10, 400, 80],
                             "children": []}]}]}}
            u = tree_to_uir(tree, png)
            u2 = tree_to_uir(tree, png)
        self.assertEqual(u.to_json(), u2.to_json())
        card = u.root.children[0]
        self.assertEqual(u.root.box["bg"], "#ffffff")
        self.assertEqual(card.box["bg"], "#eeeeee")
        text = card.children[0] if card.children else card
        st = u.tokens.text_style[text.text_ref]
        self.assertEqual(st["color"], "#222222")
        self.assertEqual(st["size"], 18)                                 # 松框: 墨迹 13 / 0.72(大写升部)
        self.assertEqual(text.box["pad"][3], 19.5)                       # 文字从框内 x=20 开始
        self.assertEqual(text.box["pad"][0], round(24.5 - 18 * 1.25 / 2 - 5, 1))  # 墨迹中线对齐
        self.assertTrue(u.meta["report"]["sampled_colors"])

    def test_image_embed_budget(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            png = Path(d, "s.png")
            Image.new("RGB", (100, 100), "#3366cc").save(png)
            tree = {"platform": "android", "scale": 1.0, "window": [0, 0, 100, 100],
                    "root": {"role": "android.widget.FrameLayout", "bounds": [0, 0, 100, 100], "children": [
                        {"role": "android.widget.ImageView", "bounds": [10, 10, 40, 40], "children": []}]}}
            with_img = tree_to_uir(tree, png)
            no_img = tree_to_uir(tree, png, embed_images=False)
        self.assertTrue(with_img.root.children[0].content["src"].startswith("data:image/png;base64,"))
        self.assertNotIn("src", no_img.root.children[0].content)


class AdapterGuards(unittest.TestCase):
    def _ctx(self, d):
        logs = []
        return BuildContext(ws=Path(d), log=lambda t, m: logs.append(m), deadline=1e18), logs

    def test_discoverable(self):
        self.assertIn("a11y", list_inputs())

    def test_exported_tree_input(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "dump.xml")
            p.write_text(ANDROID_XML, encoding="utf-8")
            ctx, logs = self._ctx(d)
            u = A11yAdapter().to_uir(GenRequest(kind="a11y", source_path=p), ctx)
        self.assertEqual(u.meta["source_kind"], "a11y")
        self.assertTrue(any("a11y(android)" in m for m in logs))

    def test_live_capture_requires_authorization(self):
        with tempfile.TemporaryDirectory() as d:
            ctx, _ = self._ctx(d)
            with self.assertRaises(PermissionError):
                A11yAdapter().to_uir(GenRequest(kind="a11y", meta={"backend": "android", "serial": "x"}), ctx)

    def test_android_capture_uses_only_public_commands(self):
        calls = []

        def fake_run(cmd, capture_output=True, timeout=None):
            calls.append(cmd)
            args = cmd[3:]
            out = b""
            if args[:2] == ["shell", "uiautomator"]:
                out = b"UI hierchary dumped to: /sdcard/uir_a11y_dump.xml"
            elif args[:2] == ["exec-out", "cat"]:
                out = ANDROID_XML.encode("utf-8")
            elif args[:3] == ["shell", "wm", "density"]:
                out = b"Physical density: 420\n"
            elif args[:2] == ["exec-out", "screencap"]:
                out = b"\x89PNG\r\n\x1a\n" + b"0" * 16
            return mock.Mock(returncode=0, stdout=out, stderr=b"")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(a11y.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(a11y.shutil, "which", return_value="/usr/bin/adb"):
            ctx, _ = self._ctx(d)
            u = A11yAdapter().to_uir(GenRequest(kind="a11y", meta={
                "backend": "android", "serial": "emulator-5554", "authorized": True}), ctx)
            self.assertTrue(Path(d, "a11y_tree.json").exists())
        self.assertEqual(u.meta["report"]["scale"], 2.625)
        allowed = {("shell", "uiautomator"), ("exec-out", "cat"), ("shell", "rm"), ("shell", "wm"),
                   ("shell", "dumpsys"), ("exec-out", "screencap")}
        for c in calls:
            self.assertEqual(c[:3], ["/usr/bin/adb", "-s", "emulator-5554"])
            self.assertIn(tuple(c[3:5]), allowed, c)
        self.assertFalse(any("pull" in c or "install" in c or "run-as" in c for c in calls))


class CorpusFixtures(unittest.TestCase):
    """入库的真实采集样本(engine_bench/a11y_cases): 树 + 截图 → UIR 合法且逐字节确定, 不需要设备与浏览器。"""

    def test_real_samples_deterministic(self):
        cases = sorted(p for g in ("tune", "holdout") for p in (ROOT / "engine_bench/a11y_cases" / g).glob("*/tree.json"))
        self.assertGreaterEqual(len(cases), 9)                           # 闸门: a11y 样本 ≥9
        for t in cases[:4]:
            tree = json.loads(t.read_text(encoding="utf-8"))
            shot = t.parent / "reference.png"
            a, b = tree_to_uir(tree, shot), tree_to_uir(tree, shot)
            self.assertEqual(a.validate(), [], t.parent.name)
            self.assertEqual(a.to_json(), b.to_json(), t.parent.name)
            self.assertTrue(a.meta["report"]["sampled_colors"])


if __name__ == "__main__":
    unittest.main()
