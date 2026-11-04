"""多视口适配护栏(2026-11-04 平板/手机视口归因后的整类修复):

- 引擎按采集视口算出的固化(字体烘焙/WAAPI 终态/校准)只在采集视口所在断点区间生效,
  区间外回落站点自己的媒体查询(cn.vuejs 手机视口 45 → 100);
- 原站自带 viewport meta 时不注入移动兜底 CSS(img height:auto!important 拉长定高图、
  body overflow 让整页高度塌成一屏);
- 轮播脚本写死的容器宽(slide width:1920px / 轨道 N×1920 / translate -1920px)改成相对值;
- 脚本按视口写的内联样式: 按比例的改 vw, 离散的移入分视口样式表。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import cloner_engine as ce  # noqa: E402


class BandFloor(unittest.TestCase):
    def test_breakpoint_parsing(self):
        css = ("@media (max-width: 734px){a{}}@media screen and (min-width:1069px){b{}}"
               "@media (min-width: 120em){c{}}@media (width >= 1440px){d{}}"
               "@media (768px < width){e{}}")
        self.assertEqual(ce._media_breakpoints(css), {735.0, 1069.0, 1920.0, 1440.0, 769.0})

    def test_floor_is_largest_breakpoint_within_capture_width(self):
        css = "@media (max-width:1068px){x{}}@media (min-width:2000px){y{}}"
        self.assertEqual(ce.capture_band_floor([css], 1920), 1069)
        self.assertEqual(ce.capture_band_floor(["@media (max-width:600px){x{}}"], 390), 0)
        self.assertEqual(ce.capture_band_floor(["p{color:red}"], 1920), 0)

    def test_link_media_counts(self):
        html = '<link rel="stylesheet" media="(min-width: 1024px)" href="/d.css">'
        self.assertEqual(ce.capture_band_floor([html], 1920), 1024)

    def test_wrap_and_scope(self):
        self.assertEqual(ce.band_wrap("a{b:c}", 0), "a{b:c}")
        self.assertEqual(ce.band_wrap("a{b:c}", 1069), "@media (min-width: 1069px){\na{b:c}\n}")
        html = '<style data-clone-bake="f">@media all{\n[data-cb~="f1-0"]{x:y}\n}</style>'
        self.assertIn('@media (min-width: 1069px){', ce.scope_bake_css(html, 1069))
        self.assertEqual(ce.scope_bake_css(html, 0), html)

    def test_viewport_bands(self):
        html = ('<style data-clone-vp="">@media all/*cv-d*/{a{}}\n'
                '@media (min-width: 99999px)/*cv-t*/{b{}}\n@media (min-width: 99999px)/*cv-m*/{c{}}</style>')
        out = ce.scope_viewport_css(html, 1069, 735)
        self.assertIn("@media (min-width: 1069px)/*cv-d*/{", out)
        self.assertIn("@media (min-width: 735px) and (max-width: 1068.98px)/*cv-t*/{", out)
        self.assertIn("@media (max-width: 734.98px)/*cv-m*/{", out)
        fallback = ce.scope_viewport_css(html, 0, 0)
        self.assertIn("@media (min-width: 1024px)/*cv-d*/{", fallback)
        self.assertIn("@media (max-width: 599.98px)/*cv-m*/{", fallback)


class ResponsiveInjection(unittest.TestCase):
    def test_site_with_viewport_meta_untouched(self):
        html = ('<html><head><meta name="viewport" content="width=device-width">'
                '</head><body></body></html>')
        self.assertEqual(ce._inject_responsive(html), html)

    def test_legacy_site_gets_meta_and_safe_fallback(self):
        out = ce._inject_responsive("<html><head></head><body></body></html>")
        self.assertIn('name="viewport"', out)
        self.assertIn('data-clone-preview="responsive"', out)
        self.assertNotIn("height: auto !important", out)
        self.assertNotIn("html, body { overflow-x", out)


def _chromium_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width">
<style>
body{margin:0} h1{font-size:20px} @media (min-width: 1000px){ h1{font-size:60px} }
.wrap{width:100%;overflow:hidden} .track{display:flex}
.slide{flex-shrink:0;height:50px}
</style></head><body>
<h1 id="t">标题</h1><p id="p">正文</p>
<div class="wrap" id="wrap"><div class="track" id="track" style="transform: translate3d(-WIDTHpx, 0px, 0px)">
<div class="slide" style="width: WIDTHpx">1</div><div class="slide" style="width: WIDTHpx">2</div>
<div class="slide" style="width: WIDTHpx">3</div></div></div>
<div id="side"></div><div id="ratio"></div>
<script>
function lay(){ var w = innerWidth;
  document.getElementById('side').style.float = w >= 1000 ? 'right' : '';
  document.getElementById('ratio').style.height = (w * 0.25) + 'px'; }
lay(); addEventListener('resize', lay);
</script></body></html>"""


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class LiveViewportScope(unittest.TestCase):
    def _page(self, p, width):
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": width, "height": 800})
        page.set_content(PAGE.replace("WIDTH", str(width)))
        return b, page

    def test_font_bake_scoped_and_fluid_slides(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b, page = self._page(p, 1440)
            try:
                self.assertGreater(page.evaluate(ce.FONT_BAKE_JS), 0)
                inline = page.evaluate("document.getElementById('t').getAttribute('style')")
                self.assertFalse(inline and "font-size" in inline, inline)
                self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('t')).fontSize"), "60px")
                fl = page.evaluate(ce.INLINE_FLUID_JS)
                self.assertEqual(fl["shift"], 1)
                rect = page.evaluate("document.querySelector('.slide').getBoundingClientRect().width")
                self.assertAlmostEqual(rect, 1440, delta=1)
                tx = page.evaluate("document.getElementById('track').getBoundingClientRect().left")
                self.assertAlmostEqual(tx, -1440, delta=1)
                html = page.content()
            finally:
                b.close()
            # 落盘作用域(断点 1000) 后在手机宽打开: 回落站点样式 20px, 轮播随容器
            html = ce.scope_bake_css(html, ce.capture_band_floor([html], 1440))
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 390, "height": 800})
                page.set_content(html.replace("<script>", "<script type='x'>"))
                self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('t')).fontSize"), "20px")
                self.assertAlmostEqual(page.evaluate(
                    "document.querySelector('.slide').getBoundingClientRect().width"), 390, delta=1)
                self.assertAlmostEqual(page.evaluate(
                    "document.getElementById('track').getBoundingClientRect().left"), -390, delta=1)
            finally:
                b.close()

    def test_viewport_dependent_inline_styles(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b, page = self._page(p, 1440)
            try:
                page.evaluate(ce.CV_RECORD_JS, "d")
                for tag, w in (("t", 768), ("m", 390)):
                    page.set_viewport_size({"width": w, "height": 800})
                    page.evaluate(ce.CV_QUIET_JS, 100)
                    page.evaluate(ce.CV_RECORD_JS, tag)
                page.set_viewport_size({"width": 1440, "height": 800})
                page.evaluate(ce.CV_QUIET_JS, 100)
                page.evaluate(ce.CV_RECORD_JS, "b")
                out = page.evaluate(ce.CV_EMIT_JS)
                self.assertEqual(out["vw"], 1)       # height = 25vw
                self.assertGreaterEqual(out["banded"], 1)   # float 只在宽屏
                self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('side')).float"), "right")
                self.assertEqual(page.evaluate("document.getElementById('ratio').style.height"), "25vw")
                html = page.content()
            finally:
                b.close()
            html = ce.scope_viewport_css(html, 1000, 0)
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 390, "height": 800})
                page.set_content(html.replace("<script>", "<script type='x'>"))
                self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('side')).float"), "none")
                page.set_viewport_size({"width": 1440, "height": 800})
                self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('side')).float"), "right")
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main()


class CalibInjection(unittest.TestCase):
    def test_put_calib_band_wrapped(self):
        eng = ce.ClonerEngine.__new__(ce.ClonerEngine)
        eng._band_floor = 1069
        out = eng._put_calib("<html><head></head><body></body></html>", "a{b:c}")
        self.assertIn("<style data-clone-calib>@media (min-width: 1069px){\na{b:c}\n}</style></head>", out)
        eng._band_floor = 0
        self.assertIn("<style data-clone-calib>a{b:c}</style>", eng._put_calib("<head></head>", "a{b:c}"))
