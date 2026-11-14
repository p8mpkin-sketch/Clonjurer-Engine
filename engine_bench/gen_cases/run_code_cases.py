"""代码直写轨真实模型跑分(不经 HTTP, 直接驱动 GenerateEngine + 真实浏览器池 + 真实模型)。

用法: python engine_bench/gen_cases/run_code_cases.py [--group tuned|holdout|all] [--only id,id]
                                                      [--budget S] [--rounds N] [--workers N] [--out DIR]
需要模型 Key: 环境变量 CLONER_AI_KEY / CLONER_AI_BASE_URL / CLONER_AI_MODEL, 或仓库外的 ~/.clonjurer_ai.env。
产物: <out>/<id>/{index.html, preview.png, f0.png, peak.png, mobile.png, report.json} + summary.json / REPORT.md
口径: 需求符合度 req-fit-v1(core/uir/codegen.py), 与克隆侧的像素还原度不可混均。
工作空间与导出走临时隔离目录, 不污染仓库 workspaces/。
"""
import argparse
import json
import shutil
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

_ISO = tempfile.mkdtemp(prefix="code_bench_")
config.WORKSPACE_ROOT = Path(_ISO) / "workspaces"
config.EXPORTS_DIR = Path(_ISO) / "exports"
config.WORKSPACE_ROOT.mkdir(parents=True)
config.EXPORTS_DIR.mkdir(parents=True)

from core.browser_pool import SyncBrowserPool  # noqa: E402
from core.uir import llm, settings  # noqa: E402
from core.uir.generate import GenerateEngine  # noqa: E402


class FakeDB:
    def __init__(self):
        self.tasks, self.batches, self.n, self.lock = {}, {}, 0, threading.Lock()

    def new(self, spec: dict) -> int:
        with self.lock:
            self.n += 1
            n = self.n
        self.batches[n] = {"id": n, "config_json": json.dumps({"mode": "generate", "gen": spec})}
        self.tasks[n] = {"id": n, "batch_id": n, "status": "pending", "target_url": settings.TARGET_URL_PREFIX + "description"}
        return n

    def get_task(self, tid):
        return dict(self.tasks[tid])

    def get_batch(self, bid):
        return dict(self.batches[bid])

    def update_task(self, tid, **f):
        self.tasks[tid].update(f)

    def increment_completed(self, bid):
        pass


class Bus:
    def __init__(self, tag, verbose):
        self.tag, self.verbose, self.lines = tag, verbose, []

    def publish(self, tid, stage, msg, level=None):
        self.lines.append(f"[{stage}] {msg}")
        if self.verbose:
            print(f"   {self.tag} [{stage}] {msg}", flush=True)


def run_case(db, pool, case, args, out: Path) -> dict:
    spec = {"kind": "description", "description": case["text"], "time_budget": args.budget,
            "max_rounds": args.rounds, "target_score": args.target, "viewport": {"width": 1440, "height": 900}}
    tid = db.new(spec)
    bus = Bus(case["id"], args.verbose)
    t0, err = time.monotonic(), ""
    try:
        GenerateEngine(tid, db, bus, pool).run()
    except Exception as exc:
        err = f"{type(exc).__name__}: {str(exc)[:200]}"
    elapsed = round(time.monotonic() - t0, 1)
    ws = config.WORKSPACE_ROOT / str(tid)
    dest = out / case["id"]
    dest.mkdir(parents=True, exist_ok=True)
    for src, name in (("index.html", "index.html"), ("cloned_preview.png", "preview.png"), ("gen_f0.png", "f0.png"),
                      ("gen_peak.png", "peak.png"), ("gen_after.png", "after.png"), ("gen_mobile.png", "mobile.png"),
                      ("generate_report.json", "report.json"), ("similarity_detail.json", "detail.json"),
                      ("gen_probe.json", "probe.json")):
        if (ws / src).is_file():
            shutil.copyfile(ws / src, dest / name)
    (dest / "log.txt").write_text("\n".join(bus.lines), encoding="utf-8")
    rep = json.loads((ws / "generate_report.json").read_text(encoding="utf-8")) if (ws / "generate_report.json").is_file() else {}
    det = json.loads((ws / "similarity_detail.json").read_text(encoding="utf-8")) if (ws / "similarity_detail.json").is_file() else {}
    items = det.get("items") or []
    r = {"id": case["id"], "group": case["group"], "text": case["text"], "status": db.get_task(tid)["status"],
         "score": rep.get("final_score"), "coverage": det.get("coverage"), "health": det.get("health"),
         "design": det.get("design"), "review_mode": det.get("review_mode"),
         "items_yes": sum(1 for i in items if i["verdict"] == "yes"), "items": len(items),
         # 与评分口径一致: 只有判 no 才算「未满足」(触发 60 分封顶); partial 单列, 不混在一起
         "must_unmet": [i["text"] for i in items if i.get("must") and i["verdict"] == "no"],
         "must_partial": [i["text"] for i in items if i.get("must") and i["verdict"] == "partial"],
         "rounds": len(rep.get("rounds") or []), "best_round": rep.get("best_round"),
         "elapsed": elapsed, "llm_seconds": rep.get("llm_seconds"), "tokens": rep.get("tokens"),
         "defects": rep.get("remaining_defects") or [], "caps": [c["reason"] for c in det.get("caps") or []], "error": err}
    print(f"== {case['id']:10s} {r['status']:9s} score={r['score']} 清单 {r['items_yes']}/{r['items']} "
          f"轮 {r['rounds']} {elapsed}s tokens={r['tokens']} {err}", flush=True)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="tuned", choices=("tuned", "holdout", "all"))
    ap.add_argument("--only", default="")
    ap.add_argument("--budget", type=int, default=settings.GENERATE_TIME_BUDGET)
    ap.add_argument("--rounds", type=int, default=1 + settings.CODE_MAX_REPAIRS)
    ap.add_argument("--target", type=float, default=settings.CODE_TARGET_SCORE)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default="")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    ai = llm.resolve_ai_cfg({})
    if not ai:
        sys.exit("没有可用的模型 Key(CLONER_AI_KEY 或 ~/.clonjurer_ai.env)")
    doc = json.loads((HERE / "code_cases.json").read_text(encoding="utf-8"))
    only = {x for x in args.only.split(",") if x}
    cases = [dict(c, group=g) for g in ("tuned", "holdout") if args.group in (g, "all") for c in doc[g]
             if not only or c["id"] in only]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else HERE / "runs" / f"code_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"模型 {ai['model']} @ {ai['base_url']}; {len(cases)} 个用例, 并发 {args.workers}, 预算 {args.budget}s / {args.rounds} 轮", flush=True)
    db, pool = FakeDB(), SyncBrowserPool.instance(max(2, args.workers * 2))
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        results = list(ex.map(lambda c: run_case(db, pool, c, args, out), cases))
    wall = round(time.monotonic() - t0, 1)
    ok = [r for r in results if r["score"] is not None]
    summary = {"stamp": stamp, "model": ai["model"], "wall": wall, "cases": results, "n": len(results), "completed": len(ok),
               "mean_score": round(statistics.mean(r["score"] for r in ok), 1) if ok else None,
               "median_elapsed": round(statistics.median(r["elapsed"] for r in ok), 1) if ok else None,
               "tokens": {k: sum((r["tokens"] or {}).get(k, 0) for r in results) for k in ("in", "out")}}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# 代码直写轨跑分 {stamp}", "", f"模型 `{ai['model']}`; 口径 req-fit-v1; 墙钟 {wall}s; "
             f"完成 {len(ok)}/{len(results)}; 均分 {summary['mean_score']}; 耗时中位 {summary['median_elapsed']}s; "
             f"tokens 入 {summary['tokens']['in']} / 出 {summary['tokens']['out']}", "",
             "| 用例 | 组 | 需求 | 分数 | 清单 | 健康 | 设计 | 验收 | 轮 | 耗时 | 明确要求里未满足或部分满足的 / 遗留硬伤 |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        left = "; ".join(["未满足: " + t for t in r["must_unmet"]] + ["部分: " + t for t in r["must_partial"]]
                         + r["defects"])[:200] or (r["error"] or "—")
        lines.append(f"| {r['id']} | {r['group']} | {r['text'][:28]} | {r['score']} | {r['items_yes']}/{r['items']} | {r['health']} | "
                     f"{r['design']} | {r['review_mode']} | {r['rounds']} | {r['elapsed']}s | {left} |")
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n完成 {len(ok)}/{len(results)}, 均分 {summary['mean_score']}, 墙钟 {wall}s → {out}", flush=True)
    pool._teardown_thread_browser()
    shutil.rmtree(_ISO, ignore_errors=True)


if __name__ == "__main__":
    main()
