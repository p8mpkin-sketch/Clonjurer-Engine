"""用户管理与日志审计模块: 成员筛选/详情/批量、登录锁定、会话失效、审计筛选/分页/统计/导出、跨区不串。

在独立子进程里导入 app(临时数据库与工作目录), 不污染其他测试与本机实例。"""
import importlib.util
import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = textwrap.dedent(r'''
    import json, sys, tempfile
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    import config
    tmp = Path(tempfile.mkdtemp())
    config.DB_PATH = tmp / "t.db"
    config.WORKSPACE_ROOT = tmp / "ws"; config.WORKSPACE_ROOT.mkdir()
    config.EXPORTS_DIR = tmp / "ex"; config.EXPORTS_DIR.mkdir()
    config.LOGIN_FAIL_MAX = 3
    config.CAPTCHA_MODE = "off"            # 这里测账号锁定本身; 验证码另见 test_websec
    from core.scheduler import Scheduler as _Sched
    _Sched.start = lambda self: None   # 导入 app 时 _boot() 就会起调度线程, 事后改实例来不及
    import app as A
    A.scheduler.start = lambda *a, **k: None
    A.time.sleep = lambda s: None          # 跳过登录失败节流
    out = {}
    admin = A.app.test_client()
    out["open_users"] = admin.get("/api/v4/users").status_code
    admin.post("/api/auth/bootstrap", json={"username": "alice", "password": "pw-alice-123"})
    for name, role in (("bob", "operator"), ("carol", "viewer"), ("dave", "admin")):
        admin.post("/api/v4/users", json={"username": name, "password": "pw-" + name + "-1234", "role": role})
    d = admin.get("/api/v4/users").get_json()
    out["summary"] = d["summary"]
    out["policy_lock"] = d["policy"]["lock_after"]
    ids = {u["username"]: u["id"] for u in d["users"]}

    # 会话版本: bob 登录后被强制下线 -> 旧会话 401
    bob = A.app.test_client()
    out["bob_login"] = bob.post("/api/auth/login", json={"username": "bob", "password": "pw-bob-1234"}).status_code
    out["bob_tasks_before"] = bob.get("/api/tasks").status_code
    out["force_logout"] = admin.post(f"/api/v4/users/{ids['bob']}/logout").status_code
    out["bob_tasks_after"] = bob.get("/api/tasks").status_code

    # 登录锁定: 3 次错密码后锁定, 正确密码也 423; 管理员解锁后可登录
    eve = A.app.test_client()
    codes = [eve.post("/api/auth/login", json={"username": "carol", "password": "wrong-pass"}).status_code for _ in range(3)]
    out["fail_codes"] = codes
    out["locked_right_pw"] = eve.post("/api/auth/login", json={"username": "carol", "password": "pw-carol-1234"}).status_code
    out["status_locked"] = next(u["status"] for u in admin.get("/api/v4/users").get_json()["users"] if u["username"] == "carol")
    out["unlock"] = admin.post(f"/api/v4/users/{ids['carol']}/unlock").status_code
    out["after_unlock"] = eve.post("/api/auth/login", json={"username": "carol", "password": "pw-carol-1234"}).status_code

    # 成员详情: 登录记录与最近操作
    det = admin.get(f"/api/v4/users/{ids['carol']}")
    out["detail"] = det.status_code
    dj = det.get_json() if det.status_code == 200 else {}
    out["detail_logins"] = sorted({e["action"] for e in dj.get("logins", [])})

    # 批量: 把 bob、carol 改为只读; 再批量移出含自己 -> 自己那条失败, 其他成功
    r = admin.post("/api/v4/users/bulk", json={"ids": [ids["bob"], ids["carol"]], "op": "role", "role": "viewer"}).get_json()
    out["bulk_role"] = (r["done"], r["failed"])
    r = admin.post("/api/v4/users/bulk", json={"ids": [ids["alice"], ids["carol"]], "op": "remove"}).get_json()
    out["bulk_remove"] = (r["done"], r["failed"], [x["ok"] for x in r["results"]])
    out["bulk_bad"] = admin.post("/api/v4/users/bulk", json={"ids": [], "op": "remove"}).status_code

    # 重置密码: 旧会话失效
    dave = A.app.test_client()
    dave.post("/api/auth/login", json={"username": "dave", "password": "pw-dave-1234"})
    out["reset_pw"] = admin.patch(f"/api/v4/users/{ids['dave']}", json={"password": "new-pass-5678"}).status_code
    out["dave_after_reset"] = dave.get("/api/tasks").status_code
    # 自己改密不踢自己
    out["self_pw"] = admin.patch(f"/api/v4/users/{ids['alice']}", json={"password": "pw-alice-456"}).status_code
    out["self_still_in"] = admin.get("/api/tasks").status_code
    # 最后一个所有者不能被停用
    out["disable_self"] = admin.post(f"/api/v4/users/{ids['alice']}/disable").status_code

    # 审计: 分类 / 结果 / 分页 / 统计 / 详情 / 导出
    a = admin.get("/api/v4/audit?category=auth&result=fail&size=10").get_json()
    out["fail_actions"] = sorted({e["action"] for e in a["entries"]})
    out["fail_label"] = a["entries"][0]["label"] if a["entries"] else ""
    a = admin.get("/api/v4/audit?size=10&page=1").get_json()
    out["paged"] = (len(a["entries"]) <= 10, a["pages"] >= 2, a["total"] > 10)
    a2 = admin.get("/api/v4/audit?size=10&page=2").get_json()
    out["page2_distinct"] = not ({e["id"] for e in a["entries"]} & {e["id"] for e in a2["entries"]})
    out["by_user"] = sorted({e["username"] for e in admin.get("/api/v4/audit?username=bob").get_json()["entries"]})
    out["sensitive_all"] = all(e["sensitive"] for e in admin.get("/api/v4/audit?sensitive=1").get_json()["entries"])
    out["q_like_escape"] = admin.get("/api/v4/audit?q=%25").get_json()["total"]
    st = admin.get("/api/v4/audit/stats?days=7").get_json()
    out["stats"] = (st["total"] > 0, len(st["by_day"]), st["failed"] >= 3, st["sensitive"] > 0)
    eid = a["entries"][0]["id"]
    out["entry"] = admin.get(f"/api/v4/audit/{eid}").status_code
    ex = admin.get("/api/v4/audit/export?category=auth")
    out["export"] = (ex.status_code, ex.data.decode("utf-8")[:1] == "﻿", "登录失败" in ex.data.decode("utf-8"))
    out["export_logged"] = admin.get("/api/v4/audit?action=audit.export").get_json()["total"]
    mx = admin.get("/api/v4/users/export")
    out["members_export"] = (mx.status_code, "alice" in mx.data.decode("utf-8"))

    # 非管理员不能看审计和成员
    v = A.app.test_client()
    v.post("/api/auth/login", json={"username": "bob", "password": "pw-bob-1234"})
    out["viewer_audit"] = v.get("/api/v4/audit").status_code
    out["viewer_users"] = v.get("/api/v4/users").status_code

    # 跨区: bob 自建乙区, 乙区审计里看不到甲区记录; 甲区看不到乙区记录
    v.post("/api/v4/workspaces", json={"name": "乙团队"})
    v.post("/api/v4/projects", json={"name": "乙项目"})
    b_entries = v.get("/api/v4/audit?size=200").get_json()["entries"]
    out["b_ws_actions"] = sorted({e["action"] for e in b_entries})
    a_all = admin.get("/api/v4/audit?size=200").get_json()["entries"]
    out["a_sees_b_project"] = any("乙项目" in e["detail"] for e in a_all)
    out["a_get_b_entry"] = admin.get(f"/api/v4/audit/{b_entries[0]['id']}").status_code
    print(json.dumps(out, ensure_ascii=False))
''')


@unittest.skipUnless(importlib.util.find_spec("flask"), "需要 flask(平台依赖)")
class IamModuleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True,
                           text=True, timeout=180, cwd=str(ROOT))
        if p.returncode != 0:
            raise AssertionError(p.stderr[-4000:])
        cls.out = json.loads(p.stdout.strip().splitlines()[-1])

    def test_summary_and_policy(self):
        o = self.out
        self.assertEqual(o["open_users"], 200)
        self.assertEqual(o["summary"]["total"], 4)
        self.assertEqual(o["summary"]["managers"], 2)
        self.assertEqual(o["policy_lock"], 3)

    def test_force_logout_invalidates_session(self):
        self.assertEqual(self.out["bob_login"], 200)
        self.assertEqual(self.out["bob_tasks_before"], 200)
        self.assertEqual(self.out["force_logout"], 200)
        self.assertEqual(self.out["bob_tasks_after"], 401)

    def test_login_lock_and_unlock(self):
        self.assertEqual(self.out["fail_codes"], [401, 401, 401])
        self.assertEqual(self.out["locked_right_pw"], 423)
        self.assertEqual(self.out["status_locked"], "locked")
        self.assertEqual(self.out["unlock"], 200)
        self.assertEqual(self.out["after_unlock"], 200)

    def test_bulk(self):
        self.assertEqual(self.out["bulk_role"], [1, 0])   # carol 本来就是只读, 不算变更
        self.assertEqual(self.out["bulk_remove"], [1, 1, [False, True]])
        self.assertEqual(self.out["bulk_bad"], 400)

    def test_password_reset_kicks_sessions(self):
        self.assertEqual(self.out["reset_pw"], 200)
        self.assertEqual(self.out["dave_after_reset"], 401)
        self.assertEqual(self.out["self_pw"], 200)
        self.assertEqual(self.out["self_still_in"], 200)
        self.assertEqual(self.out["disable_self"], 409)

    def test_member_detail(self):
        self.assertEqual(self.out["detail"], 200)
        self.assertIn("login.failed", self.out["detail_logins"])
        self.assertIn("login.locked", self.out["detail_logins"])

    def test_audit_query(self):
        o = self.out
        self.assertTrue(set(o["fail_actions"]) <= {"login.failed", "login.locked", "login.blocked"})
        self.assertTrue(o["fail_actions"])
        self.assertTrue(o["fail_label"])
        self.assertEqual(o["paged"], [True, True, True])
        self.assertTrue(o["page2_distinct"])
        self.assertEqual(o["by_user"], ["bob"])
        self.assertTrue(o["sensitive_all"])
        self.assertEqual(o["q_like_escape"], 0)
        self.assertEqual(o["stats"], [True, 7, True, True])
        self.assertEqual(o["entry"], 200)

    def test_exports(self):
        self.assertEqual(self.out["export"], [200, True, True])
        self.assertGreaterEqual(self.out["export_logged"], 1)
        self.assertEqual(self.out["members_export"], [200, True])

    def test_rbac_and_isolation(self):
        o = self.out
        self.assertEqual(o["viewer_audit"], 403)
        self.assertEqual(o["viewer_users"], 403)
        self.assertIn("project.create", o["b_ws_actions"])
        self.assertFalse(o["a_sees_b_project"])
        self.assertEqual(o["a_get_b_entry"], 404)


if __name__ == "__main__":
    unittest.main()
