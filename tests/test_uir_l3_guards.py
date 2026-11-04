"""L3 桌面目标 + 构建工具链护栏(DESIGN.md §9.8/§9.12):

- electron 目标: html_css 产物原样装进 Electron 壳(同一份 HTML, 可截图评分), main.js 安全默认
  (contextIsolation/sandbox/无 nodeIntegration, 外链交系统浏览器), package.json 钉死版本, 窗口取 UIR 视口;
- toolchain: 缺工具链时返回 {"ok": False, "gap": ...} 而不抛异常; 不在 render 中调用任何外部命令;
- 注册表: electron 可发现。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.uir import toolchain  # noqa: E402
from core.uir.adapters.description import build_from_description  # noqa: E402
from core.uir.registry import list_outputs  # noqa: E402
from core.uir.targets.electron import ElectronTarget  # noqa: E402
from core.uir.targets.html_css import HtmlCssTarget  # noqa: E402


class ElectronTargetGuards(unittest.TestCase):
    def test_wraps_same_html(self):
        u = build_from_description("蓝色登录页, 品牌叫 云帆, 有短信验证码", {"width": 420, "height": 760})
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("subprocess.run") as run:
                r = ElectronTarget().render(u, Path(d) / "e")
                HtmlCssTarget().render(u, Path(d) / "w")
                run.assert_not_called()
            self.assertTrue(r.shootable)
            app = Path(d) / "e/electron_app"
            self.assertEqual(r.entry_file, app / "app/index.html")
            self.assertEqual((app / "app/index.html").read_bytes(), (Path(d) / "w/index.html").read_bytes())
            self.assertEqual((app / "app/static/css/app.css").read_bytes(),
                             (Path(d) / "w/static/css/app.css").read_bytes())
            main = (app / "main.js").read_text(encoding="utf-8")
            for frag in ("contextIsolation: true", "sandbox: true", "nodeIntegration: false",
                         "shell.openExternal", "loadFile(", "'app', 'index.html'"):
                self.assertIn(frag, main, frag)
            pkg = json.loads((app / "package.json").read_text(encoding="utf-8"))
            self.assertEqual(pkg["main"], "main.js")
            self.assertRegex(pkg["devDependencies"]["electron"], r"^\d+\.\d+\.\d+$")   # 钉死版本
            self.assertEqual(r.meta["window"]["height"], 760)
            self.assertEqual(r.meta["window"]["width"], 480)                          # 下限钳制

    def test_discoverable(self):
        self.assertIn("electron", list_outputs())


class ToolchainGuards(unittest.TestCase):
    def test_missing_toolchain_reports_gap(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(toolchain, "_which", return_value=None), \
                mock.patch.object(toolchain, "find_android_sdk", return_value=None), \
                mock.patch("subprocess.run") as run:
            a = toolchain.build_apk(d)
            b = toolchain.build_desktop(d)
            run.assert_not_called()
        self.assertFalse(a["ok"])
        self.assertIn("Flutter SDK", a["gap"])
        self.assertIn("Android SDK", a["gap"])
        self.assertFalse(b["ok"])
        self.assertIn("Node", b["gap"])


if __name__ == "__main__":
    unittest.main()
