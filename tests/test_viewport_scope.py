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
        bare = "<html><head></head><body></body></html>"
        self.assertEqual(ce._inject_responsive(bare), bare)   # 默认保真: 原站没 meta 克隆也没有
        out = ce._inject_responsive(bare, enhance=True)
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


CLAMP_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width"></head><body>
<h1 id="h" style="font-size:72px">Title</h1>
<script>function f(){document.getElementById('h').style.fontSize = Math.min(72, innerWidth * 0.08) + 'px';}
addEventListener('resize', f); f();</script></body></html>"""


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class PiecewiseFluid(unittest.TestCase):
    """脚本按视口宽算、桌面被上限夹住的字号: 平板/手机两段同比 → 写 vw, 别的手机宽下跟着变; 桌面段仍是 px。"""

    def test_tablet_phone_proportional_to_vw(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 1440, "height": 800})
                page.set_content(CLAMP_PAGE)
                page.evaluate(ce.CV_RECORD_JS, "d")
                for tag, w in (("t", 768), ("m", 390)):
                    page.set_viewport_size({"width": w, "height": 800})
                    page.evaluate(ce.CV_QUIET_JS, 100)
                    page.evaluate(ce.CV_RECORD_JS, tag)
                page.set_viewport_size({"width": 1440, "height": 800})
                page.evaluate(ce.CV_QUIET_JS, 100)
                page.evaluate(ce.CV_RECORD_JS, "b")
                out = page.evaluate(ce.CV_EMIT_JS)
                self.assertEqual(out["vw"], 1)
                css = page.evaluate("document.querySelector('style[data-clone-vp]').textContent")
                self.assertIn("font-size:8.0000vw", css)
                self.assertIn("font-size:72px", css)
                html = page.content()
            finally:
                b.close()
            html = ce.scope_viewport_css(html, 1000, 0)
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 420, "height": 912})
                page.set_content(html.replace("<script>", "<script type='x'>"))
                self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('h')).fontSize"), "33.6px")
                page.set_viewport_size({"width": 1440, "height": 800})
                self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('h')).fontSize"), "72px")
            finally:
                b.close()


CLS_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width">
<style>.big{font-size:40px}.small{font-size:18px}</style></head><body>
<h2 id="h" class="big">Title</h2>
<script>function f(){document.getElementById('h').className = innerWidth < 600 ? 'small' : 'big';}
addEventListener('resize', f); f();</script></body></html>"""


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class ViewportClassSwap(unittest.TestCase):
    """脚本按视口换 class(useMediaQuery 类): 克隆里没有脚本再换, 由分视口 class 运行时接管。"""

    def test_class_follows_band_after_scope(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 1440, "height": 800})
                page.set_content(CLS_PAGE)
                page.evaluate(ce.CV_RECORD_JS, "d")
                for tag, w in (("t", 768), ("m", 390)):
                    page.set_viewport_size({"width": w, "height": 800})
                    page.evaluate(ce.CV_QUIET_JS, 100)
                    page.evaluate(ce.CV_RECORD_JS, tag)
                page.set_viewport_size({"width": 1440, "height": 800})
                page.evaluate(ce.CV_QUIET_JS, 100)
                page.evaluate(ce.CV_RECORD_JS, "b")
                loads = {}
                for tag, w in (("t", 768), ("m", 390)):
                    p2 = b.new_page(viewport={"width": w, "height": 800})
                    p2.set_content(CLS_PAGE)
                    loads[tag] = p2.evaluate(ce.CV_PATHS_JS)
                    p2.close()
                out = page.evaluate(ce.CV_EMIT_JS, loads)
                self.assertEqual(out.get("cls"), 1)
                self.assertEqual(page.evaluate("document.getElementById('h').className"), "big")
                html = page.content()
            finally:
                b.close()
            html = ce.scope_viewport_css(html, 1000, 0)
            # 去掉站点自己的换 class 脚本, 只留引擎注入的运行时
            html = html.replace("addEventListener('resize', f); f();", "")
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 390, "height": 800})
                page.set_content(html)
                self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('h')).fontSize"), "18px")
                page.set_viewport_size({"width": 1440, "height": 800})
                page.wait_for_timeout(100)
                self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('h')).fontSize"), "40px")
            finally:
                b.close()



XATTR_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width">
<style>body{margin:0}#box{width:50px}#box[data-pos="wide"]{height:300px}#box[data-pos="narrow"]{height:520px}</style></head><body>
<div id="box" data-pos="wide" data-track="a"></div>
<svg id="logo" viewBox="0 0 192 84" style="width:100px"></svg>
<script>function f(){var n = innerWidth < 600, b = document.getElementById('box');
b.setAttribute('data-pos', n ? 'narrow' : 'wide'); b.setAttribute('data-track', n ? 'm' : 'a');
document.getElementById('logo').setAttribute('viewBox', n ? '0 0 42 50' : '0 0 192 84');}
addEventListener('resize', f); f();</script></body></html>"""


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class ViewportAttrSwap(unittest.TestCase):
    """脚本按视口写属性选择器引用的 data-* 与 svg viewBox: 克隆按视口段换回; 样式表没引用的 data-* 不换。"""

    def test_attrs_follow_band_after_scope(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 1440, "height": 800})
                page.set_content(XATTR_PAGE)
                page.evaluate(ce.CV_RECORD_JS, "d")
                for tag, w in (("t", 768), ("m", 390)):
                    page.set_viewport_size({"width": w, "height": 800})
                    page.evaluate(ce.CV_QUIET_JS, 100)
                    page.evaluate(ce.CV_RECORD_JS, tag)
                page.set_viewport_size({"width": 1440, "height": 800})
                page.evaluate(ce.CV_QUIET_JS, 100)
                page.evaluate(ce.CV_RECORD_JS, "b")
                loads = {}
                for tag, w in (("t", 768), ("m", 390)):
                    p2 = b.new_page(viewport={"width": w, "height": 800})
                    p2.set_content(XATTR_PAGE)
                    loads[tag] = p2.evaluate(ce.CV_PATHS_JS)
                    p2.close()
                out = page.evaluate(ce.CV_EMIT_JS, loads)
                self.assertEqual(out.get("xattr"), 2)
                self.assertEqual(page.evaluate("document.getElementById('box').getAttribute('data-pos')"), "wide")
                html = page.content()
            finally:
                b.close()
            self.assertNotIn("data-track", html.split('data-cv-xa-d="', 1)[1].split('"', 1)[0])
            html = ce.scope_viewport_css(html, 1000, 0)
            html = html.replace("addEventListener('resize', f); f();", "")
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 390, "height": 800})
                page.set_content(html)
                self.assertEqual(page.evaluate("document.getElementById('box').getBoundingClientRect().height"), 520)
                self.assertEqual(page.evaluate("document.getElementById('logo').getAttribute('viewBox')"), "0 0 42 50")
                self.assertEqual(page.evaluate("document.getElementById('box').getAttribute('data-track')"), "a")
                page.set_viewport_size({"width": 1440, "height": 800})
                page.wait_for_timeout(100)
                self.assertEqual(page.evaluate("document.getElementById('box').getBoundingClientRect().height"), 300)
                self.assertEqual(page.evaluate("document.getElementById('logo').getAttribute('viewBox')"), "0 0 192 84")
            finally:
                b.close()


CANVAS_FREEZE_PAGE = """<style>img{max-width:100%;height:auto}.wf{width:100%;display:block}.wide{width:calc(100% + 40px);display:block}#box canvas{width:120px;height:40px}</style>
<div id="w" style="width:390px"><canvas id="a" class="wf" width="600" height="300"></canvas></div>
<div id="box"><canvas id="b" width="60" height="20"></canvas></div>
<div style="width:300px"><canvas id="c" class="wide" width="680" height="200"></canvas></div>
<div id="tab" style="display:none"><canvas id="d" width="160" height="160"></canvas></div><style>#tab canvas{width:134px;height:134px}</style>
<script>for (const id of ['a', 'b', 'c', 'd']) { const x = document.getElementById(id).getContext('2d'); x.fillStyle = 'red'; x.fillRect(0, 0, 600, 300); }</script>"""


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class CanvasFreezeResponsive(unittest.TestCase):
    """canvas 固化的 img 按宽高比随容器伸缩; 只靠 canvas 标签选择器定尺寸的回落成固定 px。"""

    def test_frozen_canvas_keeps_ratio(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page()
                page.set_content(CANVAS_FREEZE_PAGE)
                alt = page.evaluate(ce.ALT_EXTRACT_JS, ["div:1"])
                self.assertIn('class="wf"', alt["div:1"][0])
                self.assertNotIn("390px", alt["div:1"][0].split("data-canvas-frozen", 1)[1])
                page.evaluate(ce.DEEP_FREEZE_JS)
                size = "id => { const r = document.getElementById(id).getBoundingClientRect(); return [r.width, r.height]; }"
                self.assertEqual(page.evaluate(size, "a"), [390, 195])
                self.assertEqual(page.evaluate(size, "b"), [120, 40])
                self.assertEqual(page.evaluate(size, "c"), [340, 100])   # 站点 img{max-width:100%} 不截短
                self.assertNotIn("340px", page.evaluate("document.getElementById('c').getAttribute('style')"))
                page.evaluate("document.getElementById('tab').style.display = 'block'")   # 隐藏面板里的: 按样式表 px 定尺寸
                self.assertEqual(page.evaluate(size, "d"), [134, 134])
                page.evaluate("document.getElementById('w').style.width = '420px'")
                self.assertEqual(page.evaluate(size, "a"), [420, 210])
            finally:
                b.close()


FLUID_SELF_SIZED = """<div style="display:inline-block;padding:10px"><div id="fixed" style="width:160px;height:20px;padding:12px;box-sizing:border-box">
<i style="display:block;width:134px;height:10px"></i></div></div>
<div style="width:400px"><div id="full" style="width:400px;height:10px"></div></div>"""


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class InlineFluidSelfSized(unittest.TestCase):
    """父容器宽由本元素撑出时不改 100%(循环百分比); 父容器宽独立时照改。"""

    def test_skip_when_parent_shrink_wraps(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page()
                page.set_content(FLUID_SELF_SIZED)
                out = page.evaluate(ce.INLINE_FLUID_JS)
                self.assertEqual(out["width"], 1)
                self.assertIn("160px", page.evaluate("document.getElementById('fixed').getAttribute('style')"))
                self.assertIn("100%", page.evaluate("document.getElementById('full').getAttribute('style')"))
            finally:
                b.close()


INTERSTITIAL_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width"></head><body style="margin:0">
<div id="hdr" style="position:fixed;top:0;left:0;right:0;height:60px;z-index:9999"><iframe src="http://ads.invalid/x" width="300" height="50"></iframe></div>
<div style="height:2000px">正文</div>
<div id="pop" style="position:fixed;top:0;left:0;width:100%;height:110%;z-index:2147483647;background:rgba(0,0,0,.5)">
<iframe src="http://ads.invalid/s?wid=336&hei=597" width="336" height="597"></iframe></div>
<div id="own" style="position:fixed;inset:0;z-index:5000"><iframe src="/local" width="300" height="300"></iframe></div>
</body></html>"""


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class InterstitialHide(unittest.TestCase):
    """插屏广告层(fixed 全屏 + 跨源 iframe)评分截图时隐藏, 截完还原; 页头小广告条与同源全屏层不动。"""

    def test_hide_and_restore(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 420, "height": 912})
                page.route("**/*", lambda r: r.abort() if "ads.invalid" in r.request.url or "/local" in r.request.url else r.continue_())
                page.set_content(INTERSTITIAL_PAGE)
                self.assertEqual(page.evaluate(ce.INTERSTITIAL_HIDE_JS), 1)
                disp = "id => getComputedStyle(document.getElementById(id)).display"
                self.assertEqual(page.evaluate(disp, "pop"), "none")
                self.assertEqual(page.evaluate(disp, "hdr"), "block")
                self.assertEqual(page.evaluate(disp, "own"), "block")
                page.evaluate(ce.INTERSTITIAL_RESTORE_JS)
                self.assertEqual(page.evaluate(disp, "pop"), "block")
                self.assertNotIn("display", page.evaluate("document.getElementById('pop').getAttribute('style')"))
                self.assertIsNone(page.evaluate("document.getElementById('pop').getAttribute('data-clone-ihide')"))
            finally:
                b.close()

    def test_marketing_modal_hidden_drawer_and_cookie_bar_kept(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 420, "height": 912})
                page.set_content(MODAL_PAGE)
                self.assertEqual(page.evaluate(ce.INTERSTITIAL_HIDE_JS), 2)
                disp = "id => getComputedStyle(document.getElementById(id)).display"
                self.assertEqual(page.evaluate(disp, "bd"), "none")
                self.assertEqual(page.evaluate(disp, "dlg"), "none")
                self.assertEqual(page.evaluate(disp, "cookie"), "block")
                self.assertEqual(page.evaluate(disp, "hero"), "block")
                page.evaluate(ce.INTERSTITIAL_RESTORE_JS)
                self.assertEqual(page.evaluate(disp, "bd"), "block")
                self.assertEqual(page.evaluate(disp, "dlg"), "block")
            finally:
                b.close()


MODAL_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width"></head><body style="margin:0">
<div id="hero" style="position:relative;height:900px;z-index:1">首屏大图</div>
<div id="cookie" style="position:fixed;left:0;right:0;bottom:0;height:120px;z-index:999"><input type="checkbox">Cookie <button aria-label="Close">×</button></div>
<div id="bd" style="position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.6)"></div>
<div id="dlg" role="dialog" aria-modal="true" style="position:fixed;top:20%;left:10%;width:80%;height:50%;z-index:1001;background:#fff">
<input type="email" placeholder="Email"><button aria-label="Close">×</button></div>
</body></html>"""


FLEXIBLE_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width"></head><body style="margin:0">
<div id="col" style="max-width:10rem">内容</div>
<script>function f(){document.documentElement.style.fontSize = (innerWidth / 10) + 'px';} addEventListener('resize', f); f();</script>
</body></html>"""


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class RootFontFluid(unittest.TestCase):
    """lib-flexible 类 rem 基准(内联 font-size = 宽/10): 窄屏采集时改写成 vw, 克隆在别的手机宽下跟着变。"""

    def test_rem_base_to_vw(self):
        from playwright.sync_api import sync_playwright
        eng = ce.ClonerEngine.__new__(ce.ClonerEngine)
        eng.cfg, logs = {}, []
        eng.log = lambda level, msg: logs.append(msg)
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                ctx = b.new_context(viewport={"width": 390, "height": 800})
                ctx.route("http://flex.test/", lambda r: r.fulfill(body=FLEXIBLE_PAGE, content_type="text/html"))
                page = ctx.new_page()
                page.goto("http://flex.test/")
                eng._probe_root_font(page)
                self.assertAlmostEqual(eng._root_fluid.get("html", 0), 10.0, places=3)
                self.assertNotIn("body", eng._root_fluid)
                self.assertEqual(page.viewport_size["width"], 390)
                self.assertEqual(len(ctx.pages), 1)   # 对照页已关, 主页没被缩放/导航
                page.evaluate(ce.ROOT_FONT_APPLY_JS, eng._root_fluid)
                html = page.content().replace("addEventListener('resize', f); f();", "")
                page2 = b.new_page(viewport={"width": 420, "height": 800})
                page2.set_content(html)
                self.assertAlmostEqual(page2.evaluate("document.getElementById('col').getBoundingClientRect().width"), 420, delta=1)
                # 桌面宽采集不做
                eng2 = ce.ClonerEngine.__new__(ce.ClonerEngine)
                eng2.cfg, eng2.log = {}, (lambda *a: None)
                page.set_viewport_size({"width": 1440, "height": 800})
                eng2._probe_root_font(page)
                self.assertEqual(eng2._root_fluid, {})
            finally:
                b.close()


FLUID_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width">
<style>#css{font-size:10vw}#fixed{font-size:18px}</style></head><body style="margin:0">
<h1 id="css">CSS 随宽</h1><p id="fixed">定值</p>
<h2 id="js">脚本随宽</h2><div id="box" style="color:red">盒</div>
<script>function f(){document.getElementById('js').style.fontSize = (innerWidth * 0.1) + 'px';
document.getElementById('box').style.width = (innerWidth * 0.8) + 'px';}
addEventListener('resize', f); f();</script></body></html>"""


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class FluidSizeProbe(unittest.TestCase):
    """字号/内联 px 随视口宽同比(0.92 倍宽对照): 烘焙不钉字号, 内联 px 改 vw; 定值照旧烘焙。"""

    def test_fluid_elements_not_pinned(self):
        from playwright.sync_api import sync_playwright
        eng = ce.ClonerEngine.__new__(ce.ClonerEngine)
        eng.cfg, logs = {}, []
        eng.log = lambda level, msg: logs.append(msg)
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                ctx = b.new_context(viewport={"width": 390, "height": 800})
                ctx.route("http://fluid.test/", lambda r: r.fulfill(body=FLUID_PAGE, content_type="text/html"))
                page = ctx.new_page()
                page.goto("http://fluid.test/")
                eng._probe_root_font(page)
                self.assertEqual(eng._root_fluid, {})
                fl = eng._fluid
                self.assertEqual(sorted(fl["paths"]), ["h1:1", "h2:1"])
                self.assertEqual(fl["inline"], {"h2:1": {"font-size": "10.0000vw"}, "div:1": {"width": "80.0000vw"}})
                self.assertEqual(len(ctx.pages), 1)
                page.evaluate(ce.FLUID_APPLY_JS, fl)
                self.assertEqual(page.evaluate("document.getElementById('js').style.fontSize"), "10vw")
                self.assertGreater(page.evaluate(ce.FONT_BAKE_JS), 0)
                self.assertEqual(page.evaluate("document.querySelectorAll('[data-cv-fluidsz]').length"), 0)
                bake = page.evaluate("document.querySelector('style[data-clone-bake]').textContent")
                self.assertIn("font-size:18px", bake)        # 定值照烘
                self.assertNotIn("font-size:39px", bake)     # 随宽的不钉 390 宽的值
                html = page.content().replace("addEventListener('resize', f); f();", "")
                page2 = b.new_page(viewport={"width": 420, "height": 800})
                page2.set_content(ce.scope_bake_css(html, 320))
                self.assertEqual(page2.evaluate("getComputedStyle(document.getElementById('css')).fontSize"), "42px")
                self.assertEqual(page2.evaluate("getComputedStyle(document.getElementById('js')).fontSize"), "42px")
                self.assertAlmostEqual(page2.evaluate("document.getElementById('box').getBoundingClientRect().width"), 336, delta=1)
                self.assertEqual(page2.evaluate("getComputedStyle(document.getElementById('fixed')).fontSize"), "18px")
            finally:
                b.close()


class CalibInjection(unittest.TestCase):
    def test_put_calib_band_wrapped(self):
        eng = ce.ClonerEngine.__new__(ce.ClonerEngine)
        eng._band_floor = 1069
        out = eng._put_calib("<html><head></head><body></body></html>", "a{b:c}")
        self.assertIn("<style data-clone-calib>@media (min-width: 1069px){\na{b:c}\n}</style></head>", out)
        eng._band_floor = 0
        self.assertIn("<style data-clone-calib>a{b:c}</style>", eng._put_calib("<head></head>", "a{b:c}"))


LOAD_ONLY_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width">
<style>body{margin:0}.col{margin-top:16px}</style></head><body>
<div class="row"><div class="col" id="c">正文</div><div id="fade">渐显</div></div>
<script>
if (innerWidth >= 1000) document.getElementById('c').style.float = 'right';
document.getElementById('fade').style.opacity = innerWidth >= 1000 ? '1' : '0';
</script></body></html>"""


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class LoadTimeWidthStyles(unittest.TestCase):
    def test_reload_fills_load_time_geometry_only(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 1440, "height": 800})
                page.set_content(LOAD_ONLY_PAGE)
                page.evaluate(ce.CV_RECORD_JS, "d")
                for tag, w in (("t", 768), ("m", 390)):
                    page.set_viewport_size({"width": w, "height": 800})
                    page.evaluate(ce.CV_QUIET_JS, 100)
                    page.evaluate(ce.CV_RECORD_JS, tag)
                page.set_viewport_size({"width": 1440, "height": 800})
                page.evaluate(ce.CV_QUIET_JS, 100)
                page.evaluate(ce.CV_RECORD_JS, "b")
                loads = {}
                for tag, w in (("t", 768), ("m", 390)):
                    p2 = b.new_page(viewport={"width": w, "height": 800})
                    p2.set_content(LOAD_ONLY_PAGE)
                    loads[tag] = p2.evaluate(ce.CV_PATHS_JS)
                    p2.close()
                out = page.evaluate(ce.CV_EMIT_JS, loads)
                self.assertGreaterEqual(out["reload"], 2)          # float 的平板/手机取值
                self.assertEqual(page.evaluate("document.getElementById('c').style.float"), "")
                self.assertEqual(page.evaluate("document.getElementById('fade').style.opacity"), "1")  # 非几何属性不动
                self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('c')).float"), "right")
                html = page.content()
            finally:
                b.close()
            html = ce.scope_viewport_css(html, 1000, 0)
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 390, "height": 800})
                page.set_content(html.replace("<script>", "<script type='x'>"))
                self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('c')).float"), "none")
            finally:
                b.close()


VARIANT_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width">
<style>body{margin:0}</style></head><body><header>站头</header>
<main id="m"></main>
<script>
var m = document.getElementById('m');
var css = document.createElement('style');
if (innerWidth >= 1000) {
  m.innerHTML = '<div class="cards"><div class="card">甲</div><div class="card">乙</div><div class="card">丙</div></div>';
} else {
  css.textContent = '.slider{height:40px;background:rgb(1,2,3)}';
  m.innerHTML = '<section class="slider"><ul><li>甲</li><li>乙</li><li>丙</li></ul></section>';
}
document.head.appendChild(css);
</script></body></html>"""


class _FakeEngine:
    cfg = {}
    log = staticmethod(lambda *a: None)


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class DomVariants(unittest.TestCase):
    def test_narrow_structure_inserted_and_switched(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 1440, "height": 800})
                page.set_content(VARIANT_PAGE)
                eng = _FakeEngine()
                eng._cv_pages = []
                loads = {}
                for tag, w in (("t", 768), ("m", 390)):
                    p2 = b.new_page(viewport={"width": w, "height": 800})
                    p2.set_content(VARIANT_PAGE)
                    eng._cv_pages.append((tag, p2))
                    loads[tag] = p2.evaluate(ce.CV_PATHS_JS)
                n = ce.ClonerEngine._inject_dom_variants(eng, page, loads)
                self.assertEqual(n, 1)                       # 平板/手机同一版, 合并成一个
                self.assertFalse(page.evaluate("document.querySelector('.slider').checkVisibility()"))
                self.assertEqual(page.evaluate("document.querySelector('main').getAttribute('data-cv-vp')"), "d")
                html = page.content()
            finally:
                b.close()
            html = ce.scope_viewport_css(html, 1000, 0)
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 390, "height": 800})
                page.set_content(html.replace("<script>", "<script type='x'>"))
                self.assertFalse(page.evaluate("document.querySelector('.cards').checkVisibility()"))
                self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('.slider')).backgroundColor"),
                                 "rgb(1, 2, 3)")                # 窄屏运行时样式随变体带上
                page.set_viewport_size({"width": 1440, "height": 800})
                self.assertFalse(page.evaluate("document.querySelector('.slider').checkVisibility()"))
                self.assertTrue(page.evaluate("document.querySelector('.cards').checkVisibility()"))
            finally:
                b.close()


SVG_ATTR_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width">
<style>body{margin:0}</style></head><body>
<svg id="logo" viewBox="0 0 100 10"><rect width="100" height="10"/></svg>
<svg id="icon" width="24" height="24" viewBox="0 0 24 24"><rect width="24" height="24"/></svg>
<script>
function fit(){ var l=document.getElementById('logo'); var w=Math.min(innerWidth-48,600);
  l.setAttribute('width', String(w)); l.setAttribute('height', String(w/10)); }
fit(); addEventListener('resize', fit);
</script></body></html>"""


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class ViewportSizeAttrs(unittest.TestCase):
    def test_script_written_svg_size_attrs_follow_viewport(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 1440, "height": 800})
                page.set_content(SVG_ATTR_PAGE)
                page.evaluate(ce.CV_RECORD_JS, "d")
                for tag, w in (("t", 768), ("m", 390)):
                    page.set_viewport_size({"width": w, "height": 800})
                    page.evaluate(ce.CV_QUIET_JS, 100)
                    page.evaluate(ce.CV_RECORD_JS, tag)
                page.set_viewport_size({"width": 1440, "height": 800})
                page.evaluate(ce.CV_QUIET_JS, 100)
                page.evaluate(ce.CV_RECORD_JS, "b")
                out = page.evaluate(ce.CV_EMIT_JS)
                self.assertGreaterEqual(out["banded"], 1)
                self.assertEqual(page.evaluate("document.getElementById('logo').getBoundingClientRect().width"), 600)
                html = page.content()
            finally:
                b.close()
            html = ce.scope_viewport_css(html, 1000, 0)
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 390, "height": 800})
                page.set_content(html.replace("<script>", "<script type='x'>"))
                self.assertEqual(page.evaluate("document.getElementById('logo').getBoundingClientRect().width"), 342)
                self.assertEqual(page.evaluate("document.getElementById('icon').getBoundingClientRect().width"), 24)
            finally:
                b.close()


CANVAS_LOAD_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width">
<style>body{margin:0}canvas{display:block}</style></head><body>
<canvas id="cv"></canvas><div id="after">下文</div>
<script>
var c = document.getElementById('cv');
c.setAttribute('width', String(innerWidth)); c.setAttribute('height', innerWidth >= 1000 ? '300' : '520');
</script></body></html>"""


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class LoadTimeSizeAttrs(unittest.TestCase):
    def test_canvas_size_written_only_at_load(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 1440, "height": 800})
                page.set_content(CANVAS_LOAD_PAGE)
                for tag, w in (("d", 1440), ("t", 768), ("m", 390), ("b", 1440)):
                    page.set_viewport_size({"width": w, "height": 800})
                    page.evaluate(ce.CV_QUIET_JS, 100)
                    page.evaluate(ce.CV_RECORD_JS, tag)
                loads = {}
                for tag, w in (("t", 768), ("m", 390)):
                    p2 = b.new_page(viewport={"width": w, "height": 800})
                    p2.set_content(CANVAS_LOAD_PAGE)
                    loads[tag] = p2.evaluate(ce.CV_PATHS_JS)
                    p2.close()
                out = page.evaluate(ce.CV_EMIT_JS, loads)
                self.assertGreaterEqual(out["reload"], 1)
                self.assertEqual(page.evaluate("document.getElementById('cv').getBoundingClientRect().height"), 300)
                html = page.content()
            finally:
                b.close()
            html = ce.scope_viewport_css(html, 1000, 0)
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 390, "height": 800})
                page.set_content(html.replace("<script>", "<script type='x'>"))
                self.assertEqual(page.evaluate("document.getElementById('cv').getBoundingClientRect().height"), 520)
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main()
