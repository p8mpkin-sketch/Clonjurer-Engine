"""引擎模块速度基准 + 0–100 评分(自动化优化循环的度量核心)。

用法: python3 engine_bench/perf/bench.py [--repeat 5] [--json out.json] [--only name,...]

口径(透明、可复现):
- 基线 = engine_bench/perf/baseline/ 冻结的优化前引擎代码(commit 8a0cf35),
  与当前 core/ 代码在同一进程、同一夹具上交替运行, 机器噪声对两者等同;
- 每项交替运行 ≥repeat 次(快项自适应加采样到 ≥3s), 取最小耗时, 提速比 s = 基线耗时 / 当前耗时;
- 单项得分 = 100 × clamp(ln s / ln T, 0, 1), T = 3(提速 3 倍记满分;
  1x=0, 1.73x=50, 3x=100); 总分 = 按权重加权平均;
- 截图类基准模拟修复循环真实调用: 原站参考图在任务内固定, 克隆图每次都是
  新字节的文件(不命中任何缓存); score_long_cold 两图都新(权重 0, 仅展示);
- 正确性闸门: 每项当前输出必须与基线输出逐字节/逐值一致, 否则该项记 0 分;
  单元测试不通过时总分记 0(由 loop.py 执行)。
"""
import argparse
import importlib.util
import json
import math
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

from PIL import Image  # noqa: E402

import fixtures  # noqa: E402
from core import cloner_engine as cur_ce  # noqa: E402
from core import pattern_detector as cur_pd  # noqa: E402
from core import pixel_calibrator as cur_pc  # noqa: E402

TARGET = 3.0


def _load(name):
    spec = importlib.util.spec_from_file_location(f"baseline_{name}", HERE / "baseline" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BASE = {"ce": _load("cloner_engine"), "pd": _load("pattern_detector"), "pc": _load("pixel_calibrator")}
CUR = {"ce": cur_ce, "pd": cur_pd, "pc": cur_pc}
FX = fixtures.build()
TMP = Path(tempfile.mkdtemp(prefix="engine_bench_"))


def _engine(ce):
    eng = ce.ClonerEngine.__new__(ce.ClonerEngine)
    eng._target = fixtures.ORIGIN + "/"
    eng._raw_refs = dict(FX["raw_refs"])
    return eng


def _pairs(ce):
    eng = _engine(ce)
    routes = {"/", "/login", "/help", "/products/list"}
    srcset = {u + "?w=2x": u for u in FX["urls"][::7]}
    return eng._compose_pairs(FX["results"], routes, fixtures.ORIGIN, srcset, [], [])


PAIRS = _pairs(cur_ce)   # 替换表(两侧共用同一输入, compose 本身另行计时)


def _diff_pixels(path):
    with Image.open(path) as im:
        return im.convert("RGB").tobytes()


# ---- 基准项: fn(mods, tag) -> 可比较输出 ----------------------------------
_FRESH = [0]


def _fresh(name):
    """每次调用一张新字节的克隆截图(像素相同): 修复循环每轮都会重新截图,
    基准不得让克隆图命中任何缓存; 原站参考图在一个任务内固定不变(真实口径)。"""
    _FRESH[0] += 1
    return fixtures.fresh_copy(FX["shots"][name], TMP / f"fresh_{name}_{_FRESH[0]}.png", _FRESH[0])


def b_score_long(m, tag):
    d = TMP / f"diff_long_{tag}.png"
    r = m["ce"].compute_visual_similarity(FX["shots"]["long_a"], _fresh("long_b"), d,
                                          FX["text_a"], FX["text_b"])
    return r, d


def b_score_long_cold(m, tag):
    d = TMP / f"diff_cold_{tag}.png"
    r = m["ce"].compute_visual_similarity(_fresh("long_a"), _fresh("long_b"), d,
                                          FX["text_a"], FX["text_b"])
    return r, d


def b_score_view(m, tag):
    d = TMP / f"diff_view_{tag}.png"
    r = m["ce"].compute_visual_similarity(FX["shots"]["view_a"], _fresh("view_b"), d,
                                          FX["text_a"][:5000], FX["text_b"][:5000])
    return r, d


def b_localize_html(m, tag):
    ce = m["ce"]
    html = ce.apply_replacements(FX["html"], PAIRS)
    html = ce._BASE_TAG_RE.sub("", ce.rewrite_js_navigation(html))
    html = ce.ClonerEngine._inject_spa_disguise(html, "/")
    html = ce.ClonerEngine._inject_after_head(html, "<script>sim()</script>")
    html = ce.ClonerEngine._inject_calib(html, "a{b:c}")
    return ce._inject_responsive(html)


def b_rewrite_js(m, tag):
    ce = m["ce"]
    return ce.rewrite_js_navigation(ce.apply_replacements(FX["js"], PAIRS))


def b_css_refs(m, tag):
    return m["ce"].parse_css_refs(FX["css"], fixtures.CDN + "/css/main.css")


def b_text_score(m, tag):
    return m["ce"].text_f_score(FX["text_a"], FX["text_b"])


def b_compose_pairs(m, tag):
    return _pairs(m["ce"])


def b_pattern_detect(m, tag):
    return m["pd"].detect(FX["ws"], html=FX["html"])


def b_calib_cells(m, tag):
    return m["pc"].divergent_cells(FX["shots"]["view_a"], _fresh("view_b"))


def b_srcset(m, tag):
    ce = m["ce"]
    value = ", ".join(f"{u} {i}w" for i, u in enumerate(FX["urls"]))
    return [ce._parse_best_srcset(value, fixtures.ORIGIN + "/") for _ in range(20)]


# (名称, 函数, 权重, 说明) —— 权重按真实任务中的调用频次/耗时占比
BENCHES = [
    ("score_long", b_score_long, 3, "长页评分 v3: 修复循环常态(参考图固定+新克隆图)"),
    ("score_long_cold", b_score_long_cold, 0, "长页评分 v3: 冷启动(两图都是新文件; 仅展示不计分)"),
    ("score_view", b_score_view, 1, "首屏评分(参考图固定+新克隆图)"),
    ("localize_html", b_localize_html, 3, "HTML 本地化重写(1.5MB, ~1200 替换对)"),
    ("rewrite_js", b_rewrite_js, 3, "JS 包本地化重写(3MB)"),
    ("css_refs", b_css_refs, 1, "CSS 引用解析(1MB)"),
    ("text_score", b_text_score, 1, "文本 F2 指纹"),
    ("compose_pairs", b_compose_pairs, 1, "替换表构造(400 资源)"),
    ("pattern_detect", b_pattern_detect, 2, "框架模式识别(12MB JS 扫描)"),
    ("calib_cells", b_calib_cells, 1, "像素校准网格差分(参考图固定+新克隆图)"),
    ("srcset", b_srcset, 1, "srcset 解析"),
]


def _normalize(name, out):
    if name.startswith("score_"):
        r, d = out
        return json.dumps(r, sort_keys=True, ensure_ascii=False), _diff_pixels(d)
    return out


def run(repeat=5, only=None):
    rows = []
    for name, fn, weight, desc in BENCHES:
        if only and name not in only:
            continue
        fn(BASE, "b"), fn(CUR, "c")   # 预热(缓存/导入)
        tb, tc = [], []
        n, spent = 0, 0.0
        while n < repeat or (spent < 3.0 and n < 40):   # 快项自适应加采样
            n += 1
            order = ((BASE, "b", tb), (CUR, "c", tc))
            for mods, tag, acc in (order if n % 2 else order[::-1]):   # 交替先后, 消除次序偏差
                t0 = time.perf_counter(); out = fn(mods, tag); acc.append(time.perf_counter() - t0)
                if tag == "b":
                    ob = out
                else:
                    oc = out
            spent += tb[-1] + tc[-1]
        same = _normalize(name, ob) == _normalize(name, oc)
        mb, mc = min(tb), min(tc)   # 最小值: 共享机器上最抗噪的估计
        speedup = mb / mc if mc > 0 else float("inf")
        pts = 100.0 * min(1.0, max(0.0, math.log(speedup) / math.log(TARGET))) if same else 0.0
        rows.append({"name": name, "desc": desc, "weight": weight, "base_ms": round(mb * 1000, 2),
                     "cur_ms": round(mc * 1000, 2), "speedup": round(speedup, 2),
                     "correct": same, "points": round(pts, 1)})
    total_w = sum(r["weight"] for r in rows)
    score = sum(r["points"] * r["weight"] for r in rows) / total_w if total_w else 0.0
    return {"score": round(score, 1), "target_speedup": TARGET, "repeat": repeat, "benches": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--json")
    ap.add_argument("--only")
    a = ap.parse_args()
    res = run(a.repeat, set(a.only.split(",")) if a.only else None)
    print(f"{'bench':<16}{'base ms':>10}{'cur ms':>10}{'x':>7}{'ok':>4}{'pts':>7}")
    for r in res["benches"]:
        print(f"{r['name']:<16}{r['base_ms']:>10}{r['cur_ms']:>10}{r['speedup']:>7}"
              f"{'Y' if r['correct'] else 'N':>4}{r['points']:>7}")
    print(f"SCORE {res['score']} / 100   (T={TARGET}x 满分, 正确性闸门逐项)")
    if a.json:
        Path(a.json).write_text(json.dumps(res, ensure_ascii=False, indent=2))
    return res


if __name__ == "__main__":
    main()
