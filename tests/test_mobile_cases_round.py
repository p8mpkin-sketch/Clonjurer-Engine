"""移动端分类测试集这一轮的通用判据: 根字号随宽、对照页确认变体、无 id 兄弟组相位复位、探针定时口径。"""
import unittest

from core.cloner_engine import _root_inline_varies, _source_sibling_groups, _subtree_sig
from core.clone.js import STATE_PHASE_RESET_JS
from tests._env import chromium_available

SRC = """<html><body><div class="bar">x</div>
<div class="wrap"><div class="slide on">1</div><div class="slide">2</div><div class="slide">3</div>
<div class="dots"><b class="on"></b><b></b><b></b></div></div>
<ul id="nav"><li class="item cur">a</li><li class="item">b</li><li class="item big">c</li></ul>
<p class="k x">1</p><p class="k">2</p></body></html>"""
LIVE = """<div class="bar">x</div>
<div class="wrap"><div class="slide">1</div><div class="slide on">2</div><div class="slide">3</div>
<div class="dots"><b></b><b class="on"></b><b></b></div></div>
<ul id="nav"><li class="item">a</li><li class="item">b</li><li class="item big cur">c</li></ul>
<p class="k x y">1</p><p class="k y">2</p>"""


class Pure(unittest.TestCase):
    def test_root_inline_varies(self):
        self.assertTrue(_root_inline_varies([[1280, 100, 100, 0, 16], [768, 100, 100, 0, 16], [390, 52, 52, 0, 16]]))
        self.assertFalse(_root_inline_varies([[1280, 16, 16, 0, 16], [390, 16, 16, 0, 16]]))
        self.assertFalse(_root_inline_varies([[1280, 0, 16, 0, 16], [390, 0, 14, 0, 14]]))   # 样式表里的变化不算(只认内联)
        self.assertTrue(_root_inline_varies([[1280, 0, 16, 20, 20], [390, 0, 16, 14, 14]]))

    def test_subtree_sig(self):
        a = {"x": ["DIV", "", "", "P,P"], "x>p:1": ["P", "c", "s", ""], "y": ["DIV", "", "", ""]}
        b = {"x": ["DIV", "k", "z", "P,P"], "x>p:1": ["P", "", "", ""], "xy": ["I", "", "", ""]}
        self.assertEqual(_subtree_sig(a, "x"), _subtree_sig(b, "x"))     # 只比标签与子元素序列
        self.assertIsNone(_subtree_sig(a, "nope"))
        b["x>p:1"] = ["P", "", "", "B"]
        self.assertNotEqual(_subtree_sig(a, "x"), _subtree_sig(b, "x"))

    def test_sibling_groups(self):
        g = _source_sibling_groups(SRC)
        by = {(x["a"], tuple(x["i"])): x["k"] for x in g}
        self.assertEqual([k[1] for k in by[("", (1,))]], ["slide on", "slide", "slide", "dots"])
        self.assertEqual([k[1] for k in by[("", (1, 3))]], ["on", None, None])
        self.assertEqual([k[1] for k in by[("nav", ())]], ["item cur", "item", "item big"])
        self.assertEqual(_source_sibling_groups(""), [])


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class Browser(unittest.TestCase):
    def test_sibling_rotation_reset(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page()
                page.set_content("<body>" + LIVE + "</body>")
                r = page.evaluate(STATE_PHASE_RESET_JS, [{}, _source_sibling_groups(SRC)])
                self.assertEqual(r["groups"], 2)
                cls = page.evaluate("s => [...document.querySelectorAll(s)].map(e => e.className)", ".wrap > .slide, .dots b, #nav li, p")
                self.assertEqual(cls[:3], ["slide on", "slide", "slide"])        # 自造状态名 on: 一去一加 → 移回源码那一项
                self.assertEqual(cls[3:6], ["on", "", ""])
                self.assertEqual(cls[6:9], ["item", "item", "item big cur"])     # 另一个兄弟其余 class 不同: 不是同类轮换, 不动
                self.assertEqual(cls[9:], ["k x y", "k y"])                      # 全体都加: 不动
                # 子元素个数对不上(脚本插了节点): 整组不用
                page.set_content("<body>" + LIVE.replace('<div class="dots">', '<i></i><div class="dots">') + "</body>")
                r = page.evaluate(STATE_PHASE_RESET_JS, [{}, _source_sibling_groups(SRC)])
                self.assertEqual(page.evaluate("document.querySelector('.wrap > .slide').className"), "slide")
            finally:
                b.close()

    def test_probe_holds_long_intervals(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine_bench" / "real_sites"))
        from viewport_probe import HOLD_INTERVALS_JS
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page()
                page.add_init_script(HOLD_INTERVALS_JS)
                page.set_content("<body><script>window.a=0;window.b=0;setInterval(()=>{a++},1000);setInterval(()=>{b++},50);</script></body>")
                page.goto("about:blank")
                page.evaluate("window.a=0;window.b=0;setInterval(()=>{a++},1000);setInterval(()=>{b++},50);")
                page.wait_for_timeout(1300)
                self.assertEqual(page.evaluate("a"), 0)
                self.assertGreater(page.evaluate("b"), 5)
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main()
