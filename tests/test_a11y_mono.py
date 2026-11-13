"""a11y 适配器: 多行文本按各行墨迹宽认等宽字体(代码编辑器/日志类控件), 换等宽字体令牌并补字间距。"""
import unittest

from core.uir.adapters.a11y import _PROP_W, _mono_advance


def _spans(rows, adv=None, size=12.0, x0=10.0):
    out = []
    for r in rows:
        w = len(r) * adv if adv else sum(_PROP_W.get(ch, 556) for ch in r) / 1000.0 * size
        out.append((x0, x0 + w - 0.6))
    return out


ROWS = ["# Draft notes", "ship the capture workflow", "check the scores on the new samples",
        "- item one: gather inputs", "total = 3 items"]


class MonoAdvance(unittest.TestCase):
    def test_monospace_rows_detected(self):
        text = "\n".join(ROWS[:3]) + "\n\n" + "\n".join(ROWS[3:])
        adv = _mono_advance(text, {"spans": _spans(ROWS, adv=6.6)})
        self.assertIsNotNone(adv)
        self.assertAlmostEqual(adv, 6.6, delta=0.05)

    def test_proportional_rows_rejected(self):
        self.assertIsNone(_mono_advance("\n".join(ROWS), {"spans": _spans(ROWS)}))

    def test_extra_scrollbar_rows_tolerated(self):
        sp = _spans(ROWS, adv=7.0)
        sp = sp[:2] + [(600.0, 608.0)] + sp[2:] + [(11.0, 590.0)]   # 滚动箭头行、横向滚动条
        self.assertAlmostEqual(_mono_advance("\n".join(ROWS), {"spans": sp}), 7.0, delta=0.05)

    def test_too_few_rows_or_mismatch(self):
        self.assertIsNone(_mono_advance("\n".join(ROWS[:2]), {"spans": _spans(ROWS[:2], adv=7.0)}))
        self.assertIsNone(_mono_advance("\n".join(ROWS), {"spans": _spans(ROWS[:3], adv=7.0)}))


if __name__ == "__main__":
    unittest.main()
