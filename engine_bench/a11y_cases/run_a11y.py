"""a11y 适配器跑分(ROADMAP_95.md §3.1 闸门: dump → UIR → html_css 渲染与原应用截图像素 v3)。

语料: 每例一个目录 = tree.json(统一树 JSON, 采集时的原样输出) + reference.png(同次采集的设备截图)。
  tune/     调参时看过的样本(含曾作为留出、后被用于改进分割线/滑块的 5 例, 已污染, 归入 tune)
  holdout/  调参结束后才采集、未据其改过任何代码的留出样本
全部样本来自模拟器上的 AOSP 开源应用(Settings/Calculator2/Contacts/Dialer/DocumentsUI), 运行期授权采集。

三档口径(同一棵树):
  tree_only  只用无障碍树(无截图): 布局/文本/角色, 无颜色与图片
  no_embed   树 + 截图采色/墨迹字号/分割线, 图片位为占位
  with_shot  上一档 + 图片/图标/图形控件按截图裁剪内嵌(采集即得, 不额外请求)

用法: python engine_bench/a11y_cases/run_a11y.py [tune|holdout ...]   → runs/a11y_<时间>.json + 终端表
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from core.uir.adapters.a11y import tree_to_uir  # noqa: E402
from core.uir.render import score_against_reference  # noqa: E402
from core.uir.targets.html_css import HtmlCssTarget  # noqa: E402

HERE = Path(__file__).resolve().parent
MODES = ("tree_only", "no_embed", "with_shot")


def reference_logical(tree: dict, shot: Path, out: Path):
    """设备截图 → 窗口区域 → 逻辑分辨率(与 UIR 视口同尺寸), 作为评分参考图。"""
    sc = float(tree.get("scale") or 1.0)
    px = float(tree.get("screenshot_scale") or sc)
    x, y, w, h = tree["window"]
    lw, lh = round(w / sc), round(h / sc)
    with Image.open(shot) as im:
        box = (0, 0, round(w / sc * px), round(h / sc * px)) if tree["platform"] == "macos" else (x, y, x + w, y + h)
        im.convert("RGB").crop(box).resize((lw, lh), Image.LANCZOS).save(out)
    return lw, lh


def run_case(case: Path, work: Path, browser) -> dict:
    tree = json.loads((case / "tree.json").read_text(encoding="utf-8"))
    shot = case / "reference.png"
    work.mkdir(parents=True, exist_ok=True)
    ref = work / "ref.png"
    lw, lh = reference_logical(tree, shot, ref)
    out = {}
    for mode in MODES:
        t0 = time.monotonic()
        u = tree_to_uir(tree, None if mode == "tree_only" else shot, embed_images=(mode == "with_shot"))
        r = HtmlCssTarget().render(u, work / mode, None)
        map_s = time.monotonic() - t0
        page = browser.new_page(viewport={"width": lw, "height": lh})
        page.goto(r.entry_file.resolve().as_uri())
        page.wait_for_timeout(300)
        png = work / f"{mode}.png"
        page.screenshot(path=str(png))
        page.close()
        sc = score_against_reference(ref, png, work / f"{mode}_diff.png")
        out[mode] = {"score": sc.get("score"), "structure": sc.get("structure"), "edge": sc.get("edge"),
                     "nodes": u.root.count(), "map_s": round(map_s, 3)}
    return out


def main(groups):
    from playwright.sync_api import sync_playwright
    stamp = time.strftime("%Y%m%d_%H%M%S")
    work = HERE / "runs" / f"work_{stamp}"
    res = {}
    with sync_playwright() as p:
        br = p.chromium.launch()
        for g in groups:
            for case in sorted((HERE / g).iterdir()):
                if (case / "tree.json").exists():
                    res[f"{g}/{case.name}"] = run_case(case, work / g / case.name, br)
                    print(f"{g}/{case.name:28s}", "  ".join(f"{m}={res[f'{g}/{case.name}'][m]['score']:5.1f}" for m in MODES))
        br.close()
    summary = {}
    for g in groups:
        rows = [v for k, v in res.items() if k.startswith(g + "/")]
        if rows:
            summary[g] = {m: round(sum(r[m]["score"] for r in rows) / len(rows), 1) for m in MODES}
            summary[g]["min_with_shot"] = min(r["with_shot"]["score"] for r in rows)
            summary[g]["n"] = len(rows)
    print(json.dumps(summary, ensure_ascii=False))
    (HERE / "runs").mkdir(exist_ok=True)
    (HERE / "runs" / f"a11y_{stamp}.json").write_text(
        json.dumps({"summary": summary, "cases": res}, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


if __name__ == "__main__":
    main(sys.argv[1:] or ["tune", "holdout"])
