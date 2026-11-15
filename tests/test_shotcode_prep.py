"""截图轨确定性预处理: 设备像素比估计、浏览器外框候选分界线、素材框吸附。"""
import unittest

import numpy as np
from PIL import Image, ImageDraw

from core.uir import shotcode


class ShotPrepTest(unittest.TestCase):
    def test_dpr(self):
        self.assertEqual(shotcode.estimate_dpr(1440, 900), 1)
        self.assertEqual(shotcode.estimate_dpr(2844, 2008), 2)       # Retina 整窗截图
        self.assertEqual(shotcode.estimate_dpr(3840, 2160), 2)
        self.assertEqual(shotcode.estimate_dpr(1170, 2532), 3)       # 手机 3 倍竖图
        self.assertEqual(shotcode.estimate_dpr(390, 844), 1)

    def test_chrome_candidates(self):
        img = Image.new("RGB", (800, 600), "#f2f2f2")
        d = ImageDraw.Draw(img)
        d.rectangle([0, 46, 800, 600], fill="#ffffff")               # 外框(浅灰) / 网页(白) 分界在 46
        d.line([(0, 46), (800, 46)], fill="#c8c8c8")
        d.rectangle([0, 90, 800, 105], fill="#111111")               # 网页自己的深色通栏
        d.text((20, 300), "hello", fill="#000")
        ys = shotcode.chrome_candidates(img)
        self.assertTrue(any(abs(y - 46) <= 2 for y in ys), ys)
        self.assertTrue(any(abs(y - 90) <= 2 for y in ys), ys)

    def test_snap_box(self):
        g = np.full((400, 600), 240.0, dtype=np.float32)
        g[100:300, 150:400] = 60.0                                   # 一张"图片"
        self.assertEqual(shotcode.snap_box(g, [140, 92, 410, 310]), [150, 100, 400, 300])
        # 小图不吸附, 原样返回
        self.assertEqual(shotcode.snap_box(g, [10, 10, 40, 40]), [10, 10, 40, 40])

    def test_content_fraction(self):
        good = Image.new("RGB", (40, 40), "#ffffff")
        ImageDraw.Draw(good).ellipse([2, 2, 38, 38], fill="#3a7bd5")         # 框准的圆形头像
        bad = Image.new("RGB", (40, 40), "#ffffff")
        ImageDraw.Draw(bad).rectangle([34, 0, 40, 40], fill="#3a7bd5")      # 框偏了只带到一条边
        self.assertGreater(shotcode.content_fraction(good), 0.45)
        self.assertLess(shotcode.content_fraction(bad), 0.45)


if __name__ == "__main__":
    unittest.main()
