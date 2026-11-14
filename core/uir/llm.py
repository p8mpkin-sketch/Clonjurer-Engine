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


_NO_THINK_UNSUPPORTED = set()   # 不认 thinking 字段的上游(base_url)
_NO_JSON_MODE = set()           # 不认 response_format=json_object 的上游(base_url)


class DeadlineExceeded(RuntimeError):
    """模型调用超过墙钟上限(含重试/升档)。"""


def _read_env_file(path: str) -> dict:
    """解析仓库外的 KEY=VALUE 文件(可带 export / 引号), 只取 CLONER_AI_* 项; 文件不存在返回空。"""
    out = {}
    if not path:
        return out
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.replace("export ", "").strip()
        v = v.strip().strip("'\"")
        if k.startswith("CLONER_AI_") and v:
            out[k] = v
    return out


def resolve_ai_cfg(task_cfg: dict = None, role: str = ""):
    """任务级 > 环境/配置默认(含设置中心) > 仓库外 Key 文件; 无 Key 返回 None。

    Key 文件(settings.GEN_AI_ENV_FILE, 默认 ~/.clonjurer_ai.env)只在生成侧读取: 服务进程没带环境变量启动时
    界面生成仍能拿到模型, 又不改变克隆管线「有 Key 才启用的可选能力」的行为。
    role="judge": 验收裁判可单独配模型(CLONER_AI_JUDGE_MODEL / _KEY / _BASE_URL), 未配则与主模型相同。"""
    from core.uir import settings
    task_cfg = task_cfg or {}
    filed = _read_env_file(getattr(settings, "GEN_AI_ENV_FILE", ""))
    api_key = (task_cfg.get("ai_api_key") or "").strip() or config.AI_API_KEY
    if api_key:
        base_url = (task_cfg.get("ai_base_url") or "").strip() or config.AI_API_BASE_URL
        model = (task_cfg.get("ai_model") or "").strip() or config.AI_MODEL
    else:
        api_key = filed.get("CLONER_AI_KEY", "")
        base_url = filed.get("CLONER_AI_BASE_URL") or config.AI_API_BASE_URL
        model = filed.get("CLONER_AI_MODEL") or config.AI_MODEL
    if not api_key:
        return None
    cfg = {"base_url": base_url.rstrip("/"), "api_key": api_key, "model": model}
    if role == "judge":
        import os
        pick = lambda k: (os.environ.get(k) or filed.get(k) or "").strip()  # noqa: E731
        if pick("CLONER_AI_JUDGE_MODEL"):
            cfg = {"base_url": (pick("CLONER_AI_JUDGE_BASE_URL") or cfg["base_url"]).rstrip("/"),
                   "api_key": pick("CLONER_AI_JUDGE_KEY") or cfg["api_key"],
                   "model": pick("CLONER_AI_JUDGE_MODEL")}
    return cfg


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
    """从模型输出里取 JSON 对象。容忍: 代码围栏、对象前的说明文字、对象后的多余内容(小模型在 JSON 模式下
    也会偶发在合法对象后面再吐一段, json.loads 报 Extra data) —— 取第一个能完整解析的对象。"""
    cleaned = re.sub(r"^```(?:json)?\s*", "", (text or "").strip())
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        obj = json.loads(cleaned)
    except ValueError:
        obj, pos, dec = None, cleaned.find("{"), json.JSONDecoder()
        if pos < 0:
            raise RuntimeError(f"上游未输出 JSON: {cleaned[:160]!r}")
        first_err = None
        while pos >= 0 and obj is None:
            try:
                got, _ = dec.raw_decode(cleaned, pos)
                if isinstance(got, dict):
                    obj = got
                    break
            except ValueError as exc:
                first_err = first_err or exc
            pos = cleaned.find("{", pos + 1)
        if obj is None:
            raise first_err or ValueError("上游输出不是 JSON 对象")
    if not isinstance(obj, dict):
        raise ValueError("上游输出不是 JSON 对象")
    return obj


def _build_content(user_text: str, images=(), parts=()) -> list:
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

    return content


def _transport(ai_cfg: dict, system: str, content: list, temperature: float, timeout, deadline, thinking: bool,
               json_mode: bool = False):
    """返回 (call, left): call(budget) 发一次请求(含瞬时网络错误重试)并返回上游 JSON; left() 为剩余墙钟。"""
    if deadline is None:
        deadline = time.monotonic() + float(getattr(config, "AI_CALL_WALL_LIMIT", 180))

    def _left() -> float:
        return deadline - time.monotonic()

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
                    if _left() < 2 * (attempt + 1) + 5:
                        break
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"上游网络错误(已重试 3 次): {type(last).__name__}: {str(last)[:160]}")

    def _call_once(budget: int):
        left = _left()
        if left <= 1:
            raise DeadlineExceeded("调用墙钟上限已到")
        per = min(float(timeout or config.AI_REQUEST_TIMEOUT), max(1.0, left))
        body_json = {"model": ai_cfg["model"], "temperature": temperature, "max_tokens": budget,
                     "messages": [{"role": "system", "content": system},
                                  {"role": "user", "content": content}]}
        no_think = not thinking and ai_cfg.get("base_url") not in _NO_THINK_UNSUPPORTED
        if no_think:
            body_json["thinking"] = {"type": "disabled"}
        want_json = json_mode and ai_cfg.get("base_url") not in _NO_JSON_MODE
        if want_json:
            body_json["response_format"] = {"type": "json_object"}
        resp = requests.post(
            f"{ai_cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {ai_cfg['api_key']}"},
            json=body_json, timeout=(min(15.0, per), per), stream=True)
        if want_json and resp.status_code in (400, 422):
            # 先假定是 response_format 不被接受(可选能力): 去掉重发; 仍 400 再轮到 thinking 字段
            resp.close()
            _NO_JSON_MODE.add(ai_cfg.get("base_url"))
            return _call_once(budget)
        if no_think and resp.status_code in (400, 422):
            resp.close()
            _NO_THINK_UNSUPPORTED.add(ai_cfg.get("base_url"))
            return _call_once(budget)
        try:
            resp.raise_for_status()
            chunks = []
            raw = resp.raw
            while True:
                # read1: 有多少读多少(不凑满块), keep-alive 的单个空行也会返回, 才能逐块核对墙钟
                chunk = raw.read1(65536, decode_content=True)
                if not chunk:
                    break
                chunks.append(chunk)
                if _left() <= 0:
                    raise DeadlineExceeded("调用墙钟上限已到(上游仍在生成)")
            body = b"".join(chunks)
        finally:
            resp.close()
        try:
            return json.loads(body.decode(resp.encoding or "utf-8", errors="replace"))
        except ValueError:
            raise RuntimeError(f"上游返回非 JSON(HTTP {resp.status_code}): {body[:160]!r}")

    return _call, _left


def chat_json(ai_cfg: dict, system: str, user_text: str, images=(),
              temperature: float = 0.2, timeout: int = None, max_tokens: int = None, parts=(),
              deadline: float = None, thinking: bool = True, usage_out: dict = None,
              json_mode: bool = False) -> dict:
    """同步调用, 返回解析后的 JSON 对象; 失败抛 RuntimeError。usage_out 给出时写入上游 usage(用量统计)。

    deadline(time.monotonic() 绝对时刻)是整次调用(含重试与升档)的**墙钟上限**: 部分 OpenAI 兼容上游
    在长生成期间持续发 keep-alive 空行, requests 的读超时永远不触发(实测单请求挂起 30 分钟以上),
    所以响应体按流读取, 每收到一块就核对墙钟, 超时即断开并抛 DeadlineExceeded。

    thinking=False: 抄录/逐块填充这类机械任务关闭推理模型的思考(请求带 thinking.type=disabled);
    上游不认该字段(HTTP 400)时自动去掉重发, 并按 base_url 记住, 之后不再带。

    预算阶梯 (b, 2b, 4b): 内容为空且思考 token 吃光预算 → 升档; 内容非空但 finish_reason=length
    且 JSON 解析失败(UIR 节点树上百节点时 JSON 很长, 被截断) → 同样升档重试, 而不是直接回落。"""
    content = _build_content(user_text, images, parts)
    # json_mode: 请求带 response_format=json_object(上游保证输出是合法 JSON); 不认该字段的上游自动去掉重发
    _call, _left = _transport(ai_cfg, system, content, temperature, timeout, deadline, thinking, json_mode)
    base = int(max_tokens or config.AI_PATCH_MAX_TOKENS)
    text, last_err = "", None
    for budget in (base, base * 2, base * 4):
        if _left() <= 1:
            raise last_err or DeadlineExceeded("调用墙钟上限已到")
        payload = _call(budget)
        choice = (payload.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip()
        finish = choice.get("finish_reason")
        usage = payload.get("usage") or {}
        if text:
            try:
                obj = extract_json(text)
                if usage_out is not None:
                    usage_out.update(usage)
                return obj
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


def chat_text(ai_cfg: dict, system: str, user_text: str, images=(), temperature: float = 0.2,
              timeout: int = None, max_tokens: int = None, parts=(), deadline: float = None,
              thinking: bool = True, complete=None) -> dict:
    """同 chat_json 的传输与墙钟语义, 但返回原始文本: {"text","finish","usage","elapsed","budget"}。

    用于模型直接写代码(整页 HTML)这类非 JSON 产出。预算阶梯 (b, 2b): 内容为空且思考吃光预算, 或
    finish_reason=length 且 complete(text) 判定产物不完整 → 升档重试一次; 仍被截断则原样返回, 由调用方处理。"""
    content = _build_content(user_text, images, parts)
    _call, _left = _transport(ai_cfg, system, content, temperature, timeout, deadline, thinking)
    base = int(max_tokens or config.AI_PATCH_MAX_TOKENS)
    t0 = time.monotonic()
    out, last_err = None, None
    for budget in (base, base * 2):
        if _left() <= 1:
            break
        payload = _call(budget)
        choice = (payload.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip()
        finish = choice.get("finish_reason")
        usage = payload.get("usage") or {}
        out = {"text": text, "finish": finish, "usage": usage, "budget": budget}
        if text and (finish != "length" or (complete and complete(text))):
            break
        if finish == "length":
            last_err = RuntimeError(f"输出被截断(max_tokens={budget})")
            continue
        if not text:
            raise RuntimeError(f"上游返回空内容(finish_reason={finish}, usage={usage})")
    if out is None or not out["text"]:
        raise last_err or DeadlineExceeded("调用墙钟上限已到")
    out["elapsed"] = round(time.monotonic() - t0, 2)
    return out
