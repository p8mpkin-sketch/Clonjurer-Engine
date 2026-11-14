"""L1 生成用例离线跑分(不经 HTTP, 直接驱动 GenerateEngine + 真实浏览器池)。

用法: ./.venv/bin/python engine_bench/gen_cases/run_gen_cases.py [--holdout] [--rounds N] [--budget S] [--only id,id]
产物: engine_bench/gen_cases/runs/run_<ts>.json / REPORT_<ts>.md
口径: 截图用例 = compute_visual_similarity(参考图, 产物图) 像素评分(与克隆侧同函数);
      描述用例 = desc-self 自评(无参考图), 单列不与像素分混均。
分组: tuned(5 描述 + 5 原型回环截图) / holdout(真实站点原站截图, 只看不调), 沿用 run_corpus 的分组报分模式。
工作空间与导出走 scratchpad 隔离目录, 不污染仓库 workspaces/。
"""
import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

_ISO = tempfile.mkdtemp(prefix="gen_bench_")
config.WORKSPACE_ROOT = Path(_ISO) / "workspaces"
config.EXPORTS_DIR = Path(_ISO) / "exports"
config.WORKSPACE_ROOT.mkdir(parents=True)
config.EXPORTS_DIR.mkdir(parents=True)

from core.browser_pool import SyncBrowserPool  # noqa: E402
from core.uir import settings, render  # noqa: E402
from core.uir.generate import GenerateEngine  # noqa: E402
from core.uir.adapters.description import build_from_description  # noqa: E402
from core.uir.targets.html_css import HtmlCssTarget  # noqa: E402


class FakeDB:
    def __init__(self):
        self.tasks, self.batches, self.n = {}, {}, 0

    def new(self, spec: dict) -> int:
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


class Bus:
    def __init__(self, verbose):
        self.verbose, self.lines = verbose, []

    def publish(self, tid, stage, msg, level=None):
        self.lines.append(f"[{stage}] {msg}")
        if self.verbose:
            print(f"  [{stage}] {msg}", flush=True)


def run_one(db, pool, spec, verbose) -> dict:
    tid = db.new(spec)
    bus = Bus(verbose)
    t0 = time.monotonic()
    eng = GenerateEngine(tid, db, bus, pool)
    try:
        eng.run()
        err = ""
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:300]
    task = db.get_task(tid)
    rep = {}
    rp = config.WORKSPACE_ROOT / str(tid) / "generate_report.json"
    if rp.is_file():
        rep = json.loads(rp.read_text(encoding="utf-8"))
    best_round = next((x for x in (rep.get("rounds") or []) if x.get("round") == rep.get("best_round")), None)
    return {"task_id": tid, "status": task.get("status"), "score": task.get("similarity_score"),
            "scoring": rep.get("scoring"), "tier": (best_round or {}).get("tier") or rep.get("tier"),
            "ref_text": rep.get("reference_text"), "rounds": len(rep.get("rounds") or []),
            "best_round": rep.get("best_round"), "nodes": (rep.get("uir_stats") or {}).get("nodes"),
            "elapsed": round(time.monotonic() - t0, 1), "error": err,
            "detail": {k: (rep.get("rounds") or [{}])[-1].get(k) for k in ("structure", "edge", "text")},
            "ws": str(config.WORKSPACE_ROOT / str(tid))}


def make_archetype_reference(pool, arch: str, out_png: Path):
    """原型页 1440 宽渲染截图 = 截图用例的已知真值参考。"""
    doc = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    text = next(c["text"] for c in doc["description"] if c["archetype"] == arch)
    uir = build_from_description(text, {"width": 1440, "height": 900})
    d = out_png.parent / f"ref_{arch}"
    r = HtmlCssTarget().render(uir, d)
    render.screenshot_dir(pool, d, r.entry_file, out_png, {"width": 1440, "height": 900})
    return out_png


def _summary_one(results):
    ok = [r for r in results if r["status"] == "completed" and isinstance(r.get("score"), (int, float))]
    pix = [r for r in ok if r.get("scoring") == "v3-segmented"]
    desc = [r for r in ok if r.get("scoring") == "desc-self-v1"]
    def m(xs):
        return round(statistics.mean(xs), 1) if xs else None
    return {"n": len(results), "completed": len(ok), "failed": len(results) - len(ok),
            "pixel_mean": m([r["score"] for r in pix]), "pixel_ge85": sum(1 for r in pix if r["score"] >= 85),
            "pixel_n": len(pix), "desc_mean": m([r["score"] for r in desc]), "desc_ge85": sum(1 for r in desc if r["score"] >= 85),
            "desc_n": len(desc), "ge85_total": sum(1 for r in ok if r["score"] >= 85),
            "elapsed_mean": m([r["elapsed"] for r in ok]),
            "elapsed_p90": (round(sorted(r["elapsed"] for r in ok)[max(0, int(len(ok) * 0.9) - 1)], 1) if ok else None)}


def _summary(results):
    out = _summary_one(results)
    tuned = [r for r in results if not r.get("holdout")]
    held = [r for r in results if r.get("holdout")]
    out["tuned"] = _summary_one(tuned) if tuned else None
    out["holdout"] = _summary_one(held) if held else None
    return out


def _md(results, summary, stamp, args):
    lines = [f"# L1 生成用例跑分 {stamp}", "",
             f"- rounds={args.rounds} budget={args.budget}s target={settings.GENERATE_TARGET_SCORE} holdout={'on' if args.holdout else 'off'} llm_mode={args.llm_mode}",
             f"- AI Key: {'已配置' if config.AI_API_KEY else '未配置(全部走确定性 Tier 0)'}", ""]
    for name, key in (("语料内(tuned)", "tuned"), ("留出集(holdout, 只看不调)", "holdout")):
        g = summary.get(key)
        if not g:
            continue
        lines.append(f"## {name}: {g['completed']}/{g['n']} 完成 · 像素分均值 {g['pixel_mean']} (≥85: {g['pixel_ge85']}/{g['pixel_n']})"
                     f" · 描述自评均值 {g['desc_mean']} (≥85: {g['desc_ge85']}/{g['desc_n']}) · 合计 ≥85: {g['ge85_total']}/{g['completed']}"
                     f" · 耗时均值 {g['elapsed_mean']}s p90 {g['elapsed_p90']}s")
        lines.append("")
        lines.append("| 用例 | 组 | 口径 | 参考文本 | 分数 | 结构 | 边缘 | 文本 | 最优层级 | 轮次 | 节点 | 耗时 | 错误 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for r in sorted([x for x in results if bool(x.get("holdout")) == (key == "holdout")], key=lambda x: (x.get("score") or 0)):
            d = r.get("detail") or {}
            lines.append(f"| {r['id']} | {r['group']} | {r.get('scoring') or '-'} | {r.get('ref_text') or '-'} | {r.get('score')} | {d.get('structure')} | {d.get('edge')} | {d.get('text')} "
                         f"| {r.get('tier')} | {r.get('rounds')} | {r.get('nodes')} | {r.get('elapsed')}s | {r.get('error') or ''} |")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true", help="同时跑真实站点截图留出集")
    ap.add_argument("--rounds", type=int, default=settings.GENERATE_MAX_ROUNDS)
    ap.add_argument("--budget", type=int, default=settings.GENERATE_TIME_BUDGET)
    ap.add_argument("--only", default="")
    ap.add_argument("--llm-mode", default="skeleton_fill", choices=["skeleton_fill", "free"],
                    help="截图路线的 Tier 1 模式: 骨架填充(默认) / 模型自由生成 UIR")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    doc = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    pool = SyncBrowserPool.instance(1)
    db = FakeDB()
    refs_dir = Path(_ISO) / "refs"
    refs_dir.mkdir()
    results = []
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    def want(cid):
        return not only or cid in only

    for c in doc["description"]:
        if not want(c["id"]):
            continue
        print(f"== {c['id']} (描述)", flush=True)
        # track=uir: 这组是关键词原型轨的基线回环; 模型直写网页的用例见 run_code_cases.py
        r = run_one(db, pool, {"kind": "description", "description": c["text"], "max_rounds": args.rounds, "track": "uir",
                               "time_budget": args.budget, "viewport": {"width": 1440, "height": 900}}, args.verbose)
        r.update({"id": c["id"], "group": "desc", "holdout": False})
        results.append(r)
        print(f"   -> {r['status']} score={r['score']} ({r['scoring']}) {r['elapsed']}s {r['error']}", flush=True)
    for arch in doc["screenshot_from_archetype"]:
        cid = f"s_{arch}"
        if not want(cid):
            continue
        ref = make_archetype_reference(pool, arch, refs_dir / f"{arch}.png")
        print(f"== {cid} (截图回环, 参考 {ref.name})", flush=True)
        r = run_one(db, pool, {"kind": "screenshot", "image_path": str(ref), "max_rounds": args.rounds,
                               "time_budget": args.budget, "llm_mode": args.llm_mode}, args.verbose)
        r.update({"id": cid, "group": "shot", "holdout": False})
        results.append(r)
        print(f"   -> {r['status']} score={r['score']} ({r['scoring']}) rounds={r['rounds']} {r['elapsed']}s {r['error']}", flush=True)
    if args.holdout:
        for h in doc["holdout_screenshots"]:
            if not want(h["id"]):
                continue
            src = ROOT / "workspaces" / str(h["workspace"]) / "original_preview.png"
            if not src.is_file():
                print(f"== {h['id']} 跳过: 缺 {src}")
                continue
            print(f"== {h['id']} (真实截图 {h['note']})", flush=True)
            r = run_one(db, pool, {"kind": "screenshot", "image_path": str(src), "max_rounds": args.rounds,
                                   "time_budget": args.budget, "llm_mode": args.llm_mode}, args.verbose)
            r.update({"id": h["id"], "group": "shot", "holdout": True})
            results.append(r)
            print(f"   -> {r['status']} score={r['score']} rounds={r['rounds']} {r['elapsed']}s {r['error']}", flush=True)
    pool.shutdown_all()
    summary = _summary(results)
    out = HERE / "runs"
    out.mkdir(exist_ok=True)
    (out / f"run_{stamp}.json").write_text(json.dumps({"stamp": stamp, "args": vars(args), "summary": summary,
                                                        "results": results, "iso_dir": _ISO}, ensure_ascii=False, indent=2), encoding="utf-8")
    md = _md(results, summary, stamp, args)
    (out / f"REPORT_{stamp}.md").write_text(md, encoding="utf-8")
    print("\n" + md)
    print(f"\n隔离目录: {_ISO}")


if __name__ == "__main__":
    main()
