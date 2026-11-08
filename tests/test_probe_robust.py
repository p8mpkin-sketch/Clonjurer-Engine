"""视口探针稳健口径: 两次原站加载之间变化的区域(轮播/实时内容)两侧同处遮掉。"""
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine_bench" / "real_sites"))

import viewport_probe as vp  # noqa: E402


def _page(path, banner, body=(40, 90, 200)):
    im = Image.new("RGB", (420, 1200), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 419, 239], fill=banner)          # 轮播位: 每次加载颜色不同
    for y in range(300, 1200, 60):
        d.rectangle([20, y, 400, y + 30], fill=body)     # 稳定正文
    im.save(path)


class DynamicMask(unittest.TestCase):
    def test_carousel_masked_body_scored(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            a, a2, b, bad = t / "a.png", t / "a2.png", t / "b.png", t / "bad.png"
            _page(a, (220, 30, 30))
            _page(a2, (30, 200, 30))
            _page(b, (30, 30, 220))                      # 克隆冻结到第三种轮播相位
            _page(bad, (30, 30, 220), body=(250, 180, 0))  # 正文也错
            rects, frac = vp.dynamic_rects(a, a2)
            self.assertTrue(rects)
            self.assertLess(frac, 0.4)
            self.assertTrue(all(r[1] < 264 for r in rects))   # 只遮轮播位(含一格膨胀)
            info = {"text": ""}
            good = vp.robust_scores(a, a2, b, t, "g", info, info, info)
            worse = vp.robust_scores(a, a2, bad, t, "w", info, info, info)
            self.assertGreater(good["robust"], 95)
            self.assertGreater(good["robust_ceiling"], 95)
            self.assertLess(worse["robust"], good["robust"] - 5)

    def test_identical_loads_no_mask(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            a, a2 = t / "a.png", t / "a2.png"
            _page(a, (220, 30, 30))
            _page(a2, (220, 30, 30))
            self.assertEqual(vp.dynamic_rects(a, a2), ([], 0.0))


if __name__ == "__main__":
    unittest.main()
