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


if __name__ == "__main__":
    unittest.main()
