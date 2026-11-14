"""网站记录存档: 打包 -> 导入另一工作区还原; 角色/隔离/去重/路径安全/凭据不外泄。

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
    import io, json, sys, tempfile, zipfile
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    import config
    tmp = Path(tempfile.mkdtemp())
    config.DB_PATH = tmp / "t.db"
    config.WORKSPACE_ROOT = tmp / "ws"; config.WORKSPACE_ROOT.mkdir()
    config.EXPORTS_DIR = tmp / "ex"; config.EXPORTS_DIR.mkdir()
    import app as A
    A.scheduler.start = lambda *a, **k: None
    out = {}
    c1, c2, c3 = A.app.test_client(), A.app.test_client(), A.app.test_client()
    c1.post("/api/auth/bootstrap", json={"username": "alice", "password": "pw-alice-123"})
    p1 = c1.post("/api/v4/projects", json={"name": "客户甲", "color": "#123456"}).get_json()["project"]["id"]
    c1.post("/api/batches", json={"urls": ["https://a.example/", "https://b.example/"], "project_id": p1,
                                  "config": {"cookie": "SID=secret", "ai_api_key": "sk-secret"}})
    tasks = c1.get("/api/tasks").get_json()["tasks"]
    ta, tb = sorted(t["id"] for t in tasks)
    for tid, score in ((ta, 97.5), (tb, None)):
        ws = config.WORKSPACE_ROOT / str(tid)
        (ws / "static" / "css").mkdir(parents=True)
        (ws / "index.html").write_text("<h1>clone %d</h1>" % tid)
        (ws / "static" / "css" / "a.css").write_text("h1{color:red}")
        (ws / "original_preview.png").write_bytes(b"\x89PNG fake")
        (ws / "engine.log").write_text("10:00:00 [GEN] done\n")
        (ws / "site.json").write_text(json.dumps({"routes": {"/": "index.html"}, "upstream": "https://a.example"}))
        (ws / "flask_app").mkdir(); (ws / "flask_app" / "app.py").write_text("x")
        A.db.update_task(tid, status="completed" if score else "failed", progress=100,
                         similarity_score=score, workspace_path=str(ws),
                         stats_json=json.dumps({"stage_times": {"capture": 3.2}}))
    # 只读成员: 能打包, 不能导入
    inv = c1.post("/api/v4/invites", json={"role": "viewer"}).get_json()["url"].split("invite=")[1]
    c3.post(f"/api/auth/invite/{inv}", json={"username": "vic", "password": "pw-vic-12345"})
    r = c1.get(f"/api/v4/archive?project={p1}")
    out["export"] = r.status_code
    blob = r.data
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = zf.namelist()
    man = json.loads(zf.read("manifest.json"))
    out["has_flask_app"] = any("flask_app" in n for n in names)
    out["has_log"] = any(n.endswith("engine.log") for n in names)
    out["leak"] = ("secret" in json.dumps(man))
    out["man_tasks"] = len(man["tasks"])
    out["viewer_export"] = c3.get(f"/api/v4/archive?tasks={ta}").status_code
    out["viewer_import"] = c3.post("/api/v4/archive/import", data={"file": (io.BytesIO(blob), "a.zip")},
                                   content_type="multipart/form-data").status_code
    # 同区重导: 原任务还在 -> 全部跳过
    r = c1.post("/api/v4/archive/import", data={"file": (io.BytesIO(blob), "a.zip")}, content_type="multipart/form-data")
    out["same_ws"] = [r.status_code, r.get_json()["imported"], r.get_json()["skipped"]]
    # bob 自建乙区后导入
    inv2 = c1.post("/api/v4/invites", json={"role": "operator"}).get_json()["url"].split("invite=")[1]
    c2.post(f"/api/auth/invite/{inv2}", json={"username": "bob", "password": "pw-bob-1234"})
    c2.post("/api/v4/workspaces", json={"name": "乙团队"})
    r = c2.post("/api/v4/archive/import", data={"file": (io.BytesIO(blob), "a.zip")}, content_type="multipart/form-data")
    res = r.get_json()
    out["import"] = [r.status_code, res["imported"], res["skipped"]]
    b_tasks = c2.get("/api/tasks").get_json()["tasks"]
    out["b_tasks"] = sorted((t["domain"], t["status"], t["similarity_score"]) for t in b_tasks)
    out["b_projects"] = [p["name"] for p in c2.get("/api/v4/projects").get_json()["projects"]]
    nt = max(res["task_ids"])
    nt_a = min(res["task_ids"])
    out["preview"] = c2.get(f"/preview/{nt_a}/").data.decode()
    out["alice_sees_new"] = c1.get(f"/api/tasks/{nt_a}").status_code
    t = A.db.get_task(nt_a)
    out["ws_has_flask"] = (Path(t["workspace_path"]) / "flask_app").exists()
    out["archive_meta"] = json.loads(t["stats_json"]).get("archive", {}).get("src_id")
    out["batch_cfg_leak"] = "secret" in (A.db.get_batch(t["batch_id"])["config_json"])
    # 再导一次: 去重
    r = c2.post("/api/v4/archive/import", data={"file": (io.BytesIO(blob), "a.zip")}, content_type="multipart/form-data")
    out["again"] = [r.get_json()["imported"], r.get_json()["skipped"]]
    # 指定项目导入 + 重导出后 uid 延续
    blob2 = c2.get(f"/api/v4/archive?tasks={nt_a}").data
    out["uid_keep"] = json.loads(zipfile.ZipFile(io.BytesIO(blob2)).read("manifest.json"))["tasks"][0]["uid"] == man["tasks"][0]["uid"]
    # 恶意包
    def bad(entries):
        bio = io.BytesIO()
        with zipfile.ZipFile(bio, "w") as z:
            z.writestr("manifest.json", json.dumps({"format": "clonjurer-archive", "version": 1,
                "tasks": [{"key": "1", "uid": "x:1", "target_url": "https://e.example/", "status": "completed"}]}))
            for n, d in entries:
                z.writestr(n, d)
        bio.seek(0)
        return c2.post("/api/v4/archive/import", data={"file": (bio, "x.zip")}, content_type="multipart/form-data").status_code
    out["evil_dotdot"] = bad([("tasks/1/../../../evil.txt", "x")])
    out["evil_other"] = bad([("etc/passwd", "x")])
    out["not_zip"] = c2.post("/api/v4/archive/import", data={"file": (io.BytesIO(b"nope"), "x.zip")}, content_type="multipart/form-data").status_code
    out["escaped"] = (tmp / "evil.txt").exists() or (config.WORKSPACE_ROOT.parent / "evil.txt").exists()
    print("RESULT" + json.dumps(out, ensure_ascii=False))
''')


@unittest.skipUnless(importlib.util.find_spec("flask"), "需要 flask(平台依赖)")
class ArchiveRoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True,
                              text=True, timeout=180, cwd=str(ROOT))
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")]
        if not line:
            raise AssertionError("子进程未产出结果:\n" + proc.stdout[-3000:] + proc.stderr[-3000:])
        cls.r = json.loads(line[-1][6:])

    def test_export_contents(self):
        r = self.r
        self.assertEqual(r["export"], 200)
        self.assertEqual(r["man_tasks"], 2)
        self.assertFalse(r["has_flask_app"])
        self.assertTrue(r["has_log"])
        self.assertFalse(r["leak"])

    def test_roles(self):
        self.assertEqual(self.r["viewer_export"], 200)
        self.assertEqual(self.r["viewer_import"], 403)

    def test_import_restores(self):
        r = self.r
        self.assertEqual(r["import"], [201, 2, 0])
        self.assertEqual(r["b_tasks"], [["a.example", "completed", 97.5], ["b.example", "failed", None]])
        self.assertIn("客户甲", r["b_projects"])
        self.assertIn("<h1>clone", r["preview"])
        self.assertEqual(r["alice_sees_new"], 404)
        self.assertFalse(r["ws_has_flask"])
        self.assertIsNotNone(r["archive_meta"])
        self.assertFalse(r["batch_cfg_leak"])

    def test_dedupe(self):
        self.assertEqual(self.r["same_ws"], [201, 0, 2])
        self.assertEqual(self.r["again"], [0, 2])
        self.assertTrue(self.r["uid_keep"])

    def test_rejects_bad_archives(self):
        r = self.r
        self.assertEqual(r["evil_dotdot"], 400)
        self.assertEqual(r["evil_other"], 400)
        self.assertEqual(r["not_zip"], 400)
        self.assertFalse(r["escaped"])


if __name__ == "__main__":
    unittest.main()
