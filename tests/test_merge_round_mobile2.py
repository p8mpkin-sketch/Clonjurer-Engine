"""合并线第二批合成用例的通用修法守卫: viewport meta 上移 / 根元素 class 按 UA 拆分 / 触屏类不被运行时抹掉 / 窄屏页全新上下文。"""
import unittest

import config
from core.clone import js
from core.clone.support import _hoist_viewport_meta, _inject_responsive


class HoistViewportMeta(unittest.TestCase):
    def test_script_written_meta_moves_before_first_script(self):
        html = ('<html><head><meta charset="utf-8"><title>x</title><script>var a=1</script>'
                '<style>b{}</style><meta name="viewport" content="width=device-width,initial-scale=1"></head><body></body></html>')
        out = _hoist_viewport_meta(html)
        self.assertLess(out.index('name="viewport"'), out.index("<script>"))
        self.assertEqual(out.count('name="viewport"'), 1)
        self.assertLess(out.index('charset'), out.index('name="viewport"'))

    def test_last_meta_wins_and_duplicates_dropped(self):
        html = ('<head><meta name="viewport" content="a"><script></script>'
                '<meta name="viewport" content="b"></head>')
        out = _hoist_viewport_meta(html)
        self.assertEqual(out.count('name="viewport"'), 1)
        self.assertIn('content="b"', out)
        self.assertLess(out.index('content="b"'), out.index("<script>"))

    def test_untouched_when_meta_precedes_scripts_or_no_script(self):
        a = '<head><meta name="viewport" content="a"><script></script></head>'
        b = '<head><title>t</title><meta name="viewport" content="a"></head>'
        self.assertEqual(_hoist_viewport_meta(a), a)
        self.assertEqual(_hoist_viewport_meta(b), b)
        self.assertEqual(_hoist_viewport_meta("<p>no head</p>"), "<p>no head</p>")

    def test_inject_responsive_applies_hoist_by_default(self):
        html = '<head><script></script><meta name="viewport" content="x"></head>'
        self.assertLess(_inject_responsive(html).index("viewport"), _inject_responsive(html).index("<script>"))


class RootClassUaSplit(unittest.TestCase):
    def test_flags_and_js_hooks(self):
        self.assertTrue(config.ROOT_CLS_UA_SPLIT and config.VIEWPORT_META_HOIST and config.NARROW_FRESH_CTX)
        self.assertIn("const ROOTUA = 1;", js.CV_EMIT_JS)
        self.assertIn("alt.mt = L[1]", js.CV_EMIT_JS)
        # 运行时: 触屏设备非手机段上, 站点脚本自己加上的触屏类(只出现在 mt 变体里)不被抹掉
        self.assertIn("function keepTouch(", js.CV_CLS_RUNTIME)
        self.assertIn("keepTouch", js.CV_EMIT_JS)


class NarrowFreshContext(unittest.TestCase):
    def test_desktop_narrow_page_uses_fresh_context(self):
        from core.clone.capture import CaptureMixin

        class Ctx:
            def __init__(self, browser): self.browser, self.pages = browser, 0
            def new_page(self): self.pages += 1; return ("page", self)

        class Browser:
            def __init__(self): self.made = []
            def new_context(self, **kw): c = Ctx(self); self.made.append((c, kw)); return c

        class Page:
            def __init__(self): self.context = Ctx(Browser())
            def evaluate(self, s): return "UA/1"

        eng = CaptureMixin.__new__(CaptureMixin)
        eng._cv_extra_ctx = []
        pg = Page()
        _, ctx = eng._narrow_desktop_page(pg, 768, 900)
        self.assertIsNot(ctx, pg.context)
        self.assertEqual(pg.context.browser.made[0][1]["user_agent"], "UA/1")
        self.assertEqual(eng._cv_extra_ctx, [ctx])


if __name__ == "__main__":
    unittest.main()


class SameDocument(unittest.TestCase):
    def test_redirects_to_other_document(self):
        from core.clone.support import _same_document
        self.assertTrue(_same_document("https://www.a.com/", "https://www.a.com"))
        self.assertTrue(_same_document("https://www.a.com/x/?q=1#h", "http://WWW.a.com/x"))
        self.assertFalse(_same_document("https://m.a.com/", "https://www.a.com/"))
        self.assertFalse(_same_document("https://www.a.com/m/x", "https://www.a.com/x"))
        self.assertTrue(_same_document("about:blank", "https://www.a.com/"))   # 未提交导航不判


class VariantDomSwap(unittest.TestCase):
    """备选子树不改变当前段的兄弟序: :nth-child 奇偶样式不错位, 换段时换入换出。"""

    def test_nth_child_kept(self):
        from tests._env import chromium_available
        if not chromium_available():
            self.skipTest("需要 Playwright Chromium")
        from playwright.sync_api import sync_playwright
        from core.clone.support import scope_viewport_css
        page_html = ('<!doctype html><html><head><meta name="viewport" content="width=device-width">'
                     '<style>body{margin:0}.s{height:40px}.s:nth-child(2n){background:rgb(1,2,3)}</style></head><body>'
                     '<div class="s" id="a">a</div><div class="s" id="b">b</div><div class="s" id="c">c</div></body></html>')
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                pg = b.new_page(viewport={"width": 1200, "height": 600})
                pg.set_content(page_html)
                n = pg.evaluate(js.ALT_INJECT_JS, {"items": [{"path": "div:1", "own": "d t mx",
                                                             "alts": [{"html": '<div class="page">app</div>', "bands": "mt"}]}],
                                                  "css": ""})
                self.assertEqual(n, 1)
                # 采集活页里运行时不动 DOM(序列化要带上备选子树)
                self.assertEqual(pg.evaluate("document.querySelectorAll('[data-cv-vp]').length"), 2)
                html = scope_viewport_css("<!doctype html>" + pg.evaluate("document.documentElement.outerHTML"), 1024, 768)
            finally:
                b.close()
            b = p.chromium.launch()
            try:
                bg = "() => getComputedStyle(document.getElementById('b')).backgroundColor"
                d = b.new_page(viewport={"width": 1200, "height": 600})
                d.set_content(html)
                self.assertEqual(d.evaluate(bg), "rgb(1, 2, 3)")
                self.assertEqual(d.evaluate("document.querySelectorAll('.page').length"), 0)
                ctx = b.new_context(viewport={"width": 390, "height": 700}, has_touch=True, is_mobile=True)
                m = ctx.new_page()
                m.set_content(html)
                self.assertEqual(m.evaluate("document.querySelectorAll('.page').length"), 1)
                self.assertIsNone(m.evaluate("document.getElementById('a')"))
                m.set_viewport_size({"width": 1200, "height": 700})
                m.wait_for_timeout(100)
                self.assertTrue(m.evaluate("!!document.getElementById('a') && document.getElementById('a').isConnected"))
                self.assertEqual(m.evaluate("document.querySelectorAll('.page').length"), 0)
            finally:
                b.close()
