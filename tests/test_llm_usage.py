"""模型消耗统计: 单价匹配与费用估算、请求层记账(升档重试也算、跨线程归属)、流水汇总、预算提醒与各接口。"""
import json
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import llm_usage
from core.uir import llm
from models import Database

from tests._env import has_modules

ROOT = Path(__file__).resolve().parents[1]


class Pricing(unittest.TestCase):
    def tearDown(self):
        llm_usage.set_prices(llm_usage.DEFAULT_PRICES, llm_usage.DEFAULT_CURRENCY)

    def test_longest_prefix_and_provider_prefix(self):
        llm_usage.set_prices([{"match": "gpt-4o", "input": 10, "output": 40},
                              {"match": "gpt-4o-mini", "input": 1, "output": 4}])
        self.assertEqual(llm_usage.price_for("GPT-4o-mini-2024")["match"], "gpt-4o-mini")
        self.assertEqual(llm_usage.price_for("openai/gpt-4o")["match"], "gpt-4o")
        self.assertIsNone(llm_usage.price_for("glm-4v"))
        self.assertIsNone(llm_usage.estimate_cost("glm-4v", 100, 100))       # 未定价 ≠ 免费
        self.assertAlmostEqual(llm_usage.estimate_cost("gpt-4o", 1_000_000, 500_000), 30.0)

    def test_normalize(self):
        rows = llm_usage.normalize_prices([{"match": " DeepSeek ", "input": "2", "output": 3},
                                           {"match": "", "input": 9}, {"match": "deepseek", "input": 1, "output": 1}])
        self.assertEqual(rows, [{"match": "deepseek", "input": 1.0, "output": 1.0}])
        with self.assertRaises(ValueError):
            llm_usage.normalize_prices([{"match": "x", "input": -1}])
        with self.assertRaises(ValueError):
            llm_usage.normalize_prices([{"match": "x", "input": "abc"}])

    def test_tokens_of(self):
        self.assertEqual(llm_usage.tokens_of({"prompt_tokens": 3, "completion_tokens": 4}), (3, 4))
        self.assertEqual(llm_usage.tokens_of({"input_tokens": 5, "output_tokens": 6}), (5, 6))
        self.assertEqual(llm_usage.tokens_of(None), (0, 0))


class Attribution(unittest.TestCase):
    def setUp(self):
        self.got = []
        llm_usage.SINK = self.got.append

    def tearDown(self):
        llm_usage.SINK = None

    def test_scope_tag_step_merge(self):
        with llm_usage.scope(task_id=7, kind="generate", username="bob"):
            cfg = llm_usage.tag({"model": "deepseek-chat"})
            with llm_usage.scope(kind="ai_mock"), llm_usage.step("写页面"):
                llm_usage.record(cfg, {"prompt_tokens": 10, "completion_tokens": 5})
        # 线程池里没有线程上下文: 靠 ai_cfg 上的标签归属
        with ThreadPoolExecutor(1) as ex:
            ex.submit(llm_usage.record, cfg, {"prompt_tokens": 1, "completion_tokens": 1}).result()
        a, b = self.got
        self.assertEqual((a["task_id"], a["kind"], a["username"], a["step"]), (7, "ai_mock", "bob", "写页面"))
        self.assertEqual((b["task_id"], b["kind"], b["username"], b["step"]), (7, "generate", "bob", ""))
        self.assertGreater(a["cost"], 0)

    def test_sink_errors_swallowed(self):
        def boom(rec):
            raise RuntimeError("disk full")
        llm_usage.SINK = boom
        llm_usage.record({"model": "m"}, {})      # 不抛


class _Script(BaseHTTPRequestHandler):
    queue: list = []

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps(type(self).queue.pop(0)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class TransportRecordsEveryRequest(unittest.TestCase):
    def test_budget_ladder_counts_both_requests(self):
        _Script.queue = [
            # 第一次: 推理模型把预算吃光, 内容为空 → 升档重试; 这次同样按 token 计费
            {"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
             "usage": {"prompt_tokens": 100, "completion_tokens": 800,
                       "completion_tokens_details": {"reasoning_tokens": 800}}},
            {"choices": [{"message": {"content": "{\"ok\": 1}"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 100, "completion_tokens": 50}},
        ]
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Script)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        got = []
        llm_usage.SINK = got.append
        try:
            with llm_usage.scope(task_id=3, kind="generate"):
                cfg = llm.resolve_ai_cfg({"ai_api_key": "k", "ai_model": "deepseek-chat",
                                          "ai_base_url": f"http://127.0.0.1:{srv.server_address[1]}"})
            # 离开线程上下文后照样归属到任务 3(ai_cfg 带标签)
            with llm_usage.step("验收"):
                self.assertEqual(llm.chat_json(cfg, "s", "u", max_tokens=800, thinking=True), {"ok": 1})
        finally:
            llm_usage.SINK = None
            srv.shutdown()
            srv.server_close()
        self.assertEqual([(r["task_id"], r["step"], r["tokens_in"], r["tokens_out"]) for r in got],
                         [(3, "验收", 100, 800), (3, "验收", 100, 50)])


class Ledger(unittest.TestCase):
    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "t.db")
        self.p1 = self.db.ensure_default_project(1)
        self.p2 = self.db.create_project("营销站", "", "#6366f1", workspace_id=1)
        bid = self.db.create_batch("b", [("https://a.example/", "a.example"), ("https://b.example/", "b.example")],
                                   {}, self.p1, created_by="bob", workspace_id=1)
        self.t1, self.t2 = [t["id"] for t in self.db.list_tasks(batch_id=bid, limit=5, workspace_id=1)]

    def test_add_and_group(self):
        db = self.db
        db.add_llm_call({"task_id": self.t1, "kind": "generate", "model": "m1", "step": "写页面",
                         "tokens_in": 100, "tokens_out": 50, "cost": 0.5})
        db.add_llm_call({"task_id": self.t2, "kind": "ai_repair", "model": "m2", "username": "carol",
                         "tokens_in": 10, "tokens_out": 10, "cost": None})
        db.add_llm_call({"task_id": None, "kind": "ai_test", "model": "m1", "username": "dave",
                         "workspace_id": 1, "tokens_in": 1, "tokens_out": 1, "cost": 0.01})
        tot = db.llm_usage_totals(1)
        self.assertEqual((tot["calls"], tot["tokens"], tot["unpriced"]), (3, 172, 1))
        self.assertAlmostEqual(tot["cost"], 0.51)
        users = {r["key"]: r for r in db.llm_usage_group(1, "user")}
        self.assertEqual(set(users), {"bob", "carol", "dave"})      # 任务调用默认记在提交人名下, 显式发起人优先
        proj = {r["label"]: r["tokens"] for r in db.llm_usage_group(1, "project")}
        self.assertEqual(proj, {"默认项目": 170, "未归属项目": 2})
        self.assertEqual(db.llm_usage_group(1, "model")[0]["key"], "m1")   # 按费用降序
        self.assertEqual(db.llm_usage_totals(2)["calls"], 0)             # 不串工作区
        self.assertEqual(db.llm_usage_totals(1, username="carol")["tokens"], 20)

        # 任务移到别的项目: 消耗跟着走
        db.move_tasks_to_project([self.t1], self.p2)
        self.assertEqual(db.llm_usage_totals(1, project_id=self.p2)["tokens"], 150)

        # 补上 m2 的单价: 只补没有费用的行; 全量重算会覆盖已有估算
        prices = {"m1": {"input": 0, "output": 0}, "m2": {"input": 1000000, "output": 0}}
        db.recompute_token_costs(prices.get, only_missing=True)
        self.assertAlmostEqual(db.llm_usage_totals(1)["cost"], 0.51 + 10)
        db.recompute_token_costs(prices.get)
        self.assertAlmostEqual(db.llm_usage_totals(1)["cost"], 10)


@unittest.skipUnless(has_modules("flask"), "未安装 flask")
class Api(unittest.TestCase):
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
        from core import llm_usage
        A.scheduler.start = lambda *a, **k: None
        out = {}
        c = A.app.test_client()
        pid = A.db.ensure_default_project(1)
        bid = A.db.create_batch("b", [("https://a.example/", "a.example")], {}, pid, workspace_id=1)
        tid = A.db.list_tasks(batch_id=bid, limit=1, workspace_id=1)[0]["id"]
        # 单价表: 改 deepseek 的价, 之后的调用按新价
        r = c.put("/api/v4/admin/llm-pricing", json={"currency": "$", "prices": [
            {"match": "deepseek", "input": 1000000, "output": 0}]})
        out["pricing"] = r.status_code, r.get_json()["currency"]
        out["bad_pricing"] = c.put("/api/v4/admin/llm-pricing", json={"prices": "x"}).status_code
        # 预算 15, 提醒线 50%: 记一笔 10 → 提醒一次, 再记一笔 10 → 超预算再提醒一次
        out["budget"] = c.put("/api/v4/workspace/llm-budget", json={"monthly": 15, "alert_pct": 50}).get_json()["level"]
        with llm_usage.scope(task_id=tid, kind="generate"), llm_usage.step("写页面"):
            llm_usage.record({"model": "deepseek-chat"}, {"prompt_tokens": 10, "completion_tokens": 7})
            llm_usage.record({"model": "deepseek-chat"}, {"prompt_tokens": 10, "completion_tokens": 0})
        llm_usage.record({"model": "glm-4v"}, {"prompt_tokens": 3, "completion_tokens": 3}, kind="ai_test")
        out["notes"] = [e["detail"][:9] for e in A.db.list_audit(limit=50, action="llm.budget")]
        d = c.get("/api/v4/workspace/llm-usage?days=7").get_json()
        out["totals"] = d["totals"]
        out["series_len"] = len(d["series"])
        out["series_today"] = d["series"][-1]["tokens"]
        out["kinds"] = [r["label"] for r in d["by_kind"]]
        out["unpriced_models"] = d["unpriced_models"]
        out["budget_after"] = d["budget"]["level"], d["budget"]["spent"]
        out["bad_days"] = c.get("/api/v4/workspace/llm-usage?days=5").get_json()["days"]
        t = c.get(f"/api/tasks/{tid}/tokens").get_json()
        out["task_rows"] = [(r["step"], r["calls"], r["tokens_in"]) for r in t["rows"]]
        out["task_cost"] = t["cost"]
        p = c.get(f"/api/projects/{pid}/llm-usage?days=30").get_json()
        out["project_tokens"] = p["totals"]["tokens"], p["top_tasks"][0]["key"] == tid
        out["project_404"] = c.get("/api/projects/9999/llm-usage").status_code
        out["users"] = c.get("/api/v4/users/llm-usage").get_json()["users"]
        print("OUT" + json.dumps(out, ensure_ascii=False))
    ''')

    def test_endpoints(self):
        proc = subprocess.run([sys.executable, "-c", self.SCRIPT, str(ROOT)], capture_output=True, text=True,
                              timeout=180, cwd=ROOT)
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("OUT")), None)
        self.assertIsNotNone(line, proc.stdout[-2000:] + proc.stderr[-3000:])
        out = json.loads(line[3:])
        self.assertEqual(out["pricing"], [200, "$"])
        self.assertEqual(out["bad_pricing"], 400)
        self.assertEqual(out["budget"], "ok")
        self.assertEqual(out["notes"], ["本月模型消耗已超出", "本月模型消耗已达预"])   # 提醒线、超预算各一次, 新的在前
        self.assertEqual(out["totals"]["calls"], 3)
        self.assertEqual(out["totals"]["tokens"], 33)
        self.assertAlmostEqual(out["totals"]["cost"], 20.0)
        self.assertEqual(out["totals"]["unpriced"], 1)
        self.assertEqual((out["series_len"], out["series_today"]), (7, 33))
        self.assertEqual(set(out["kinds"]), {"界面生成", "模型测试"})
        self.assertEqual(out["unpriced_models"], ["glm-4v"])
        self.assertEqual(out["budget_after"], ["over", 20.0])
        self.assertEqual(out["bad_days"], 30)
        self.assertEqual(out["task_rows"], [["写页面", 2, 20]])
        self.assertAlmostEqual(out["task_cost"], 20.0)
        self.assertEqual(out["project_tokens"], [27, True])
        self.assertEqual(out["project_404"], 404)
        self.assertEqual(out["users"][""]["tokens"], 33)       # 开放模式没有用户名


if __name__ == "__main__":
    unittest.main()
