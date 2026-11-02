"""引擎速度基准的确定性合成夹具(固定种子, 无网络/浏览器)。

规模取自引擎真实上限: MAX_ASSETS_PER_TASK=400 条资源 → ~1200 条替换对;
长页截图 1440×9000(评分 v3 分段上限内); JS 包 3MB; CSS 1MB; JS 扫描 12MB。
"""
import random
from pathlib import Path

import numpy as _np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent / ".fixtures"
ORIGIN = "https://www.example-shop.com"
CDN = "https://cdn.example-static.net"
WORDS = ("account login password submit search product price cart order "
         "service help news about contact privacy terms download app mobile "
         "member coupon shipping return review rating category brand").split()
CJK = "登录注册密码验证码首页产品服务帮助新闻关于我们联系隐私条款下载应用会员优惠配送退货评价分类品牌购物车订单搜索价格"


def _rnd(seed):
    return random.Random(seed)


def asset_urls(n=400, seed=1):
    r = _rnd(seed)
    exts = [".js", ".css", ".png", ".jpg", ".svg", ".woff2", ".webp"]
    urls = []
    for i in range(n):
        host = ORIGIN if r.random() < 0.6 else CDN
        depth = "/".join(r.choice(["static", "assets", "img", "js", "css", "v2", "dist"])
                         for _ in range(r.randint(1, 3)))
        name = f"{r.choice(WORDS)}-{i}.{r.getrandbits(40):010x}{r.choice(exts)}"
        q = f"?v={r.randint(1, 999)}" if r.random() < 0.3 else ""
        urls.append(f"{host}/{depth}/{name}{q}")
    return urls


def results_for(urls):
    out = {}
    for i, u in enumerate(urls):
        ext = "." + u.split("?")[0].rsplit(".", 1)[-1]
        name = f"{i:016x}{ext}"
        out[u] = {"local": f"/static/misc/{name}", "abs_path": Path("/ws/static/misc") / name,
                  "ext": ext}
    return out


def raw_refs_for(urls, seed=2):
    r = _rnd(seed)
    refs = {}
    for u in urls:
        if r.random() < 0.5:
            path = u.split("://", 1)[1].split("/", 1)[1]
            refs["../" + path] = u
            refs["/" + path.split("?")[0]] = u
    return refs


def html_page(urls, size=1_500_000, seed=3):
    r = _rnd(seed)
    parts = ['<!DOCTYPE html><html><head><meta charset="utf-8"><base href="https://www.example-shop.com/">'
             '<title>shop</title>']
    for u in urls[:60]:
        if u.split("?")[0].endswith(".css"):
            parts.append(f'<link rel="stylesheet" href="{u}">')
    parts.append("</head><body>")
    n = 0
    while n < size:
        kind = r.random()
        if kind < 0.25:
            u = r.choice(urls)
            s = f'<img src="{u}" alt="{r.choice(WORDS)}" class="c{r.randint(0, 99)}">'
        elif kind < 0.35:
            u = r.choice(urls)
            s = f'<div style="background-image:url(\'{u}\')" data-x="{r.randint(0, 9999)}"></div>'
        elif kind < 0.4:
            s = ('<script>if(x){window.top.location = "/login";}'
                 "window.open(url,'_self');parent.location.href=u;</script>")
        elif kind < 0.7:
            s = "<p>" + " ".join(r.choice(WORDS) for _ in range(r.randint(5, 30))) + "</p>"
        else:
            s = "<span>" + "".join(r.choice(CJK) for _ in range(r.randint(4, 40))) + "</span>"
        parts.append(s)
        n += len(s)
    parts.append("</body></html>")
    return "".join(parts)


def js_bundle(urls, size=3_000_000, seed=4):
    r = _rnd(seed)
    parts = ["/*! bundle */(function(){var __webpack_require__=function(){};"]
    n = 0
    while n < size:
        k = r.random()
        if k < 0.04:
            s = f'e.src="{r.choice(urls)}";'
        elif k < 0.05:
            s = "if(t){window.top.location=r.href}"
        else:
            s = (f"function f{r.getrandbits(24):x}(a,b){{var c=a+b*{r.randint(0, 999)};"
                 f"return c>{r.randint(0, 99)}?'{r.choice(WORDS)}':a.map(function(x){{return x+1}})}}")
        parts.append(s)
        n += len(s)
    parts.append("})();")
    return "".join(parts)


def css_sheet(urls, size=1_000_000, seed=5):
    r = _rnd(seed)
    parts = ['@import url("theme.css");@import "print.css";']
    n = 0
    while n < size:
        k = r.random()
        if k < 0.15:
            u = r.choice(urls)
            s = f".c{r.randint(0, 9999)}{{background:url({u}) no-repeat;}}"
        elif k < 0.2:
            s = (f'.i{r.randint(0, 9999)}{{background-image:image-set("a{r.randint(0, 99)}.png" 1x,'
                 f' url("b{r.randint(0, 99)}.png") 2x)}}')
        elif k < 0.22:
            s = ".d{background:url(data:image/png;base64,iVBORw0KGgo=)}"
        else:
            s = (f".x{r.randint(0, 99999)} .y{r.randint(0, 99)}{{margin:{r.randint(0, 40)}px;"
                 f"color:#{r.getrandbits(24):06x};display:flex;}}")
        parts.append(s)
        n += len(s)
    return "".join(parts)


def page_text(size=200_000, seed=6):
    r = _rnd(seed)
    out, n = [], 0
    while n < size:
        s = (" ".join(r.choice(WORDS) for _ in range(8)) if r.random() < 0.5
             else "".join(r.choice(CJK) for _ in range(12)))
        out.append(s)
        n += len(s)
    return "\n".join(out)


def mutate_text(text, seed=7):
    r = _rnd(seed)
    lines = text.split("\n")
    return "\n".join(l for l in lines if r.random() > 0.15) + "\n额外 fallback shell text"


def screenshot(path, height, seed, shift=0):
    r = _rnd(seed)
    im = Image.new("RGB", (1440, height), (250, 250, 250))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 1440, 80], fill=(30, 60, 200))
    y = 100
    while y < height:
        h = r.randint(40, 400)
        if r.random() < 0.3:
            # 照片类区域: 渐变 + 颗粒噪声(真实截图的 PNG 体积/解码成本主要来自这类区域)
            bh = min(h, height - y)
            if bh > 0:
                g = _np.random.default_rng(seed * 1000 + y)
                base = _np.linspace(0, 1, 1320)[None, :, None] * _np.array(
                    [r.randrange(256) for _ in range(3)])[None, None, :]
                tile = base + _np.linspace(0, 80, bh)[:, None, None] + g.normal(0, 12, (bh, 1320, 3))
                im.paste(Image.fromarray(_np.clip(tile, 0, 255).astype(_np.uint8)), (60 + shift, y))
        else:
            for line in range(y, min(y + h, height), 22):
                w = r.randint(200, 1300)
                d.rectangle([80 + shift, line, 80 + w + shift, line + 12], fill=(40, 40, 40))
        y += h + r.randint(10, 60)
    im = im.filter(ImageFilter.GaussianBlur(0.6))
    im.save(path)
    return path


def fresh_copy(src, dst, tag):
    """同像素、不同字节的 PNG 副本(插入 tEXt 块): 模拟修复循环每轮的新克隆截图。"""
    import struct
    import zlib
    data = Path(src).read_bytes()
    body = b"bench\x00" + str(tag).encode()
    chunk = struct.pack(">I", len(body)) + b"tEXt" + body + struct.pack(">I", zlib.crc32(b"tEXt" + body))
    Path(dst).write_bytes(data[:33] + chunk + data[33:])   # 8 签名 + 25 IHDR 之后
    return Path(dst)


def build():
    """生成(或复用)全部夹具, 返回 dict。"""
    ROOT.mkdir(exist_ok=True)
    urls = asset_urls()
    fx = {"urls": urls, "results": results_for(urls), "raw_refs": raw_refs_for(urls)}
    shots = {}
    for name, h, seed, shift in (("long_a", 9000, 11, 0), ("long_b", 8600, 12, 3),
                                 ("view_a", 900, 13, 0), ("view_b", 900, 14, 2)):
        p = ROOT / f"{name}.png"
        if not p.is_file():
            screenshot(p, h, seed, shift)
        shots[name] = p
    fx["shots"] = shots
    fx["html"] = html_page(urls)
    fx["js"] = js_bundle(urls)
    fx["css"] = css_sheet(urls)
    fx["text_a"] = page_text()
    fx["text_b"] = mutate_text(fx["text_a"])
    ws = ROOT / "ws"
    jsdir = ws / "static" / "js"
    if not jsdir.is_dir():
        jsdir.mkdir(parents=True)
        for i in range(6):   # 6 × 2MB 无框架标识 = 预算内全量扫描的最坏情况
            r = _rnd(100 + i)
            body = "".join(f"function q{r.getrandbits(32):x}(n){{return n*{r.randint(0, 99)}}}"
                           for _ in range(45_000))
            (jsdir / f"chunk{i}.js").write_text(body[:2_000_000])
    fx["ws"] = ws
    return fx
