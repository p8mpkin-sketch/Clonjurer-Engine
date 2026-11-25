"""评分补齐区口径: 两图都没有内容的分段尾部既不扣分也不白送分(Top1000 action.com 逐像素差 0.13% 被评 88.9)。"""
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from core.clone.support import compute_visual_similarity


def _page(path, last_row, body_shift=0, h=1080):
    im = Image.new("RGB", (1920, h), (250, 250, 250))
    d = ImageDraw.Draw(im)
    for i in range(12):
        d.rectangle((100 + body_shift, 60 + i * 80, 900 + body_shift, 100 + i * 80), fill=(30, 60, 120))
    d.line((0, h - 1, 1920, h - 1), fill=last_row)
    im.save(path)


class ScorePadding(unittest.TestCase):
    def test_bottom_row_artifact_not_penalised(self):
        with tempfile.TemporaryDirectory() as t:
            a, b = Path(t) / "a.png", Path(t) / "b.png"
            _page(a, (250, 250, 250))
            _page(b, (200, 200, 200))          # 只有最后一行不同
            self.assertGreater(compute_visual_similarity(a, b)["score"], 93)

    def test_padding_gives_no_free_credit(self):
        with tempfile.TemporaryDirectory() as t:
            a, b, c = Path(t) / "a.png", Path(t) / "b.png", Path(t) / "c.png"
            _page(a, (250, 250, 250))
            Image.new("RGB", (1920, 1080), (250, 250, 250)).save(b)   # 克隆整页空白
            blank = compute_visual_similarity(a, b)["structure"]
            self.assertLess(blank, 0.6)          # 补齐区不再按"双平坦同色 = 1"冲淡空白页的失分


if __name__ == "__main__":
    unittest.main()
