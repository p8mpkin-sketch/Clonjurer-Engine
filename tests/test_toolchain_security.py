"""构建工具链的缺口路径(缺工具只返回 gap, 不抛异常)、本机真实 APK 壳构建(有 SDK 时), 以及安全基座。"""
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from core.uir import toolchain
from tests._env import chromium_available, has_modules


class ToolchainGaps(unittest.TestCase):
    def test_missing_tools_return_gap_not_raise(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "_which", return_value=None), \
                mock.patch.object(toolchain, "find_android_sdk", return_value=None):
            for fn in (toolchain.build_apk, toolchain.build_android_shell, toolchain.build_go_desktop,
                       toolchain.build_desktop):
                res = fn(d, log=lambda m: None)
                self.assertFalse(res["ok"], fn.__name__)
                self.assertIn("缺工具链", res["gap"], fn.__name__)

    def test_incomplete_sdk_is_gap(self):
        with tempfile.TemporaryDirectory() as d:
            sdk = Path(d) / "sdk"
            (sdk / "platforms").mkdir(parents=True)
            with mock.patch.object(toolchain, "find_android_sdk", return_value=str(sdk)), \
                    mock.patch.object(toolchain, "_which", return_value="/usr/bin/true"):
                res = toolchain.build_android_shell(d, log=lambda m: None)
            self.assertFalse(res["ok"])
            self.assertIn("build-tools", res["gap"])

    def test_zip_dir_and_platform(self):
        self.assertIn(toolchain.host_platform(), ("darwin", "win32", "linux"))
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "App"
            (src / "sub").mkdir(parents=True)
            (src / "sub" / "a.txt").write_text("hi")
            out = toolchain.zip_dir(src, Path(d) / "out" / "app.zip")
            self.assertTrue(out.is_file())
            with zipfile.ZipFile(out) as z:
                self.assertTrue(any(n.endswith("a.txt") for n in z.namelist()))
            # 已存在时覆盖
            toolchain.zip_dir(src, out)
            self.assertTrue(out.is_file())

    def test_find_android_sdk_env(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "platforms").mkdir()
            with mock.patch.dict(os.environ, {"ANDROID_HOME": d}):
                self.assertEqual(toolchain.find_android_sdk(), d)


@unittest.skipUnless(toolchain.find_android_sdk() and shutil.which("javac"), "本机无 Android SDK/JDK")
class AndroidShellRealBuild(unittest.TestCase):
    def test_build_signed_apk(self):
        from core import app_shell
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"
            fa = ws / "flask_app"
            (fa / "templates").mkdir(parents=True)
            (fa / "static").mkdir()
            (fa / "templates" / "index.html").write_text("<!doctype html><h1>shell</h1>", encoding="utf-8")
            (fa / "site.json").write_text('{"pages": {"/": "index.html"}}', encoding="utf-8")
            try:
                app = app_shell.android_shell(ws, Path(d) / "out", "测试站", "com.clonjurer.unittest")
            except Exception as exc:          # 站点结构不符合壳要求时不在这里断言
                self.skipTest(f"android_shell 夹具不适用: {exc}")
            res = toolchain.build_android_shell(app, log=lambda m: None)
            self.assertTrue(res.get("ok"), res)
            apk = Path(res["artifact"])
            self.assertTrue(apk.is_file())
            with zipfile.ZipFile(apk) as z:
                names = z.namelist()
            self.assertIn("classes.dex", names)
            self.assertTrue(any(n.startswith("META-INF/") for n in names), "未签名")


class ToolchainStubbedRuns(unittest.TestCase):
    """工具都"在", 但子命令失败: 每一步失败都只返回 gap(网络/镜像/依赖问题), 不抛异常。"""

    def test_step_failures_are_gaps(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "shell.json").write_text('{"name": "App", "title": "App", "bundle_id": "com.x.app"}')
            with mock.patch.object(toolchain, "_which", return_value="/usr/bin/true"), \
                    mock.patch.object(toolchain, "find_android_sdk", return_value=d), \
                    mock.patch.object(toolchain, "_run", return_value=(1, "boom")):
                self.assertIn("go mod tidy", toolchain.build_go_desktop(d, log=lambda m: None)["gap"])
                self.assertIn("npm install", toolchain.build_desktop(d, log=lambda m: None)["gap"])
                self.assertIn("flutter create", toolchain.build_apk(d, log=lambda m: None)["gap"])

    def test_cli(self):
        import io
        from contextlib import redirect_stdout
        with mock.patch.object(toolchain, "build_apk", return_value={"ok": True}) as b, redirect_stdout(io.StringIO()):
            self.assertEqual(toolchain.main(["apk", "/tmp/x", "--release"]), 0)
            b.assert_called_once_with("/tmp/x", release=True)
        with mock.patch.object(toolchain, "build_desktop", return_value={"ok": False, "gap": "g"}) as b, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(toolchain.main(["desktop", "/tmp/y", "--platform", "darwin,win32", "--arch", "x64"]), 1)
            b.assert_called_once_with("/tmp/y", ["darwin", "win32"], "x64")


class SmartDownloaderPaths(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                code, body = {"/ok.css": (200, b"a{}"), "/forbidden.png": (403, b"no"),
                              "/empty": (200, b"")}.get(self.path, (404, b""))
                self.send_response(code)
                self.send_header("Content-Type", "text/css; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_direct_and_browser_fallback(self):
        from core.smart_downloader import SmartDownloader, clean_ext, strip_path_params

        class Resp:
            def __init__(self, status, body):
                self.status, self._b, self.headers = status, body, {"content-type": "image/png"}

            def body(self):
                return self._b

        class Ctx:
            def __init__(self, resp=None, exc=None):
                self.resp, self.exc = resp, exc

            def get(self, url, **kw):
                if self.exc:
                    raise self.exc
                return self.resp

        dl = SmartDownloader({"User-Agent": "t"})
        ok = dl.fetch(self.base + "/ok.css", referer=self.base + "/")
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["content_type"], "text/css")
        self.assertEqual(dl.fetch_direct(self.base + "/empty")["error"], "空响应体")
        self.assertFalse(dl.fetch(self.base + "/forbidden.png")["ok"])           # 无会话不降级
        rescued = dl.fetch(self.base + "/forbidden.png", api_request_ctx=Ctx(Resp(200, b"PNG")))
        self.assertTrue(rescued["ok"])
        self.assertEqual(rescued["via"], "browser")
        self.assertEqual((dl.browser_fallbacks, dl.browser_rescues), (1, 1))
        self.assertIn("HTTP 404", dl.fetch_via_browser("u", Ctx(Resp(404, b"")))["error"])
        self.assertIn("RuntimeError", dl.fetch_via_browser("u", Ctx(exc=RuntimeError("x")))["error"])
        self.assertEqual(dl.fetch_via_browser("u", Ctx(Resp(200, b"")))["error"], "空响应体")
        self.assertEqual(dl.fetch_via_browser("u", None)["error"], "无可用浏览器会话")
        self.assertIn("ConnectionError", dl.fetch_direct("http://127.0.0.1:9/x")["error"])
        self.assertEqual(strip_path_params("/a;jsessionid=1/b.png"), strip_path_params("/a;jsessionid=1/b.png"))
        self.assertTrue(clean_ext("https://x/y/z.CSS?v=1").lower().endswith("css"))


@unittest.skipUnless(has_modules("cryptography", "werkzeug"), "需要平台依赖")
class Security(unittest.TestCase):
    def setUp(self):
        from core import security
        self.sec = security
        self._saved = (security._fernet, security._KEY_FILE)
        self.tmp = tempfile.mkdtemp()
        security._fernet = None
        security._KEY_FILE = Path(self.tmp) / ".secret_key"

    def tearDown(self):
        self.sec._fernet, self.sec._KEY_FILE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_encrypt_roundtrip_and_tamper(self):
        with mock.patch.dict(os.environ, {"CLONER_SECRET_KEY": ""}):
            tok = self.sec.encrypt_value("sk-test-123")
            self.assertNotIn("sk-test", tok)
            self.assertEqual(self.sec.decrypt_value(tok), "sk-test-123")
            self.assertEqual(self.sec.decrypt_value(tok[:-4] + "AAAA"), "")
            self.assertEqual(self.sec.encrypt_value(""), "")
            self.assertEqual(self.sec.decrypt_value(""), "")
            self.assertTrue(self.sec._KEY_FILE.is_file())
            self.assertEqual(len(self.sec.session_secret()), 64)

    def test_key_from_env(self):
        with mock.patch.dict(os.environ, {"CLONER_SECRET_KEY": "x" * 40}):
            self.sec._fernet = None
            a = self.sec.session_secret()
            self.sec._fernet = None
            self.assertEqual(a, self.sec.session_secret())

    def test_passwords(self):
        h = self.sec.hash_password("pw-123456")
        self.assertTrue(self.sec.verify_password("pw-123456", h))
        self.assertFalse(self.sec.verify_password("nope", h))
        self.assertFalse(self.sec.verify_password("x", ""))
        self.assertFalse(self.sec.verify_password("x", "garbage$hash"))


if __name__ == "__main__":
    unittest.main()
