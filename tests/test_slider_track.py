"""轮播轨道(父宽 = 等宽子项之和)不改 vw; 其他同比项不受影响。"""
import unittest

from core.cloner_engine import _drop_slider_tracks


class SliderTrack(unittest.TestCase):
    def test_track_dropped(self):
        d = {"a>t": {"width": "500.0000vw"}, "a>t>d:5": {"width": "100.0000vw", "height": "3vw"}, "b": {"width": "50vw"}}
        for i in range(1, 5):
            d[f"a>t>d:{i}"] = {"width": "100.0000vw"}
        self.assertEqual(_drop_slider_tracks(d), 6)
        self.assertEqual(d, {"a>t>d:5": {"height": "3vw"}, "b": {"width": "50vw"}})

    def test_non_track_kept(self):
        d = {"a": {"width": "90vw"}, "a>x:1": {"width": "40vw"}, "a>x:2": {"width": "30vw"}}
        self.assertEqual(_drop_slider_tracks(d), 0)
        self.assertEqual(len(d), 3)


if __name__ == "__main__":
    unittest.main()
