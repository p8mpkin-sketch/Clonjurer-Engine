"""对已完成的克隆工作区跑「交互可用」检查(不重新克隆)。

用法: python engine_bench/interaction/run_interaction.py --data <主仓库> --ids 588,591,... [--out DIR]
需要外网访问原站。输出每个任务的 interaction_check 与汇总。
"""
import argparse, json, sys, time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.clone.interaction_check import check, penalty  # noqa: E402
from core.clone.support import _LocalPreviewServer  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--ids", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--shim", action="store_true", help="先把 ARIA 兜底层注入到工作区副本的入口页再检查(评估兜底层收益, 不改原工作区)")
    a = ap.parse_args()
    import sqlite3
    from playwright.sync_api import sync_playwright
    db = sqlite3.connect(Path(a.data) / "cloner.db")
    out = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()

        @contextmanager
        def ctx_factory(vp):
            c = b.new_context(viewport=vp)
            try:
                yield c
            finally:
                c.close()
        for tid in [int(x) for x in a.ids.split(",") if x]:
            url, score = db.execute("select target_url, similarity_score from clone_tasks where id=?", (tid,)).fetchone()
            ws = Path(a.data) / "workspaces" / str(tid)
            site = json.loads((ws / "site.json").read_text(encoding="utf-8"))
            entry = (site.get("routes") or {}).get("/", "index.html")
            tmp = None
            if a.shim:
                import re, shutil, tempfile
                from core.clone.aria_shim import ARIA_SHIM_JS
                tmp = Path(tempfile.mkdtemp(prefix="ic_shim_")) / "ws"
                shutil.copytree(ws, tmp, symlinks=True, ignore=shutil.ignore_patterns("flask_app", "frames", "*.zip", "capture.mhtml"))
                for rel in {v for v in (site.get("routes") or {}).values() if str(v).endswith(".html")} | {entry}:
                    f = tmp / rel                        # 与引擎一致: 每个交付页都注入(入口页常是跳转壳)
                    if not f.is_file():
                        continue
                    html = f.read_text(encoding="utf-8", errors="replace")
                    m = re.search(r"<head[^>]*>", html, re.I)
                    at = m.end() if m else 0
                    f.write_text(html[:at] + ARIA_SHIM_JS + html[at:], encoding="utf-8")
                ws = tmp
            srv = _LocalPreviewServer(ws)
            srv.start()
            t = time.time()
            try:
                r = check(ctx_factory, url, f"http://127.0.0.1:{srv.port}/{entry}", limit=12, budget_s=75)
            except Exception as exc:
                r = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
            finally:
                srv.stop()
                if tmp is not None:
                    import shutil
                    shutil.rmtree(tmp.parent, ignore_errors=True)
            row = {"id": tid, "url": url, "score": score, **r, "penalty": penalty(r) if "error" not in r else None}
            out.append(row)
            print(f"#{tid} {url[:50]:50s} score={score} checked={r.get('checked')} ok={r.get('ok')} "
                  f"failed={len(r.get('failed') or [])} pen={row['penalty']} {time.time()-t:.0f}s "
                  + "; ".join(f"{f['text'][:16]}({f['reason'][:24]})" for f in (r.get('failed') or [])[:3])
                  + (r.get('error') or ''), flush=True)
        b.close()
    if a.out:
        Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
