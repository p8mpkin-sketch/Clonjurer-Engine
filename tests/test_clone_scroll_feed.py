"""评分克隆滚动对无限下拉流的护栏: 页高随滚动增长时在底部等增长, 直到达到原站基准高(core/clone/scoring.py)。"""
import unittest

from core.clone.scoring import ScoringMixin

FEED = """<!doctype html><html><body style="margin:0">
<div id=f></div><div id=s style="height:10px"></div>
<script>
let n = 0, busy = false;
function add(){ const d = document.createElement('div'); d.style.height = '700px'; d.textContent = 'batch ' + (n++);
  document.getElementById('f').appendChild(d); }
add(); add();
new IntersectionObserver(es => { if (es[0].isIntersecting && !busy && n < 8) { busy = true;
  setTimeout(() => { add(); busy = false; }, 350); } }).observe(document.getElementById('s'));
</script></body></html>"""


def _run(target):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        try:
            p = b.new_page(viewport={"width": 800, "height": 600})
            p.set_content(FEED)
            p.evaluate(ScoringMixin.CLONE_SCROLL_JS, target)
            return p.evaluate("() => document.documentElement.scrollHeight"), p.evaluate("() => scrollY")
        finally:
            b.close()


class CloneScrollFeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import playwright.sync_api  # noqa: F401
        except Exception:
            raise unittest.SkipTest("无 playwright")

    def test_waits_for_feed_until_baseline(self):
        h, y = _run(5000)
        self.assertGreaterEqual(h, 0.97 * 5000)
        self.assertEqual(y, 0)          # 结束回顶

    def test_no_target_keeps_old_behaviour(self):
        h, _ = _run(0)
        self.assertLess(h, 4000)         # 不知道基准高: 不在底部等(与旧实现同)

    def test_not_past_baseline(self):
        h, _ = _run(2100)
        self.assertLess(h, 3600)         # 已到基准高即停, 不把流拉得比原站更长


class StaticKeepsFeed(unittest.TestCase):
    """保真轨领先但页面是滚动续载的信息流: 不换静态版(静态版流冻结, 窄屏首屏以下全是骨架)。"""

    def _obj(self, big):
        o = ScoringMixin.__new__(ScoringMixin)
        o._fidelity_score, o._feed_big, o.logs = 99.8, big, []
        o.log = lambda tag, msg: o.logs.append(msg)
        o._main_page_file = lambda: (_ for _ in ()).throw(AssertionError("不该走到生成静态版"))
        return o

    def test_feed_keeps_interactive(self):
        o = self._obj(True)
        self.assertIsNone(o._try_static_track(None, 70.3))
        self.assertTrue(any("信息流" in m for m in o.logs))

    def test_collapsed_interactive_still_switches(self):
        o = self._obj(True)
        with self.assertRaises(AssertionError):   # 交互轨塌了(< 50): 照常试静态版
            o._try_static_track(None, 20.0)


if __name__ == "__main__":
    unittest.main()
