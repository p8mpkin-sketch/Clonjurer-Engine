"""克隆侧 AI 辅助模块的离线测试: 表单 Mock 生成(确定性/LLM/回退) 与视觉修复补丁消毒落地。"""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import ai_mock_generator as amg
from core import ai_vision_repair as avr

FORM_PAGE = """<html><head><title>登录 - 示例站</title></head><body>
<form action="/api/login" method="post"><input name="user"><input type="password" name="pwd"></form>
<form action="https://example.com/search?q=1"><input name="q"></form>
<form><input name="nothing"></form></body></html>"""


class _LLM(BaseHTTPRequestHandler):
    reply = '{"success": false, "code": 503, "message": "服务繁忙"}'

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        data = json.dumps({"choices": [{"message": {"content": type(self).reply}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class MockGenerator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _LLM)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.cfg = {"base_url": f"http://127.0.0.1:{cls.srv.server_address[1]}", "api_key": "k", "model": "m"}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_extract_forms(self):
        forms = amg.extract_forms(FORM_PAGE, "https://site.test/account/")
        paths = {f["path"] for f in forms}
        self.assertIn("/api/login", paths)
        login = next(f for f in forms if f["path"] == "/api/login")
        self.assertEqual(login["method"], "POST")
        self.assertIn("pwd", login["fields"])

    def test_smart_mocks_deterministic_and_dedup(self):
        logs = []
        out = amg.generate_smart_mocks([FORM_PAGE, FORM_PAGE], [{"path": "search"}], None,
                                       log=lambda *a: logs.append(a))
        paths = [e["path"] for e in out]
        self.assertEqual(len(paths), len(set(paths)))          # 同一路径只生成一次
        self.assertNotIn("/search", paths)                       # 已录制的不重复
        body = json.loads(out[0]["response_body"])
        self.assertFalse(body["success"])

    def test_smart_mocks_llm_and_fallback(self):
        _LLM.reply = '{"success": false, "code": 503, "message": "服务繁忙"}'
        out = amg.generate_smart_mocks([FORM_PAGE], [], self.cfg)
        self.assertEqual(json.loads(out[0]["response_body"])["code"], 503)
        _LLM.reply = "不是 JSON"
        logs = []
        out = amg.generate_smart_mocks([FORM_PAGE], [], self.cfg, log=lambda *a: logs.append(a))
        self.assertEqual(json.loads(out[0]["response_body"])["code"], 500)
        self.assertTrue(any("回退" in str(a) for a in logs))

    def test_scene_mocks(self):
        for scene in ("电商商品列表", "订单", "用户管理", "文章", "其他"):
            e = amg.generate_scene_mocks(scene, 3, "api/x", "post")
            self.assertEqual(e["path"], "/api/x")
            self.assertEqual(e["method"], "POST")
            self.assertEqual(len(json.loads(e["response_body"])["data"]), 3)
        self.assertEqual(len(json.loads(amg.generate_scene_mocks("", 999, "/y")["response_body"])["data"]), 50)


class VisionRepairPatch(unittest.TestCase):
    def test_sanitizers(self):
        css = avr.sanitize_css('@import url(http://evil/x.css); a{background:url(https://cdn/x.png);width:expression(1)}</style>')
        self.assertNotIn("@import", css)
        self.assertNotIn("https://cdn", css)
        self.assertNotIn("expression(", css)
        html = avr.sanitize_html('<div onclick="x()"><script>alert(1)</script><img src="https://evil/a.png"></div>')
        self.assertNotIn("<script", html)
        self.assertNotIn("onclick", html)
        self.assertNotIn("https://evil", html)

    def test_region_hints(self):
        self.assertIn("未发现", avr.region_hints([], {}))
        s = avr.region_hints([(0.5, 0.25, (10, 20, 30), 42.0)], {"height_ratio": 0.8})
        self.assertIn("80%", s)
        self.assertIn("rgb(10,20,30)", s)

    def test_apply_patch_and_restore(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            page = ws / "index.html"
            page.write_text("<html><head></head><body><p>x</p></body></html>", encoding="utf-8")
            snap = avr._snapshot(ws, page)
            res = avr.apply_patch(ws, {"css": "p{color:red}", "html_insert": [{"html": "<div>added<script>1</script></div>"}]}, 1)
            self.assertGreater(res["css_bytes"], 0)
            self.assertEqual(res["html_inserts"], 1)
            html = page.read_text(encoding="utf-8")
            self.assertIn(avr.AI_PATCH_LINK, html)
            self.assertIn("added", html)
            self.assertNotIn("<script>1", html)
            avr._restore(snap)
            self.assertNotIn("added", page.read_text(encoding="utf-8"))
            self.assertFalse((ws / "static" / "css" / "ai_patch.css").is_file())


class _Seq(BaseHTTPRequestHandler):
    queue = []

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = type(self).queue.pop(0) if type(self).queue else {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def _c(text, finish="stop", usage=None):
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}], "usage": usage or {}}


class VisionRepairLoop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Seq)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.cfg = {"base_url": f"http://127.0.0.1:{cls.srv.server_address[1]}", "api_key": "k", "model": "vm"}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def _ws(self, d):
        from PIL import Image
        ws = Path(d)
        for n in ("original_preview.png", "cloned_preview.png", "diff_preview.png"):
            Image.new("RGB", (300, 200), (200, 200, 200)).save(ws / n)
        (ws / "index.html").write_text("<html><head></head><body><div>x</div></body></html>", encoding="utf-8")
        return ws

    def test_improving_patch_kept_and_bad_patch_rolled_back(self):
        from unittest import mock
        u = {"completion_tokens": 9, "completion_tokens_details": {"reasoning_tokens": 9}}
        _Seq.queue = [_c("", "length", u),                                   # 思考吃光预算 → 升档
                      _c('说明 {"diagnosis": "缺底色", "css": "div{background:#fff}"} 结束'),
                      _c('```json\n{"css": "div{color:red}"}\n```'),
                      _c('{"css": ""}')]
        scores = iter([({"score": 80.0}, [], []), ({"score": 70.0}, [], [])])
        logs = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(avr.config, "AI_REPAIR_MAX_ROUNDS", 3):
            ws = self._ws(d)
            rep = avr.run_vision_repair(ws, self.cfg, {"score": 60.0, "warnings": ["w"]}, lambda *a: logs.append(a),
                                        lambda: next(scores), region_cells=[(0.1, 0.2, (1, 2, 3), 9.0)])
            self.assertEqual(rep["final_score"], 80.0)
            self.assertFalse(rep["rounds"][0]["rolled_back"])
            self.assertTrue(rep["rounds"][1]["rolled_back"])
            self.assertTrue(rep["rounds"][2]["skipped"])
            css = (ws / "static" / "css" / "ai_patch.css").read_text(encoding="utf-8")
            self.assertIn("background:#fff", css)
            self.assertNotIn("color:red", css)
            self.assertTrue((ws / "ai_repair_report.json").is_file())

    def test_upstream_failure_and_render_failure(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d, mock.patch.object(avr.config, "AI_REPAIR_MAX_ROUNDS", 2):
            ws = self._ws(d)
            _Seq.queue = [_c("")]                                             # 空补丁 → 调用失败, 结束
            rep = avr.run_vision_repair(ws, self.cfg, {"score": 50.0}, lambda *a: None, lambda: (None, [], []))
            self.assertIn("error", rep["rounds"][0])
            _Seq.queue = [_c('{"css": "div{margin:0}"}')]                     # 复评渲染失败 → 回滚
            rep = avr.run_vision_repair(ws, self.cfg, {"score": 50.0}, lambda *a: None, lambda: (None, [], []))
            self.assertTrue(rep["rounds"][0]["rolled_back"])
            self.assertFalse((ws / "static" / "css" / "ai_patch.css").is_file())


if __name__ == "__main__":
    unittest.main()
