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
