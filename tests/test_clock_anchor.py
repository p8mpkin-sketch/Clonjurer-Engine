"""时钟锚定脚本(js.CLOCK_ANCHOR_JS): Date 从给定时刻起走, 带参构造/静态方法/原型链不变。"""
import unittest

from core.clone.js import seed_random_js

try:
    from playwright.sync_api import sync_playwright
except Exception:   # pragma: no cover
    sync_playwright = None

T0 = 1_600_000_000_000   # 2020-09-13


@unittest.skipUnless(sync_playwright, "需要 playwright")
class ClockAnchor(unittest.TestCase):
    def _eval(self, js, t0=T0):
        with sync_playwright() as p:
            br = p.chromium.launch()
            try:
                page = br.new_page()
                page.add_init_script(seed_random_js(1, t0_ms=t0))
                page.goto("about:blank")
                return page.evaluate(js)
            finally:
                br.close()

    def test_anchored(self):
        r = self._eval("""() => { class Sub extends Date {}
          return {now: Date.now(), ctor: new Date().getTime(), str: Date(), fixed: new Date(2021, 0, 2).getFullYear(),
            parse: Date.parse('2021-01-02T00:00:00Z'), utc: Date.UTC(2021, 0, 2), inst: new Date() instanceof Date,
            sub: new Sub() instanceof Date && Math.abs(new Sub().getTime() - Date.now()) < 5000,
            via: new (new Date().constructor)().getTime(), name: Date.name, perf: performance.now() < 60000}; }""")
        for k in ("now", "ctor", "via"):
            self.assertTrue(T0 <= r[k] < T0 + 30_000, (k, r[k]))
        self.assertIn("2020", r["str"])
        self.assertEqual(r["fixed"], 2021)
        self.assertEqual(r["parse"], r["utc"])
        self.assertTrue(r["inst"] and r["sub"] and r["perf"])
        self.assertEqual(r["name"], "Date")

    def test_near_now_not_installed(self):
        import time
        r = self._eval("() => !!(window.__cloneClk && Date.toString().includes('native code'))", t0=int(time.time() * 1000))
        self.assertTrue(r)


if __name__ == "__main__":
    unittest.main()
