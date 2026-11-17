"""视觉修复: 纯文本模型(收到图片返回 400)自动降级为文字线索模式; 一直失败时给出可读原因。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from core import ai_vision_repair as avr


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return self._body if isinstance(self._body, dict) else json.loads(self._body)


_OK = {"choices": [{"message": {"content": '{"css": "a{color:red}", "diagnosis": "x"}'},
                    "finish_reason": "stop"}], "usage": {"total_tokens": 10}}


class TextOnlyFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.a, self.b = d / "a.png", d / "b.png"
        for p in (self.a, self.b):
            Image.new("RGB", (40, 30), "white").save(p)
        avr._TEXT_ONLY_MODELS.clear()
        self.cfg = {"base_url": "https://api.example.com/v1", "api_key": "k", "model": "text-model"}

    def tearDown(self):
        avr._TEXT_ONLY_MODELS.clear()
        self.tmp.cleanup()

    def _run(self, responses):
        sent = []

        def fake_post(url, headers=None, json=None, timeout=None):
            sent.append(json)
            return responses.pop(0)
        notice = []
        with mock.patch.object(avr.requests, "post", fake_post), \
                mock.patch.object(avr.llm_usage, "record", lambda *a, **k: None):
            patch = avr._chat_vision(self.cfg, self.a, self.b, "<html>", {}, notice=notice)
        return patch, sent, notice

    def test_image_rejected_then_text_only_succeeds_and_is_remembered(self):
        patch, sent, notice = self._run([_Resp(400, "unknown variant `image_url`"), _Resp(200, _OK)])
        self.assertEqual(patch["css"], "a{color:red}")
        self.assertIsInstance(sent[0]["messages"][1]["content"], list)   # 第一次带图
        self.assertIsInstance(sent[1]["messages"][1]["content"], str)    # 降级后纯文字
        self.assertTrue(notice and "不接受图片" in notice[0])
        # 第二次调用直接走纯文字, 不再白发图片
        _patch, sent2, _ = self._run([_Resp(200, _OK)])
        self.assertIsInstance(sent2[0]["messages"][1]["content"], str)

    def test_both_attempts_fail_gives_readable_reason(self):
        with self.assertRaises(RuntimeError) as cm:
            self._run([_Resp(400, "bad image"), _Resp(400, "model not found")])
        self.assertIn("400", str(cm.exception))
        self.assertIn("model not found", str(cm.exception))

    def test_other_http_errors_include_body(self):
        with self.assertRaises(RuntimeError) as cm:
            self._run([_Resp(401, "invalid api key")])
        self.assertIn("401", str(cm.exception))
        self.assertIn("invalid api key", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
