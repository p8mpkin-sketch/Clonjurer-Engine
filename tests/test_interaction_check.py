"""交互可用检查与 ARIA 兜底层: 同一页面有脚本时判可用, 去掉脚本后判点不动; 注入兜底层后 ARIA 控件恢复可用。"""
import tempfile
import threading
import unittest
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests._env import chromium_available

PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">{inject}<style>
.menu{{display:none;border:1px solid #333;padding:20px;margin:10px}} [aria-expanded=true]+.menu{{display:none}}
[role=tabpanel]{{padding:30px;font:20px sans-serif}}</style></head><body style="font:16px sans-serif">
<nav><button id="b" aria-expanded="false" aria-controls="m">产品菜单</button><div class="menu" id="m" hidden>菜单内容 菜单内容</div></nav>
<div role="tablist"><button role="tab" aria-selected="true" aria-controls="p1">页签一</button>
<button role="tab" aria-selected="false" aria-controls="p2">页签二</button></div>
<div role="tabpanel" id="p1">第一个面板的内容</div><div role="tabpanel" id="p2" hidden>第二个面板的内容</div>
<button id="c">计数按钮</button><span id="n">0</span>
<x-drop id="xd"><button slot="btn" aria-expanded="false" aria-controls="xp">组件菜单</button>
<div slot="panel" id="xp" style="padding:24px">组件菜单面板内容</div></x-drop>
<script>customElements.define('x-drop',class extends HTMLElement{{constructor(){{super();const r=this.attachShadow({{mode:'open'}});
r.innerHTML='<slot name="btn"></slot><slot name="panel" hidden></slot>'}}}});</script>
{script}</body></html>"""
APP_JS = """<script>
b.onclick=()=>{const o=b.getAttribute('aria-expanded')!=='true';b.setAttribute('aria-expanded',o);m.hidden=!o;m.style.display=o?'block':'none'};
xd.querySelector('button').onclick=e=>{const sl=xd.shadowRoot.querySelector('slot[name=panel]');sl.hidden=!sl.hidden;
 e.currentTarget.setAttribute('aria-expanded',String(!sl.hidden))};
document.querySelectorAll('[role=tab]').forEach(t=>t.onclick=()=>{document.querySelectorAll('[role=tab]').forEach(x=>{
 const on=x===t;x.setAttribute('aria-selected',on);document.getElementById(x.getAttribute('aria-controls')).hidden=!on})});
c.onclick=()=>{n.textContent=+n.textContent+1};
</script>"""


@unittest.skipUnless(chromium_available(), "需要 Chromium")
class InteractionCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from core.clone.aria_shim import ARIA_SHIM_JS
        cls.dir = Path(tempfile.mkdtemp())
        (cls.dir / "orig.html").write_text(PAGE.format(inject="", script=APP_JS), encoding="utf-8")
        (cls.dir / "dead.html").write_text(PAGE.format(inject="", script=""), encoding="utf-8")
        (cls.dir / "shim.html").write_text(PAGE.format(inject=ARIA_SHIM_JS, script=""), encoding="utf-8")
        (cls.dir / "both.html").write_text(PAGE.format(inject=ARIA_SHIM_JS, script=APP_JS), encoding="utf-8")
        h = partial(SimpleHTTPRequestHandler, directory=str(cls.dir))
        h.log_message = lambda *a: None
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), h)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.shutdown()

    def _run(self, clone):
        from core.clone.interaction_check import check

        @contextmanager
        def factory(vp):
            c = self.browser.new_context(viewport=vp)
            try:
                yield c
            finally:
                c.close()
        # 原站也走本地服务器: 用另一个主机名(localhost)让克隆页的「只放本地」守卫不误放原站
        orig = self.base.replace("127.0.0.1", "localhost") + "/orig.html"
        return check(factory, orig, f"{self.base}/{clone}", limit=8, budget_s=40)

    def test_working_clone_passes(self):
        r = self._run("orig.html")
        self.assertGreaterEqual(r["checked"], 3, r)
        self.assertEqual(r["failed"], [], r)

    def test_dead_clone_fails(self):
        r = self._run("dead.html")
        self.assertGreaterEqual(r["checked"], 3, r)
        self.assertEqual(r["ok"], 0, r)

    def test_shim_restores_aria_controls(self):
        r = self._run("shim.html")
        ok = {x for x in ("产品菜单", "页签二", "组件菜单") if x not in {f["text"] for f in r["failed"]}}
        self.assertEqual(ok, {"产品菜单", "页签二", "组件菜单"}, r)     # 含影子 DOM 里隐藏 slot 的组件菜单
        self.assertIn("计数按钮", {f["text"] for f in r["failed"]})     # 没有 ARIA 语义的自定义行为兜不了, 如实判失败

    def test_shim_does_not_fight_working_scripts(self):
        r = self._run("both.html")
        self.assertEqual(r["failed"], [], r)


if __name__ == "__main__":
    unittest.main()
