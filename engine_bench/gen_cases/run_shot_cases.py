"""截图 → 代码直写轨真实模型跑分(不经 HTTP, 直接驱动 GenerateEngine + 真实浏览器池 + 真实模型)。

用法: python engine_bench/gen_cases/run_shot_cases.py [--group tuned|holdout|all] [--only id,id] [--hint 文字]
                                                      [--budget S] [--rounds N] [--workers N] [--out DIR]
产物: <out>/<id>/{ref.png(去外框的参考图), desktop.png(原图宽度渲染), tablet.png, mobile.png, index.html, assets/,
      report.json, detail.json, log.txt, sheet.jpg(原图 | 桌面 | 平板 | 手机 并排)} + summary.json / REPORT.md
口径: shot-fit-v1(core/uir/shotcode.py)。工作空间走临时隔离目录。
"""
import argparse
import json
import os
import shutil
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import run_code_cases as rc  # noqa: E402  (隔离工作区 / FakeDB / Bus)
from run_code_cases import FakeDB, Bus, config  # noqa: E402
from core.browser_pool import SyncBrowserPool  # noqa: E402
from core.uir import llm, settings  # noqa: E402
from core.uir.generate import GenerateEngine  # noqa: E402

# 原始上传(gen_inputs/)与克隆工作区(workspaces/)是仓库外数据; 在工作树里跑时用 CLONER_BENCH_DATA 指向主仓库
MAIN = Path(os.environ.get("CLONER_BENCH_DATA") or ROOT)


def _data_root() -> Path:
    return MAIN


def build_input(pool, case, inputs: Path) -> Path:
    from PIL import Image
    inputs.mkdir(parents=True, exist_ok=True)
    out = inputs / f"{case['id']}.png"
    if out.is_file():
        return out
    root = _data_root()
    src = case["source"]
    if src == "upload":
        shutil.copyfile(root / case["path"], out)
    elif src == "workspace":
        with Image.open(root / "workspaces" / str(case["ws"]) / "original_preview.png") as im:
            im.convert("RGB").crop((0, 0, im.size[0], min(im.size[1], int(case.get("crop_h", 1300))))).save(out)
    elif src == "archetype":
        from run_gen_cases import make_archetype_reference
        make_archetype_reference(pool, case["arch"], out)
    elif src in ("mobile_ws", "page_mobile"):
        page_file = (root / "workspaces" / str(case["ws"]) / "index.html") if src == "mobile_ws" else root / case["path"]
        with pool.context(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True, has_touch=True) as ctx:
            p = ctx.new_page()
            p.route("**/*", lambda r: r.abort() if r.request.url.startswith("http") else r.continue_())
            p.goto(page_file.resolve().as_uri(), wait_until="load")
            p.wait_for_timeout(1500)
            p.screenshot(path=str(out))
            p.close()
    else:
        raise ValueError(src)
    return out


def sheet(dest: Path, files, caption: str):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype("/System/Library/Fonts/Hiragino Sans GB.ttc", 26) if Path("/System/Library/Fonts/Hiragino Sans GB.ttc").is_file() else None
    H = 1000
    tiles = []
    for label, f in files:
        if f and Path(f).is_file():
            with Image.open(f) as im:
                im = im.convert("RGB")
                im = im.crop((0, 0, im.size[0], min(im.size[1], int(im.size[0] * 1.6) if im.size[0] > 600 else im.size[1])))
                k = H / im.size[1]
                tiles.append((label, im.resize((max(1, int(im.size[0] * k)), H))))
    if not tiles:
        return
    W = sum(t[1].size[0] for t in tiles) + 20 * (len(tiles) + 1)
    sh = Image.new("RGB", (W, H + 110), (245, 245, 245))
    d = ImageDraw.Draw(sh)
    d.text((20, 10), caption, fill=(20, 20, 20), font=font)
    x = 20
    for label, im in tiles:
        sh.paste(im, (x, 96))
        d.text((x, 56), label, fill=(60, 60, 60), font=font)
        x += im.size[0] + 20
    sh.thumbnail((3000, 3000))
    sh.save(dest / "sheet.jpg", quality=80)


def run_case(db, pool, case, args, out: Path, inputs: Path) -> dict:
    img = build_input(pool, case, inputs)
    spec = {"kind": "screenshot", "description": args.hint or case.get("hint", ""), "image_path": str(img),
            "time_budget": args.budget, "max_rounds": args.rounds, "target_score": args.target,
            "viewport": {"width": 1440, "height": 900}}
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
    for src, name in (("index.html", "index.html"), ("original_preview.png", "ref.png"), ("gen_fid.png", "desktop.png"),
                      ("gen_tablet.png", "tablet.png"), ("gen_mobile.png", "mobile.png"), ("cloned_preview.png", "preview.png"),
                      ("generate_report.json", "report.json"), ("similarity_detail.json", "detail.json"),
                      ("gen_probe.json", "probe.json"), ("brief.json", "brief.json")):
        if (ws / src).is_file():
            shutil.copyfile(ws / src, dest / name)
    if (ws / "assets").is_dir():
        shutil.copytree(ws / "assets", dest / "assets", dirs_exist_ok=True)
    (dest / "log.txt").write_text("\n".join(bus.lines), encoding="utf-8")
    rep = json.loads((dest / "report.json").read_text(encoding="utf-8")) if (dest / "report.json").is_file() else {}
    det = json.loads((dest / "detail.json").read_text(encoding="utf-8")) if (dest / "detail.json").is_file() else {}
    items = det.get("items") or []
    fid = det.get("fidelity") or {}
    pr = json.loads((dest / "probe.json").read_text(encoding="utf-8")) if (dest / "probe.json").is_file() else {}
    r = {"id": case["id"], "group": case["group"], "note": case.get("note", case.get("arch", "")),
         "status": db.get_task(tid)["status"], "score": rep.get("final_score"), "fidelity": fid.get("score"),
         "structure": fid.get("structure"), "text": fid.get("text"), "coverage": det.get("coverage"),
         "health": det.get("health"), "design": det.get("design"),
         "items_yes": sum(1 for i in items if i["verdict"] == "yes"), "items": len(items),
         "overflow": {"desktop": (pr.get("layout") or {}).get("overflow_x"), "tablet": (pr.get("tablet") or {}).get("overflow_x"),
                      "mobile": (pr.get("mobile") or {}).get("overflow_x")},
         "source": rep.get("source"), "assets": len(rep.get("assets") or []),
         "rounds": len(rep.get("rounds") or []), "elapsed": elapsed, "tokens": rep.get("tokens"),
         "defects": rep.get("remaining_defects") or [], "error": err}
    sheet(dest, [("原图(去外框, CSS 尺寸)", dest / "ref.png"), ("产出 · 原图宽度", dest / "desktop.png"),
                 ("产出 · 平板 768", dest / "tablet.png"), ("产出 · 手机 390", dest / "mobile.png")],
          f"{case['id']} · 分 {r['score']} · 相似度 {r['fidelity']} · {elapsed}s")
    print(f"== {case['id']:12s} {r['status']:9s} score={r['score']} fid={r['fidelity']} 清单 {r['items_yes']}/{r['items']} "
          f"溢出 {r['overflow']} 素材 {r['assets']} 轮 {r['rounds']} {elapsed}s {err}", flush=True)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="tuned", choices=("tuned", "holdout", "all"))
    ap.add_argument("--only", default="")
    ap.add_argument("--hint", default="")
    ap.add_argument("--budget", type=int, default=settings.GENERATE_TIME_BUDGET)
    ap.add_argument("--rounds", type=int, default=1 + settings.CODE_MAX_REPAIRS)
    ap.add_argument("--target", type=float, default=settings.CODE_TARGET_SCORE)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default="")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    ai = llm.resolve_ai_cfg({})
    if not ai:
        sys.exit("没有可用的模型 Key")
    doc = json.loads((HERE / "shot_cases.json").read_text(encoding="utf-8"))
    only = {x for x in args.only.split(",") if x}
    cases = [dict(c, group=g) for g in ("tuned", "holdout") if args.group in (g, "all") for c in doc[g]
             if not only or c["id"] in only]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else HERE / "runs" / f"shot_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    inputs = HERE / "runs" / "shot_inputs"
    print(f"模型 {ai['model']}; {len(cases)} 个用例, 并发 {args.workers}, 预算 {args.budget}s / {args.rounds} 轮", flush=True)
    db, pool = FakeDB(), SyncBrowserPool.instance(max(2, args.workers * 2))
    for c in cases:                                   # 输入先串行备好(原型页渲染要用浏览器池)
        build_input(pool, c, inputs)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        results = list(ex.map(lambda c: run_case(db, pool, c, args, out, inputs), cases))
    wall = round(time.monotonic() - t0, 1)
    ok = [r for r in results if r["score"] is not None]
    mean = lambda k, rs: round(statistics.mean(r[k] for r in rs if r.get(k) is not None), 1) if [r for r in rs if r.get(k) is not None] else None  # noqa: E731
    summary = {"stamp": stamp, "model": ai["model"], "wall": wall, "cases": results, "completed": len(ok),
               "mean_score": mean("score", ok), "mean_fidelity": mean("fidelity", ok),
               "by_group": {g: {"score": mean("score", [r for r in ok if r["group"] == g]),
                                "fidelity": mean("fidelity", [r for r in ok if r["group"] == g])} for g in ("tuned", "holdout")},
               "median_elapsed": round(statistics.median(r["elapsed"] for r in ok), 1) if ok else None}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# 截图代码直写轨跑分 {stamp}", "", f"模型 `{ai['model']}`; 口径 shot-fit-v1; 墙钟 {wall}s; 完成 {len(ok)}/{len(results)}; "
             f"均分 {summary['mean_score']}(tuned {summary['by_group']['tuned']['score']} / holdout {summary['by_group']['holdout']['score']}); "
             f"原图相似度均值 {summary['mean_fidelity']}; 耗时中位 {summary['median_elapsed']}s", "",
             "| 用例 | 组 | 说明 | 分数 | 原图相似度 | 结构 | 文字 | 清单 | 设计 | 溢出 桌/平/手 | 素材 | 轮 | 耗时 | 遗留 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    yn = lambda v: "是" if v else "否"  # noqa: E731
    for r in results:
        o = r["overflow"]
        lines.append(f"| {r['id']} | {r['group']} | {r['note']} | {r['score']} | {r['fidelity']} | {r['structure']} | {r['text']} | "
                     f"{r['items_yes']}/{r['items']} | {r['design']} | {yn(o['desktop'])}/{yn(o['tablet'])}/{yn(o['mobile'])} | "
                     f"{r['assets']} | {r['rounds']} | {r['elapsed']}s | {('; '.join(r['defects']) or r['error'] or '—')[:120]} |")
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n完成 {len(ok)}/{len(results)}, 均分 {summary['mean_score']}, 相似度 {summary['mean_fidelity']}, 墙钟 {wall}s → {out}", flush=True)
    pool._teardown_thread_browser()
    shutil.rmtree(rc._ISO, ignore_errors=True)


if __name__ == "__main__":
    main()
