"""引擎算法通用性评分(六项加权), 把 SCORECARD_20261111 的人工折算口径写成固定规则。

只看引擎没调过的东西: 留出站、机制合成集、跨输入(L1)、跨应用/跨目标(L2/L3)。

用法(每项输入都可给多份, 同一站/用例取中位数; 缺项按 0 权重另报, 不臆造):
  python engine_bench/score_generality.py \
      --corpus runs/ci_1.json runs/ci_2.json runs/cn_1.json ... \
      --cases  engine_bench/real_sites/runs/mobile_cases/cases_*.json \
      --l1     engine_bench/gen_cases/runs/run_*.json \
      --a11y   engine_bench/a11y_cases/runs/a11y_*.json \
      --xtarget engine_bench/a11y_cases/runs/xtarget_*.json \
      [--blind runs/blind_*.json] [--out SCORE_GEN.md]

规则(改规则 = 改口径, 须在评分卡里注明):
  头条 30%  = 留出站(holdout=true, 非环境受阻)逐站头条分中位数的均值
  窄屏 20%  = 留出站逐站 平板/手机 探针分中位数, 两视口各取均值后再平均
  合成 15%  = 机制合成集通过率 × 100(每个视口都 ≥ 门槛才算过; 已知缺口也算不过)
  L1   15%  = L1 真实截图留出像素分均值
  L2L3 15%  = 留出样本分 × (0.7 + 0.3 × 覆盖度); 留出样本分 = a11y 留出(with_shot) 与跨目标留出(flutter)均值的平均;
              覆盖度 = (min(1, 留出不同应用数/4) + min(1, 留出平台数/3)) / 2
  纪律 5%   = 95(禁特判守卫在) ; 另有盲测集且 |盲测头条 - 留出头条| ≤ 3 时 98; 守卫缺失 60
另报(不计分): 语料内头条(回退闸门)、交互轨与头条差(双轨收敛)、盲测集头条。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = {"headline": 0.30, "narrow": 0.20, "cases": 0.15, "l1": 0.15, "l2l3": 0.15, "discipline": 0.05}
LABEL = {"headline": "留出站头条", "narrow": "留出窄屏", "cases": "机制合成集", "l1": "L1 跨输入",
         "l2l3": "L2/L3 跨应用跨目标", "discipline": "防过拟合纪律"}


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


ROW_KEYS = ("url", "holdout", "status", "score", "interactive", "fidelity", "blocked", "vp", "elapsed_s")


def emit_rows(path):
    """把 run json 压成每站一行 GENROW|{...}(CI 日志尾部; 远程只读得到日志尾)。合成集 cases json 出 GENCASE 行。"""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    for r in doc.get("results", []):
        print("GENROW|" + json.dumps({k: r.get(k) for k in ROW_KEYS if r.get(k) is not None},
                                     ensure_ascii=False, separators=(",", ":")))
    for r in doc.get("rows", []):
        print("GENCASE|" + json.dumps({"case": r.get("case"), "vp": r.get("vp")}, ensure_ascii=False, separators=(",", ":")))


def parse_log(text):
    """日志文本(可带时间戳前缀) → {"results": [...], "rows": [...]}。"""
    out = {"results": [], "rows": []}
    for line in text.splitlines():
        for tag, key in (("GENROW|", "results"), ("GENCASE|", "rows")):
            i = line.find(tag)
            if i >= 0:
                try:
                    out[key].append(json.loads(line[i + len(tag):]))
                except ValueError:
                    pass
    return out


def _load(paths):
    docs = []
    for p in paths or []:
        try:
            text = Path(p).read_text(encoding="utf-8")
            docs.append(parse_log(text) if "GENROW|" in text or "GENCASE|" in text else json.loads(text))
        except (OSError, ValueError) as exc:
            print(f"(跳过 {p}: {exc})", file=sys.stderr)
    return docs


def _mean(xs):
    xs = [x for x in xs if _num(x)]
    return round(statistics.mean(xs), 1) if xs else None


def _site_medians(docs, holdout):
    """url → {headline, interactive, tablet, mobile, desktop} 各取多次运行的中位数。"""
    acc: dict = {}
    for d in docs:
        for r in d.get("results", []):
            if bool(r.get("holdout")) != holdout or r.get("blocked") or r.get("status") != "completed":
                continue
            a = acc.setdefault(r.get("url"), {"headline": [], "interactive": [], "desktop": [], "tablet": [], "mobile": []})
            if _num(r.get("score")):
                a["headline"].append(r["score"])
            if _num(r.get("interactive")):
                a["interactive"].append(r["interactive"])
            for v in ("desktop", "tablet", "mobile"):
                x = (r.get("vp") or {}).get(v)
                if _num(x):
                    a[v].append(x)
    return {u: {k: (round(statistics.median(v), 1) if v else None) for k, v in a.items()} | {"runs": len(a["headline"])}
            for u, a in acc.items()}


def corpus_part(docs):
    held = _site_medians(docs, True)
    tuned = _site_medians(docs, False)
    out = {"sites": held, "n_holdout": len(held), "n_tuned": len(tuned)}
    out["headline"] = _mean(s["headline"] for s in held.values())
    tab = _mean(s["tablet"] for s in held.values())
    mob = _mean(s["mobile"] for s in held.values())
    out["tablet"], out["mobile"], out["desktop"] = tab, mob, _mean(s["desktop"] for s in held.values())
    out["narrow"] = _mean([tab, mob])
    out["tuned_headline"] = _mean(s["headline"] for s in tuned.values())
    gaps = [s["headline"] - s["interactive"] for s in held.values() if _num(s["headline"]) and _num(s["interactive"])]
    out["track_gap_median"] = round(statistics.median(gaps), 1) if gaps else None
    out["track_gap_max"] = round(max(gaps), 1) if gaps else None
    return out


def cases_part(docs, gate):
    lows: dict = {}
    for d in docs:
        for r in d.get("rows", []):
            vals = [x for x in (r.get("vp") or {}).values() if _num(x)]
            lows.setdefault(r["case"], []).append(min(vals) if vals and len(vals) == len(r.get("vp") or {}) else None)
    if not lows:
        return {}
    med = {c: (statistics.median([x for x in v if _num(x)]) if any(_num(x) for x in v) else None) for c, v in lows.items()}
    ok = sorted(c for c, x in med.items() if _num(x) and x >= gate)
    return {"n": len(med), "pass": len(ok), "score": round(100 * len(ok) / len(med), 1),
            "failing": sorted(set(med) - set(ok))}


def l1_part(docs):
    vals = [((d.get("summary") or {}).get("holdout") or {}).get("pixel_mean") for d in docs]
    ge = [((d.get("summary") or {}).get("holdout") or {}).get("pixel_ge85") for d in docs]
    loop = [((d.get("summary") or {}).get("tuned") or {}).get("pixel_mean") for d in docs]
    return {"score": _mean(vals), "ge85": ge[-1] if ge else None, "loop": _mean(loop)}


def _holdout_cases(group="holdout"):
    apps, plats = set(), set()
    for f in (ROOT / "engine_bench" / "a11y_cases" / group).glob("*/tree.json"):
        try:
            t = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        apps.add(t.get("app") or f.parent.name)
        plats.add(t.get("platform") or "?")
    return apps, plats


def l2l3_part(a11y_docs, xt_docs):
    a = _mean(((d.get("summary") or {}).get("holdout") or {}).get("with_shot") for d in a11y_docs)
    x = _mean(((d.get("summary") or {}).get("holdout") or {}).get("flutter") for d in xt_docs)
    base = _mean([a, x])
    apps, plats = _holdout_cases()
    cov = round((min(1, len(apps) / 4) + min(1, len(plats) / 3)) / 2, 3)
    return {"a11y_holdout": a, "xtarget_holdout": x, "apps": sorted(apps), "platforms": sorted(plats),
            "coverage": cov, "score": round(base * (0.7 + 0.3 * cov), 1) if _num(base) else None}


def discipline_part(headline, blind):
    guard = (ROOT / "tests" / "test_no_site_special_case.py").exists()
    if not guard:
        return {"score": 60, "guard": False}
    if _num(blind) and _num(headline) and abs(blind - headline) <= 3:
        return {"score": 98, "guard": True}
    return {"score": 95, "guard": True}


def score(args):
    c = corpus_part(_load(args.corpus))
    blind = corpus_part([{**d, "results": [{**r, "holdout": True} for r in d.get("results", [])]}
                         for d in _load(args.blind)]) if args.blind else {}
    parts = {
        "headline": c.get("headline"),
        "narrow": c.get("narrow"),
        "cases": cases_part(_load(args.cases), args.gate).get("score"),
        "l1": l1_part(_load(args.l1)).get("score"),
        "l2l3": l2l3_part(_load(args.a11y), _load(args.xtarget)).get("score"),
        "discipline": discipline_part(c.get("headline"), blind.get("headline")).get("score"),
    }
    have = {k: v for k, v in parts.items() if _num(v)}
    total = round(sum(v * WEIGHTS[k] for k, v in have.items()), 1)
    w = sum(WEIGHTS[k] for k in have)
    return {
        "parts": parts, "total": total if w > 0.999 else None,
        "total_partial": round(total / w, 1) if w else None, "weight_covered": round(w, 2),
        "corpus": c, "blind": {k: blind.get(k) for k in ("headline", "narrow", "n_holdout")} if blind else None,
        "cases": cases_part(_load(args.cases), args.gate), "l1": l1_part(_load(args.l1)),
        "l2l3": l2l3_part(_load(args.a11y), _load(args.xtarget)),
    }


def render(res):
    p = res["parts"]
    lines = ["| 子项(权重) | 分 | 依据 |", "|---|---|---|"]
    c, cs, l1, l2 = res["corpus"], res["cases"], res["l1"], res["l2l3"]
    why = {
        "headline": f"留出 {c.get('n_holdout')} 站逐站中位数均值",
        "narrow": f"平板 {c.get('tablet')} / 手机 {c.get('mobile')}(桌面噪声基线 {c.get('desktop')})",
        "cases": f"{cs.get('pass')}/{cs.get('n')} 过; 未过 {', '.join(cs.get('failing') or []) or '无'}" if cs else "未提供",
        "l1": f"留出 ≥85: {l1.get('ge85')}, 回环 {l1.get('loop')}",
        "l2l3": f"a11y 留出 {l2.get('a11y_holdout')} / 跨目标 {l2.get('xtarget_holdout')}, 覆盖度 {l2.get('coverage')}"
                f"(应用 {len(l2.get('apps') or [])}, 平台 {','.join(l2.get('platforms') or [])})",
        "discipline": "禁特判守卫" + (", 盲测差 ≤3" if p["discipline"] == 98 else ""),
    }
    for k in WEIGHTS:
        v = p[k]
        lines.append(f"| {LABEL[k]}({int(WEIGHTS[k] * 100)}%) | {'-' if v is None else v} | {why[k]} |")
    tot = res["total"] if res["total"] is not None else f"{res['total_partial']}(只覆盖 {res['weight_covered']:.0%} 权重)"
    lines.append(f"| **合计** | **{tot}** | |")
    lines += ["", f"另报: 语料内头条 {c.get('tuned_headline')}; 留出站 头条-交互 差中位数 {c.get('track_gap_median')}"
              f" / 最大 {c.get('track_gap_max')}" + (f"; 盲测头条 {res['blind'].get('headline')}" if res.get("blind") else ""), "",
              "| 留出站 | 次数 | 头条 | 交互 | 桌面 | 平板 | 手机 |", "|---|---|---|---|---|---|---|"]
    for u, s in sorted(c.get("sites", {}).items(), key=lambda kv: kv[1].get("headline") or 0):
        lines.append(f"| {u} | {s['runs']} | {s['headline']} | {s['interactive']} | {s['desktop']} | {s['tablet']} | {s['mobile']} |")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for k in ("corpus", "cases", "l1", "a11y", "xtarget", "blind"):
        ap.add_argument(f"--{k}", nargs="*", default=[])
    ap.add_argument("--gate", type=float, default=95.0)
    ap.add_argument("--out", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--emit-rows", default="", help="把一份 run/cases json 压成 GENROW/GENCASE 行后退出(CI 用)")
    args = ap.parse_args(argv)
    if args.emit_rows:
        return emit_rows(args.emit_rows)
    res = score(args)
    md = render(res)
    print(md)
    if args.out:
        Path(args.out).write_text(md + "\n", encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


if __name__ == "__main__":
    main()
