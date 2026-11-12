"""a11y 适配器: 窗口底色取子控件之间露出来的颜色, 不取整块面积众数(大片白色列表/编辑区会把众数拉成白)。"""
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from core.uir.adapters.a11y import tree_to_uir


def _leaf(role, b, name=""):
    return {"role": role, "name": name, "value": "", "bounds": b, "children": []}


class RootOpenBg(unittest.TestCase):
    def test_window_bg_is_uncovered_color(self):
        im = Image.new("RGB", (400, 300), (236, 236, 236))      # 窗口底(两个面板之间的分隔带)
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, 189, 299], fill=(255, 255, 255))     # 占大半面积的白色列表与编辑区
        d.rectangle([210, 0, 399, 299], fill=(255, 255, 255))
        tree = {"platform": "macos", "app": "t", "scale": 1.0, "window": [0, 0, 400, 300],
                "root": {"role": "AXWindow", "name": "", "value": "", "bounds": [0, 0, 400, 300],
                         "children": [_leaf("AXList", [0, 0, 190, 300]),
                                      _leaf("AXTextArea", [210, 0, 190, 300])]}}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.png"
            im.save(p)
            u = tree_to_uir(tree, p, embed_images=False)
        self.assertEqual(u.root.box["bg"].lower(), "#ececec")


if __name__ == "__main__":
    unittest.main()
