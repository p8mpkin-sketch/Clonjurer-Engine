"""图片地址钉住: 缩放来回中被站点脚本清空的 img/source 地址按缩放前还原; 脚本换成的新地址不覆盖; 原宽不渲染的不还原(留给窄屏页补真图)。"""
import unittest

from core.clone.js import IMG_SRC_PIN_JS
from tests._env import chromium_available

HTML = """<body><img id="a" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" srcset="x.png 1x">
<img id="b" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">
<picture><source id="s" srcset="y.png 1x"><img id="c" src="z.png"></picture>
<img id="h" style="display:none" src="blur.png"></body>"""


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class ImgSrcPin(unittest.TestCase):
    def test_restore_cleared_only(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page()
                page.set_content(HTML)
                page.evaluate(IMG_SRC_PIN_JS, "save")
                page.evaluate("""() => { const a = document.getElementById('a'); a.setAttribute('src', ''); a.removeAttribute('srcset');
                    document.getElementById('b').setAttribute('src', 'new.png');
                    document.getElementById('s').setAttribute('srcset', ''); document.getElementById('h').setAttribute('src', ''); }""")
                self.assertEqual(page.evaluate(IMG_SRC_PIN_JS, "restore"), 2)
                got = page.evaluate("""() => ({a: document.getElementById('a').getAttribute('src').slice(0, 10),
                    as: document.getElementById('a').getAttribute('srcset'), b: document.getElementById('b').getAttribute('src'),
                    s: document.getElementById('s').getAttribute('srcset'), h: document.getElementById('h').getAttribute('src')})""")
                self.assertEqual(got, {"a": "data:image", "as": "x.png 1x", "b": "new.png", "s": "y.png 1x", "h": ""})
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main()
