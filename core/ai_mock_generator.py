"""
V4 大模型智能 Mock 辅助生成。

定位: 克隆页中的 <form>(尤其登录/搜索/验证码组件)在原站是活的, 克隆后
点击无响应。本模块解析最终落地 HTML 中的表单(action/method/字段),
为生成的 Flask 子工程追加"智能 Mock 路由":

  - 确定性模式(默认, 无需大模型): 表单结构 -> 标准 JSON 响应模板,
    字段名回显, 保证提交有响应、有格式正确的 JSON;
  - LLM 模式(配置了 API Key 时): 把表单结构交给大模型, 生成贴近原站
    业务语义的响应体(如登录成功/失败报文)。LLM 失败自动回退确定性模板。

与已有"XHR/Fetch 流量录制 mock_data.json"互补: 录制覆盖动态接口,
本模块覆盖经典表单提交。
"""
import json
import re
from urllib.parse import urlsplit

import requests

import config

FORM_RE = re.compile(r"<form\b[^>]*>", re.I)
ACTION_RE = re.compile(r"""action\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
METHOD_RE = re.compile(r"""method\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
INPUT_NAME_RE = re.compile(
    r"""<input\b[^>]*\bname\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
BASE_URL_RE = re.compile(r"""<base\b[^>]*href\s*=\s*["']([^"']+)["']""", re.I)


def _attr_value(match) -> str:
    for group in (match.group(2), match.group(3), match.group(4)):
        if group is not None:
            return group.strip()
    return ""


def _form_action_path(tag: str, page_url: str) -> str:
    action = ""
    match = ACTION_RE.search(tag)
    if match:
        action = _attr_value(match)
    if not action or action.startswith(("javascript:", "#", "data:")):
        return ""
    try:
        if action.startswith(("http://", "https://", "//")):
            normalized = action if action.startswith("http") else "https:" + action
            return urlsplit(normalized).path or "/"
        # 相对路径: 相对克隆页路径解析(克隆后页面均在 / 下)
        base = page_url or "/"
        if base.startswith("http"):
            base = urlsplit(base).path or "/"
        from urllib.parse import urljoin
        return urlsplit(urljoin(base, action)).path or "/"
    except ValueError:
        return ""


def extract_forms(html: str, page_url: str = "/") -> list:
    """从 HTML 提取 [{path, method, fields[]}]; path 为空(纯 JS 提交)时跳过。"""
    forms = []
    for tag_match in FORM_RE.finditer(html or ""):
        tag = tag_match.group(0)
        path = _form_action_path(tag, page_url)
        if not path:
            continue
        method_match = METHOD_RE.search(tag)
        method = (_attr_value(method_match) if method_match else "GET").upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            method = "POST"
        # 字段取表单开始之后 4KB 窗口(避免跨表单采集)
        window = html[tag_match.end():tag_match.end() + 4096]
        fields = []
        for name_match in INPUT_NAME_RE.finditer(window):
            name = _attr_value(name_match)
            if name and name not in fields:
                fields.append(name)
            if len(fields) >= 24:
                break
        forms.append({"path": path, "method": method, "fields": fields})
    return forms


def _deterministic_body(form: dict) -> str:
    """确定性响应模板: 与仿真交互层一致的"繁忙失败"口径。

    登录类表单在克隆环境不应返回成功 —— 统一反馈网络繁忙, 前端点击
    亦由 INTERACTION_SIM 层拦截提示, 口径自洽。
    """
    payload = {
        "success": False,
        "code": 500,
        "message": "网络繁忙，请稍后重试",
        "msg": "网络繁忙，请稍后重试",
        "data": None,
    }
    return json.dumps(payload, ensure_ascii=False)


def _llm_body(ai_cfg: dict, form: dict, page_title_hint: str) -> str:
    """LLM 生成贴近业务语义的响应体; 任何异常由调用方回退确定性模板。"""
    prompt = (
        "你是前端接口 Mock 专家。以下是一个克隆演示网页中的登录/提交表单, 该演示环境"
        "要求所有提交统一返回\"服务繁忙\"风格的失败响应(不能返回成功)。请生成该表单"
        "提交后接口最可能返回的失败 JSON 响应体(单个 JSON 对象, 不要 markdown 代码块,"
        "不要解释文字)。要求: 包含 success=false 与 code 字段, message/msg 用简体中文"
        "的繁忙/稍后重试类文案, 可带 1~2 个贴近该业务的扩展字段。\n"
        f"表单提交地址: {form['method']} {form['path']}\n"
        f"输入字段: {', '.join(form['fields']) or '(无显式字段)'}\n"
        f"页面: {page_title_hint[:80]}\n"
        "只输出 JSON 本体。"
    )
    resp = requests.post(
        f"{ai_cfg['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {ai_cfg['api_key']}"},
        json={"model": ai_cfg["model"], "temperature": 0.3, "max_tokens": 500,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=config.AI_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    text = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip()).strip()
    json.loads(text)  # 校验; 非法即抛异常走回退
    return text


def generate_scene_mocks(scene: str, count: int, path: str,
                         method: str = "GET") -> dict:
    """按业务场景描述确定性生成 Mock 响应体(无 LLM 依赖, 离线可用)。

    scene: 业务场景描述(如 "电商商品列表"), 关键词决定字段与取值风格;
    count: 列表数据条数(1-50); path/method: 目标接口。
    返回 entry: {path, method, status, response_body(JSON 字符串)}。
    """
    import random as _random
    import time as _time
    scene = (scene or "").strip()
    count = max(1, min(50, int(count or 5)))
    path = path if path.startswith("/") else "/" + path
    method = (method or "GET").upper()

    field_plans = {
        "商品": [("id", "seq"), ("name", "名称"), ("price", "金额"),
                 ("stock", "数量"), ("status", "状态")],
        "订单": [("orderId", "seq"), ("user", "用户名"), ("amount", "金额"),
                 ("status", "状态"), ("createdAt", "时间")],
        "用户": [("id", "seq"), ("username", "用户名"), ("email", "邮箱"),
                 ("role", "状态"), ("createdAt", "时间")],
        "文章": [("id", "seq"), ("title", "名称"), ("author", "用户名"),
                 ("views", "数量"), ("publishedAt", "时间")],
    }
    plan = None
    for kw, pl in field_plans.items():
        if kw in scene:
            plan = pl
            break
    if plan is None:
        plan = [("id", "seq"), ("name", "名称"), ("status", "状态"),
                ("createdAt", "时间")]

    seq = _random.randint(100, 999)
    items = []
    for i in range(count):
        item = {}
        for field, kind in plan:
            if kind == "seq":
                item[field] = seq + i
            elif kind == "名称":
                item[field] = f"{scene or '示例'}项目-{seq + i}"
            elif kind == "用户名":
                item[field] = f"user_{seq + i}"
            elif kind == "邮箱":
                item[field] = f"user_{seq + i}@example.com"
            elif kind == "金额":
                item[field] = round(_random.uniform(9.9, 999.9), 2)
            elif kind == "数量":
                item[field] = _random.randint(0, 500)
            elif kind == "时间":
                item[field] = _time.strftime(
                    "%Y-%m-%d %H:%M:%S", _time.localtime(_time.time() - i * 3600))
            else:
                item[field] = _random.choice(["active", "pending", "done"])
        items.append(item)

    payload = {"code": 0, "message": "ok", "scene": scene or "通用",
               "data": items, "total": count}
    return {"path": path, "method": method, "status": 200,
            "response_body": json.dumps(payload, ensure_ascii=False)}


def generate_smart_mocks(html_sources: list, existing_entries: list,
                         ai_cfg: dict = None, log=None) -> list:
    """生成智能 Mock 条目(不与已录制接口重复)。

    html_sources: 最终落地的页面 HTML 列表(index + frames)。
    existing_entries: 录制型 Mock(mock_data.json 现有条目)。
    ai_cfg: {base_url, api_key, model}; None 走纯确定性模式。
    """
    log = log or (lambda *_args: None)
    known = {("/" + str(e.get("path") or "").strip("/")) for e in existing_entries}
    entries, seen = [], set()
    for html in html_sources:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html or "",
                                re.I | re.S)
        title = (title_match.group(1).strip() if title_match else "") or "克隆页"
        for form in extract_forms(html):
            if len(entries) >= config.AI_MOCK_MAX_FORMS:
                return entries
            path = "/" + form["path"].strip("/")
            if path in known or path in seen:
                continue
            seen.add(path)
            body = _deterministic_body(form)
            if ai_cfg is not None:
                try:
                    body = _llm_body(ai_cfg, form, title)
                    log("AI-MOCK", f"LLM 生成表单 Mock: {form['method']} {path}")
                except Exception as exc:
                    log("AI-MOCK", f"LLM 生成失败回退模板 ({type(exc).__name__}): "
                                   f"{form['method']} {path}")
            entries.append({"path": path, "method": form["method"], "query": "",
                            "status": 200, "response_body": body,
                            "smart": True, "fields": form["fields"][:16]})
            log("MOCK-API", f"智能表单 Mock: {form['method']} {path}"
                            f" (字段 {len(form['fields'])} 个)")
    return entries
