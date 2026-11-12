"""a11y 适配器: 列表项「图标在左/字在右」拆分(列游程按空白列分团)与焦点虚线框不算墨迹。"""
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from core.uir.adapters.a11y import _Sampler, _text_units


def _cell(dotted: bool) -> Image.Image:
    im = Image.new("RGB", (220, 17), "white")
    px = im.load()
    for x in range(5, 17):                # 彩色方形图标 12x11
        for y in range(3, 14):
            px[x, y] = (209, 167, 42)
    for x in range(21, 66):               # 单行字: 9 行高的竖笔画, 每 3 列一笔
        if x % 3:
            for y in range(4, 13):
                px[x, y] = (20, 20, 20)
    if dotted:                            # 焦点虚线框(隔点), 从字的左边起围住文字
        for x in range(18, 220, 2):
            px[x, 0] = px[x, 16] = (0, 0, 0)
        for y in range(0, 17, 2):
            px[18, y] = (0, 0, 0)
    return im


class IconSplit(unittest.TestCase):
    def _split(self, dotted):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.png"
            _cell(dotted).save(p)
            s = _Sampler(p, 1.0)
            return s.icon_split(0, 0, 220, 17, s.bg(0, 0, 220, 17), _text_units("Invoices"))

    def test_icon_left_text_right(self):
        sp = self._split(False)
        self.assertIsNotNone(sp)
        icon, label = sp
        self.assertTrue(16 <= icon[2] <= 21 and label[0] == icon[2])

    def test_focus_dotted_rect_ignored(self):
        self.assertIsNotNone(self._split(True))


if __name__ == "__main__":
    unittest.main()
