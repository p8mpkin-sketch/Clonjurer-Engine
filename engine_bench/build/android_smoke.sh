#!/usr/bin/env bash
# APK 模拟器冒烟(build.yml 在 android-emulator-runner 里调用): 安装 → 启动 → 进程存活 → screencap。
# 用法: android_smoke.sh <apk> <输出目录>
set -euo pipefail
APK="$1"; OUT="$2"
mkdir -p "$OUT"
BT=$(ls -d "$ANDROID_HOME"/build-tools/*/ | sort -V | tail -1)
PKG=$("${BT}aapt2" dump packagename "$APK")
echo "package: $PKG"
adb wait-for-device
adb install -r "$APK"
adb logcat -c || true
adb shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1
sleep 25
if ! adb shell pidof "$PKG" > "$OUT/pid.txt"; then
  echo "应用启动后未在运行(崩溃?)"; adb logcat -d -t 400 > "$OUT/logcat.txt" || true; exit 1
fi
adb exec-out screencap -p > "$OUT/android.png"
adb shell wm density | tail -1 | sed 's/.*: *//' | tr -d '\r' > "$OUT/density.txt"
adb logcat -d -t 400 > "$OUT/logcat.txt" || true
echo "冒烟通过: pid $(cat "$OUT/pid.txt"), density $(cat "$OUT/density.txt")"
