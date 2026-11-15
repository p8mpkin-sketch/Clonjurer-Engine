"""远程发布 API: 新建校验 -> 异步发布 -> 列表/详情(进度/阶段/日志尾部) -> 启停 / 日志 / 存活探测 ->
编辑与重置指纹 -> 删除(连不上时可只删档案) -> 凭据原文与密文不出现在任何 API 返回里。

在独立子进程里导入 app(临时数据库、工作目录与主密钥); paramiko 换成 tests/_fake_ssh.py 的假实现,
平台侧 HTTP 探活也 mock, 不打真实网络、不连真主机。"""
import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from tests._env import has_modules

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = textwrap.dedent(r'''
    import json, os, sys, tempfile, time, zipfile
    from pathlib import Path
    from unittest import mock
    sys.path.insert(0, sys.argv[1])
    tmp = Path(tempfile.mkdtemp())
    os.environ["CLONER_SECRET_FILE"] = str(tmp / "secret")
    import config
    config.DB_PATH = tmp / "t.db"
    config.WORKSPACE_ROOT = tmp / "ws"; config.WORKSPACE_ROOT.mkdir()
    config.EXPORTS_DIR = tmp / "ex"; config.EXPORTS_DIR.mkdir()
    import app as A
    from core import remote_deploy as rd
    from tests._fake_ssh import FakeWorld
    A.scheduler.start = lambda *a, **k: None

    PW = "Sup3r-Secret-Pw!"
    world = FakeWorld()
    srv = world.add("vps.example.com", password=PW)
    mock.patch.object(rd, "_paramiko", lambda: world.module()).start()
    mock.patch.object(rd, "_http_ok", lambda url, timeout=3.0: True).start()

    bid = A.db.create_batch("b", [("https://a.example/", "a.example"), ("https://b.example/", "b.example")],
                            {}, workspace_id=1)
    tasks = A.db.list_tasks(batch_id=bid, workspace_id=1)
    done, pending = tasks[0]["id"], tasks[1]["id"]
    zp = config.EXPORTS_DIR / f"{done}.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr(f"clone_site_{done}/app.py", "x")
        zf.writestr(f"clone_site_{done}/requirements.txt", "flask\n")
    A.db.update_task(done, status="completed", zip_path=str(zp))

    out = {}
    bodies = []
    c = A.app.test_client()

    def call(method, url, **kw):
        r = getattr(c, method)(url, **kw)
        bodies.append(r.get_data(as_text=True))
        return r

    def wait(dep_id):
        for _ in range(500):
            d = call("get", f"/api/v4/remote/deploys/{dep_id}").get_json()["deploy"]
            if d["status"] != "deploying":
                return d
            time.sleep(0.02)
        return d

    base = {"name": "正式环境", "host": "vps.example.com", "ssh_port": 22, "username": "deploy",
            "password": PW, "clone_task_id": done, "remote_port": 8080, "note": "n"}
    out["bad"] = [call("post", "/api/v4/remote/deploys", json=dict(base, **patch)).status_code for patch in (
        {"remote_port": "8080; reboot"}, {"ssh_port": 0}, {"host": "vps;id"}, {"username": "a b"},
        {"name": ""}, {"password": ""}, {"clone_task_id": pending}, {"clone_task_id": 9999},
        {"auth_type": "agent"})]
    r = call("post", "/api/v4/remote/deploys", json=dict(base, deploy_now=False))
    out["create"] = r.status_code
    dep = r.get_json()["deploy"]
    out["created"] = [dep["status"], dep["password_masked"], dep["has_secret"], dep["remote_dir"], dep["service"]]
    dep_id = dep["id"]
    raw = A.db.get_remote_deploy(dep_id)
    out["stored_encrypted"] = bool(raw["password_enc"]) and PW not in raw["password_enc"]
    from core import security
    out["decrypts"] = security.decrypt_value(raw["password_enc"]) == PW

    out["test_conn"] = call("post", "/api/v4/remote/test-connection",
                            json={"host": "vps.example.com", "ssh_port": 22, "username": "deploy", "password": PW}).get_json()["ok"]
    out["test_conn_saved"] = call("post", "/api/v4/remote/test-connection",
                                  json={"id": dep_id, "host": "vps.example.com", "ssh_port": 22, "username": "deploy"}).get_json()["ok"]
    out["test_conn_cmds"] = len(srv.commands)

    r = call("post", f"/api/v4/remote/deploys/{dep_id}/deploy")
    out["deploy"] = [r.status_code, r.get_json().get("started")]
    d = wait(dep_id)
    out["after"] = [d["status"], d["progress"], d["stage"], d["service_url"], "部署完成" in d["deploy_log"],
                    d["host_fingerprint"].startswith("SHA256:")]
    lst = call("get", "/api/v4/remote/deploys").get_json()["deploys"]
    out["list"] = [len(lst), lst[0]["status"], "deploy_log" in lst[0], lst[0]["clone_domain"] == A.db.get_task(done)["domain"]]
    out["search"] = [len(call("get", "/api/v4/remote/deploys?q=正式").get_json()["deploys"]),
                     len(call("get", "/api/v4/remote/deploys?q=nothing").get_json()["deploys"])]
    out["logs_deploy"] = "部署完成" in call("get", f"/api/v4/remote/deploys/{dep_id}/logs").get_json()["logs"]
    out["logs_runtime"] = "waitress" in call("get", f"/api/v4/remote/deploys/{dep_id}/logs?kind=runtime&tail=50").get_json()["logs"]
    st = call("get", f"/api/v4/remote/deploys/{dep_id}/status").get_json()
    out["status"] = [st["process"], st["healthy"], st["reachable"]]
    out["stop"] = [call("post", f"/api/v4/remote/deploys/{dep_id}/stop").status_code,
                   A.db.get_remote_deploy(dep_id)["status"]]
    out["start"] = [call("post", f"/api/v4/remote/deploys/{dep_id}/start").get_json()["ok"],
                    A.db.get_remote_deploy(dep_id)["status"]]
    out["bad_action"] = call("post", f"/api/v4/remote/deploys/{dep_id}/reboot").status_code

    # 编辑: 不改口令时密文不变; 改主机 -> 自动作废旧指纹
    enc_before = A.db.get_remote_deploy(dep_id)["password_enc"]
    r = call("patch", f"/api/v4/remote/deploys/{dep_id}", json={"name": "改名", "note": "x"})
    out["patch"] = [r.status_code, r.get_json()["deploy"]["name"], A.db.get_remote_deploy(dep_id)["password_enc"] == enc_before]
    out["patch_bad"] = call("patch", f"/api/v4/remote/deploys/{dep_id}", json={"remote_port": "80 && id"}).status_code

    # TOFU: 服务器公钥被换 -> 发布失败且口令没发出去
    attempts = len(srv.auth_attempts)
    srv.key = "AAAAC3NzaC1lZDI1NTE5AAAAIImpostor"
    call("post", f"/api/v4/remote/deploys/{dep_id}/deploy")
    d = wait(dep_id)
    out["tofu"] = [d["status"], "主机密钥" in d["deploy_log"], len(srv.auth_attempts) == attempts]
    tc = call("post", "/api/v4/remote/test-connection", json={"id": dep_id, "host": "vps.example.com",
                                                             "ssh_port": 22, "username": "deploy"}).get_json()
    out["tofu_test"] = [tc["ok"], tc.get("host_key_changed")]
    call("patch", f"/api/v4/remote/deploys/{dep_id}", json={"reset_host_key": True})
    out["reset"] = A.db.get_remote_deploy(dep_id)["host_key"] == ""
    call("post", f"/api/v4/remote/deploys/{dep_id}/deploy")
    out["redeploy"] = wait(dep_id)["status"]

    # 重启恢复: 遗留 deploying -> failed
    A.db.update_remote_deploy(dep_id, status="deploying")
    out["recover"] = [A.remote_deployer.recover_after_restart(), A.db.get_remote_deploy(dep_id)["status"]]

    # 删除: 连不上时 502 + 可强制只删档案; 正常删除会停服务并可清目录
    r2 = call("post", "/api/v4/remote/deploys", json=dict(base, host="10.255.255.1", name="连不上"))
    dep2 = r2.get_json()["deploy"]["id"]
    A.db.update_remote_deploy(dep2, status="failed")
    r = call("delete", f"/api/v4/remote/deploys/{dep2}?purge=1")
    out["del_unreachable"] = [r.status_code, r.get_json().get("can_force")]
    out["del_force"] = [call("delete", f"/api/v4/remote/deploys/{dep2}?force=1").status_code,
                        A.db.get_remote_deploy(dep2) is None]
    r = call("delete", f"/api/v4/remote/deploys/{dep_id}?purge=1")
    out["del"] = [r.status_code, r.get_json().get("purged"), srv.running, srv.present,
                  call("get", f"/api/v4/remote/deploys/{dep_id}").status_code]
    out["missing"] = [call("post", "/api/v4/remote/deploys/99999/deploy").status_code,
                      call("get", "/api/v4/remote/deploys/99999/logs").status_code]

    # 凭据: 原文与 Fernet 密文都不能出现在任何一次 API 返回里, 部署日志也不能有原文
    blob = "\n".join(bodies)
    out["leak_plain"] = PW in blob
    out["leak_enc"] = enc_before in blob
    out["leak_key_field"] = '"password_enc"' in blob or '"host_key"' in blob
    out["leak_log"] = any(PW in (r["log"] or "") for r in A.db._conn.execute("SELECT log FROM remote_deploys"))
    audit = "\n".join(str(dict(r)) for r in A.db._conn.execute("SELECT * FROM audit_logs"))
    out["leak_audit"] = PW in audit or enc_before in audit
    print("RESULT" + json.dumps(out, ensure_ascii=False))
''')


@unittest.skipUnless(has_modules("flask", "cryptography"), "未安装 flask / cryptography(平台依赖)")
class RemoteDeployApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True, text=True,
                              timeout=180, cwd=ROOT)
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")]
        if not line:
            raise AssertionError(proc.stdout[-3000:] + proc.stderr[-3000:])
        cls.out = json.loads(line[-1][6:])

    def test_validation(self):
        self.assertEqual(self.out["bad"], [400] * 9)

    def test_create_stores_only_ciphertext(self):
        o = self.out
        self.assertEqual(o["create"], 201)
        self.assertEqual(o["created"][:3], ["idle", "••••••••", True])
        self.assertRegex(o["created"][3], r"^~/clonjurer_deploy/rd_\d+$")
        self.assertRegex(o["created"][4], r"^clonjurer_rd_\d+$")
        self.assertTrue(o["stored_encrypted"])
        self.assertTrue(o["decrypts"])

    def test_connection_test_only_logs_in(self):
        o = self.out
        self.assertTrue(o["test_conn"])
        self.assertTrue(o["test_conn_saved"])
        self.assertEqual(o["test_conn_cmds"], 0)

    def test_async_deploy_and_detail(self):
        o = self.out
        self.assertEqual(o["deploy"], [202, True])
        self.assertEqual(o["after"], ["running", 100, "运行中", "http://vps.example.com:8080/", True, True])
        self.assertEqual(o["list"], [1, "running", False, True])
        self.assertEqual(o["search"], [1, 0])
        self.assertTrue(o["logs_deploy"])
        self.assertTrue(o["logs_runtime"])
        self.assertEqual(o["status"], ["running", True, True])

    def test_start_stop(self):
        o = self.out
        self.assertEqual(o["stop"], [200, "stopped"])
        self.assertEqual(o["start"], [True, "running"])
        self.assertEqual(o["bad_action"], 400)

    def test_edit(self):
        o = self.out
        self.assertEqual(o["patch"], [200, "改名", True])
        self.assertEqual(o["patch_bad"], 400)

    def test_tofu_reject_and_reset(self):
        o = self.out
        self.assertEqual(o["tofu"], ["failed", True, True])
        self.assertEqual(o["tofu_test"], [False, True])
        self.assertTrue(o["reset"])
        self.assertEqual(o["redeploy"], "running")

    def test_restart_recovery(self):
        self.assertEqual(self.out["recover"], [1, "failed"])

    def test_delete(self):
        o = self.out
        self.assertEqual(o["del_unreachable"], [502, True])
        self.assertEqual(o["del_force"], [200, True])
        self.assertEqual(o["del"], [200, True, False, False, 404])
        self.assertEqual(o["missing"], [404, 404])

    def test_secret_never_in_api_or_logs(self):
        o = self.out
        self.assertFalse(o["leak_plain"])
        self.assertFalse(o["leak_enc"])
        self.assertFalse(o["leak_key_field"])
        self.assertFalse(o["leak_log"])
        self.assertFalse(o["leak_audit"])


if __name__ == "__main__":
    unittest.main()
