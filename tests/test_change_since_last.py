"""差异区域与「与上次相比」: 相同截图判几乎没变, 局部改动给出区域框; 同网址上一次成功任务的查找。"""
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from core import diff_regions
from models import Database


class ChangeSinceLastTest(unittest.TestCase):
    def _shot(self, d: Path, banner: bool):
        d.mkdir(parents=True, exist_ok=True)
        im = Image.new("RGB", (1440, 2000), "#ffffff")
        dr = ImageDraw.Draw(im)
        dr.rectangle([0, 0, 1440, 80], fill="#1f2937")
        for i in range(6):
            dr.rectangle([100 + i * 210, 300, 280 + i * 210, 520], fill="#e5e7eb")
        if banner:
            dr.rectangle([200, 1200, 1240, 1420], fill="#dc2626")      # 新出现的横幅
        im.save(d / "original_preview.png")

    def test_same_and_changed(self):
        t = Path(tempfile.mkdtemp())
        self._shot(t / "a", False); self._shot(t / "b", False); self._shot(t / "c", True)
        same = diff_regions.site_change(t / "a", t / "b")
        self.assertEqual(same["regions"], [])
        self.assertIn("几乎没有变化", same["verdict"])
        ch = diff_regions.site_change(t / "a", t / "c")
        self.assertEqual(len(ch["regions"]), 1)
        r = ch["regions"][0]
        self.assertAlmostEqual(r["y"], 1200 / 2000, delta=0.02)
        self.assertAlmostEqual(r["x"], 200 / 1440, delta=0.03)
        self.assertIn("1 处变化", ch["verdict"])

    def test_previous_completed_task(self):
        db = Database(Path(tempfile.mkdtemp()) / "t.db")
        bid = db.create_batch("x", [("https://a.example/", "a.example"), ("https://a.example/", "a.example"),
                                    ("https://b.example/", "b.example")], {}, None, workspace_id=1)
        ids = sorted(t["id"] for t in db.list_tasks(batch_id=bid, limit=10, workspace_id=1))
        db.update_task(ids[0], status="completed", workspace_path="/w/1")
        self.assertIsNone(db.previous_completed_task(ids[0], "https://a.example/"))
        self.assertEqual(db.previous_completed_task(ids[1], "https://a.example/")["id"], ids[0])
        self.assertIsNone(db.previous_completed_task(ids[2], "https://b.example/"))


if __name__ == "__main__":
    unittest.main()
