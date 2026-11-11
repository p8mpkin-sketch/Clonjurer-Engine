"""只在手机 UA 下换结构的变体(mt)只给触屏显示, 主页版(mx)在触屏手机段藏起; 非触屏窄窗口仍显示主页版。"""
import unittest

from core.clone.js import ALT_INJECT_JS
from tests._env import chromium_available


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class UaSplit(unittest.TestCase):
    def test_touch_split(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page()
                page.set_content('<body><div id="a">main</div><p id="b">resp</p></body>')
                n = page.evaluate(ALT_INJECT_JS, {"items": [
                    {"path": "div:1", "own": "d t m mx", "alts": [{"bands": "mt", "html": '<div id="a2">phone</div>'}]},
                    {"path": "p:1", "own": "d t", "alts": [{"bands": "m", "html": '<p id="b2">narrow</p>'}]}], "css": ""})
                self.assertEqual(n, 2)
                self.assertTrue(page.evaluate("!!document.querySelector('script[data-clone-preview=cv-touch]')"))
                # 落盘时手机段占位条件换成真实区间; 这里直接让手机段生效
                page.evaluate("""() => { const st = document.querySelector('style[data-clone-vp=alt]');
                    st.textContent = st.textContent.replace('@media all/*cv-d*/', '@media (max-width: 1px)/*cv-d*/').replace('(min-width: 99999px)/*cv-m*/', 'all'); }""")
                vis = "() => ['a','a2','b','b2'].filter(i => document.getElementById(i).checkVisibility())"
                self.assertEqual(page.evaluate(vis), ["a", "b2"])
                page.evaluate("document.documentElement.setAttribute('data-cv-touch', '')")
                self.assertEqual(page.evaluate(vis), ["a2", "b2"])
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main()
