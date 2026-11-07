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
    # 真机口径: 移动 UA + 触屏 + DPR + 视口 meta 生效(is_mobile)。服务端按 UA 分发移动版、
    # (hover:none)/(pointer:coarse) 媒体查询、JS 嗅探 UA 的站, 只有这样才与真机看到的一致
    "iphone-air": {"width": 420, "height": 912},
    "ipad-air": {"width": 820, "height": 1180},
}
IOS_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1")
# iPadOS 13 起 Safari 默认「桌面级浏览」: 发 Mac Safari UA(不含 iPad/Mobile), 靠触屏区分
IPAD_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
           "(KHTML, like Gecko) Version/18.6 Safari/605.1.15")
DEVICE_OPTS = {
    "iphone-air": {"user_agent": IOS_UA, "device_scale_factor": 3, "is_mobile": True, "has_touch": True},
    "ipad-air": {"user_agent": IPAD_UA, "device_scale_factor": 2, "is_mobile": True, "has_touch": True},
}


def context_opts(name):
    """视口名 -> new_context 参数(桌面口径: 桌面 UA、DPR 1; 真机口径见 DEVICE_OPTS)。"""
    opts = {"viewport": PROBE_VIEWPORTS[name], "user_agent": UA, "device_scale_factor": 1}
    opts.update(DEVICE_OPTS.get(name, {}))
    return opts
MAX_SCREENS = 8          # 每侧最多截前 N 屏: 长页尾部(推荐流/页脚)对适配判断信息量低
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 两侧同口径: 停动画/过渡, 滚动一遍触发懒加载, 回顶
SETTLE_CSS = ("*,*::before,*::after{animation-play-state:paused!important;"
              "transition:none!important;caret-color:transparent!important}")
# 每步等 320ms(与引擎采集滚动同节奏): 无限推荐流在快滚下来不及续页, 原站只量到首屏长度,
# 而用户往下滑时流会续上(克隆里冻结的就是续上后的样子), 快滚会把这当成克隆"多出来"的内容
SCROLL_JS = r"""
async (maxH) => {
  const h = () => Math.min(document.documentElement.scrollHeight, maxH);
  for (let y = 0; y < h(); y += innerHeight * 0.8) { scrollTo(0, y); await new Promise(r => setTimeout(r, 320)); }
  scrollTo(0, 0); await new Promise(r => setTimeout(r, 400));
  return document.documentElement.scrollWidth;
}
"""
# 截图区内的图全部转 eager 并等到齐(封顶), 视频停帧, 字体就绪: 两侧同口径, 减少"一侧图没到"的噪声
SETTLE_MEDIA_JS = r"""
async ([maxH, capMs]) => {
  const t0 = performance.now();
  try { document.querySelectorAll('video').forEach(v => { try { v.pause(); } catch (e) {} }); } catch (e) {}
  const inArea = im => { const r = im.getBoundingClientRect(); return r.width > 1 && r.height > 1 && r.top + scrollY < maxH; };
  for (const im of document.images) if (im.loading === 'lazy' && inArea(im)) im.loading = 'eager';
  try { await Promise.race([document.fonts.ready, new Promise(r => setTimeout(r, capMs))]); } catch (e) {}
  while (performance.now() - t0 < capMs) {
    const pending = Array.from(document.images).filter(im => !im.complete && inArea(im)).length;
    if (!pending) break;
    await new Promise(r => setTimeout(r, 150));
  }
  return Math.round(performance.now() - t0);
}
"""


def _shoot(ctx, url, vp, out: Path, timeout_ms=45000):
    page = ctx.new_page()
    info = {"url": url}
    try:
        for attempt in range(4):   # 网络层抖动(RESET/CLOSED/断网)退避重试, 不当成克隆缺陷
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                break
            except Exception as exc:   # noqa: BLE001
                if attempt == 3 or "net::ERR_" not in str(exc):
                    raise
                time.sleep(2 * (attempt + 1))
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=8000)
        with suppress(Exception):
            page.add_style_tag(content=SETTLE_CSS)
        max_h = vp["height"] * MAX_SCREENS
        sw = page.evaluate(SCROLL_JS, max_h)
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=4000)
        with suppress(Exception):
            page.evaluate(SETTLE_MEDIA_JS, [max_h, 6000])
        with suppress(Exception):
            from core.cloner_engine import AD_RECTS_JS
            info["ads"] = page.evaluate(AD_RECTS_JS) or []
        info["scroll_width"] = sw
        info["overflow_x"] = sw > vp["width"] + 2
        full_h = page.evaluate("document.documentElement.scrollHeight")
        info["height"] = full_h
        clip_h = min(full_h, max_h)
        page.screenshot(path=str(out), full_page=True, animations="disabled", scale="css",
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


def probe_one(browser, base, task_id, url, viewports, outdir: Path, clone_tpl=None, noise=False):
    from core.cloner_engine import compute_visual_similarity
    res = {"task_id": task_id, "url": url}
    for name in viewports:
        vp = PROBE_VIEWPORTS[name]
        ctx = browser.new_context(**context_opts(name))
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
            rects = (ia.get("ads") or []) + (ib.get("ads") or [])
            if rects:   # 第三方竞价广告位两侧统一遮灰(与引擎评分同口径)
                from core.cloner_engine import _mask_rects
                _mask_rects(a, rects)
                _mask_rects(b, rects)
            sim = compute_visual_similarity(a, b, outdir / f"{task_id}_{name}_diff.png",
                                            orig_text=ia.get("text", ""), clone_text=ib.get("text", ""))
            row.update({k: sim.get(k) for k in ("score", "structure", "edge", "text", "height_ratio")})
            if noise:
                # 原站自比: 隔一次重新加载原站再比, 得到本口径下的可达上限(活体随机性)
                ctx2 = browser.new_context(**context_opts(name))
                try:
                    a2 = outdir / f"{task_id}_{name}_orig2.png"
                    i2 = _shoot(ctx2, url, vp, a2)
                finally:
                    ctx2.close()
                if i2.get("ok"):
                    if rects or i2.get("ads"):
                        from core.cloner_engine import _mask_rects
                        _mask_rects(a2, rects + (i2.get("ads") or []))
                    row["ceiling"] = compute_visual_similarity(
                        a, a2, None, orig_text=ia.get("text", ""), clone_text=i2.get("text", "")).get("score")
        else:
            row["error"] = ia.get("error") or ib.get("error")
        res[name] = row
    return res


def probe_tasks(base, tasks, viewports=("desktop", "tablet", "mobile"), outdir=None, workers=3,
                clone_tpl=None, noise=False):
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
                    r = probe_one(browser, base, tid, url, viewports, outdir, clone_tpl, noise)
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
    ap.add_argument("--noise", action="store_true", help="另测原站自比上限(每视口多一次原站加载)")
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
                              args.clone_url or None, args.noise)
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
            if c.get("ceiling") is not None:
                s = f"{s} /{c['ceiling']}"
            cells.append(str(s))
        print(f"| {tid} | {url} | " + " | ".join(cells) + " |")
    (outdir / "probe.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
