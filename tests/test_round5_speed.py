"""第 5 轮提速的行为守卫: 批量会话补抓、结构性缺失判定、修复循环收敛口径、窄屏首载对照页提前发起。"""
import threading
import time
import unittest
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
from core import smart_downloader
from core.cloner_engine import ClonerEngine


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/ok":
            body = b"hello"
        elif path == "/needs-cookie":
            if "sid=abc" not in (self.headers.get("Cookie") or ""):
                self.send_response(403)
                self.end_headers()
                return
            body = b"secret"
        elif path == "/slow":
            time.sleep(0.4)
            body = b"slow"
        elif path == "/waf":
            self.send_response(403)
            self.end_headers()
            return
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)


class _Resp:
    def __init__(self, status, body=b"browser"):
        self.status, self._b, self.headers = status, body, {"content-type": "text/plain"}

    def body(self):
        return self._b


class _Ctx:
    def __init__(self):
        self.calls = []

    def get(self, url, **_k):
        self.calls.append(url)
        return _Resp(200, b"via-browser")


class SessionFetchMany(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_cookie_concurrency_and_selective_browser_retry(self):
        dl = smart_downloader.SmartDownloader({})
        ctx = _Ctx()
        urls = [self.base + p for p in ("/ok", "/needs-cookie", "/missing", "/waf")]
        out = dl.fetch_session_many(urls, ctx, cookies=[{"name": "sid", "value": "abc", "domain": "127.0.0.1"}],
                                    deadline=time.monotonic() + 10)
        self.assertEqual(out[self.base + "/ok"]["content"], b"hello")
        self.assertEqual(out[self.base + "/needs-cookie"]["content"], b"secret")   # 带会话 Cookie 直连即可
        self.assertFalse(out[self.base + "/missing"]["ok"])                          # 404 不再走浏览器会话
        self.assertEqual(out[self.base + "/waf"]["content"], b"via-browser")         # 风控类才串行走会话
        self.assertEqual(ctx.calls, [self.base + "/waf"])

    def test_parallel_not_serial(self):
        dl = smart_downloader.SmartDownloader({})
        t0 = time.monotonic()
        out = dl.fetch_session_many([self.base + f"/slow?{i}" for i in range(8)], None,
                                    deadline=time.monotonic() + 10)
        self.assertTrue(all(o["ok"] for o in out.values()))
        self.assertLess(time.monotonic() - t0, 2.0)   # 串行要 3.2s

    def test_deadline_marks_unfinished(self):
        dl = smart_downloader.SmartDownloader({})
        out = dl.fetch_session_many([self.base + "/slow"], None, deadline=time.monotonic() + 0.05)
        self.assertFalse(out[self.base + "/slow"]["ok"])


class StructuralMissing(unittest.TestCase):
    def test_only_css_and_fonts_count(self):
        sm = ClonerEngine._structural_missing
        self.assertEqual(sm({"/a.png", "/b.js", "/api/x"}, {"https://cdn.x/y.jpg"}), [])
        self.assertEqual(sm({"/s/site.css"}, set()), ["/s/site.css"])
        self.assertEqual(sm(set(), {"https://fonts.x/f.woff2?v=1"}), ["https://fonts.x/f.woff2?v=1"])

    def test_config_switches_exist(self):
        for name in ("REPAIR_STRUCTURAL_ONLY", "CALIB_INPLACE_GATE", "FIDELITY_PRESTART",
                     "VIEWPORT_RELOAD_EARLY", "SESSION_FETCH_BUDGET"):
            self.assertTrue(hasattr(config, name), name)


if __name__ == "__main__":
    unittest.main()
