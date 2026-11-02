"""第二轮真实语料(2026-11-02 22:41)归因后的整类修复护栏。

- 识别: 第三方代码里的框架名字符串(选择器清单、在线沙箱模板、依赖清单、外链 URL)
  不再算作框架证据; 证据片段取自语料站点的真实字节(只保留触发误判的那一小段);
- 沙箱: 文档导航永不由 Mock 接口应答(根路径带查询串的接口录成 path="/");
- 本地化: HTML 属性里序列化成 &amp; 的 URL 也能被改写;
- 失败分类: 网络层不可达不触发整链重跑。
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import cloner_engine as ce  # noqa: E402
from core import pattern_detector as pd  # noqa: E402


def _detect(js_chunks, html=""):
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        jsdir = ws / "static" / "js"
        jsdir.mkdir(parents=True)
        for i, chunk in enumerate(js_chunks):
            (jsdir / f"c{i}.js").write_bytes(chunk)
        return pd.detect(ws, html=html)


class ThirdPartyStringsAreNotEvidence(unittest.TestCase):
    def test_selector_list_is_not_nuxt_or_angular(self):
        # 第三方监测脚本里的"框架挂载点"选择器清单(techcrunch 误判 Nuxt)
        js = (b'var Fia=\'router-outlet router-view main [role="main"] #__next #__nuxt '
              b'#app #root app-root [data-reactroot] [ng-version]\'.split(" ");'
              b'Vk=function(a){return a.querySelector("[ng-version]")!=null};'
              b'n.p="/wp-content/themes/tc-24/dist/";')
        html = '<link rel="stylesheet" href="/wp-content/themes/tc-24/style.css">'
        r = _detect([js], html)
        self.assertEqual(r["pattern"], "M-WordPress CMS")
        self.assertNotIn("Nuxt", r["features"])
        self.assertNotIn("Angular", r["features"])

    def test_sandbox_templates_are_not_ui_libraries(self):
        # 在线代码沙箱模板里的依赖清单与 import 路径(ant.design 误判 Vue)
        js = (b'JSON.stringify({dependencies:{"@angular/core":"^11.2.0","zone.js":"^0.11"}});'
              b'code:\'import { render } from "solid-js/web";\';'
              b'devDependencies:{"@vue/cli-plugin-babel":"^5.0.8"};'
              b'var ReactDOM=require("react-dom");')
        r = _detect([js])
        self.assertEqual(r["pattern"], "M-React SPA")
        self.assertEqual(r["evidence"]["js"], ["React"])

    def test_external_wp_upload_and_detector_code_are_not_wordpress(self):
        # 外链图片(cn.vuejs.org 赞助位)与"检测 WordPress"的探针代码(allbirds)
        vue = b'a.__VUE__=!0;"image":"https://www.wrappixel.com/wp-content/uploads/2023/x.png"'
        probe = (b'document.querySelector(\'link[href*="/wp-content/"], '
                 b'link[href*="/wp-includes/"], script[src*="/wp-content/"]\')')
        self.assertEqual(_detect([vue])["pattern"], "M-Vue/Vite SPA")
        shop = '<script>Shopify.theme = {"name":"x"};</script><div data-v-app=""></div>'
        self.assertEqual(_detect([probe, vue], shop)["pattern"], "M-Shopify CMS")

    def test_vue_material_package_name_is_not_evidence(self):
        r = _detect([b'a.__VUE__=!0;links:["vue-material","vuetify"]'])
        self.assertEqual(r["pattern"], "M-Vue/Vite SPA")

    def test_real_runtimes_still_detected(self):
        self.assertEqual(_detect([b'o.default.__nuxt__fetch__mixin__||1'])["pattern"], "M-Nuxt SSR")
        self.assertEqual(_detect([b'V.done=_$HY.done=!0'])["pattern"], "M-Solid SPA")
        self.assertEqual(_detect([b'typeof ngDevMode'])["pattern"], "M-Angular SPA")
        self.assertEqual(_detect([b'$.getJSON(\'/wp-content/themes/runoob/x.php\')'])["pattern"],
                         "M-WordPress CMS")
        self.assertEqual(_detect([], '<meta name="generator" content="WordPress 6.8">')["pattern"],
                         "M-WordPress CMS")


class SandboxDocumentNeverMocked(unittest.TestCase):
    def _handler(self, path, accept):
        h = ce._SandboxHandler.__new__(ce._SandboxHandler)
        h._mocks = [{"path": "/", "method": "GET", "status": 200,
                     "response_body": '{"code":0}'}]
        h._tape = []
        h.path = path
        h.headers = {"Accept": accept}
        h.wfile = io.BytesIO()
        sent = []
        h.send_response = lambda code, *a: sent.append(code)
        h.send_header = lambda *a: None
        h.end_headers = lambda: None
        return h, sent

    def test_navigation_to_root_gets_page_not_json(self):
        h, sent = self._handler("/", "text/html,application/xhtml+xml,*/*;q=0.8")
        self.assertFalse(h._serve_mock("GET"))
        self.assertEqual(sent, [])

    def test_api_fetch_to_root_still_mocked(self):
        h, sent = self._handler("/?functionId=x", "application/json, text/plain, */*")
        self.assertTrue(h._serve_mock("GET"))
        self.assertEqual(sent, [200])
        self.assertIn(b'"code":0', h.wfile.getvalue())


class HtmlEscapedUrlRewrite(unittest.TestCase):
    URL = "https://img.example-cdn.com/a/1.jpeg@base@tag=imgScale&F=webp&h=1180&w=2560"

    def test_amp_escaped_attribute_is_localized(self):
        pairs = [(self.URL, "/static/img/abc.jpeg"), ("https://x.com/b.css", "/static/css/b.css")]
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        html = ('<img src="' + self.URL.replace("&", "&amp;") + '">'
                '<link href="https://x.com/b.css">')
        out = ce.apply_replacements(html, ce._html_escaped_pairs(pairs))
        self.assertEqual(out, '<img src="/static/img/abc.jpeg"><link href="/static/css/b.css">')

    def test_no_ampersand_keeps_table_identity(self):
        pairs = [("https://x.com/long/b.css", "/b.css"), ("https://x.com/a.js", "/a.js")]
        self.assertIs(ce._html_escaped_pairs(pairs), pairs)

    def test_sorted_longest_first(self):
        pairs = [(self.URL, "/1"), ("https://h.com/p?a=1&b=2", "/2")]
        out = ce._html_escaped_pairs(pairs)
        lens = [len(k) for k, _ in out]
        self.assertEqual(lens, sorted(lens, reverse=True))
        self.assertIn(("https://h.com/p?a=1&amp;b=2", "/2"), out)


class UnreachableIsNotTransient(unittest.TestCase):
    def test_unreachable_marked_and_distinct(self):
        exc = ce.TargetUnreachable("目标不可达(网络层): net::ERR_TIMED_OUT")
        self.assertIsInstance(exc, RuntimeError)
        # 文本里带 net::ERR_TIMED_OUT(瞬时标记之一), 但类型优先: run() 不整链重跑
        self.assertTrue(any(m in str(exc) for m in ce._TRANSIENT_ERROR_MARKS))
        src = Path(ce.__file__).read_text(encoding="utf-8")
        self.assertIn("not isinstance(exc, TargetUnreachable)", src)


if __name__ == "__main__":
    unittest.main()


class MediaShotContract(unittest.TestCase):
    """空白 Canvas(WebGL)/无帧视频从纯净基准截图回填(wix 首屏视频、WebGL 插画整块空白)。
    浏览器级验证: 合成站 WebGL + 脚本播放的 webm, 交互轨 90.1 -> 99.4、保真轨 79.7 -> 97.4。"""

    def test_scripts_and_hook(self):
        self.assertIn("getImageData", ce.MEDIA_SHOT_JS)
        self.assertIn("data-clone-shot", ce.MEDIA_SHOT_JS)
        self.assertIn("poster", ce.MEDIA_SHOT_APPLY_JS)
        src = Path(ce.__file__).read_text(encoding="utf-8")
        # 必须在纯净基准截图之后、DEEP_FREEZE 之前
        i_shot = src.index('page.screenshot(path=str(self.ws / "original_preview.png")')
        i_media = src.index("self._freeze_media_from_baseline(page)")
        i_freeze = src.index("counts = _safe_eval(DEEP_FREEZE_JS)")
        self.assertLess(i_shot, i_media)
        self.assertLess(i_media, i_freeze)
