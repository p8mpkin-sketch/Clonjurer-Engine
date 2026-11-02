"""第三阶段(提速): 新实现与冻结基线(engine_bench/perf/baseline)逐值等价的差分模糊测试。"""
import importlib.util
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

from core import cloner_engine as ce  # noqa: E402
from core import pattern_detector as pd  # noqa: E402
from core import pixel_calibrator as pc  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"baseline_{name}", ROOT / "engine_bench" / "perf" / "baseline" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B_CE, B_PD, B_PC = _load("cloner_engine"), _load("pattern_detector"), _load("pixel_calibrator")

NAV_TOKENS = ["window.", "window", "top", "parent", ".location", ".href", "location", " = ", "=",
              ";", "{", "}", "$", "_", ".", "a", " ", "\n", "window.open(", "u", ", '_self')",
              ",\"_top\")", "'_parent'", ")", "(", "x.top", "==", "中"]


def _fuzz(rnd, tokens, n):
    return "".join(rnd.choice(tokens) for _ in range(n))


class NavRewriteEquivalence(unittest.TestCase):
    def test_fuzz_matches_baseline(self):
        rnd = random.Random(42)
        for _ in range(4000):
            text = _fuzz(rnd, NAV_TOKENS, rnd.randint(0, 60))
            self.assertEqual(ce.rewrite_js_navigation(text), B_CE.rewrite_js_navigation(text), repr(text))

    def test_known_cases(self):
        for text in ("window.top.location = '/x';", "top.location.href=u", "a.top.location=1",
                     "$top.location=1", "window.parent.location.href = y", "parent.location=z",
                     "window.open(url, '_self')", "window.top.location=window.top.location=1"):
            self.assertEqual(ce.rewrite_js_navigation(text), B_CE.rewrite_js_navigation(text), text)


class ReplacementEquivalence(unittest.TestCase):
    def test_fuzz_sorted_pairs(self):
        rnd = random.Random(7)
        alphabet = "ab/:.?=h"
        for _ in range(1500):
            keys = {"".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 7)))
                    for _ in range(rnd.randint(1, 25))}
            pairs = sorted(((k, f"<{i}>") for i, k in enumerate(keys)), key=lambda kv: -len(kv[0]))
            text = "".join(rnd.choice(alphabet + "xy") for _ in range(rnd.randint(0, 120)))
            self.assertEqual(ce.apply_replacements(text, pairs), B_CE.apply_replacements(text, pairs))

    def test_unsorted_and_duplicate_pairs_keep_old_semantics(self):
        pairs = [("a", "1"), ("ab", "2"), ("a", "3"), ("abc", "4")]
        for text in ("abc", "aab", "xabcx"):
            self.assertEqual(ce.apply_replacements(text, pairs), B_CE.apply_replacements(text, pairs))

    def test_regex_metachars_and_unicode(self):
        keys = ["https://x.com/a(1).png?v=[2]", "https://x.com/a", "/中文/路径.js", "//x.com/a*b+c"]
        pairs = sorted(((k, f"/L{i}") for i, k in enumerate(keys)), key=lambda kv: -len(kv[0]))
        text = " ".join(keys) + " https://x.com/a(1).png?v=[3] /中文/路径.jsx"
        self.assertEqual(ce.apply_replacements(text, pairs), B_CE.apply_replacements(text, pairs))

    def test_very_long_key_does_not_crash(self):
        key = "https://x.com/" + "a" * 5000
        pairs = [(key, "/L"), ("https://x.com/a", "/S")]
        text = key + " https://x.com/aa"
        self.assertEqual(ce.apply_replacements(text, pairs), B_CE.apply_replacements(text, pairs))


class TextScoreEquivalence(unittest.TestCase):
    def test_fuzz(self):
        rnd = random.Random(3)
        toks = ["中", "文", "测", "试", "a", "B", "_", "1", " ", "ab", "Hello", "\n", "é", "ー", "\ud800", "😀", "\u212a", "İ", "\u9fff", "\u4e00"]
        for _ in range(800):
            a, b = _fuzz(rnd, toks, rnd.randint(0, 50)), _fuzz(rnd, toks, rnd.randint(0, 50))
            self.assertEqual(ce._text_tokens(a), B_CE._text_tokens(a))
            self.assertEqual(ce.text_f_score(a, b), B_CE.text_f_score(a, b))
            self.assertEqual(ce.text_overlap_score(a, b), B_CE.text_overlap_score(a, b))


def _page(seed, w, h, shift=0):
    rnd = random.Random(seed)
    im = Image.new("RGB", (w, h), tuple(rnd.randrange(200, 256) for _ in range(3)))
    d = ImageDraw.Draw(im)
    for _ in range(rnd.randint(0, 60)):
        x, y = rnd.randrange(w), rnd.randrange(h)
        d.rectangle([x + shift, y, x + shift + rnd.randrange(300), y + rnd.randrange(80)],
                    fill=tuple(rnd.randrange(256) for _ in range(3)))
    return im.filter(ImageFilter.GaussianBlur(rnd.choice([0, 0.5, 1.2])))


class VisualScoreEquivalence(unittest.TestCase):
    def test_random_pairs_identical_detail_and_diff(self):
        sizes = [(1440, 900), (1440, 9000), (1280, 2000), (375, 812), (800, 300), (1440, 16000)]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for i in range(14):
                rnd = random.Random(i)
                wa, ha = rnd.choice(sizes)
                wb, hb = (wa, ha) if rnd.random() < 0.5 else rnd.choice(sizes)
                a, b = tmp / f"a{i}.png", tmp / f"b{i}.png"
                _page(i, wa, ha).save(a)
                (_page(i, wb, hb, shift=rnd.randint(0, 5)) if rnd.random() < 0.7
                 else _page(i + 100, wb, hb)).convert(rnd.choice(["RGB", "RGBA", "L"])).save(b)
                da, db = tmp / f"da{i}.png", tmp / f"db{i}.png"
                ta, tb = "中文 hello world", rnd.choice(["", "中文 hello", "hello world 中文"])
                new = ce.compute_visual_similarity(a, b, da, ta, tb)
                old = B_CE.compute_visual_similarity(a, b, db, ta, tb)
                self.assertEqual(new, old, (i, (wa, ha), (wb, hb)))
                with Image.open(da) as x, Image.open(db) as y:
                    self.assertEqual(x.size, y.size)
                    self.assertEqual(x.convert("RGB").tobytes(), y.convert("RGB").tobytes())
                # 无 diff 输出路径(两图均命中缓存)
                self.assertEqual(ce.compute_visual_similarity(b, a), B_CE.compute_visual_similarity(b, a))
                # 修复循环常态: 参考图命中缓存 + 新克隆图(分带重采样路径)
                b2 = tmp / f"b2_{i}.png"
                _page(i + 7, wb, hb + rnd.randint(0, 999), shift=1).save(b2)
                self.assertEqual(ce.compute_visual_similarity(a, b2, da, ta, tb),
                                 B_CE.compute_visual_similarity(a, b2, db, ta, tb), (i, "band"))
                with Image.open(da) as x, Image.open(db) as y:
                    self.assertEqual(x.convert("RGB").tobytes(), y.convert("RGB").tobytes())
                # 同路径被改写: 内容寻址缓存必须失效
                _page(i + 9, wa, ha).save(a)
                self.assertEqual(ce.compute_visual_similarity(a, b2), B_CE.compute_visual_similarity(a, b2))

    def test_banded_diff_png_is_strictly_valid(self):
        import struct
        import zlib
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            a, b, d = tmp / "a.png", tmp / "b.png", tmp / "d.png"
            _page(1, 1440, 6000).save(a)
            _page(2, 1440, 5000).save(b)
            ce.compute_visual_similarity(a, b, d)
            data, pos, idat = d.read_bytes(), 8, b""
            while pos < len(data):
                ln = struct.unpack(">I", data[pos:pos + 4])[0]
                kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + ln]
                self.assertEqual(zlib.crc32(kind + body), struct.unpack(">I", data[pos + 8 + ln:pos + 12 + ln])[0])
                idat += body if kind == b"IDAT" else b""
                pos += 12 + ln
            with Image.open(d) as im:
                w, h = im.size
            self.assertEqual(len(zlib.decompress(idat)), h * (w * 3 + 1))   # 校验 adler32 与长度

    def test_edge_iou_and_ncc_segments(self):
        for seed in range(6):
            ga = _page(seed, 512, 512).convert("L")
            gb = _page(seed + 50, 512, 512).convert("L")
            self.assertEqual(ce._edge_iou(ga, gb), B_CE._edge_iou(ga, gb))
            self.assertEqual(ce._edge_iou(ga, ga), B_CE._edge_iou(ga, ga))


class CalibrationEquivalence(unittest.TestCase):
    def test_divergent_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for i, (w, h) in enumerate([(1440, 900), (1440, 3000), (1000, 700), (400, 200), (1450, 913)]):
                a, b = tmp / f"a{i}.png", tmp / f"b{i}.png"
                base = Image.new("RGB", (w, h), (245, 245, 245))
                ImageDraw.Draw(base).rectangle([0, 0, w, h // 7], fill=(20, 40, 160))
                base.save(a)
                other = _page(i, w if i % 2 else w - 37, h)
                other.save(b)
                self.assertEqual(pc.divergent_cells(a, b), B_PC.divergent_cells(a, b))
                self.assertEqual(pc.divergent_cells(b, a), B_PC.divergent_cells(b, a))
                _page(i + 30, w, h).save(b)   # 同路径改写: 内容寻址缓存不得陈旧
                self.assertEqual(pc.divergent_cells(a, b), B_PC.divergent_cells(a, b))


class PatternDetectEquivalence(unittest.TestCase):
    def test_fuzz_workspaces(self):
        snippets = [b"Vue.js v3", b"VUE.JS V2", b"@vue/runtime", b"__vue__", b"vue-material", b"react-dom",
                    b"REACT.PRODUCTION.min", b"ReactDOM", b"__NEXT_DATA__", b"next/dist", b"__NUXT__",
                    b"nuxt", b"zone.js", b"ng-version", b"@angular/core", b"jquery", b"webpackJsonp",
                    b"__webpack_require__", b"__vite__", b"vite/preload", b"x" * 50, b"function(){}",
                    b"\xc4\xb0nit", b"JQUERY", b"Vue.js vX", b"vue.js v9"]
        doms = ['<div data-v-1a2b3c4d>', '<div id="__next">', '<app-root ng-version="1">', '<iframe src=x>',
                '<video >', 'initGeetest', '<script data-clone-preview="x">react-dom</script>', '<p>plain</p>',
                'data-reactroot', 'md-theme-default', '__INITIAL_STATE__', '_nghost-abc',
                '<İframe>', 'ng-verſion', 'DATA-V-APP', '<VIDEO>', '_NGHOST-\u212a', '中文<p>']
        rnd = random.Random(11)
        for i in range(200):
            with tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp)
                jsdir = ws / "static" / "js"
                jsdir.mkdir(parents=True)
                for k in range(rnd.randint(0, 4)):
                    body = b" ".join(rnd.choice(snippets) for _ in range(rnd.randint(0, 6)))
                    (jsdir / f"{k}.js").write_bytes(body * rnd.randint(1, 3))
                html = "".join(rnd.choice(doms) for _ in range(rnd.randint(0, 4)))
                if rnd.random() < 0.3:
                    (ws / "index.html").write_text(html, encoding="utf-8")
                    self.assertEqual(pd.detect(ws), B_PD.detect(ws))
                self.assertEqual(pd.detect(ws, html=html), B_PD.detect(ws, html=html))


class UrlFastPathEquivalence(unittest.TestCase):
    def test_fuzz_against_urllib(self):
        from urllib.parse import urljoin, urlsplit
        rnd = random.Random(9)
        parts = ["http://", "https://", "HTTPS://", "//", "/", "a", "b.c", ":", "80", "?", "#", ";", "..", ".",
                 "%2e", " ", "\t", "x=1", "&", "@", "[", "]", "中", "~", "!", "'", "(", ")", "*", "+", ",", "-"]
        bases = ["https://s.com/p/q.html", "http://s.com/", "https://s.com", "", "ftp://x/y", "https://s.com/a/b/"]
        for _ in range(20000):
            url = "".join(rnd.choice(parts) for _ in range(rnd.randint(0, 10)))
            for base in bases:
                try:
                    ref = urljoin(base, url)
                except ValueError:
                    with self.assertRaises(ValueError):
                        ce._urljoin(base, url)
                    continue
                self.assertEqual(ce._urljoin(base, url), ref, (base, url))
            try:
                ref = urlsplit(url)
            except ValueError:
                continue
            self.assertEqual(ce._split_scheme_netloc_path(url), (ref.scheme, ref.netloc, ref.path), url)

    def test_variants_and_compose(self):
        for url in ("https://s.com/a/b.png?v=1", "https://cdn.x/a", "http://s.com:8080/x", "https://s.com"):
            self.assertEqual(ce.ClonerEngine._variants(url, "s.com"), B_CE.ClonerEngine._variants(url, "s.com"))


class ComposePairsEquivalence(unittest.TestCase):
    def _url(self, rnd, strict):
        hosts = ["s.com", "cdn.x.net", "S.COM", "a:8080", "u@h", "", "h"]
        segs = ["a", "b.png", "c;p=1", "", "..", "x y", "%20", "中", "q"]
        url = rnd.choice(["https://", "http://"] if strict else ["https://", "http://", "HTTP://", "//", "/", "ftp://", " https://"])
        url += rnd.choice(hosts)
        for _ in range(rnd.randint(0, 4)):
            url += "/" + rnd.choice(segs)
        if rnd.random() < 0.4:
            url += "?" + rnd.choice(["v=1", "", "a/b", "x?y"])
        if not strict and rnd.random() < 0.2:
            url += rnd.choice(["#f", "#", "\t", "[", "]"])
        return url

    def _engine(self, mod, raw_refs):
        eng = mod.ClonerEngine.__new__(mod.ClonerEngine)
        eng._target = "https://s.com/index.html"
        eng._raw_refs = dict(raw_refs)
        return eng

    def test_fuzz_matches_baseline(self):
        rnd = random.Random(21)
        for trial in range(400):
            strict = trial % 2 == 0
            urls = [self._url(rnd, strict and rnd.random() < 0.95) for _ in range(rnd.randint(0, 30))]
            results = {u: {"local": f"/static/x/{i}.bin"} for i, u in enumerate(urls)}
            srcset = {self._url(rnd, strict): rnd.choice(urls) if urls else "x" for _ in range(rnd.randint(0, 5))}
            raw = {"../" + str(i) + "/img.png": u for i, u in enumerate(urls[:5])}
            routes = {"/", "/login", "/a/b"}
            iframes = [(self._url(rnd, True), "<p>")] if rnd.random() < 0.3 else []
            frames = [(urls[0], "<p>", "t")] if urls and rnd.random() < 0.3 else []
            args = (results, routes, "https://s.com", srcset, iframes, frames)
            try:
                old = self._engine(B_CE, raw)._compose_pairs(*args)
            except ValueError:   # 非法 IPv6 主机: 两版都应抛出
                with self.assertRaises(ValueError):
                    self._engine(ce, raw)._compose_pairs(*args)
                continue
            self.assertEqual(self._engine(ce, raw)._compose_pairs(*args), old, (trial, urls))


class CssAndSrcsetEquivalence(unittest.TestCase):
    def test_css_refs_fuzz(self):
        rnd = random.Random(5)
        toks = ["url(", ")", "'", '"', "a.png", "../b.css", "data:x", "@import ", "image-set(", " 1x", ",",
                " ", "#f", "http://h/c.png", "//cdn/x.woff", "url( x.gif )", ";", "{", "}",
                "URL(", "Url( ", "@IMPORT ", "@Import url(", "IMAGE-SET(", "image-ſet(", "İ", "ı", "ſ", "url(url("]
        for _ in range(6000):
            css = _fuzz(rnd, toks, rnd.randint(0, 40))
            self.assertEqual(ce.parse_css_refs(css, "https://s.com/css/m.css"),
                             B_CE.parse_css_refs(css, "https://s.com/css/m.css"), css)

    def test_srcset_fuzz(self):
        rnd = random.Random(6)
        toks = ["a.png", " ", "1x", "2x", "100w", ",", " , ", "data:x", "/b.jpg", "http://h/c.webp", "0w", "x"]
        for _ in range(1500):
            v = _fuzz(rnd, toks, rnd.randint(0, 20))
            self.assertEqual(ce._parse_best_srcset(v, "https://s.com/p/"),
                             B_CE._parse_best_srcset(v, "https://s.com/p/"), v)


if __name__ == "__main__":
    unittest.main()
