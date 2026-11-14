"""多租户隔离: 甲工作区的成员对乙工作区的每类资源都只能拿到 404/空列表。

在独立子进程里导入 app(临时数据库与工作目录), 不污染其他测试与本机实例。"""
import json
import subprocess
import sys
import textwrap
import importlib.util
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
    config.NEW_WORKSPACE_PLAN = "free"   # 模拟对外 SaaS: 新区默认免费版
    import app as A
    A.scheduler.start = lambda *a, **k: None          # 不真跑克隆
    out = {}
    c1, c2 = A.app.test_client(), A.app.test_client()
    # 开放模式 -> 初始化管理员(平台管理员 + 默认工作区所有者)
    r = c1.post("/api/auth/bootstrap", json={"username": "alice", "password": "pw-alice-123"})
    out["bootstrap"] = r.status_code
    # 甲区(默认区)建项目和批次
    p1 = c1.post("/api/v4/projects", json={"name": "甲项目"}).get_json()["project"]["id"]
    b1 = c1.post("/api/batches", json={"urls": ["https://a.example/"], "project_id": p1}).get_json()
    t1 = c1.get("/api/tasks").get_json()["tasks"][0]["id"]
    # 邀请 bob 以操作员加入甲区, 然后 bob 自建乙区
    inv = c1.post("/api/v4/invites", json={"role": "operator"}).get_json()["url"].split("invite=")[1]
    out["invite_info"] = c2.get(f"/api/auth/invite/{inv}").status_code
    out["join"] = c2.post(f"/api/auth/invite/{inv}", json={"username": "bob", "password": "pw-bob-1234"}).status_code
    out["invite_reuse"] = A.app.test_client().post(f"/api/auth/invite/{inv}", json={"username": "eve", "password": "pw-eve-1234"}).status_code
    out["bob_sees_a"] = len(c2.get("/api/tasks").get_json()["tasks"])
    r = c2.post("/api/v4/workspaces", json={"name": "乙团队"})
    out["create_ws"] = r.status_code
    p2 = c2.post("/api/v4/projects", json={"name": "乙项目"}).get_json()["project"]["id"]
    c2.post("/api/batches", json={"urls": ["https://b.example/"], "project_id": p2})
    t2 = c2.get("/api/tasks").get_json()["tasks"][0]["id"]
    # alice 不是乙区成员: 乙区的一切对她不可见
    out["a_tasks"] = sorted(t["id"] for t in c1.get("/api/tasks").get_json()["tasks"])
    out["t1"], out["t2"] = t1, t2
    out["a_get_t2"] = c1.get(f"/api/tasks/{t2}").status_code
    out["a_preview_t2"] = c1.get(f"/preview/{t2}/").status_code
    out["a_dl_t2"] = c1.get(f"/api/download/{t2}").status_code
    out["a_rebuild_t2"] = c1.post(f"/api/tasks/{t2}/rebuild").status_code
    out["a_share_t2"] = c1.post(f"/api/tasks/{t2}/share").status_code
    out["a_patch_p2"] = c1.patch(f"/api/v4/projects/{p2}", json={"name": "偷改"}).status_code
    out["a_del_p2"] = c1.delete(f"/api/v4/projects/{p2}").status_code
    out["a_projects"] = [p["name"] for p in c1.get("/api/v4/projects").get_json()["projects"]]
    out["a_batch_into_p2"] = c1.post("/api/batches", json={"urls": ["https://x.example/"], "project_id": p2}).status_code
    out["a_switch_b"] = c1.post("/api/v4/workspaces/switch", json={"id": 2}).status_code
    out["a_batches"] = len(c1.get("/api/batches").get_json()["batches"])
    out["a_tel_batches"] = len(c1.get("/api/telemetry").get_json()["batches"])
    # bob 切回甲区: 能看甲, 但看不到乙
    c2.post("/api/v4/workspaces/switch", json={"id": 1})
    out["b_in_a_tasks"] = sorted(t["id"] for t in c2.get("/api/tasks").get_json()["tasks"])
    out["b_in_a_users"] = c2.get("/api/v4/users").status_code       # 操作员不能管成员
    out["b_admin_settings"] = c2.get("/api/v4/admin/settings").status_code  # 非平台管理员
    # 乙区所有者 bob 对甲区 t1 仍可见(他是甲区成员), 对乙区自己的 t2 可见
    out["b_get_t1"] = c2.get(f"/api/tasks/{t1}").status_code
    # 分享链接: 匿名可看, 篡改签名不行
    url = c1.post(f"/api/tasks/{t1}/share", json={"hours": 1}).get_json()["url"]
    anon = A.app.test_client()
    tok = url.split("share=")[1]
    out["anon_plain"] = anon.get(f"/api/tasks/{t1}").status_code
    out["anon_bad_share"] = anon.get(f"/preview/{t1}/?share={tok[:-2]}xx").status_code
    out["anon_share_auth"] = A._share_token_ok(t1, tok)
    out["anon_share_other_task"] = A._share_token_ok(t2, tok)
    # 配额: 乙区(免费版 30 次/月, 已排队 1 个) 一次提交 30 个超额, 29 个刚好
    c2.post("/api/v4/workspaces/switch", json={"id": 2})
    many = [f"https://s{i}.example/" for i in range(30)]
    out["quota_over"] = c2.post("/api/batches", json={"urls": many}).status_code
    out["quota_ok"] = c2.post("/api/batches", json={"urls": many[:29]}).status_code
    out["usage"] = c2.get("/api/v4/usage").get_json()
    # 公平调度: 甲区(不限)排着 1 个, 乙区(并发 1)排着 30 个; 领 3 次应甲乙交替且乙不超并发
    picked = []
    for _ in range(3):
        t = A.db.claim_next_task(concurrency=lambda plan: (config.PLANS.get(plan) or {}).get("concurrent"))
        picked.append(t and t["workspace_id"])
    out["picked"] = picked
    print("RESULT" + json.dumps(out))
''')


@unittest.skipUnless(importlib.util.find_spec("flask"), "需要 flask(平台依赖)")
class MultiTenantIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True,
                              text=True, timeout=180, cwd=str(ROOT))
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")]
        if not line:
            raise AssertionError("子进程未产出结果:\n" + proc.stdout[-3000:] + proc.stderr[-3000:])
        cls.r = json.loads(line[-1][6:])

    def test_onboarding(self):
        self.assertEqual(self.r["bootstrap"], 201)
        self.assertEqual(self.r["invite_info"], 200)
        self.assertEqual(self.r["join"], 200)
        self.assertEqual(self.r["invite_reuse"], 404)  # 单次邀请用完即失效
        self.assertEqual(self.r["bob_sees_a"], 1)
        self.assertEqual(self.r["create_ws"], 201)

    def test_other_workspace_is_invisible(self):
        r = self.r
        self.assertEqual(r["a_tasks"], [r["t1"]])
        for k in ("a_get_t2", "a_preview_t2", "a_dl_t2", "a_rebuild_t2", "a_share_t2",
                  "a_patch_p2", "a_del_p2", "a_switch_b"):
            self.assertEqual(r[k], 404, k)
        self.assertNotIn("乙项目", r["a_projects"])
        self.assertEqual(r["a_batch_into_p2"], 400)
        self.assertEqual(r["a_batches"], 1)
        self.assertEqual(r["a_tel_batches"], 1)

    def test_roles(self):
        r = self.r
        self.assertEqual(r["b_in_a_tasks"], [r["t1"]])
        self.assertEqual(r["b_in_a_users"], 403)
        self.assertEqual(r["b_admin_settings"], 403)
        self.assertEqual(r["b_get_t1"], 200)

    def test_quota_and_fair_scheduling(self):
        r = self.r
        self.assertEqual(r["quota_over"], 429)
        self.assertEqual(r["quota_ok"], 201)
        self.assertEqual(r["usage"]["plan"], "free")
        self.assertEqual(r["usage"]["queued"], 30)
        # 乙区并发 1: 第一次领乙(运行 0 的区里最早的任务是甲的? 两区都 0 运行 -> 取最早 id 即甲)
        self.assertEqual(r["picked"][0], 1)
        self.assertEqual(r["picked"][1], 2)
        self.assertIsNone(r["picked"][2])  # 甲区已空, 乙区已占满并发 1

    def test_share_links(self):
        r = self.r
        self.assertEqual(r["anon_plain"], 401)
        self.assertEqual(r["anon_bad_share"], 404)
        self.assertTrue(r["anon_share_auth"])
        self.assertFalse(r["anon_share_other_task"])


if __name__ == "__main__":
    unittest.main()
