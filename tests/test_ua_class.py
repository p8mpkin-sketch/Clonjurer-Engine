"""只随手机 UA 变的 class(桌面 UA 同宽页与主页相同、手机 UA 首载页不同)只给触屏手机段; 轮换相位(同组件别的实例反向变同一状态词)不换。"""
import unittest

import core.cloner_engine as ce
from tests._env import chromium_available

PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width">
<style>body{margin:0}.card{height:50px}.card.is-open{height:300px}.nav{height:10px}.nav.nav-touch{height:60px}</style></head><body>
<div id="c1" class="card is-open">a</div><div id="c2" class="card">b</div><div id="c3" class="card">c</div>
<div id="x" class="nav">n</div>
<div id="s1" class="slide active">1</div><div id="s2" class="slide">2</div>
<div id="k1" class="acc">k</div>
</body></html>"""


def variant(cls_map):
    h = PAGE
    for i, (old, new) in cls_map.items():
        h = h.replace(f'id="{i}" class="{old}"', f'id="{i}" class="{new}"')
    return h


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class UaClass(unittest.TestCase):
    def test_touch_only_class(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 1440, "height": 800})
                page.set_content(PAGE)
                for tag in ("d", "t", "m", "b"):
                    page.evaluate(ce.CV_RECORD_JS, tag)
                loads = {}
                for key, h in (("m", variant({"c1": ("card is-open", "card"), "x": ("nav", "nav nav-touch"),
                                              "s1": ("slide active", "slide"), "s2": ("slide", "slide active")})),
                               ("__mdesk", variant({"k1": ("acc", "acc is-open")}))):
                    p2 = b.new_page(viewport={"width": 390, "height": 800})
                    p2.set_content(h)
                    loads[key] = p2.evaluate(ce.CV_PATHS_JS)
                    p2.close()
                out = page.evaluate(ce.CV_EMIT_JS, loads)
                self.assertEqual(out.get("clsTouch"), 2)
                self.assertIsNone(page.evaluate("document.getElementById('s1').getAttribute('data-cv-cls-mt')"))
                self.assertEqual(out.get("touchOff"), 2)   # c1(桌面 UA 同宽也展开) 与 k1
                html = page.content()
            finally:
                b.close()
            html = ce.scope_viewport_css(html, 1000, 0)
            b = p.chromium.launch()
            try:
                cls = "() => ['c1','x','s1','s2'].map(i => document.getElementById(i).className)"
                pg = b.new_page(viewport={"width": 390, "height": 800})
                pg.set_content(html)
                self.assertEqual(pg.evaluate(cls), ["card is-open", "nav", "slide active", "slide"])
                ctx = b.new_context(viewport={"width": 390, "height": 800}, has_touch=True, is_mobile=True)
                pt = ctx.new_page()
                pt.set_content(html)
                self.assertEqual(pt.evaluate(cls), ["card", "nav nav-touch", "slide active", "slide"])
                # 桌面版脚本加载后把 k1 展开: 触屏手机段上点按前拿掉; 点按后放行
                pt.evaluate("document.getElementById('k1').classList.add('is-open')")
                pt.wait_for_timeout(50)
                self.assertEqual(pt.evaluate("document.getElementById('k1').className"), "acc")
                pt.tap("#k1")
                pt.evaluate("document.getElementById('k1').classList.add('is-open')")
                pt.wait_for_timeout(50)
                self.assertEqual(pt.evaluate("document.getElementById('k1').className"), "acc is-open")
                pg.evaluate("document.getElementById('k1').classList.add('is-open')")
                pg.wait_for_timeout(50)
                self.assertEqual(pg.evaluate("document.getElementById('k1').className"), "acc is-open")
                pt.set_viewport_size({"width": 1440, "height": 800})
                pt.wait_for_timeout(100)
                self.assertEqual(pt.evaluate(cls)[:2], ["card is-open", "nav"])
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main()
