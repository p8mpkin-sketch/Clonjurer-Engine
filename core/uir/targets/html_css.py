"""UIR → HTML/CSS 输出目标(L1 产物, 接入现有预览/导出链路)。

- 类型化字段(layout/box/text_ref/content)翻译为 flexbox/grid + CSS 变量(令牌表→:root);
- 带 raw 的节点原样回吐 tag/attrs/css/inner_html(Web 零回归逃生舱);
- 交互意图翻成最小内联行为: toggle/open-overlay 切 class, countdown 倒计时,
  submit-form 提示繁忙(与 INTERACTION_SIM 同语义), navigate 走 href;
- 产物: out_dir/index.html + out_dir/static/css/app.css, 无任何外链(离线可运行)。
"""
import html
import json
import re
from pathlib import Path

from core.uir.model import UIR, Node, Tokens, CONTAINER_ROLES
from core.uir.registry import register_output, RenderResult, BuildContext

_SAFE_ATTR = re.compile(r"^[a-zA-Z_:][-a-zA-Z0-9_:.]*$")
_URL_ATTRS = {"src", "href", "poster", "data", "action", "formaction"}
_ICONS = {  # 内联 SVG 图标(沿用既有约定: 不引外部图标库)
    "search": "<circle cx='11' cy='11' r='7'/><path d='m20 20-3.5-3.5'/>",
    "menu": "<path d='M4 6h16M4 12h16M4 18h16'/>",
    "user": "<circle cx='12' cy='8' r='4'/><path d='M4 21a8 8 0 0 1 16 0'/>",
    "cart": "<circle cx='9' cy='21' r='1'/><circle cx='20' cy='21' r='1'/><path d='M1 1h4l2.7 13.4a2 2 0 0 0 2 1.6h9.7a2 2 0 0 0 2-1.6L23 6H6'/>",
    "bell": "<path d='M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9'/><path d='M13.7 21a2 2 0 0 1-3.4 0'/>",
    "heart": "<path d='M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l8.8 8.8 8.8-8.8a5.5 5.5 0 0 0 0-7.8z'/>",
    "star": "<path d='m12 2 3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1z'/>",
    "check": "<path d='M20 6 9 17l-5-5'/>",
    "chevron": "<path d='m9 18 6-6-6-6'/>",
    "arrow": "<path d='M5 12h14M12 5l7 7-7 7'/>",
    "home": "<path d='m3 11 9-8 9 8v10a1 1 0 0 1-1 1h-5v-7h-6v7H4a1 1 0 0 1-1-1z'/>",
    "settings": "<circle cx='12' cy='12' r='3'/><path d='M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z'/>",
    "mail": "<rect x='3' y='5' width='18' height='14' rx='2'/><path d='m3 7 9 6 9-6'/>",
    "lock": "<rect x='4' y='11' width='16' height='10' rx='2'/><path d='M8 11V7a4 4 0 0 1 8 0v4'/>",
    "image": "<rect x='3' y='3' width='18' height='18' rx='2'/><circle cx='8.5' cy='8.5' r='1.5'/><path d='m21 15-5-5L5 21'/>",
    "play": "<path d='m6 4 14 8-14 8z'/>",
    "share": "<circle cx='18' cy='5' r='3'/><circle cx='6' cy='12' r='3'/><circle cx='18' cy='19' r='3'/><path d='m8.6 13.5 6.8 4M15.4 6.5l-6.8 4'/>",
    "dot": "<circle cx='12' cy='12' r='4'/>",
}

_RUNTIME_JS = r"""
(function(){
  function q(sel,root){return Array.prototype.slice.call((root||document).querySelectorAll(sel));}
  q('[data-intent]').forEach(function(el){
    var intents=[]; try{intents=JSON.parse(el.getAttribute('data-intent')||'[]');}catch(e){}
    intents.forEach(function(it){
      var ev=it.on||'click';
      if(it.do==='toggle'){el.addEventListener(ev,function(e){var t=it.target?document.getElementById(it.target):el; if(t){t.classList.toggle('uir-hidden');} e.preventDefault();});}
      else if(it.do==='open-overlay'){el.addEventListener(ev,function(e){var t=it.target?document.getElementById(it.target):null; if(t){t.classList.remove('uir-hidden');} e.preventDefault();});}
      else if(it.do==='countdown'){el.addEventListener(ev,function(e){e.preventDefault(); if(el.dataset.busy)return; var s=(it.meta&&it.meta.seconds)||60, txt=el.textContent; el.dataset.busy='1'; el.setAttribute('disabled',''); var tm=setInterval(function(){s--; el.textContent=s+'s'; if(s<=0){clearInterval(tm); el.textContent=txt; el.removeAttribute('disabled'); delete el.dataset.busy;}},1000);});}
      else if(it.do==='submit-form'){el.addEventListener(ev,function(e){e.preventDefault(); var msgs=['网络繁忙，请稍后重试','系统繁忙，请稍后再试','网络异常，请稍后重试']; var m=msgs[Math.floor(Math.random()*msgs.length)]; var tip=document.createElement('div'); tip.className='uir-toast'; tip.textContent=m; document.body.appendChild(tip); setTimeout(function(){tip.remove();},2200);});}
      else if(it.do==='carousel-advance'){el.addEventListener(ev,function(e){e.preventDefault(); var t=it.target?document.getElementById(it.target):el.parentElement; if(!t)return; var kids=q(':scope > *',t); if(!kids.length)return; var cur=kids.findIndex(function(k){return !k.classList.contains('uir-hidden');}); if(cur<0)cur=0; kids[cur].classList.add('uir-hidden'); kids[(cur+1)%kids.length].classList.remove('uir-hidden');});}
    });
  });
  q('form').forEach(function(f){ if(!f.hasAttribute('data-intent')) f.addEventListener('submit',function(e){e.preventDefault();}); });
})();
"""

_BASE_CSS = """
*,*::before,*::after{box-sizing:border-box}
html,body{margin:0;padding:0}
body{font-family:var(--font-f0,system-ui,-apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif);
     color:var(--color-c1,#111827);background:var(--color-c0,#fff);line-height:1.5;-webkit-font-smoothing:antialiased}
img{display:block;max-width:100%;height:auto}
a{color:inherit;text-decoration:none}
p{margin:0}
ul,ol{margin:0;padding:0;list-style:none}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer;border:0;background:none;padding:0}
input{border:1px solid rgba(0,0,0,.14);border-radius:6px;padding:10px 12px;background:#fff;width:100%;outline:none}
textarea{border:1px solid rgba(0,0,0,.14);background:#fff;width:100%;outline:none;resize:none;overflow:hidden;display:block;white-space:pre}
input:focus{border-color:var(--color-c2,#2563eb);box-shadow:0 0 0 3px rgba(37,99,235,.15)}
.uir-hidden{display:none!important}
.uir-toast{position:fixed;left:50%;top:14%;transform:translateX(-50%);background:rgba(17,24,39,.92);color:#fff;
           padding:10px 18px;border-radius:8px;font-size:14px;z-index:9999;box-shadow:0 8px 24px rgba(0,0,0,.25)}
.uir-ph{background:linear-gradient(135deg,#e5e7eb,#d1d5db);display:flex;align-items:center;justify-content:center;color:#6b7280;overflow:hidden}
.uir-ph svg{width:28%;height:28%;max-width:64px;max-height:64px;opacity:.55}
.uir-icon{display:inline-flex;width:1.25em;height:1.25em;flex:0 0 auto}
.uir-icon svg{width:100%;height:100%;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
hr.uir-divider{border:0;border-top:1px solid rgba(0,0,0,.1);margin:0;width:100%}
"""


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _px(v, tokens: Tokens, group: str = "space"):
    """数值/令牌 → CSS 长度; 字符串原值(如 '100%','auto')原样。"""
    v = tokens.resolve(group, v)
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return f"{v:g}px"
    return str(v)


def _sides(vals, tokens: Tokens):
    if vals is None:
        return None
    if not isinstance(vals, (list, tuple)):
        vals = [vals]
    parts = [_px(v, tokens) or "0" for v in vals[:4]]
    return " ".join(parts)


class _Css:
    def __init__(self):
        self.rules = []

    def add(self, selector: str, decls: dict):
        body = ";".join(f"{k}:{v}" for k, v in decls.items() if v not in (None, ""))
        if body:
            self.rules.append(f"{selector}{{{body}}}")

    def text(self) -> str:
        return "\n".join(self.rules)


def _node_css(node: Node, tokens: Tokens) -> dict:
    d = {}
    L, B = node.layout or {}, node.box or {}
    mode = L.get("mode") or ("flex" if node.role in CONTAINER_ROLES and node.children else None)
    if mode == "flex":
        d["display"] = "flex"
        d["flex-direction"] = "row" if L.get("direction") == "row" else "column"
        jm = {"start": "flex-start", "center": "center", "end": "flex-end",
              "between": "space-between", "around": "space-around"}
        am = {"start": "flex-start", "center": "center", "end": "flex-end", "stretch": "stretch"}
        if L.get("justify"):
            d["justify-content"] = jm.get(L["justify"], L["justify"])
        if L.get("align"):
            d["align-items"] = am.get(L["align"], L["align"])
        if L.get("wrap"):
            d["flex-wrap"] = "wrap"
    elif mode == "grid":
        d["display"] = "grid"
        cols = int(L.get("cols") or 2)
        d["grid-template-columns"] = f"repeat({cols},minmax(0,1fr))"
    elif mode == "stack":
        d["position"] = "relative"
    elif mode == "absolute":
        d["position"] = "absolute"
    if L.get("gap") is not None:
        d["gap"] = _px(L["gap"], tokens)
    if B.get("w") is not None:
        d["width"] = _px(B["w"], tokens)
    if B.get("h") is not None:
        d["height"] = _px(B["h"], tokens)
    if B.get("min_h") is not None:
        d["min-height"] = _px(B["min_h"], tokens)
    if B.get("pad") is not None:
        d["padding"] = _sides(B["pad"], tokens)
    if B.get("margin") is not None:
        d["margin"] = _sides(B["margin"], tokens)
    if B.get("bg"):
        d["background"] = tokens.resolve("color", B["bg"])
    if B.get("radius") is not None:
        d["border-radius"] = _px(B["radius"], tokens, "radius")
    if isinstance(B.get("border"), dict):
        bd = B["border"]
        d["border"] = (f"{_px(bd.get('w', 1), tokens)} {bd.get('style', 'solid')} "
                       f"{tokens.resolve('color', bd.get('color', '#e5e7eb'))}")
    if B.get("shadow"):
        d["box-shadow"] = tokens.resolve("shadow", B["shadow"])
    if B.get("opacity") is not None:
        d["opacity"] = str(B["opacity"])
    if B.get("grow"):
        d["flex"] = "1 1 0"
    if B.get("overflow"):
        d["overflow"] = str(B["overflow"])
        if B["overflow"] == "hidden":
            d["white-space"] = "nowrap"
    pos = B.get("position")
    if isinstance(pos, dict):
        d["position"] = pos.get("mode", "absolute")
        for k in ("top", "left", "right", "bottom"):
            if pos.get(k) is not None:
                d[k] = _px(pos[k], tokens)
        if pos.get("z") is not None:
            d["z-index"] = str(pos["z"])
    ts = tokens.resolve("text_style", node.text_ref) if node.text_ref else None
    if isinstance(ts, dict):
        if ts.get("size") is not None:
            d["font-size"] = _px(ts["size"], tokens)
        if ts.get("weight") is not None:
            d["font-weight"] = str(ts["weight"])
        if ts.get("line") is not None:
            d["line-height"] = str(ts["line"])
        if ts.get("color"):
            d["color"] = tokens.resolve("color", ts["color"])
        if ts.get("font"):
            f = tokens.resolve("font", ts["font"])
            if isinstance(f, dict):
                d["font-family"] = f"'{f.get('family', 'system-ui')}',{f.get('fallback', 'sans-serif')}"
        if ts.get("align"):
            d["text-align"] = ts["align"]
    if B.get("color"):
        d["color"] = tokens.resolve("color", B["color"])
    if B.get("letter_spacing") is not None:
        d["letter-spacing"] = _px(B["letter_spacing"], tokens)
    return d


def _raw_attrs(attrs: dict) -> str:
    out = []
    for k, v in (attrs or {}).items():
        if not _SAFE_ATTR.match(str(k)) or str(k).lower().startswith("on"):
            continue
        if str(k).lower() in _URL_ATTRS and re.match(r"^\s*(https?:)?//", str(v or "")):
            continue  # 离线产物不得回源
        out.append(f' {k}="{_esc(v)}"')
    return "".join(out)


class _Emitter:
    def __init__(self, uir: UIR):
        self.uir = uir
        self.tokens = uir.tokens
        self.css = _Css()
        self.has_intents = False

    def emit(self, node: Node) -> str:
        if node.raw:
            return self._emit_raw(node)
        css = _node_css(node, self.tokens)
        if css:
            self.css.add(f"#{node.id}", css)
        attrs = f' id="{_esc(node.id)}" data-role="{_esc(node.role)}"'
        if node.intents:
            self.has_intents = True
            attrs += f" data-intent='{_esc(json.dumps(node.intents, ensure_ascii=False))}'"
        c = node.content or {}
        role = node.role
        inner = "".join(self.emit(ch) for ch in node.children)
        if role == "text":
            return f"<p{attrs}>{_esc(c.get('text', ''))}</p>".replace("\n", "<br>")   # 硬换行保留
        if role == "image":
            src = c.get("src") or ""
            if src and not re.match(r"^\s*(https?:)?//", src):
                return f"<img{attrs} src=\"{_esc(src)}\" alt=\"{_esc(c.get('alt', ''))}\">"
            return (f"<div{attrs} class=\"uir-ph\" role=\"img\" aria-label=\"{_esc(c.get('alt', '图片'))}\">"
                    f"<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.6'>{_ICONS['image']}</svg></div>")
        if role == "icon":
            paths = _ICONS.get(str(c.get("icon") or "dot"), _ICONS["dot"])
            return f"<span{attrs} class=\"uir-icon\"><svg viewBox='0 0 24 24'>{paths}</svg></span>"
        if role == "button":
            return f"<button{attrs} type=\"button\">{_esc(c.get('text', ''))}{inner}</button>"
        if role == "link":
            return f"<a{attrs} href=\"{_esc(c.get('href') or '#')}\">{_esc(c.get('text', ''))}{inner}</a>"
        if role == "input":
            itype = c.get("type") or "text"
            if itype not in ("text", "password", "email", "search", "tel", "number", "url"):
                itype = "text"
            if c.get("multiline"):
                return (f"<textarea{attrs} placeholder=\"{_esc(c.get('placeholder', ''))}\">"
                        f"{_esc(c.get('text', ''))}</textarea>")
            return (f"<input{attrs} type=\"{itype}\" placeholder=\"{_esc(c.get('placeholder', ''))}\""
                    f" value=\"{_esc(c.get('text', ''))}\">")
        if role == "divider":
            return f"<hr{attrs} class=\"uir-divider\">"
        if role == "badge":
            return f"<span{attrs}>{_esc(c.get('text', ''))}</span>"
        if role == "avatar":
            return (f"<span{attrs} class=\"uir-ph\" style=\"border-radius:9999px\">"
                    f"<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.6'>{_ICONS['user']}</svg></span>")
        tag = {"nav": "nav", "header": "header", "footer": "footer", "list": "ul",
               "list_item": "li", "form": "form", "section": "section", "table": "div"}.get(role, "div")
        text = f"<span>{_esc(c['text'])}</span>" if c.get("text") else ""
        return f"<{tag}{attrs}>{text}{inner}</{tag}>"

    def _emit_raw(self, node: Node) -> str:
        r = node.raw
        tag = str(r.get("tag") or "div").lower()
        if not re.match(r"^[a-z][a-z0-9-]*$", tag) or tag in ("script", "iframe", "object", "embed", "link"):
            tag = "div"
        attrs = _raw_attrs(r.get("attrs") or {})
        if r.get("css"):
            self.css.add(f"#{node.id}", {"__raw": None})
            self.css.rules.append(f"#{node.id}{{{r['css']}}}")
        if "id" not in (r.get("attrs") or {}):
            attrs = f' id="{_esc(node.id)}"' + attrs
        inner = r.get("inner_html") or "".join(self.emit(ch) for ch in node.children)
        if tag in ("img", "br", "hr", "input", "meta"):
            return f"<{tag}{attrs}>"
        return f"<{tag}{attrs}>{inner}</{tag}>"


def _root_vars(tokens: Tokens) -> str:
    lines = []
    for k, v in tokens.color.items():
        lines.append(f"--color-{k}:{v}")
    for k, v in tokens.font.items():
        if isinstance(v, dict):
            lines.append(f"--font-{k}:'{v.get('family', 'system-ui')}',{v.get('fallback', 'sans-serif')}")
    for k, v in tokens.space.items():
        lines.append(f"--space-{k}:{v}px")
    return (":root{" + ";".join(lines) + "}" if lines else "") + _metric_faces(tokens)


def _metric_faces(tokens: Tokens) -> str:
    """字体令牌带 metric_local(字宽相近的本机字体名列表)时, 给该字族补一组 @font-face: 本机装着原字体就用原字体,
    没装则按列表顺序取第一个本机有的字宽相近字体 —— 否则浏览器落到通用默认字体, 字宽差一成, 单行文本被截断/折行。"""
    out = []
    for v in tokens.font.values():
        names = v.get("metric_local") if isinstance(v, dict) else None
        fam = str(v.get("family") or "") if isinstance(v, dict) else ""
        if not names or not re.match(r"^[A-Za-z0-9 _-]+$", fam):
            continue
        names = [str(n) for n in names if re.match(r"^[A-Za-z0-9 _-]+$", str(n))]
        for weight, suffix in (("100 500", ""), ("600 900", " Bold")):
            src = ",".join(f'local("{n}{suffix}")' for n in [fam] + names)
            out.append(f'@font-face{{font-family:"{fam}";font-weight:{weight};src:{src}}}')
    return "".join(out)


def render_html(uir: UIR) -> tuple:
    """UIR → (index_html, app_css)。纯函数, 便于测试。"""
    uir.normalize()
    em = _Emitter(uir)
    body = em.emit(uir.root)
    css = _BASE_CSS + "\n" + _root_vars(uir.tokens) + "\n" + em.css.text()
    vp = uir.meta.get("viewport") or {}
    title = _esc(uir.meta.get("title") or "Generated Page")
    lang = _esc(uir.meta.get("lang") or "zh")
    js = f"<script>{_RUNTIME_JS}</script>" if em.has_intents else ""
    page = (f"<!DOCTYPE html>\n<html lang=\"{lang}\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<meta name=\"generator\" content=\"clonjurer-uir/1\">"
            f"<title>{title}</title><link rel=\"stylesheet\" href=\"static/css/app.css\"></head>"
            f"<body data-uir-w=\"{int(vp.get('w') or 0)}\">{body}{js}</body></html>")
    return page, css


@register_output("html_css")
class HtmlCssTarget:
    shootable = True

    def render(self, uir: UIR, out_dir: Path, ctx: BuildContext = None) -> RenderResult:
        out_dir = Path(out_dir)
        (out_dir / "static" / "css").mkdir(parents=True, exist_ok=True)
        if uir.root.raw and uir.root.raw.get("document"):
            # Web 直通逃生舱(DESIGN.md §9.4): 克隆 DOM 原文逐字节回吐, 零回归闸门的被测路径
            page = uir.root.raw["document"]
            (out_dir / "index.html").write_text(page, encoding="utf-8")
            (out_dir / "uir.json").write_text(uir.to_json(indent=2), encoding="utf-8")
            return RenderResult(entry_file=out_dir / "index.html", shootable=True,
                                assets=["uir.json"], meta={"passthrough": True, "bytes": len(page)})
        page, css = render_html(uir)
        (out_dir / "index.html").write_text(page, encoding="utf-8")
        (out_dir / "static" / "css" / "app.css").write_text(css, encoding="utf-8")
        (out_dir / "uir.json").write_text(uir.to_json(indent=2), encoding="utf-8")
        return RenderResult(entry_file=out_dir / "index.html", shootable=True,
                            assets=["static/css/app.css", "uir.json"],
                            meta={"nodes": uir.root.count(), "bytes": len(page) + len(css)})
