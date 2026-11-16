"""定时任务变化通知: 汇总规则、推送条件、按 webhook 主机适配消息格式、地址校验;
平台侧: 批次跑完只通知一次、通知地址只回掩码、测试通知接口。外呼全部 mock, 不打真实网络。"""
import importlib.util
import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from core import schedule_notify as sn

ROOT = Path(__file__).resolve().parents[1]


def _task(i, status="completed", change=None, url=None):
    stats = {}
    if change is not None:
        stats["change_since_last"] = {"prev_task_id": 100 + i, **change}
    return {"id": i, "target_url": url or f"https://s{i}.example/", "status": status,
            "similarity_score": 90.0, "stats": stats, "error_msg": "超时" if status == "failed" else ""}


SAME = {"regions": [], "area_pct": 0.2, "verdict": "与上次相比几乎没有变化"}
DIFF = {"regions": [[0, 0, 10, 10]], "area_pct": 4.5, "verdict": "与上次相比有 1 处变化, 约占 4.5% 面积"}


class SummarizeTests(unittest.TestCase):
    def test_waits_until_all_finished(self):
        self.assertIsNone(sn.summarize([_task(1, change=SAME), _task(2, status="running")]))
        self.assertIsNone(sn.summarize([]))

    def test_counts_changed_failed_and_first_run(self):
        s = sn.summarize([_task(1, change=SAME), _task(2, change=DIFF), _task(3, status="failed"), _task(4)])
        self.assertEqual((s["total"], s["changed"], s["failed"]), (4, 1, 1))
        by = {x["task_id"]: x for x in s["sites"]}
        self.assertTrue(by[2]["changed"])
        self.assertTrue(by[4]["first_run"])
        self.assertIn("超时", by[3]["verdict"])

    def test_large_area_without_regions_counts_as_change(self):
        s = sn.summarize([_task(1, change={"regions": [], "area_pct": 2.0, "verdict": "细微变化"})])
        self.assertEqual(s["changed"], 1)

    def test_should_notify(self):
        quiet = sn.summarize([_task(1, change=SAME)])
        loud = sn.summarize([_task(1, change=DIFF)])
        failed = sn.summarize([_task(1, status="failed")])
        self.assertFalse(sn.should_notify("change", quiet))
        self.assertTrue(sn.should_notify("change", loud))
        self.assertTrue(sn.should_notify("change", failed))
        self.assertTrue(sn.should_notify("always", quiet))
        self.assertFalse(sn.should_notify("off", loud))


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.summary = sn.summarize([_task(1, change=DIFF), _task(2, change=SAME)])
        self.sc = {"id": 3, "name": "竞品巡检"}

    def test_formats_by_host(self):
        f = sn.build_payload("https://open.feishu.cn/open-apis/bot/v2/hook/x", self.sc, 9, self.summary)
        self.assertEqual(f["msg_type"], "text")
        self.assertIn("竞品巡检", f["content"]["text"])
        d = sn.build_payload("https://oapi.dingtalk.com/robot/send?access_token=x", self.sc, 9, self.summary)
        self.assertEqual(d["msgtype"], "text")
        self.assertIn("定时任务", d["text"]["content"])   # 钉钉关键词
        w = sn.build_payload("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x", self.sc, 9, self.summary)
        self.assertEqual(w["msgtype"], "text")
        sl = sn.build_payload("https://hooks.slack.com/services/x", self.sc, 9, self.summary)
        self.assertEqual(set(sl), {"text"})
        g = sn.build_payload("https://ops.example.com/hook", self.sc, 9, self.summary)
        self.assertEqual((g["event"], g["batch_id"], g["changed"]), ("schedule.run_finished", 9, 1))
        self.assertEqual(len(g["sites"]), 2)

    def test_text_mentions_changed_site(self):
        text = sn.summary_text("竞品巡检", self.summary)
        self.assertIn("1 个有变化", text)
        self.assertIn("https://s1.example/", text)

    def test_validate_webhook(self):
        self.assertEqual(sn.validate_webhook(""), "")
        self.assertEqual(sn.validate_webhook(" https://a.example/h "), "https://a.example/h")
        for bad in ("ftp://a.example/", "http://169.254.169.254/latest", "https://a b/", "javascript:x"):
            with self.assertRaises(ValueError, msg=bad):
                sn.validate_webhook(bad)

    def test_send_handles_errors(self):
        import requests
        with mock.patch.object(sn.requests.Session, "post", side_effect=requests.ConnectionError()):
            self.assertEqual(sn.send("https://a.example/h", {}), (False, "网络错误: ConnectionError"))
        with mock.patch.object(sn.requests.Session, "post", return_value=mock.Mock(status_code=500)):
            self.assertEqual(sn.send("https://a.example/h", {}), (False, "HTTP 500"))
        with mock.patch.object(sn.requests.Session, "post", return_value=mock.Mock(status_code=200)) as post:
            self.assertEqual(sn.send("https://a.example/h", {"a": 1}), (True, "HTTP 200"))
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertEqual(sn.send("http://169.254.169.254/", {})[0], False)


SCRIPT = textwrap.dedent(r'''
    import json, sys, tempfile, time
    from pathlib import Path
    from unittest import mock
    sys.path.insert(0, sys.argv[1])
    import config
    tmp = Path(tempfile.mkdtemp())
    config.DB_PATH = tmp / "t.db"
    config.WORKSPACE_ROOT = tmp / "ws"; config.WORKSPACE_ROOT.mkdir()
    config.EXPORTS_DIR = tmp / "ex"; config.EXPORTS_DIR.mkdir()
    import app as A
    A.scheduler.start = lambda *a, **k: None
    posts = []
    def fake_post(url, json=None, **kw):
        posts.append([url, json])
        return mock.Mock(status_code=200)
    mock.patch.object(A.schedule_notify.requests.Session, "post", side_effect=fake_post).start()
    out = {}
    c = A.app.test_client()
    HOOK = "https://oapi.dingtalk.com/robot/send?access_token=SECRET123456"
    out["bad_hook"] = c.post("/api/v4/schedules", json={"name": "x", "urls": ["https://a.example/"],
                                                       "webhook_url": "ftp://x"}).status_code
    out["bad_mode"] = c.post("/api/v4/schedules", json={"name": "x", "urls": ["https://a.example/"],
                                                       "notify": "sometimes"}).status_code
    r = c.post("/api/v4/schedules", json={"name": "巡检", "urls": ["https://a.example/", "https://b.example/"],
                                          "webhook_url": HOOK})
    out["create"] = r.status_code
    sc = r.get_json()["schedule"]
    sid = sc["id"]
    out["view"] = [sc["notify"], sc["webhook_set"], sc["webhook_masked"], "SECRET" in r.get_data(as_text=True)]
    out["list_has_secret"] = "SECRET" in c.get("/api/v4/schedules").get_data(as_text=True)
    out["test_notify"] = [c.post(f"/api/v4/schedules/{sid}/test-notify").status_code, len(posts),
                          posts[-1][0] == HOOK if posts else None]
    posts.clear()
    # 手动运行一次, 再把两个任务标成完成(一个有变化)
    out["run"] = c.post(f"/api/v4/schedules/{sid}/run").status_code
    out["pending_handled"] = A._check_schedule_notifications()     # 还没跑完: 不处理
    tasks = A.db.list_tasks(workspace_id=1)
    ch = {"prev_task_id": 1, "regions": [[0, 0, 5, 5]], "area_pct": 3.0, "verdict": "与上次相比有 1 处变化"}
    A.db.update_task(tasks[0]["id"], status="completed", similarity_score=91.0,
                     stats_json=json.dumps({"change_since_last": ch}))
    A.db.update_task(tasks[1]["id"], status="completed", similarity_score=95.0, stats_json="{}")
    out["handled"] = A._check_schedule_notifications()
    out["posts"] = [[p[0] == HOOK, p[1]["msgtype"], "1 个有变化" in p[1]["text"]["content"]] for p in posts]
    out["handled_again"] = A._check_schedule_notifications()
    out["posts_after"] = len(posts)
    run = c.get("/api/v4/schedules").get_json()["schedules"][0]["runs"][0]
    out["run_view"] = [run.get("changed"), run.get("failed_sites"), run.get("notified")]
    # 编辑: 留空不改地址; 清除地址
    c.patch(f"/api/v4/schedules/{sid}", json={"notify": "always"})
    out["kept"] = A.db.get_schedule(sid)["webhook_url"] == HOOK
    c.patch(f"/api/v4/schedules/{sid}", json={"webhook_clear": True})
    out["cleared"] = A.db.get_schedule(sid)["webhook_url"]
    out["test_without_hook"] = c.post(f"/api/v4/schedules/{sid}/test-notify").status_code
    print("RESULT" + json.dumps(out, ensure_ascii=False))
''')


@unittest.skipUnless(importlib.util.find_spec("flask"), "需要 flask(平台依赖)")
class ScheduleNotifyApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True, text=True,
                              timeout=180, cwd=ROOT)
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")]
        if not line:
            raise AssertionError(proc.stdout[-3000:] + proc.stderr[-3000:])
        cls.out = json.loads(line[-1][6:])

    def test_validation(self):
        self.assertEqual([self.out["bad_hook"], self.out["bad_mode"]], [400, 400])

    def test_webhook_is_masked(self):
        o = self.out
        self.assertEqual(o["create"], 201)
        self.assertEqual(o["view"][:2], ["change", True])
        self.assertTrue(o["view"][2].startswith("https://oapi.dingtalk.com/…"))
        self.assertFalse(o["view"][3])
        self.assertFalse(o["list_has_secret"])

    def test_test_notify(self):
        self.assertEqual(self.out["test_notify"], [200, 1, True])
        self.assertEqual(self.out["test_without_hook"], 400)

    def test_notifies_once_when_batch_finishes(self):
        o = self.out
        self.assertEqual(o["run"], 201)
        self.assertEqual(o["pending_handled"], 0)
        self.assertEqual(o["handled"], 1)
        self.assertEqual(o["posts"], [[True, "text", True]])
        self.assertEqual([o["handled_again"], o["posts_after"]], [0, 1])
        self.assertEqual(o["run_view"], [1, 0, True])

    def test_edit_keeps_or_clears_webhook(self):
        self.assertTrue(self.out["kept"])
        self.assertEqual(self.out["cleared"], "")


if __name__ == "__main__":
    unittest.main()
