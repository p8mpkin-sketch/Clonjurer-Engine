"""OutputTarget 产物 → 可分发二进制的显式构建步骤(APK / 桌面 .app / .exe)。

render() 只做纯代码生成; 构建依赖仓库外的工具链(Flutter SDK + Android SDK、Node + Electron),
这里只负责探测与调用, 缺工具链时返回 {"ok": False, "gap": ...} 而不是抛异常, 便于上层诚实报告。

    python -m core.uir.toolchain apk <flutter_app 目录> [--release]
    python -m core.uir.toolchain desktop <electron_app 目录> [--platform darwin,win32] [--arch arm64]
"""
import argparse
import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

_FLUTTER_CANDIDATES = ("/opt/homebrew/bin/flutter", "/usr/local/bin/flutter",
                       "~/flutter/bin/flutter", "~/development/flutter/bin/flutter")
_ANDROID_CANDIDATES = ("~/Library/Android/sdk", "/opt/homebrew/share/android-commandlinetools",
                       "/usr/local/share/android-commandlinetools", "~/Android/Sdk")


def _which(name, candidates=()):
    p = shutil.which(name)
    if p:
        return p
    for c in candidates:
        c = os.path.expanduser(c)
        if os.access(c, os.X_OK):
            return c
    return None


def find_android_sdk():
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        v = os.environ.get(env)
        if v and Path(v, "platforms").is_dir():
            return v
    for c in _ANDROID_CANDIDATES:
        c = os.path.expanduser(c)
        if Path(c, "platforms").is_dir():
            return c
    return None


def _run(cmd, cwd, env, log, timeout):
    log(f"$ {' '.join(cmd)}")
    t = time.monotonic()
    p = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout)
    tail = (p.stdout + p.stderr).strip().splitlines()[-15:]
    log(f"  → exit {p.returncode} ({time.monotonic() - t:.0f}s)")
    return p.returncode, "\n".join(tail)


def _print(msg):
    print(msg, flush=True)


def host_platform() -> str:
    return {"Darwin": "darwin", "Windows": "win32"}.get(_platform.system(), "linux")


def zip_dir(src, dest) -> Path:
    """把构建目录压成 zip, 保留符号链接(macOS .app 的 Framework 依赖 Versions/Current 链接, 展开会损坏签名结构)。"""
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    z = shutil.which("zip")
    if z:
        subprocess.run([z, "-qry", str(dest), src.name], cwd=str(src.parent), check=True)
    else:  # pragma: no cover - 无 zip 命令时退化(符号链接会被展开)
        shutil.make_archive(str(dest.with_suffix("")), "zip", str(src.parent), src.name)
    return dest


def _patch_android(app_dir: Path, log):
    """按工程里的 clonjurer_android.json 补 Android 清单: 应用名、联网权限、只对指定主机放行明文 http。"""
    spec_f = app_dir / "clonjurer_android.json"
    man = app_dir / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    if not spec_f.is_file() or not man.is_file():
        return
    spec = json.loads(spec_f.read_text(encoding="utf-8"))
    xml = man.read_text(encoding="utf-8")
    if spec.get("label"):
        lab = (str(spec["label"]).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;"))
        xml = re.sub(r'android:label="[^"]*"', f'android:label="{lab}"', xml, count=1)
    if spec.get("internet") and "android.permission.INTERNET" not in xml:
        xml = xml.replace("<application", '<uses-permission android:name="android.permission.INTERNET"/>\n    <application', 1)
    hosts = [h for h in spec.get("cleartext_hosts") or [] if re.match(r"^[A-Za-z0-9.-]+$", str(h))]
    if hosts and "networkSecurityConfig" not in xml:
        xml = xml.replace("<application", '<application android:networkSecurityConfig="@xml/network_security_config"', 1)
        res = app_dir / "android" / "app" / "src" / "main" / "res" / "xml"
        res.mkdir(parents=True, exist_ok=True)
        doms = "".join(f'<domain includeSubdomains="false">{h}</domain>' for h in hosts)
        (res / "network_security_config.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<network-security-config>'
            f'<domain-config cleartextTrafficPermitted="true">{doms}</domain-config></network-security-config>\n',
            encoding="utf-8")
    man.write_text(xml, encoding="utf-8")
    log("  已按 clonjurer_android.json 补 Android 清单(应用名/联网权限/明文主机)")


def build_apk(app_dir, release=False, log=_print, timeout=1800, org="com.clonjurer.generated") -> dict:
    """Flutter 工程(FlutterTarget 产物)→ APK。补齐 android/ 平台骨架时不覆盖已生成的 lib/。"""
    app_dir = Path(app_dir)
    flutter = _which("flutter", _FLUTTER_CANDIDATES)
    sdk = find_android_sdk()
    missing = [n for n, v in (("Flutter SDK", flutter), ("Android SDK", sdk), ("JDK", _which("java"))) if not v]
    if missing:
        return {"ok": False, "gap": "缺工具链: " + ", ".join(missing)}
    env = dict(os.environ, ANDROID_HOME=sdk, ANDROID_SDK_ROOT=sdk)
    if not (app_dir / "android").is_dir():
        # flutter create 对已存在的 lib/main.dart、pubspec.yaml 不覆盖(无 --overwrite)
        rc, out = _run([flutter, "create", ".", "--platforms=android", "--project-name", "generated_ui",
                        "--org", org], app_dir, env, log, timeout)
        if rc:
            return {"ok": False, "gap": "flutter create 失败", "log": out}
    _patch_android(app_dir, log)
    tpl = app_dir / "test" / "widget_test.dart"
    if tpl.is_file() and "MyApp" in tpl.read_text(encoding="utf-8", errors="ignore") and \
            "class MyApp" not in "".join(p.read_text(encoding="utf-8", errors="ignore") for p in (app_dir / "lib").glob("*.dart")):
        tpl.unlink()   # flutter create 模板测试引用不存在的 MyApp, 留着只会让 analyze/test 误报
    rc, out = _run([flutter, "pub", "get"], app_dir, env, log, timeout)
    if rc:
        return {"ok": False, "gap": "flutter pub get 失败(网络/依赖)", "log": out}
    rc, analyze = _run([flutter, "analyze", "--no-fatal-infos", "--no-fatal-warnings"], app_dir, env, log, timeout)
    rc_t, test = _run([flutter, "test"], app_dir, env, log, timeout) if (app_dir / "test").is_dir() else (None, "")
    mode = "release" if release else "debug"
    rc_b, out = _run([flutter, "build", "apk", f"--{mode}"], app_dir, env, log, timeout)
    apk = app_dir / "build" / "app" / "outputs" / "flutter-apk" / f"app-{mode}.apk"
    if rc_b or not apk.is_file():
        return {"ok": False, "gap": f"flutter build apk --{mode} 失败", "log": out}
    signing = "debug keystore(Flutter 模板默认)"  # 正式发布需自备 keystore + key.properties
    return {"ok": True, "artifact": str(apk), "bytes": apk.stat().st_size, "mode": mode,
            "analyze_ok": rc == 0, "analyze_tail": analyze, "test_ok": (rc_t == 0) if rc_t is not None else None,
            "test_tail": test, "signing": signing}


def _latest(parent: Path, pattern: str):
    found = sorted(parent.glob(pattern), key=lambda p: [int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", p.name.split("-")[-1])])
    return found[-1] if found else None


def build_android_shell(app_dir, log=_print, timeout=600, min_sdk=24) -> dict:
    """原生 Android 壳(app_shell.android_shell 产物)→ 签名 APK。不走 Gradle/Flutter:
    aapt2 link(清单+assets)→ javac → d8 → 并入 classes.dex → zipalign → apksigner。只需 Android SDK(build-tools + platform)与 JDK。"""
    import zipfile
    app_dir = Path(app_dir)
    sdk = find_android_sdk()
    javac = _which("javac")
    if not sdk or not javac:
        return {"ok": False, "gap": "缺工具链: " + ", ".join(n for n, v in (("Android SDK", sdk), ("JDK(javac)", javac)) if not v)}
    bt = _latest(Path(sdk) / "build-tools", "*")
    plat = _latest(Path(sdk) / "platforms", "android-*")
    if not bt or not plat or not (plat / "android.jar").is_file():
        return {"ok": False, "gap": "缺工具链: Android build-tools / platform(android.jar)"}
    tool = {n: str(bt / n) for n in ("aapt2", "d8", "zipalign", "apksigner")}
    if not all(os.access(v, os.X_OK) for v in tool.values()):
        return {"ok": False, "gap": f"build-tools {bt.name} 不完整"}
    jar = str(plat / "android.jar")
    target_sdk = plat.name.split("-")[-1]
    build = app_dir / "build"
    if build.exists():
        shutil.rmtree(build)
    (build / "classes").mkdir(parents=True)
    (build / "dex").mkdir()
    env = os.environ.copy()
    base, aligned, final = build / "base.apk", build / "aligned.apk", build / "app-release.apk"
    steps = [
        [tool["aapt2"], "link", "-o", str(base), "--manifest", str(app_dir / "AndroidManifest.xml"), "-I", jar,
         "-A", str(app_dir / "assets"), "--min-sdk-version", str(min_sdk), "--target-sdk-version", target_sdk],
        [javac, "--release", "11", "-nowarn", "-encoding", "UTF-8", "-classpath", jar, "-d", str(build / "classes")]
        + [str(p) for p in (app_dir / "src").rglob("*.java")],
    ]
    for cmd in steps:
        rc, out = _run(cmd, app_dir, env, log, timeout)
        if rc:
            return {"ok": False, "gap": f"{Path(cmd[0]).name} 失败", "log": out}
    rc, out = _run([tool["d8"], "--release", "--min-api", str(min_sdk), "--lib", jar, "--output", str(build / "dex")]
                   + [str(p) for p in (build / "classes").rglob("*.class")], app_dir, env, log, timeout)
    if rc:
        return {"ok": False, "gap": "d8 失败", "log": out}
    with zipfile.ZipFile(base, "a", zipfile.ZIP_DEFLATED) as z:
        z.write(build / "dex" / "classes.dex", "classes.dex")
    rc, out = _run([tool["zipalign"], "-p", "-f", "4", str(base), str(aligned)], app_dir, env, log, timeout)
    if rc:
        return {"ok": False, "gap": "zipalign 失败", "log": out}
    ks = Path.home() / ".android" / "debug.keystore"
    if not ks.is_file():
        keytool = _which("keytool")
        if not keytool:
            return {"ok": False, "gap": "缺 debug keystore 且无 keytool"}
        ks.parent.mkdir(parents=True, exist_ok=True)
        rc, out = _run([keytool, "-genkeypair", "-keystore", str(ks), "-storepass", "android", "-keypass", "android",
                        "-alias", "androiddebugkey", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
                        "-dname", "CN=Android Debug,O=Android,C=US"], app_dir, env, log, timeout)
        if rc:
            return {"ok": False, "gap": "生成 debug keystore 失败", "log": out}
    # Android 公开约定的 debug keystore 口令(android), 不是用户凭据; 正式发布需自备 keystore
    rc, out = _run([tool["apksigner"], "sign", "--ks", str(ks), "--ks-pass", "pass:android", "--key-pass", "pass:android",
                    "--ks-key-alias", "androiddebugkey", "--out", str(final), str(aligned)], app_dir, env, log, timeout)
    if rc or not final.is_file():
        return {"ok": False, "gap": "apksigner 签名失败", "log": out}
    return {"ok": True, "artifact": str(final), "bytes": final.stat().st_size, "mode": "release",
            "signing": "Android debug keystore", "build_tools": bt.name, "target_sdk": target_sdk}


_INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>{exe}</string>
<key>CFBundleIdentifier</key><string>{bid}</string>
<key>CFBundleName</key><string>{name}</string>
<key>CFBundleDisplayName</key><string>{name}</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1.0</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>11.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
"""


def build_go_desktop(app_dir, platforms=None, log=_print, timeout=1800) -> dict:
    """Go 桌面壳(app_shell.desktop_shell 产物)→ macOS .app(本机 cgo 构建) / Windows .exe(纯 Go 交叉编译)。"""
    from xml.sax.saxutils import escape
    app_dir = Path(app_dir)
    go = _which("go", ("/opt/homebrew/bin/go", "/usr/local/go/bin/go"))
    if not go:
        return {"ok": False, "gap": "缺工具链: Go"}
    spec = json.loads((app_dir / "shell.json").read_text(encoding="utf-8"))
    name, title, bid = spec["name"], spec["title"], spec["bundle_id"]
    host = host_platform()
    platforms = platforms or [host]
    env = dict(os.environ, GOFLAGS="-mod=mod")
    rc, out = _run([go, "mod", "tidy"], app_dir, env, log, timeout)
    if rc:
        return {"ok": False, "gap": "go mod tidy 失败(网络/代理)", "log": out}
    built, fails = [], {}
    for plat in platforms:
        arch = ("arm64" if _platform.machine() in ("arm64", "aarch64") else "amd64") if plat == host else "amd64"
        tag = "x64" if arch == "amd64" else arch
        d = app_dir / "dist" / f"{name}-{plat}-{tag}"
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        if plat == "darwin":
            if host != "darwin":
                fails[plat] = "macOS 包需在 macOS 上构建(cgo + WebKit)"
                continue
            macos = d / f"{name}.app" / "Contents" / "MacOS"
            macos.mkdir(parents=True)
            exe = macos / name
            benv = dict(env, CGO_ENABLED="1", GOOS="darwin", GOARCH=arch)
            rc, out = _run([go, "build", "-trimpath", "-ldflags", "-s -w", "-o", str(exe), "."], app_dir, benv, log, timeout)
            if rc:
                fails[plat] = out
                continue
            (d / f"{name}.app" / "Contents" / "Info.plist").write_text(
                _INFO_PLIST.format(exe=escape(name), bid=escape(bid), name=escape(title)), encoding="utf-8")
            target = d / f"{name}.app"
        elif plat == "win32":
            exe = d / f"{name}.exe"
            benv = dict(env, CGO_ENABLED="0", GOOS="windows", GOARCH=arch)
            rc, out = _run([go, "build", "-trimpath", "-ldflags", "-s -w -H windowsgui", "-o", str(exe), "."],
                           app_dir, benv, log, timeout)
            if rc:
                fails[plat] = out
                continue
            target = exe
        else:
            fails[plat] = "Linux 桌面壳未实现(需 GTK WebKit)"
            continue
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        built.append({"platform": plat, "arch": tag, "dir": str(d), "executable": str(target),
                      "exe_bytes": exe.stat().st_size, "dir_bytes": size})
    gap = "; ".join(f"{k}: {str(v)[:200]}" for k, v in fails.items()) or None
    return {"ok": bool(built) and not fails, "artifacts": built, "failures": fails, "gap": gap,
            "signing": "未签名(macOS 未公证 / Windows 无 Authenticode); Windows 需系统自带 WebView2 运行时"}


def build_desktop(app_dir, platforms=None, arch=None, log=_print, timeout=1800) -> dict:
    """Electron 工程(ElectronTarget 产物)→ 各平台可执行包(macOS .app / Windows .exe / Linux)。"""
    app_dir = Path(app_dir)
    npm, npx = _which("npm"), _which("npx")
    if not (npm and npx):
        return {"ok": False, "gap": "缺工具链: Node.js/npm"}
    arch_given = arch
    arch = arch or ("arm64" if _platform.machine() in ("arm64", "aarch64") else "x64")
    host = host_platform()
    platforms = platforms or [host]
    if not (app_dir / "node_modules" / "electron").is_dir():
        rc, out = _run([npm, "install", "--no-audit", "--no-fund"], app_dir, os.environ.copy(), log, timeout)
        if rc:
            return {"ok": False, "gap": "npm install 失败(网络/镜像)", "log": out}
    built, fails = [], {}
    for plat in platforms:
        # 交叉打包(如 macOS 上出 Windows 包)默认 x64: 目标机器主流架构, 而不是宿主架构
        a = arch_given or (arch if plat == host else "x64")
        rc, out = _run([npx, "--no-install", "@electron/packager", ".", f"--platform={plat}", f"--arch={a}",
                        "--out=dist", "--overwrite", "--ignore=^/dist"], app_dir, os.environ.copy(), log, timeout)
        if rc:
            fails[plat] = out
            continue
        for d in sorted((app_dir / "dist").glob(f"*-{plat}-{a}")):
            exe = None
            if plat == "darwin":
                exe = next(d.glob("*.app"), None)
            elif plat == "win32":
                exe = next((p for p in d.glob("*.exe") if "squirrel" not in p.name.lower()), None)
            else:
                exe = next((p for p in d.iterdir() if p.is_file() and os.access(p, os.X_OK)
                            and p.suffix == "" and not p.name.startswith("chrome")), None)
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file() and not f.is_symlink())
            built.append({"platform": plat, "arch": a, "dir": str(d), "executable": str(exe) if exe else None,
                          "exe_bytes": (exe.stat().st_size if exe and exe.is_file() else None), "dir_bytes": size})
    return {"ok": bool(built) and not fails, "artifacts": built, "failures": fails,
            "signing": "未签名(macOS 未公证 / Windows 无 Authenticode)"}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m core.uir.toolchain")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apk")
    a.add_argument("app_dir")
    a.add_argument("--release", action="store_true")
    d = sub.add_parser("desktop")
    d.add_argument("app_dir")
    d.add_argument("--platform", default="")
    d.add_argument("--arch", default="")
    args = ap.parse_args(argv)
    if args.cmd == "apk":
        res = build_apk(args.app_dir, release=args.release)
    else:
        res = build_desktop(args.app_dir, [p for p in args.platform.split(",") if p] or None, args.arch or None)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
