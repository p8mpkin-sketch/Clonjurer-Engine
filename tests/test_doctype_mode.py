"""序列化保留原文档 doctype: 渲染模式(标准 / 有限怪异 / 怪异)不能被克隆改掉。"""
import unittest

from tests._env import chromium_available

XHTML_T = ('<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
           '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">')
BODY = ('<html><head><style>h3{font-size:24px;line-height:1.1;margin:0}a{font-size:18px}</style></head>'
        '<body><h3 id="t"><a>集团要闻</a></h3></body></html>')
PROBE = "() => [document.compatMode, document.getElementById('t').getBoundingClientRect().height]"


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class DoctypePreserved(unittest.TestCase):
    def _roundtrip(self, page, html):
        from core.clone.js import OUTER_HTML_JS
        page.set_content(html)
        before = page.evaluate(PROBE)
        out = page.evaluate(OUTER_HTML_JS)
        page.set_content(out)
        return before, page.evaluate(PROBE), out

    def test_modes_survive_serialization(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page()
                for doctype in (XHTML_T, "<!DOCTYPE html>", ""):
                    before, after, out = self._roundtrip(page, doctype + BODY)
                    self.assertEqual(before, after, f"doctype {doctype!r} 渲染模式/行高被改: {out[:200]}")
                # 有限怪异模式下 h3 只计行内子元素行高, 比标准模式矮
                _, limited, out = self._roundtrip(page, XHTML_T + BODY)
                _, std, _ = self._roundtrip(page, "<!DOCTYPE html>" + BODY)
                self.assertLess(limited[1], std[1] - 3)
                self.assertTrue(out.startswith('<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"'))
                _, _, out = self._roundtrip(page, BODY)
                self.assertTrue(out.startswith("<html"), "原文档无 doctype(怪异模式)时不能补")
            finally:
                b.close()

    def test_shadow_and_iframe_serializers_use_doctype(self):
        from core.clone.js import IFRAME_EXTRACT_JS, SHADOW_SERIALIZE_JS
        for js in (SHADOW_SERIALIZE_JS, IFRAME_EXTRACT_JS):
            self.assertNotIn("__DOCTYPE__", js)
            self.assertNotIn("'<!DOCTYPE html>", js)


if __name__ == "__main__":
    unittest.main()
