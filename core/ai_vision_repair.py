"""
V4 AI 视觉闭环纠错引擎。

定位(门控兜底, 不是第一响应):
  确定性管线(iframe 递归 / 活体回补 / 渲染修复循环)完成后, 诚实评分仍低于
  AI_REPAIR_TRIGGER_SCORE 时, 才把【原站截图 + 克隆截图 + HTML 结构摘要】
  交给多模态大模型(OpenAI 兼容接口, 如 DeepSeek-V4.1 视觉版):
    a) 对比视觉差异, 定位缺失背景/错位表单/空白区域;
    b) 输出【AI 样式补丁 JSON】: css + html_insert[];
  系统自动持久化补丁到 static/css/ai_patch.css 并在 index.html 注入引用,
  随后离线重渲染 + 重新三通道评分, 迭代至达标或轮数用尽。

补丁格式(模型输出):
  {
    "diagnosis": "差异分析(中文)",
    "css": "追加的 CSS 代码",
    "html_insert": [{"selector": "body", "position": "beforeend", "html": "..."}],
    "notes": "补充说明"
  }
"""
import base64
import json
import re
import tempfile
from pathlib import Path

import requests
from PIL import Image

import config

AI_PATCH_LINK = '<link id="ai-vision-patch" rel="stylesheet" href="/static/css/ai_patch.css">'
AI_HTML_MARKER = "<!--ai-vision-html-->"

_SYSTEM_PROMPT = (
    "你是网页视觉还原专家。用户会提供同一页面的两张截图: 第一张是原站(标准答案), "
    "第二张是本地克隆(存在缺陷)。请对比后输出 JSON 补丁让克隆页在视觉上逼近原站。"
    "只输出一个 JSON 对象, 不要 markdown 代码块, 不要解释。格式:\n"
    '{"diagnosis": "中文差异分析", "css": "追加的CSS代码(可为空字符串)", '
    '"html_insert": [{"selector": "body", "position": "beforeend", "html": "插入的HTML片段"}], '
    '"notes": "补充说明"}\n'
    "要求: css 用固定定位/背景/尺寸规则弥补缺失视觉元素; html_insert 仅在克隆页"
    "出现整块空白区域(如登录表单缺失)时使用, 结构尽量简单; position 仅支持 "
    "beforeend; 禁止外链任何远程资源。"
    "用户会附上引擎的像素差分结果(差异最大的区域坐标、原站该处颜色、色差)和"
    "差异热力图(第 3 张图, 亮处=差异大), 请优先修这些区域, 不要改动没有列出差异的区域。"
)

# 补丁消毒: 模型输出不可信 —— 外链资源会让离线产物回源, <script> 会改变运行时
_CSS_REMOTE_URL_RE = re.compile(r"url\(\s*['\"]?\s*(?:https?:)?//[^)]*\)", re.I)
_CSS_IMPORT_RE = re.compile(r"@import[^;]*;", re.I)
_CSS_EXPRESSION_RE = re.compile(r"expression\s*\(", re.I)
_HTML_SCRIPT_RE = re.compile(r"<script\b.*?</script\s*>|<script\b[^>]*/?>", re.I | re.S)
_HTML_REMOTE_REF_RE = re.compile(r"""(\b(?:src|href|poster|data)\s*=\s*['\"])(?:https?:)?//[^'\"]*(['\"])""", re.I)
_HTML_EVENT_ATTR_RE = re.compile(r"""\son\w+\s*=\s*(?:'[^']*'|"[^"]*"|[^\s>]+)""", re.I)


def sanitize_css(css: str) -> str:
    """剥离外链 url()、@import、expression() —— 离线产物不得回源, 不得执行表达式。"""
    css = _CSS_IMPORT_RE.sub("", css or "")
    css = _CSS_REMOTE_URL_RE.sub("none", css)
    css = _CSS_EXPRESSION_RE.sub("blocked(", css)
    return css.replace("</style", "<\\/")


def sanitize_html(fragment: str) -> str:
    """剥离 <script>、内联事件、外链引用: html_insert 只允许静态结构与样式。"""
    html = _HTML_SCRIPT_RE.sub("", fragment or "")
    html = _HTML_EVENT_ATTR_RE.sub("", html)
    html = _HTML_REMOTE_REF_RE.sub(r"\1about:blank\2", html)
    return html


def region_hints(cells, detail: dict) -> str:
    """像素差分格 → 文本线索(给模型定位差异区域, 而不是让它在整页里猜)。

    cells: pixel_calibrator.divergent_cells 的输出 [(cx, cy, rgb, dist)], 差异降序。"""
    lines = []
    ratio = (detail or {}).get("height_ratio")
    if isinstance(ratio, (int, float)) and ratio < 0.95:
        lines.append(f"- 克隆页整页高度与原站之比 {ratio:.0%}: 存在整块区域缺失/多余")
    for cx, cy, rgb, dist in (cells or [])[:8]:
        r, g, b = (int(v) for v in rgb)
        lines.append(f"- 横向 {cx:.0%} / 纵向 {cy:.0%} 处: 原站该区域为纯色 rgb({r},{g},{b}), "
                     f"克隆页色差 {dist:.0f}(0=相同)")
    return "\n".join(lines) if lines else "- (像素差分未发现可定位的纯色区域差异)"


def _downscale_copy(image_path: Path, max_edge: int) -> bytes:
    """压缩截图(PNG)控制 token 体积, 返回 bytes。"""
    with Image.open(image_path) as img:
        img = img.convert("RGB")
        img.thumbnail((max_edge, max_edge))
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as buf:   # 关闭临时文件(曾泄漏句柄)
            img.save(buf, format="PNG")
            buf.seek(0)
            return buf.read()


def _chat_vision(ai_cfg: dict, original: Path, cloned: Path,
                 html_summary: str, detail: dict, hints: str = "",
                 diff_image: Path = None) -> dict:
    """调用 OpenAI 兼容多模态接口; 返回解析后的补丁 dict。

    hints: 像素差分区域线索(region_hints); diff_image: 差异热力图(可选第 3 张图)。"""
    images = []
    paths = [original, cloned]
    if diff_image is not None and Path(diff_image).is_file():
        paths.append(diff_image)
    for path in paths:
        data = _downscale_copy(path, config.AI_VISION_MAX_IMAGE_EDGE)
        images.append({"type": "image_url",
                       "image_url": {"url": "data:image/png;base64,"
                                            + base64.b64encode(data).decode("ascii")}})
    warnings = "\n".join(f"- {w}" for w in (detail or {}).get("warnings", [])) or "- (无)"
    diff_note = ("第 3 张图是差异热力图(亮处=差异大)。\n" if len(paths) == 3 else "")
    user_text = (
        f"原站截图为第 1 张图, 克隆截图为第 2 张图。{diff_note}"
        f"克隆页三通道评分: 结构 {detail.get('structure', '?')} / "
        f"边缘 {detail.get('edge', '?')} / 文本 {detail.get('text', '?')}\n"
        f"评分告警:\n{warnings}\n\n"
        f"像素差分定位到的差异区域(按色差降序):\n{hints or '- (无)'}\n\n"
        f"克隆页 HTML 结构摘要:\n{html_summary}\n\n"
        "请只针对上面列出的差异区域输出 JSON 补丁。"
    )

    def _call(budget: int):
        resp = requests.post(
            f"{ai_cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {ai_cfg['api_key']}"},
            json={
                "model": ai_cfg["model"], "temperature": 0.2, "max_tokens": budget,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": [{"type": "text", "text": user_text}, *images]},
                ],
            },
            timeout=config.AI_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            raise RuntimeError(
                f"上游返回非 JSON(HTTP {resp.status_code}): {resp.text[:160]!r} "
                f"— 请在模型配置中心执行三段式测试核实 base_url/model/key")

    # max_tokens 梯度: 推理模型的思考 token 计入补全预算, 预算太小会被
    # reasoning 全额吃掉(finish_reason=length 且 content 为空), 必须升档重试
    text, last_err = "", None
    for budget in (config.AI_PATCH_MAX_TOKENS, config.AI_PATCH_MAX_TOKENS * 2):
        payload = _call(budget)
        choice = (payload.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip()
        if text:
            break
        usage = payload.get("usage") or {}
        comp = usage.get("completion_tokens") or 0
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        if choice.get("finish_reason") == "length" and reasoning and reasoning * 2 >= comp:
            last_err = RuntimeError(
                f"推理模型思考 token 耗尽补全预算(max_tokens={budget}, "
                f"reasoning_tokens={reasoning}) — 已自动升档到 {budget * 2} 重试; "
                f"若仍失败请更换非推理多模态模型或继续调大补全预算")
            continue
        raise RuntimeError(
            f"上游返回空补丁(finish_reason={choice.get('finish_reason') or '未知'}, "
            f"usage={usage}) — 多模态模型可能不支持图像输入, "
            f"请在模型配置中心更换多模态模型")
    if not text:
        raise last_err or RuntimeError("上游返回空补丁")
    cleaned = re.sub(r"^```(?:json)?\s*", "", text)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        patch = json.loads(cleaned)
    except ValueError:
        # 模型可能在 JSON 前后夹说明文字: 截取首个 { 到最后一个 } 再试
        lo, hi = cleaned.find("{"), cleaned.rfind("}")
        if lo < 0 or hi <= lo:
            raise RuntimeError(f"上游未输出 JSON 补丁: {cleaned[:160]!r}")
        patch = json.loads(cleaned[lo:hi + 1])
    if not isinstance(patch, dict):
        raise ValueError("补丁不是 JSON 对象")
    patch.setdefault("css", "")
    patch.setdefault("html_insert", [])
    return patch


def _html_summary(ws: Path, page_file: Path = None) -> str:
    """克隆页结构摘要(标签统计 + 头部片段), 控制 token。"""
    index = Path(page_file) if page_file else ws / "index.html"
    if not index.is_file():
        return "(index.html 缺失)"
    html = index.read_text(encoding="utf-8", errors="replace")
    tags = {}
    for match in re.finditer(r"<(\w+)", html):
        tags[match.group(1).lower()] = tags.get(match.group(1).lower(), 0) + 1
    top = ", ".join(f"{k}x{v}" for k, v in
                    sorted(tags.items(), key=lambda kv: -kv[1])[:18])
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    head = html[:600].replace("\n", " ")
    return (f"title: {(title.group(1).strip() if title else '(无)')}\n"
            f"标签统计: {top}\nhead 开头: {head}")


def apply_patch(ws: Path, patch: dict, round_no: int, page_file: Path = None) -> dict:
    """补丁落地: ai_patch.css 追加 + 页面注入引用与 HTML 片段。

    page_file: 真实被渲染的页面(目录型 URL 时 index.html 只是跳转桩, 补丁必须
    落到 page_dir 下的影子页, 否则注入从不生效); 缺省 index.html。
    补丁先经 sanitize_css / sanitize_html 消毒: 模型输出不可信。"""
    applied = {"css_bytes": 0, "html_inserts": 0}
    css_text = sanitize_css(str(patch.get("css") or "")).strip()
    if css_text:
        css_file = ws / "static" / "css" / "ai_patch.css"
        css_file.parent.mkdir(parents=True, exist_ok=True)
        existing = css_file.read_text(encoding="utf-8") if css_file.is_file() else ""
        block = (f"\n/* ===== AI vision repair round {round_no} ===== */\n"
                 + css_text + "\n")
        css_file.write_text(existing + block, encoding="utf-8")
        applied["css_bytes"] = len(css_text.encode("utf-8"))

    index_file = Path(page_file) if page_file else ws / "index.html"
    if index_file.is_file():
        html = index_file.read_text(encoding="utf-8", errors="replace")
        if css_text and AI_PATCH_LINK not in html:
            head = re.search(r"<head[^>]*>", html, re.I)
            insert_at = head.end() if head else 0
            html = html[:insert_at] + AI_PATCH_LINK + html[insert_at:]
        inserts = patch.get("html_insert") or []
        if isinstance(inserts, list) and inserts:
            fragments = []
            for item in inserts:
                if isinstance(item, dict) and str(item.get("html") or "").strip():
                    cleaned = sanitize_html(str(item["html"])).strip()
                    if cleaned:
                        fragments.append(cleaned)
            if fragments:
                block = AI_HTML_MARKER + "\n".join(fragments)
                if AI_HTML_MARKER not in html:
                    if "</body>" in html.lower():
                        html = re.sub(r"</body>", block + "</body>", html, count=1,
                                      flags=re.I)
                    else:
                        html = html + block
                applied["html_inserts"] = len(fragments)
        index_file.write_text(html, encoding="utf-8")
    return applied


def _snapshot(ws: Path, page_file: Path) -> dict:
    """补丁前的产物快照(页面/补丁 CSS/截图/文本), 用于复评不升时回滚。"""
    snap = {}
    for key, path in (("page", page_file), ("css", ws / "static" / "css" / "ai_patch.css"),
                      ("shot", ws / "cloned_preview.png"), ("text", ws / "cloned_text.txt"),
                      ("diff", ws / "diff_preview.png")):
        snap[key] = (path, path.read_bytes() if path.is_file() else None)
    return snap


def _restore(snap: dict):
    for path, data in snap.values():
        if data is None:
            if path.is_file():
                path.unlink()
        else:
            path.write_bytes(data)


def run_vision_repair(ws: Path, ai_cfg: dict, detail: dict, log,
                      render_and_score, page_file: Path = None,
                      region_cells=None) -> dict:
    """视觉闭环主循环。render_and_score: () -> (detail, failed_local, remote)。

    - page_file: 真实渲染页(目录型 URL 的影子页); 缺省 index.html;
    - region_cells: pixel_calibrator.divergent_cells 的输出, 作为模型的区域线索;
    - 回滚守卫: 每轮补丁前快照页面/CSS/截图/文本, 复评不高于补丁前即整体恢复
      —— 与像素校准同一套"必须净提升"语义, 坏补丁不会留在产物里。"""
    page_file = Path(page_file) if page_file else ws / "index.html"
    report = {"model": ai_cfg["model"], "trigger_score": detail.get("score"),
              "rounds": [], "final_score": None}
    best_score = detail.get("score")
    for round_no in range(1, config.AI_REPAIR_MAX_ROUNDS + 1):
        log("AI-VISION", f"第 {round_no}/{config.AI_REPAIR_MAX_ROUNDS} 轮: "
                         f"提交双图、差异热力图与差分线索给大模型分析")
        hints = region_hints(region_cells, detail or {})
        try:
            patch = _chat_vision(ai_cfg, ws / "original_preview.png",
                                 ws / "cloned_preview.png",
                                 _html_summary(ws, page_file), detail or {},
                                 hints=hints, diff_image=ws / "diff_preview.png")
        except Exception as exc:
            log("AI-VISION", f"大模型调用失败: {type(exc).__name__}: {str(exc)[:200]}")
            report["rounds"].append({"round": round_no, "error": str(exc)[:300]})
            break
        diagnosis = str(patch.get("diagnosis") or "")[:500]
        log("AI-VISION", f"诊断: {diagnosis or '(未提供)'}")
        snap = _snapshot(ws, page_file)
        applied = apply_patch(ws, patch, round_no, page_file=page_file)
        log("AI-VISION", f"补丁已注入: CSS {applied['css_bytes']} 字节 / "
                         f"HTML 片段 {applied['html_inserts']} 个")
        if not applied["css_bytes"] and not applied["html_inserts"]:
            log("AI-VISION", "补丁为空(或消毒后无有效内容), 本轮跳过复评")
            report["rounds"].append({"round": round_no, "diagnosis": diagnosis,
                                     "applied": applied, "score_before": best_score,
                                     "score_after": None, "skipped": True})
            continue
        new_detail, _failed, _remote = render_and_score()
        round_record = {
            "round": round_no, "diagnosis": diagnosis,
            "notes": str(patch.get("notes") or "")[:300],
            "applied": applied,
            "score_before": best_score,
            "score_after": new_detail.get("score") if new_detail else None,
            "rolled_back": False,
        }
        report["rounds"].append(round_record)
        if new_detail is None:
            log("AI-VISION", "复评渲染失败, 回滚本轮补丁并终止闭环")
            _restore(snap)
            round_record["rolled_back"] = True
            break
        score = new_detail.get("score")
        log("AI-VISION", f"复评: {score}% (此前 {best_score}%)")
        improved = score is not None and (best_score is None or score > best_score)
        if improved:
            best_score = score
            report["final_score"] = score
            detail = new_detail
        else:
            # 回滚守卫: 补丁没有净提升 → 恢复补丁前的页面/CSS/截图/文本/热力图
            _restore(snap)
            round_record["rolled_back"] = True
            log("AI-VISION", f"补丁未提升({best_score}→{score}), 已回滚到补丁前产物")
        if score is not None and improved and score >= config.AI_REPAIR_TARGET_SCORE:
            log("AI-VISION", f"已达目标分 {config.AI_REPAIR_TARGET_SCORE:.0f}%, 闭环结束")
            break
    report["final_score"] = best_score if best_score is not None else report["final_score"]
    (ws / "ai_repair_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log("AI-VISION", f"视觉闭环完成: 最终评分 {report['final_score']}% "
                     f"(触发时 {report['trigger_score']}%), 报告已写入 ai_repair_report.json")
    return report
