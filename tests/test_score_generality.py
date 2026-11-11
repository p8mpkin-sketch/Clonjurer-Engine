"""通用性评分脚本口径: 多次运行取中位数、窄屏取平板/手机均值、缺项不臆造总分。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine_bench"))
import score_generality as sg  # noqa: E402


def _run(scores):
    return {"results": [{"url": u, "holdout": h, "status": "completed", "score": s, "interactive": i,
                         "vp": {"desktop": 90, "tablet": t, "mobile": m}} for u, h, s, i, t, m in scores]}


class ScoreGenerality(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _w(self, name, doc):
        p = self.tmp / name
        p.write_text(json.dumps(doc), encoding="utf-8")
        return str(p)

    def test_median_and_narrow(self):
        a = self._w("a.json", _run([("x", True, 80, 70, 70, 60), ("y", True, 100, 100, 90, 90), ("z", False, 99, 99, 99, 99)]))
        b = self._w("b.json", _run([("x", True, 90, 60, 80, 70), ("y", True, 100, 100, 90, 90)]))
        c = self._w("c.json", _run([("x", True, 40, 40, 20, 20)]))
        res = sg.corpus_part(sg._load([a, b, c]))
        self.assertEqual(res["sites"]["x"]["headline"], 80)   # 中位数, 不是均值
        self.assertEqual(res["headline"], 90.0)
        self.assertEqual(res["narrow"], 77.5)                 # (平板 80 + 手机 75) / 2
        self.assertEqual(res["tuned_headline"], 99)
        self.assertEqual(res["track_gap_max"], 20)   # x: 头条中位 80 - 交互中位 60

    def test_blocked_excluded_and_partial_total(self):
        doc = _run([("x", True, 90, 90, 90, 90)])
        doc["results"].append({"url": "w", "holdout": True, "status": "completed", "score": 10, "blocked": "login"})
        a = self._w("a.json", doc)
        res = sg.score(sg.argparse.Namespace(corpus=[a], cases=[], l1=[], a11y=[], xtarget=[], blind=[], gate=95))
        self.assertEqual(res["parts"]["headline"], 90)
        self.assertIsNone(res["total"])                       # 缺项时不出总分
        self.assertEqual(res["weight_covered"], 0.55)

    def test_cases_pass_rate(self):
        doc = {"rows": [{"case": "a", "vp": {"t": 96, "m": 99}}, {"case": "b", "vp": {"t": 94, "m": 99}},
                        {"case": "c", "vp": {"t": 99, "m": None}}]}
        res = sg.cases_part([doc], 95)
        self.assertEqual((res["pass"], res["n"]), (1, 3))
        self.assertEqual(res["failing"], ["b", "c"])

    def test_l2l3_coverage_penalty(self):
        res = sg.l2l3_part([{"summary": {"holdout": {"with_shot": 80}}}], [{"summary": {"holdout": {"flutter": 80}}}])
        self.assertLess(res["score"], 80)
        self.assertGreater(res["coverage"], 0)


if __name__ == "__main__":
    unittest.main()
