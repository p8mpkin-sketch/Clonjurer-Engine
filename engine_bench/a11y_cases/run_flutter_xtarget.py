"""跨目标像素评分(ROADMAP_95.md §3.2): a11y 树 → UIR → Flutter 原生工程 → release APK → 模拟器真机渲染截图,
与原应用截图走同一像素 v3 口径。这是 Flutter 目标第一次有真实分数(此前只验证"能构建")。

流程(每例): tree_to_uir(with_shot) → FlutterTarget.render → toolchain.build_apk(release) → adb install -r →
monkey 启动 → 等首帧 → screencap → 裁出与参考同一窗口区域 → 缩到逻辑分辨率 → score_against_reference → 卸载。
同一棵 UIR 也走一遍 html_css(浏览器) 作为对照, 两者之差就是 Flutter 目标自身的版式损失。

只用模拟器(设备分辨率需与采集设备一致), 测试包装完即卸载。
用法: python engine_bench/a11y_cases/run_flutter_xtarget.py [--serial emulator-5554] [tune|holdout ...] [--only name,...]
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from core.uir import toolchain  # noqa: E402
from core.uir.adapters.a11y import tree_to_uir  # noqa: E402
from core.uir.render import score_against_reference  # noqa: E402
from core.uir.targets.flutter import FlutterTarget  # noqa: E402
from core.uir.targets.html_css import HtmlCssTarget  # noqa: E402

HERE = Path(__file__).resolve().parent
PKG = "com.clonjurer.generated.generated_ui"


def adb(serial, *args, binary=False, timeout=120):
    p = subprocess.run(["adb", "-s", serial, *args], capture_output=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f"adb {' '.join(args)} 失败: {p.stderr.decode(errors='replace')[:200]}")
    return p.stdout if binary else p.stdout.decode(errors="replace")


def device_matches(serial, tree) -> bool:
    size = adb(serial, "shell", "wm", "size")
    w, h = (int(x) for x in size.strip().split(":")[-1].strip().split("x"))
    x, y, ww, hh = tree["window"]
    return w >= x + ww and h >= y + hh


def crop_logical(tree, png_in: Path, png_out: Path, app_frame: bool = False):
    """app_frame: 裁被测应用的画面。窗口从屏幕顶边开始(全屏)时与参考同一区域; 否则(对话框等浮动窗口)
    生成的应用是全屏应用, 内容从状态栏下方左上角开始, 按同尺寸从 (0, 状态栏高) 裁。"""
    sc = float(tree.get("scale") or 1.0)
    x, y, w, h = tree["window"]
    if app_frame and y != 0:
        x, y = 0, round(24 * sc)
    lw, lh = round(w / sc), round(h / sc)
    with Image.open(png_in) as im:
        im.convert("RGB").crop((x, y, x + w, y + h)).resize((lw, lh), Image.LANCZOS).save(png_out)
    return lw, lh


def run_case(case: Path, work: Path, serial: str, browser) -> dict:
    tree = json.loads((case / "tree.json").read_text(encoding="utf-8"))
    shot = case / "reference.png"
    work.mkdir(parents=True, exist_ok=True)
    ref = work / "ref.png"
    lw, lh = crop_logical(tree, shot, ref)
    uir = tree_to_uir(tree, shot, embed_images=True)
    out = {"nodes": uir.root.count()}

    # 对照: 同一 UIR 的 html_css 渲染
    r = HtmlCssTarget().render(uir, work / "html", None)
    page = browser.new_page(viewport={"width": lw, "height": lh})
    page.goto(r.entry_file.resolve().as_uri())
    page.wait_for_timeout(300)
    page.screenshot(path=str(work / "html.png"))
    page.close()
    out["html"] = score_against_reference(ref, work / "html.png", work / "html_diff.png").get("score")

    # Flutter: 构建 → 安装 → 截图
    t0 = time.monotonic()
    FlutterTarget().render(uir, work / "fl")
    res = toolchain.build_apk(work / "fl" / "flutter_app", release=True, log=lambda m: None)
    out["build_s"] = round(time.monotonic() - t0, 1)
    if not res.get("ok"):
        out["flutter"] = None
        out["error"] = res.get("gap")
        return out
    try:
        adb(serial, "install", "-r", res["artifact"], timeout=300)
        adb(serial, "shell", "am", "broadcast", "-a", "android.intent.action.CLOSE_SYSTEM_DIALOGS")
        adb(serial, "shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")
        time.sleep(3.0)
        # 模拟器上别的应用的崩溃/系统对话框会盖在前面: 焦点不在被测应用就关对话框重来(最多 3 次)
        for _ in range(3):
            focus = adb(serial, "shell", "dumpsys", "window", "windows")
            cur = next((ln for ln in focus.splitlines() if "mCurrentFocus" in ln), "")
            if PKG in cur:
                break
            adb(serial, "shell", "am", "broadcast", "-a", "android.intent.action.CLOSE_SYSTEM_DIALOGS")
            adb(serial, "shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")
            time.sleep(2.0)
        out["focus_ok"] = PKG in cur
        (work / "device.png").write_bytes(adb(serial, "exec-out", "screencap", "-p", binary=True))
    finally:
        try:
            adb(serial, "uninstall", PKG)
        except Exception:
            pass
    crop_logical(tree, work / "device.png", work / "flutter.png", app_frame=True)
    sc = score_against_reference(ref, work / "flutter.png", work / "flutter_diff.png")
    out.update(flutter=sc.get("score"), structure=sc.get("structure"), edge=sc.get("edge"))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("groups", nargs="*", default=["tune", "holdout"])
    ap.add_argument("--serial", default="emulator-5554")
    ap.add_argument("--only", default="")
    args = ap.parse_args(argv)
    only = {x for x in args.only.split(",") if x}
    from playwright.sync_api import sync_playwright
    stamp = time.strftime("%Y%m%d_%H%M%S")
    work = HERE / "runs" / f"xtarget_{stamp}"
    res = {}
    with sync_playwright() as p:
        br = p.chromium.launch()
        for g in args.groups:
            for case in sorted((HERE / g).iterdir()):
                if not (case / "tree.json").exists() or (only and case.name not in only):
                    continue
                tree = json.loads((case / "tree.json").read_text(encoding="utf-8"))
                if tree.get("platform") != "android" or not device_matches(args.serial, tree):
                    continue
                key = f"{g}/{case.name}"
                try:
                    res[key] = run_case(case, work / g / case.name, args.serial, br)
                except Exception as exc:
                    res[key] = {"flutter": None, "error": f"{type(exc).__name__}: {exc}"[:300]}
                r = res[key]
                print(f"{key:36s} flutter={r.get('flutter')}  html={r.get('html')}  build={r.get('build_s')}s"
                      + (f"  ERR {r['error']}" if r.get("error") else ""), flush=True)
        br.close()
    summary = {}
    for g in args.groups:
        rows = [v for k, v in res.items() if k.startswith(g + "/") and isinstance(v.get("flutter"), (int, float))]
        if rows:
            summary[g] = {"n": len(rows), "flutter": round(sum(r["flutter"] for r in rows) / len(rows), 1),
                          "html": round(sum(r["html"] for r in rows) / len(rows), 1),
                          "min_flutter": min(r["flutter"] for r in rows),
                          "build_s_mean": round(sum(r["build_s"] for r in rows) / len(rows), 1)}
    print(json.dumps(summary, ensure_ascii=False))
    (HERE / "runs").mkdir(exist_ok=True)
    (HERE / "runs" / f"xtarget_{stamp}.json").write_text(
        json.dumps({"summary": summary, "cases": res}, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


if __name__ == "__main__":
    main()
