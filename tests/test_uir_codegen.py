"""代码直写轨(core/uir/codegen.py + core/uir/probe.py): 文字需求 → 模型写网页 → 体检 → 验收 → 修复。

三层:
  纯函数   HTML 提取 / 任务书与验收结果规整 / 需求符合度评分的硬证据闸门与封顶 / 无题材特判守卫
  体检     合成页面上的确定性测量(会动与不动、报错、外网依赖、溢出、按钮有无反应、键盘监听)
  端到端   GenerateEngine + 本地假模型上游: 首版有硬伤 → 修复 → 达标; 各种兜底(无 Key / 规划失败 / 非 HTML / 验收失败)
"""
import json
import re
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
from core.uir import codegen, settings
from tests._env import chromium_available

ROOT = Path(__file__).resolve().parents[1]

MOVING = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>t</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#fdf6f0}
.dot{width:140px;height:140px;border-radius:50%;background:#c2410c;animation:p 1s ease-in-out infinite}
@keyframes p{50%{transform:scale(1.35)}}</style></head><body><div class="dot"></div>
<p id="n">0</p><button id="add">加一</button>
<script>let n=0;document.getElementById('add').onclick=()=>{document.getElementById('n').textContent=++n}</script>
</body></html>"""
STATIC = MOVING.replace("animation:p 1s ease-in-out infinite", "")
BROKEN = MOVING.replace("let n=0;", "let n=0;undefinedFunctionCall();")
DEAD = STATIC.replace("document.getElementById('add').onclick", "window.__unused")
REMOTE = MOVING.replace("</head>", '<script src="https://cdn.invalid/lib.js"></script></head>')
WIDE = STATIC.replace('<div class="dot">', '<div style="width:2400px;height:10px"></div><div class="dot">')
KEYS = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#111">
<canvas id="c" width="400" height="300"></canvas><script>
const g=document.getElementById('c').getContext('2d');let x=10;
function d(){g.clearRect(0,0,400,300);g.fillStyle='#e5e5e5';g.fillRect(x,100,40,40)}
addEventListener('keydown',e=>{if(e.key==='ArrowRight'){x+=50;d()}});d()</script></body></html>"""
# 首屏以下的区块进入视口才显示(滚动渐显)
REVEAL = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>body{margin:0;background:#fff}
section{height:900px;display:grid;place-items:center}
.r{width:70%;height:500px;background:#1f2937;opacity:0;transition:opacity .15s}.r.on{opacity:1}</style></head><body>
<section><h1>首屏</h1></section><section><div class="r"></div></section><section><div class="r"></div></section>
<script>const io=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting)e.target.classList.add('on')}));
document.querySelectorAll('.r').forEach(e=>io.observe(e))</script></body></html>"""
# 空闲时静止, 点了按钮才开始按秒走数
STARTS = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#fff">
<p id="t" style="font:80px monospace">25:00</p><button id="go">开始</button><script>let s=1500,h=0;
document.getElementById('go').onclick=()=>{if(h)return;h=setInterval(()=>{s--;
document.getElementById('t').textContent=String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0')},1000)}
</script></body></html>"""


class Pure(unittest.TestCase):
    def test_extract_html(self):
        html, ok = codegen.extract_html("好的, 代码如下:\n```html\n<!DOCTYPE html><html><body>x</body></html>\n```\n以上。")
        self.assertTrue(ok)
        self.assertTrue(html.startswith("<!DOCTYPE html>") and html.rstrip().endswith("</html>"))
        html, ok = codegen.extract_html("<html><body>截断了")
        self.assertFalse(ok)
        with self.assertRaises(codegen.CodegenError):
            codegen.extract_html("抱歉, 我无法完成")
        # 正文里出现 ``` 不应被当成围栏
        html, ok = codegen.extract_html("<!doctype html><html><body><pre>```js</pre></body></html>")
        self.assertTrue(ok and "<pre>" in html)

    def test_normalize_plan(self):
        brief = codegen.normalize_plan({"title": "T", "checklist": [
            {"text": "有图形", "type": "visual", "must": True}, {"text": "有图形", "type": "visual"},
            {"text": "在动", "type": "MOTION"}, {"text": "", "type": "content"}, {"text": "别的", "type": "weird"}]}, "d")
        self.assertEqual([i["id"] for i in brief["checklist"]], ["c1", "c2", "c3"])
        self.assertEqual([i["type"] for i in brief["checklist"]], ["visual", "motion", "content"])
        with self.assertRaises(ValueError):
            codegen.normalize_plan({"checklist": []}, "d")
        # 模型一条 must 都没标: 至少保留一条代表用户原话
        self.assertTrue(codegen.normalize_plan({"checklist": [{"text": "a"}]}, "d")["checklist"][0]["must"])
        self.assertTrue(codegen.fallback_plan("需求原话")["checklist"][0]["must"])

    @staticmethod
    def _rep(**kw):
        rep = {"errors": [], "external": [], "blank": False, "frozen": False,
               "motion": {"moving": True, "running_animations": 1, "raf_per_s": 0, "max_frac": 0.01},
               "layout": {"overflow_x": False}, "mobile": {"overflow_x": False},
               "interactions": [{"kind": "click", "tag": "button", "label": "x", "action": "click", "responded": True}]}
        rep.update(kw)
        return rep

    BRIEF = {"checklist": [{"id": "c1", "text": "主体图形", "type": "visual", "must": True},
                           {"id": "c2", "text": "持续动效", "type": "motion", "must": True},
                           {"id": "c3", "text": "按钮计数", "type": "interaction", "must": False}]}

    def _rev(self, v=("yes", "yes", "yes"), design=8):
        return {"items": {f"c{i + 1}": {"verdict": x, "evidence": "e"} for i, x in enumerate(v)},
                "design_score": design, "issues": [], "summary": "", "mode": "vision"}

    def test_score_full_and_weights(self):
        d = codegen.score(self.BRIEF, self._rev(), self._rep())
        self.assertEqual(d["scoring"], "req-fit-v1")
        self.assertAlmostEqual(d["score"], 100 * (0.6 + 0.2 + 0.2 * 0.8), places=1)
        # 用户明确要求的条目权重是补充条目的 2 倍
        d = codegen.score(self.BRIEF, self._rev(("yes", "yes", "no")), self._rep())
        self.assertAlmostEqual(d["coverage"], 4 / 5, places=3)

    def test_hard_evidence_overrides_model(self):
        still = self._rep(motion={"moving": False, "running_animations": 0, "raf_per_s": 0, "max_frac": 0.0})
        d = codegen.score(self.BRIEF, self._rev(), still)       # 模型说在动, 帧差说没动
        item = next(i for i in d["items"] if i["id"] == "c2")
        self.assertEqual(item["verdict"], "no")
        self.assertTrue(item["gate"])
        self.assertLessEqual(d["score"], 60)                    # 用户明确要求的条目未满足 → 封顶
        dead = self._rep(interactions=[{"kind": "click", "tag": "button", "label": "x", "action": "click", "responded": False}])
        d = codegen.score(self.BRIEF, self._rev(), dead)
        self.assertEqual(next(i for i in d["items"] if i["id"] == "c3")["verdict"], "no")
        none = self._rep(interactions=[])
        self.assertEqual(next(i for i in codegen.score(self.BRIEF, self._rev(), none)["items"] if i["id"] == "c3")["verdict"], "no")

    def test_gates_only_when_falsifiable(self):
        idle_still = {"moving": False, "running_animations": 0, "raf_per_s": 0, "max_frac": 0.0}
        # 空闲时静止, 但操作之后画面在持续变化: 体检证伪不了「会动」, 不覆盖模型结论
        started = self._rep(motion=idle_still, post={"moving": True, "frame_diffs": [0.0, 0.01]})
        d = codegen.score(self.BRIEF, self._rev(), started)
        item = next(i for i in d["items"] if i["id"] == "c2")
        self.assertEqual((item["verdict"], item["gate"]), ("yes", None))
        # 某次操作引出了动画也一样
        clicked = self._rep(motion=idle_still, interactions=[
            {"kind": "click", "tag": "button", "label": "x", "action": "click", "responded": True, "animated": True}])
        self.assertEqual(next(i for i in codegen.score(self.BRIEF, self._rev(), clicked)["items"] if i["id"] == "c2")["verdict"], "yes")
        # 没看图的文本验收: 外观类条目最多 partial, 并标为「无法确认」(不进修复清单)
        d = codegen.score(self.BRIEF, dict(self._rev(), mode="text"), self._rep())
        item = next(i for i in d["items"] if i["id"] == "c1")
        self.assertEqual((item["verdict"], item["soft"]), ("partial", True))
        self.assertFalse(d["caps"])                              # partial 不触发「明确要求未满足」封顶
        self.assertEqual(codegen.problem_list([], d), [])
        self.assertFalse(codegen.actionable([], d))
        self.assertTrue(codegen.actionable(["报错"], d))
        d = codegen.score(self.BRIEF, self._rev(("yes", "yes", "partial")), self._rep())
        self.assertTrue(codegen.actionable([], d))

    def test_extract_json_tolerates_trailing_content(self):
        from core.uir import llm
        self.assertEqual(llm.extract_json('{"a": {"b": [1, 2]}}\n{"extra": 1}'), {"a": {"b": [1, 2]}})
        self.assertEqual(llm.extract_json('说明 {x} 然后 ```json\n{"ok": true}\n``` 完毕'), {"ok": True})
        with self.assertRaises(ValueError):
            llm.extract_json('{"a": 1,, }')

    def test_caps(self):
        self.assertLessEqual(codegen.score(self.BRIEF, self._rev(), self._rep(errors=["boom"]))["score"], 40)
        self.assertLessEqual(codegen.score(self.BRIEF, self._rev(), self._rep(blank=True))["score"], 40)
        self.assertLessEqual(codegen.score(self.BRIEF, None, self._rep())["score"], 70)   # 未经模型验收
        # 无关页面: 用户要求全部不满足 → 远低于旧口径的 80+
        d = codegen.score(self.BRIEF, self._rev(("no", "no", "no"), design=6), self._rep())
        self.assertLess(d["score"], 35)

    def test_apply_patches(self):
        src = "<html>\n  <style>\n    .a { color: red; }\n  </style>\n  <p>旧</p>\n</html>"
        out, ok, bad = codegen.apply_patches(src, "<<<<<<< SEARCH\n  <p>旧</p>\n=======\n  <p>新</p>\n>>>>>>> REPLACE")
        self.assertEqual((ok, bad), (1, 0))
        self.assertIn("<p>新</p>", out)
        # 缩进抄错: 逐行去空白后仍能定位; 定位不到的块记失败, 不猜
        text = ("```\n<<<<<<< SEARCH\n.a { color: red; }\n=======\n    .a { color: blue; }\n>>>>>>> REPLACE\n"
                "<<<<<<< SEARCH\n不存在的原文\n=======\nx\n>>>>>>> REPLACE\n```")
        out, ok, bad = codegen.apply_patches(src, text)
        self.assertEqual((ok, bad), (1, 1))
        self.assertIn("color: blue", out)
        self.assertNotIn("color: red", out)
        self.assertEqual(codegen.apply_patches(src, "没有修改块")[1:], (0, 0))

    def test_problem_list_order(self):
        d = codegen.score(self.BRIEF, dict(self._rev(("partial", "no", "yes")), issues=[
            {"severity": "mid", "problem": "标题被裁切", "fix": "减小字号"}]), self._rep())
        probs = codegen.problem_list(["运行时报错: x"], d)
        self.assertTrue(probs[0].startswith("[硬伤]"))
        self.assertIn("持续动效", probs[1])                     # 未满足(no)排在部分满足之前
        self.assertIn("标题被裁切", probs[-1])

    def test_no_subject_specific_branches(self):
        """通用性守卫: 管线代码里不得出现任何题材词(验证样例里的题材只存在于测试与基准用例)。"""
        banned = ("爱心", "heart", "贪吃蛇", "snake", "时钟", "clock", "番茄", "pomodoro", "星空", "咖啡", "记账")
        for name in ("codegen.py", "probe.py"):
            src = (ROOT / "core" / "uir" / name).read_text(encoding="utf-8").lower()
            for w in banned:
                self.assertNotIn(w, src, f"{name} 出现题材词 {w!r}")


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class Probe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from core.browser_pool import SyncBrowserPool
        cls.tmp = Path(tempfile.mkdtemp(prefix="probe_"))
        cls.pool = SyncBrowserPool.instance(2)

    @classmethod
    def tearDownClass(cls):
        from core.browser_pool import SyncBrowserPool
        SyncBrowserPool.instance()._teardown_thread_browser()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _inspect(self, name, html):
        from core.uir import probe
        d = self.tmp / name
        d.mkdir()
        (d / "index.html").write_text(html, encoding="utf-8")
        rep = probe.inspect_page(self.pool, d / "index.html", d / "p", {"width": 1280, "height": 800}, d / "full.png")
        return rep, probe.facts(rep), probe.hard_defects(rep)

    def test_moving_page(self):
        rep, f, defects = self._inspect("moving", MOVING)
        self.assertTrue(f["motion_seen"] and not f["motion_none"])
        self.assertGreaterEqual(rep["motion"]["running_animations"], 1)
        self.assertEqual(defects, [])
        self.assertEqual(f["responded_n"], 1)                   # 画面在动, 但按钮改了文字 → 仍能归因
        for k in ("f0", "peak", "after", "mobile"):
            self.assertTrue(Path(rep["shots"][k]).is_file())
        self.assertTrue((self.tmp / "moving" / "full.png").is_file())

    def test_static_page(self):
        rep, f, _ = self._inspect("static", STATIC)
        self.assertTrue(f["motion_none"] and not f["motion_seen"])
        self.assertEqual((f["responded_n"], f["dead_n"]), (1, 0))

    def test_broken_dead_remote_wide(self):
        _, f, defects = self._inspect("broken", BROKEN)
        self.assertTrue(f["errors"])
        self.assertTrue(any("undefinedFunctionCall" in d for d in defects))
        _, f, defects = self._inspect("dead", DEAD)
        self.assertEqual((f["responded_n"], f["dead_n"]), (0, 1))
        self.assertTrue(any("没有任何反应" in d for d in defects))
        rep, f, defects = self._inspect("remote", REMOTE)
        self.assertTrue(f["external"] and any("外网" in d for d in defects))
        _, f, defects = self._inspect("wide", WIDE)
        self.assertTrue(f["overflow_desktop"] and f["overflow_mobile"])

    def test_scroll_reveal_is_triggered_before_full_shot(self):
        from PIL import Image
        rep, f, _ = self._inspect("reveal", REVEAL)
        self.assertTrue(rep["layout"]["revealed"])
        with Image.open(self.tmp / "reveal" / "full.png") as im:
            px = im.convert("L")
            w, h = px.size
            # 第 2、3 屏中央原本是透明的(整页截图不会触发渐显), 滚过一遍之后应当是深色块
            self.assertLess(px.getpixel((w // 2, int(h * 0.5))), 90)
            self.assertLess(px.getpixel((w // 2, int(h * 0.83))), 90)
        self.assertRegex(rep["shot_at"]["f0"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_motion_that_starts_after_operation(self):
        rep, f, _ = self._inspect("starts", STARTS)
        self.assertFalse(f["motion_seen"])                      # 空闲时确实没动
        self.assertTrue(rep["post"]["moving"])                  # 点了「开始」之后跨过 1 秒, 读数变了
        self.assertTrue(f["motion_after"] and not f["motion_none"])
        self.assertIn("25:00", rep["post"]["text_removed"])
        self.assertIn("操作完之后", codegen.probe_digest(rep))

    def test_keyboard_listener(self):
        rep, f, _ = self._inspect("keys", KEYS)
        self.assertIn("keydown", rep["listeners"])
        kb = next(i for i in rep["interactions"] if i["kind"] == "keyboard")
        self.assertTrue(kb["responded"])


class _FakeLLM(BaseHTTPRequestHandler):
    """按系统提示词分流的假上游。mode 控制各步行为; calls 记录每次请求的 (步骤, 是否带图)。"""
    mode = {}
    calls = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        has_img = any(p.get("type") == "image_url" for p in user)
        text = user[0]["text"]
        m = type(self).mode
        if system == codegen.PLAN_SYSTEM:
            step = "plan"
            out = "不是 JSON" if m.get("plan") == "bad" else json.dumps({
                "title": "演示", "lang": "zh-CN", "summary": "理解", "kind": "动效", "design": {}, "tech": "css",
                "checklist": [{"text": "圆形主体", "type": "visual", "must": True},
                              {"text": "持续缩放动效", "type": "motion", "must": True},
                              {"text": "按钮点击后数字加一", "type": "interaction", "must": False}]})
        elif system == codegen.REPAIR_FULL_SYSTEM:
            step = "repair_full"
            out = MOVING
        elif system == codegen.REPAIR_SYSTEM:
            step = "repair"
            out = {"patch": "<<<<<<< SEARCH\nlet n=0;undefinedFunctionCall();\n=======\nlet n=0;\n>>>>>>> REPLACE",
                   "badpatch": "<<<<<<< SEARCH\n这段原文并不存在\n=======\nx\n>>>>>>> REPLACE"}.get(m.get("repair"), MOVING)
        elif system == codegen.WRITE_SYSTEM:
            step = "write"
            out = {"broken": BROKEN, "static": STATIC, "text": "抱歉, 做不到", "fenced": "```html\n" + MOVING + "\n```"}.get(
                m.get("write"), MOVING)
        elif system == codegen.REVIEW_SYSTEM:
            step = "review"
            if m.get("review") == "error":
                self.send_response(500)
                self.end_headers()
                type(self).calls.append((step, has_img))
                return
            ids = re.findall(r"^- (c\d+) ", text, flags=re.M)
            out = json.dumps({"items": [{"id": i, "verdict": "yes", "evidence": "看到了"} for i in ids],
                              "design": {"score": 8, "comment": "ok"}, "issues": [], "summary": "符合"})
        else:
            step = "selftest"
            out = json.dumps({"digits": "00000"})               # 假上游看不见图: 自检必然失败
        type(self).calls.append((step, has_img))
        data = json.dumps({"choices": [{"message": {"content": out}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 100, "completion_tokens": 50}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class _DB:
    def __init__(self):
        self.tasks, self.batches, self.n = {}, {}, 0

    def new(self, spec):
        self.n += 1
        self.batches[self.n] = {"id": self.n, "config_json": json.dumps({"mode": "generate", "gen": spec})}
        self.tasks[self.n] = {"id": self.n, "batch_id": self.n, "status": "pending",
                              "target_url": settings.TARGET_URL_PREFIX + spec["kind"]}
        return self.n

    def get_task(self, tid):
        return dict(self.tasks[tid])

    def get_batch(self, bid):
        return dict(self.batches[bid])

    def update_task(self, tid, **f):
        self.tasks[tid].update(f)

    def increment_completed(self, bid):
        pass


class _Bus:
    def __init__(self):
        self.lines = []

    def publish(self, tid, stage, msg, level=None):
        self.lines.append(f"[{stage}] {msg}")


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from core.browser_pool import SyncBrowserPool
        cls.tmp = Path(tempfile.mkdtemp(prefix="codegen_e2e_"))
        cls._saved = {k: getattr(config, k) for k in ("WORKSPACE_ROOT", "EXPORTS_DIR", "AI_API_KEY", "AI_API_BASE_URL", "AI_MODEL")}
        cls._env_file = settings.GEN_AI_ENV_FILE
        settings.GEN_AI_ENV_FILE = ""                           # 不读开发机上的真实 Key 文件
        config.WORKSPACE_ROOT = cls.tmp / "ws"
        config.EXPORTS_DIR = cls.tmp / "exports"
        config.WORKSPACE_ROOT.mkdir()
        config.EXPORTS_DIR.mkdir()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLM)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pool = SyncBrowserPool.instance(2)

    @classmethod
    def tearDownClass(cls):
        from core.browser_pool import SyncBrowserPool
        SyncBrowserPool.instance()._teardown_thread_browser()
        cls.srv.shutdown()
        cls.srv.server_close()
        for k, v in cls._saved.items():
            setattr(config, k, v)
        settings.GEN_AI_ENV_FILE = cls._env_file
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, mode=None, key=True, vision=False, **spec):
        from core.uir.generate import GenerateEngine
        config.AI_API_KEY = "test-key" if key else ""
        config.AI_API_BASE_URL = f"http://127.0.0.1:{self.srv.server_address[1]}"
        config.AI_MODEL = "fake-model"
        _FakeLLM.mode, _FakeLLM.calls = dict(mode or {}), []
        ident = (config.AI_API_BASE_URL, "fake-model")
        codegen._NO_VISION.discard(ident)
        codegen._VISION_OK.discard(ident)
        if vision:
            codegen._VISION_OK.add(ident)
        db, bus = _DB(), _Bus()
        type(self)._seq = db.n = getattr(type(self), "_seq", 0) + 1     # 每次运行独立的任务号/工作区
        db.n -= 1
        tid = db.new(dict({"kind": "description", "description": "一个会缩放的圆, 带计数按钮", "time_budget": 120}, **spec))
        ws = config.WORKSPACE_ROOT / str(tid)
        err = None
        try:
            GenerateEngine(tid, db, bus, self.pool).run()
        except Exception as exc:
            err = exc
        rep = json.loads((ws / "generate_report.json").read_text(encoding="utf-8")) if (ws / "generate_report.json").is_file() else None
        return db.get_task(tid), rep, bus.lines, ws, err

    def test_first_try_passes_with_vision(self):
        task, rep, lines, ws, err = self._run(vision=True)
        self.assertIsNone(err)
        self.assertEqual(task["status"], "completed")
        self.assertEqual((rep["track"], rep["scoring"]), ("code", "req-fit-v1"))
        self.assertEqual([c[0] for c in _FakeLLM.calls], ["plan", "write", "review"])   # 一次达标只有 3 次调用
        self.assertTrue(_FakeLLM.calls[-1][1])                  # 验收带了截图
        self.assertGreaterEqual(task["similarity_score"], 85)
        detail = json.loads((ws / "similarity_detail.json").read_text(encoding="utf-8"))
        self.assertEqual([i["verdict"] for i in detail["items"]], ["yes", "yes", "yes"])
        self.assertEqual(detail["review_mode"], "vision")
        self.assertIn("animation", (ws / "index.html").read_text(encoding="utf-8"))
        for name in ("cloned_preview.png", "brief.json", "gen_probe.json", "gen_f0.png", "site.json"):
            self.assertTrue((ws / name).is_file(), name)
        self.assertFalse((ws / "gen_probe").exists())           # 体检截图目录不进交付工程
        self.assertTrue(Path(task["zip_path"]).is_file())
        self.assertEqual(rep["tokens"], {"in": 300, "out": 150})

    def test_broken_first_draft_gets_repaired(self):
        task, rep, lines, ws, err = self._run({"write": "broken"})
        self.assertIsNone(err)
        steps = [c[0] for c in _FakeLLM.calls]
        self.assertEqual(steps[:3], ["plan", "write", "repair"])    # 有运行硬伤: 跳过验收直接修
        self.assertEqual([r["action"] for r in rep["rounds"]], ["write", "repair"])
        self.assertLessEqual(rep["rounds"][0]["score"], 40)
        self.assertTrue(rep["rounds"][1]["kept"])
        self.assertEqual(rep["best_round"], 2)
        # 假上游的看图自检不通过 → 文本验收: 外观类条目只能判 partial, 分数相应低一档; 没有可修的目标所以不再迭代
        self.assertTrue(80 <= task["similarity_score"] < 85)
        self.assertNotIn("undefinedFunctionCall", (ws / "index.html").read_text(encoding="utf-8"))
        # 报告如实标注
        self.assertEqual(rep["rounds"][1]["review_mode"], "text")
        self.assertTrue(any("修复目标" in ln and "undefinedFunctionCall" in ln for ln in lines))
        self.assertTrue(any("文本验收" in ln and "自检" in ln for ln in lines))     # 降级原因写进日志

    def test_patch_repair_and_fallback_to_full_rewrite(self):
        task, rep, lines, ws, err = self._run({"write": "broken", "repair": "patch"})
        self.assertIsNone(err)
        self.assertEqual(next(c for c in rep["calls"] if c["step"] == "repair")["mode"], "patch")
        self.assertNotIn("undefinedFunctionCall", (ws / "index.html").read_text(encoding="utf-8"))
        self.assertGreaterEqual(task["similarity_score"], 80)
        # 修改块对不上原文: 这一轮作废, 下一轮改为整页重写
        task, rep, lines, ws, err = self._run({"write": "broken", "repair": "badpatch"})
        self.assertIsNone(err)
        steps = [c[0] for c in _FakeLLM.calls]
        self.assertIn("repair_full", steps)
        self.assertLess(steps.index("repair"), steps.index("repair_full"))
        self.assertGreaterEqual(task["similarity_score"], 80)

    def test_static_page_fails_motion_item_despite_model(self):
        # 模型写了不会动的页面, 验收模型却全判满足: 帧差硬证据把动效项判回 no, 触发修复
        task, rep, lines, ws, err = self._run({"write": "static"}, vision=True)
        self.assertIsNone(err)
        self.assertLessEqual(rep["rounds"][0]["score"], 60)
        self.assertEqual(rep["rounds"][1]["action"], "repair")
        self.assertGreaterEqual(task["similarity_score"], 85)

    def test_no_key_fails_loudly_instead_of_template(self):
        task, rep, lines, ws, err = self._run(key=False)
        self.assertIsInstance(err, RuntimeError)
        self.assertEqual(task["status"], "failed")
        self.assertIn("模型", task["error_msg"])
        self.assertFalse((ws / "index.html").exists())          # 不再交付关键词模板

    def test_plan_failure_falls_back_to_verbatim_brief(self):
        task, rep, lines, ws, err = self._run({"plan": "bad"})
        self.assertIsNone(err)
        self.assertEqual(task["status"], "completed")
        self.assertTrue(rep["brief"].get("fallback"))
        self.assertEqual(len(rep["brief"]["checklist"]), 1)

    def test_non_html_output_fails(self):
        task, rep, lines, ws, err = self._run({"write": "text"}, max_rounds=2)
        self.assertIsNotNone(err)
        self.assertEqual(task["status"], "failed")
        self.assertEqual([c[0] for c in _FakeLLM.calls].count("write"), 2)      # 重试过一次

    def test_review_failure_caps_score(self):
        task, rep, lines, ws, err = self._run({"review": "error"}, max_rounds=1)
        self.assertIsNone(err)
        self.assertLessEqual(task["similarity_score"], 70)
        self.assertEqual(rep["rounds"][0]["review_mode"], "none")

    def test_fenced_output_and_uir_track_escape_hatch(self):
        task, rep, lines, ws, err = self._run({"write": "fenced"}, vision=True)
        self.assertIsNone(err)
        self.assertTrue((ws / "index.html").read_text(encoding="utf-8").startswith("<!DOCTYPE html>"))
        task, rep, lines, ws, err = self._run(key=False, track="uir")
        self.assertIsNone(err)
        self.assertEqual(rep["scoring"], "desc-self-v1")


if __name__ == "__main__":
    unittest.main()
