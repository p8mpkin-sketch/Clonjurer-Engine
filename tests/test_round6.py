"""第 6 轮(通宵)优化守卫: 字体按需下载计划。"""
import unittest

from core.clone.support import fontface_plan, fontface_swap_for_remote, page_codepoints

BASE = "https://s.com/css/x.css"
CSS = """@font-face{font-family:A;src:url(a.eot);src:url(a.eot?#iefix) format("embedded-opentype"),
url(a.woff2) format("woff2"),url(a.woff) format("woff"),url("a.ttf") format("truetype")}
@font-face{font-family:B;src:local("B"),url(/f/b-cyr.woff2) format("woff2");unicode-range:U+0400-045F,U+0490-0491}
@font-face{font-family:B;src:url(/f/b-lat.woff2) format("woff2");unicode-range:U+0000-00FF,U+0131}
@font-face{font-family:B;src:url(/f/b-cjk.woff2) format("woff2");unicode-range:U+4E??}
@font-face{font-family:I;src:url(i.woff2);unicode-range:U+E000-E0FF}
@font-face{font-family:C;src:url(c.woff2) format("woff2") tech(variations), url(c.ttf)}
@font-face{font-family:D;src:url(data:font/woff2;base64,AAAA) format("woff2"),url(d.woff)}
@font-face{font-family:E;src:url(e.svg#e) format("svg"),url(e.woff)}"""


class FontPlan(unittest.TestCase):
    def test_primary_format_kept_fallbacks_skipped(self):
        need, skip, fb = fontface_plan(CSS, BASE, None)
        self.assertIn("https://s.com/css/a.woff2", need)
        for u in ("a.woff", "a.ttf", "a.eot", "c.ttf", "d.woff"):
            self.assertIn("https://s.com/css/" + u, skip, u)
        self.assertIn("https://s.com/css/c.woff2", need)          # tech() 不影响首选
        self.assertIn("https://s.com/css/e.woff", need)           # svg 字体不受支持, 往后取
        self.assertEqual(fb["https://s.com/css/a.woff"], "https://s.com/css/a.woff2")
        # cps=None: 不按字符剔除
        self.assertTrue({"https://s.com/f/b-cyr.woff2", "https://s.com/f/b-cjk.woff2"} <= need)

    def test_unicode_range_by_page_chars(self):
        cps = page_codepoints(["<p>Hello &#x4e00;</p>"])
        need, skip, _ = fontface_plan(CSS, BASE, cps)
        self.assertIn("https://s.com/f/b-lat.woff2", need)
        self.assertIn("https://s.com/f/b-cjk.woff2", need)        # 实体反转义后的字符也算
        self.assertIn("https://s.com/f/b-cyr.woff2", skip)
        self.assertIn("https://s.com/css/i.woff2", need)          # 私用区(图标字体)不按字符剔除

    def test_css_escape_counts_as_page_char(self):
        cps = page_codepoints(['.x:before{content:"\\0416"}'])
        need, _, _ = fontface_plan(CSS, BASE, cps)
        self.assertIn("https://s.com/f/b-cyr.woff2", need)

    def test_unparseable_range_is_conservative(self):
        css = "@font-face{font-family:Z;src:url(z.woff2);unicode-range:U+ZZZZ}"
        need, skip, _ = fontface_plan(css, BASE, [65])
        self.assertIn("https://s.com/css/z.woff2", need)
        self.assertFalse(skip)

    def test_swap_ignores_remote_fallbacks_when_primary_local(self):
        css = ('@font-face{font-family:A;src:url(/static/fonts/a.woff2) format("woff2"),'
               'url(https://cdn.x/a.ttf) format("truetype")}')
        self.assertEqual(fontface_swap_for_remote(css), css)
        css2 = ('@font-face{font-family:A;src:url(https://cdn.x/a.woff2) format("woff2"),'
                'url(/static/fonts/a.ttf) format("truetype")}')
        self.assertIn("font-display:swap", fontface_swap_for_remote(css2))


if __name__ == "__main__":
    unittest.main()


class BoundedCall(unittest.TestCase):
    """无超时的 Playwright 同步调用加时限: 永不返回的页内 Promise 在时限内抛 TimeoutError, 页面仍可继续用。"""

    def test_times_out_and_page_stays_usable(self):
        import time
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("无 playwright")
        from core.clone.support import bounded_call
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch()
        except Exception as exc:  # 无浏览器环境
            self.skipTest(f"无 chromium: {exc}")
        try:
            page = browser.new_page()
            page.set_content("<p>x</p>")
            t0 = time.monotonic()
            with self.assertRaises(TimeoutError):
                bounded_call(page, page._impl_obj.evaluate("() => new Promise(() => {})"), 1.0)
            self.assertLess(time.monotonic() - t0, 5)
            self.assertEqual(page.evaluate("1 + 1"), 2)
            cdp = page.context.new_cdp_session(page)
            snap = bounded_call(cdp, cdp._impl_obj.send("Page.captureSnapshot", {"format": "mhtml"}), 30)
            self.assertIn("MIME", snap["data"][:400])
            bounded_call(page, page._impl_obj.close(), 10)
            self.assertTrue(page.is_closed())
        finally:
            browser.close()
            pw.stop()


PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000201e0a1b3a90000000049454e44ae426082")
HASH = "a6fc20e8360913e8c796df1def661bf1d7f1b9c8"


class ImgShardResolve(unittest.TestCase):
    """运行时拼出的图片地址(换分片主机 / 追加 @变换后缀)就近指向资源池里同一底图的本地文件。"""

    def test_host_suffix(self):
        from core.clone.support import host_suffix
        self.assertEqual(host_suffix("i1.hdslb.com"), "hdslb.com")
        self.assertEqual(host_suffix("img.alicdn.com.cn"), "alicdn.com.cn")
        self.assertEqual(host_suffix("a.b.co.uk"), "b.co.uk")
        self.assertEqual(host_suffix("127.0.0.1"), "127.0.0.1")
        self.assertEqual(host_suffix("localhost"), "localhost")

    def test_map_builder(self):
        import json
        from core.clone.support import img_shard_map, img_shard_script
        items = [
            (f"http://i0.cdn.test/bfs/archive/{HASH}.jpg@100w.webp", "/static/img/s.webp", 10),
            (f"http://i0.cdn.test/bfs/archive/{HASH}.jpg@900w.webp", "/static/img/l.webp", 900),
            ("https://site.test/own.png", "/static/img/own.png", 5),       # 页面自身主机: 本就本地化
            ("http://i0.cdn.test/api/feed", "/static/misc/f.json", 5),     # 非图片
        ]
        js = img_shard_script(img_shard_map(items, "site.test"))
        m = json.loads(js.split("var G=", 1)[1].split(";var SL=", 1)[0])
        self.assertEqual(list(m), ["cdn.test"])
        self.assertEqual(m["cdn.test"]["b"][f"/bfs/archive/{HASH}.jpg"], ["i0.cdn.test", "/static/img/l.webp"])
        self.assertEqual(len(m["cdn.test"]["e"]), 2)
        self.assertEqual(img_shard_script(img_shard_map(items[2:], "site.test")), "")

    def test_runtime_rewrite(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("无 playwright")
        from core.clone.support import img_shard_map, img_shard_script
        items = [(f"http://i0.cdn.test/bfs/archive/{HASH}.jpg@672w_378h_1c_!cover.avif", "/static/img/a.png", 99),
                 ("http://i0.cdn.test/bfs/static/logo.png@48w.avif", "/static/img/logo.png", 9)]
        shim = img_shard_script(img_shard_map(items, "site.test"))
        html = ("<html><head>" + shim + "</head><body>"
                f"<img id=p src='//i2.cdn.test/bfs/archive/{HASH}.jpg@976w.avif'>"
                "<div id=box></div><script>"
                f"var a=new Image();a.id='js';a.src='http://i1.cdn.test/bfs/archive/{HASH}.jpg@672w_378h_1c.avif';"
                "document.body.appendChild(a);"
                f"document.getElementById('box').innerHTML=\"<img id=h src='https://i1.cdn.test/bfs/archive/{HASH}.jpg'>\";"
                "var c=document.createElement('img');c.id='cross';c.setAttribute('src','http://i1.cdn.test/bfs/static/logo.png@48w.avif');"
                "document.body.appendChild(c);"
                "var d=document.createElement('img');d.id='same';d.src='http://i0.cdn.test/bfs/static/logo.png@96w.avif';"
                "document.body.appendChild(d);"
                "</script></body></html>")
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch()
        except Exception as exc:
            self.skipTest(f"无 chromium: {exc}")
        try:
            page = browser.new_page()
            seen = []

            def handle(route):
                url = route.request.url
                seen.append(url)
                if url == "http://site.test/":
                    route.fulfill(status=200, content_type="text/html", body=html)
                elif url.startswith("http://site.test/static/img/"):
                    route.fulfill(status=200, content_type="image/png", body=PNG_1x1)
                else:
                    route.abort()
            page.route("**/*", handle)
            page.goto("http://site.test/", wait_until="load")
            page.wait_for_timeout(300)
            src = {i: page.get_attribute("#" + i, "src") for i in ("p", "js", "h", "cross", "same")}
            self.assertEqual(src["p"], "/static/img/a.png")        # 解析器插入: 换分片主机 + 换后缀
            self.assertEqual(src["js"], "/static/img/a.png")       # .src 赋值
            self.assertEqual(src["h"], "/static/img/a.png")        # innerHTML 插入, 无后缀底图
            self.assertTrue(src["cross"].startswith("http://i1."))  # 跨主机且无内容哈希: 不串号
            self.assertEqual(src["same"], "/static/img/logo.png")  # 同主机同底图: 变体就近
            self.assertTrue(page.evaluate("document.getElementById('js').naturalWidth") > 0)
            # 脚本插入/赋值的图片不再外发(解析器插入的那张由预加载扫描器先发, 由 HTML 侧改写覆盖)
            self.assertFalse([u for u in seen if "cdn.test" in u and HASH in u and "976w" not in u])

        finally:
            browser.close()
            pw.stop()

    def test_html_rewrite(self):
        from core.clone.support import img_shard_map, rewrite_img_shards
        g = img_shard_map([(f"http://i0.cdn.test/bfs/archive/{HASH}.jpg@672w.avif", "/static/img/a.avif", 5),
                           ("http://i0.cdn.test/bfs/static/logo.png", "/static/img/logo.png", 5)], "site.test")
        html = (f'<img alt=x src="//i1.cdn.test/bfs/archive/{HASH}.jpg@976w_550h_!cover" '
                f'srcset="http://i2.cdn.test/bfs/archive/{HASH}.jpg@2x.avif 2x, /local/y.png 1x">'
                '<img src="//i1.cdn.test/bfs/static/logo.png"><script src="//i1.cdn.test/bfs/x.js"></script>'
                f'<a href="//i1.cdn.test/bfs/archive/{HASH}.jpg">link</a>')
        out, n = rewrite_img_shards(html, g)
        self.assertEqual(n, 2)
        self.assertIn('src="/static/img/a.avif" srcset="/static/img/a.avif 2x, /local/y.png 1x"', out)
        self.assertIn('<img src="//i1.cdn.test/bfs/static/logo.png">', out)   # 跨主机无哈希不动
        self.assertIn(f'<a href="//i1.cdn.test/bfs/archive/{HASH}.jpg">', out)  # 只动 img/source


class UrlReplBoundary(unittest.TestCase):
    """无协议键的左边界: 挡住长路径中段误改, 但独立的 ./ ../ 相对前缀照旧替换(与旧行为一致)。"""

    def test_cases(self):
        from core.clone.support import apply_url_replacements
        pairs = sorted([("/airpods-pro/", "/static/misc/x.bin"), ("/chunk-A.js", "/static/js/a.js"),
                        ("chunk-B.js", "/static/js/b.js")], key=lambda p: -len(p[0]))
        cases = {
            '"/105/media/us/airpods-pro/2025/large.mp4"': '"/105/media/us/airpods-pro/2025/large.mp4"',
            'from"./chunk-A.js"': 'from"./static/js/a.js"',
            'import(`../chunk-A.js`)': 'import(`../static/js/a.js`)',
            'from"./chunk-B.js"': 'from".//static/js/b.js"',
            'x/y./chunk-A.js': 'x/y./chunk-A.js',
            '"/airpods-pro/"': '"/static/misc/x.bin"',
            "url(chunk-B.js)": "url(/static/js/b.js)",
        }
        for src, want in cases.items():
            self.assertEqual(apply_url_replacements(src, pairs), want, src)


class MhtmlRetry(unittest.TestCase):
    """MHTML 快照首次超时: 探测 frame 状态进日志, 停止加载后重试一次拿到快照。"""

    def test_retry_after_timeout(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("无 playwright")
        import config
        from core.clone.capture import CaptureMixin
        from core.clone.support import bounded_call

        class Stub:
            logs: list = []

            def log(self, kind, msg):
                self.logs.append((kind, msg))

            def _diag_mark(self, what):
                pass
        stub = Stub()
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch()
        except Exception as exc:
            self.skipTest(f"无 chromium: {exc}")
        try:
            page = browser.new_page()
            page.set_content("<p>x</p><iframe srcdoc='<b>y</b>'></iframe>")
            cdp = page.context.new_cdp_session(page)
            impl = cdp._impl_obj
            with self.assertRaises(TimeoutError):
                bounded_call(cdp, impl.send('Page.captureSnapshot', {'format': 'mhtml'}), 0.0)
            snap = CaptureMixin._mhtml_retry(stub, page, cdp, impl)
            self.assertIn("MIME", snap["data"][:400])
            self.assertTrue(any("未返回" in m and "complete" in m for _k, m in stub.logs), stub.logs)
            self.assertEqual(page.evaluate("1 + 1"), 2)
            self.assertGreater(config.MHTML_RETRY_TIMEOUT_S, 0)
        finally:
            browser.close()
            pw.stop()
