"""登录验证码: 本地生成的图形验证码, 不依赖任何外部服务, 单机离线可用。

- 图片由 Pillow 在本进程内绘制(字符逐个旋转/错位 + 干扰线与噪点), 以 data URI 下发;
  答案只在服务端内存里保存其 HMAC 摘要, 不进 cookie、不进数据库。
- 每个挑战一次性: 无论对错, 校验一次即作废; 5 分钟过期; 内存表有上限, 超出先丢最旧的。
- 何时要求验证码由调用方决定(见 app.py 的 _captcha_needed): 默认「同一账号或同一来源 IP
  连续输错若干次后」才出现, 正常登录不打扰; CLONER_CAPTCHA=always 每次都要, off 关闭。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import math
import random
import secrets
import threading
import time
from collections import OrderedDict

# 去掉易混字符(0/O、1/I/L、2/Z、5/S、8/B), 大小写不敏感
ALPHABET = "ACDEFGHJKMNPQRTUVWXY34679"
LENGTH = 4
TTL_SECONDS = 300
MAX_PENDING = 5000
WIDTH, HEIGHT = 132, 44

_key = secrets.token_bytes(32)
_lock = threading.Lock()
_pending: "OrderedDict[str, tuple[bytes, float]]" = OrderedDict()


def _digest(answer: str) -> bytes:
    return hmac.new(_key, answer.strip().upper().encode("utf-8"), hashlib.sha256).digest()


def _prune(now: float) -> None:
    while _pending:
        cid, (_d, exp) = next(iter(_pending.items()))
        if exp > now and len(_pending) <= MAX_PENDING:
            break
        _pending.pop(cid, None)


def new_challenge() -> dict:
    """生成一个挑战: {"id", "image"(data:image/png;base64,...), "ttl"}。"""
    rng = random.SystemRandom()
    answer = "".join(rng.choice(ALPHABET) for _ in range(LENGTH))
    cid = secrets.token_urlsafe(18)
    now = time.time()
    with _lock:
        _pending[cid] = (_digest(answer), now + TTL_SECONDS)
        _prune(now)
    png = render_png(answer, rng)
    return {"id": cid, "image": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
            "ttl": TTL_SECONDS}


def verify(cid: str, answer: str) -> bool:
    """一次性校验; 空/过期/不存在一律 False。"""
    if not cid or not answer:
        return False
    with _lock:
        item = _pending.pop(str(cid), None)
    if item is None:
        return False
    digest, exp = item
    if exp < time.time():
        return False
    return hmac.compare_digest(digest, _digest(str(answer)[:16]))


def pending_count() -> int:
    with _lock:
        return len(_pending)


def render_png(text: str, rng=None) -> bytes:
    """画验证码。配色取中性灰阶 + 品牌橙一笔干扰线, 浅底深字, 深浅主题下都看得清。"""
    from PIL import Image, ImageDraw, ImageFont

    rng = rng or random.SystemRandom()
    scale = 2  # 按 2x 尺寸输出, 前端按 CSS 尺寸显示, 高分屏不糊
    w, h = WIDTH * scale, HEIGHT * scale
    img = Image.new("RGB", (w, h), (246, 244, 240))
    draw = ImageDraw.Draw(img)

    for _ in range(140):  # 噪点
        x, y = rng.randrange(w), rng.randrange(h)
        c = rng.randrange(150, 210)
        draw.ellipse((x, y, x + 2, y + 2), fill=(c, c, c))

    try:
        font = ImageFont.load_default(size=26 * scale)
    except TypeError:  # 极老的 Pillow 没有可缩放默认字体
        font = ImageFont.load_default()
    slot = (w - 12 * scale) / len(text)
    for i, ch in enumerate(text):
        # 每个字符画在 h×h 的透明方块正中, 绕中心旋转不会裁掉笔画, 再贴到各自的格子里
        glyph = Image.new("RGBA", (h, h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glyph)
        x0, y0, x1, y1 = gd.textbbox((0, 0), ch, font=font)
        shade = rng.randrange(30, 80)
        gd.text(((h - (x1 - x0)) / 2 - x0, (h - (y1 - y0)) / 2 - y0), ch, font=font,
                fill=(shade, shade + rng.randrange(0, 12), shade + rng.randrange(0, 18), 255))
        glyph = glyph.rotate(rng.uniform(-24, 24), resample=Image.BICUBIC)
        cx = 6 * scale + slot * (i + 0.5) + rng.uniform(-3, 3) * scale
        cy = h / 2 + rng.uniform(-4, 4) * scale
        img.paste(glyph, (int(cx - h / 2), int(cy - h / 2)), glyph)

    for k in range(3):  # 干扰曲线, 穿过字符
        amp = rng.uniform(4, 10) * scale
        freq = rng.uniform(1.2, 2.6)
        phase = rng.uniform(0, math.tau)
        base = rng.uniform(0.35, 0.7) * h
        pts = [(x, base + amp * math.sin(freq * math.tau * x / w + phase)) for x in range(0, w, 4)]
        color = (196, 83, 47) if k == 0 else (rng.randrange(90, 140),) * 3
        draw.line(pts, fill=color, width=2 * scale if k == 0 else scale)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
