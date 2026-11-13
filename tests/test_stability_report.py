"""多轮稳定性报告(engine_bench/stability.py)的口径护栏。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine_bench"))
import stability  # noqa: E402


def _r(url, score, tab=90.0, status="completed", **kw):
    return {"url": url, "holdout": True, "status": status, "score": score, "interactive": score,
            "vp": {"desktop": 90.0, "tablet": tab, "mobile": 90.0}, **kw}


class StabilityReport(unittest.TestCase):
    def test_spread_and_classes(self):
        docs = [{"results": [_r("a", 98.0), _r("b", 90.0, tab=50.0), _r("c", 0, status="failed", error="Boom"),
                             _r("d", 0, status="failed", blocked="验证码")]},
                {"results": [_r("a", 98.5), _r("b", 95.0, tab=85.0),
                             _r("e", 0, status="failed", failure="net::ERR_NAME_NOT_RESOLVED")]}]
        res = stability.analyse(docs)
        self.assertEqual(res["classes"], {"ok": 4, "blocked": 1, "unreachable": 1, "engine_fail": 1})
        by = {r["url"]: r for r in res["sites"]}
        self.assertEqual(by["a"]["unstable"], [])
        self.assertIn("头条差 5.0", by["b"]["unstable"][0])
        self.assertTrue(any("tablet" in w for w in by["b"]["unstable"]))
        self.assertEqual(res["multi_sites"], 2)
        self.assertEqual(res["head_ok"], 1)
        self.assertIn("不稳定清单", stability.render(res))

    def test_preview_flip_flagged(self):
        docs = [{"results": [_r("a", 99.0, preview_track="static")]},
                {"results": [_r("a", 99.0, preview_track="interactive")]}]
        row = stability.analyse(docs)["sites"][0]
        self.assertTrue(row["preview_flip"])


if __name__ == "__main__":
    unittest.main()
