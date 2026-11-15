"""视觉修复评测集: 对低分克隆的工作区副本跑一次 AI 视觉修复, 记录修复前后分数与改动区域(不改真实工作区)。

用法: python engine_bench/vision_repair/run_repair_eval.py --data <主仓库> --ids 18,9,19,... [--out FILE]
需要模型 Key。
"""
import argparse, json, shutil, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="repair_eval_"))
config.WORKSPACE_ROOT = TMP / "workspaces"
config.WORKSPACE_ROOT.mkdir(parents=True)

from core import ai_vision_repair, cloner_engine  # noqa: E402
from core.browser_pool import SyncBrowserPool  # noqa: E402
from core.uir import llm  # noqa: E402


class _DB:
    def __init__(self, row): self.row = row
    def get_task(self, tid): return dict(self.row)
    def update_task(self, *a, **k): pass
    def __getattr__(self, n): return lambda *a, **k: None


class _Bus:
    def __init__(self, tag): self.tag, self.lines = tag, []
    def publish(self, tid, stage, msg, level=None): self.lines.append(f"[{stage}] {msg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--ids", required=True)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    import sqlite3
    ai = llm.resolve_ai_cfg({})
    if not ai:
        sys.exit("需要模型 Key")
    db = sqlite3.connect(Path(a.data) / "cloner.db"); db.row_factory = sqlite3.Row
    pool = SyncBrowserPool.instance(2)
    out = []
    for tid in [int(x) for x in a.ids.split(",") if x]:
        row = dict(db.execute("select * from clone_tasks where id=?", (tid,)).fetchone())
        src = Path(a.data) / "workspaces" / str(tid)
        ws = config.WORKSPACE_ROOT / str(tid)
        shutil.copytree(src, ws, symlinks=True, ignore=shutil.ignore_patterns("flask_app", "*.zip", "capture.mhtml"))
        detail = json.loads((ws / "similarity_detail.json").read_text(encoding="utf-8")) if (ws / "similarity_detail.json").is_file() else {}
        bus = _Bus(tid)
        eng = cloner_engine.ClonerEngine(tid, _DB(row), bus, pool)
        eng.bind_target(row["target_url"])
        t0 = time.time()
        try:
            with pool.context(viewport={"width": 1920, "height": 1080}) as ctx:
                rs = eng._make_render_and_score(ctx)
                bd, _f, _r = rs()                       # 修复前在本机重新评一次(与修复后同口径)
                b_score = (bd or {}).get("score")
                if bd:
                    detail = bd
                rep = ai_vision_repair.run_vision_repair(ws, ai, detail, lambda s, m: bus.publish(tid, s, m), rs,
                                                         page_file=eng._main_page_file(), region_cells=eng._region_cells())
            r = {"id": tid, "url": row["target_url"], "db_score": row["similarity_score"], "before": b_score,
                 "after": rep.get("final_score"), "rounds": len(rep.get("rounds") or []),
                 "kept": sum(1 for x in rep.get("rounds") or [] if not x.get("rolled_back") and x.get("score_after") is not None
                             and (x.get("score_before") or 0) < x["score_after"]),
                 "round_scores": [(x.get("score_before"), x.get("score_after"), x.get("rolled_back")) for x in rep.get("rounds") or []],
                 "diagnosis": [str(x.get("diagnosis") or x.get("error") or "")[:120] for x in rep.get("rounds") or []],
                 "elapsed": round(time.time() - t0, 1)}
        except Exception as exc:
            r = {"id": tid, "url": row["target_url"], "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        out.append(r)
        print(json.dumps(r, ensure_ascii=False)[:400], flush=True)
        shutil.rmtree(ws, ignore_errors=True)
    if a.out:
        Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    pool._teardown_thread_browser()
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
