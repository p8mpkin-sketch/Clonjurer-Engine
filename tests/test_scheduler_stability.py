"""调度器稳定性护栏: 任务不会卡在 running(启动收回 / 看门狗 / 落库失败工作线程不死)、磁盘空间守卫。"""
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

import config
from core import scheduler as sched_mod
from models import Database


class _Bus:
    def __init__(self):
        self.msgs = []

    def publish(self, task_id, stage, message, level=None):
        self.msgs.append((task_id, stage, message, level))


class _Pool:
    max = 1

    def shutdown_all(self):
        pass


def _db(tmp):
    db = Database(Path(tmp) / "t.db")
    bid = db.create_batch("b", [("https://a.example/", "a.example"), ("https://b.example/", "b.example")], {})
    return db, bid


def _running(db, tid, age_s=0):
    db.update_task(tid, status="running")
    if age_s:
        with db._lock:
            db._conn.execute("UPDATE clone_tasks SET updated_at = datetime('now', ?) WHERE id = ?",
                             (f"-{int(age_s)} seconds", tid))
            db._conn.commit()


class SchedulerStability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db, _ = _db(self.tmp.name)
        self.ids = [t["id"] for t in self.db.list_tasks()]
        self.bus = _Bus()
        self.s = sched_mod.Scheduler(self.db, self.bus, _Pool())

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_boot_recovers_orphans(self):
        _running(self.db, self.ids[0])
        self.assertEqual(self.db.recover_orphan_tasks(), [self.ids[0]])
        t = self.db.get_task(self.ids[0])
        self.assertEqual(t["status"], "failed")
        self.assertIn("中断", t["error_msg"])
        self.assertEqual(self.db.get_task(self.ids[1])["status"], "pending")

    def test_watchdog_orphan_vs_active(self):
        a, b = self.ids
        _running(self.db, a, age_s=300)          # 不在执行名单: 工作线程已死
        _running(self.db, b, age_s=300)          # 在执行, 但心跳只停了 5 分钟
        self.s._active[b] = 0
        self.assertEqual(self.s._watch_once(), [a])
        self.assertEqual(self.db.get_task(b)["status"], "running")
        _running(self.db, b, age_s=config.TASK_STALE_S + 60)
        self.assertEqual(self.s._watch_once(), [b])

    def test_failed_write_does_not_kill_worker(self):
        boom = RuntimeError("engine blew up")

        class _Eng:
            def __init__(self, *a, **k):
                pass

            def run(self):
                raise boom

        task = self.db.get_task(self.ids[0])
        with mock.patch.object(sched_mod, "ClonerEngine", _Eng), \
                mock.patch.object(self.db, "update_task", side_effect=OSError(28, "No space left on device")), \
                mock.patch.object(sched_mod.time, "sleep"):
            self.s._run_task(0, task)        # 不抛
        self.assertTrue(any("落库失败" in m[2] for m in self.bus.msgs))

    def test_disk_full_message(self):
        class _Eng:
            def __init__(self, *a, **k):
                pass

            def run(self):
                raise OSError(28, "No space left on device")

        task = self.db.get_task(self.ids[0])
        with mock.patch.object(sched_mod, "ClonerEngine", _Eng):
            self.s._run_task(0, task)
        self.assertTrue(self.db.get_task(self.ids[0])["error_msg"].startswith("磁盘空间不足"))

    def test_disk_guard_pauses_claiming(self):
        DU = namedtuple("DU", "total used free")
        with mock.patch.object(sched_mod.shutil, "disk_usage", return_value=DU(10, 10, 100 * 1024 * 1024)):
            self.assertFalse(self.s._disk_ok())
            self.assertFalse(self.s._disk_ok())
            with mock.patch.object(self.s._stop, "wait"):
                self.s._worker_step(0)  # 不领任务
        self.assertEqual(sum("暂停领取" in m[2] for m in self.bus.msgs), 1)   # 只报一次
        self.assertEqual(self.db.get_task(self.ids[0])["status"], "pending")
        with mock.patch.object(sched_mod.shutil, "disk_usage", return_value=DU(10, 10, 50 * 1024 ** 3)):
            self.assertTrue(self.s._disk_ok())
        self.assertTrue(any("恢复领取" in m[2] for m in self.bus.msgs))


if __name__ == "__main__":
    unittest.main()


class ClockAnchorAligned(unittest.TestCase):
    def test_t0_on_second_one_of_minute(self):
        from core.clone.capture import CaptureMixin
        o = CaptureMixin.__new__(CaptureMixin)
        t0 = o._replay_clock_t0()
        if t0 is None:
            self.skipTest("REPLAY_CLOCK 关")
        self.assertEqual(t0 % 60000, 1000)
        import time as _t
        self.assertLess(_t.time() * 1000 - t0, 60000)
