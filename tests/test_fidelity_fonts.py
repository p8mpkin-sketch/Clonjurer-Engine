"""保真轨补字体(scoring._fidelity_font_css): MHTML 不打包网页字体, 回放前按工作空间本地化样式表里的 @font-face 注入。"""
import tempfile
import unittest
from pathlib import Path

from core.clone.scoring import ScoringMixin


class _Fake(ScoringMixin):
    def __init__(self, ws):
        self.ws = ws


class FidelityFonts(unittest.TestCase):
    def test_collects_localized_fontfaces_as_data_urls(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "static" / "css").mkdir(parents=True)
            (ws / "static" / "fonts").mkdir(parents=True)
            (ws / "static" / "fonts" / "a.woff2").write_bytes(b"wOF2fake")
            (ws / "static" / "fonts" / "a.woff").write_bytes(b"wOFFfake")
            (ws / "static" / "css" / "x.css").write_text(
                "@font-face{font-family:'SF Pro';font-weight:600;src:url(/static/fonts/a.woff) format('woff'),"
                "url('/static/fonts/a.woff2') format('woff2');unicode-range:U+0-FF}"
                "@font-face{font-family:Remote;src:url(https://cdn.example.com/r.woff2)}"
                "body{font-family:'SF Pro'}", encoding="utf-8")
            css, n = _Fake(ws)._fidelity_font_css()
            self.assertEqual(n, 1)                         # 远程未本地化的不注入
            self.assertIn("font-family:'SF Pro'", css)
            self.assertIn("font-weight:600", css)
            self.assertIn("unicode-range:U+0-FF", css)
            self.assertIn("data:font/woff2;base64,", css)  # 同一声明里优先 woff2
            self.assertNotIn("/static/fonts/", css)

    def test_no_fonts(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(_Fake(Path(d))._fidelity_font_css(), ("", 0))


if __name__ == "__main__":
    unittest.main()
