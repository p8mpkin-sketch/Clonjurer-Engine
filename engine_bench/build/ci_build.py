"""三平台构建 CI 的驱动脚本(ROADMAP_95.md §3.3, .github/workflows/build.yml 调用)。

样本取 engine_bench/gen_cases/cases.json 的描述用例(确定性路线, 不调模型), 渲染成
Flutter / Electron 工程 + html_css 参考页, 再调用 core.uir.toolchain 构建签名, 最后做启动冒烟:
桌面包带 --remote-debugging-port 启动, 经 CDP 截图, 与同一份 HTML 在 Playwright Chromium 下的
渲染按评分 v3 同口径比较(同为 Chromium, 应接近逐值)。所有子命令都把结果写成 JSON, 便于汇总。

    python engine_bench/build/ci_build.py render  --case d_login --out build_ci
    python engine_bench/build/ci_build.py apk     --app build_ci/flutter/flutter_app --report r.json
    python engine_bench/build/ci_build.py desktop --app build_ci/electron/electron_app --platform win32 --report r.json
    python engine_bench/build/ci_build.py smoke-desktop --report r.json --ref build_ci/web/index.html \
        --shots shots --min-score 90
    python engine_bench/build/ci_build.py summary r1.json r2.json ...   # Markdown 汇总(写入运行摘要)

不含任何按应用/站点分支的逻辑: 用例只决定输入描述。
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

CASES = ROOT / "engine_bench" / "gen_cases" / "cases.json"


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)


def _case(case_id):
    cases = json.loads(CASES.read_text(encoding="utf-8"))["description"]
    for c in cases:
        if c["id"] == case_id:
            return c
    raise SystemExit(f"未知用例 {case_id!r}; 可选: {', '.join(c['id'] for c in cases)}")


def cmd_render(args):
    from core.uir.adapters.description import build_from_description
    from core.uir.targets.electron import ElectronTarget
    from core.uir.targets.flutter import FlutterTarget
    from core.uir.targets.html_css import HtmlCssTarget
    c = _case(args.case)
    w, h = (int(v) for v in args.viewport.lower().split("x"))
    u = build_from_description(c["text"], {"width": w, "height": h})
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "uir.json").write_text(u.to_json(),
                                  encoding="utf-8")
    web = HtmlCssTarget().render(u, out / "web")
    ele = ElectronTarget().render(u, out / "electron")
    flu = FlutterTarget().render(u, out / "flutter")
    _write(out / "render.json", {"case": c["id"], "viewport": [w, h], "web": str(web.entry_file),
                                 "electron": str(out / "electron" / "electron_app"),
                                 "flutter": str(out / "flutter" / "flutter_app"), "window": ele.meta.get("window"),
                                 "flutter_meta": flu.meta})
    return 0


def cmd_apk(args):
    from core.uir import toolchain
    res = toolchain.build_apk(args.app, release=args.release)
    _write(args.report, {"kind": "apk", **res})
    return 0 if res.get("ok") else 1


def cmd_desktop(args):
    from core.uir import toolchain
    plats = [p for p in args.platform.split(",") if p] or None
    res = toolchain.build_desktop(args.app, plats, args.arch or None)
    _write(args.report, {"kind": "desktop", **res})
    return 0 if res.get("ok") else 1


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _launch_binary(art):
    """包目录里的可启动二进制: macOS 取 .app/Contents/MacOS 下的主程序, 其余取 executable。"""
    exe = art.get("executable")
    if exe and exe.endswith(".app"):
        macos = Path(exe) / "Contents" / "MacOS"
        cands = sorted(p for p in macos.iterdir() if p.is_file())
        return str(cands[0]) if cands else None
    return exe


def _kill_tree(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        with suppress(Exception):
            os.killpg(proc.pid, signal.SIGTERM)
        with suppress(Exception):
            proc.wait(10)
        with suppress(Exception):
            os.killpg(proc.pid, signal.SIGKILL)


def _shot_page(page, out_png):
    page.wait_for_load_state("load")
    with suppress(Exception):
        page.evaluate("() => document.fonts && document.fonts.ready")
    page.wait_for_timeout(500)
    page.screenshot(path=str(out_png), full_page=True)
    return page.evaluate("() => ({w: innerWidth, h: innerHeight, text: document.body ? document.body.innerText : ''})")


def smoke_one(art, ref_html, shots, settle_s=3.0, boot_timeout=90):
    """启动一个桌面产物 → CDP 截图 → 与参考 HTML 同视口渲染比较。返回结果字典(不抛异常)。"""
    from playwright.sync_api import sync_playwright
    from core.cloner_engine import compute_visual_similarity
    shots = Path(shots)
    shots.mkdir(parents=True, exist_ok=True)
    tag = f"{art['platform']}-{art['arch']}"
    r = {"platform": art["platform"], "arch": art["arch"], "launched": False, "alive": False}
    binary = _launch_binary(art)
    if not binary:
        r["error"] = "产物里找不到可执行文件"
        return r
    port = _free_port()
    cmd = [binary, f"--remote-debugging-port={port}"]
    if sys.platform.startswith("linux"):
        cmd.append("--no-sandbox")  # 托管 runner 无 suid chrome-sandbox; 只影响 Linux 冒烟
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000200}
    log_path = shots / f"app-{tag}.log"
    t0 = time.monotonic()
    with open(log_path, "wb") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, **kw)
        try:
            ver = None
            while time.monotonic() - t0 < boot_timeout and proc.poll() is None:
                with suppress(Exception):
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
                        ver = json.loads(resp.read().decode())
                        break
                time.sleep(0.5)
            if not ver:
                r["error"] = "启动失败或 CDP 端口未就绪" + (f"(exit {proc.returncode})" if proc.poll() is not None else "")
                return r
            r["launched"] = True
            r["boot_s"] = round(time.monotonic() - t0, 1)
            r["browser"] = ver.get("Browser")
            app_png, ref_png, diff_png = shots / f"app-{tag}.png", shots / f"ref-{tag}.png", shots / f"diff-{tag}.png"
            with sync_playwright() as p:
                b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                page = None
                for _ in range(60):
                    pages = [pg for c in b.contexts for pg in c.pages if pg.url.startswith("file:")]
                    if pages:
                        page = pages[0]
                        break
                    time.sleep(0.5)
                if page is None:
                    r["error"] = "窗口未加载包内页面"
                    return r
                time.sleep(settle_s)
                got = _shot_page(page, app_png)
                r["viewport"] = [got["w"], got["h"]]
                r["alive"] = proc.poll() is None   # 截图完成时应用仍在运行(没有启动即崩)
                with suppress(Exception):
                    b.close()  # 只断开 CDP, 不关应用
                ref_b = p.chromium.launch()
                ctx = ref_b.new_context(viewport={"width": got["w"], "height": got["h"]})
                pg = ctx.new_page()
                pg.goto(Path(ref_html).resolve().as_uri())
                ref = _shot_page(pg, ref_png)
                ref_b.close()
            sc = compute_visual_similarity(ref_png, app_png, diff_png, orig_text=ref["text"], clone_text=got["text"])
            r["score"] = sc.get("score")
            r["detail"] = {k: sc.get(k) for k in ("structure", "edge", "text") if k in sc}
            r["shots"] = [str(app_png), str(ref_png), str(diff_png)]
        except Exception as exc:  # 冒烟失败也要产出报告
            r["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            _kill_tree(proc)
    with suppress(Exception):
        r["app_log_tail"] = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
    return r


def cmd_smoke_desktop(args):
    rep = json.loads(Path(args.report).read_text(encoding="utf-8"))
    plats = [p for p in args.platform.split(",") if p]
    arts = [a for a in rep.get("artifacts", []) if not plats or a["platform"] in plats]
    results = [smoke_one(a, args.ref, args.shots, boot_timeout=args.boot_timeout) for a in arts]
    ok = bool(results) and all(x.get("launched") and x.get("alive") and isinstance(x.get("score"), (int, float))
                               and x["score"] >= args.min_score for x in results)
    _write(args.out or Path(args.shots) / "smoke.json",
           {"kind": "smoke-desktop", "ok": ok, "min_score": args.min_score, "results": results})
    return 0 if ok else 1


def cmd_score(args):
    """APK 冒烟: 模拟器 screencap 与参考 HTML 同视口渲染比较(跨渲染器, 只记录不设门槛)。"""
    from PIL import Image
    from playwright.sync_api import sync_playwright
    from core.cloner_engine import compute_visual_similarity
    shot = Path(args.shot)
    w, h = Image.open(shot).size
    scale = float(args.scale or 1.0)
    vw, vh = max(1, round(w / scale)), max(1, round(h / scale))
    ref_png = shot.with_name("ref-" + shot.name)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_context(viewport={"width": vw, "height": vh}, device_scale_factor=scale).new_page()
        pg.goto(Path(args.ref).resolve().as_uri())
        pg.wait_for_timeout(500)
        pg.screenshot(path=str(ref_png))
        b.close()
    sc = compute_visual_similarity(ref_png, shot, shot.with_name("diff-" + shot.name))
    _write(args.out, {"kind": "score", "shot": str(shot), "ref": str(ref_png), "score": sc.get("score"),
                      "detail": {k: sc.get(k) for k in ("structure", "edge", "text") if k in sc}})
    return 0


def _mb(n):
    return f"{n / 1048576:.1f} MB" if isinstance(n, (int, float)) else "-"


def cmd_summary(args):
    lines = ["| 报告 | 结果 | 产物 | 签名 | 冒烟 |", "|---|---|---|---|---|"]
    for f in args.reports:
        p = Path(f)
        if not p.is_file():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        kind = d.get("kind", "?")
        ok = "✅" if d.get("ok") else "❌ " + str(d.get("gap") or d.get("error") or "")
        if kind == "apk":
            art = f"{Path(d.get('artifact') or '-').name} {_mb(d.get('bytes'))}"
            lines.append(f"| {p.name} | {ok} | {art} | {d.get('signing', '-')} | - |")
        elif kind == "desktop":
            art = "; ".join(f"{a['platform']}-{a['arch']} {_mb(a.get('dir_bytes'))}" for a in d.get("artifacts", []))
            lines.append(f"| {p.name} | {ok} | {art or '-'} | {d.get('signing', '-')} | - |")
        elif kind == "smoke-desktop":
            sm = "; ".join(f"{x['platform']}: " + (f"{x['score']:.1f}" if isinstance(x.get("score"), (int, float))
                                                   else x.get("error", "未出分")) for x in d.get("results", []))
            lines.append(f"| {p.name} | {ok} | - | - | {sm} (门槛 {d.get('min_score')}) |")
        elif kind == "score":
            lines.append(f"| {p.name} | 记录 | - | - | 模拟器截图 vs 参考 HTML: {d.get('score')} |")
    out = "\n".join(lines) + "\n"
    print(out)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(out)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ci_build.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--case", default="d_login")
    r.add_argument("--viewport", default="420x760")
    r.add_argument("--out", required=True)
    a = sub.add_parser("apk")
    a.add_argument("--app", required=True)
    a.add_argument("--release", action="store_true")
    a.add_argument("--report", required=True)
    d = sub.add_parser("desktop")
    d.add_argument("--app", required=True)
    d.add_argument("--platform", default="")
    d.add_argument("--arch", default="")
    d.add_argument("--report", required=True)
    s = sub.add_parser("smoke-desktop")
    s.add_argument("--report", required=True, help="desktop 子命令的报告")
    s.add_argument("--ref", required=True, help="html_css 参考页 index.html")
    s.add_argument("--shots", required=True)
    s.add_argument("--platform", default="")
    s.add_argument("--min-score", type=float, default=90.0)
    s.add_argument("--boot-timeout", type=float, default=90.0)
    s.add_argument("--out", default="")
    c = sub.add_parser("score")
    c.add_argument("--shot", required=True)
    c.add_argument("--ref", required=True)
    c.add_argument("--scale", default="1")
    c.add_argument("--out", required=True)
    m = sub.add_parser("summary")
    m.add_argument("reports", nargs="*")
    args = ap.parse_args(argv)
    return {"render": cmd_render, "apk": cmd_apk, "desktop": cmd_desktop, "smoke-desktop": cmd_smoke_desktop,
            "score": cmd_score, "summary": cmd_summary}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
