"""交付产物部署首屏实测(按需 / 可选 Playwright)。

把导出的 Flask 工程(exports/<task>.zip 或已解压目录)真实起服务, 用无头浏览器量
首屏加载: 墙钟耗时、FCP、DOMContentLoaded、总字节、请求数、最慢/最大资源、是否回源上游。
`--blackhole` 把上游源站改成不可达地址, 复现"部署机离原站远/断网"的最坏情况 —— 这正是
评分口径量不到的场景(离线渲染对非本地请求瞬时 abort, 交付产物却走反代兜底等满超时)。

用法:
  python3 engine_bench/perf/deploy_load_probe.py exports/562.zip
  python3 engine_bench/perf/deploy_load_probe.py exports/562.zip --blackhole
  python3 engine_bench/perf/deploy_load_probe.py path/to/clone_site_562   # 已解压目录

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


def _measure(url: str) -> dict:
    from playwright.sync_api import sync_playwright
    reqs = []
    with sync_playwright() as p:
        br = p.chromium.launch(args=["--no-sandbox"])
        page = br.new_context(viewport={"width": 1366, "height": 800}).new_page()
        origin = urlsplit(url).netloc

        def on_resp(r):
            try:
                n = len(r.body())
            except Exception:
                n = 0
            reqs.append({"url": r.url, "host": urlsplit(r.url).netloc,
                         "status": r.status, "bytes": n,
                         "proxy": r.headers.get("x-clone-proxy")})
        page.on("response", on_resp)
        t0 = time.time()
        err = None
        try:
            page.goto(url, wait_until="load", timeout=90000)
        except Exception as e:
            err = str(e)[:160]
        wall = round(time.time() - t0, 2)
        nav = page.evaluate(
            "() => {const n=performance.getEntriesByType('navigation')[0]||{};"
            "const p={};performance.getEntriesByType('paint').forEach(e=>p[e.name]=Math.round(e.startTime));"
            "return {dcl: Math.round(n.domContentLoadedEventEnd||0), load: Math.round(n.loadEventEnd||0),"
            " fcp: p['first-contentful-paint']||null, imgs: document.images.length};}")
        page.wait_for_timeout(800)
        br.close()
    tot = sum(r["bytes"] for r in reqs)
    ext = sorted({r["host"] for r in reqs if r["host"] and r["host"] != origin})
    slow = sorted(reqs, key=lambda r: -r["bytes"])[:8]
    return {"wall_load_s": wall, "load_err": err, "nav": nav,
            "requests": len(reqs), "total_mb": round(tot / 1048576, 2),
            "proxied_to_upstream": sum(1 for r in reqs if r.get("proxy")),
            "external_hosts": ext,
            "failed": sum(1 for r in reqs if (r["status"] or 0) >= 400),
            "biggest": [{"url": r["url"][-70:], "kb": round(r["bytes"] / 1024, 1)} for r in slow]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="exports/<task>.zip 或已解压的 clone_site_* 目录")
    ap.add_argument("--blackhole", action="store_true", help="上游置为不可达(最坏情况)")
    ap.add_argument("--json", help="写详细结果到文件")
    args = ap.parse_args()

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[skip] 未安装 Playwright, 跳过浏览器实测(CI 精简环境一致)。"
              "防回归守卫见 tests/test_deploy_perf_guard.py")
        return 0

    venv_py = Path(sys.executable)
    work = Path(tempfile.mkdtemp(prefix="deploy_probe_"))
    proc = None
    try:
        app_dir = _app_dir(Path(args.target), work)
        if args.blackhole:
            _blackhole(app_dir / "app.py")
        port = _free_port()
        proc = subprocess.Popen([str(venv_py), "app.py"], cwd=app_dir,
                                env={"PORT": str(port), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}/"
        for _ in range(40):
            try:
                import urllib.request
                urllib.request.urlopen(base, timeout=1).read(1)
                break
            except Exception:
                time.sleep(0.25)
        res = _measure(base)
        res["mode"] = "blackhole(上游不可达)" if args.blackhole else "上游可达"
        print(json.dumps(res, ensure_ascii=False, indent=1))
        if args.json:
            Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        # 软门限提示(不作硬失败: 大图体积是另一类问题)
        dcl = res["nav"].get("dcl") or 0
        if dcl > 5000:
            print(f"\n[WARN] DOMContentLoaded {dcl}ms > 5000ms: 疑有阻塞外链/回源。")
    finally:
        if proc:
            proc.terminate()
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
