"""交付形态(应用壳)护栏: 任务网站工程 → 桌面 / Android 应用壳。

- bundle_site: 只带站点内容(templates/static/mock_data 等), 不带 Flask 运行时文件; 页面路由/上游/反代开关进 site_meta;
- Electron 壳: main.js 只监听 127.0.0.1, 实现路由表 / 录制接口 / 影子文件 / 反代兜底, 安全默认;
- Android 原生壳: 站点进 assets, 虚拟 https 源拦截作答, 不放行明文, 包名校验;
- Go 桌面壳: 只听 127.0.0.1, 窗口钳制, 依赖版本钉死;
- toolchain._patch_android: 按声明改 AndroidManifest; 缺工具链/产物时 package_task 只报缺口不抛异常。
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import app_shell  # noqa: E402
from core.uir import toolchain  # noqa: E402


def _fake_ws(d: Path) -> Path:
    ws = d / "ws"
    fa = ws / "flask_app"
    (fa / "templates" / "about").mkdir(parents=True)
    (fa / "static" / "css").mkdir(parents=True)
    (fa / "templates" / "index.html").write_text("<html><link href='/static/css/a.css'>首页</html>", encoding="utf-8")
    (fa / "templates" / "about" / "index.html").write_text("<html>关于</html>", encoding="utf-8")
    (fa / "static" / "css" / "a.css").write_text("body{color:red}", encoding="utf-8")
    (fa / "mock_data.json").write_text(json.dumps([{"path": "/api/x", "method": "POST", "status": 200,
                                                    "response_body": "{\"ok\":1}"}]), encoding="utf-8")
    for infra in ("app.py", "Dockerfile", "requirements.txt", "run.sh"):
        (fa / infra).write_text("x", encoding="utf-8")
    (ws / "site.json").write_text(json.dumps({"routes": {"/": "index.html", "/about": "about/index.html"},
                                              "upstream": "https://example.com", "proxy_enabled": True}), encoding="utf-8")
    return ws


class AppShellGuards(unittest.TestCase):
    def test_bundle_site_content_only(self):
        with tempfile.TemporaryDirectory() as d:
            ws = _fake_ws(Path(d))
            meta = app_shell.bundle_site(ws, Path(d) / "site")
            site = Path(d) / "site"
            self.assertTrue((site / "templates/about/index.html").is_file())
            self.assertTrue((site / "static/css/a.css").is_file())
            self.assertTrue((site / "mock_data.json").is_file())
            for infra in ("app.py", "Dockerfile", "requirements.txt", "run.sh"):
                self.assertFalse((site / infra).exists(), infra)
            self.assertEqual(meta["pages"]["/about"], "about/index.html")
            self.assertTrue(meta["proxy"])

    def test_electron_shell_local_server(self):
        with tempfile.TemporaryDirectory() as d:
            ws = _fake_ws(Path(d))
            app = app_shell.electron_shell(ws, Path(d) / "out", "测试站", {"width": 1920, "height": 1080})
            main = (app / "main.js").read_text(encoding="utf-8")
            for frag in ("'127.0.0.1'", "matchMock", "META.pages", "contextIsolation: true", "nodeIntegration: false",
                         "sandbox: true", "META.proxy", "shell.openExternal"):
                self.assertIn(frag, main, frag)
            self.assertNotIn("0.0.0.0", main)
            pkg = json.loads((app / "package.json").read_text(encoding="utf-8"))
            self.assertEqual(pkg["productName"], "测试站")
            self.assertTrue((app / "site/site_meta.json").is_file())

    def test_android_shell_native(self):
        with tempfile.TemporaryDirectory() as d:
            ws = _fake_ws(Path(d))
            app = app_shell.android_shell(ws, Path(d) / "out", "测试站", "com.clonjurer.task7")
            self.assertTrue((app / "assets/site/static/css/a.css").is_file())
            self.assertFalse((app / "assets/site/app.py").exists())
            man = (app / "AndroidManifest.xml").read_text(encoding="utf-8")
            self.assertIn('package="com.clonjurer.task7"', man)
            self.assertIn('android:label="测试站"', man)
            self.assertIn('android:usesCleartextTraffic="false"', man)   # 虚拟 https 源, 不放行明文
            java = (app / "src/com/clonjurer/task7/MainActivity.java").read_text(encoding="utf-8")
            for frag in ("shouldInterceptRequest", "matchMock", "setAllowFileAccess(false)", "https://\" + HOST",
                         "ACTION_VIEW"):
                self.assertIn(frag.replace("\\", ""), java.replace("\\", ""), frag)
            self.assertNotIn("flutter", java.lower())
            with self.assertRaises(ValueError):
                app_shell.android_shell(ws, Path(d) / "out2", "x", "Bad Package")

    def test_desktop_shell_go(self):
        with tempfile.TemporaryDirectory() as d:
            ws = _fake_ws(Path(d))
            app = app_shell.desktop_shell(ws, Path(d) / "out", "测试站", {"width": 1920, "height": 1080}, "com.clonjurer.task7")
            for f in ("server.go", "main.go", "window_darwin.go", "window_windows.go", "config.go", "go.mod",
                      "site/site_meta.json", "site/templates/index.html"):
                self.assertTrue((app / f).is_file(), f)
            server = (app / "server.go").read_text(encoding="utf-8")
            self.assertIn('"127.0.0.1:"', server)
            self.assertNotIn("0.0.0.0", server)
            cfg = (app / "config.go").read_text(encoding="utf-8")
            self.assertIn('AppTitle  = "测试站"', cfg)
            self.assertIn("AppWidth  = 1440", cfg)                       # 窗口钳制
            gomod = (app / "go.mod").read_text(encoding="utf-8")
            reqs = re.findall(r"^\s+(\S+) (v\S+)$", gomod, re.M)
            self.assertEqual(len(reqs), 2)
            self.assertTrue(all("latest" not in v for _, v in reqs))      # 依赖版本钉死

    def test_patch_android_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            app = Path(d)
            man = app / "android/app/src/main/AndroidManifest.xml"
            man.parent.mkdir(parents=True)
            man.write_text('<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
                           '    <application\n        android:label="generated_ui">\n    </application>\n</manifest>\n',
                           encoding="utf-8")
            (app / "clonjurer_android.json").write_text(json.dumps(
                {"label": "测试站", "internet": True, "cleartext_hosts": ["127.0.0.1"]}), encoding="utf-8")
            toolchain._patch_android(app, lambda m: None)
            xml = man.read_text(encoding="utf-8")
            self.assertIn('android:label="测试站"', xml)
            self.assertIn("android.permission.INTERNET", xml)
            self.assertIn('android:networkSecurityConfig="@xml/network_security_config"', xml)
            nsc = (app / "android/app/src/main/res/xml/network_security_config.xml").read_text(encoding="utf-8")
            self.assertIn("<domain includeSubdomains=\"false\">127.0.0.1</domain>", nsc)
            self.assertNotIn("base-config", nsc)   # 不全局放开明文

    def test_package_task_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "empty_ws"
            ws.mkdir()
            res = app_shell.package_task(1, ws, "desktop", log=lambda m: None)
            self.assertFalse(res["ok"])
            self.assertIn("gap", res)
            self.assertTrue((ws / "build_report.json").is_file())
            ws2 = _fake_ws(Path(d))
            with mock.patch.object(toolchain, "_which", return_value=None):
                res2 = app_shell.package_task(2, ws2, "apk", log=lambda m: None)
            self.assertFalse(res2["ok"])
            self.assertIn("缺工具链", res2["gap"])


if __name__ == "__main__":
    unittest.main()
