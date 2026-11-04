"""通用 UI 重建引擎(UIR)护栏(DESIGN.md §9):

- Schema/模型: 闭集校验、normalize 补 id、令牌引用 + 原值覆盖双层解析;
- Web 零回归闸门: 克隆产物 → UIR(web_dom) → HtmlCss 直通回吐, 与克隆直出逐字节相同
  (同一对图评分必然逐值一致);
- HtmlCss 目标: 无外链回源、脚本/事件属性被剥离、交互意图翻成运行时;
- 描述适配器: 五类通用原型可判定且产出合法 UIR; 不含任何站点特判;
- 视觉适配器 Tier 0: 版面切分确定性(同图两次结果相同)、节点上限;
- 调度分流: target_url 前缀判定, 与克隆任务互不干扰。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from core.uir import settings  # noqa: E402
from core.uir.model import UIR, Node, Tokens, ROLES  # noqa: E402
from core.uir.registry import list_inputs, list_outputs, get_output  # noqa: E402
from core.uir.adapters.description import build_from_description, detect_archetype  # noqa: E402
from core.uir.adapters.vision import segment  # noqa: E402
from core.uir.adapters.web_dom import from_clone_workspace  # noqa: E402
from core.uir.targets.html_css import render_html, HtmlCssTarget  # noqa: E402
from core.uir.generate import is_generate_task  # noqa: E402


class ModelGuards(unittest.TestCase):
    def test_normalize_and_validate(self):
        u = UIR(root=Node(role="box", children=[Node(role="text"), Node(role="weird")]))
        u.normalize()
        ids = [n.id for n in u.root.walk()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(u.root.children[1].role, "box")  # 非法角色归并为 box
        self.assertEqual(u.validate(), [])

    def test_closed_sets(self):
        u = UIR(root=Node(role="button", intents=[{"on": "click", "do": "launch-rocket"}])).normalize()
        self.assertTrue(any("交互意图非法" in e for e in u.validate()))
        u2 = UIR(root=Node(role="text", text_ref="t9")).normalize()
        self.assertTrue(any("text_ref 未定义" in e for e in u2.validate()))

    def test_token_ref_or_raw(self):
        t = Tokens(color={"c0": "#123456"}, space={"s2": 8})
        self.assertEqual(t.resolve("color", "c0"), "#123456")
        self.assertEqual(t.resolve("color", "#fff"), "#fff")      # 原值覆盖
        self.assertEqual(t.resolve("space", 12), 12)
        self.assertEqual(t.resolve("space", None, 0), 0)

    def test_roundtrip_json(self):
        u = build_from_description("登录页")
        again = UIR.from_json(u.to_json())
        self.assertEqual(again.to_dict(), u.to_dict())

    def test_schema_roles_match_model(self):
        schema = json.loads((ROOT / "core/uir/schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(schema["definitions"]["node"]["properties"]["role"]["enum"]), set(ROLES))


class WebZeroRegression(unittest.TestCase):
    def test_passthrough_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as d:
            ws, out = Path(d) / "ws", Path(d) / "out"
            ws.mkdir()
            doc = ("<!DOCTYPE html><html lang=\"zh\"><head><title>T</title><style data-cssom>.a{color:#ff0000}</style>"
                   "</head><body><div class=\"a\" id=\"x\">hi<img src=\"static/img/a.png\"></div><p>世界</p></body></html>")
            (ws / "index.html").write_text(doc, encoding="utf-8")
            u = from_clone_workspace(ws)
            self.assertEqual(u.meta["source_kind"], "web_dom")
            self.assertEqual(u.validate(), [])
            r = HtmlCssTarget().render(u, out)
            self.assertTrue(r.meta.get("passthrough"))
            self.assertEqual((out / "index.html").read_bytes(), (ws / "index.html").read_bytes())
            self.assertEqual(len(u.root.children), 2)
            self.assertEqual(u.root.children[0].raw["tag"], "div")


class HtmlTargetGuards(unittest.TestCase):
    def test_no_remote_refs_and_no_scripts(self):
        u = UIR(root=Node(role="box", children=[
            Node(role="image", content={"src": "https://evil.example/x.png"}),
            Node(role="box", raw={"tag": "script", "attrs": {"src": "//cdn/x.js", "onload": "alert(1)"}, "inner_html": "x"}),
            Node(role="link", content={"href": "#", "text": "go"}, intents=[{"on": "click", "do": "navigate"}]),
        ])).normalize()
        page, css = render_html(u)
        self.assertNotIn("https://evil.example", page)
        self.assertNotIn("<script src", page)
        self.assertNotIn("onload", page)
        self.assertIn("uir-ph", page)          # 外链图退化为占位
        self.assertIn("data-intent", page)     # 意图落到运行时
        self.assertNotIn("http", css)

    def test_tokens_to_css_vars_and_layout(self):
        u = build_from_description("仪表盘 后台")
        page, css = render_html(u)
        self.assertIn(":root{", css)
        self.assertIn("display:grid", css)
        self.assertIn("display:flex", css)
        self.assertIn('rel="stylesheet" href="static/css/app.css"', page)


class DescriptionGuards(unittest.TestCase):
    def test_archetypes(self):
        cases = {"登录页有验证码": "login", "产品官网落地页 hero": "landing", "后台仪表盘 KPI": "dashboard",
                 "电商商品列表": "list", "博客文章正文": "article", "随便一个页面": "landing"}
        for desc, arch in cases.items():
            self.assertEqual(detect_archetype(desc), arch, desc)
            u = build_from_description(desc)
            self.assertEqual(u.validate(), [], desc)
            self.assertGreaterEqual(u.root.count(), 10, desc)

    def test_slots_from_description(self):
        u = build_from_description("蓝色登录页, 品牌叫 云帆, 标题「欢迎回来」, 有短信验证码")
        texts = [n.content.get("text") for n in u.root.walk() if n.content.get("text")]
        self.assertIn("欢迎回来", texts)
        self.assertTrue(any(i.get("do") == "countdown" for n in u.root.walk() for i in n.intents))
        self.assertEqual(u.tokens.color["c2"], "#2563eb")

    def test_no_site_specific_branches(self):
        src = (ROOT / "core/uir/adapters/description.py").read_text(encoding="utf-8")
        for banned in ("jd.com", "taobao", "baidu", "apple.com", "bilibili"):
            self.assertNotIn(banned, src)


class VisionTier0Guards(unittest.TestCase):
    def _img(self):
        im = Image.new("RGB", (640, 480), "#ffffff")
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, 640, 60], fill="#1f2937")
        d.rectangle([40, 100, 300, 300], fill="#2563eb")
        d.rectangle([340, 100, 600, 300], fill="#f3f4f6")
        for y in range(340, 440, 16):
            d.rectangle([40, y, 600, y + 6], fill="#111827")
        return im

    def test_deterministic_and_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.png"
            self._img().save(p)
            u1, u2 = segment(p, asset_dir=Path(d) / "static" / "img"), segment(p, asset_dir=Path(d) / "static" / "img")
            self.assertEqual(u1.to_dict(), u2.to_dict())
            self.assertLessEqual(u1.meta.get("picture_area", 0), 0.40)   # 图片回填面积封顶, 整页不退化为一张图
            self.assertLessEqual(u1.root.count(), settings.SEG_MAX_NODES + 1)
            self.assertEqual(u1.validate(), [])
            self.assertEqual(u1.meta["viewport"], {"w": 640, "h": 480})
            self.assertGreaterEqual(u1.root.count(), 4)   # 至少切出顶栏/两块/文本带
            page, css = render_html(u1)
            self.assertIn("position:absolute", css)


class RegistryAndDispatch(unittest.TestCase):
    def test_registry(self):
        self.assertIn("screenshot", list_inputs())
        self.assertIn("description", list_inputs())
        self.assertIn("web_dom", list_inputs())
        self.assertIn("html_css", list_outputs())
        self.assertTrue(get_output("html_css").shootable)

    def test_dispatch_prefix(self):
        self.assertTrue(is_generate_task({"target_url": settings.TARGET_URL_PREFIX + "description:x"}))
        self.assertFalse(is_generate_task({"target_url": "https://example.com/"}))
        self.assertFalse(is_generate_task({}))


if __name__ == "__main__":
    unittest.main()
