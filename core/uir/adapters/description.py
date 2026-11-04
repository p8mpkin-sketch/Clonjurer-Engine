"""文字描述 → UIR。

Tier 0(确定性, 始终可用): 通用页面原型库(登录/落地/仪表盘/列表/文章 + 通用兜底)。
原型是 UI 设计系统级的通用版式, 不是对任何站点/应用的特判; 描述中的关键词决定
原型与配色/文案槽位, 描述里出现的短语会被填入标题/按钮/字段等内容槽。
Tier 1(LLM, 有 Key 时): 让模型直接产出 UIR JSON, 校验失败则回落 Tier 0。
"""
import re

from core.uir import llm
from core.uir.model import UIR, Node, Tokens
from core.uir.registry import register_input, GenRequest, BuildContext

_ARCHETYPE_KEYS = {
    "login": ("登录", "注册", "sign in", "login", "signin", "密码", "验证码", "账号"),
    "dashboard": ("仪表盘", "dashboard", "后台", "统计", "图表", "kpi", "监控", "数据看板", "管理台"),
    "list": ("商品", "电商", "列表", "购物", "product", "shop", "店铺", "商城", "价格", "加入购物车"),
    "article": ("文章", "博客", "新闻", "正文", "article", "blog", "post", "阅读", "资讯"),
    "landing": ("落地页", "官网", "首页", "landing", "hero", "产品介绍", "宣传", "营销", "下载", "立即体验"),
}
_COLOR_WORDS = {
    "蓝": "#2563eb", "blue": "#2563eb", "紫": "#7c3aed", "purple": "#7c3aed", "绿": "#059669",
    "green": "#059669", "红": "#dc2626", "red": "#dc2626", "橙": "#ea580c", "orange": "#ea580c",
    "黑": "#111827", "dark": "#111827", "深色": "#0f172a", "金": "#ca8a04", "粉": "#db2777",
}
_QUOTE_RE = re.compile(r"[「“\"']([^」”\"']{1,24})[」”\"']")
_BRAND_RE = re.compile(r"(?:叫|名为|品牌|名称是|名字是|called|named)\s*[:：]?\s*([A-Za-z0-9一-龥]{2,16})")
_NUM_RE = re.compile(r"(\d{1,2})\s*(?:个|张|列|栏|条|项|块)")


def detect_archetype(desc: str) -> str:
    d = (desc or "").lower()
    best, best_n = "landing", 0
    for name, keys in _ARCHETYPE_KEYS.items():
        n = sum(d.count(k.lower()) for k in keys)
        if n > best_n:
            best, best_n = name, n
    return best


def _accent(desc: str) -> str:
    d = (desc or "").lower()
    for w, c in _COLOR_WORDS.items():
        if w in d:
            return c
    return "#2563eb"


def _brand(desc: str, default: str) -> str:
    m = _BRAND_RE.search(desc or "")
    if m:
        return m.group(1)
    m = _QUOTE_RE.search(desc or "")
    return m.group(1) if m else default


def _dark(desc: str) -> bool:
    return any(k in (desc or "").lower() for k in ("深色", "暗色", "dark", "黑色背景"))


def base_tokens(accent: str, dark: bool = False) -> Tokens:
    return Tokens(
        color={"c0": "#0b1220" if dark else "#ffffff", "c1": "#e5e7eb" if dark else "#111827",
               "c2": accent, "c3": "#94a3b8" if dark else "#6b7280",
               "c4": "#111a2e" if dark else "#f3f4f6", "c5": "#ffffff", "c6": "#1f2a44" if dark else "#e5e7eb"},
        font={"f0": {"family": "system-ui", "fallback": "-apple-system,'PingFang SC','Microsoft YaHei',sans-serif"}},
        text_style={"t0": {"size": 15, "weight": 400, "line": 1.6, "color": "c1"},
                    "t1": {"size": 40, "weight": 700, "line": 1.2, "color": "c1"},
                    "t2": {"size": 24, "weight": 600, "line": 1.3, "color": "c1"},
                    "t3": {"size": 13, "weight": 400, "line": 1.5, "color": "c3"},
                    "t4": {"size": 15, "weight": 600, "line": 1.2, "color": "c5", "align": "center"},
                    "t5": {"size": 18, "weight": 600, "line": 1.3, "color": "c1"},
                    "t6": {"size": 28, "weight": 700, "line": 1.2, "color": "c1"}},
        space={"s0": 0, "s1": 4, "s2": 8, "s3": 12, "s4": 16, "s5": 24, "s6": 32, "s7": 48, "s8": 64},
        radius={"r0": 0, "r1": 6, "r2": 12, "r3": 9999},
        shadow={"sh0": "0 1px 2px rgba(0,0,0,.06)", "sh1": "0 10px 30px rgba(0,0,0,.08)"},
    )


def T(text, ref="t0", **box):
    return Node(role="text", text_ref=ref, content={"text": text}, box=box)


def Btn(text, primary=True, intent="submit-form", **extra):
    box = {"pad": ["s3", "s5"], "radius": "r1", "bg": "c2" if primary else "c4"}
    box.update(extra)
    return Node(role="button", text_ref="t4" if primary else "t0", content={"text": text},
                box=box, intents=[{"on": "click", "do": intent}])


def Input(placeholder, itype="text"):
    return Node(role="input", content={"placeholder": placeholder, "type": itype},
                intents=[{"on": "input", "do": "input"}])


def Img(h, alt="图片", **box):
    b = {"h": h, "w": "100%", "radius": "r2"}
    b.update(box)
    return Node(role="image", content={"alt": alt}, box=b)


def Navbar(brand, links, cta=None):
    items = [Node(role="link", content={"text": l, "href": "#"}, text_ref="t0",
                  intents=[{"on": "click", "do": "navigate"}]) for l in links]
    right = [Node(role="box", layout={"mode": "flex", "direction": "row", "gap": "s5", "align": "center"},
                  children=items)]
    if cta:
        right.append(Btn(cta, intent="navigate", pad=["s2", "s4"]))
    return Node(role="header", layout={"mode": "flex", "direction": "row", "justify": "between", "align": "center"},
                box={"pad": ["s4", "s7"], "bg": "c0", "border": {"w": 1, "color": "c6"}},
                children=[T(brand, "t5"),
                          Node(role="box", layout={"mode": "flex", "direction": "row", "gap": "s5", "align": "center"},
                               children=right)])


def Footer(brand):
    return Node(role="footer", layout={"mode": "flex", "direction": "row", "justify": "between", "align": "center"},
                box={"pad": ["s5", "s7"], "bg": "c4"},
                children=[T(f"© 2026 {brand}", "t3"),
                          Node(role="box", layout={"mode": "flex", "direction": "row", "gap": "s4"},
                               children=[T(x, "t3") for x in ("关于我们", "隐私政策", "联系方式")])])


def Card(children, **box):
    b = {"pad": ["s5"], "bg": "c0", "radius": "r2", "border": {"w": 1, "color": "c6"}, "shadow": "sh0"}
    b.update(box)
    return Node(role="card", layout={"mode": "flex", "direction": "col", "gap": "s3"}, box=b, children=children)


# ---------------------------------------------------------------------------
def _login(desc, brand, phrases, n=0):
    fields = [Input("手机号 / 邮箱", "text"), Input("密码", "password")]
    if any(k in desc for k in ("验证码", "短信", "sms", "otp")):
        fields.append(Node(role="box", layout={"mode": "flex", "direction": "row", "gap": "s2"},
                           children=[Input("短信验证码"), Btn("获取验证码", primary=False, intent="countdown",
                                                              pad=["s3", "s4"], w=130)]))
    title = phrases[0] if phrases else f"登录 {brand}"
    form = Node(role="form", layout={"mode": "flex", "direction": "col", "gap": "s4"},
                box={"w": 400, "pad": ["s7"], "bg": "c0", "radius": "r2", "shadow": "sh1"},
                children=[T(title, "t2"), T(_lead(desc) or "欢迎回来, 请输入账号信息", "t3"), *fields,
                          Node(role="box", layout={"mode": "flex", "direction": "row", "justify": "between"},
                               children=[T("记住我", "t3"), T("忘记密码?", "t3")]),
                          Btn("登 录"),
                          Node(role="box", layout={"mode": "flex", "direction": "row", "justify": "center", "gap": "s1"},
                               children=[T("还没有账号?", "t3"), T("立即注册", "t3")])])
    root = Node(role="box", layout={"mode": "flex", "direction": "col", "justify": "center", "align": "center"},
                box={"min_h": "100vh", "bg": "c4", "pad": ["s8", "s5"]}, children=[form])
    return root


def _landing(desc, brand, phrases, n):
    heads = phrases or [f"让 {brand} 为你的业务提速", "一站式解决方案"]
    feats = ["极速部署", "安全可靠", "弹性扩展", "全球加速", "智能分析", "专家支持"][:max(3, min(6, n or 3))]
    hero = Node(role="hero", layout={"mode": "flex", "direction": "row", "align": "center", "gap": "s8"},
                box={"pad": ["s8", "s7"], "bg": "c0"},
                children=[Node(role="box", layout={"mode": "flex", "direction": "col", "gap": "s4"}, box={"grow": 1},
                               children=[T(heads[0], "t1"),
                                         T(_lead(desc) or "面向企业的新一代产品, 开箱即用, 三分钟上线。", "t0"),
                                         Node(role="box", layout={"mode": "flex", "direction": "row", "gap": "s3"},
                                              children=[Btn("立即体验", intent="navigate"),
                                                        Btn("了解更多", primary=False, intent="navigate")])]),
                          Img(340, "产品示意", grow=1)])
    grid = Node(role="section", layout={"mode": "grid", "cols": 3, "gap": "s5"}, box={"pad": ["s7"], "bg": "c4"},
                children=[Card([Node(role="icon", content={"icon": i}, box={"color": "c2"}),
                                T(f, "t5"), T("清晰的价值主张与可量化的收益说明。", "t3")])
                          for f, i in zip(feats, ("check", "lock", "arrow", "share", "star", "settings"))])
    cta = Node(role="section", layout={"mode": "flex", "direction": "col", "align": "center", "gap": "s4"},
               box={"pad": ["s8", "s7"], "bg": "c2"},
               children=[Node(role="text", text_ref="t6", content={"text": "现在开始, 免费试用 14 天"}, box={"color": "c5"}),
                         Btn("免费注册", primary=False, intent="navigate")])
    return Node(role="box", layout={"mode": "flex", "direction": "col"}, box={"bg": "c0"},
                children=[Navbar(brand, ["产品", "方案", "定价", "文档"], "开始使用"), hero, grid, cta, Footer(brand)])


def _dashboard(desc, brand, phrases, n):
    kpis = [("今日访问", "128,430"), ("转化率", "4.8%"), ("活跃用户", "32,019"), ("营收", "¥ 1.2M")]
    side = Node(role="nav", layout={"mode": "flex", "direction": "col", "gap": "s1"},
                box={"w": 220, "pad": ["s5", "s4"], "bg": "#0f172a", "min_h": "100vh"},
                children=[Node(role="text", text_ref="t5", content={"text": brand}, box={"color": "c5", "pad": ["s0", "s2", "s5", "s2"]})]
                + [Node(role="link", text_ref="t0", content={"text": x, "href": "#"},
                        box={"pad": ["s2", "s3"], "radius": "r1", "color": "#cbd5e1",
                             "bg": "#1e293b" if i == 0 else None}, intents=[{"on": "click", "do": "navigate"}])
                   for i, x in enumerate(["概览", "数据分析", "订单", "用户", "设置"])])
    top = Node(role="header", layout={"mode": "flex", "direction": "row", "justify": "between", "align": "center"},
               box={"pad": ["s4", "s6"], "bg": "c0", "border": {"w": 1, "color": "c6"}},
               children=[Node(role="box", layout={"mode": "flex", "direction": "col", "gap": "s1"},
                              children=[T(phrases[0] if phrases else "运营概览", "t2"), T(_lead(desc), "t3")]),
                         Node(role="box", layout={"mode": "flex", "direction": "row", "gap": "s3", "align": "center"},
                              children=[Node(role="input", content={"placeholder": "搜索…", "type": "search"}, box={"w": 220}),
                                        Node(role="icon", content={"icon": "bell"}), Node(role="avatar", box={"w": 32, "h": 32})])])
    kpi_row = Node(role="section", layout={"mode": "grid", "cols": 4, "gap": "s4"}, box={"pad": ["s5", "s6", "s0", "s6"]},
                   children=[Card([T(k, "t3"), T(v, "t6")]) for k, v in kpis])
    charts = Node(role="section", layout={"mode": "grid", "cols": 3, "gap": "s4"}, box={"pad": ["s5", "s6"]},
                  children=[Card([T("流量趋势", "t5"), Img(260, "折线图", radius="r1")], grow=1),
                            Card([T("渠道占比", "t5"), Img(260, "饼图", radius="r1")])])
    charts.children[0].box["gridspan"] = 2
    table = Node(role="table", layout={"mode": "flex", "direction": "col"}, box={"margin": ["s0", "s6", "s6", "s6"], "bg": "c0", "radius": "r2", "border": {"w": 1, "color": "c6"}},
                 children=[Node(role="box", layout={"mode": "flex", "direction": "row", "justify": "between"},
                                box={"pad": ["s3", "s4"], "bg": "c4", "border": {"w": 1, "color": "c6"}},
                                children=[T(h, "t3") for h in ("订单号", "客户", "金额", "状态", "时间")])]
                 + [Node(role="box", layout={"mode": "flex", "direction": "row", "justify": "between"},
                         box={"pad": ["s3", "s4"], "border": {"w": 1, "color": "c6"}},
                         children=[T(f"#{20261100 + i}", "t0"), T(f"客户 {i + 1}", "t0"), T(f"¥ {(i + 3) * 129}", "t0"),
                                   Node(role="badge", content={"text": "已完成" if i % 3 else "处理中"},
                                        box={"pad": ["s1", "s2"], "radius": "r3", "bg": "#dcfce7" if i % 3 else "#fef3c7"}, text_ref="t3"),
                                   T(f"11-0{i % 4 + 1} 10:{10 + i}", "t3")]) for i in range(6)])
    main = Node(role="box", layout={"mode": "flex", "direction": "col"}, box={"grow": 1, "bg": "c4", "min_h": "100vh"},
                children=[top, kpi_row, charts, table])
    return Node(role="box", layout={"mode": "flex", "direction": "row"}, children=[side, main])


def _list(desc, brand, phrases, n):
    count = max(6, min(12, (n or 8)))
    cards = [Card([Img(180, f"商品 {i + 1}", radius="r1"), T(f"商品名称 {i + 1} · 热销款", "t5"),
                   T("简洁的卖点描述, 突出核心参数与适用场景。", "t3"),
                   Node(role="box", layout={"mode": "flex", "direction": "row", "justify": "between", "align": "center"},
                        children=[Node(role="text", text_ref="t5", content={"text": f"¥ {(i + 1) * 99}"}, box={"color": "#dc2626"}),
                                  Btn("加入购物车", pad=["s2", "s3"], intent="submit-form")])])
             for i in range(count)]
    filters = Node(role="nav", layout={"mode": "flex", "direction": "row", "gap": "s2", "wrap": True},
                   box={"pad": ["s4", "s7"], "bg": "c0", "border": {"w": 1, "color": "c6"}},
                   children=[Node(role="badge", text_ref="t0", content={"text": x},
                                  box={"pad": ["s2", "s4"], "radius": "r3", "bg": "c2" if i == 0 else "c4",
                                       "color": "c5" if i == 0 else "c1"})
                             for i, x in enumerate(["全部", "新品", "热销", "折扣", "数码", "家居", "服饰"])])
    grid = Node(role="list", layout={"mode": "grid", "cols": 4, "gap": "s5"}, box={"pad": ["s6", "s7"], "bg": "c4"},
                children=cards)
    return Node(role="box", layout={"mode": "flex", "direction": "col"},
                children=[Navbar(brand, ["首页", "分类", "活动", "我的"], "购物车"),
                          Node(role="section", layout={"mode": "flex", "direction": "col", "gap": "s2"}, box={"pad": ["s6", "s7", "s4", "s7"]},
                               children=[T(phrases[0] if phrases else "全部商品", "t2"), T(_lead(desc) or f"共 {count} 件商品 · 按综合排序", "t3")]),
                          filters, grid, Footer(brand)])


def _article(desc, brand, phrases, n):
    paras = ["这是文章的首段导语, 用一到两句话交代背景与核心观点, 引导读者继续阅读。",
             "正文第二段展开论述: 给出事实、数据或案例, 段落之间保持逻辑递进, 句子长度错落有致。",
             "第三段承接上文, 补充不同视角的分析, 并用过渡句把话题引向结论部分。",
             "结尾段总结要点并给出可操作的建议, 留下一个值得思考的问题, 让读者有所收获。"]
    body = Node(role="section", layout={"mode": "flex", "direction": "col", "gap": "s4"}, box={"w": 760, "pad": ["s7", "s0"]},
                children=[Node(role="badge", text_ref="t3", content={"text": "技术 · 深度"}, box={"pad": ["s1", "s3"], "radius": "r3", "bg": "c4", "w": 110}),
                          T(phrases[0] if phrases else "一篇关于产品设计与工程实践的长文", "t1"),
                          T(_lead(desc), "t3"),
                          Node(role="box", layout={"mode": "flex", "direction": "row", "gap": "s3", "align": "center"},
                               children=[Node(role="avatar", box={"w": 36, "h": 36}), T("作者 · 2026-11-04 · 阅读 8 分钟", "t3")]),
                          Img(380, "题图"), *[T(p, "t0") for p in paras],
                          T("小标题: 关键结论", "t2"), *[T(p, "t0") for p in paras[:2]],
                          Node(role="divider"),
                          Node(role="box", layout={"mode": "flex", "direction": "row", "gap": "s3"},
                               children=[Btn("点赞", primary=False, pad=["s2", "s4"]), Btn("收藏", primary=False, pad=["s2", "s4"]),
                                         Btn("分享", primary=False, pad=["s2", "s4"])])])
    aside = Node(role="box", layout={"mode": "flex", "direction": "col", "gap": "s4"}, box={"w": 300, "pad": ["s7", "s0"]},
                 children=[Card([T("相关阅读", "t5"), *[T(f"· 推荐文章标题 {i + 1}", "t3") for i in range(5)]]),
                           Card([T("热门标签", "t5"), Node(role="box", layout={"mode": "flex", "direction": "row", "gap": "s2", "wrap": True},
                                                       children=[Node(role="badge", text_ref="t3", content={"text": t}, box={"pad": ["s1", "s3"], "radius": "r3", "bg": "c4"})
                                                                 for t in ("设计", "工程", "产品", "效率", "团队")])])])
    return Node(role="box", layout={"mode": "flex", "direction": "col"}, box={"bg": "c0"},
                children=[Navbar(brand, ["首页", "专栏", "标签", "关于"]),
                          Node(role="box", layout={"mode": "flex", "direction": "row", "gap": "s8", "justify": "center"}, box={"pad": ["s0", "s7"]},
                               children=[body, aside]),
                          Footer(brand)])


_BUILDERS = {"login": _login, "landing": _landing, "dashboard": _dashboard, "list": _list, "article": _article}


def _lead(desc: str, limit: int = 90) -> str:
    """描述的首句作为页面导语(副标题), 让描述里的内容词真实出现在产物文案中。"""
    first = re.split(r"[。！？!?\n]", (desc or "").strip())[0]
    return first[:limit]


def build_from_description(desc: str, viewport: dict = None) -> UIR:
    desc = (desc or "").strip()
    arch = detect_archetype(desc)
    accent, dark = _accent(desc), _dark(desc)
    brand = _brand(desc, {"login": "示例系统", "landing": "Acme", "dashboard": "控制台",
                          "list": "好物商城", "article": "字里行间"}[arch])
    phrases = [p for p in _QUOTE_RE.findall(desc) if p != brand][:3]
    m = _NUM_RE.search(desc)
    n = int(m.group(1)) if m else 0
    tokens = base_tokens(accent, dark)
    root = _BUILDERS[arch](desc, brand, phrases, n)
    vp = viewport or {}
    uir = UIR(root=root, tokens=tokens,
              meta={"source_kind": "description", "title": brand, "lang": "zh", "archetype": arch,
                    "viewport": {"w": int(vp.get("width") or 1440), "h": int(vp.get("height") or 900)}})
    return uir.normalize()


_LLM_SYSTEM = (
    "你是 UI 结构化专家。把用户的页面描述转换为 UIR v1 JSON(统一 UI 中间表示)。只输出一个 JSON 对象, "
    "不要 markdown。结构: {\"uir_version\":\"1\",\"meta\":{\"title\":..,\"lang\":\"zh\"},"
    "\"tokens\":{\"color\":{\"c0\":\"#fff\",...},\"text_style\":{\"t0\":{\"size\":16,\"weight\":400,\"color\":\"c1\"}},"
    "\"space\":{\"s1\":4,\"s2\":8,\"s4\":16,\"s6\":32}},\"root\":NODE}。NODE={\"role\":角色,\"layout\":{\"mode\":"
    "\"flex|grid\",\"direction\":\"row|col\",\"gap\":令牌,\"cols\":n},\"box\":{\"pad\":[令牌..],\"bg\":令牌或#hex,"
    "\"radius\":令牌,\"w\":px或'100%',\"h\":px},\"text_ref\":文本样式令牌,\"content\":{\"text\":..,\"placeholder\":..,"
    "\"icon\":..},\"intents\":[{\"on\":\"click\",\"do\":\"navigate|toggle|submit-form|countdown\"}],\"children\":[..]}。"
    "角色闭集: box,text,image,icon,button,input,link,nav,list,list_item,card,header,footer,hero,divider,badge,avatar,"
    "table,form,section。图片用 image 角色不要给外链 src。节点总数 30~120, 文案必须具体、符合描述。"
)


@register_input("description")
class DescriptionAdapter:
    def to_uir(self, req: GenRequest, ctx: BuildContext) -> UIR:
        desc = (req.description or "").strip()
        if ctx.ai_cfg and not ctx.expired() and not req.hints.get("force_tier0"):
            try:
                ctx.log("UIR", f"描述→UIR: 调用大模型 ({ctx.ai_cfg['model']})")
                obj = llm.chat_json(ctx.ai_cfg, _LLM_SYSTEM, f"页面描述:\n{desc}\n\n"
                                    + (f"上一轮问题: {req.hints.get('feedback')}\n" if req.hints.get("feedback") else ""))
                uir = UIR.from_dict(obj).normalize()
                errs = uir.validate()
                if errs:
                    raise ValueError("; ".join(errs[:3]))
                uir.meta.update({"source_kind": "description", "tier": "llm"})
                if not uir.tokens.text_style:
                    uir.tokens = base_tokens(_accent(desc), _dark(desc))
                return uir
            except Exception as exc:
                ctx.log("UIR", f"大模型 UIR 不可用, 回落确定性原型: {type(exc).__name__}: {str(exc)[:160]}")
        uir = build_from_description(desc, req.viewport)
        uir.meta["tier"] = "deterministic"
        ctx.log("UIR", f"描述→UIR(确定性原型 {uir.meta['archetype']}): {uir.root.count()} 节点")
        return uir
