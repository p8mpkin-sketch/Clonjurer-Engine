"""部署(Docker)管理器: 用假 docker 验证参数安全、幂等销毁、端口分配、自动销毁与对账。

真 Docker 端到端(极速启动约 1 秒、销毁后零残留)见 PR 说明; 这里不依赖 Docker。
"""
import json
import tempfile
import unittest
from pathlib import Path

from core import deploy as D
from models import Database


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.containers = {}   # name -> state
        self.images = {D.RUNTIME_IMAGE}
        self.up = True

    def __call__(self, args, timeout=60, input_text=None):
        self.calls.append(list(args))
        a = args
        if not self.up:
            return 1, "", "Cannot connect to the Docker daemon"
        if a[0] == "version":
            return 0, "27.3.1\n", ""
        if a[:2] == ["image", "inspect"]:
            return (0, "[]", "") if a[2] in self.images else (1, "", "No such image")
        if a[0] == "build":
            self.images.add(a[a.index("-t") + 1]); return 0, "", ""
        if a[0] == "run":
            name = a[a.index("--name") + 1]
            self.containers[name] = "running"; return 0, "c0ffee" * 10 + "\n", ""
        if a[0] == "rm":
            self.containers.pop(a[-1], None); return 0, "", ""
        if a[0] == "rmi":
            self.images.discard(a[-1]); return 0, "", ""
        if a[0] == "stop":
            if a[-1] not in self.containers:
                return 1, "", "No such container"
            self.containers[a[-1]] = "exited"; return 0, "", ""
        if a[0] in ("start", "restart"):
            if a[-1] not in self.containers:
                return 1, "", "No such container"
            self.containers[a[-1]] = "running"; return 0, "", ""
        if a[0] == "ps":
            return 0, "\n".join(json.dumps({"Names": n, "State": s, "ID": "abc", "Image": "x", "Status": s})
                                for n, s in self.containers.items()), ""
        if a[0] == "logs":
            return (0, "booted\n", "") if a[-1] in self.containers else (1, "", "No such container")
        return 0, "", ""


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "t.db")
        bid = self.db.create_batch("b", [("https://a.example/", "a.example"),
                                         ("https://b.example/", "b.example")], {})
        self.tasks = self.db.list_tasks(batch_id=bid)
        for t in self.tasks:
            self.db.update_task(t["id"], status="completed")
        self.fake = FakeDocker()
        self.busy_ports = set()

        def regen(task, ws):
            (ws / "flask_app").mkdir(parents=True, exist_ok=True)
            (ws / "flask_app" / "app.py").write_text("app = None")

        self.dep = D.DockerDeployer(
            self.db, lambda t: self.tmp / "ws" / str(t["id"]), regen, self.db.get_task,
            run=self.fake, port_free=lambda h, p: p not in self.busy_ports,
            health=lambda port, t: True)
        self.tid = self.tasks[0]["id"]

    def _run_args(self):
        return [c for c in self.fake.calls if c[0] == "run"][-1]

    def test_fast_deploy_is_locked_down_and_labelled(self):
        out = self.dep.deploy(self.tid)
        a = self._run_args()
        self.assertIn(f"127.0.0.1:{D.PORT_RANGE[0]}:8000", a)          # 默认只绑本机
        for flag in ("--read-only", "--cap-drop", "no-new-privileges", "--pids-limit", "--memory"):
            self.assertIn(flag, a)
        self.assertIn("clonjurer.managed=1", a)
        self.assertIn(f"clonjurer.task={self.tid}", a)
        mount = a[a.index("-v") + 1]
        self.assertTrue(mount.endswith("/flask_app:/app:ro"), mount)    # 只读挂载工程
        self.assertFalse(any(c[0] == "build" for c in self.fake.calls))  # 极速: 不构建
        self.assertEqual(out["deploy"]["status"], "running")
        self.assertTrue(out["deploy"]["url"].startswith("http://127.0.0.1:"))
        self.assertIsNotNone(out["deploy"]["expires_in_s"])             # 默认 24 小时自动销毁

    def test_rejects_unlisted_values(self):
        for kw in ({"mode": "fast; rm -rf /"}, {"bind": "1.2.3.4"}, {"mem_limit": "99g"},
                   {"ttl_hours": 5}):
            with self.assertRaises(D.DeployError):
                self.dep.deploy(self.tid, **kw)
        self.assertFalse(any(c[0] == "run" for c in self.fake.calls))

    def test_unfinished_task_cannot_deploy(self):
        self.db.update_task(self.tid, status="running")
        with self.assertRaises(D.DeployError):
            self.dep.deploy(self.tid)

    def test_redeploy_replaces_and_keeps_port(self):
        p1 = self.dep.deploy(self.tid)["deploy"]["port"]
        p2 = self.dep.deploy(self.tid, ttl_hours=1)["deploy"]["port"]
        self.assertEqual(p1, p2)
        self.assertEqual(len(self.dep.list()), 1)

    def test_ports_skip_used_and_busy(self):
        self.busy_ports.add(D.PORT_RANGE[0])
        p1 = self.dep.deploy(self.tid)["deploy"]["port"]
        p2 = self.dep.deploy(self.tasks[1]["id"])["deploy"]["port"]
        self.assertEqual(p1, D.PORT_RANGE[0] + 1)
        self.assertEqual(p2, D.PORT_RANGE[0] + 2)

    def test_destroy_is_idempotent_and_frees_port(self):
        self.dep.deploy(self.tid, mode="image")
        self.assertIn(D.site_image(self.tid), self.fake.images)
        self.dep.destroy(self.tid)
        self.dep.destroy(self.tid)                                      # 再点一次不报错
        self.assertEqual(self.fake.containers, {})
        self.assertNotIn(D.site_image(self.tid), self.fake.images)
        self.assertIsNone(self.db.get_docker(self.tid))
        self.assertEqual(self.dep.deploy(self.tasks[1]["id"])["deploy"]["port"], D.PORT_RANGE[0])

    def test_destroy_touches_only_our_names(self):
        self.dep.destroy(self.tid)
        for c in self.fake.calls:
            if c[0] in ("rm", "rmi"):
                self.assertTrue(c[-1] in (D.container_name(self.tid), D.site_image(self.tid)), c)

    def test_stop_start_and_missing_container_redeploys(self):
        self.dep.deploy(self.tid)
        self.dep.stop(self.tid)
        self.assertEqual(self.dep.get(self.tid)["status"], "stopped")
        self.fake.containers.clear()                                    # 用户手动删了容器
        self.assertEqual(self.dep.start(self.tid)["deploy"]["status"], "running")

    def test_reaper_destroys_expired_only(self):
        self.dep.deploy(self.tid, ttl_hours=1)
        self.dep.deploy(self.tasks[1]["id"], ttl_hours=0)               # 不自动销毁
        self.db.upsert_docker(self.tid, expires_at="2000-01-01 00:00:00")
        self.assertEqual(self.dep.reap_expired(), [self.tid])
        self.assertEqual([c["task_id"] for c in self.dep.list()], [self.tasks[1]["id"]])

    def test_health_failure_stops_and_records(self):
        self.dep._health = lambda port, t: False
        with self.assertRaises(D.DeployError):
            self.dep.deploy(self.tid)
        self.assertEqual(self.db.get_docker(self.tid)["status"], "failed")
        self.assertEqual(self.fake.containers[D.container_name(self.tid)], "exited")

    def test_batch_cleanup_and_orphans(self):
        self.dep.deploy(self.tid)
        self.dep.deploy(self.tasks[1]["id"])
        self.dep.stop(self.tasks[1]["id"])
        self.fake.containers[D.container_name(999)] = "exited"         # 未登记的旧容器
        self.assertTrue(any(c["orphan"] for c in self.dep.list()))
        n = self.dep.batch("cleanup_stopped")                          # 已停止的 + 未登记的旧容器
        self.assertEqual(n, 2)
        self.assertEqual([c["task_id"] for c in self.dep.list()], [self.tid])

    def test_docker_down_is_reported_not_raised(self):
        self.fake.up = False
        s = self.dep.summary()
        self.assertFalse(s["available"])
        self.assertTrue(s["hint"])
        with self.assertRaises(D.DeployError):
            self.dep.deploy(self.tid)

    def test_reconcile_marks_missing(self):
        self.dep.deploy(self.tid)
        self.fake.containers.clear()
        self.assertEqual(self.dep.reconcile()["fixed"], 1)
        self.assertEqual(self.db.get_docker(self.tid)["status"], "missing")


if __name__ == "__main__":
    unittest.main()
