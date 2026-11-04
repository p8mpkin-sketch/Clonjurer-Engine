"""L2 骨架护栏(DESIGN.md §9.7/§9.11):

- apk_static 适配器: 工程明文布局 → UIR 控件映射; 二进制 AXML 解析(用测试内最小编码器构造, 往返一致);
  从不读取 classes*.dex / lib / assets(即使存在也只记录"跳过"); 布局缺失 → static_unavailable + 运行期采集建议, 不做绕过;
- flutter 目标: 产出可读 main.dart/theme.dart/pubspec, 令牌进 AppColors, 意图落 onPressed 桩, 不含外链;
- 注册表: apk_static / flutter 可发现。
"""
import io
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.uir.adapters import apk_static as A  # noqa: E402
from core.uir.adapters.description import build_from_description  # noqa: E402
from core.uir.registry import list_inputs, list_outputs  # noqa: E402
from core.uir.targets.flutter import FlutterTarget  # noqa: E402

LAYOUT_XML = """<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent" android:layout_height="match_parent"
    android:orientation="vertical" android:padding="16dp" android:background="#FAFAFA">
  <androidx.appcompat.widget.Toolbar android:layout_width="match_parent" android:layout_height="56dp" android:background="#6200EE"/>
  <TextView android:layout_width="wrap_content" android:layout_height="wrap_content"
      android:text="@string/welcome" android:textSize="20sp" android:textStyle="bold" android:textColor="#212121"/>
  <EditText android:layout_width="match_parent" android:layout_height="wrap_content" android:hint="@string/phone_hint"/>
  <EditText android:layout_width="match_parent" android:layout_height="wrap_content" android:hint="密码" android:inputType="textPassword"/>
  <LinearLayout android:layout_width="match_parent" android:layout_height="wrap_content" android:orientation="horizontal">
    <ImageView android:layout_width="40dp" android:layout_height="40dp" android:src="@drawable/ic_logo"/>
    <Button android:layout_width="0dp" android:layout_weight="1" android:layout_height="wrap_content" android:text="登录"/>
  </LinearLayout>
  <View android:layout_width="match_parent" android:layout_height="1dp" android:background="#E0E0E0"/>
  <androidx.recyclerview.widget.RecyclerView android:layout_width="match_parent" android:layout_height="wrap_content"/>
</LinearLayout>
"""
STRINGS_XML = """<resources><string name="welcome">欢迎登录云帆</string><string name="phone_hint">手机号</string><string name="app_name">云帆</string></resources>"""
MANIFEST_XML = """<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.example.yunfan">
  <application android:label="@string/app_name"><activity android:name=".MainActivity"/></application></manifest>"""


def _project_zip(with_layout=True, with_dex=False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("AndroidManifest.xml", MANIFEST_XML)
        if with_layout:
            z.writestr("res/layout/activity_main.xml", LAYOUT_XML)
        z.writestr("res/values/strings.xml", STRINGS_XML)
        z.writestr("res/drawable/ic_logo.png", b"\x89PNG\r\n\x1a\n")
        if with_dex:
            z.writestr("classes.dex", b"dex\n035\0" + b"\0" * 64)
            z.writestr("lib/arm64-v8a/libnative.so", b"\x7fELF" + b"\0" * 32)
            z.writestr("assets/secret.bin", b"\0" * 16)
    return buf.getvalue()


# ---- 测试内最小 AXML 编码器(字符串池 UTF-16 + 起止元素), 只为验证解析器往返 ----
def _encode_axml(tag: str, attrs: dict, children=()) -> bytes:
    strings = []

    def sid(s):
        if s not in strings:
            strings.append(s)
        return strings.index(s)

    def elem(tag, attrs, children):
        ti = sid(tag)
        items = [(sid(k), sid(v)) for k, v in attrs.items()]
        body = struct.pack("<IIHHH", 0xFFFFFFFF, ti, 20, 20, len(items)) + struct.pack("<HHH", 0, 0, 0)
        for ni, vi in items:
            body += struct.pack("<IIIHBBI", 0xFFFFFFFF, ni, vi, 8, 0, A._TYPE_STRING, vi)
        start = struct.pack("<HHI", A._RES_XML_START_ELEMENT, 16, 16 + len(body)) + struct.pack("<II", 0, 0) + body
        inner = b"".join(elem(*c) for c in children)
        end = struct.pack("<HHI", A._RES_XML_END_ELEMENT, 16, 24) + struct.pack("<IIII", 0, 0, 0xFFFFFFFF, ti)
        return start + inner + end

    body = elem(tag, attrs, children)
    enc = b"".join(struct.pack("<H", len(s)) + s.encode("utf-16-le") + b"\0\0" for s in strings)
    offs, p = [], 0
    for s in strings:
        offs.append(p)
        p += 2 + len(s) * 2 + 2
    header = struct.pack("<HHI", A._RES_STRING_POOL, 28, 28 + 4 * len(strings) + len(enc))
    pool = header + struct.pack("<IIIII", len(strings), 0, 0, 28 + 4 * len(strings), 0) + struct.pack(f"<{len(strings)}I", *offs) + enc
    return struct.pack("<HHI", A._RES_XML, 8, 8 + len(pool) + len(body)) + pool + body


class ApkStaticGuards(unittest.TestCase):
    def test_project_zip_to_uir(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "app.zip"
            p.write_bytes(_project_zip())
            u = A.from_apk(p)
            self.assertEqual(u.validate(), [])
            rep = u.meta["report"]
            self.assertFalse(rep.get("static_unavailable"))
            self.assertEqual(rep["layout"], "res/layout/activity_main.xml")
            self.assertEqual(rep["manifest"]["package"], "com.example.yunfan")
            self.assertEqual(u.meta["title"], "云帆")
            roles = [n.role for n in u.root.walk()]
            for r in ("header", "text", "input", "button", "image", "divider", "list"):
                self.assertIn(r, roles, r)
            texts = [n.content.get("text") for n in u.root.walk() if n.content.get("text")]
            self.assertIn("欢迎登录云帆", texts)               # @string 解析
            inputs = [n for n in u.root.walk() if n.role == "input"]
            self.assertEqual(inputs[0].content["placeholder"], "手机号")
            self.assertEqual(inputs[1].content["type"], "password")
            btn = next(n for n in u.root.walk() if n.role == "button")
            self.assertEqual(btn.intents[0]["do"], "submit-form")
            self.assertEqual(btn.box.get("grow"), 1)
            self.assertEqual(u.root.layout, {"mode": "flex", "direction": "col"})
            self.assertEqual(u.root.box.get("pad"), [16, 16, 16, 16])

    def test_protected_sections_never_read(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "app.zip"
            p.write_bytes(_project_zip(with_dex=True))
            src = A.ApkSource(p)
            self.assertEqual(sorted(src.report["skipped_protected_sections"]), ["assets/", "classes", "lib/"])
            for name in ("classes.dex", "lib/arm64-v8a/libnative.so", "assets/secret.bin"):
                with self.assertRaises(PermissionError):
                    src.read(name)
            u = A.from_apk(p)     # 有 dex/so 不影响公开资源路线
            self.assertFalse(u.meta["report"].get("static_unavailable"))
            # 适配器从不打开受保护条目: 用 zip 读取记录核对(比字符串扫描更可靠)
            opened = []
            orig = src.zip.read
            src.zip.read = lambda n: (opened.append(n), orig(n))[1]
            A.from_apk(p)
            src2 = A.ApkSource(p)
            self.assertTrue(all(not n.startswith(A.FORBIDDEN_PREFIXES) for n in src2.layouts()))
            self.assertFalse(any(n.startswith(A.FORBIDDEN_PREFIXES) for n in opened))

    def test_no_layout_is_reported_not_bypassed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "app.zip"
            p.write_bytes(_project_zip(with_layout=False))
            u = A.from_apk(p)
            rep = u.meta["report"]
            self.assertTrue(rep["static_unavailable"])
            self.assertIn("运行期", rep["advice"])
            self.assertEqual(u.validate(), [])

    def test_binary_axml_roundtrip(self):
        raw = _encode_axml("LinearLayout", {"layout_width": "match_parent", "orientation": "vertical", "padding": "8dp"},
                           [("TextView", {"text": "你好", "textSize": "18sp"}, []),
                            ("Button", {"text": "提交"}, [])])
        root = A.parse_axml(raw)
        self.assertEqual(root.tag, "LinearLayout")
        self.assertEqual(root.get("orientation"), "vertical")
        self.assertEqual([c.tag for c in root], ["TextView", "Button"])
        self.assertEqual(root[0].get("text"), "你好")
        with self.assertRaises(ValueError):
            A.parse_axml(b"<not axml/>")
        # 整包: 二进制布局 + 明文 strings
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bin.apk"
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("AndroidManifest.xml", MANIFEST_XML)
                z.writestr("res/layout/activity_main.xml", raw)
                z.writestr("res/values/strings.xml", STRINGS_XML)
            p.write_bytes(buf.getvalue())
            u = A.from_apk(p)
            self.assertEqual([n.role for n in u.root.children], ["text", "button"])
            self.assertEqual(u.root.box.get("pad"), [8, 8, 8, 8])

    def test_project_directory(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "res/layout").mkdir(parents=True)
            (Path(d) / "res/values").mkdir(parents=True)
            (Path(d) / "res/layout/fragment_home.xml").write_text(LAYOUT_XML, encoding="utf-8")
            (Path(d) / "res/values/strings.xml").write_text(STRINGS_XML, encoding="utf-8")
            u = A.from_apk(d)
            self.assertEqual(u.meta["report"]["kind"], "project")
            self.assertEqual(u.meta["report"]["layout"], "res/layout/fragment_home.xml")


class FlutterTargetGuards(unittest.TestCase):
    def test_renders_readable_dart(self):
        u = build_from_description("蓝色登录页, 品牌叫 云帆, 有短信验证码")
        with tempfile.TemporaryDirectory() as d:
            r = FlutterTarget().render(u, Path(d))
            self.assertFalse(r.shootable)
            main = (Path(d) / "flutter_app/lib/main.dart").read_text(encoding="utf-8")
            theme = (Path(d) / "flutter_app/lib/theme.dart").read_text(encoding="utf-8")
            self.assertTrue((Path(d) / "flutter_app/pubspec.yaml").is_file())
            for frag in ("runApp(", "Scaffold(", "Column(", "TextField(", "ElevatedButton(", "云帆", "SnackBar(", "obscureText: true"):
                self.assertIn(frag, main, frag)
            self.assertIn("class AppColors", theme)
            self.assertIn("Color(0xFF2563EB)", theme)
            self.assertNotIn("http", main)
            self.assertEqual(main.count("("), main.count(")"))   # 括号配对: 可读代码的最低闸门
            self.assertEqual(main.count("["), main.count("]"))

    def test_apk_uir_to_flutter(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "app.zip"
            p.write_bytes(_project_zip())
            u = A.from_apk(p)
            r = FlutterTarget().render(u, Path(d) / "out")
            main = r.entry_file.read_text(encoding="utf-8")
            self.assertIn("obscureText: true", main)
            self.assertIn("Expanded(", main)
            self.assertIn("Divider(", main)


class L2Registry(unittest.TestCase):
    def test_discoverable(self):
        self.assertIn("apk_static", list_inputs())
        self.assertIn("flutter", list_outputs())


if __name__ == "__main__":
    unittest.main()
