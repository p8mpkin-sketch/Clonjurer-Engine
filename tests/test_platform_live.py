"""实时进度 / 定时任务 / 修复任务 / 部署清理: 功能与跨工作区隔离。

在独立子进程里导入 app(临时数据库与工作目录), 不污染其他测试与本机实例。"""
import importlib.util
import json
import subprocess
import sys
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = textwrap.dedent(r'''
    import json, sys, tempfile, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    import config
    tmp = Path(tempfile.mkdtemp())
    config.DB_PATH = tmp / "t.db"
    config.WORKSPACE_ROOT = tmp / "ws"; config.WORKSPACE_ROOT.mkdir()
    config.EXPORTS_DIR = tmp / "ex"; config.EXPORTS_DIR.mkdir()
    config.NEW_WORKSPACE_PLAN = "free"
    import app as A
    from core import live
    A.scheduler.start = lambda *a, **k: None
    out = {}
    c1, c2, c3 = A.app.test_client(), A.app.test_client(), A.app.test_client()
    c1.post("/api/auth/bootstrap", json={"username": "alice", "password": "pw-alice-123"})
    inv = c1.post("/api/v4/invites", json={"role": "viewer"}).get_json()["url"].split("invite=")[1]
    c3.post(f"/api/auth/invite/{inv}", json={"username": "vera", "password": "pw-vera-1234"})
    inv = c1.post("/api/v4/invites", json={"role": "operator"}).get_json()["url"].split("invite=")[1]
    c2.post(f"/api/auth/invite/{inv}", json={"username": "bob", "password": "pw-bob-1234"})
    c2.post("/api/v4/workspaces", json={"name": "乙团队"})          # bob 现在在乙区(免费版)

    # ---- 定时任务: 建 / 改 / 隔离 / 只读成员不能写 ----
    body = {"name": "每日巡检", "urls": ["https://a.example/", "not a url"], "freq": "daily", "hour": 9, "minute": 30}
    r = c1.post("/api/v4/schedules", json=body)
    out["create"] = r.status_code
    sc = r.get_json()["schedule"]; sid = sc["id"]
    out["urls"], out["rule"], out["has_next"] = sc["urls"], sc["rule"], bool(sc["next_run_at"])
    out["bad_freq"] = c1.post("/api/v4/schedules", json={**body, "freq": "yearly"}).status_code
    out["bad_hour"] = c1.post("/api/v4/schedules", json={**body, "hour": 25}).status_code
    out["no_urls"] = c1.post("/api/v4/schedules", json={**body, "urls": []}).status_code
    out["viewer_list"] = len(c3.get("/api/v4/schedules").get_json()["schedules"])
    out["viewer_create"] = c3.post("/api/v4/schedules", json=body).status_code
    out["viewer_run"] = c3.post(f"/api/v4/schedules/{sid}/run").status_code
    out["other_list"] = len(c2.get("/api/v4/schedules").get_json()["schedules"])
    out["other_patch"] = c2.patch(f"/api/v4/schedules/{sid}", json={"name": "偷改"}).status_code
    out["other_run"] = c2.post(f"/api/v4/schedules/{sid}/run").status_code
    out["other_delete"] = c2.delete(f"/api/v4/schedules/{sid}").status_code
    r = c1.patch(f"/api/v4/schedules/{sid}", json={"enabled": False})
    out["paused_next"] = r.get_json()["schedule"]["next_run_at"]
    r = c1.patch(f"/api/v4/schedules/{sid}", json={"enabled": True, "freq": "weekly", "weekday": 6})
    out["weekly_rule"] = r.get_json()["schedule"]["rule"]

    # ---- 后台触发(无请求上下文): 到点的才跑, 跑完推到下一次 ----
    A.db.update_schedule(sid, next_run_at="2000-01-01 00:00:00")
    due = A.db.due_schedules(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
    out["due"] = [d["id"] for d in due]
    res = A._fire_schedule(due[0], "alice")
    out["fired"] = res.get("accepted")
    after = A.db.get_schedule(sid)
    out["advanced"] = after["next_run_at"] > time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    out["runs"] = after["runs"]
    view = c1.get("/api/v4/schedules").get_json()["schedules"][0]
    out["history_total"] = view["runs"][0]["total"]
    # 额度: 乙区免费版 30 次/月, 定时任务一次要 31 个 -> 跳过且不入队
    many = [f"https://s{i}.example/" for i in range(31)]
    s2 = c2.post("/api/v4/schedules", json={"name": "太多", "urls": many}).get_json()["schedule"]["id"]
    out["quota_run"] = c2.post(f"/api/v4/schedules/{s2}/run").status_code
    out["quota_tasks"] = len(c2.get("/api/tasks").get_json()["tasks"])

    # ---- 实时快照: 自己的任务有阶段和预计剩余, 别区的只显示占用 ----
    t1 = c1.get("/api/tasks").get_json()["tasks"][0]["id"]
    A.db.update_task(t1, status="running", stage="渲染", progress=40)
    now_m = time.monotonic()
    live.begin(t1, 0)
    live.attach_timeline(t1, [("渲染", now_m - 6.0), ("渲染", now_m - 4.0), ("资源", now_m - 1.0)])
    la = c1.get("/api/v4/live").get_json()
    out["a_worker"] = la["workers"][0].get("task_id") == t1
    out["a_trail"] = [(s["stage"], s["current"]) for s in la["tasks"][0]["trail"]]
    out["a_trail_secs_ok"] = 4.5 < la["tasks"][0]["trail"][0]["secs"] < 6.5
    lb = c2.get("/api/v4/live").get_json()
    out["b_worker"] = lb["workers"][0]
    out["b_tasks"] = len(lb["tasks"])
    out["b_leak"] = "a.example" in json.dumps(lb)
    live.end(t1)
    out["after_end"] = c1.get("/api/v4/live").get_json()["workers"][0]["busy"]

    # ---- 修复任务进度: 只在本区可见 ----
    A._ai_jobs[t1] = {"task_id": t1, "domain": "a.example", "state": "running", "started": time.time(),
                      "ended": None, "before": 90.0, "after": None, "log": [], "prompt": "",
                      "workspace_id": 1}
    out["a_jobs"] = len(c1.get("/api/v4/ai/jobs").get_json()["jobs"])
    out["b_jobs"] = len(c2.get("/api/v4/ai/jobs").get_json()["jobs"])
    out["job_no_ws"] = "workspace_id" not in c1.get("/api/v4/ai/jobs").get_json()["jobs"][0]

    # ---- 删项目: 上面的部署要一起下线 ----
    destroyed = []
    A.deployer.destroy = lambda tid: destroyed.append(tid)
    p = c1.post("/api/v4/projects", json={"name": "要删的"}).get_json()["project"]["id"]
    c1.post("/api/batches", json={"urls": ["https://d.example/"], "project_id": p})
    td = [t["id"] for t in c1.get("/api/tasks").get_json()["tasks"] if t["domain"] == "d.example"][0]
    A.db.upsert_docker(td, status="running", mode="process", port=9100)
    out["del_project"] = c1.delete(f"/api/v4/projects/{p}").status_code
    out["destroyed"] = destroyed == [td]

    # ---- 部署: 同一站点部署进行中再点一次 -> 409 ----
    A.deployer.precheck = lambda *a, **k: None
    A.deployer._steps[t1] = "启动"
    out["deploy_busy"] = c1.post(f"/api/tasks/{t1}/docker/deploy", json={"async": True, "mode": "process"}).status_code
    print("RESULT" + json.dumps(out))
''')


@unittest.skipUnless(importlib.util.find_spec("flask"), "需要 flask")
class PlatformLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True,
                           text=True, timeout=180)
        line = [ln for ln in p.stdout.splitlines() if ln.startswith("RESULT")]
        if not line:
            raise AssertionError(p.stdout[-2000:] + p.stderr[-4000:])
        cls.o = json.loads(line[-1][6:])

    def test_schedule_crud_and_validation(self):
        o = self.o
        self.assertEqual(o["create"], 201)
        self.assertEqual(o["urls"], ["https://a.example/"])
        self.assertEqual(o["rule"], "每天 09:30")
        self.assertTrue(o["has_next"])
        self.assertEqual((o["bad_freq"], o["bad_hour"], o["no_urls"]), (400, 400, 400))
        self.assertEqual(o["paused_next"], "")
        self.assertEqual(o["weekly_rule"], "每周日 09:30")

    def test_schedule_isolation_and_roles(self):
        o = self.o
        self.assertEqual(o["viewer_list"], 1)
        self.assertEqual((o["viewer_create"], o["viewer_run"]), (403, 403))
        self.assertEqual(o["other_list"], 0)
        self.assertEqual((o["other_patch"], o["other_run"], o["other_delete"]), (404, 404, 404))

    def test_schedule_fires_in_background(self):
        o = self.o
        self.assertEqual(o["due"], [1])
        self.assertEqual(o["fired"], 1)
        self.assertTrue(o["advanced"])
        self.assertEqual(o["runs"], 1)
        self.assertEqual(o["history_total"], 1)

    def test_schedule_respects_quota(self):
        self.assertEqual(self.o["quota_run"], 429)
        self.assertEqual(self.o["quota_tasks"], 0)

    def test_live_snapshot_and_isolation(self):
        o = self.o
        self.assertTrue(o["a_worker"])
        self.assertEqual(o["a_trail"], [["渲染", False], ["资源", True]])
        self.assertTrue(o["a_trail_secs_ok"])
        self.assertTrue(o["b_worker"]["busy"])
        self.assertIsNone(o["b_worker"]["task_id"])
        self.assertEqual(o["b_tasks"], 0)
        self.assertFalse(o["b_leak"])
        self.assertFalse(o["after_end"])

    def test_ai_jobs_isolation(self):
        self.assertEqual((self.o["a_jobs"], self.o["b_jobs"]), (1, 0))
        self.assertTrue(self.o["job_no_ws"])

    def test_project_delete_takes_deployments_down(self):
        self.assertEqual(self.o["del_project"], 200)
        self.assertTrue(self.o["destroyed"])

    def test_deploy_in_progress_conflict(self):
        self.assertEqual(self.o["deploy_busy"], 409)


class NextRunTests(unittest.TestCase):
    def _local(self, s):
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone()

    def test_rules(self):
        from core import schedules as S
        base = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc).astimezone()   # 本地某个周五/周六
        d = self._local(S.next_run("daily", base.hour, base.minute, after=base))
        self.assertEqual((d - base).days, 1)                       # 同一分钟不重复触发, 推到明天
        h = self._local(S.next_run("hourly", 0, (base.minute + 5) % 60, after=base))
        self.assertEqual(int((h - base).total_seconds()), 300)
        for wd in range(7):
            w = self._local(S.next_run("weekly", 8, 0, wd, after=base))
            self.assertEqual(w.weekday(), wd)
            self.assertGreater(w, base)
            self.assertLessEqual((w - base).days, 7)
        for i in range(7):
            k = self._local(S.next_run("weekdays", 8, 0, after=base.replace(day=13 + i)))
            self.assertLess(k.weekday(), 5)

    def test_describe(self):
        from core import schedules as S
        self.assertEqual(S.describe("hourly", 0, 5), "每小时第 5 分")
        self.assertEqual(S.describe("weekdays", 8, 0), "工作日 08:00")


if __name__ == "__main__":
    unittest.main()
