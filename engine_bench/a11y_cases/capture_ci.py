"""CI 上采集 a11y 样本(统一树 JSON + 同次截图) → engine_bench/a11y_cases/<tune|holdout>/<用例名>/。

  python engine_bench/a11y_cases/capture_ci.py windows   # GitHub windows runner(交互会话, 需 uiautomation + pillow)
  python engine_bench/a11y_cases/capture_ci.py android   # 模拟器已就绪(adb 在 PATH), AOSP 镜像
  python engine_bench/a11y_cases/capture_ci.py macos     # GitHub macOS runner(需 pyobjc; 辅助功能/屏幕录制由 runner 镜像预授权)

样本来源只有两类: 开源应用(AOSP 自带应用 / 7-Zip / Notepad++)与本仓库自建的合成应用(apps/)。
留出(holdout)样本采下来只评分, 不据其改适配器; 调参只看 tune 与自建合成应用。
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent

from core.uir.adapters import a11y  # noqa: E402


def _save(group: str, name: str, tree: dict, png_bytes: bytes = None, image=None):
    d = HERE / group / name
    if (d / "tree.json").exists() and "--refresh" not in sys.argv:
        # 已入库的样本不覆盖: 留出样本一旦采下就冻结(重采会换一帧截图, 分数跟着漂)
        print(f"已有 {group}/{name}, 不覆盖(加 --refresh 才重采)")
        return
    d.mkdir(parents=True, exist_ok=True)
    tree.pop("screenshot", None)
    (d / "tree.json").write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
    if png_bytes is not None:
        (d / "reference.png").write_bytes(png_bytes)
    elif image is not None:
        image.save(d / "reference.png")
    print(f"采集 {group}/{name}: 平台 {tree.get('platform')} 应用 {tree.get('app')!r} 窗口 {tree.get('window')} 节点 {a11y._count(tree['root'])}")


# ---------------------------------------------------------------------------
def windows():
    import ctypes
    import uiautomation as auto
    from PIL import Image, ImageGrab

    pf = Path(r"C:\Program Files")
    ps = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    apps = [
        ("tune", "win_form_login", ps + [str(HERE / "apps" / "win" / "form_login.ps1")], None),
        ("tune", "win_form_list", ps + [str(HERE / "apps" / "win" / "form_list.ps1")], None),
        ("tune", "win_form_explorer", ps + [str(HERE / "apps" / "win" / "form_explorer.ps1")], None),
        ("tune", "win_form_editor", ps + [str(HERE / "apps" / "win" / "form_editor.ps1")], None),
        ("tune", "win_form_codepad", ps + [str(HERE / "apps" / "win" / "form_codepad.ps1")], None),
        ("holdout", "win_7zip_fm", [str(pf / "7-Zip" / "7zFM.exe"), str(ROOT / "engine_bench")], (0, 0, 900, 640)),
        ("holdout", "win_notepadpp", [str(pf / "Notepad++" / "notepad++.exe"), "-multiInst", "-nosession", str(ROOT / "requirements.txt")],
         (0, 0, 900, 640)),
    ]
    user32 = ctypes.windll.user32
    ok = 0

    def app_window(pid):
        for c in auto.GetRootControl().GetChildren():
            if c.ProcessId == pid and c.ClassName not in a11y._WIN_CONSOLE_CLASSES and c.BoundingRectangle.width() > 100:
                return c
        return None

    # runner 自己的控制台/终端窗口盖在桌面最上层: 先最小化, 免得挡住被采应用、也不让它进截图
    for c in auto.GetRootControl().GetChildren():
        if c.ClassName in a11y._WIN_CONSOLE_CLASSES:
            with_suppress(lambda c=c: user32.ShowWindow(c.NativeWindowHandle, 6))
    for group, name, cmd, rect in apps:
        if not Path(cmd[0]).exists() and cmd[0] != "powershell":
            print(f"跳过 {name}: 未安装 {cmd[0]}")
            continue
        proc = subprocess.Popen(cmd)
        try:
            win = None
            for _ in range(40):
                time.sleep(0.5)
                win = app_window(proc.pid)
                if win is not None:
                    break
            if win is None:
                print(f"跳过 {name}: 20s 内没有出现顶层窗口")
                continue
            hwnd = win.NativeWindowHandle
            if rect:
                user32.MoveWindow(hwnd, rect[0], rect[1], rect[2], rect[3], True)
            with_suppress(lambda: win.SetActive())
            with_suppress(lambda: user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0003))   # 置顶, 不动位置大小
            with_suppress(lambda: user32.SetForegroundWindow(hwnd))
            time.sleep(2.0)
            tree = a11y.capture_windows(pid=proc.pid)
            # 截图只留窗口矩形内的像素(窗口外的桌面与其他窗口涂黑, 不入库)
            x, y, w, h = [int(v) for v in tree["window"]]
            full = ImageGrab.grab()
            shot = Image.new("RGB", full.size, (0, 0, 0))
            shot.paste(full.crop((x, y, x + w, y + h)), (x, y))
            _save(group, name, tree, image=shot)
            ok += 1
        except Exception as exc:   # noqa: BLE001 单个应用采不到不影响其余
            print(f"失败 {name}: {type(exc).__name__}: {str(exc)[:300]}")
        finally:
            with_suppress(proc.kill)
            time.sleep(1.0)
    return ok


def with_suppress(fn):
    try:
        return fn()
    except Exception:   # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
ANDROID_SCREENS = [
    # (分组, 用例名, am start 参数)。按类别意图启动, 不写死包名(各版本 AOSP 镜像包名不同)
    ("holdout", "android_clock", ["-a", "android.intent.action.SHOW_ALARMS"]),
    ("holdout", "android_calendar", ["-a", "android.intent.action.MAIN", "-c", "android.intent.category.APP_CALENDAR"]),
    ("holdout", "android_messaging", ["-a", "android.intent.action.MAIN", "-c", "android.intent.category.APP_MESSAGING"]),
    ("holdout", "android_gallery", ["-a", "android.intent.action.MAIN", "-c", "android.intent.category.APP_GALLERY"]),
    ("holdout", "android_browser", ["-a", "android.intent.action.MAIN", "-c", "android.intent.category.APP_BROWSER"]),
    ("holdout", "android_email", ["-a", "android.intent.action.MAIN", "-c", "android.intent.category.APP_EMAIL"]),
]
_NOT_APP = ("launcher", "permissioncontroller", "systemui", "resolver", "packageinstaller", "android")


def android(serial: str = "emulator-5554"):
    def sh(*args):
        return a11y._adb(serial, "shell", *args, timeout=40)

    ok = 0
    tmp = HERE / "runs" / "capture_tmp"
    for group, name, start in ANDROID_SCREENS:
        try:
            sh("input", "keyevent", "KEYCODE_HOME")
            out = sh("am", "start", "-W", *start)
            if "Error" in out or "unable to resolve" in out.lower():
                print(f"跳过 {name}: 镜像里没有对应应用 ({out.strip().splitlines()[-1][:120]})")
                continue
            time.sleep(6.0)
            tree = a11y.capture_android(serial, out_dir=tmp)
            app = str(tree.get("app") or "")
            # 前台必须是应用自己的界面: 包名形如 a.b.c; 系统弹窗(兼容性提示/权限框)、桌面、选择器都不算
            if "." not in app or app == "android" or any(k in app for k in _NOT_APP[:5]):
                print(f"跳过 {name}: 前台不是应用界面({app!r})")
                continue
            shot = Path(tree.get("screenshot") or "")
            if not shot.exists():
                print(f"跳过 {name}: 没截到图")
                continue
            _save(group, name, tree, png_bytes=shot.read_bytes())
            ok += 1
        except Exception as exc:   # noqa: BLE001
            print(f"失败 {name}: {type(exc).__name__}: {str(exc)[:300]}")
    return ok


def macos():
    """GitHub macOS runner: 自建合成应用(pyobjc)+ 开源应用(brew cask 装的), AX 树 + screencapture 窗口区域截图。"""
    import ApplicationServices as AS
    from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication, NSWorkspace

    print(f"辅助功能授权: {bool(AS.AXIsProcessTrusted())}")
    apps = [
        ("tune", "mac_form_prefs", [sys.executable, str(HERE / "apps" / "mac" / "form_prefs.py")], None),
        ("holdout", "mac_platypus", ["open", "-a", "Platypus"], "Platypus"),
        ("holdout", "mac_coteditor", ["open", "-a", "CotEditor"], "CotEditor"),
    ]
    ok = 0
    tmp = HERE / "runs" / "capture_tmp"

    def pid_of(name):
        for ra in NSWorkspace.sharedWorkspace().runningApplications():
            if str(ra.localizedName() or "") == name:
                return int(ra.processIdentifier())
        return None

    for group, name, cmd, app_name in apps:
        proc = None
        try:
            errf = open(tmp / f"{name}.err", "w+") if tmp.mkdir(parents=True, exist_ok=True) is None else None
            proc = subprocess.Popen(cmd, stderr=errf)
            tree, last = None, ""
            for _ in range(20):
                time.sleep(1.0)
                pid = pid_of(app_name) if app_name else proc.pid
                if not pid:
                    continue
                with_suppress(lambda: NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                              .activateWithOptions_(NSApplicationActivateIgnoringOtherApps))
                try:
                    t = a11y.capture_macos(pid, out_dir=tmp)
                except Exception as exc:   # noqa: BLE001
                    last = f"{type(exc).__name__}: {str(exc)[:160]}"
                    continue
                last = f"窗口 {t['window']} 节点 {a11y._count(t['root'])} 根 {t['root'].get('role')}"
                if t["window"][2] >= 200 and t["window"][3] >= 120 and a11y._count(t["root"]) >= 6:
                    time.sleep(2.0)     # 窗口动画落定后重采一次
                    tree = a11y.capture_macos(pid, out_dir=tmp)
                    break
            if tree is None:
                errf.seek(0)
                print(f"跳过 {name}: 20s 内没采到应用窗口; {last}; 进程退出码 {proc.poll()}; stderr: {errf.read()[-600:]!r}")
                continue
            shot = Path(tree.get("screenshot") or "")
            if not shot.exists():
                print(f"跳过 {name}: 没截到图(屏幕录制未授权?)")
                continue
            _save(group, name, tree, png_bytes=shot.read_bytes())
            ok += 1
        except Exception as exc:   # noqa: BLE001
            print(f"失败 {name}: {type(exc).__name__}: {str(exc)[:300]}")
        finally:
            if app_name:
                with_suppress(lambda: subprocess.run(["pkill", "-x", app_name], timeout=10))
            elif proc is not None:
                with_suppress(proc.kill)
            time.sleep(1.0)
    return ok


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):   # Windows runner 的控制台是 cp1252, 中文日志会抛 UnicodeEncodeError
        with_suppress(lambda: _s.reconfigure(encoding="utf-8", errors="replace"))
    which = sys.argv[1] if len(sys.argv) > 1 else ""
    n = {"windows": windows, "android": android, "macos": macos}[which]()
    print(f"共采集 {n} 例")
