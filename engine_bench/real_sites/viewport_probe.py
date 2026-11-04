"""多视口还原度探针: 桌面视口采集的克隆, 在平板/手机视口下与原站同宽对比。

克隆按 1920 桌面视口采集, 但交付物会在各种宽度下被打开(预览面板的平板/手机切换、
真机访问)。本探针对每个已完成任务, 在同一 UA 下以若干视口宽度分别渲染原站与克隆
预览(/preview/<id>/), 用引擎同款 compute_visual_similarity 打分。

为把"响应式还原缺陷"与"活体原站随机性"分开, 默认同时测 desktop 宽(1920):
desktop 探针分是本方法的噪声基线, tablet/mobile 与它的差才是视口适配差距。

用法:
  python3 engine_bench/real_sites/viewport_probe.py --base http://127.0.0.1:8897 --tasks 536,529
  python3 engine_bench/real_sites/viewport_probe.py --run runs/run_xxx.json   # 跑分产物里的全部完成任务
被 run_corpus.py --viewports 复用(probe_tasks)。
"""
import argparse
import json
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

PROBE_VIEWPORTS = {
    "desktop": {"width": 1920, "height": 1080},
    "tablet": {"width": 768, "height": 1024},
    "mobile": {"width": 390, "height": 844},
}
MAX_SCREENS = 8          # 每侧最多截前 N 屏: 长页尾部(推荐流/页脚)对适配判断信息量低
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 两侧同口径: 停动画/过渡, 滚动一遍触发懒加载, 回顶
SETTLE_CSS = ("*,*::before,*::after{animation-play-state:paused!important;"
              "transition:none!important;caret-color:transparent!important}")
SCROLL_JS = r"""
async (maxH) => {
  const h = () => Math.min(document.documentElement.scrollHeight, maxH);
  for (let y = 0; y < h(); y += innerHeight * 0.8) { scrollTo(0, y); await new Promise(r => setTimeout(r, 120)); }
  scrollTo(0, 0); await new Promise(r => setTimeout(r, 400));
  return document.documentElement.scrollWidth;
}
"""


def _shoot(ctx, url, vp, out: Path, timeout_ms=45000):
    page = ctx.new_page()
    info = {"url": url}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=8000)
        with suppress(Exception):
            page.add_style_tag(content=SETTLE_CSS)
        max_h = vp["height"] * MAX_SCREENS
        sw = page.evaluate(SCROLL_JS, max_h)
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=4000)
        info["scroll_width"] = sw
        info["overflow_x"] = sw > vp["width"] + 2
        full_h = page.evaluate("document.documentElement.scrollHeight")
        info["height"] = full_h
        clip_h = min(full_h, max_h)
        page.screenshot(path=str(out), full_page=True, animations="disabled",
                        clip={"x": 0, "y": 0, "width": vp["width"], "height": clip_h})
        info["text"] = page.evaluate("document.body ? document.body.innerText.slice(0, 200000) : ''")
        info["ok"] = True
    except Exception as exc:   # noqa: BLE001 单侧失败记入结果
        info["ok"] = False
        info["error"] = str(exc)[:200]
    finally:
        with suppress(Exception):
            page.close()
    return info


def probe_one(browser, base, task_id, url, viewports, outdir: Path, clone_tpl=None):
    from core.cloner_engine import compute_visual_similarity
    res = {"task_id": task_id, "url": url}
    for name in viewports:
        vp = PROBE_VIEWPORTS[name]
        ctx = browser.new_context(viewport=vp, user_agent=UA, device_scale_factor=1)
        try:
            a = outdir / f"{task_id}_{name}_orig.png"
            b = outdir / f"{task_id}_{name}_clone.png"
            ia = _shoot(ctx, url, vp, a)
            clone_url = (clone_tpl or "{base}/preview/{id}/").format(base=base.rstrip("/"), id=task_id)
            ib = _shoot(ctx, clone_url, vp, b)
        finally:
            ctx.close()
        row = {"orig_overflow_x": ia.get("overflow_x"), "clone_overflow_x": ib.get("overflow_x"),
               "orig_h": ia.get("height"), "clone_h": ib.get("height")}
        if ia.get("ok") and ib.get("ok"):
            sim = compute_visual_similarity(a, b, outdir / f"{task_id}_{name}_diff.png",
                                            orig_text=ia.get("text", ""), clone_text=ib.get("text", ""))
            row.update({k: sim.get(k) for k in ("score", "structure", "edge", "text", "height_ratio")})
        else:
            row["error"] = ia.get("error") or ib.get("error")
        res[name] = row
    return res


def probe_tasks(base, tasks, viewports=("desktop", "tablet", "mobile"), outdir=None, workers=3,
                clone_tpl=None):
    """tasks: [(task_id, url)]。返回 {task_id: {viewport: {score,...}}}。"""
    from playwright.sync_api import sync_playwright
    outdir = Path(outdir or tempfile.mkdtemp(prefix="vprobe_"))
    outdir.mkdir(parents=True, exist_ok=True)
    results = {}

    def work(chunk):
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                for tid, url in chunk:
                    t0 = time.time()
                    r = probe_one(browser, base, tid, url, viewports, outdir, clone_tpl)
                    r["probe_s"] = round(time.time() - t0, 1)
                    results[tid] = r
                    print(f"  探针 #{tid} {url}: " + "  ".join(
                        f"{v} {r.get(v, {}).get('score', '-')}" for v in viewports), flush=True)
            finally:
                browser.close()
    chunks = [tasks[i::workers] for i in range(workers)]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, [c for c in chunks if c]))
    return results, outdir


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8897")
    ap.add_argument("--tasks", default="", help="逗号分隔任务号")
    ap.add_argument("--run", default="", help="run_*.json, 取其中全部完成任务")
    ap.add_argument("--db", default=str(ROOT / "cloner.db"))
    ap.add_argument("--viewports", default="desktop,tablet,mobile")
    ap.add_argument("--out", default="")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--clone-url", default="", help="克隆地址模板, 默认 {base}/preview/{id}/")
    args = ap.parse_args()
    pairs = []
    if args.run:
        doc = json.loads(Path(args.run).read_text(encoding="utf-8"))
        pairs = [(r["task_id"], r["url"]) for r in doc["results"] if r["status"] == "completed"]
    if args.tasks:
        import sqlite3
        con = sqlite3.connect(args.db)
        for tid in [int(x) for x in args.tasks.split(",") if x.strip()]:
            row = con.execute("select target_url from clone_tasks where id=?", (tid,)).fetchone()
            if row:
                pairs.append((tid, row[0]))
    vps = [v.strip() for v in args.viewports.split(",") if v.strip()]
    res, outdir = probe_tasks(args.base, pairs, vps, args.out or None, args.workers,
                              args.clone_url or None)
    print(f"\n截图/热力图: {outdir}")
    print("| # | 站点 | " + " | ".join(vps) + " |")
    print("|---|---|" + "---|" * len(vps))
    for tid, url in pairs:
        r = res.get(tid, {})
        cells = []
        for v in vps:
            c = r.get(v, {})
            s = c.get("score", "-")
            if c.get("clone_overflow_x") and not c.get("orig_overflow_x"):
                s = f"{s} ↔"
            cells.append(str(s))
        print(f"| {tid} | {url} | " + " | ".join(cells) + " |")
    (outdir / "probe.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
