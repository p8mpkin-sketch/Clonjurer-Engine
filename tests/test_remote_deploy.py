"""远程发布服务层: 命令模板构造(路径 / 端口派生)、进度映射、TOFU 指纹变化拒绝、凭据脱敏、
失败与超时、重启恢复、拆除幂等、打包同源。

paramiko 整体替换为 tests/_fake_ssh.py 的假实现(不开 socket、不连真主机); 平台侧 HTTP 探活也 mock。
凭据加解密用可逆的假函数, 不依赖 cryptography, 在 engine-gate(只装评分依赖)上同样运行。"""
import tempfile
import time
import unittest
import re
import zipfile
from pathlib import Path
from unittest import mock

from core import remote_deploy as rd
from models import Database
from tests._fake_ssh import FakeChannel, FakeKey, FakeWorld

PASSWORD = "s3cret-Pa55!"
_real_sleep = time.sleep


def enc(s):
    return "enc:" + s[::-1]


def dec(s):
    return s[4:][::-1] if s.startswith("enc:") else ""


def make_export_zip(path: Path, top: str = "clone_site_3") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{top}/app.py", "print('hi')\n")
        zf.writestr(f"{top}/requirements.txt", "flask>=3.0.0\nwaitress>=3.0.0\n")
        zf.writestr(f"{top}/templates/index.html", "<html></html>")
    return path


class CommandTemplateTests(unittest.TestCase):
    def test_paths_and_service_derive_from_id(self):
        self.assertEqual(rd.remote_dir(7), "clonjurer_deploy/rd_7")
        self.assertEqual(rd.service_name(7), "clonjurer_rd_7")
        cmds = rd.build_commands(7, 8080)
        self.assertEqual(cmds["mkdir"], "mkdir -p ~/clonjurer_deploy/rd_7")
        self.assertEqual(cmds["purge"], "rm -rf ~/clonjurer_deploy/rd_7")
        self.assertIn("cd ~/clonjurer_deploy/rd_7/app", cmds["start"])
        self.assertIn("CLONJURER_SERVICE=clonjurer_rd_7", cmds["start"])
        self.assertIn("PORT=8080 nohup ../venv/bin/python app.py 8080", cmds["start"])
        self.assertIn("~/clonjurer_deploy/rd_7/clonjurer_rd_7.pid", cmds["start"])
        self.assertIn("~/clonjurer_deploy/rd_7/clonjurer_rd_7.pid", cmds["stop"])
        self.assertIn("http://127.0.0.1:8080/__healthz", cmds["local_health"])
        self.assertIn("app/requirements.txt", cmds["deps"])
        # 每条命令里只出现这一个档案的路径, 不会串到别的档案
        for name, cmd in cmds.items():
            for other in ("rd_70", "rd_17", "rd_8"):
                self.assertNotIn(other, cmd, name)

    def test_deps_script_adapts_to_fresh_servers(self):
        cmd = rd.build_commands(7, 8080)["deps"]
        # 整段放在 sh -c '...' 里: 脚本自身不能含单引号, 否则会截断命令
        self.assertNotIn("'", rd._DEPS_SCRIPT)
        self.assertTrue(cmd.startswith("cd ~/clonjurer_deploy/rd_7 && sh -c '"))
        self.assertTrue(cmd.endswith("'"))
        for needle in ("python3 -m venv venv", "-venv", "apt-get", "dnf", "yum", "apk", "sudo -n",
                       "--without-pip", "get-pip.py", "mirrors.aliyun.com", "app/requirements.txt"):
            self.assertIn(needle, cmd, needle)

    def test_port_must_be_validated_integer(self):
        self.assertEqual(rd.validate_port("8080"), 8080)
        self.assertEqual(rd.validate_port(" 22 "), 22)
        for bad in ("8080; rm -rf ~", "8080 && id", "$(id)", "-1", "0", "65536", "80.5", "", None, True, 8.0, [80]):
            with self.assertRaises(rd.RemoteDeployError, msg=repr(bad)):
                rd.validate_port(bad)
        with self.assertRaises(rd.RemoteDeployError):
            rd.build_commands(1, "80; reboot")

    def test_id_must_be_integer(self):
        with self.assertRaises(ValueError):
            rd.build_commands("1; rm -rf /", 80)    # type: ignore[arg-type]
        self.assertEqual(rd.build_commands("12", 80)["mkdir"], "mkdir -p ~/clonjurer_deploy/rd_12")

    def test_runtime_log_tail_is_clamped_integer(self):
        self.assertEqual(rd.runtime_log_command(3, 50),
                         "tail -n 50 ~/clonjurer_deploy/rd_3/runtime.log 2>/dev/null; exit 0")
        self.assertIn("tail -n 2000 ", rd.runtime_log_command(3, 10 ** 9))
        self.assertIn("tail -n 1 ", rd.runtime_log_command(3, -5))
        self.assertIn("tail -n 200 ", rd.runtime_log_command(3, "50; id"))

    def test_free_text_never_reaches_command_line(self):
        """名称 / 备注 / 主机 / 用户名都是自由文本: 走完一次完整发布, 远端命令里一个字也不出现。"""
        h = RemoteHarness(self)
        dep = h.create(name="名称$(reboot)`id`", note="; rm -rf ~ #", remote_port=8080)
        h.deploy(dep)
        allowed = set(rd.build_commands(dep, 8080).values()) | {rd.runtime_log_command(dep, 30)}
        stamp = rd.stamp_command(dep, "0" * 64).replace("0" * 64, "")
        for cmd, _timeout in h.srv.commands:
            if cmd.replace(cmd[5:69], "") == stamp and re.fullmatch(r"[0-9a-f]{64}", cmd[5:69]):
                continue   # 摘要是本地算出的十六进制串, 其余部分固定

            self.assertIn(cmd, allowed)
            for frag in ("reboot", "`id`", "rm -rf ~ #", "名称", "vps.example.com"):
                self.assertNotIn(frag, cmd)

    def test_host_and_username_validation(self):
        self.assertEqual(rd.validate_host(" VPS.Example.com "), "vps.example.com")
        self.assertEqual(rd.validate_host("[2001:db8::1]"), "2001:db8::1")
        self.assertEqual(rd.validate_host("47.92.75.70"), "47.92.75.70")
        for bad in ("", "a b", "host;id", "user@host", "-oProxyCommand=x", "a..b", "http://x", "x" * 300):
            with self.assertRaises(rd.RemoteDeployError, msg=bad):
                rd.validate_host(bad)
        self.assertEqual(rd.validate_username(""), "root")
        for bad in ("a b", "x;id", "-l", "u" * 40):
            with self.assertRaises(rd.RemoteDeployError, msg=bad):
                rd.validate_username(bad)
        self.assertEqual(rd.service_url("2001:db8::1", 80), "http://[2001:db8::1]:80/")


class ProgressMappingTests(unittest.TestCase):
    def test_stage_bands_match_spec(self):
        bands = {k: (lo, hi) for k, _label, lo, hi in rd.STAGES}
        self.assertEqual([k for k, *_ in rd.STAGES],
                         ["package", "connect", "upload", "extract", "deps", "launch", "health"])
        self.assertEqual(bands, {"package": (5, 20), "connect": (18, 18), "upload": (24, 42),
                                 "extract": (46, 46), "deps": (52, 78), "launch": (82, 82),
                                 "health": (90, 100)})

    def test_progress_for_interpolates_and_clamps(self):
        self.assertEqual(rd.progress_for("package"), 5)
        self.assertEqual(rd.progress_for("upload", 0.5), 33)
        self.assertEqual(rd.progress_for("upload", 1), 42)
        self.assertEqual(rd.progress_for("deps", 2.0), 78)
        self.assertEqual(rd.progress_for("deps", -1), 52)
        self.assertEqual(rd.progress_for("health", 1.0), 100)
        self.assertEqual(rd.stage_label("deps"), "安装依赖")
        with self.assertRaises(KeyError):
            rd.progress_for("nope")

    def test_full_run_progress_is_monotonic_and_ends_at_100(self):
        h = RemoteHarness(self)
        dep = h.create()
        seen = []
        orig = h.db.update_remote_deploy

        def spy(dep_id, **fields):
            if "progress" in fields:
                seen.append((fields["progress"], fields.get("stage")))
            return orig(dep_id, **fields)
        with mock.patch.object(h.db, "update_remote_deploy", side_effect=spy):
            h.deploy(dep)
        pcts = [p for p, _ in seen]
        self.assertEqual(pcts, sorted(pcts), "进度不能倒退")
        self.assertEqual(pcts[-1], 100)
        stages = []
        for _p, s in seen:
            if s and (not stages or stages[-1] != s):
                stages.append(s)
        self.assertEqual(stages, ["打包工程", "连接服务器", "上传工程包", "解压工程", "安装依赖",
                                  "启动服务", "健康检查", "运行中"])
        rec = h.db.get_remote_deploy(dep)
        self.assertEqual((rec["status"], rec["progress"]), ("running", 100))


class RemoteHarness:
    """临时库 + 假服务器 + 导出 ZIP。deploy() 在当前线程跑完一次发布。"""

    def __init__(self, case: unittest.TestCase, host: str = "vps.example.com"):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "t.db")
        case.addCleanup(self.db.close)
        self.world = FakeWorld()
        self.host = host
        self.srv = self.world.add(host, password=PASSWORD)
        self.export = make_export_zip(self.tmp / "3.zip")
        self.packaged: list = []

        def package(task_id):
            self.packaged.append(task_id)
            return self.export
        self.dep = rd.RemoteDeployer(self.db, package, dec, self.tmp / "remote")
        for p in (mock.patch.object(rd, "_paramiko", lambda: self.world.module()),
                  mock.patch.object(rd, "_http_ok", lambda url, timeout=3.0: True),
                  mock.patch.object(rd.time, "sleep", lambda s: _real_sleep(min(s, 0.005)))):
            p.start()
            case.addCleanup(p.stop)

    def create(self, name="正式环境", note="", remote_port=8080, password=PASSWORD, host=None) -> int:
        return self.db.create_remote_deploy(name, host or self.host, 22, "deploy", enc(password) if password else "",
                                            3, remote_port, note)

    def deploy(self, dep_id: int):
        self.dep.begin_deploy(dep_id)
        self.dep.run_deploy(dep_id)
        return self.db.get_remote_deploy(dep_id)


class DeployFlowTests(unittest.TestCase):
    def setUp(self):
        self.h = RemoteHarness(self)

    def test_happy_path_uploads_bundle_and_runs_fixed_steps(self):
        dep = self.h.create()
        rec = self.h.deploy(dep)
        self.assertEqual(rec["status"], "running")
        self.assertEqual(rec["service_url"], "http://vps.example.com:8080/")
        self.assertEqual(self.h.packaged, [3])
        local, remote, size = self.h.srv.uploads[0]
        self.assertEqual(remote, f"/home/deploy/clonjurer_deploy/rd_{dep}/app.zip")
        with zipfile.ZipFile(local) as zf:   # 与导出包同源, 只是顶层目录换成 app/
            self.assertEqual(sorted(zf.namelist()), ["app/app.py", "app/requirements.txt", "app/templates/index.html"])
        cmds = rd.build_commands(dep, 8080)
        ran = [c for c, _t in self.h.srv.commands]
        digest = rd.bundle_digest(local)
        order = [cmds["mkdir"], cmds["bundle_state"], cmds["extract"], rd.stamp_command(dep, digest),
                 cmds["deps"], cmds["stop"], cmds["start"], cmds["local_health"]]
        self.assertEqual(ran[:len(order)], order)
        for cmd, timeout in self.h.srv.commands:  # 每条命令都带超时
            self.assertTrue(timeout and timeout > 0, cmd)
        client = self.h.world.clients[0]
        self.assertEqual(client.connect_kwargs["timeout"], rd.SSH_TIMEOUT)
        self.assertFalse(client.connect_kwargs["allow_agent"])
        self.assertFalse(client.connect_kwargs["look_for_keys"])
        self.assertFalse(client.loaded_system_keys)
        self.assertTrue(client.closed)
        self.assertIn("部署完成", rec["log"])

    def test_deps_failure_sets_failed_with_hint_and_keeps_log(self):
        self.h.srv.fail["pip install"] = (1, "Error: ensurepip is not available\n")
        rec = self.h.deploy(self.h.create())
        self.assertEqual(rec["status"], "failed")
        self.assertIn("python3-venv", rec["log"])
        self.assertIn("ensurepip is not available", rec["log"])
        self.assertIn("开始发布", rec["log"])
        self.assertFalse(self.h.dep.is_busy(rec["id"]))

    def test_deps_failure_without_root_explains_permission(self):
        self.h.srv.fail["pip install"] = (1, "ensurepip is not available\nno root or sudo\n")
        rec = self.h.deploy(self.h.create())
        self.assertEqual(rec["status"], "failed")
        self.assertIn("没有 root 或免密 sudo", rec["log"])

    def test_health_failure_appends_runtime_tail(self):
        self.h.srv.healthy = False
        with mock.patch.object(rd, "HEALTH_TIMEOUT_S", 0.05):
            rec = self.h.deploy(self.h.create())
        self.assertEqual(rec["status"], "failed")
        self.assertIn("健康检查", rec["log"])
        self.assertIn("运行日志尾部", rec["log"])

    def test_unreachable_from_platform_still_running_with_warning(self):
        with mock.patch.object(rd, "_http_ok", lambda url, timeout=3.0: False):
            rec = self.h.deploy(self.h.create())
        self.assertEqual(rec["status"], "running")
        self.assertIn("WARNING", rec["log"])
        self.assertIn("TCP 8080", rec["log"])

    def test_command_timeout_closes_channel_and_fails(self):
        self.h.srv.hang = "pip install"
        with mock.patch.dict(rd.CMD_TIMEOUTS, {"deps": 0.05}):
            rec = self.h.deploy(self.h.create())
        self.assertEqual(rec["status"], "failed")
        self.assertIn("超时", rec["log"])

    def test_run_command_timeout_unit(self):
        chan = FakeChannel(never_exits=True)
        client = mock.Mock()
        client.exec_command.return_value = (None, mock.Mock(channel=chan), None)
        with self.assertRaises(rd.RemoteDeployError):
            rd.run_command(client, "true", timeout=0.05)
        self.assertTrue(chan.closed)

    def test_unreachable_host(self):
        dep = self.h.create(host="10.255.255.1")
        rec = self.h.deploy(dep)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("连接超时", rec["log"])

    def test_concurrent_deploy_rejected(self):
        dep = self.h.create()
        self.h.dep.begin_deploy(dep)
        with self.assertRaises(rd.RemoteDeployError):
            self.h.dep.begin_deploy(dep)
        self.h.dep.run_deploy(dep)
        self.assertFalse(self.h.dep.is_busy(dep))

    def test_start_stop_and_deploy_are_mutually_exclusive(self):
        dep = self.h.create()
        self.h.deploy(dep)
        # 发布进行中: 启动 / 停止被拒
        self.h.dep.begin_deploy(dep)
        for op in (self.h.dep.start, self.h.dep.stop):
            with self.assertRaises(rd.RemoteDeployError):
                op(dep)
        self.h.dep.run_deploy(dep)
        # 启动进行中: 发布被拒, 结束后释放
        seen = []
        orig = self.h.dep._session

        def session(dep_id, fn):
            with self.assertRaises(rd.RemoteDeployError):
                self.h.dep.begin_deploy(dep_id)
            seen.append(True)
            return orig(dep_id, fn)
        with mock.patch.object(self.h.dep, "_session", session):
            self.h.dep.start(dep)
        self.assertEqual(seen, [True])
        self.assertFalse(self.h.dep.is_busy(dep))

    def test_unchanged_bundle_skips_upload_and_extract(self):
        dep = self.h.create()
        self.h.deploy(dep)
        digest = rd.bundle_digest(self.h.srv.uploads[0][0])
        cmds = rd.build_commands(dep, 8080)
        self.h.srv.fail[cmds["bundle_state"]] = (0, f"{digest}\nHAS_APP\n")
        n_up, n_cmd = len(self.h.srv.uploads), len(self.h.srv.commands)
        rec = self.h.deploy(dep)
        self.assertEqual(rec["status"], "running")
        self.assertEqual(len(self.h.srv.uploads), n_up)            # 没再上传
        ran = [c for c, _t in self.h.srv.commands[n_cmd:]]
        self.assertNotIn(cmds["extract"], ran)                     # 也没再解压
        self.assertIn("跳过上传和解压", rec["log"])

    def test_bundle_digest_ignores_zip_metadata(self):
        a, b = self.h.tmp / "a.zip", self.h.tmp / "b.zip"
        with zipfile.ZipFile(a, "w", zipfile.ZIP_STORED) as z:
            z.writestr(zipfile.ZipInfo("app/x.txt", (2020, 1, 1, 0, 0, 0)), "hi")
        with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(zipfile.ZipInfo("app/x.txt", (2026, 5, 5, 0, 0, 0)), "hi")
        self.assertEqual(rd.bundle_digest(a), rd.bundle_digest(b))
        with self.assertRaises(rd.RemoteDeployError):
            rd.stamp_command(1, "abc; reboot")

    def test_missing_secret_rejected_before_connect(self):
        dep = self.h.create(password="")
        with self.assertRaises(rd.RemoteDeployError):
            self.h.dep.begin_deploy(dep)
        self.assertEqual(self.h.world.clients, [])

    def test_restart_marks_stale_deploying_failed(self):
        a, b = self.h.create(), self.h.create()
        self.h.db.update_remote_deploy(a, status="deploying", progress=52, stage="安装依赖")
        self.h.db.update_remote_deploy(b, status="running")
        self.assertEqual(self.h.dep.recover_after_restart(), 1)
        ra, rb = self.h.db.get_remote_deploy(a), self.h.db.get_remote_deploy(b)
        self.assertEqual(ra["status"], "failed")
        self.assertIn("平台重启", ra["log"])
        self.assertEqual(rb["status"], "running")
        self.assertEqual(self.h.dep.recover_after_restart(), 0)

    def test_start_stop_status_logs(self):
        dep = self.h.create()
        self.h.deploy(dep)
        self.assertEqual(self.h.dep.stop(dep), {"ok": True, "status": "stopped"})
        self.assertEqual(self.h.db.get_remote_deploy(dep)["status"], "stopped")
        self.assertEqual(self.h.dep.probe(dep)["process"], "stopped")
        self.assertTrue(self.h.dep.start(dep)["ok"])
        self.assertEqual(self.h.db.get_remote_deploy(dep)["status"], "running")
        probe = self.h.dep.probe(dep)
        self.assertEqual((probe["process"], probe["healthy"], probe["reachable"]), ("running", True, True))
        self.assertIn("waitress", self.h.dep.runtime_logs(dep, 50))
        self.assertIn(rd.runtime_log_command(dep, 50), [c for c, _ in self.h.srv.commands])

    def test_start_before_first_deploy_is_refused(self):
        dep = self.h.create()
        with self.assertRaises(rd.RemoteDeployError):
            self.h.dep.start(dep)

    def test_teardown_idempotent_and_optional_purge(self):
        dep = self.h.create()
        # 从没发布过: 服务与目录都不存在, 依然成功
        self.assertEqual(self.h.dep.teardown(dep), {"ok": True, "purged": False})
        self.h.deploy(dep)
        self.assertEqual(self.h.dep.teardown(dep, purge=True), {"ok": True, "purged": True})
        self.assertFalse(self.h.srv.running)
        self.assertFalse(self.h.srv.present)
        self.assertIn(f"rm -rf ~/clonjurer_deploy/rd_{dep}", [c for c, _ in self.h.srv.commands])
        self.assertEqual(self.h.dep.teardown(dep, purge=True), {"ok": True, "purged": True})


class HostKeyTofuTests(unittest.TestCase):
    def setUp(self):
        self.h = RemoteHarness(self)

    def test_verify_host_key_unit(self):
        a, b = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAA", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBB"
        rd.verify_host_key("", a)          # 首次: 放行
        rd.verify_host_key(a, a)           # 一致: 放行
        with self.assertRaises(rd.HostKeyChanged) as cm:
            rd.verify_host_key(a, b)
        self.assertIn(rd.fingerprint(a), str(cm.exception))
        self.assertIn(rd.fingerprint(b), str(cm.exception))
        self.assertTrue(rd.fingerprint(a).startswith("SHA256:"))
        self.assertEqual(rd.host_key_id(FakeKey("QUJD")), "ssh-ed25519 QUJD")

    def test_first_connect_records_then_changed_key_aborts_before_auth(self):
        dep = self.h.create()
        rec = self.h.deploy(dep)
        self.assertEqual(rec["host_key"], "ssh-ed25519 " + self.h.srv.key)
        self.assertIn("首次连接", rec["log"])
        self.assertEqual(self.h.srv.auth_attempts, [PASSWORD])

        self.h.srv.key = "AAAAC3NzaC1lZDI1NTE5AAAAIImpostorKey"   # 服务器公钥变了
        n_cmds = len(self.h.srv.commands)
        rec2 = self.h.deploy(dep)
        self.assertEqual(rec2["status"], "failed")
        self.assertIn("主机密钥", rec2["log"])
        self.assertIn("未发送凭据", rec2["log"])
        self.assertEqual(self.h.srv.auth_attempts, [PASSWORD], "指纹不符时不能把口令发出去")
        self.assertEqual(len(self.h.srv.commands), n_cmds, "指纹不符时不能执行任何远端命令")
        self.assertEqual(rec2["host_key"], rec["host_key"], "不能用新指纹覆盖旧记录")
        for op in (self.h.dep.start, self.h.dep.stop, self.h.dep.probe):
            with self.assertRaises(rd.RemoteDeployError):
                op(dep)

    def test_reset_host_key_allows_retrust(self):
        dep = self.h.create()
        self.h.deploy(dep)
        self.h.srv.key = "AAAAC3NzaC1lZDI1NTE5AAAAINewKeyAfterReinstall"
        self.assertEqual(self.h.deploy(dep)["status"], "failed")
        self.h.db.update_remote_deploy(dep, host_key="")   # 用户在编辑里勾选「重置主机指纹」
        rec = self.h.deploy(dep)
        self.assertEqual(rec["status"], "running")
        self.assertEqual(rec["host_key"], "ssh-ed25519 " + self.h.srv.key)

    def test_test_connection_only_logs_in(self):
        ok = rd.RemoteDeployer.test_connection("vps.example.com", 22, "deploy", PASSWORD)
        self.assertTrue(ok["ok"], ok)
        self.assertTrue(ok["fingerprint"].startswith("SHA256:"))
        self.assertEqual(self.h.srv.commands, [], "连接测试不执行任何远端命令")
        self.assertEqual(self.h.srv.uploads, [])
        changed = rd.RemoteDeployer.test_connection("vps.example.com", 22, "deploy", PASSWORD,
                                                    stored_host_key="ssh-ed25519 QUJD")
        self.assertFalse(changed["ok"])
        self.assertTrue(changed["host_key_changed"])
        bad = rd.RemoteDeployer.test_connection("vps.example.com", 22, "deploy", "wrong")
        self.assertEqual((bad["ok"], bad["error"]), (False, "认证失败: 用户名、口令或私钥不对"))
        self.assertFalse(rd.RemoteDeployer.test_connection("vps;id", 22, "deploy", PASSWORD)["ok"])
        self.assertFalse(rd.RemoteDeployer.test_connection("vps.example.com", "22;id", "deploy", PASSWORD)["ok"])


class SecretMaskingTests(unittest.TestCase):
    def setUp(self):
        self.h = RemoteHarness(self)

    def test_present_never_contains_secret_or_ciphertext(self):
        dep = self.h.create()
        self.h.deploy(dep)
        rec = self.h.db.get_remote_deploy(dep)
        out = rd.RemoteDeployer.present(rec, log_tail=500)
        self.assertNotIn("password_enc", out)
        self.assertNotIn("host_key", out)
        self.assertEqual(out["password_masked"], rd.MASK)
        self.assertTrue(out["has_secret"])
        blob = repr(out)
        self.assertNotIn(PASSWORD, blob)
        self.assertNotIn(rec["password_enc"], blob)
        self.assertTrue(out["host_fingerprint"].startswith("SHA256:"))
        self.assertEqual(out["remote_dir"], f"~/clonjurer_deploy/rd_{dep}")
        self.assertEqual(out["service"], f"clonjurer_rd_{dep}")
        empty = rd.RemoteDeployer.present(dict(rec, password_enc=""))
        self.assertEqual((empty["password_masked"], empty["has_secret"]), ("", False))
        self.assertEqual(rd.mask_secret("abc"), rd.MASK)
        self.assertNotIn("a", rd.mask_secret("abc"))   # 前 0 位 + 掩码

    def test_deploy_log_scrubs_echoed_secret(self):
        self.h.srv.echo = f"Looking in indexes: https://user:{PASSWORD}@pypi.example/simple\n"
        rec = self.h.deploy(self.h.create())
        self.assertEqual(rec["status"], "running")
        self.assertNotIn(PASSWORD, rec["log"])
        self.assertIn(rd.MASK, rec["log"])

    def test_failure_paths_never_log_secret(self):
        self.h.srv.fail["zipfile"] = (2, f"BadZipFile: password {PASSWORD} rejected\n")
        rec = self.h.deploy(self.h.create())
        self.assertEqual(rec["status"], "failed")
        self.assertNotIn(PASSWORD, rec["log"])
        dep2 = self.h.create(password="another-secret-123")
        rec2 = self.h.deploy(dep2)   # 认证失败
        self.assertEqual(rec2["status"], "failed")
        self.assertIn("认证失败", rec2["log"])
        self.assertNotIn("another-secret-123", rec2["log"])

    def test_scrub_masks_private_key_lines(self):
        key = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAA\n-----END OPENSSH PRIVATE KEY-----"
        out = rd.scrub("oops b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAA leaked", key)
        self.assertNotIn("b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAA", out)


class PackagingTests(unittest.TestCase):
    def test_repack_renames_top_folder_only(self):
        tmp = Path(tempfile.mkdtemp())
        src = make_export_zip(tmp / "src.zip", top="clone_site_42")
        out = rd.repack_export_zip(src, tmp / "out" / "rd_1.zip")
        with zipfile.ZipFile(src) as a, zipfile.ZipFile(out) as b:
            self.assertEqual(sorted(b.namelist()), sorted("app/" + n.split("/", 1)[1] for n in a.namelist()))
            for n in a.namelist():
                self.assertEqual(a.read(n), b.read("app/" + n.split("/", 1)[1]))

    def test_repack_rejects_traversal_and_multi_top(self):
        tmp = Path(tempfile.mkdtemp())
        evil = tmp / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("top/../../etc/cron.d/x", "boom")
        with self.assertRaises(rd.RemoteDeployError):
            rd.repack_export_zip(evil, tmp / "o1.zip")
        multi = tmp / "multi.zip"
        with zipfile.ZipFile(multi, "w") as zf:
            zf.writestr("a/app.py", "")
            zf.writestr("b/app.py", "")
        with self.assertRaises(rd.RemoteDeployError):
            rd.repack_export_zip(multi, tmp / "o2.zip")


if __name__ == "__main__":
    unittest.main()
