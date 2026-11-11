"""CI 上采集 a11y 样本(统一树 JSON + 同次截图) → engine_bench/a11y_cases/<tune|holdout>/<用例名>/。

  python engine_bench/a11y_cases/capture_ci.py windows   # GitHub windows runner(交互会话, 需 uiautomation + pillow)
  python engine_bench/a11y_cases/capture_ci.py android   # 模拟器已就绪(adb 在 PATH), AOSP 镜像

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
    from PIL import ImageGrab

    pf = Path(r"C:\Program Files")
    ps = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    apps = [
        ("tune", "win_form_login", ps + [str(HERE / "apps" / "win" / "form_login.ps1")], None),
        ("tune", "win_form_list", ps + [str(HERE / "apps" / "win" / "form_list.ps1")], None),
        ("holdout", "win_7zip_fm", [str(pf / "7-Zip" / "7zFM.exe"), str(ROOT / "engine_bench")], (0, 0, 900, 640)),
        ("holdout", "win_notepadpp", [str(pf / "Notepad++" / "notepad++.exe"), "-multiInst", "-nosession", str(ROOT / "requirements.txt")],
         (0, 0, 900, 640)),
    ]
    user32 = ctypes.windll.user32
    ok = 0
    for group, name, cmd, rect in apps:
        if not Path(cmd[0]).exists() and cmd[0] != "powershell":
            print(f"跳过 {name}: 未安装 {cmd[0]}")
            continue
        proc = subprocess.Popen(cmd)
        try:
            win = None
            for _ in range(40):
                time.sleep(0.5)
                w = auto.WindowControl(searchDepth=1, ProcessId=proc.pid)
                if w.Exists(0, 0) and w.BoundingRectangle.width() > 100:
                    win = w
                    break
            if win is None:
                print(f"跳过 {name}: 20s 内没有出现顶层窗口")
                continue
            hwnd = win.NativeWindowHandle
            if rect:
                user32.MoveWindow(hwnd, rect[0], rect[1], rect[2], rect[3], True)
            with_suppress(lambda: win.SetActive())
            with_suppress(lambda: user32.SetForegroundWindow(hwnd))
            time.sleep(2.0)
            tree = a11y.capture_windows(pid=proc.pid)
            _save(group, name, tree, image=ImageGrab.grab())
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


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):   # Windows runner 的控制台是 cp1252, 中文日志会抛 UnicodeEncodeError
        with_suppress(lambda: _s.reconfigure(encoding="utf-8", errors="replace"))
    which = sys.argv[1] if len(sys.argv) > 1 else ""
    n = {"windows": windows, "android": android}[which]()
    print(f"共采集 {n} 例")
