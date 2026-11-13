"""非法嵌套修复护栏(core/clone/js.py NEST_FIX_JS): <p> 里脚本造出的块级容器序列化再解析后不拆树。"""
import unittest

from core.clone.js import NEST_FIX_JS

BUILD = """() => {
  document.body.innerHTML = '<style>.t{font:16px/20px sans-serif;margin:0;width:300px}.l{display:inline-block;color:red}'
    + '.b div{color:blue}</style><div id=c></div><div id=d class=b></div>';
  const mk = host => { for (let i = 0; i < 3; i++) { const p = document.createElement('p'); p.className = 't';
    const a = document.createElement('a'); const d = document.createElement('div'); d.className = 'l'; d.textContent = '直播中';
    a.appendChild(d); a.appendChild(document.createTextNode('标题标题' + i)); p.appendChild(a);
    document.getElementById(host).appendChild(p); } };
  mk('c'); mk('d');
}"""

H = "() => [document.getElementById('c').getBoundingClientRect().height, document.getElementById('d').getBoundingClientRect().height, document.querySelectorAll('#c p').length]"


class NestFix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import playwright.sync_api  # noqa: F401
        except Exception:
            raise unittest.SkipTest("无 playwright")

    def _reparse(self, fix):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            try:
                p = b.new_page()
                p.set_content("<!doctype html><html><head></head><body></body></html>")
                p.evaluate(BUILD)
                live = p.evaluate(H)
                res = p.evaluate(NEST_FIX_JS) if fix else None
                after = p.evaluate(H)
                html = p.evaluate("() => '<!doctype html>' + document.documentElement.outerHTML")
                q = b.new_page()
                q.set_content(html)
                return live, after, q.evaluate(H), res
            finally:
                b.close()

    def test_fixed_survives_reparse(self):
        live, after, rep, res = self._reparse(True)
        self.assertEqual(live, after)                 # 活体渲染不变
        self.assertEqual(rep[0], live[0])             # 回解析后 #c 卡片不拆树
        self.assertEqual(rep[2], 3)
        self.assertEqual(res["fixed"], 3)
        self.assertEqual(res["reverted"], 3)          # #d 被 .b div 标签选择器命中: 换回原 div

    def test_unfixed_breaks(self):
        live, _, rep, _ = self._reparse(False)
        self.assertNotEqual(rep[0], live[0])          # 对照: 不修时解析器拆树, 高度变化


if __name__ == "__main__":
    unittest.main()
