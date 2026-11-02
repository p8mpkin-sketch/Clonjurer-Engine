"""引擎速度优化自动化循环: 单元测试闸门 → 基准评分 → 历史记账 → 回退告警。

用法:
  python3 engine_bench/perf/loop.py               # 跑一轮, 记入 history.jsonl
  python3 engine_bench/perf/loop.py --label "说明"  # 带本轮改动说明
  python3 engine_bench/perf/loop.py --watch 300     # 常驻: core/ 有改动即重跑(间隔秒)

每轮输出: 测试结果、各项提速/正确性/得分、总分、相对历史最佳的变化。
退出码: 0 = 测试全绿且全部输出与基线一致; 1 = 测试失败或任一输出不一致(总分记 0)。
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
HISTORY = HERE / "history.jsonl"
WATCHED = [REPO / "core" / n for n in ("cloner_engine.py", "pattern_detector.py", "pixel_calibrator.py")]


def _git_rev():
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                             capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "core"], cwd=REPO,
                               capture_output=True, text=True).stdout.strip()
        return rev + ("+dirty" if dirty else "")
    except OSError:
        return "?"


def _tests():
    p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                       cwd=REPO, capture_output=True, text=True)
    tail = (p.stderr or p.stdout).strip().splitlines()[-3:]
    return p.returncode == 0, " | ".join(tail)


def _best():
    if not HISTORY.is_file():
        return None
    best = None
    for line in HISTORY.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if best is None or rec["score"] > best["score"]:
            best = rec
    return best


def one_round(label="", repeat=5):
    ok_tests, test_tail = _tests()
    print(f"[tests] {'PASS' if ok_tests else 'FAIL'}  {test_tail}")
    out = subprocess.run([sys.executable, str(HERE / "bench.py"), "--repeat", str(repeat),
                          "--json", str(HERE / "last_run.json")], cwd=REPO, capture_output=True, text=True)
    print(out.stdout.rstrip())
    if out.returncode != 0:
        print(out.stderr[-2000:])
        return 1
    res = json.loads((HERE / "last_run.json").read_text())
    all_correct = all(b["correct"] for b in res["benches"])
    score = res["score"] if ok_tests else 0.0
    best = _best()
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "rev": _git_rev(), "label": label,
           "tests": ok_tests, "all_correct": all_correct, "score": score,
           "benches": {b["name"]: [b["speedup"], b["points"], b["correct"]] for b in res["benches"]}}
    with HISTORY.open("a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if best:
        delta = score - best["score"]
        print(f"[loop] 本轮 {score}  历史最佳 {best['score']} ({best['ts']} {best['label']})  Δ={delta:+.1f}")
        if delta < -5:
            print("[loop] ⚠ 明显回退: 检查最近改动")
    if not all_correct:
        bad = [b["name"] for b in res["benches"] if not b["correct"]]
        print(f"[loop] ✗ 输出与基线不一致: {bad} —— 正确性优先, 必须修复或回滚")
    return 0 if ok_tests and all_correct else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="")
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--watch", type=int, default=0)
    a = ap.parse_args()
    if not a.watch:
        sys.exit(one_round(a.label, a.repeat))
    last = None
    while True:
        stamp = tuple(p.stat().st_mtime for p in WATCHED)
        if stamp != last:
            last = stamp
            one_round(a.label or "watch", a.repeat)
        time.sleep(a.watch)


if __name__ == "__main__":
    main()
