"""任务删除 / 编辑(改名、移动项目)、Mock 规则删除、短链任务编辑。

在独立子进程里导入 app(临时数据库与工作目录), 调度器不启动, 任务状态直接写库模拟。"""
import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from tests._env import has_modules

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
    from core.scheduler import Scheduler as _Sched
    _Sched.start = lambda self: None   # 导入 app 时 _boot() 就会起调度线程, 事后改实例来不及
    import app as A
    A.scheduler.start = lambda *a, **k: None
    A._regenerate_project = lambda task, ws: (config.EXPORTS_DIR / f"{task['id']}.zip")

    out = {}
    c = A.app.test_client()
    db = A.db
    r = c.post("/api/batches", json={"urls": ["https://a.example/", "https://b.example/", "https://c.example/",
                                              "https://d.example/"]})
    bid = r.get_json()["batch_id"]
    ids = [t["id"] for t in db.list_tasks(batch_id=bid, workspace_id=1)]
    ids.sort()
    a, b, cc, d = ids
    for tid in ids:
        ws = config.WORKSPACE_ROOT / str(tid); ws.mkdir()
        (ws / "index.html").write_text("<title>站点 %d</title>" % tid, encoding="utf-8")
        z = config.EXPORTS_DIR / f"{tid}.zip"; z.write_bytes(b"zip")
        db.update_task(tid, status="completed", workspace_path=str(ws), zip_path=str(z), similarity_score=90)
    db.update_task(d, status="running")

    # 改名 / 恢复自动标题
    r = c.patch(f"/api/tasks/{a}", json={"alias": "  我的  首页 "})
    out["rename"] = [r.status_code, r.get_json()["task"]["title"]]
    out["rename_list"] = [t["title"] for t in c.get("/api/tasks").get_json()["tasks"] if t["id"] == a]
    out["rename_long"] = c.patch(f"/api/tasks/{a}", json={"alias": "x" * 81}).status_code
    r = c.patch(f"/api/tasks/{a}", json={"alias": ""})
    out["rename_reset"] = r.get_json()["task"]["title"]

    # 移动到其他项目: 只移一部分 -> 拆出新批次; 非法项目 400
    pid = c.post("/api/v4/projects", json={"name": "新项目"}).get_json()["project"]["id"]
    out["move_bad"] = c.patch(f"/api/tasks/{a}", json={"project_id": 9999}).status_code
    r = c.post("/api/tasks/batch/move", json={"task_ids": [a, b], "project_id": pid})
    out["move"] = [r.status_code, r.get_json()["moved"]]
    out["move_proj_tasks"] = sorted(t["id"] for t in db.list_tasks(project_id=pid, workspace_id=1))
    out["batches"] = sorted([bb["total_urls"], bb["completed_urls"]] for bb in db.list_batches(workspace_id=1))

    # 删除: 运行中 409 / 批量跳过运行中 / 工作空间与 ZIP 被删 / 关联的短链任务解除关联
    st = c.post("/api/v4/link-tasks", json={"name": "lk", "target_url": "https://x.example/", "clone_task_id": a})
    lk = st.get_json()["task"]["id"]
    out["del_running"] = c.delete(f"/api/tasks/{d}").status_code
    out["del_one"] = c.delete(f"/api/tasks/{a}").status_code
    out["del_one_files"] = [(config.WORKSPACE_ROOT / str(a)).exists(), (config.EXPORTS_DIR / f"{a}.zip").exists()]
    out["del_one_gone"] = c.get(f"/api/tasks/{a}").status_code
    out["link_unlinked"] = db.get_short_task(lk)["clone_task_id"]
    r = c.post("/api/tasks/batch/delete", json={"task_ids": [b, cc, d, 99999]})
    j = r.get_json()
    out["batch_del"] = [r.status_code, j["deleted"], sorted(s["reason"] for s in j["skipped"])]
    out["batch_del_bad"] = c.post("/api/tasks/batch/delete", json={}).status_code
    out["left"] = [t["id"] for t in c.get("/api/tasks").get_json()["tasks"]]
    out["empty_batches"] = len(db.list_batches(workspace_id=1))

    # Mock 规则删除(带 path/method 校验)
    e = d
    db.update_task(e, status="completed")
    ws = config.WORKSPACE_ROOT / str(e)
    (ws / "mock_data.json").write_text(json.dumps([
        {"path": "/api/a", "method": "GET", "status": 200, "response_body": "{}"},
        {"path": "/api/b", "method": "POST", "status": 200, "response_body": "{}"}]), encoding="utf-8")
    out["mock_stale"] = c.delete(f"/api/tasks/{e}/mock/0?path=/api/b&method=POST").status_code
    out["mock_oob"] = c.delete(f"/api/tasks/{e}/mock/5").status_code
    r = c.delete(f"/api/tasks/{e}/mock/1?path=/api/b&method=POST")
    out["mock_del"] = [r.status_code, [m["path"] for m in c.get(f"/api/tasks/{e}/mock").get_json()["entries"]]]

    # 短链任务编辑
    r = c.patch(f"/api/v4/link-tasks/{lk}", json={"name": "改名", "note": "备注", "service": "tinyurl",
                                                   "target_url": "https://y.example/p", "clone_task_id": e})
    t = r.get_json()["task"]
    out["lk_edit"] = [r.status_code, t["name"], t["note"], t["service"], t["target_url"], t["clone_task_id"]]
    out["lk_unlink"] = c.patch(f"/api/v4/link-tasks/{lk}", json={"clone_task_id": ""}).get_json()["task"]["clone_task_id"]
    out["lk_bad"] = [c.patch(f"/api/v4/link-tasks/{lk}", json={"name": ""}).status_code,
                     c.patch(f"/api/v4/link-tasks/{lk}", json={"target_url": "javascript:1"}).status_code,
                     c.patch(f"/api/v4/link-tasks/{lk}", json={"clone_task_id": 99999}).status_code,
                     c.patch("/api/v4/link-tasks/99999", json={"name": "x"}).status_code]
    print("RESULT" + json.dumps(out, ensure_ascii=False))
''')


@unittest.skipUnless(has_modules("flask"), "未安装 flask")
class TaskDeleteEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True, text=True,
                              timeout=180, cwd=ROOT)
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")]
        if not line:
            raise AssertionError(proc.stdout[-3000:] + proc.stderr[-3000:])
        cls.out = json.loads(line[-1][6:])

    def test_rename(self):
        o = self.out
        self.assertEqual(o["rename"], [200, "我的 首页"])
        self.assertEqual(o["rename_list"], ["我的 首页"])
        self.assertEqual(o["rename_long"], 400)
        self.assertTrue(o["rename_reset"].startswith("站点 "))

    def test_move(self):
        o = self.out
        self.assertEqual(o["move_bad"], 400)
        self.assertEqual(o["move"], [200, 2])
        self.assertEqual(len(o["move_proj_tasks"]), 2)
        self.assertEqual(o["batches"], [[2, 1], [2, 2]])   # 原批次剩 2 个(1 个完成 1 个运行中), 新批次 2 个

    def test_delete(self):
        o = self.out
        self.assertEqual(o["del_running"], 409)
        self.assertEqual(o["del_one"], 200)
        self.assertEqual(o["del_one_files"], [False, False])
        self.assertEqual(o["del_one_gone"], 404)
        self.assertIsNone(o["link_unlinked"])
        self.assertEqual(o["batch_del"][0], 200)
        self.assertEqual(len(o["batch_del"][1]), 2)
        self.assertEqual(o["batch_del"][2], ["不存在", "执行中"])
        self.assertEqual(o["batch_del_bad"], 400)
        self.assertEqual(len(o["left"]), 1)
        self.assertEqual(o["empty_batches"], 1)   # 删空的批次一并清掉

    def test_mock_delete(self):
        o = self.out
        self.assertEqual([o["mock_stale"], o["mock_oob"]], [409, 400])
        self.assertEqual(o["mock_del"], [200, ["/api/a"]])

    def test_link_task_edit(self):
        o = self.out
        self.assertEqual(o["lk_edit"][:5], [200, "改名", "备注", "tinyurl", "https://y.example/p"])
        self.assertIsNotNone(o["lk_edit"][5])
        self.assertIsNone(o["lk_unlink"])
        self.assertEqual(o["lk_bad"], [400, 400, 400, 404])


if __name__ == "__main__":
    unittest.main()
