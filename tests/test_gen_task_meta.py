"""生成任务的显示名与 token 流水: 任务书标题规则、报告 calls → 流水行、流水整体替换(幂等)与按天/按项目汇总。"""
import tempfile
import unittest
from pathlib import Path

from core.uir.generate import task_title, token_rows
from models import Database


class GenTaskMetaTest(unittest.TestCase):
    def test_title(self):
        self.assertEqual(task_title({"title": "跳动的爱心"}, {"description": "帮我写一个在跳动的爱心的网页"}), "跳动的爱心")
        # 规划失败的退化任务书: 用需求原话前 24 字
        self.assertEqual(task_title({"title": "x", "fallback": True}, {"description": "一" * 30}), "一" * 24)
        self.assertEqual(task_title({}, {"kind": "screenshot"}), "界面截图")

    def test_token_rows(self):
        rep = {"ai_model": "m1", "judge_model": "m2",
               "calls": [{"step": "plan", "in": 10, "out": 5}, {"step": "write", "in": 100, "out": 900},
                         {"step": "review", "in": 50, "out": 20}, {"step": "repair", "in": 0, "out": 0}]}
        rows = token_rows(rep)
        self.assertEqual([r["step"] for r in rows], ["规划/读图", "写页面", "验收"])
        self.assertEqual(rows[2]["model"], "m2")

    def test_db_usage(self):
        db = Database(Path(tempfile.mkdtemp()) / "t.db")
        bid = db.create_batch("生成 · x", [("uir:generate:description:x", "x")], {}, None, workspace_id=1)
        tid = db.list_tasks(batch_id=bid, limit=1, workspace_id=1)[0]["id"]
        rows = [{"model": "m", "step": "写页面", "in": 100, "out": 50}]
        db.replace_token_usage(tid, "generate", rows)
        db.replace_token_usage(tid, "generate", rows)            # 回填重复执行不累加
        db.add_token_usage(tid, "ai_repair", "m", "视觉修复第 1 轮", 7, 3)
        got = db.task_token_usage(tid)
        self.assertEqual(sum(r["tokens_in"] for r in got), 107)
        day = db.token_usage_summary(1, "day")
        self.assertEqual(day[0]["tokens_in"] + day[0]["tokens_out"], 160)
        self.assertEqual(day[0]["ai_repair"], 10)
        self.assertEqual(db.token_usage_summary(1, "model")[0]["label"], "m")


if __name__ == "__main__":
    unittest.main()
