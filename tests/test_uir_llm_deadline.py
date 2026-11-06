"""模型调用墙钟上限: 上游持续发 keep-alive 空行时, requests 读超时不会触发, chat_json 必须按墙钟断开。"""
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core.uir import llm


class _KeepAlive(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            for _ in range(200):          # 最多 ~20s 的空行, 远超测试里的墙钟上限
                self.wfile.write(b"\n")
                self.wfile.flush()
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass


class ChatJsonDeadline(unittest.TestCase):
    def test_keepalive_stream_is_cut_at_deadline(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _KeepAlive)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            cfg = {"base_url": f"http://127.0.0.1:{srv.server_address[1]}", "api_key": "x", "model": "m"}
            t0 = time.monotonic()
            with self.assertRaises(llm.DeadlineExceeded):
                llm.chat_json(cfg, "s", "u", deadline=time.monotonic() + 1.5)
            self.assertLess(time.monotonic() - t0, 5.0)
        finally:
            srv.shutdown()
            srv.server_close()



class _Script(BaseHTTPRequestHandler):
    """按队列依次返回预设响应, 覆盖升档/截断/空内容/非 JSON 分支。"""
    queue = []

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        status, body = type(self).queue.pop(0) if type(self).queue else (200, b"{}")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _resp(content, finish="stop", usage=None):
    import json
    return 200, json.dumps({"choices": [{"message": {"content": content}, "finish_reason": finish}],
                            "usage": usage or {}}).encode()


class ChatJsonBranches(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Script)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.cfg = {"base_url": f"http://127.0.0.1:{cls.srv.server_address[1]}", "api_key": "x", "model": "m"}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_fenced_json(self):
        _Script.queue = [_resp('```json\n{"a": 1}\n```')]
        self.assertEqual(llm.chat_json(self.cfg, "s", "u"), {"a": 1})

    def test_truncated_then_escalated(self):
        _Script.queue = [_resp('{"a": [1, 2', finish="length"), _resp('{"a": [1, 2]}')]
        self.assertEqual(llm.chat_json(self.cfg, "s", "u", max_tokens=10), {"a": [1, 2]})

    def test_reasoning_exhausted_then_escalated(self):
        u = {"completion_tokens": 10, "completion_tokens_details": {"reasoning_tokens": 10}}
        _Script.queue = [_resp("", finish="length", usage=u), _resp('{"ok": true}')]
        self.assertEqual(llm.chat_json(self.cfg, "s", "u", max_tokens=10), {"ok": True})

    def test_empty_and_garbage_raise(self):
        _Script.queue = [_resp("", finish="stop")]
        with self.assertRaises(RuntimeError):
            llm.chat_json(self.cfg, "s", "u")
        _Script.queue = [_resp("no json here")]
        with self.assertRaises(RuntimeError):
            llm.chat_json(self.cfg, "s", "u")
        _Script.queue = [(200, b"<html>gateway</html>")]
        with self.assertRaises(RuntimeError):
            llm.chat_json(self.cfg, "s", "u")

    def test_image_parts_tiles_long_page(self):
        import tempfile
        from pathlib import Path
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "long.png"
            Image.new("RGB", (1920, 9000), (255, 255, 255)).save(p)
            parts, ranges, (W, H) = llm.image_parts(p)
            self.assertGreater(len(parts), 1)
            self.assertEqual(ranges[0][0], 0)
            self.assertEqual(ranges[-1][1], 9000)
            _Script.queue = [_resp('{"seen": 1}')]
            self.assertEqual(llm.chat_json(self.cfg, "s", "u", images=[p]), {"seen": 1})

    def test_resolve_cfg(self):
        self.assertIsNone(llm.resolve_ai_cfg({"ai_api_key": ""}) if not llm.config.AI_API_KEY else None)
        cfg = llm.resolve_ai_cfg({"ai_api_key": "k", "ai_base_url": "http://x/v1/", "ai_model": "mm"})
        self.assertEqual(cfg, {"base_url": "http://x/v1", "api_key": "k", "model": "mm"})


if __name__ == "__main__":
    unittest.main()
