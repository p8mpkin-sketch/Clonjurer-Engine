"""生成引擎端到端(离线): GenerateEngine 全流程 + 本地假模型上游(OpenAI 兼容)。

覆盖: 描述输入(无 Key 确定性)、截图输入(有 Key: 读图参考文本 + 骨架填充预取 + 多候选择优)、
不可截图目标(Flutter 单轮出码)、模型上游报错时回落确定性、模型请求关思考字段被上游拒绝时自动去掉。
"""
import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
from core.uir import llm, settings
from core.uir.adapters import vision
from tests._env import chromium_available, has_modules


class _FakeLLM(BaseHTTPRequestHandler):
    mode = "ok"            # ok / error / reject_thinking
    calls = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        type(self).calls.append(body)
        if type(self).mode == "error":
            self.send_response(500)
            self.end_headers()
            return
        if type(self).mode == "reject_thinking" and "thinking" in body:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"unknown field thinking"}')
            return
        system = body["messages"][0]["content"]
        if system == vision._READ_SYSTEM:
            out = {"lines": ["Welcome back", "Sign in to continue", "Email", "Password"]}
        elif system == vision._FILL_SYSTEM:
            user = body["messages"][1]["content"][0]["text"]
            ids = [ln.split(":")[0] for ln in user.splitlines() if ln.startswith("b")]
            out = {"blocks": [{"id": i, "role": "text", "texts": ["Welcome back"]} for i in ids]}
        else:
            out = {"note": "not a uir"}           # 自由生成路径: 校验失败应回落
        payload = {"choices": [{"message": {"content": json.dumps(out)}, "finish_reason": "stop"}],
                   "usage": {"completion_tokens": 10}}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class _DB:
    def __init__(self):
        self.tasks, self.batches, self.n = {}, {}, 0

    def new(self, spec):
        self.n += 1
        self.batches[self.n] = {"id": self.n, "config_json": json.dumps({"mode": "generate", "gen": spec})}
        self.tasks[self.n] = {"id": self.n, "batch_id": self.n, "status": "pending",
                              "target_url": settings.TARGET_URL_PREFIX + spec["kind"]}
        return self.n

    def get_task(self, tid):
        return dict(self.tasks[tid])

    def get_batch(self, bid):
        return dict(self.batches[bid])

    def update_task(self, tid, **f):
        self.tasks[tid].update(f)

    def increment_completed(self, bid):
        pass


class _Bus:
    def __init__(self):
        self.lines = []

    def publish(self, tid, stage, msg, level=None):
        self.lines.append(f"[{stage}] {msg}")


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class GenerateEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from core.browser_pool import SyncBrowserPool
        cls.tmp = Path(tempfile.mkdtemp(prefix="gen_e2e_"))
        cls._saved = {k: getattr(config, k) for k in ("WORKSPACE_ROOT", "AI_API_KEY", "AI_API_BASE_URL", "AI_MODEL")}
        config.WORKSPACE_ROOT = cls.tmp / "ws"
        config.WORKSPACE_ROOT.mkdir()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLM)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pool = SyncBrowserPool.instance(2)
        # 截图输入: 描述原型渲染出的登录页(已知真值)
        from core.uir.adapters.description import build_from_description
        from core.uir.render import screenshot_dir
        from core.uir.targets.html_css import HtmlCssTarget
        uir = build_from_description("登录页: 标题 Welcome back, 邮箱和密码输入框, 登录按钮",
                                     {"width": 1024, "height": 700})
        d = cls.tmp / "ref"
        r = HtmlCssTarget().render(uir, d)
        cls.shot = cls.tmp / "login.png"
        screenshot_dir(cls.pool, d, r.entry_file, cls.shot, {"width": 1024, "height": 700})

    @classmethod
    def tearDownClass(cls):
        # 主线程里的 playwright 同步实例必须关掉, 否则后续用 sync_playwright() 的测试会撞上残留事件循环
        from core.browser_pool import SyncBrowserPool
        SyncBrowserPool.instance()._teardown_thread_browser()
        cls.srv.shutdown()
        cls.srv.server_close()
        for k, v in cls._saved.items():
            setattr(config, k, v)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _key(self, on: bool):
        config.AI_API_KEY = "test-key" if on else ""
        config.AI_API_BASE_URL = f"http://127.0.0.1:{self.srv.server_address[1]}"
        config.AI_MODEL = "fake-model"

    def _run(self, spec):
        from core.uir.generate import GenerateEngine, is_generate_task
        db, bus = _DB(), _Bus()
        tid = db.new(spec)
        self.assertTrue(is_generate_task(db.get_task(tid)))
        GenerateEngine(tid, db, bus, self.pool).run()
        rep = json.loads((config.WORKSPACE_ROOT / str(tid) / "generate_report.json").read_text(encoding="utf-8"))
        return db.get_task(tid), rep, bus.lines

    def test_description_deterministic(self):
        self._key(False)
        task, rep, _ = self._run({"kind": "description", "description": "商品列表页: 顶栏, 搜索框, 六个商品卡片, 页脚",
                                  "track": "uir"})
        self.assertEqual(task["status"], "completed")
        self.assertEqual(rep["scoring"], "desc-self-v1")
        self.assertGreater(task["similarity_score"], 50)

    def test_screenshot_with_model(self):
        self._key(True)
        _FakeLLM.mode, _FakeLLM.calls = "ok", []
        task, rep, lines = self._run({"kind": "screenshot", "image_path": str(self.shot), "time_budget": 60})
        self.assertEqual(task["status"], "completed")
        self.assertEqual(rep["reference_text"], "model-read")
        self.assertGreaterEqual(len(rep["rounds"]), 1)
        # 抄录/填充请求带关思考字段
        self.assertTrue(any(c.get("thinking") == {"type": "disabled"} for c in _FakeLLM.calls))
        self.assertGreater(task["similarity_score"], 60)

    def test_screenshot_model_errors_fall_back(self):
        self._key(True)
        _FakeLLM.mode = "error"
        vision._READ_CACHE.clear()          # 同一截图的读图结果会被缓存
        try:
            task, rep, lines = self._run({"kind": "screenshot", "image_path": str(self.shot), "time_budget": 40})
        finally:
            _FakeLLM.mode = "ok"
        self.assertEqual(task["status"], "completed")
        self.assertEqual(rep["reference_text"], "none")
        self.assertTrue(any("回落" in ln or "读图失败" in ln for ln in lines))

    def test_thinking_field_rejected_is_dropped(self):
        self._key(True)
        _FakeLLM.mode, _FakeLLM.calls = "reject_thinking", []
        llm._NO_THINK_UNSUPPORTED.discard(config.AI_API_BASE_URL)
        try:
            out = llm.chat_json(llm.resolve_ai_cfg({}), vision._READ_SYSTEM, "x", thinking=False)
        finally:
            _FakeLLM.mode = "ok"
            llm._NO_THINK_UNSUPPORTED.discard(config.AI_API_BASE_URL)
        self.assertEqual(out["lines"][0], "Welcome back")
        self.assertEqual(["thinking" in c for c in _FakeLLM.calls], [True, False])

    def test_flutter_target_not_shootable(self):
        self._key(False)
        task, rep, _ = self._run({"kind": "description", "description": "设置页: 列表项与开关", "target": "flutter"})
        self.assertEqual(task["status"], "completed")
        self.assertEqual(rep["scoring"], "none")

    def test_free_mode_invalid_uir_falls_back(self):
        self._key(True)
        task, rep, lines = self._run({"kind": "screenshot", "image_path": str(self.shot),
                                      "time_budget": 40, "llm_mode": "free", "max_rounds": 2})
        self.assertEqual(task["status"], "completed")

    def test_build_steps_with_stubbed_toolchain(self):
        from unittest import mock
        from core.uir import toolchain
        self._key(False)
        saved = config.EXPORTS_DIR
        config.EXPORTS_DIR = self.tmp / "exports"
        try:
            fake_apk = self.tmp / "fake.apk"
            fake_apk.write_bytes(b"PK")
            with mock.patch.object(toolchain, "build_apk", return_value={"ok": True, "artifact": str(fake_apk),
                                                                          "signing": "debug"}):
                _, rep, _ = self._run({"kind": "description", "description": "登录页", "target": "flutter", "build": "apk"})
            self.assertTrue(rep["build"]["ok"])
            self.assertEqual(rep["build"]["artifacts"][0]["label"], "APK")
            app_dir = self.tmp / "Fake-darwin-arm64"
            app_dir.mkdir(exist_ok=True)
            (app_dir / "x.txt").write_text("x")
            with mock.patch.object(toolchain, "build_desktop", return_value={
                    "artifacts": [{"dir": str(app_dir), "platform": "darwin", "arch": "arm64"}], "failures": {"win32": "x"}}):
                _, rep, _ = self._run({"kind": "description", "description": "登录页", "target": "electron", "build": "desktop"})
            self.assertTrue(rep["build"]["ok"])
            self.assertIn("win32", rep["build"]["gap"])
            # 未知构建类型 / 工具链异常: 只记缺口, 任务照常完成
            task, rep, _ = self._run({"kind": "description", "description": "登录页", "target": "flutter", "build": "bogus"})
            self.assertEqual(task["status"], "completed")
            self.assertFalse(rep["build"]["ok"])
            with mock.patch.object(toolchain, "build_apk", side_effect=OSError("boom")):
                _, rep, _ = self._run({"kind": "description", "description": "登录页", "target": "flutter", "build": "apk"})
            self.assertIn("构建异常", rep["build"]["gap"])
        finally:
            config.EXPORTS_DIR = saved


if __name__ == "__main__":
    unittest.main()
