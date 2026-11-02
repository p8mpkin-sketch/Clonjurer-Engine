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
)


def _downscale_copy(image_path: Path, max_edge: int) -> bytes:
    """压缩截图(PNG)控制 token 体积, 返回 bytes。"""
    with Image.open(image_path) as img:
        img = img.convert("RGB")
        img.thumbnail((max_edge, max_edge))
        buf = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf.read()


def _chat_vision(ai_cfg: dict, original: Path, cloned: Path,
                 html_summary: str, detail: dict) -> dict:
    """调用 OpenAI 兼容多模态接口; 返回解析后的补丁 dict。"""
    images = []
    for path in (original, cloned):
        data = _downscale_copy(path, config.AI_VISION_MAX_IMAGE_EDGE)
        images.append({"type": "image_url",
                       "image_url": {"url": "data:image/png;base64,"
                                            + base64.b64encode(data).decode("ascii")}})
    warnings = "\n".join(f"- {w}" for w in (detail or {}).get("warnings", [])) or "- (无)"
    user_text = (
        f"原站截图为第 1 张图, 克隆截图为第 2 张图。\n"
        f"克隆页三通道评分: 结构 {detail.get('structure', '?')} / "
        f"边缘 {detail.get('edge', '?')} / 文本 {detail.get('text', '?')}\n"
        f"评分告警:\n{warnings}\n\n"
        f"克隆页 HTML 结构摘要:\n{html_summary}\n\n"
        "请输出 JSON 补丁。"
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


def _html_summary(ws: Path) -> str:
    """克隆页结构摘要(标签统计 + 头部片段), 控制 token。"""
    index = ws / "index.html"
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


def apply_patch(ws: Path, patch: dict, round_no: int) -> dict:
    """补丁落地: ai_patch.css 追加 + index.html 注入引用与 HTML 片段。"""
    applied = {"css_bytes": 0, "html_inserts": 0}
    css_text = str(patch.get("css") or "").strip()
    if css_text:
        css_file = ws / "static" / "css" / "ai_patch.css"
        css_file.parent.mkdir(parents=True, exist_ok=True)
        existing = css_file.read_text(encoding="utf-8") if css_file.is_file() else ""
        block = (f"\n/* ===== AI vision repair round {round_no} ===== */\n"
                 + css_text.replace("</style", "<\\/") + "\n")
        css_file.write_text(existing + block, encoding="utf-8")
        applied["css_bytes"] = len(css_text.encode("utf-8"))

    index_file = ws / "index.html"
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
                    fragments.append(str(item["html"]).strip())
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


def run_vision_repair(ws: Path, ai_cfg: dict, detail: dict, log,
                      render_and_score) -> dict:
    """视觉闭环主循环。render_and_score: () -> (detail, failed_local, remote)。"""
    report = {"model": ai_cfg["model"], "trigger_score": detail.get("score"),
              "rounds": [], "final_score": None}
    best_score = detail.get("score")
    for round_no in range(1, config.AI_REPAIR_MAX_ROUNDS + 1):
        log("AI-VISION", f"第 {round_no}/{config.AI_REPAIR_MAX_ROUNDS} 轮: "
                         f"提交双图与结构摘要给大模型分析")
        try:
            patch = _chat_vision(ai_cfg, ws / "original_preview.png",
                                 ws / "cloned_preview.png",
                                 _html_summary(ws), detail or {})
        except Exception as exc:
            log("AI-VISION", f"大模型调用失败: {type(exc).__name__}: {str(exc)[:200]}")
            report["rounds"].append({"round": round_no, "error": str(exc)[:300]})
            break
        diagnosis = str(patch.get("diagnosis") or "")[:500]
        log("AI-VISION", f"诊断: {diagnosis or '(未提供)'}")
        applied = apply_patch(ws, patch, round_no)
        log("AI-VISION", f"补丁已注入: CSS {applied['css_bytes']} 字节 / "
                         f"HTML 片段 {applied['html_inserts']} 个")
        new_detail, _failed, _remote = render_and_score()
        round_record = {
            "round": round_no, "diagnosis": diagnosis,
            "notes": str(patch.get("notes") or "")[:300],
            "applied": applied,
            "score_before": best_score,
            "score_after": new_detail.get("score") if new_detail else None,
        }
        report["rounds"].append(round_record)
        if new_detail is None:
            log("AI-VISION", "复评渲染失败, 终止闭环")
            break
        score = new_detail.get("score")
        log("AI-VISION", f"复评: {score}% (此前 {best_score}%)")
        if score is not None and (best_score is None or score > best_score):
            best_score = score
            report["final_score"] = score
        if score is not None and score >= config.AI_REPAIR_TARGET_SCORE:
            log("AI-VISION", f"已达目标分 {config.AI_REPAIR_TARGET_SCORE:.0f}%, 闭环结束")
            break
        detail = new_detail
    report["final_score"] = best_score if best_score is not None else report["final_score"]
    (ws / "ai_repair_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log("AI-VISION", f"视觉闭环完成: 最终评分 {report['final_score']}% "
                     f"(触发时 {report['trigger_score']}%), 报告已写入 ai_repair_report.json")
    return report
