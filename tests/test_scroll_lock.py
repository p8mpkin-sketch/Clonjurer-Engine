"""滚动锁: 序列化前剥掉 html/body 内联 style 上弹层打开时写的锁; 克隆页里脚本不等用户手势就写的锁由守卫撤掉, 用户点开后的锁保留。"""
import unittest

from core.clone.js import SCROLL_LOCK_STRIP_JS
from core.clone.support import SCROLL_UNLOCK_GUARD_JS
from tests._env import chromium_available

LOCKED = """<!doctype html><html style="--banner-h: 46px; overflow: hidden; position: absolute; inset: 0px;"><head></head>
<body style="overflow: hidden; position: absolute; inset: 0px; height: unset; padding-right: 15px; background: #fff">
<div style="height: 3000px">tall</div></body></html>"""

RUNTIME = """<!doctype html><html><head>GUARD</head><body style="background:#fff">
<div style="height: 3000px">tall</div>
<button id="b" onclick="document.body.style.cssText='overflow: hidden; position: fixed; top: 0px; background: #fff'">menu</button>
<script>document.documentElement.style.cssText='overflow: hidden; position: absolute; inset: 0px;';
document.body.style.cssText='overflow: hidden; position: absolute; inset: 0px; height: unset; background: #fff';</script>
</body></html>"""


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class ScrollLock(unittest.TestCase):
    def test_strip_before_serialize(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 390, "height": 844})
                page.set_content(LOCKED)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollHeight"), 844)
                n = page.evaluate(SCROLL_LOCK_STRIP_JS)
                self.assertEqual(n, 8)
                self.assertEqual(page.evaluate("document.documentElement.getAttribute('style')"), "--banner-h: 46px")
                self.assertEqual(page.evaluate("document.body.getAttribute('style')"), "background: #fff")
                self.assertGreater(page.evaluate("document.documentElement.scrollHeight"), 2900)
            finally:
                b.close()

    def test_runtime_guard_reverts_load_lock_keeps_user_lock(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 390, "height": 844})
                page.set_content(RUNTIME.replace("GUARD", SCROLL_UNLOCK_GUARD_JS))
                page.wait_for_timeout(300)
                self.assertGreater(page.evaluate("document.documentElement.scrollHeight"), 2900)
                self.assertEqual(page.evaluate("document.body.getAttribute('style')"), "background: rgb(255, 255, 255)")
                self.assertGreaterEqual(page.evaluate("window.__cloneUnlocked"), 7)
                page.click("#b")   # 用户手势之后写的锁是真打开的菜单, 不撤
                page.wait_for_timeout(200)
                self.assertIn("position: fixed", page.evaluate("document.body.getAttribute('style')"))
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main()
