"""UIR v1 数据模型: dataclass + 轻量自校验 + (反)序列化。

权威结构见 core/uir/schema.json 与 DESIGN.md §9.3。不引第三方 jsonschema:
校验器只检查闭集字段、id 唯一、令牌引用可解析(或为原值覆盖)。

令牌引用 + 原值覆盖(取舍 2): 节点样式字段既可以是令牌键("c0"/"s2"/"t0"),
也可以直接是原值("#fff"/8/{...}); `resolve()` 两者都认。
"""
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.json")

ROLES = ("box", "text", "image", "icon", "button", "input", "link", "nav", "list",
         "list_item", "card", "header", "footer", "hero", "divider", "overlay",
         "badge", "avatar", "table", "form", "section")
LAYOUT_MODES = ("flow", "flex", "grid", "stack", "absolute")
INTENT_DO = ("navigate", "toggle", "open-overlay", "submit-form", "countdown",
             "carousel-advance", "input", "none")
INTENT_ON = ("click", "submit", "input", "change", "hover")
SOURCE_KINDS = ("web_dom", "vision", "description", "apk_static", "a11y")

# 容器类角色: 默认按 flex 列布局渲染
CONTAINER_ROLES = {"box", "nav", "list", "list_item", "card", "header", "footer",
                   "hero", "overlay", "table", "form", "section"}
# 叶子类角色: 不应有子节点(有也只渲染内容)
LEAF_ROLES = {"text", "image", "icon", "input", "divider", "badge", "avatar"}

_COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\(|hsla?\(|(?:linear|radial)-gradient\(|transparent$|[a-z]+$)")


@dataclass
class Tokens:
    color: dict = field(default_factory=dict)
    font: dict = field(default_factory=dict)
    text_style: dict = field(default_factory=dict)
    space: dict = field(default_factory=dict)
    radius: dict = field(default_factory=dict)
    shadow: dict = field(default_factory=dict)

    def resolve(self, group: str, value, default=None):
        """令牌键 → 值; 非令牌键视为原值覆盖原样返回; None → default。"""
        if value is None:
            return default
        table = getattr(self, group, None) or {}
        if isinstance(value, str) and value in table:
            return table[value]
        return value

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v}

    @classmethod
    def from_dict(cls, d: dict) -> "Tokens":
        d = d or {}
        return cls(**{k: dict(d.get(k) or {}) for k in
                      ("color", "font", "text_style", "space", "radius", "shadow")})


@dataclass
class Node:
    role: str = "box"
    id: str = ""
    layout: dict = field(default_factory=dict)
    box: dict = field(default_factory=dict)
    text_ref: str = ""
    content: dict = field(default_factory=dict)
    intents: list = field(default_factory=list)
    children: list = field(default_factory=list)
    raw: dict = None

    def to_dict(self) -> dict:
        out = {"id": self.id, "role": self.role}
        if self.layout:
            out["layout"] = self.layout
        if self.box:
            out["box"] = self.box
        if self.text_ref:
            out["text_ref"] = self.text_ref
        if self.content:
            out["content"] = self.content
        if self.intents:
            out["intents"] = self.intents
        if self.children:
            out["children"] = [c.to_dict() for c in self.children]
        if self.raw:
            out["raw"] = self.raw
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Node":
        if not isinstance(d, dict):
            raise ValueError("node 必须是对象")
        return cls(
            role=str(d.get("role") or "box"),
            id=str(d.get("id") or ""),
            layout=dict(d.get("layout") or {}),
            box=dict(d.get("box") or {}),
            text_ref=str(d.get("text_ref") or ""),
            content=dict(d.get("content") or {}),
            intents=list(d.get("intents") or []),
            children=[cls.from_dict(c) for c in (d.get("children") or [])],
            raw=dict(d["raw"]) if isinstance(d.get("raw"), dict) else None,
        )

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def count(self) -> int:
        return sum(1 for _ in self.walk())


@dataclass
class UIR:
    root: Node
    tokens: Tokens = field(default_factory=Tokens)
    meta: dict = field(default_factory=dict)
    uir_version: str = "1"

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"uir_version": self.uir_version, "meta": self.meta,
                "tokens": self.tokens.to_dict(), "root": self.root.to_dict()}

    def to_json(self, indent=None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, d: dict) -> "UIR":
        if not isinstance(d, dict) or "root" not in d:
            raise ValueError("UIR 必须是含 root 的对象")
        return cls(root=Node.from_dict(d["root"]), tokens=Tokens.from_dict(d.get("tokens")),
                   meta=dict(d.get("meta") or {}), uir_version=str(d.get("uir_version") or "1"))

    @classmethod
    def from_json(cls, text: str) -> "UIR":
        return cls.from_dict(json.loads(text))

    # ------------------------------------------------------------------
    def normalize(self) -> "UIR":
        """补齐缺失 id(n1, n2, ...), 去重 id, 合法化角色; 原地修改并返回自身。"""
        seen, counter = set(), [0]

        def fresh():
            counter[0] += 1
            while f"n{counter[0]}" in seen:
                counter[0] += 1
            return f"n{counter[0]}"

        for node in self.root.walk():
            if node.role not in ROLES:
                node.role = "box"
            if not node.id or node.id in seen:
                node.id = fresh()
            seen.add(node.id)
            # 对生成侧(LLM)常见漂移做宽松归一化, 而不是整树拒绝回落:
            mode = node.layout.get("mode")
            if mode in ("row", "col", "column", "horizontal", "vertical"):
                node.layout["mode"] = "flex"
                node.layout.setdefault("direction", "row" if mode in ("row", "horizontal") else "col")
            elif mode in ("block", "inline", "none", ""):
                node.layout.pop("mode", None)
            elif mode and mode not in LAYOUT_MODES:
                node.layout["mode"] = "flex"
            if node.layout.get("direction") in ("column", "vertical"):
                node.layout["direction"] = "col"
            elif node.layout.get("direction") in ("horizontal",):
                node.layout["direction"] = "row"
            kept = []
            for it in node.intents:
                if not isinstance(it, dict):
                    continue
                if it.get("do") not in INTENT_DO:
                    it["do"] = {"submit": "submit-form", "tap": "navigate", "press": "navigate", "link": "navigate",
                                "open": "open-overlay", "close": "toggle", "focus": "input", "type": "input"}.get(str(it.get("do")), "none")
                if it.get("on") and it["on"] not in INTENT_ON:
                    it["on"] = "click"
                kept.append(it)
            node.intents = kept
        self.uir_version = "1"
        return self

    def validate(self) -> list:
        """返回错误列表(空 = 合法)。只做结构/闭集/引用检查, 不做语义判断。"""
        errors = []
        if self.uir_version != "1":
            errors.append(f"uir_version 必须为 '1', 得到 {self.uir_version!r}")
        kind = self.meta.get("source_kind")
        if kind and kind not in SOURCE_KINDS:
            errors.append(f"meta.source_kind 非法: {kind!r}")
        ids = set()
        for node in self.root.walk():
            if node.role not in ROLES:
                errors.append(f"节点 {node.id!r} role 非法: {node.role!r}")
            if not node.id:
                errors.append("存在缺失 id 的节点(先调用 normalize)")
            elif node.id in ids:
                errors.append(f"id 重复: {node.id!r}")
            ids.add(node.id)
            mode = node.layout.get("mode")
            if mode and mode not in LAYOUT_MODES:
                errors.append(f"节点 {node.id!r} layout.mode 非法: {mode!r}")
            for it in node.intents:
                if not isinstance(it, dict) or it.get("do") not in INTENT_DO:
                    errors.append(f"节点 {node.id!r} 交互意图非法: {it!r}")
                elif it.get("on") and it["on"] not in INTENT_ON:
                    errors.append(f"节点 {node.id!r} intents.on 非法: {it.get('on')!r}")
            if node.text_ref and node.text_ref not in self.tokens.text_style:
                errors.append(f"节点 {node.id!r} text_ref 未定义: {node.text_ref!r}")
            bg = node.box.get("bg")
            if isinstance(bg, str) and bg and bg not in self.tokens.color and not _COLOR_RE.match(bg):
                errors.append(f"节点 {node.id!r} box.bg 既非令牌也非颜色原值: {bg!r}")
        return errors

    def stats(self) -> dict:
        roles = {}
        for node in self.root.walk():
            roles[node.role] = roles.get(node.role, 0) + 1
        return {"nodes": self.root.count(), "roles": roles,
                "tokens": {k: len(v) for k, v in self.tokens.to_dict().items()}}


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
