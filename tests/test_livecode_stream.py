"""界面生成实时代码: 流式读取(SSE)拼回与非流式同形结果、上游不支持流式时回落、livecode 通道增量读取。"""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core.uir import livecode, llm

HTML = "<!doctype html><html><head><title>t</title></head><body><h1>心</h1></body></html>"


class _Handler(BaseHTTPRequestHandler):
    mode = "sse"            # sse / reject_stream
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append(body)
        if body.get("stream") and type(self).mode == "reject_stream":
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"stream not supported"}')
            return
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = [{"choices": [{"delta": {"reasoning_content": "想一想"}}]}]
            events += [{"choices": [{"delta": {"content": HTML[i:i + 7]}}]} for i in range(0, len(HTML), 7)]
            events += [{"choices": [{"delta": {}, "finish_reason": "stop"}]},
                       {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 22}}]
            self.wfile.write(b": keep-alive\n\n")
            for ev in events:
                self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            return
        payload = {"choices": [{"message": {"content": HTML}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
        out = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class LiveCodeStreamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _cfg(self, tag):
        return {"base_url": f"http://127.0.0.1:{self.srv.server_port}/{tag}", "api_key": "x", "model": "m"}

    def test_stream_matches_non_stream_and_feeds_channel(self):
        _Handler.mode, _Handler.seen = "sse", []
        ws = tempfile.mkdtemp()
        ch = livecode.Channel(ws)
        ch.begin("编写页面")
        got = []
        out = llm.chat_text(self._cfg("a"), "sys", "user", thinking=False,
                            on_delta=lambda k, t: (got.append((k, t)), ch.feed(k, t)))
        self.assertEqual(out["text"], HTML)
        self.assertEqual(out["finish"], "stop")
        self.assertEqual(out["usage"]["completion_tokens"], 22)
        self.assertTrue(_Handler.seen[-1].get("stream"))
        self.assertEqual("".join(t for k, t in got if k == "content"), HTML)
        r = livecode.read(ws)
        self.assertTrue(r["reset"])
        self.assertEqual(r["code"], HTML)
        self.assertEqual(r["think"], "想一想")
        # 增量读取: 已收到的位置之后没有新内容
        r2 = livecode.read(ws, r["epoch"], r["code_len"], r["think_len"])
        self.assertFalse(r2["reset"])
        self.assertEqual(r2["code"], "")
        # 新一轮: epoch 变化, 客户端旧位置作废
        ch.begin("修复第 1 轮")
        ch.feed("content", "<<<<<<< SEARCH")
        r3 = livecode.read(ws, r["epoch"], r["code_len"])
        self.assertTrue(r3["reset"])
        self.assertEqual(r3["code"], "<<<<<<< SEARCH")
        self.assertEqual(r3["label"], "修复第 1 轮")
        ch.close()
        self.assertFalse(livecode.read(ws)["active"])

    def test_without_callback_stays_non_stream(self):
        _Handler.mode, _Handler.seen = "sse", []
        out = llm.chat_text(self._cfg("b"), "sys", "user", thinking=False)
        self.assertEqual(out["text"], HTML)
        self.assertNotIn("stream", _Handler.seen[-1])

    def test_upstream_rejecting_stream_falls_back(self):
        _Handler.mode, _Handler.seen = "reject_stream", []
        got = []
        out = llm.chat_text(self._cfg("c"), "sys", "user", thinking=False, on_delta=lambda k, t: got.append(t))
        self.assertEqual(out["text"], HTML)
        self.assertTrue(_Handler.seen[0].get("stream"))
        self.assertNotIn("stream", _Handler.seen[-1])
        # 记住该上游不支持流式, 下次直接走非流式
        llm.chat_text(self._cfg("c"), "sys", "user", thinking=False, on_delta=lambda k, t: None)
        self.assertNotIn("stream", _Handler.seen[-1])


if __name__ == "__main__":
    unittest.main()
