"""状态类相位复位: 同类组件实例间状态词移动(自动轮换转到第 k 项)复位成源码分配; 全体加/去、单实例、非状态 class 变化不动。"""
import unittest

from core.cloner_engine import _source_id_classes
from core.clone.js import STATE_PHASE_RESET_JS
from tests._env import chromium_available

SRC = """<div id="a1" class="card wixui-card is-open">A</div><div id="a2" class="card wixui-card">B</div><div id="a3" class="card wixui-card">C</div>
<nav id="m1" class="menu">x</nav><nav id="m2" class="menu">y</nav>
<div id="solo" class="panel">z</div><div id="t1" class="tab">1</div><div id="t2" class="tab">2</div>"""
LIVE = """<div id="a1" class="card wixui-card">A</div><div id="a2" class="card wixui-card">B</div><div id="a3" class="card wixui-card is-open">C</div>
<nav id="m1" class="menu active">x</nav><nav id="m2" class="menu active">y</nav>
<div id="solo" class="panel open">z</div><div id="t1" class="tab-x">1</div><div id="t2" class="tab open">2</div>"""


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class StatePhase(unittest.TestCase):
    def test_reset_rotated_group_only(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page()
                page.set_content("<body>" + LIVE + "</body>")
                r = page.evaluate(STATE_PHASE_RESET_JS, _source_id_classes(SRC))
                self.assertEqual(r, {"groups": 1, "els": 2})
                cls = page.evaluate("Object.fromEntries([...document.querySelectorAll('[id]')].map(e => [e.id, e.className]))")
                self.assertEqual(cls["a1"], "card wixui-card is-open")
                self.assertEqual(cls["a3"], "card wixui-card")
                self.assertEqual(cls["m1"], "menu active")        # 全体都加了: 不是轮换
                self.assertEqual(cls["solo"], "panel open")       # 单实例
                self.assertEqual(cls["t2"], "tab open")           # 组里另一员非状态 class 变了, 组不成立
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main()
