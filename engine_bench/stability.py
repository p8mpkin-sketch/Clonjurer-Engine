"""多轮稳定性报告(ROADMAP_95 §6): 同一批站跑 N 轮后, 逐站分差、不稳定清单、失败分类。

用法(输入与 score_generality 相同: run_corpus 的 run json, 或带 GENROW| 行的 CI 日志; 同一站在多份里出现即为多轮):
  python engine_bench/stability.py runs/ci_1.log runs/ci_2.log runs/cn_1.json ... [--out STABILITY.md] [--json out.json]

口径(改口径须在报告里注明):
  头条分差 = 同站各轮头条 max - min, 门槛 ≤ HEAD_SPREAD(1.5); 视口分差门槛 ≤ VP_SPREAD(10)
  失败分类: 环境受阻(blocked 拦截页) / 不可达(failure 记网络类) / 引擎失败(其余 failed) —— 只有引擎失败计失败率
  稳定分(参考) = 100 × 头条分差达标站占比, 再按引擎失败率扣分(每 1% 扣 5)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score_generality import _load, _num  # noqa: E402

HEAD_SPREAD = 1.5
VP_SPREAD = 10.0
METRICS = ("score", "interactive", "desktop", "tablet", "mobile")
_NET = ("ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION", "ERR_TIMED_OUT", "ERR_INTERNET", "不可达", "unreachable", "ENOTFOUND")


def _classify(r) -> str:
    if r.get("blocked"):
        return "blocked"
    st = r.get("status")
    if st == "completed":
        return "ok"
    text = f"{r.get('failure') or ''} {r.get('error') or ''}"
    if any(m in text for m in _NET):
        return "unreachable"
    return "engine_fail"


def analyse(docs) -> dict:
    sites: dict = {}
    cls = {"ok": 0, "blocked": 0, "unreachable": 0, "engine_fail": 0}
    fails = []
    for d in docs:
        for r in d.get("results", []):
            c = _classify(r)
            cls[c] += 1
            if c == "engine_fail":
                fails.append((r.get("url"), (r.get("error") or r.get("failure") or r.get("status") or "")[:120]))
            if c != "ok":
                continue
            s = sites.setdefault(r.get("url"), {"holdout": bool(r.get("holdout")), **{m: [] for m in METRICS},
                                                "preview_track": []})
            for m in METRICS:
                x = r.get(m) if m in ("score", "interactive") else (r.get("vp") or {}).get(m)
                if _num(x):
                    s[m].append(float(x))
            if r.get("preview_track"):
                s["preview_track"].append(r["preview_track"])
    rows = []
    for url, s in sites.items():
        row = {"url": url, "holdout": s["holdout"], "runs": len(s["score"])}
        for m in METRICS:
            v = s[m]
            row[m] = {"med": round(statistics.median(v), 1), "min": min(v), "max": max(v),
                      "spread": round(max(v) - min(v), 1)} if v else None
        tracks = sorted(set(s["preview_track"]))
        row["preview_flip"] = len(tracks) > 1
        why = []
        if row["runs"] >= 2:
            if row["score"] and row["score"]["spread"] > HEAD_SPREAD:
                why.append(f"头条差 {row['score']['spread']}")
            for m in ("desktop", "tablet", "mobile"):
                if row[m] and row[m]["spread"] > VP_SPREAD:
                    why.append(f"{m} 差 {row[m]['spread']}")
            if row["preview_flip"]:
                why.append("交付轨在轮间切换(" + "/".join(tracks) + ")")
        row["unstable"] = why
        rows.append(row)
    rows.sort(key=lambda r: (-len(r["unstable"]), -(r["score"] or {}).get("spread", 0), r["url"] or ""))
    multi = [r for r in rows if r["runs"] >= 2 and r["score"]]
    ok_head = [r for r in multi if r["score"]["spread"] <= HEAD_SPREAD]
    total = sum(cls.values())
    fail_rate = round(100 * cls["engine_fail"] / total, 2) if total else None
    score = None
    if multi:
        score = round(max(0.0, 100 * len(ok_head) / len(multi) - 5 * (fail_rate or 0)), 1)
    return {"sites": rows, "classes": cls, "engine_fail_rate": fail_rate, "engine_fails": fails,
            "multi_sites": len(multi), "head_ok": len(ok_head), "stability_score": score}


def render(res) -> str:
    c = res["classes"]
    out = ["# 多轮稳定性报告", "",
           f"多轮站 {res['multi_sites']} 个, 头条分差 ≤{HEAD_SPREAD} 的 {res['head_ok']} 个; "
           f"任务 {sum(c.values())} 个: 完成 {c['ok']} / 环境受阻 {c['blocked']} / 不可达 {c['unreachable']} / "
           f"**引擎失败 {c['engine_fail']}**(失败率 {res['engine_fail_rate']}%)。稳定分(参考) **{res['stability_score']}**", ""]
    uns = [r for r in res["sites"] if r["unstable"]]
    out += ["## 不稳定清单", ""]
    if not uns:
        out.append("(无)")
    for r in uns:
        out.append(f"- {r['url']}{' (留出)' if r['holdout'] else ''} × {r['runs']} 轮: " + "; ".join(r["unstable"]))
    out += ["", "## 逐站(中位 [最小~最大])", "", "| 站 | 轮 | 头条 | 交互 | 桌面 | 平板 | 手机 |", "|---|---|---|---|---|---|---|"]

    def cell(x):
        return "" if not x else (f"{x['med']}" if x["spread"] == 0 else f"{x['med']} [{x['min']}~{x['max']}]")
    for r in res["sites"]:
        out.append(f"| {r['url']}{' ★' if r['holdout'] else ''} | {r['runs']} | " +
                   " | ".join(cell(r[m]) for m in METRICS) + " |")
    if res["engine_fails"]:
        out += ["", "## 引擎失败", ""] + [f"- {u}: {e}" for u, e in res["engine_fails"]]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    res = analyse(_load(a.runs))
    text = render(res)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    if a.json:
        Path(a.json).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
