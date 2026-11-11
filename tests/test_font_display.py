"""font-display:optional|fallback 在离线克隆里会让迟到的网页字体永远不换上(qwik 手机端 41/100 双峰), 交付时放宽为 swap。"""
import unittest

from core.clone.support import relax_font_display


class FontDisplay(unittest.TestCase):
    def test_relax(self):
        css = "@font-face{font-family:P;font-display:optional;src:url(a.woff2)}@font-face{font-display : fallback}b{font-display:swap}c{font-display:block}"
        out = relax_font_display(css)
        self.assertEqual(out.count("swap"), 3)
        self.assertIn("font-display:block", out)
        self.assertNotIn("optional", out)
        self.assertEqual(relax_font_display(b"x{FONT-DISPLAY:Optional}"), b"x{FONT-DISPLAY:swap}")
        self.assertEqual(relax_font_display("font-display-x: optional"), "font-display-x: optional")


if __name__ == "__main__":
    unittest.main()
