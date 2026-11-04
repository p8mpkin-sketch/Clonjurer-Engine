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
import time
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
              temperature: float = 0.2, timeout: int = None, max_tokens: int = None) -> dict:
    """同步调用, 返回解析后的 JSON 对象; 失败抛 RuntimeError。

    预算阶梯 (b, 2b, 4b): 内容为空且思考 token 吃光预算 → 升档; 内容非空但 finish_reason=length
    且 JSON 解析失败(UIR 节点树上百节点时 JSON 很长, 被截断) → 同样升档重试, 而不是直接回落。"""
    content = [{"type": "text", "text": user_text}]
    for p in images or ():
        p = Path(p)
        if p.is_file():
            content.append(_image_part(p, config.AI_VISION_MAX_IMAGE_EDGE))

    def _call(budget: int):
        # 瞬时网络错误(SSL EOF / 连接重置 / 读超时)重试 2 次, 退避 2s/4s: 一次抖动不应让整轮回落
        last = None
        for attempt in range(3):
            try:
                return _call_once(budget)
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
                last = exc
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"上游网络错误(已重试 3 次): {type(last).__name__}: {str(last)[:160]}")

    def _call_once(budget: int):
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

    base = int(max_tokens or config.AI_PATCH_MAX_TOKENS)
    text, last_err = "", None
    for budget in (base, base * 2, base * 4):
        payload = _call(budget)
        choice = (payload.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip()
        finish = choice.get("finish_reason")
        usage = payload.get("usage") or {}
        if text:
            try:
                return extract_json(text)
            except (ValueError, RuntimeError) as exc:
                if finish == "length":
                    last_err = RuntimeError(f"输出被截断(max_tokens={budget}, finish_reason=length), 已升档重试")
                    continue
                raise RuntimeError(f"上游 JSON 不可解析: {exc}") from exc
        comp = usage.get("completion_tokens") or 0
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        if finish == "length" and reasoning and reasoning * 2 >= comp:
            last_err = RuntimeError(f"推理模型思考 token 耗尽补全预算(max_tokens={budget}), 已升档重试")
            continue
        raise RuntimeError(f"上游返回空内容(finish_reason={finish}, usage={usage})")
    raise last_err or RuntimeError("上游返回空内容")
