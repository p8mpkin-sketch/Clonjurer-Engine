"""窄屏变体抽取里读回为空的 WebGL canvas 打 data-cv-shot 并回报矩形(引擎按窄屏页截图回填);
入场显形不给 SVG 内部图元写 transform:none(其 transform 是几何定位)。"""
import unittest

from core.clone.js import ALT_EXTRACT_JS, DEEP_FREEZE_JS


def _chromium_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


PAGE = """<!doctype html><html><body style="margin:0">
<div><canvas id="gl" width="60" height="60" style="width:60px;height:60px"></canvas>
<canvas id="c2" width="60" height="60" style="width:60px;height:60px"></canvas>
<canvas id="empty2d" width="60" height="60" style="width:60px;height:60px"></canvas></div>
<script>
const gl = document.getElementById('gl').getContext('webgl', {preserveDrawingBuffer: false});
if (gl) { gl.clearColor(1, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT); }
const g = document.getElementById('c2').getContext('2d'); g.fillStyle = '#0a0'; g.fillRect(0, 0, 60, 60);
document.getElementById('empty2d').getContext('2d');
</script></body></html>"""

SVG_PAGE = """<!doctype html><html><body style="margin:0">
<div data-animate="true" style="transform:translateY(40px)">x</div>
<svg width="80" height="20"><g class="dots" data-animate="true" transform="translate(35 2)"><rect width="10" height="10"/></g></svg>
</body></html>"""


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class VariantCanvasShot(unittest.TestCase):
    def test_blank_webgl_marked_2d_kept(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.set_content(PAGE)
            pg.wait_for_timeout(200)   # 让 WebGL 帧合成, 之后读回为空
            out = pg.evaluate(ALT_EXTRACT_JS, ["div:1"])
            b.close()
        html, *_rest = out["div:1"]
        shots = out["div:1"][5]
        has_gl = 'id="gl"' in html
        if has_gl:   # 无 GPU/软件 WebGL 时 getContext 可能失败, 只断言能断言的
            self.assertIn('data-cv-shot', html)
            self.assertEqual(len(shots), 1)
            sid, x, y, w, h = shots[0]
            self.assertEqual((round(w), round(h)), (60, 60))
        # 有内容的 2D canvas 照常 toDataURL 固化, 不打标记; 空的 2D canvas 是真透明, 不截图
        self.assertEqual(html.count('data-cv-shot'), len(shots))
        self.assertIn('id="c2"', html)
        self.assertNotIn('id="c2" data-cv-shot', html)


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class RevealKeepsSvgTransform(unittest.TestCase):
    def test_svg_group_keeps_geometry_html_reset(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.set_content(SVG_PAGE)
            pg.evaluate(DEEP_FREEZE_JS)
            g_tf = pg.evaluate("getComputedStyle(document.querySelector('g.dots')).transform")
            div_tf = pg.evaluate("getComputedStyle(document.querySelector('div[data-animate]')).transform")
            b.close()
        self.assertEqual(g_tf, "matrix(1, 0, 0, 1, 35, 2)")
        self.assertEqual(div_tf, "none")


if __name__ == "__main__":
    unittest.main()
