"""版面分析 v2 护栏(core/uir/adapters/layout.py): 纯函数与确定性, 不依赖浏览器。"""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from core.uir.adapters import layout as L


def _page(path: Path):
    im = Image.new("RGB", (480, 320), (250, 250, 250))
    d = ImageDraw.Draw(im)
    # 带 1px 边框 + 圆角的白卡片, 底边外多一行投影
    d.rounded_rectangle((40, 40, 260, 200), radius=8, fill=(255, 255, 255), outline=(229, 231, 235))
    d.line((48, 201, 252, 201), fill=(244, 244, 244))
    for i, y in enumerate((70, 100, 130)):
        d.rectangle((60, y, 60 + 120 + i * 20, y + 12), fill=(30, 30, 30))   # 文字行代理(实心墨迹)
    # 深色底板按钮 + 白字
    d.rounded_rectangle((300, 60, 420, 100), radius=6, fill=(37, 99, 235))
    d.rectangle((330, 74, 390, 86), fill=(255, 255, 255))
    im.save(path)


class LayoutV2(unittest.TestCase):
    def test_border_with_shadow_row(self):
        a = np.full((60, 80, 3), 255, np.uint8)
        a[0, :] = (229, 231, 235)
        a[:, 0] = a[:, -1] = (229, 231, 235)
        a[-2, :] = (229, 231, 235)
        a[-1, :] = (244, 244, 244)   # 投影行: 不该被当成边框色
        bd = L._border(a, np.array([255, 255, 255], np.float32))
        self.assertIsNotNone(bd)
        self.assertEqual(bd[3], (0, 1, 0, 0))
        self.assertLessEqual(L._cdiff(bd[1], (229, 231, 235)), 2)

    def test_plate_found_around_text(self):
        a = np.full((60, 200, 3), 15, np.uint8)            # 深底
        a[10:50, 40:160] = (30, 41, 59)                     # 按钮底板(与底色差 < 前景阈值)
        a[24:36, 70:130] = 255                              # 白字
        p = L._plate(a, np.array([15, 15, 15], np.float32), 24, 36, 70, 130)
        self.assertIsNotNone(p)
        self.assertEqual(p[:4], (10, 50, 40, 160))

    def test_split_mixed_avatar_then_text(self):
        fg = np.zeros((24, 120), bool)
        fg[:, 0:24] = True                                   # 头像 24px 高
        for x in range(34, 110, 6):
            fg[8:18, x:x + 4] = True                         # 10px 高的一行小字
        segs = L._split_mixed(fg, 0, 120)
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0], (0, 24))

    def test_segment_deterministic_and_shaped(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "p.png"
            _page(p)
            u1 = L.segment_v2(p, asset_dir=Path(td) / "img")
            u2 = L.segment_v2(p, asset_dir=Path(td) / "img2")
            self.assertEqual(json.dumps(u1.to_dict(), sort_keys=True), json.dumps(u2.to_dict(), sort_keys=True))
            self.assertEqual(u1.meta["seg"], "v2")
            self.assertEqual(u1.validate(), [])
            boxes = [n.box for n in u1.root.children]
            self.assertTrue(any(b.get("border") for b in boxes), "卡片边框未检出")
            texts = [c for n in u1.root.children for c in n.walk() if c.role == "text"]
            self.assertGreaterEqual(len(texts), 4)
            plates = [c for n in u1.root.children for c in n.walk()
                      if c.role == "box" and c.box.get("bg") and any(k.role == "text" for k in c.children)]
            self.assertTrue(plates, "按钮底板未检出")

    def test_no_crop_when_cap_zero(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "p.png"
            im = Image.fromarray((np.random.RandomState(0).rand(200, 300, 3) * 255).astype(np.uint8))
            im.save(p)
            seg = L._Seg(np.asarray(im), im, Path(td) / "img", 500, pic_cap=0.0)
            seg.rec(0, 200, 0, 300, None)
            self.assertEqual(seg.pics, 0)


if __name__ == "__main__":
    unittest.main()
