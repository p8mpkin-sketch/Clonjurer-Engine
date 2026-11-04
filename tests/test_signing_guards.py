"""签名步骤护栏(ROADMAP_95.md §3.3, core/uir/toolchain.py):

- 无证书 → 不抛异常, 退回 debug / ad-hoc / 不签, label 写明「未签名」且 signed=False;
- 有证书 → 口令不出现在命令行日志与返回的日志尾里, base64 解出的临时证书用完即删;
- apksigner verify 报出的证书仍是 Android Debug 时不得标「已签名」;
- CLONER_SIGN=0 整体跳过; 非 Windows 宿主不把同名 signtool 当成 Authenticode 签名器。
"""
import base64
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.uir import toolchain  # noqa: E402

_SECRET_ENVS = ("ANDROID_KEYSTORE_BASE64", "ANDROID_KEYSTORE_PATH", "ANDROID_KEYSTORE_PASSWORD", "ANDROID_KEY_ALIAS",
                "ANDROID_KEY_PASSWORD", "APPLE_SIGN_IDENTITY", "APPLE_ID", "APPLE_APP_PASSWORD", "APPLE_TEAM_ID",
                "WINDOWS_CERT_PFX_BASE64", "WINDOWS_CERT_PFX_PATH", "WINDOWS_CERT_PASSWORD", "CLONER_SIGN")


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in _SECRET_ENVS}
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=True)


@unittest.skipIf(os.name == "nt", "伪 apksigner 用 sh 脚本")
class ApkSigning(unittest.TestCase):
    def _sdk(self, d, cert="CN=Clonjurer Release"):
        bt = Path(d, "sdk", "build-tools", "36.0.0")
        bt.mkdir(parents=True)
        log = Path(d, "calls.log")
        tool = bt / "apksigner"
        tool.write_text("#!/bin/sh\n"
                        f'echo "$@" >> "{log}"\n'
                        'if [ "$1" = verify ]; then\n'
                        f'  echo "Signer #1 certificate DN: {cert}"\n'
                        "fi\n"
                        'case "$*" in *--ks*) ls "$3" >/dev/null || exit 3;; esac\n'
                        "exit 0\n")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        apk = Path(d, "app-release.apk")
        apk.write_bytes(b"PK")
        return str(Path(d, "sdk")), apk, log

    def test_no_keystore_labels_debug(self):
        with tempfile.TemporaryDirectory() as d, _clean_env():
            sdk, apk, log = self._sdk(d, cert="C=US, O=Android, CN=Android Debug")
            r = toolchain.sign_apk(apk, sdk, log=lambda m: None)
        self.assertFalse(r["signed"])
        self.assertEqual(r["status"], "unsigned-debug")
        self.assertIn("未签名", r["label"])
        self.assertTrue(r["verified"])
        self.assertIn("Android Debug", r["cert"])

    def test_keystore_from_secret(self):
        logs = []
        with tempfile.TemporaryDirectory() as d, _clean_env(
                ANDROID_KEYSTORE_BASE64=base64.b64encode(b"fake-keystore").decode(),
                ANDROID_KEYSTORE_PASSWORD="s3cret-pw", ANDROID_KEY_ALIAS="upload"):
            sdk, apk, calls = self._sdk(d)
            with mock.patch("tempfile.mkstemp", wraps=tempfile.mkstemp) as mk:
                r = toolchain.sign_apk(apk, sdk, log=logs.append)
                ks_tmp = mk.call_args_list[0].args if mk.call_args_list else None
            argv = calls.read_text()
        self.assertTrue(r["signed"], r)
        self.assertEqual(r["status"], "signed")
        self.assertNotIn("未签名", r["label"])
        self.assertIn("env:ANDROID_KEYSTORE_PASSWORD", argv)
        self.assertNotIn("s3cret-pw", argv + "\n".join(logs))
        self.assertIsNotNone(ks_tmp)
        ks_path = argv.split("--ks ")[1].split()[0]
        self.assertFalse(Path(ks_path).exists(), "base64 解出的临时 keystore 应已删除")

    def test_signed_but_debug_cert_is_not_signed(self):
        with tempfile.TemporaryDirectory() as d, _clean_env(
                ANDROID_KEYSTORE_BASE64=base64.b64encode(b"k").decode(),
                ANDROID_KEYSTORE_PASSWORD="pw", ANDROID_KEY_ALIAS="a"):
            sdk, apk, _ = self._sdk(d, cert="CN=Android Debug")
            r = toolchain.sign_apk(apk, sdk, log=lambda m: None)
        self.assertFalse(r["signed"])
        self.assertIn("未签名", r["label"])

    def test_sign_disabled(self):
        with tempfile.TemporaryDirectory() as d, _clean_env(
                CLONER_SIGN="0", ANDROID_KEYSTORE_BASE64=base64.b64encode(b"k").decode(),
                ANDROID_KEYSTORE_PASSWORD="pw", ANDROID_KEY_ALIAS="a"):
            sdk, apk, calls = self._sdk(d)
            r = toolchain.sign_apk(apk, sdk, log=lambda m: None)
            argv = calls.read_text()
        self.assertFalse(r["signed"])
        self.assertNotIn("--ks", argv)

    def test_missing_apksigner_is_gap_not_exception(self):
        with tempfile.TemporaryDirectory() as d, _clean_env(), \
                mock.patch.object(toolchain, "_which", return_value=None):
            apk = Path(d, "a.apk")
            apk.write_bytes(b"PK")
            r = toolchain.sign_apk(apk, str(Path(d, "nosdk")), log=lambda m: None)
        self.assertFalse(r["signed"])
        self.assertIn("apksigner", r["gap"])


class DesktopSigning(unittest.TestCase):
    def test_windows_without_cert(self):
        with _clean_env(), mock.patch("subprocess.run") as run:
            r = toolchain.sign_desktop_artifact("win32", Path("x.exe"), log=lambda m: None)
            run.assert_not_called()
        self.assertFalse(r["signed"])
        self.assertIn("未签名", r["label"])

    def test_windows_password_redacted(self):
        logs = []

        def fake_run(cmd, **kw):
            return mock.Mock(returncode=0, stdout="echo " + " ".join(map(str, cmd)), stderr="")
        with tempfile.TemporaryDirectory() as d, _clean_env(
                WINDOWS_CERT_PFX_BASE64=base64.b64encode(b"pfx").decode(), WINDOWS_CERT_PASSWORD="pfx-pw-123"), \
                mock.patch.object(toolchain, "_signtool", return_value=None), \
                mock.patch.object(toolchain, "_which", side_effect=lambda n, c=(): "/usr/bin/" + n), \
                mock.patch("subprocess.run", side_effect=fake_run), mock.patch("os.replace"):
            exe = Path(d, "app.exe")
            exe.write_bytes(b"MZ")
            r = toolchain.sign_windows_exe(exe, log=logs.append)
        self.assertTrue(r["signed"])
        self.assertTrue(r["timestamped"])
        self.assertNotIn("pfx-pw-123", "\n".join(logs) + repr(r))

    def test_non_windows_ignores_foreign_signtool(self):
        if os.name == "nt":
            self.skipTest("仅非 Windows")
        with mock.patch.object(toolchain, "_which", return_value="/usr/bin/signtool"):
            self.assertIsNone(toolchain._signtool())

    def test_macos_on_other_host(self):
        with mock.patch.object(toolchain, "host_platform", return_value="linux"), \
                mock.patch("subprocess.run") as run:
            r = toolchain.sign_desktop_artifact("darwin", Path("A.app"), log=lambda m: None)
            run.assert_not_called()
        self.assertFalse(r["signed"])
        self.assertIn("未签名", r["label"])

    def test_macos_adhoc_without_identity(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")
        with _clean_env(), mock.patch.object(toolchain, "host_platform", return_value="darwin"), \
                mock.patch.object(toolchain, "_which", side_effect=lambda n, c=(): "/usr/bin/" + n), \
                mock.patch("subprocess.run", side_effect=fake_run):
            r = toolchain.sign_macos_app(Path("/tmp/A.app"), log=lambda m: None)
        self.assertEqual(r["status"], "adhoc")
        self.assertFalse(r["signed"])
        self.assertIn("未签名", r["label"])
        self.assertIn("-", calls[0][calls[0].index("--sign") + 1])

    def test_linux_unsigned(self):
        r = toolchain.sign_desktop_artifact("linux", Path("app"), log=lambda m: None)
        self.assertFalse(r["signed"])


class WorkflowGuards(unittest.TestCase):
    """build.yml 只通过 secret 名引用证书, 不得内嵌证书内容; 引用的 secret 名都在 toolchain 文档里。"""

    def test_workflow_references_known_secrets(self):
        wf = ROOT / ".github" / "workflows" / "build.yml"
        if not wf.is_file():
            self.skipTest("workflow 不在本分支(推送权限受限时以补丁形式交付)")
        text = wf.read_text(encoding="utf-8")
        import re
        names = set(re.findall(r"secrets\.([A-Z0-9_]+)", text))
        self.assertTrue(names)
        doc = (ROOT / "core" / "uir" / "toolchain.py").read_text(encoding="utf-8")
        for n in names:
            self.assertIn(n, doc, f"{n} 未在 toolchain.py 签名说明里登记")
        self.assertNotRegex(text, r"BEGIN (RSA |EC )?PRIVATE KEY")

    def test_build_targets_resolve(self):
        """BUILD_TARGETS 每一行都能落到真实的 build 函数 / 装壳函数 / OutputTarget; workflow 默认值在表里。"""
        import importlib.util
        import re
        from core import app_shell
        from core.uir.registry import list_outputs
        spec = importlib.util.spec_from_file_location("ci_build", ROOT / "engine_bench" / "build" / "ci_build.py")
        ci = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ci)
        for name, t in ci.BUILD_TARGETS.items():
            self.assertTrue(callable(getattr(toolchain, t["build"], None)), name)
            self.assertIn(t["kind"], ("apk", "desktop"))
            if t["render"] == "shell":
                self.assertTrue(callable(getattr(app_shell, t["shell"], None)), name)
            else:
                self.assertIn(name, list_outputs())
        wf = ROOT / ".github" / "workflows" / "build.yml"
        if wf.is_file():
            for d in re.findall(r"inputs\.(?:android|desktop)_target \|\| '([^']+)'", wf.read_text(encoding="utf-8")):
                self.assertIn(d, ci.BUILD_TARGETS)


if __name__ == "__main__":
    unittest.main()
