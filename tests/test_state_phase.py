"""状态类相位复位: 同类组件实例间状态词移动(自动轮换转到第 k 项)复位成源码分配; 全体加/去、单实例、非状态 class 变化不动。"""
import unittest

from core.cloner_engine import _source_id_classes
from core.clone.js import STATE_PHASE_RESET_JS
from tests._env import chromium_available

SRC = """<div id="a1" class="card a1-box wixui-card is-open">A</div><div id="a2" class="card a2-box wixui-card">B</div><div id="a3" class="card a3-box wixui-card">C</div>
<nav id="m1" class="menu">x</nav><nav id="m2" class="menu">y</nav>
<div id="solo" class="panel">z</div><i id="c1" class="acc">1</i><i id="c2" class="acc">2</i><i id="c3" class="acc">3</i><div id="t1" class="tab">1</div><div id="t2" class="tab">2</div><b id="h1" class="hy">1</b><b id="h2" class="hy">2</b><b id="h3" class="hy">3</b><b id="h4" class="hy">4</b>"""
LIVE = """<div id="a1" class="card a1-box wixui-card">A</div><div id="a2" class="card a2-box wixui-card">B</div><div id="a3" class="card a3-box wixui-card is-open">C</div>
<nav id="m1" class="menu active">x</nav><nav id="m2" class="menu active">y</nav>
<div id="solo" class="panel open">z</div><i id="c1" class="acc">1</i><i id="c2" class="acc is-open">2</i><i id="c3" class="acc">3</i><div id="t1" class="tab-x">1</div><div id="t2" class="tab open">2</div><b id="h1" class="hy show">1</b><b id="h2" class="hy show">2</b><b id="h3" class="hy">3</b><b id="h4" class="hy">4</b>"""


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
                self.assertEqual({k: r[k] for k in ("groups", "els", "cand")}, {"groups": 2, "els": 3, "cand": 4})
                cls = page.evaluate("Object.fromEntries([...document.querySelectorAll('[id]')].map(e => [e.id, e.className]))")
                self.assertEqual(cls["a1"], "card a1-box wixui-card is-open")   # 实例私有 class(含自身 id)不妨碍认同类
                self.assertEqual(cls["a3"], "card a3-box wixui-card")
                self.assertEqual(cls["c2"], "acc")                # 源码全收起、采集时展开了其中一个: 复位
                self.assertEqual(cls["m1"], "menu active")        # 全体都加了: 不是轮换
                self.assertEqual(cls["solo"], "panel open")       # 单实例
                self.assertEqual(cls["t2"], "tab open")
                self.assertEqual(cls["h1"], "hy show")            # 多个实例同时变(水合给出的首屏态): 不动           # 组里另一员非状态 class 变了, 组不成立
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main()
