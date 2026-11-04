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


MAX_TILES = 6          # 超长页最多切几段送模型(再长的尾部不读, 与评分覆盖区一致)
TILE_MAX_H = 1600      # 每段(按宽缩放后)的最大高度


def _b64_png(img: Image.Image) -> dict:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return {"type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")}}


def image_parts(path, max_width: int = None, max_tiles: int = MAX_TILES, tile_h: int = TILE_MAX_H):
    """截图 → 一个或多个图片 part, 外加每段覆盖的原图 y 区间。

    按「宽」缩放而不是按最长边: 整页截图常是 1920×7000, 按最长边缩到 1280 后只剩 ~350px 宽,
    模型读不出字(实测直接回答"分辨率过低无法辨认")。缩放后高度超过 tile_h 的页面纵向切段,
    每段单独一张图, 并返回各段在原图坐标系的 [y0, y1)。"""
    max_width = int(max_width or config.AI_VISION_MAX_IMAGE_EDGE)
    with Image.open(path) as im:
        im = im.convert("RGB")
        W, H = im.size
        scale = min(1.0, max_width / W)
        work = im.resize((max(1, int(W * scale)), max(1, int(H * scale))), Image.BILINEAR) if scale < 1 else im.copy()
    parts, ranges = [], []
    wh = work.size[1]
    n = min(max_tiles, max(1, -(-wh // tile_h)))
    step = -(-wh // n)
    for k in range(n):
        y0, y1 = k * step, min(wh, (k + 1) * step)
        if y1 <= y0:
            break
        parts.append(_b64_png(work.crop((0, y0, work.size[0], y1))))
        ranges.append((round(y0 * H / wh), H if y1 >= wh else round(y1 * H / wh)))
    return parts, ranges, (W, H)


def _image_part(path: Path, max_edge: int) -> dict:
    """单图(短页)兼容入口。"""
    return image_parts(path, max_edge, max_tiles=1, tile_h=10 ** 9)[0][0]


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
              temperature: float = 0.2, timeout: int = None, max_tokens: int = None, parts=()) -> dict:
    """同步调用, 返回解析后的 JSON 对象; 失败抛 RuntimeError。

    预算阶梯 (b, 2b, 4b): 内容为空且思考 token 吃光预算 → 升档; 内容非空但 finish_reason=length
    且 JSON 解析失败(UIR 节点树上百节点时 JSON 很长, 被截断) → 同样升档重试, 而不是直接回落。"""
    content = [{"type": "text", "text": user_text}] + list(parts or ())   # parts: 已编码好的图片 part(切段并行读图用)
    notes = []
    for p in images or ():
        p = Path(p)
        if not p.is_file():
            continue
        parts, ranges, (W, H) = image_parts(p)
        content.extend(parts)
        if len(parts) > 1:
            notes.append(f"图 {p.name} 共 {W}×{H}px, 因过长按纵向切成 {len(parts)} 段依次给出, 各段覆盖原图 y 区间: "
                         + ", ".join(f"第{k + 1}段 [{a},{b})" for k, (a, b) in enumerate(ranges)))
    if notes:
        content[0]["text"] = user_text + "\n\n" + "\n".join(notes)

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
