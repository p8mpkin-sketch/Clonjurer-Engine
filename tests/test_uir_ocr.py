"""本地 OCR 文字层护栏(core/uir/ocr.py): 几何分配是纯函数, 不依赖 OCR 后端与浏览器。"""
import unittest

from core.uir import ocr
from core.uir.adapters.vision import merge_fill
from core.uir.model import UIR, Node, Tokens


def _text(top, left, w, h, ref="ts16"):
    return Node(role="text", layout={"mode": "absolute"}, text_ref=ref, content={"text": "占位占位"},
                box={"position": {"top": top, "left": left}, "w": w, "h": h, "overflow": "hidden"})


def _uir(blocks):
    root = Node(role="box", layout={"mode": "stack"}, box={"w": 800, "h": 600, "position": {"mode": "relative"}},
                children=blocks)
    return UIR(root=root, tokens=Tokens(text_style={"ts16": {"size": 16, "weight": 400, "line": "20px"}}),
               meta={"seg": "v2"})


def _block(top, left, kids):
    return Node(role="box", layout={"mode": "absolute"}, box={"position": {"top": top, "left": left}, "w": 700, "h": 100},
                children=kids)


class OcrAssign(unittest.TestCase):
    def test_line_split_across_slots_by_x(self):
        # 导航: 一条 OCR 行横跨三个文字段(块内相对坐标 + 块偏移)
        a, b, c = _text(10, 0, 32, 20), _text(10, 80, 32, 20), _text(10, 160, 64, 20)
        u = _uir([_block(100, 50, [a, b, c])])
        line = {"x0": 50, "y0": 112, "x1": 274, "y1": 128, "text": "首页", "conf": 0.9}
        # 行内三段文字: 用三条 OCR 行(分别覆盖)与一条整行都应正确
        lines = [dict(line, x1=82), dict(line, x0=130, x1=162, text="产品"), dict(line, x0=210, x1=274, text="服务支持")]
        n = ocr.apply_ocr(u, lines)
        self.assertEqual(n, 3)
        self.assertEqual([a.content["text"], b.content["text"], c.content["text"]], ["首页", "产品", "服务支持"])
        self.assertTrue(all(x.content.get("ocr") for x in (a, b, c)))

    def test_one_long_line_is_sliced(self):
        a, b = _text(0, 0, 64, 20), _text(0, 96, 64, 20)
        u = _uir([_block(0, 0, [a, b])])
        n = ocr.apply_ocr(u, [{"x0": 0, "y0": 2, "x1": 160, "y1": 18, "text": "开源产品价格案例", "conf": 0.9}])
        self.assertEqual(n, 2)
        self.assertEqual(a.content["text"] + b.content["text"], "开源产品价格案例")   # 段间空隙里的字归最近段, 不丢
        self.assertTrue(a.content["text"].startswith("开源"))
        self.assertTrue(b.content["text"].endswith("案例"))

    def test_low_confidence_and_far_lines_ignored(self):
        a = _text(0, 0, 64, 20)
        u = _uir([_block(0, 0, [a])])
        n = ocr.apply_ocr(u, [{"x0": 0, "y0": 2, "x1": 60, "y1": 18, "text": "错字", "conf": 0.1},
                              {"x0": 0, "y0": 300, "x1": 60, "y1": 318, "text": "远处", "conf": 0.9}])
        self.assertEqual(n, 0)
        self.assertEqual(a.content["text"], "占位占位")

    def test_letter_spacing_fits_width(self):
        a = _text(0, 0, 200, 20)
        u = _uir([_block(0, 0, [a])])
        ocr.apply_ocr(u, [{"x0": 0, "y0": 2, "x1": 200, "y1": 18, "text": "四个汉字", "conf": 0.9}])
        # 4 字 × 16px = 64 → 补到 200: 每字 +34 → 封顶 0.6em = 9.6
        self.assertAlmostEqual(a.box["letter_spacing"], 9.6, places=1)

    def test_model_fill_does_not_override_ocr_block(self):
        a, b = _text(0, 0, 64, 20), _text(30, 0, 64, 20)
        blk = _block(0, 0, [a, b])
        u = _uir([blk])
        ocr.apply_ocr(u, [{"x0": 0, "y0": 2, "x1": 64, "y1": 18, "text": "真实文字", "conf": 0.9}])
        merge_fill(u, {"blocks": [{"id": "b1", "role": "card", "texts": ["模型一", "模型二"]}]})
        self.assertEqual(a.content["text"], "真实文字")
        self.assertEqual(blk.role, "card")    # 角色仍由模型给


if __name__ == "__main__":
    unittest.main()
