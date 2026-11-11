"""内联态钉住: 改宽来回后 style/class 变了的元素复原成 save 时的值, 新插入的元素不动。"""
import unittest

from core.clone.js import ROUNDTRIP_PIN_JS
from tests._env import chromium_available


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class RoundtripPin(unittest.TestCase):
    def test_restore(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page()
                page.set_content('<body><div id="t" class="track" style="width:1170px;transform:translateX(-390px)"></div><i id="s" class="slide current"></i><u id="u"></u></body>')
                page.evaluate(ROUNDTRIP_PIN_JS, "save")
                page.evaluate("""() => { const t = document.getElementById('t'); t.style.width = '1320px'; t.style.transform = 'translateX(-880px)';
                    document.getElementById('s').className = 'slide'; document.getElementById('u').setAttribute('style', 'color:red');
                    const n = document.createElement('b'); n.id = 'n'; n.className = 'new'; document.body.appendChild(n); }""")
                self.assertEqual(page.evaluate(ROUNDTRIP_PIN_JS, "restore"), 3)
                got = page.evaluate("""() => [document.getElementById('t').getAttribute('style'), document.getElementById('s').className,
                    document.getElementById('u').getAttribute('style'), document.getElementById('n').className]""")
                self.assertEqual(got, ["width:1170px;transform:translateX(-390px)", "slide current", None, "new"])
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main()
