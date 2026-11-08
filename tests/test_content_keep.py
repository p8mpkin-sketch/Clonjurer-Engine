"""内容守卫: 克隆页里脚本不等用户手势就整块拆掉已渲染内容(水合失配后客户端重建拿不到数据)时放回原块;
拆了再换一份、异步数据稍后补上、用户手势之后的移除都不管。"""
import unittest

from core.clone.support import CONTENT_KEEP_GUARD_JS
from tests._env import chromium_available

PAGE = """<!doctype html><html><head>GUARD</head><body style="margin:0">
<div id="top" style="height:200px">top</div>
<div id="sec"><div id="title" style="height:22px">Best Sellers</div><div id="car" style="height:394px">carousel</div></div>
<div id="foot"><footer id="f1" style="height:800px">footer</footer></div>
<div id="lazy"><div id="ph" style="height:300px">placeholder</div></div>
<button id="b" onclick="document.getElementById('top').remove()">x</button>
<script>
  // 水合重建: 轮播整块被拆掉, 没有东西补上
  document.getElementById('car').remove();
  // 页脚拆了再换一份(不同高度): 不是塌掉
  var f = document.getElementById('f1'); var g = document.createElement('div'); g.style.height = '520px'; g.textContent = 'grid';
  f.parentNode.replaceChild(g, f);
  // 占位块先拆, 1.4s 后异步内容才到
  document.getElementById('ph').remove();
  setTimeout(function(){ var d = document.createElement('div'); d.id = 'real'; d.style.height = '300px'; d.textContent = 'real'; document.getElementById('lazy').appendChild(d); }, 1400);
</script></body></html>"""


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class ContentKeep(unittest.TestCase):
    def test_guard(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 390, "height": 844})
                page.set_content(PAGE.replace("GUARD", CONTENT_KEEP_GUARD_JS))
                page.wait_for_timeout(900)
                self.assertEqual(page.evaluate("!!document.getElementById('car')"), True, "拆掉的轮播应放回")
                self.assertEqual(page.evaluate("document.getElementById('sec').children[1].id"), "car", "放回原位(标题之后)")
                self.assertEqual(page.evaluate("!!document.getElementById('f1')"), False, "换了一份的页脚不放回")
                self.assertEqual(page.evaluate("!!document.getElementById('ph')"), True, "占位块先放回")
                page.wait_for_timeout(1400)
                self.assertEqual(page.evaluate("!!document.getElementById('real')"), True)
                self.assertEqual(page.evaluate("!!document.getElementById('ph')"), False, "异步内容到了, 放回的占位块撤掉")
                self.assertEqual(page.evaluate("window.__cloneKept"), 1)
                page.click("#b")   # 用户手势之后的移除不管
                page.wait_for_timeout(900)
                self.assertEqual(page.evaluate("!!document.getElementById('top')"), False)
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main()
