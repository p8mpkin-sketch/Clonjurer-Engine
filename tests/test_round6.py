"""第 6 轮(通宵)优化守卫: 字体按需下载计划。"""
import unittest

from core.clone.support import fontface_plan, fontface_swap_for_remote, page_codepoints

BASE = "https://s.com/css/x.css"
CSS = """@font-face{font-family:A;src:url(a.eot);src:url(a.eot?#iefix) format("embedded-opentype"),
url(a.woff2) format("woff2"),url(a.woff) format("woff"),url("a.ttf") format("truetype")}
@font-face{font-family:B;src:local("B"),url(/f/b-cyr.woff2) format("woff2");unicode-range:U+0400-045F,U+0490-0491}
@font-face{font-family:B;src:url(/f/b-lat.woff2) format("woff2");unicode-range:U+0000-00FF,U+0131}
@font-face{font-family:B;src:url(/f/b-cjk.woff2) format("woff2");unicode-range:U+4E??}
@font-face{font-family:I;src:url(i.woff2);unicode-range:U+E000-E0FF}
@font-face{font-family:C;src:url(c.woff2) format("woff2") tech(variations), url(c.ttf)}
@font-face{font-family:D;src:url(data:font/woff2;base64,AAAA) format("woff2"),url(d.woff)}
@font-face{font-family:E;src:url(e.svg#e) format("svg"),url(e.woff)}"""


class FontPlan(unittest.TestCase):
    def test_primary_format_kept_fallbacks_skipped(self):
        need, skip, fb = fontface_plan(CSS, BASE, None)
        self.assertIn("https://s.com/css/a.woff2", need)
        for u in ("a.woff", "a.ttf", "a.eot", "c.ttf", "d.woff"):
            self.assertIn("https://s.com/css/" + u, skip, u)
        self.assertIn("https://s.com/css/c.woff2", need)          # tech() 不影响首选
        self.assertIn("https://s.com/css/e.woff", need)           # svg 字体不受支持, 往后取
        self.assertEqual(fb["https://s.com/css/a.woff"], "https://s.com/css/a.woff2")
        # cps=None: 不按字符剔除
        self.assertTrue({"https://s.com/f/b-cyr.woff2", "https://s.com/f/b-cjk.woff2"} <= need)

    def test_unicode_range_by_page_chars(self):
        cps = page_codepoints(["<p>Hello &#x4e00;</p>"])
        need, skip, _ = fontface_plan(CSS, BASE, cps)
        self.assertIn("https://s.com/f/b-lat.woff2", need)
        self.assertIn("https://s.com/f/b-cjk.woff2", need)        # 实体反转义后的字符也算
        self.assertIn("https://s.com/f/b-cyr.woff2", skip)
        self.assertIn("https://s.com/css/i.woff2", need)          # 私用区(图标字体)不按字符剔除

    def test_css_escape_counts_as_page_char(self):
        cps = page_codepoints(['.x:before{content:"\\0416"}'])
        need, _, _ = fontface_plan(CSS, BASE, cps)
        self.assertIn("https://s.com/f/b-cyr.woff2", need)

    def test_unparseable_range_is_conservative(self):
        css = "@font-face{font-family:Z;src:url(z.woff2);unicode-range:U+ZZZZ}"
        need, skip, _ = fontface_plan(css, BASE, [65])
        self.assertIn("https://s.com/css/z.woff2", need)
        self.assertFalse(skip)

    def test_swap_ignores_remote_fallbacks_when_primary_local(self):
        css = ('@font-face{font-family:A;src:url(/static/fonts/a.woff2) format("woff2"),'
               'url(https://cdn.x/a.ttf) format("truetype")}')
        self.assertEqual(fontface_swap_for_remote(css), css)
        css2 = ('@font-face{font-family:A;src:url(https://cdn.x/a.woff2) format("woff2"),'
                'url(/static/fonts/a.ttf) format("truetype")}')
        self.assertIn("font-display:swap", fontface_swap_for_remote(css2))


if __name__ == "__main__":
    unittest.main()
