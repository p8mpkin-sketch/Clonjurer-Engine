"""手机端适配分类测试集: 起合成站点 → 经平台实例克隆每个用例 → 各移动视口探针 → 按机制类别出表。

  python engine_bench/mobile_cases/run.py                      # 全部用例, 默认视口
  python engine_bench/mobile_cases/run.py --only rerun,lazy    # 只跑某几类或某几个用例名
  python engine_bench/mobile_cases/run.py --gate 95            # 任一格低于门槛则退出码 1(CI 用; case_site.KNOWN_GAPS 里的不计)

平台实例不在线时自动起一个(CLONER_PORT=--port); 报告写到 engine_bench/mobile_cases/runs/。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine_bench" / "real_sites"))
from case_site import CASES, CATEGORY_LABEL, ENGINE_GATED, INTERACTIVE_ALSO, KNOWN_GAPS, serve   # noqa: E402

DEFAULT_VPS = "desktop,tablet,mobile,iphone-air,galaxy-s24,ipad-air"
RUNS = ROOT / "engine_bench" / "real_sites" / "runs"


def _up(base):
    try:
        urllib.request.urlopen(base + "/api/auth/state", timeout=3).read()
        return True
    except Exception:   # noqa: BLE001
        return False


def ensure_instance(base, port, log):
    if _up(base):
        return None
    env = dict(os.environ, CLONER_PORT=str(port))
    proc = subprocess.Popen([sys.executable, "app.py"], cwd=str(ROOT), env=env,
                            stdout=open(log, "w"), stderr=subprocess.STDOUT)
    for _ in range(60):
        if _up(base):
            return proc
        time.sleep(1)
    proc.terminate()
    raise SystemExit(f"平台实例未就绪, 见 {log}")


def summarize(doc, names, vps, base_site):
    by_url = {r.get("url"): r for r in doc.get("results", [])}
    rows = []
    for n in names:
        r = by_url.get(f"{base_site}/{n}") or {}
        row = {"case": n, "category": CASES[n][0], "status": r.get("status"), "task_id": r.get("task_id"),
               "engine": r.get("score"), "interactive": r.get("interactive"),
               "vp": {v: (r.get("vp") or {}).get(v) for v in vps}}
        if CASES[n][0] in ENGINE_GATED:
            # 原页每次加载都不同的类别: 只看引擎交互轨分(对冻结基准), 探针分留档不计
            row["vp_probe"], row["vp"] = row["vp"], {"interactive": r.get("interactive")}
        elif CASES[n][0] in INTERACTIVE_ALSO:
            row["vp"]["interactive"] = r.get("interactive")
        rows.append(row)
    return rows


def render(rows, vps, gate):
    out = ["| 类别 | 用例 | 引擎分 | " + " | ".join(vps) + " | 最低 |", "|---|---|---|" + "---|" * (len(vps) + 1)]
    cats = {}
    for r in rows:
        gated = r["category"] in ENGINE_GATED
        vals = [(r.get("vp_probe") or r["vp"]).get(v) for v in vps]
        also = r["category"] in INTERACTIVE_ALSO
        nums = [x for x in (list(r["vp"].values()) if (gated or also) else vals) if isinstance(x, (int, float))]
        if also and not isinstance(r["vp"].get("interactive"), (int, float)):
            nums = []   # 交互轨没出分 = 不达标
        low = min(nums) if nums else None
        r["min"] = low
        cats.setdefault(r["category"], []).append(low)
        mark = "" if (low is not None and low >= gate) else (" ✗(已知缺口)" if r["case"] in KNOWN_GAPS else " ✗")
        eng = "-" if r["engine"] is None else (f"{r['engine']}/交互 {r.get('interactive')}" if (gated or also) else r["engine"])
        out.append(f"| {CATEGORY_LABEL[r['category']]} | {r['case']} | {eng} | "
                   + " | ".join("-" if v is None else f"{v:.1f}" for v in vals)
                   + f" | {'-' if low is None else f'{low:.1f}'}{mark} |")
    out += ["", "| 类别 | 用例数 | 达标数(每格 ≥ %g) | 类内最低 |" % gate, "|---|---|---|---|"]
    ok_all = 0
    for c, lows in cats.items():
        ok = sum(1 for x in lows if x is not None and x >= gate)
        ok_all += ok
        nums = [x for x in lows if x is not None]
        out.append(f"| {CATEGORY_LABEL[c]} | {len(lows)} | {ok} | {'-' if not nums else f'{min(nums):.1f}'} |")
    out.append(f"\n达标 {ok_all}/{len(rows)} 个用例(门槛: 所有视口 ≥ {gate:g})")
    bad = [r["case"] for r in rows if not (r["min"] is not None and r["min"] >= gate)]
    for n in bad:
        if n in KNOWN_GAPS:
            out.append(f"- 已知缺口 {n}: {KNOWN_GAPS[n]}")
    return "\n".join(out), [n for n in bad if n not in KNOWN_GAPS]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8896, help="平台实例端口(不在线时自动起)")
    ap.add_argument("--site-port", type=int, default=0)
    ap.add_argument("--only", default="", help="逗号分隔的类别或用例名")
    ap.add_argument("--viewports", default=DEFAULT_VPS)
    ap.add_argument("--gate", type=float, default=95.0)
    ap.add_argument("--no-fail", action="store_true", help="低于门槛也返回 0(只出表)")
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    out_dir = HERE / "runs"
    out_dir.mkdir(exist_ok=True)
    want = {x.strip() for x in args.only.split(",") if x.strip()}
    names = [n for n, (c, _) in CASES.items() if not want or n in want or c in want]
    if not names:
        raise SystemExit("没有匹配的用例")
    httpd, site = serve(args.site_port)
    proc = ensure_instance(base, args.port, out_dir / "instance.log")
    try:
        before = set(RUNS.glob("run_*.json")) if RUNS.is_dir() else set()
        cmd = [sys.executable, str(ROOT / "engine_bench" / "real_sites" / "run_corpus.py"), "--base", base,
               "--sites", ",".join(f"{site}/{n}" for n in names), "--viewports", args.viewports,
               "--probe-each", "--timeout", "3600"]
        subprocess.call(cmd, cwd=str(ROOT))
        new = sorted(set(RUNS.glob("run_*.json")) - before, key=lambda p: p.stat().st_mtime)
        if not new:
            raise SystemExit("语料运行没有产出 run_*.json")
        doc = json.loads(new[-1].read_text(encoding="utf-8"))
        from viewport_probe import expand_viewports   # noqa: E402
        vps = expand_viewports([v.strip() for v in args.viewports.split(",") if v.strip()])
        rows = summarize(doc, names, vps, site)
        table, bad = render(rows, vps, args.gate)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        (out_dir / f"cases_{stamp}.json").write_text(json.dumps({"run": new[-1].name, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
        (out_dir / f"CASES_{stamp}.md").write_text(table + "\n", encoding="utf-8")
        print("\n" + table)
        print(f"\n明细: {new[-1]}")
        if bad:
            print("未达标(不在已知缺口内): " + ", ".join(bad))
        return 0 if (args.no_fail or not bad) else 1
    finally:
        httpd.shutdown()
        if proc is not None:
            proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
