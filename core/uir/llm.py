"""OpenAI 兼容多模态通道(复用克隆侧 ai_vision_repair 的调用形态与预算阶梯)。

- 文本 + 任意张图(base64 data URL)→ 要求模型只回一个 JSON 对象;
- max_tokens 阶梯: 推理模型的思考 token 计入补全预算, 被吃光(finish_reason=length
  且 content 空)时自动升档重试一次;
- JSON 提取容错: 剥 ``` 围栏 / 截取首个 { 到最后一个 }。
"""
import base64
import io
import json
import re
from pathlib import Path

import requests
from PIL import Image

import config


def resolve_ai_cfg(task_cfg: dict = None):
    """任务级 > 环境/配置默认; 无 Key 返回 None(与 ClonerEngine._ai_config 同语义)。"""
    task_cfg = task_cfg or {}
    base_url = (task_cfg.get("ai_base_url") or "").strip() or config.AI_API_BASE_URL
    api_key = (task_cfg.get("ai_api_key") or "").strip() or config.AI_API_KEY
    model = (task_cfg.get("ai_model") or "").strip() or config.AI_MODEL
    if not api_key:
        return None
    return {"base_url": base_url.rstrip("/"), "api_key": api_key, "model": model}


def _image_part(path: Path, max_edge: int) -> dict:
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((max_edge, max_edge))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
    return {"type": "image_url",
            "image_url": {"url": "data:image/png;base64,"
                                 + base64.b64encode(buf.getvalue()).decode("ascii")}}


def extract_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*", "", (text or "").strip())
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        obj = json.loads(cleaned)
    except ValueError:
        lo, hi = cleaned.find("{"), cleaned.rfind("}")
        if lo < 0 or hi <= lo:
            raise RuntimeError(f"上游未输出 JSON: {cleaned[:160]!r}")
        obj = json.loads(cleaned[lo:hi + 1])
    if not isinstance(obj, dict):
        raise ValueError("上游输出不是 JSON 对象")
    return obj


def chat_json(ai_cfg: dict, system: str, user_text: str, images=(),
              temperature: float = 0.2, timeout: int = None) -> dict:
    """同步调用, 返回解析后的 JSON 对象; 失败抛 RuntimeError。"""
    content = [{"type": "text", "text": user_text}]
    for p in images or ():
        p = Path(p)
        if p.is_file():
            content.append(_image_part(p, config.AI_VISION_MAX_IMAGE_EDGE))

    def _call(budget: int):
        resp = requests.post(
            f"{ai_cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {ai_cfg['api_key']}"},
            json={"model": ai_cfg["model"], "temperature": temperature, "max_tokens": budget,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": content}]},
            timeout=timeout or config.AI_REQUEST_TIMEOUT)
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            raise RuntimeError(f"上游返回非 JSON(HTTP {resp.status_code}): {resp.text[:160]!r}")

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
            last_err = RuntimeError(f"推理模型思考 token 耗尽补全预算(max_tokens={budget}), 已升档重试")
            continue
        raise RuntimeError(f"上游返回空内容(finish_reason={choice.get('finish_reason')}, usage={usage})")
    if not text:
        raise last_err or RuntimeError("上游返回空内容")
    return extract_json(text)
