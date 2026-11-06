"""现有克隆产物(DOM 权威快照) → UIR(DESIGN.md §9.4, Web 平滑迁入的证明)。

不做语义识别: 整份文档进 root.raw.document(直通逃生舱), 同时把 <body> 的顶层
子元素拆成 raw 节点(tag/attrs/inner_html)供结构视图。HtmlCss 目标遇 raw.document
原样回吐 → 与克隆直出逐字节等价 → 评分 v3 对同一对图打分逐值一致(零回归闸门)。
"""
import re
from pathlib import Path

from core.uir.model import UIR, Node, Tokens
from core.uir.registry import register_input, GenRequest, BuildContext

_BODY_RE = re.compile(r"<body\b[^>]*>(.*)</body\s*>", re.S | re.I)
_TOP_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9-]*)(\s[^>]*)?>", re.S)
_ATTR_RE = re.compile(r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_LANG_RE = re.compile(r"<html[^>]*\blang=[\"']?([a-zA-Z-]+)", re.I)
_COLOR_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")


def _attrs(s: str) -> dict:
    out = {}
    for m in _ATTR_RE.finditer(s or ""):
        out[m.group(1)] = m.group(2) if m.group(2) is not None else (m.group(3) or m.group(4) or "")
    return out


def _top_level_children(body_html: str, limit: int = 200) -> list:
    """浅层切分 <body> 的顶层元素(容错: 标签不配对时整段落入一个节点)。"""
    nodes, pos, n = [], 0, len(body_html)
    depth_stack = []
    i = 0
    start = None
    while i < n and len(nodes) < limit:
        m = _TOP_TAG_RE.search(body_html, i)
        if not m:
            break
        tag = m.group(1).lower()
        if body_html.startswith("</", m.start()) or body_html[m.start():m.start() + 2] == "<!":
            i = m.end()
            continue
        if start is None:
            start = m.start()
        self_closing = (m.group(0).endswith("/>") or tag in ("br", "hr", "img", "input", "meta", "link"))
        if not self_closing:
            close = re.compile(rf"</{tag}\s*>", re.I)
            # 找匹配的关闭标签(同名嵌套计数)
            j, level = m.end(), 1
            open_re = re.compile(rf"<{tag}\b[^>]*?(?<!/)>", re.I)
            while level and j < n:
                mo, mc = open_re.search(body_html, j), close.search(body_html, j)
                if not mc:
                    j = n
                    break
                if mo and mo.start() < mc.start():
                    level += 1
                    j = mo.end()
                else:
                    level -= 1
                    j = mc.end()
            end = j
        else:
            end = m.end()
        if start is not None and depth_stack == []:
            seg = body_html[start:end]
            inner = seg[m.end() - start:] if self_closing else seg[m.end() - start: end - start - (len(tag) + 3)]
            nodes.append(Node(role="box", raw={"tag": tag, "attrs": _attrs(m.group(2) or ""),
                                              "inner_html": inner if not self_closing else ""}))
        i = end
        start = None
    return nodes


def from_clone_workspace(ws, page_file: str = "index.html") -> UIR:
    ws = Path(ws)
    html_path = ws / page_file
    doc = html_path.read_text(encoding="utf-8", errors="replace")
    body_m = _BODY_RE.search(doc)
    body = body_m.group(1) if body_m else doc
    children = _top_level_children(body)
    colors = {}
    for i, c in enumerate(dict.fromkeys(_COLOR_RE.findall(doc)[:64])):
        colors[f"c{i}"] = c.lower()
    title_m, lang_m = _TITLE_RE.search(doc), _LANG_RE.search(doc)
    root = Node(role="box", raw={"document": doc, "tag": "body"}, children=children)
    uir = UIR(root=root, tokens=Tokens(color=colors),
              meta={"source_kind": "web_dom", "title": (title_m.group(1).strip() if title_m else ""),
                    "lang": (lang_m.group(1) if lang_m else "zh"), "page_file": page_file,
                    "workspace": str(ws)})
    return uir.normalize()


@register_input("web_dom")
class WebDomAdapter:
    def to_uir(self, req: GenRequest, ctx: BuildContext) -> UIR:
        uir = from_clone_workspace(req.source_path, req.meta.get("page_file", "index.html"))
        ctx.log("UIR", f"克隆 DOM→UIR: 顶层 {len(uir.root.children)} 节点, 色值 {len(uir.tokens.color)}")
        return uir
