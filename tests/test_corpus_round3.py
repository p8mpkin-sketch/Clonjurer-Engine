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
        i_shot = src.index('self._full_shot(page, self.ws / "original_preview.png"')
        i_media = src.index("self._freeze_media_from_baseline(page)")
        i_freeze = src.index("counts = _safe_eval(DEEP_FREEZE_JS)")
        self.assertLess(i_shot, i_media)
        self.assertLess(i_media, i_freeze)


class CloneLoadShared(unittest.TestCase):
    """首评与像素/布局校准重渲染共用同一加载序列(含滚动揭示懒加载);
    此前校准重渲染不滚动, vitejs 框架图标行/头像在重截图里缺失却仍"提分"。"""

    def test_all_renders_use_helper(self):
        src = Path(ce.__file__).read_text(encoding="utf-8")
        body = src[src.index("    def _load_clone("):]
        body = body[:body.index("\n    def ", 10)]
        self.assertIn("CLONE_SCROLL_JS", body)
        self.assertIn("networkidle", body)
        self.assertGreaterEqual(src.count("self._load_clone(page, base"), 3)
        # 克隆页 goto 只剩 helper 内一处
        self.assertEqual(src.count("page.goto(base + self._page_dir + self._page_frag"), 1)


class AssetQueuePriority(unittest.TestCase):
    """条目上限下: 活体加载过的最先, 再 样式→图片→未知→字体→脚本。
    ant.design 曾 398 个 JS 分包吃光名额(图片 0 张); mi 曾数百个字体切片吃光名额。"""

    def test_order(self):
        urls = ["https://ant.design/a.js", "https://z.cdn/logo.png", "https://y.cdn/s.css",
                "https://x.cdn/img?id=1", "https://w.cdn/f.woff2", "https://ant.design/b.js.map",
                "https://w.cdn/used.woff2"]
        live = {"https://w.cdn/used.woff2": "font"}
        got = sorted(urls, key=lambda u: ce._asset_priority(u, live))
        self.assertEqual(got, ["https://w.cdn/used.woff2", "https://y.cdn/s.css",
                               "https://z.cdn/logo.png", "https://x.cdn/img?id=1",
                               "https://w.cdn/f.woff2", "https://ant.design/a.js",
                               "https://ant.design/b.js.map"])


class FrameBounce(unittest.TestCase):
    """跨域 iframe 顶级导航被弹回宿主页时不固化(jd 登录弹层固化成了首页)。"""

    def test_bounce(self):
        f = ce._frame_bounced
        self.assertTrue(f("https://passport.jd.com/uc/newPopupLogin?ReturnUrl=x",
                          "https://www.jd.com/", "https://www.jd.com/"))
        self.assertFalse(f("https://example.com/w", "https://www.example.com/w?y=1", "https://b.com/"))
        self.assertFalse(f("https://a.com/widget", "https://a.com/login", "https://b.com/"))
        self.assertTrue(f("https://b.com/embed", "https://b.com/", "https://b.com/"))

    def test_live_frame_match(self):
        m = ce._match_live_frame
        live = {"https://passport.jd.com/uc/newPopupLogin?ReturnUrl=a&t=2": "<p>QR</p>"}
        self.assertEqual(m("https://passport.jd.com/uc/newPopupLogin?ReturnUrl=a&t=1", live), "<p>QR</p>")
        self.assertIsNone(m("https://other.com/uc/newPopupLogin", live))


class Utf8CharsetMeta(unittest.TestCase):
    """落盘 UTF-8 时 charset 声明一并改为 utf-8(jd 登录 frame 保留 GBK 声明致乱码)。"""

    def test_rewrite(self):
        h = ('<meta http-equiv="Content-Type" content="text/html; charset=GBK">'
             '<meta charset=gb2312><title>登录</title>')
        out = ce._utf8_charset(h)
        self.assertNotIn("GBK", out)
        self.assertNotIn("gb2312", out)
        self.assertEqual(out.count("utf-8"), 2)
        self.assertIn("登录", out)

    def test_cssom_rewrite_keeps_data_uri(self):
        # CSSOM 序列化: 引号内整体匹配, data: SVG 内的 url(%23id) 不被改写(合成页 96.0 -> 100)
        self.assertIn("%23|https?:", ce.DEEP_FREEZE_JS)
        self.assertIn("(?:\"([^\"]*)\"|'([^']*)'|", ce.DEEP_FREEZE_JS)

    def test_fragment_refs_skipped(self):
        refs = ce.parse_css_refs("a{fill:url(%23SVGabc)} b{background:url(x.png)}",
                                 "https://cn.vuejs.org/assets/s.css")
        self.assertEqual([r for r, _ in refs], ["x.png"])


class TimeFreezeOrder(unittest.TestCase):
    """时间冻结在基准截图之前、相位固化在其后且早于 DEEP_FREEZE; 评分加载序列做相位对齐。"""

    def test_order(self):
        src = Path(ce.__file__).read_text(encoding="utf-8")
        i_frz = src.index("frz = _safe_eval(TIME_FREEZE_JS)")
        i_shot = src.index('self._full_shot(page, self.ws / "original_preview.png"')
        i_ph = src.index("n_ph = _safe_eval(ANIM_PHASE_JS)")
        i_deep = src.index("counts = _safe_eval(DEEP_FREEZE_JS)")
        self.assertLess(i_frz, i_shot)
        self.assertLess(i_shot, i_ph)
        self.assertLess(i_ph, i_deep)
        body = src[src.index("    def _load_clone("):]
        body = body[:body.index("\n    def ", 10)]
        self.assertIn("ANIM_ALIGN_JS", body)


class EngineLogPersisted(unittest.TestCase):
    """任务日志落盘到工作区 engine.log, 且只追加不抛错。"""

    def test_log_appends_to_workspace(self):
        import tempfile
        from pathlib import Path
        from core.cloner_engine import ClonerEngine

        class _Bus:
            def __init__(self):
                self.items = []

            def publish(self, *a):
                self.items.append(a)

        eng = ClonerEngine.__new__(ClonerEngine)
        eng.bus, eng.task_id = _Bus(), "t"
        with tempfile.TemporaryDirectory() as d:
            eng.ws = Path(d)
            eng.log("采集", "第一行")
            eng.log("评分", "第二行")
            text = (Path(d) / "engine.log").read_text(encoding="utf-8")
        self.assertIn("[采集] 第一行", text)
        self.assertIn("[评分] 第二行", text)
        self.assertEqual(len(eng.bus.items), 2)


class FullShotFallback(unittest.TestCase):
    """整页截图失败时退化为限高截图, 高度覆盖评分区域且不超过页面实际高度。"""

    def _engine(self):
        from core.cloner_engine import ClonerEngine
        eng = ClonerEngine.__new__(ClonerEngine)
        eng.logs = []
        eng.log = lambda stage, msg: eng.logs.append((stage, msg))
        return eng

    def test_fallback_clip(self):
        from core.cloner_engine import SCORE_MAX_HEIGHT, SCORE_WIDTH

        class _Page:
            def __init__(self, doc_h):
                self.calls, self.doc_h = [], doc_h

            def screenshot(self, **kw):
                self.calls.append(kw)
                if len(self.calls) == 1:
                    raise TimeoutError("Timeout 30000ms exceeded")

            def evaluate(self, _js):
                return [1280, self.doc_h]

        eng = self._engine()
        cap = int(SCORE_MAX_HEIGHT * 1280 / SCORE_WIDTH)
        # 超长页: 先限高(失败) → 整页(成功)
        tall = _Page(90000)
        eng._full_shot(tall, "/tmp/x.png", "原站")
        self.assertEqual(tall.calls[0]["clip"], {"x": 0, "y": 0, "width": 1280, "height": cap})
        self.assertNotIn("clip", tall.calls[1])
        short = _Page(3000)
        eng._full_shot(short, "/tmp/x.png")
        self.assertEqual(short.calls[1]["clip"]["height"], 3000)

    def test_overtall_clips_directly(self):
        from core.cloner_engine import SCORE_MAX_HEIGHT, SCORE_WIDTH

        class _Page:
            def __init__(self):
                self.calls = []

            def screenshot(self, **kw):
                self.calls.append(kw)

            def evaluate(self, _js):
                return [1280, 41000, 800]

        eng = self._engine()
        pg = _Page()
        eng._full_shot(pg, "/tmp/x.png", "原站")
        self.assertEqual(len(pg.calls), 1)
        self.assertEqual(pg.calls[0]["clip"]["height"], int(SCORE_MAX_HEIGHT * 1280 / SCORE_WIDTH))
        self.assertNotIn("animations", pg.calls[0])

    def test_success_no_fallback(self):
        class _Page:
            calls = 0

            def screenshot(self, **kw):
                _Page.calls += 1

        eng = self._engine()
        eng._full_shot(_Page(), "/tmp/x.png")
        self.assertEqual(_Page.calls, 1)
        self.assertEqual(eng.logs, [])


class TiledShotFallback(unittest.TestCase):
    """整页与限高截图都失败时按视口分块拼接, 拼出的高度覆盖页面(不超评分上限)。"""

    def test_tiles_stitched(self):
        import io as _io
        import tempfile
        from pathlib import Path
        from PIL import Image
        from core.cloner_engine import ClonerEngine

        class _Page:
            def __init__(self):
                self.scroll, self.full_fail = 0, 0

            def screenshot(self, **kw):
                if kw.get("full_page"):
                    self.full_fail += 1
                    raise RuntimeError("Unable to capture screenshot")
                shade = min(255, self.scroll // 10)
                buf = _io.BytesIO()
                Image.new("RGB", (1000, 800), (shade, 0, 0)).save(buf, "PNG")
                return buf.getvalue()

            def evaluate(self, js):
                if "clientWidth" in js:
                    return [1000, 2000, 800]
                if "scrollTo(0, 0)" in js:
                    self.scroll = 0
                    return None
                y = int(js.split("scrollTo(0, ")[1].split(")")[0])
                self.scroll = min(y, 2000 - 800)
                return self.scroll

            def wait_for_timeout(self, _ms):
                pass

        eng = ClonerEngine.__new__(ClonerEngine)
        eng.log = lambda *a: None
        page = _Page()
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "shot.png"
            eng._full_shot(page, out, "克隆页")
            im = Image.open(out)
            self.assertEqual(im.size, (1000, 2000))
            self.assertEqual(page.full_fail, 2)
            self.assertEqual(im.getpixel((5, 5))[0], 0)          # 第一屏
            self.assertEqual(im.getpixel((5, 1999))[0], 120)     # 末屏按实际滚动位置 1200 对齐


def _chromium_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class ShadowDomSerialization(unittest.TestCase):
    """开放影子根(含嵌套与构造样式表)序列化为声明式模板, 无脚本也能还原。"""

    PAGE = """<!doctype html><html lang="en" data-x="a&quot;b"><body><x-card>slotted</x-card><script>
    const sh = new CSSStyleSheet(); sh.replaceSync('h2{color:rgb(1, 2, 3)}');
    customElements.define('x-card', class extends HTMLElement { connectedCallback() {
      const r = this.attachShadow({mode: 'open'}); r.adoptedStyleSheets = [sh];
      r.innerHTML = '<h2>Title</h2><slot></slot><x-in></x-in>'; }});
    customElements.define('x-in', class extends HTMLElement { connectedCallback() {
      this.attachShadow({mode: 'open'}).innerHTML = '<b>inner</b>'; }});
    </script></body></html>"""

    def test_roundtrip_without_scripts(self):
        import re
        from playwright.sync_api import sync_playwright
        from core.cloner_engine import SHADOW_SERIALIZE_JS, OUTER_HTML_JS
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.set_content(self.PAGE)
            out = pg.evaluate(SHADOW_SERIALIZE_JS)
            self.assertEqual(out["roots"], 2)
            self.assertEqual(out["adopted"], 1)
            html = re.sub(r"<script\b.*?</script>", "", out["html"], flags=re.S)
            self.assertIn('lang="en"', html)
            pg2 = b.new_page()
            pg2.set_content(html)
            got = pg2.evaluate("""() => {
              const r = document.querySelector('x-card').shadowRoot;
              return [getComputedStyle(r.querySelector('h2')).color,
                      r.querySelector('x-in').shadowRoot.textContent,
                      document.documentElement.getAttribute('data-x')]; }""")
            self.assertEqual(got, ["rgb(1, 2, 3)", "inner", 'a"b'])
            # 无影子根: 返回 null, 调用方沿用 outerHTML
            pg3 = b.new_page()
            pg3.set_content("<p>plain</p>")
            self.assertIsNone(pg3.evaluate(SHADOW_SERIALIZE_JS)["html"])
            self.assertIn("<p>plain</p>", pg3.evaluate(OUTER_HTML_JS))
            b.close()


class LayoutFixCss(unittest.TestCase):
    """布局校准: 只对最深的失配块出规则; 横向不一致的不算同一块。"""

    def test_leaf_rules(self):
        from core.cloner_engine import layout_fix_css
        orig = [["body>main:nth-of-type(1)", 0, 500, 0, 1000],
                ["body>main:nth-of-type(1)>div:nth-of-type(1)", 0, 200, 0, 1000],
                ["body>main:nth-of-type(1)>div:nth-of-type(1)>p:nth-of-type(1)", 10, 100, 0, 1000],
                ["body>main:nth-of-type(1)>div:nth-of-type(10)", 300, 100, 0, 1000],
                ["body>main:nth-of-type(1)>div:nth-of-type(2)", 200, 100, 0, 1000]]
        clone = [["body>main:nth-of-type(1)", 0, 490, 0, 1000],
                 ["body>main:nth-of-type(1)>div:nth-of-type(1)", 0, 190, 0, 1000],
                 ["body>main:nth-of-type(1)>div:nth-of-type(1)>p:nth-of-type(1)", 10, 90, 0, 1000],
                 ["body>main:nth-of-type(1)>div:nth-of-type(10)", 290, 120, 0, 1000],
                 ["body>main:nth-of-type(1)>div:nth-of-type(2)", 190, 100, 0, 600]]
        css, n = layout_fix_css(orig, clone)
        self.assertEqual(n, 1)
        self.assertIn("body>main:nth-of-type(1)>div:nth-of-type(1)>p:nth-of-type(1)"
                      "{min-height:100px!important}", css)
        self.assertNotIn("max-height", css)          # 偏高块不收回(裁内容换对齐不可接受)
        self.assertNotIn("div:nth-of-type(10)", css)
        self.assertNotIn("div:nth-of-type(2)", css)
        self.assertEqual(layout_fix_css(orig, orig), ("", 0))

    @unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
    def test_browser_roundtrip(self):
        from playwright.sync_api import sync_playwright
        from core.cloner_engine import LAYOUT_MAP_JS, layout_fix_css
        page_html = ('<body style="margin:0"><header style="height:80px">h</header><main>'
                     '<section><div style="height:{h}px;width:500px">x</div></section>'
                     '<section style="height:300px">tail</section></main></body>')
        js = f"() => ({LAYOUT_MAP_JS})(4000)"
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.set_content(page_html.format(h=240))
            orig = pg.evaluate(js)
            pg.set_content(page_html.format(h=200))
            css, n = layout_fix_css(orig, pg.evaluate(js))
            self.assertEqual(n, 1)
            pg.set_content(page_html.format(h=200).replace("</body>", f"<style>{css}</style></body>"))
            fixed = {e[0]: (e[1], e[2]) for e in pg.evaluate(js)}
            want = {e[0]: (e[1], e[2]) for e in orig}
            self.assertEqual(fixed, want)
            b.close()


class RepairRoundRestore(unittest.TestCase):
    """修复轮回退: 恢复最优轮的页面字节、删除其后新增的文件、复原资源表与校准样式。"""

    def test_snapshot_restore(self):
        import tempfile
        from pathlib import Path
        from core.cloner_engine import ClonerEngine
        eng = ClonerEngine.__new__(ClonerEngine)
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            eng.ws, eng._calib_css = ws, "a{}"
            (ws / "index.html").write_text("v1")
            (ws / "engine.log").write_text("log")
            (ws / "static").mkdir()
            results = {"u1": 1}
            snap = eng._snapshot_round(0, {"score": 80.0}, results)
            (ws / "index.html").write_text("v2")
            (ws / "static" / "ad.js").write_text("x")
            (ws / "engine.log").write_text("log more")
            eng._calib_css = "a{}\nb{}"
            results["u2"] = 2
            eng._restore_round(snap, results)
            self.assertEqual((ws / "index.html").read_text(), "v1")
            self.assertFalse((ws / "static" / "ad.js").exists())
            self.assertEqual((ws / "engine.log").read_text(), "log more")   # 日志不回滚
            self.assertEqual(eng._calib_css, "a{}")
            self.assertEqual(results, {"u1": 1})


@unittest.skipUnless(_chromium_available(), "需要 Playwright Chromium")
class BlobUrlInline(unittest.TestCase):
    """运行时 blob: 背景图在采集时内联为 data:, 离线可解析。"""

    def test_inline(self):
        from playwright.sync_api import sync_playwright
        from core.cloner_engine import ClonerEngine, OUTER_HTML_JS
        eng = ClonerEngine.__new__(ClonerEngine)
        eng.log = lambda *a: None
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.set_content("""<div id=d style="width:50px;height:50px"></div><script>
              const c = document.createElement('canvas'); c.width = c.height = 4;
              const g = c.getContext('2d'); g.fillStyle = 'red'; g.fillRect(0, 0, 4, 4);
              c.toBlob(bl => { document.getElementById('d').style.backgroundImage =
                'url(' + URL.createObjectURL(bl) + ')'; document.body.dataset.ok = 1; });
            </script>""")
            pg.wait_for_selector("body[data-ok]")
            html = eng._inline_blob_urls(pg, pg.evaluate(OUTER_HTML_JS))
            b.close()
        self.assertNotIn("blob:", html)
        self.assertIn("data:image/png;base64,", html)
