"""参考文本读图: 长截图切段后每段一个请求并行发出, 按段序拼接; 单段失败只丢该段; 同图缓存。(无网络, 打桩)"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from core.uir import llm
from core.uir.adapters import vision


class ReadParallel(unittest.TestCase):
    def setUp(self):
        vision._READ_CACHE.clear()
        self.td = tempfile.TemporaryDirectory()
        self.img = Path(self.td.name) / "long.png"
        Image.new("RGB", (1280, 9000), (255, 255, 255)).save(self.img)   # 6 段

    def tearDown(self):
        self.td.cleanup()

    def test_parallel_ordered_and_cached(self):
        active, peak, lock, calls = [0], [0], threading.Lock(), []

        def fake(cfg, system, user, parts=(), **kw):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                calls.append(user)
            k = int(user.split("中的第 ")[1].split(" 段")[0])
            time.sleep(0.15 * (7 - k))          # 前面的段反而更慢: 结果仍须按段序
            with lock:
                active[0] -= 1
            return {"lines": [f"seg{k}-a", f"seg{k}-b"]}

        with mock.patch.object(llm, "chat_json", side_effect=fake):
            t0 = time.monotonic()
            text = vision.read_screenshot_text({"model": "m"}, self.img)
            wall = time.monotonic() - t0
            again = vision.read_screenshot_text({"model": "m"}, self.img)
        lines = text.splitlines()
        self.assertEqual(lines[0], "seg1-a")
        self.assertEqual(lines[-1], "seg6-b")
        self.assertEqual(len(lines), 12)
        self.assertGreaterEqual(peak[0], 4)
        self.assertLess(wall, 1.5)              # 串行需 ~3.15s
        self.assertEqual(again, text)
        self.assertEqual(len(calls), 6)         # 第二次命中缓存

    def test_one_tile_failure_tolerated(self):
        def fake(cfg, system, user, parts=(), **kw):
            k = int(user.split("中的第 ")[1].split(" 段")[0])
            if k == 3:
                raise RuntimeError("boom")
            return {"lines": [f"seg{k}"]}

        with mock.patch.object(llm, "chat_json", side_effect=fake):
            text = vision.read_screenshot_text({"model": "m"}, self.img)
        self.assertEqual(text.splitlines(), ["seg1", "seg2", "seg4", "seg5", "seg6"])


if __name__ == "__main__":
    unittest.main()
