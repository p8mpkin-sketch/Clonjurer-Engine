"""重放去重守卫: 冻结 DOM 已含脚本插入的内容, 交互版重跑同步脚本时去掉重复副本; 加载后的异步插入不动。"""
import unittest

from core.clone.support import REPLAY_DEDUPE_JS
from tests._env import chromium_available, has_modules

PAGE = """<!doctype html><html><head>GUARD</head><body>
<div id="box"><p data-cb="f2-1">hello world paragraph</p><p>second paragraph text here</p></div>
<script>document.getElementById('box').insertAdjacentHTML('beforeend', '<p>hello world paragraph</p><p>second paragraph text here</p>');</script>
<script>document.addEventListener('DOMContentLoaded', function(){setTimeout(function(){
  var b=document.getElementById('box'); var p=document.createElement('p'); p.textContent='second paragraph text here'; b.appendChild(p);}, 50);});</script>
</body></html>"""


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class ReplayDedupe(unittest.TestCase):
    def test_sync_replay_removed_async_kept(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page()
                page.set_content(PAGE.replace("GUARD", REPLAY_DEDUPE_JS))
                page.wait_for_timeout(400)
                texts = page.evaluate("[...document.querySelectorAll('#box p')].map(e=>e.textContent)")
                # 同步重放的两段被去掉; DOMContentLoaded 之后的异步插入保留
                self.assertEqual(texts, ["hello world paragraph", "second paragraph text here", "second paragraph text here"])
            finally:
                b.close()

    def test_hidden_measure_probe_kept(self):
        """脚本同步新建的隐藏测量 span 与冻结 DOM 里的旧副本逐字相同: 不删, 否则脚本读到脱离文档节点的空字号。"""
        from playwright.sync_api import sync_playwright
        page_html = ("<!doctype html><html><head>GUARD</head><body><main>"
                     '<span class="zoom-probe" style="position:absolute;visibility:hidden;font-size:1rem">x</span>'
                     "<script>var s=document.createElement('span');s.className='zoom-probe';"
                     "s.setAttribute('style','position:absolute;visibility:hidden;font-size:1rem');s.textContent='x';"
                     "document.querySelector('main').appendChild(s);"
                     "document.addEventListener('DOMContentLoaded',function(){window.__fs=parseFloat(getComputedStyle(s).fontSize);});</script>"
                     "</main></body></html>")
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page()
                page.set_content(page_html.replace("GUARD", REPLAY_DEDUPE_JS))
                page.wait_for_timeout(300)
                self.assertEqual(page.evaluate("window.__fs"), 16)
                self.assertEqual(page.evaluate("document.querySelectorAll('.zoom-probe').length"), 2)
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main()
