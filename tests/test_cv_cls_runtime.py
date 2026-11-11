"""分视口 class/属性运行时: 解析期即时应用, 站点脚本(defer/module, 跑在 DOMContentLoaded 之前)初始化时读到的已是本视口段的 class/aria 态。"""
import unittest

from core.clone.js import CV_CLS_RUNTIME
from tests._env import chromium_available

PAGE = """<!doctype html><html><head><script>RUNTIME</script>
<style>:root{--cv-band:m}</style></head><body>
<button id="b" aria-expanded="true" data-cv-xa-d='{"aria-expanded":"true"}' data-cv-xa-m='{"aria-expanded":"false"}'>menu</button>
<div id="c" class="menu-content md:block" data-cv-cls-d="menu-content md:block" data-cv-cls-m="menu-content md:block hidden">x</div>
<script type="module">
  // 站点打包脚本(module/defer, 解析完才跑): 按钮态烘进 HTML 时若读到桌面的 true, 会把窄屏上该折叠的内容展开;
  // 同步内联脚本插在元素后面立刻读则赶不上(观察者回调是微任务, 排在该脚本之后), 不在此保证
  window.__seen = [document.getElementById('b').getAttribute('aria-expanded'), document.getElementById('c').className];
  if (document.getElementById('b').getAttribute('aria-expanded') === 'true') document.getElementById('c').classList.remove('hidden');
</script>
</body></html>"""


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class CvClsRuntime(unittest.TestCase):
    def test_applied_before_site_script(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 390, "height": 844})
                page.set_content(PAGE.replace("RUNTIME", CV_CLS_RUNTIME))
                page.wait_for_timeout(100)
                self.assertEqual(page.evaluate("window.__seen"), ["false", "menu-content md:block hidden"])
                self.assertEqual(page.evaluate("document.getElementById('c').className"), "menu-content md:block hidden")
                self.assertEqual(page.evaluate("document.getElementById('b').getAttribute('aria-expanded')"), "false")
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main()
