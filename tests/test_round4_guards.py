"""2026-11-04 冲分轮的整类修复护栏:

- srcset 按 HTML 规范切分(URL 内部逗号属于 URL: WordPress/Jetpack `?resize=w,h`);
- 带查询串的相对引用整串登记替换(同端点不同参数不再被去参 stem 改成同一张图);
- 评分: 同宽两图先在原分辨率补齐再缩放(内容相同只多几行时不再因取整错位失分);
- 识别: 运行时证据只在字节+DOM 判成弱类时改判, 并抬高互证置信度。
"""
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import cloner_engine as ce  # noqa: E402
from core import pattern_detector as pd  # noqa: E402


class SrcsetSpec(unittest.TestCase):
    def test_commas_inside_url(self):
        v = ("https://a.com/x.jpg?w=4000 4000w, https://a.com/x.jpg?resize=150,100 150w, "
             "https://a.com/x.jpg?resize=300,200 300w")
        self.assertEqual(ce._srcset_entries(v), [
            ("https://a.com/x.jpg?w=4000", 4000),
            ("https://a.com/x.jpg?resize=150,100", 150),
            ("https://a.com/x.jpg?resize=300,200", 300)])

    def test_plain_lists(self):
        self.assertEqual(ce._srcset_entries("a.jpg 1x,b.jpg 2x"), [("a.jpg", 1), ("b.jpg", 2)])
        self.assertEqual(ce._srcset_entries("a.jpg, b.jpg 2x"), [("a.jpg", 1), ("b.jpg", 2)])


class QueryVariants(unittest.TestCase):
    def test_relative_query_refs_registered(self):
        pairs = {}
        items = [("https://s.com/_next/image?url=%2Fa.png&w=640", "/static/img/a.png"),
                 ("https://s.com/_next/image?url=%2Fb.png&w=640", "/static/img/b.png")]
        ce.ClonerEngine._fill_variants_bulk(pairs, items, "s.com")
        self.assertEqual(pairs["/_next/image?url=%2Fa.png&w=640"], "/static/img/a.png")
        self.assertEqual(pairs["/_next/image?url=%2Fb.png&w=640"], "/static/img/b.png")
        self.assertEqual(pairs["//s.com/_next/image?url=%2Fb.png&w=640"], "/static/img/b.png")
        # 替换按长度降序: 整串优先于去参 stem, 两张图各归各的
        ordered = sorted(pairs.items(), key=lambda kv: -len(kv[0]))
        html = ('<img srcset="/_next/image?url=%2Fa.png&w=640 640w">'
                '<img srcset="/_next/image?url=%2Fb.png&w=640 640w">')
        out = ce.apply_replacements(html, ordered)
        self.assertIn("/static/img/a.png 640w", out)
        self.assertIn("/static/img/b.png 640w", out)


class PadBeforeScale(unittest.TestCase):
    def test_extra_rows_do_not_misalign(self):
        im = Image.new("RGB", (1920, 6905), "white")
        d = ImageDraw.Draw(im)
        for y in range(0, 6905, 37):
            d.text((40 + (y % 400), y), "Lorem ipsum dolor sit amet 0123456789", fill="black")
        longer = Image.new("RGB", (1920, 6910), "white")
        longer.paste(im, (0, 0))
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.png", Path(tmp) / "b.png"
            im.save(a)
            longer.save(b)
            self.assertGreaterEqual(ce.compute_visual_similarity(a, b)["score"], 99.0)


class LiveFrameStaticRefs(unittest.TestCase):
    def test_styles_and_media_only(self):
        html = ('<script src="//s.com/vue.js"></script><link rel="modulepreload" href="/m.mjs">'
                '<link rel="stylesheet" href="//s.com/a.css"><img src="//qr.s.com/show?a=1&amp;t=2">'
                '<div style="background:url(i/x.png)"></div>')
        refs = ce._static_refs(html, "https://p.s.com/uc/login")
        self.assertEqual(refs["urls"], ["https://s.com/a.css", "https://qr.s.com/show?a=1&t=2",
                                        "https://p.s.com/uc/i/x.png"])


class RuntimeEvidence(unittest.TestCase):
    def _ws(self, html):
        tmp = tempfile.mkdtemp()
        (Path(tmp) / "index.html").write_text(html, encoding="utf-8")
        return Path(tmp)

    def test_rescues_weak_pattern(self):
        ws = self._ws("<html><body><div id=root></div></body></html>")
        self.assertEqual(pd.detect(ws)["pattern"], "M-STATIC 纯静态")
        r = pd.detect(ws, runtime=["React"])
        self.assertEqual(r["pattern"], "M-React SPA")
        self.assertEqual(r["confidence"], 0.93)
        self.assertEqual(r["evidence"]["runtime"], ["React"])

    def test_does_not_override_strong_pattern(self):
        ws = self._ws('<html><body><div id="__next"></div></body></html>')
        r = pd.detect(ws, runtime=["Vue", "Next"])
        self.assertEqual(r["pattern"], "M-Next SSR")
        self.assertEqual(r["confidence"], 0.97)   # DOM + 运行时互证

    def test_unknown_runtime_names_ignored(self):
        ws = self._ws("<html><body></body></html>")
        self.assertEqual(pd.detect(ws, runtime=["Evil<script>"])["pattern"], "M-STATIC 纯静态")


if __name__ == "__main__":
    unittest.main()


class BlockedPageDetect(unittest.TestCase):
    """环境受阻(风控/限流/人机验证)单独标记, 不混进引擎均分。"""

    REAL = "京东\n搜索\n" + "\n".join(f"频道{i}/商品类目{i}/推荐内容{i}" for i in range(80))

    def test_baseline_is_block_page(self):
        orig = "全部频道\n内容太火爆了，请稍后再试\n登录领限时福利\n当前页面异常\n刷新\n切换账号"
        self.assertIn("基准页即拦截页", ce.detect_blocked_page(orig, orig))
        self.assertIn("基准页即拦截页", ce.detect_blocked_page("Just a moment...\nChecking your browser", ""))
        # Cloudflare Turnstile 新版挑战页(人机勾选框在 iframe 里, 主文档只有提示语)
        cf = ("www.npmjs.com\nPerforming security verification\nThis website uses a security service to protect "
              "against malicious bots.\nRay ID: a562\nPerformance and Security by Cloudflare\nPrivacy")
        self.assertIn("基准页即拦截页", ce.detect_blocked_page(cf, cf))

    def test_capture_replaced_midway(self):
        # 原站局部模块也带同样话术(基准里有), 但采集文本骤缩成拦截页: 仍判受阻
        orig = self.REAL + "\n内容太火爆了，请稍后再试"
        cap = "全部频道\n内容太火爆了，请稍后再试\n当前页面异常\n刷新\n切换账号"
        self.assertIn("采集中途被风控替换", ce.detect_blocked_page(orig, cap))

    def test_normal_pages_not_flagged(self):
        self.assertEqual(ce.detect_blocked_page(self.REAL, self.REAL), "")
        # 登录页本身带"验证码": 基准里就有, 不算受阻
        login = "账号登录\n手机号\n验证码\n获取验证码\n登录"
        self.assertEqual(ce.detect_blocked_page(login, login), "")
        # 采集文本少但没有风控话术(静态小站): 不算受阻
        self.assertEqual(ce.detect_blocked_page(self.REAL, "Example Domain"), "")

    def test_weak_phrase_needs_new_and_shrunk(self):
        cap = "系统繁忙，请稍后再试"
        self.assertIn("采集中途被风控替换", ce.detect_blocked_page(self.REAL, cap))
        self.assertEqual(ce.detect_blocked_page(self.REAL + "\n系统繁忙，请稍后再试", cap), "")


class BeaconLike(unittest.TestCase):
    def test_generic_beacon_shapes(self):
        from core.cloner_engine import beacon_like
        yes = ["https://track.example.net/se.gif?a=1",
               "https://example.net/log/web?x=1",
               "https://example.net/collect?v=2&tid=x",
               "https://stat.example.org/pv.gif",
               "https://cdn.example.org/p.gif?" + "a=1&" * 40]
        no = ["https://cdn.example.org/assets/app.3f2a.js",
              "https://cdn.example.org/img/banner.png",
              "https://example.org/static/css/main.css?v=3",
              "https://fonts.example.org/catalog/logo.woff2",
              "https://example.org/blog/how-to-track-time.html"]
        for u in yes:
            self.assertTrue(beacon_like(u), u)
        for u in no:
            self.assertFalse(beacon_like(u), u)
