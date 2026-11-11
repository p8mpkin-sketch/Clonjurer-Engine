"""交付产物部署首屏实测(按需 / 可选 Playwright)。

把导出的 Flask 工程(exports/<task>.zip 或已解压目录)真实起服务, 用无头浏览器量
首屏加载: 墙钟耗时、FCP、DOMContentLoaded、总字节、请求数、最慢/最大资源、是否回源上游。
`--blackhole` 把上游源站改成不可达地址, 复现"部署机离原站远/断网"的最坏情况 —— 这正是
评分口径量不到的场景(离线渲染对非本地请求瞬时 abort, 交付产物却走反代兜底等满超时)。

用法:
  python3 engine_bench/perf/deploy_load_probe.py exports/562.zip
  python3 engine_bench/perf/deploy_load_probe.py exports/562.zip --blackhole
  python3 engine_bench/perf/deploy_load_probe.py path/to/clone_site_562   # 已解压目录
  python3 engine_bench/perf/deploy_load_probe.py exports/*.zip --throttle broadband --compact  # 批量, 带宽限速

没装 Playwright 时打印跳过并返回 0(与 CI 精简环境一致)。防回归的硬守卫是无浏览器的
tests/test_deploy_perf_guard.py; 本工具是人工复测/取证用。
"""
import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _app_dir(src: Path, work: Path) -> Path:
    if src.is_dir():
        if (src / "app.py").is_file():
            return src
        inner = next((d for d in src.iterdir() if (d / "app.py").is_file()), None)
        if inner:
            return inner
        raise SystemExit(f"目录里找不到 app.py: {src}")
    with zipfile.ZipFile(src) as zf:
        zf.extractall(work)
    cand = next((p.parent for p in work.rglob("app.py")), None)
    if not cand:
        raise SystemExit(f"ZIP 里找不到 app.py: {src}")
    return cand


def _blackhole(app_py: Path):
    txt = app_py.read_text(encoding="utf-8")
    txt = re.sub(r'UPSTREAM_ORIGIN = "[^"]*"',
                 'UPSTREAM_ORIGIN = "http://10.255.255.1"', txt, count=1)
    app_py.write_text(txt, encoding="utf-8")


# 部署机到访客的网络画像(CDP 限速): 本机回环上体积不花时间, 量不出"首屏要搬多少字节"。
# broadband ≈ 家用宽带 / 机房到办公网; 4g ≈ 移动网络。吞吐单位 字节/秒。
THROTTLE = {
    "none": None,
    "broadband": {"latency": 30, "downloadThroughput": 20 * 1024 * 1024 / 8, "uploadThroughput": 5 * 1024 * 1024 / 8},
    "4g": {"latency": 80, "downloadThroughput": 9 * 1024 * 1024 / 8, "uploadThroughput": 2 * 1024 * 1024 / 8},
}

_LCP_JS = """() => new Promise(res => {
  const out = {lcp: null};
  try {
    new PerformanceObserver(l => { for (const e of l.getEntries()) out.lcp = Math.round(e.startTime); })
      .observe({type: 'largest-contentful-paint', buffered: true});
  } catch (e) {}
  setTimeout(() => res(out), 50);
})"""


def _measure(url: str, throttle: str = "none", timeout_s: float = 90.0) -> dict:
    from playwright.sync_api import sync_playwright
    reqs = []
    with sync_playwright() as p:
        br = p.chromium.launch(args=["--no-sandbox"])
        ctx = br.new_context(viewport={"width": 1366, "height": 800})
        page = ctx.new_page()
        origin = urlsplit(url).netloc
        prof = THROTTLE.get(throttle)
        if prof:
            cdp = ctx.new_cdp_session(page)
            cdp.send("Network.enable")
            cdp.send("Network.emulateNetworkConditions", dict(prof, offline=False))
        t0 = time.time()
        loaded = {"t": None}

        def on_done(r):
            try:
                sz = r.sizes()
                n = (sz.get("responseBodySize") or 0) + (sz.get("responseHeadersSize") or 0)
            except Exception:
                n = 0
            try:
                resp = r.response()
                status, proxy = (resp.status, resp.headers.get("x-clone-proxy")) if resp else (0, None)
            except Exception:
                status, proxy = 0, None
            reqs.append({"url": r.url, "host": urlsplit(r.url).netloc, "status": status, "bytes": n,
                         "proxy": proxy, "t": time.time() - t0,
                         "before_load": loaded["t"] is None})

        def on_fail(r):
            reqs.append({"url": r.url, "host": urlsplit(r.url).netloc, "status": 0, "bytes": 0,
                         "proxy": None, "t": time.time() - t0, "before_load": loaded["t"] is None})
        page.on("requestfinished", on_done)
        page.on("requestfailed", on_fail)
        err = None
        try:
            page.goto(url, wait_until="load", timeout=int(timeout_s * 1000))
        except Exception as e:
            err = str(e)[:160]
        loaded["t"] = time.time() - t0
        wall = round(loaded["t"], 2)
        nav = {}
        try:
            nav = page.evaluate(
                "() => {const n=performance.getEntriesByType('navigation')[0]||{};"
                "const p={};performance.getEntriesByType('paint').forEach(e=>p[e.name]=Math.round(e.startTime));"
                "return {dcl: Math.round(n.domContentLoadedEventEnd||0), load: Math.round(n.loadEventEnd||0),"
                " fcp: p['first-contentful-paint']||null, imgs: document.images.length,"
                " html_kb: Math.round((n.encodedBodySize||0)/1024), html_decoded_kb: Math.round((n.decodedBodySize||0)/1024)};}")
            nav.update(page.evaluate(_LCP_JS))
        except Exception as e:
            err = err or str(e)[:160]
        page.wait_for_timeout(800)
        br.close()
    tot = sum(r["bytes"] for r in reqs)
    pre = sum(r["bytes"] for r in reqs if r["before_load"])
    ext = sorted({r["host"] for r in reqs if r["host"] and r["host"] != origin})
    slow = sorted(reqs, key=lambda r: -r["bytes"])[:8]
    return {"wall_load_s": wall, "load_err": err, "nav": nav, "throttle": throttle,
            "requests": len(reqs), "requests_before_load": sum(1 for r in reqs if r["before_load"]),
            "total_mb": round(tot / 1048576, 2), "mb_before_load": round(pre / 1048576, 2),
            "proxied_to_upstream": sum(1 for r in reqs if r.get("proxy")),
            "external_hosts": ext,
            "failed": sum(1 for r in reqs if (r["status"] or 0) >= 400 or not r["status"]),
            "biggest": [{"url": r["url"][-70:], "kb": round(r["bytes"] / 1024, 1)} for r in slow]}


def probe(target: Path, blackhole: bool = False, throttle: str = "none", timeout_s: float = 90.0) -> dict:
    """起交付工程 → 量首屏 → 收摊。返回 _measure 结果(加 mode)。"""
    venv_py = Path(sys.executable)
    work = Path(tempfile.mkdtemp(prefix="deploy_probe_"))
    proc = None
    try:
        app_dir = _app_dir(Path(target), work)
        if blackhole:
            _blackhole(app_dir / "app.py")
        port = _free_port()
        env = {"PORT": str(port), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
        proc = subprocess.Popen([str(venv_py), "app.py"], cwd=app_dir, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}/"
        import urllib.request
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "__healthz", timeout=1).read(1)
                break
            except Exception:
                time.sleep(0.25)
        res = _measure(base, throttle=throttle, timeout_s=timeout_s)
        res["mode"] = "blackhole(上游不可达)" if blackhole else "上游可达"
        return res
    finally:
        if proc:
            proc.terminate()
            with __import__("contextlib").suppress(Exception):
                proc.wait(timeout=5)
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="+", help="exports/<task>.zip 或已解压的 clone_site_* 目录(可多个)")
    ap.add_argument("--blackhole", action="store_true", help="上游置为不可达(最坏情况)")
    ap.add_argument("--throttle", default="none", choices=sorted(THROTTLE), help="访客网络画像")
    ap.add_argument("--timeout", type=float, default=90.0, help="load 事件等待上限(秒)")
    ap.add_argument("--compact", action="store_true", help="每个目标一行 COMPACTDL 摘要(CI 日志尾部用)")
    ap.add_argument("--json", help="写详细结果到文件")
    args = ap.parse_args()

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[skip] 未安装 Playwright, 跳过浏览器实测(CI 精简环境一致)。"
              "防回归守卫见 tests/test_deploy_perf_guard.py")
        return 0

    out = {}
    for t in args.target:
        try:
            res = probe(Path(t), blackhole=args.blackhole, throttle=args.throttle, timeout_s=args.timeout)
        except BaseException as e:  # noqa: BLE001 单个产物起不来不影响其余
            if isinstance(e, KeyboardInterrupt):
                raise
            res = {"error": f"{type(e).__name__}: {e}"[:200]}
        out[t] = res
        if args.compact:
            nav = res.get("nav") or {}
            print("COMPACTDL|{}|load={}|fcp={}|lcp={}|dcl={}|MB={}|MB_pre={}|req={}|req_pre={}|html_kb={}|proxied={}|fail={}|err={}".format(
                Path(t).stem, res.get("wall_load_s"), nav.get("fcp"), nav.get("lcp"), nav.get("dcl"),
                res.get("total_mb"), res.get("mb_before_load"), res.get("requests"),
                res.get("requests_before_load"), nav.get("html_kb"), res.get("proxied_to_upstream"),
                res.get("failed"), (res.get("load_err") or res.get("error") or "")[:60]), flush=True)
            big = res.get("biggest") or []
            if big and (res.get("total_mb") or 0) >= 2:
                print("COMPACTDL-BIG|{}|{}".format(Path(t).stem, " ; ".join(
                    f"{b['url'][-48:]} {b['kb']:.0f}KB" for b in big[:5])), flush=True)
        else:
            print(json.dumps(res, ensure_ascii=False, indent=1))
            dcl = (res.get("nav") or {}).get("dcl") or 0
            if dcl > 5000:
                print(f"\n[WARN] DOMContentLoaded {dcl}ms > 5000ms: 疑有阻塞外链/回源。")
    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
