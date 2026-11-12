"""<html>/<body> 内联 --自定义属性 按加载宽度不同: CV_PATHS_JS 根行 [9] 记下, CV_EMIT_JS 写进平板/手机段根规则(对照页一致才写)。"""
import unittest

from core.clone.js import CV_EMIT_JS, CV_PATHS_JS


def _chromium_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


PAGE = """<!doctype html><html style="--banner-h:32px;--x-rand:7"><head><style>
.hero{height:calc(400px - var(--banner-h))}</style></head><body><div class="hero">h</div></body></html>"""


def _root(var_str):
    return ["HTML", "", "", "", "", "", "", "", "", var_str]


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class RootVars(unittest.TestCase):
    def _run(self, loads):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.set_content(PAGE)
            row = pg.evaluate(CV_PATHS_JS)["@html"]
            pg.evaluate("() => { window.__cvRec = {snaps: {d: [], b: []}, els: [], paths: []}; }")
            out = pg.evaluate(CV_EMIT_JS, loads)
            css = pg.evaluate("() => [...document.querySelectorAll('style[data-clone-vp]')].map(s => s.textContent).join('\\n')")
            b.close()
        return row, out, css

    def test_width_dependent_root_var_goes_to_band(self):
        loads = {"m": {"@html": _root("--banner-h:46px;--x-rand:9")},
                 "__pair": {"m": {"w": 359, "paths": {"@html": _root("--banner-h:46px;--x-rand:3")}}}}
        row, out, css = self._run(loads)
        self.assertEqual(row[9], "--banner-h:32px;--x-rand:7")
        self.assertEqual(out.get("rootVars"), 1)
        self.assertIn("html{--banner-h:46px!important}", css)
        self.assertNotIn("--x-rand", css)                  # 两次窄屏加载值不同: 随机量, 不写
        self.assertIn("/*cv-m*/{\nhtml{--banner-h:46px!important}", css)

    def test_same_value_or_no_narrow_load_writes_nothing(self):
        row, out, css = self._run({"m": {"@html": _root("--banner-h:32px;--x-rand:7")}})
        self.assertNotIn("rootVars", out)
        self.assertNotIn("--banner-h", css)


if __name__ == "__main__":
    unittest.main()
