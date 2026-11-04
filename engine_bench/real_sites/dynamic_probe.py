#!/usr/bin/env python3
"""动态形态探针: 度量克隆对"滚动中"形态的还原(静态整页评分度量不到的部分)。

整页基准评分只比"停在顶部、时间冻结"的一张长图; 滚动驱动动画(scroll-timeline /
脚本按 scrollY 写 transform)、sticky 叙事段、进视口才显形的块、自动播放视频/canvas
序列帧都只在"真的滚过去"时才出现。本探针对原站与克隆(本地沙盒, 与评分同一服务器)
各自用同一套滚动脚本逐步滚到若干检查点, 每个检查点截一张视口图, 用引擎同款三通道
评分逐点比较, 并统计两侧的动态特征(sticky 数、滚动时间线动画数、可见视频是否在播、
canvas 是否非空、滚动造成的视口变化量)。

用法:
  python3 engine_bench/real_sites/dynamic_probe.py --task 530            # 读 workspaces/530/site.json
  python3 engine_bench/real_sites/dynamic_probe.py --task 530 --points 8 --out /tmp/x
输出: 逐检查点分数、动态特征对比、均分(滚动轨), 以及 JSON(--out 目录下 dyn_<task>.json)。
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

import config  # noqa: E402
from core.cloner_engine import _LocalPreviewServer, compute_visual_similarity  # noqa: E402

FEATURES_JS = r"""() => {
  const out = {sticky: 0, stickyVisible: 0, scrollAnims: 0, docAnims: 0, videos: 0,
               videosPlaying: 0, videosVisible: 0, canvases: 0, canvasBlank: 0,
               hiddenReveal: 0, scrollY: scrollY, height: document.documentElement.scrollHeight};
  const vis = r => r.width > 4 && r.height > 4 && r.bottom > 0 && r.top < innerHeight;
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.position === 'sticky') { out.sticky++; if (vis(el.getBoundingClientRect())) out.stickyVisible++; }
  }
  try {
    for (const a of document.getAnimations()) {
      if (a.timeline && a.timeline !== document.timeline) out.scrollAnims++; else out.docAnims++;
    }
  } catch (e) {}
  for (const v of document.querySelectorAll('video')) {
    out.videos++;
    const r = v.getBoundingClientRect();
    if (vis(r)) { out.videosVisible++; if (!v.paused && v.readyState >= 2) out.videosPlaying++; }
  }
  for (const c of document.querySelectorAll('canvas')) {
    const r = c.getBoundingClientRect();
    if (!vis(r)) continue;
    out.canvases++;
    try {
      const t = document.createElement('canvas'); t.width = 32; t.height = 32;
      t.getContext('2d').drawImage(c, 0, 0, 32, 32);
      const d = t.getContext('2d').getImageData(0, 0, 32, 32).data;
      let any = false; for (let i = 3; i < d.length; i += 4) if (d[i]) { any = true; break; }
      if (!any) out.canvasBlank++;
    } catch (e) {}
  }
  // 视口内文字/图片块却透明(进视口才显形的入场动画没触发)
  for (const el of document.querySelectorAll('h1,h2,h3,p,img,picture,video')) {
    const r = el.getBoundingClientRect();
    if (!vis(r)) continue;
    let n = el, op = 1;
    while (n && n.nodeType === 1) { op *= parseFloat(getComputedStyle(n).opacity) || 0; n = n.parentElement; }
    if (op < 0.05) out.hiddenReveal++;
  }
  return out;
}"""

# 逐步滚到目标位置(触发滚动监听/IntersectionObserver), 再停 settle_ms 让补间收敛
SCROLL_TO_JS = r"""async ([target, step, pause]) => {
  let y = scrollY;
  const dir = target >= y ? 1 : -1;
  while (Math.abs(target - y) > step) {
    y += dir * step; window.scrollTo(0, y);
    await new Promise(r => setTimeout(r, pause));
  }
  window.scrollTo(0, target);
  return scrollY;
}"""


def _walk(page, url, positions, shot_dir, tag, settle_ms):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    rows = []
    for i, y in enumerate(positions):
        # 页内起跑 + 限时等完成标记: 克隆页脚本卡死时 evaluate 会永久挂起
        try:
            page.evaluate("([src, a]) => { window.__dynDone = null; (0, eval)('(' + src + ')')(a)"
                          ".then(v => { window.__dynDone = v; }); }", [SCROLL_TO_JS, [int(y), 240, 60]])
            page.wait_for_function("window.__dynDone !== null", timeout=30000, polling=100)
            page.wait_for_timeout(settle_ms)
            page.wait_for_function("true", timeout=8000)
        except Exception as exc:
            rows.append({"y": y, "actual": None, "shot": None, "features": {}, "hung": str(exc)[:80]})
            break
        actual = page.evaluate("window.__dynDone")
        feats = page.evaluate(FEATURES_JS)
        path = shot_dir / f"{tag}_{i:02d}.png"
        page.screenshot(path=str(path))
        rows.append({"y": y, "actual": actual, "shot": str(path), "features": feats})
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", type=int, required=True)
    ap.add_argument("--points", type=int, default=8, help="检查点数(含顶部)")
    ap.add_argument("--settle", type=int, default=900, help="每个检查点停留毫秒")
    ap.add_argument("--viewport", default="desktop", choices=sorted(config.VIEWPORTS))
    ap.add_argument("--out", default=None)
    ap.add_argument("--root", default=str(ROOT), help="实例根目录(含 workspaces/ 与 cloner.db)")
    args = ap.parse_args()

    ws = Path(args.root) / "workspaces" / str(args.task)
    site = json.loads((ws / "site.json").read_text(encoding="utf-8"))
    tasks = None
    url = site.get("target_url") or site.get("url")
    if not url:
        import sqlite3
        con = sqlite3.connect(str(Path(args.root) / "cloner.db"))
        tasks = con.execute("select target_url from clone_tasks where id=?", (args.task,)).fetchone()
        url = tasks[0] if tasks else site["upstream"] + "/"
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    page_path = parts.path or "/"
    out_dir = Path(args.out) if args.out else ws / "dyn_probe"
    out_dir.mkdir(parents=True, exist_ok=True)

    vp = config.VIEWPORTS[args.viewport]
    server = _LocalPreviewServer(ws, use_tape=True)
    server.start()
    clone_url = f"http://127.0.0.1:{server.port}{page_path}"
    t0 = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx_kw = dict(viewport={"width": vp["width"], "height": vp["height"]},
                      device_scale_factor=vp.get("device_scale_factor", 1),
                      user_agent=config.DEFAULT_USER_AGENT, locale="zh-CN")
        ctx_o = browser.new_context(**ctx_kw)
        po = ctx_o.new_page()
        po.goto(url, wait_until="domcontentloaded", timeout=60000)
        po.wait_for_timeout(2500)
        H = po.evaluate("document.documentElement.scrollHeight")
        cap = min(H - vp["height"], 12 * vp["height"])
        positions = [int(cap * k / max(1, args.points - 1)) for k in range(args.points)]
        orig = _walk(po, url, positions, out_dir, "orig", args.settle)
        ctx_c = browser.new_context(**ctx_kw)
        # 克隆只放行本地源(与评分同口径, 外联不回源)
        local = f"http://127.0.0.1:{server.port}"
        ctx_c.route("**/*", lambda r: r.continue_() if r.request.url.startswith((local, "data:", "blob:"))
                    else r.abort())
        pc = ctx_c.new_page()
        clone = _walk(pc, clone_url, positions, out_dir, "clone", args.settle)
        browser.close()
    server.stop()

    report = []
    for i, o in enumerate(orig):
        c = clone[i] if i < len(clone) else {"shot": None, "features": {}}
        if not o.get("shot") or not c.get("shot"):
            # 任一侧卡死: 该点及之后记 0 分(克隆页卡死对用户就是整页不可用)
            report.append({"y": o["y"], "score": 0.0, "orig": o["features"], "clone": c["features"],
                           "hung": True})
            continue
        det = compute_visual_similarity(Path(o["shot"]), Path(c["shot"]))
        report.append({"y": o["y"], "score": det["score"], "orig": o["features"], "clone": c["features"]})
    mean = sum(r["score"] for r in report) / max(1, len(report))
    keys = ["sticky", "stickyVisible", "scrollAnims", "videosVisible", "videosPlaying",
            "canvases", "canvasBlank", "hiddenReveal"]
    print(f"{url}  滚动轨均分 {mean:.1f}  ({len(report)} 点, {time.time() - t0:.0f}s)")
    print("  y      分    " + "  ".join(f"{k}(原/克)" for k in keys))
    for r in report:
        cells = "  ".join(f"{r['orig'].get(k, 0)}/{r['clone'].get(k, 0)}".rjust(len(k) + 5) for k in keys)
        print(f"  {r['y']:<6} {r['score']:5.1f}  {cells}" + ("  [卡死]" if r.get("hung") else ""))
    (out_dir / f"dyn_{args.task}.json").write_text(
        json.dumps({"url": url, "mean": mean, "points": report}, ensure_ascii=False, indent=1),
        encoding="utf-8")


if __name__ == "__main__":
    main()
