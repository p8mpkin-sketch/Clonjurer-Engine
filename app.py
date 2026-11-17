"""
Enterprise Web Cloner & Flask App Generator —— V2 主平台服务端。

V2 新增:
  - Referer 智能沙盒路由兜底: /static/... 与任意未匹配路由在 Referer 含
    /preview/<task_id> 时, 自动转发至对应工作空间(静态文件 / 路由页 / Mock)。
  - 在线热补丁: POST /api/tasks/<id>/patch 注入 CSS / HTML 片段并自动重打包。
  - 截图访问路由: /api/tasks/<id>/screenshots/{original|cloned|diff}。
  - Mock 新增: 保存接口支持 index=-1 创建全新自定义 Mock 路由。

REST API:
  POST /api/batches                 批量提交(1~50 URL)
  GET  /api/batches                 批次列表(工作空间切换器)
  GET  /api/tasks                   任务列表(批次/状态/关键字过滤)
  GET  /api/tasks/<id>              任务详情
  POST /api/tasks/<id>/rebuild      重新构建
  GET/POST /api/tasks/<id>/mock     读取 / 在线编辑与新增 Mock
  GET/POST /api/tasks/<id>/patch    读取 / 注入热补丁(CSS/HTML)并重打包
  GET  /api/tasks/<id>/assets       工作空间静态资产文件树
  GET  /api/tasks/<id>/file         文本资产内容预览
  GET  /api/tasks/<id>/structure    生成的 Flask 工程结构树
  GET  /api/tasks/<id>/live_code    界面生成: 模型正在写的代码(增量, 任务卡实时展示)
  GET  /api/tasks/<id>/screenshots/<img_type>   原站/克隆/差异图
  GET  /api/stream_logs             SSE 实时日志流
  GET  /api/telemetry               系统遥测(浏览器池/CAS/队列)
  GET  /preview/<id>/               沙盒预览(含反向代理兜底)
  GET  /api/download/<id>           单任务 ZIP 导出
  GET  /api/export_all              批量导出全部已完成 ZIP
  POST /api/cache/clear             清空 CAS 缓存池
  GET/POST /api/v4/link-tasks       短链任务列表(?q=)与新建
  POST /api/v4/link-tasks/<id>/generate  后台生成该任务的短链
  GET  /api/v4/link-tasks/<id>      短链任务详情(含其下所有短链)
  GET  /api/v4/link-tasks/<id>/export    导出 txt / csv
  GET  /api/v4/links/providers      四家缩链服务可用性预检
  GET/POST /api/v4/links            短链列表与单独新建
  GET  /s/<短码>                    短链跳转(302)并计点击(老短链也认 /s/<编号>)
  GET/POST /api/v4/remote/deploys   远程发布档案列表(?q=)与新建
  GET/PATCH/DELETE /api/v4/remote/deploys/<id>   详情(进度/阶段/日志尾部)/编辑/删除(?purge=1 清远端目录)
  POST /api/v4/remote/deploys/<id>/deploy        发布(异步, 后台线程)
  POST /api/v4/remote/deploys/<id>/start|stop    远端服务启停
  GET  /api/v4/remote/deploys/<id>/logs          部署日志(?kind=deploy) / 运行日志(?kind=runtime&tail=N)
  GET  /api/v4/remote/deploys/<id>/status        远端服务存活探测
  POST /api/v4/remote/test-connection            保存前连通性测试(只验证登录)
"""
import calendar
import io
import csv
import io
import json
import secrets
import os
import base64
import html
import re
import shutil
import subprocess
import threading
import time
import zipfile
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import requests as upstream_http
from flask import (Flask, Response, abort, jsonify, redirect, render_template, request,
                   g, send_file, send_from_directory, session)

import config
from core import archive, flask_generator, iam, live, security
from core import ai_mock_generator, captcha, llm_usage, websec
from core.clone.api_match import pick_entry
from core.browser_pool import SyncBrowserPool
from core.logger import LogBus
from core.scheduler import Scheduler
from models import ALL_WORKSPACES, Database, domain_of

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True  # 模板改动即时生效
app.secret_key = security.session_secret()
app.permanent_session_lifetime = timedelta(hours=max(1, config.SESSION_HOURS))
# 会话 Cookie: 脚本读不到(HttpOnly), 跨站请求不带(SameSite=Lax), 经 HTTPS 访问时只走 HTTPS
app.config.update(
    SESSION_COOKIE_NAME="clonjurer_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.COOKIE_SECURE,
)
db = Database(config.DB_PATH)
db.audit_max_rows = config.AUDIT_MAX_ROWS
bus = LogBus(config.LOG_RING_SIZE)
pool = SyncBrowserPool.instance(config.MAX_CONCURRENT_CONTEXTS)
scheduler = Scheduler(db, bus, pool)

# ---------------------------------------------------------------------------
# 鉴权 / RBAC / 审计
# ---------------------------------------------------------------------------
ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}          # 账号级(仅 admin 有平台含义)
WS_ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3, "owner": 4}  # 工作区内角色
_PLATFORM_PATHS = ("/api/v4/admin", "/api/scheduler/", "/api/pool/", "/api/cache/")
_WS_ADMIN_PATHS = ("/api/v4/users", "/api/v4/audit", "/api/v4/invites", "/api/v4/workspace/")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")


def _auth_required() -> bool:
    """无任何用户时为开放模式(单机起步零摩擦), 一旦初始化即强制登录。"""
    return db.count_users() > 0


def _current_user():
    uid = session.get("uid")
    if not uid:
        return None
    user = db.get_user(uid)
    # 会话版本: 强制下线 / 重置密码 / 停用会把 users.session_ver 加一, 旧会话随即失效
    if user is None or not user.get("is_active") \
            or int(session.get("sv") or 0) != int(user.get("session_ver") or 0):
        session.clear()
        return None
    return user


def _login_session(user: dict, ws=None):
    session.clear()  # 登录前的会话内容一律作废, 不沿用
    session["uid"] = user["id"]
    session["sv"] = int(user.get("session_ver") or 0)
    if ws is None:
        session.pop("ws", None)
    else:
        session["ws"] = ws
    session.permanent = True
    g.pop("ws_ctx", None)


def _audit_account(user: dict, action: str, detail: str = ""):
    """账号级事件(登录/退出/锁定)写进该账号所在的每个工作区, 各区管理员都能看到、互不串区;
    不存在的用户名(撞库)记在默认工作区。"""
    spaces = [s["id"] for s in db.user_workspaces(user["id"])] if user and user.get("id") else []
    name = (user or {}).get("username") or "?"
    for wid in spaces or [1]:
        db.log_audit(name, action, detail, _client_ip(), workspace_id=wid)


_WEAK_PASSWORDS = {"12345678", "123456789", "1234567890", "password", "password1", "password123", "qwerty123",
                   "11111111", "88888888", "00000000", "abc12345", "abcd1234", "admin123", "admin1234",
                   "1qaz2wsx", "qwertyui", "iloveyou", "a1234567", "aa123456", "woaini1314", "p@ssw0rd"}


def _password_problem(password: str, username: str = "") -> str:
    """新密码规则: 8~128 位, 至少包含字母、数字、符号中的两类, 不能是用户名或常见弱口令。
    只管新设的密码, 已有账号的旧密码照常能登录。"""
    if len(password) < 8:
        return "密码至少 8 位"
    if len(password) > 128:
        return "密码最长 128 位"
    kinds = sum([any(c.isalpha() for c in password), any(c.isdigit() for c in password),
                 any(not c.isalnum() for c in password)])
    if kinds < 2:
        return "密码需要包含字母、数字、符号中的至少两类"
    low = password.lower()
    if low in _WEAK_PASSWORDS or username and low == username.lower():
        return "这个密码太常见或与用户名相同, 请换一个"
    return ""


def _public_user(user: dict):
    if not user:
        return None
    return {"id": user["id"], "username": user["username"],
            "display_name": user.get("display_name") or user["username"],
            "role": user["role"]}


def _client_ip() -> str:
    """来源 IP: 直连对端是本机/受信代理时才采信 X-Forwarded-For, 否则用 TCP 对端地址(防伪造)。"""
    if "client_ip" not in g:
        g.client_ip = websec.client_ip(request.remote_addr or "", request.headers.get("X-Forwarded-For", ""),
                                       _TRUSTED_PROXIES)
    return g.client_ip


def _audit(action: str, detail: str = "", username: str = None, workspace_id=None):
    user = _current_user() if username is None else {"username": username}
    if workspace_id is None:
        workspace_id = _ws_ctx()[0] or 1
    db.log_audit((user or {}).get("username") or "anonymous",
                 action, detail, _client_ip(), workspace_id=workspace_id)


def _is_platform_admin(user) -> bool:
    """平台管理员 = 账号级 role=admin(运维全局资源: AI 供应商/存储/调度器), 不自动看到各区数据。"""
    return bool(user) and user.get("role") == "admin"


def _ws_ctx():
    """(当前工作区 id, 我在该区的角色)。开放模式恒为 (1, owner);
    登录后取会话里选的区(须仍是成员), 否则落到第一个加入的区; 不属于任何区为 (None, None)。"""
    if "ws_ctx" in g:
        return g.ws_ctx
    if not _auth_required():
        g.ws_ctx = (1, "owner")
        return g.ws_ctx
    user = _current_user()
    ctx = (None, None)
    if user is not None:
        wid = session.get("ws")
        m = db.get_membership(int(wid), user["id"]) if str(wid or "").isdigit() else None
        if m is not None:
            ctx = (m["workspace_id"], m["role"])
        else:
            spaces = db.user_workspaces(user["id"])
            if spaces:
                ctx = (spaces[0]["id"], spaces[0]["role"])
                session["ws"] = spaces[0]["id"]
    g.ws_ctx = ctx
    return ctx


def _me_name() -> str:
    """当前登录成员的用户名(开放模式为空): 模型消耗记在谁名下。"""
    if not _auth_required():
        return ""
    return (_current_user() or {}).get("username") or ""


def _cur_ws() -> int:
    wid = _ws_ctx()[0]
    if wid is None:
        abort(403)
    return wid


def _can_see_ws(workspace_id) -> bool:
    """按成员关系判定可见(不限当前所选区: 预览 iframe/新窗口可能属于另一个已加入的区)。"""
    if workspace_id is None:
        return False
    if not _auth_required():
        return not _open_mode_remote()
    user = _current_user()
    return user is not None and db.get_membership(int(workspace_id), user["id"]) is not None


# ---------------------------------------------------------------------------
# Web 安全闸门: Host 白名单 / 同源校验 / 预览独立源 / 开放模式只限本机 / 安全响应头
# ---------------------------------------------------------------------------
_ALLOWED_HOSTS = websec.parse_list(config.ALLOWED_HOSTS)
_TRUSTED_ORIGINS = websec.origin_netlocs(config.TRUSTED_ORIGINS + "," + os.environ.get("CLONER_PUBLIC_URL", ""))
_TRUSTED_PROXIES = {x.strip() for x in config.TRUSTED_PROXIES.split(",") if x.strip()}
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
# 预览独立源: 由 __main__ 起第二个监听口; 测试/被 import 时不起, 行为同旧版(同源预览)
_PREVIEW = {"on": False}
# 开放模式下从其他机器初始化管理员要用的一次性口令(启动时打印在终端)
_SETUP_TOKEN = secrets.token_urlsafe(9)


def _preview_isolated() -> bool:
    return _PREVIEW["on"] and config.PREVIEW_PORT > 0


def _on_preview_side() -> bool:
    return _preview_isolated() and str(request.environ.get("SERVER_PORT", "")) == str(config.PREVIEW_PORT)


def _preview_base() -> str:
    """平台页面里 iframe / 新窗口打开预览用的源; 未隔离时为空串(同源相对路径)。"""
    if not _preview_isolated():
        return ""
    if config.PREVIEW_PUBLIC_URL:
        return config.PREVIEW_PUBLIC_URL
    host = websec.split_host(request.host)
    host = f"[{host}]" if ":" in host else host
    return f"{request.scheme}://{host}:{config.PREVIEW_PORT}"


def _is_local_request() -> bool:
    import ipaddress as _ip
    try:
        return _ip.ip_address(_client_ip()).is_loopback
    except ValueError:
        return _client_ip() in ("local", "localhost")


def _open_mode_remote() -> bool:
    """开放模式(还没有账号)下来自其他机器的请求: 只能看登录页, 凭终端里的初始化口令建管理员。"""
    return config.OPEN_MODE_LOCAL_ONLY and not _auth_required() and not _is_local_request()


@app.before_request
def _host_gate():
    """防 DNS 重绑定: 监听回环地址时只认本机主机名(及 CLONER_ALLOWED_HOSTS)。"""
    if not websec.host_allowed(request.host, config.HOST, _ALLOWED_HOSTS):
        return Response("Host not allowed", status=421, mimetype="text/plain")
    return None


@app.before_request
def _preview_origin_gate():
    """预览独立源: 预览端口只提供 /preview/* 与克隆页逃逸到根路径的资源, 不提供平台页面与接口;
    平台端口上的 /preview/* 跳到预览端口(老链接、分享链接照常能打开)。"""
    if not _preview_isolated():
        return None
    if _on_preview_side():
        if request.path.startswith("/preview/"):
            return None
        resp = _referer_fallback(allow_root=True)
        if resp is not None:
            return resp
        return Response("Not Found", status=404, mimetype="text/plain")
    if request.path.startswith("/preview/") and request.method in _SAFE_METHODS:
        url = _preview_base() + request.full_path.rstrip("?")
        return redirect(url, code=302)
    if request.path.startswith("/preview/"):
        abort(404)
    return None


@app.before_request
def _csrf_gate():
    """改动类请求必须同源: 浏览器跨站(含预览端口上的克隆页)发起的 POST/PUT/PATCH/DELETE 一律拒绝。"""
    if request.method in _SAFE_METHODS or _on_preview_side():
        return None
    if request.path.startswith("/preview/") and not _preview_isolated():
        return None  # 旧的同源预览: 克隆页表单/接口打到自己的 Mock, 不碰平台数据
    if websec.same_origin_ok(request.headers.get("Origin", ""), request.headers.get("Referer", ""),
                             request.host, _TRUSTED_ORIGINS):
        return None
    return jsonify({"error": "请求来源校验失败(跨站请求已拦截)。如经反向代理访问, 请配置 CLONER_TRUSTED_ORIGINS"}), 403


_CSP_PLATFORM = "; ".join([
    "frame-ancestors 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
])


@app.after_request
def _security_headers(resp):
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    if _on_preview_side() or request.path.startswith("/preview/"):
        # 预览页是任意第三方页面的克隆件: 不缓存到共享代理, 不把平台地址带给外站
        h.setdefault("Referrer-Policy", "no-referrer-when-downgrade")
        if _on_preview_side() and resp.mimetype == "text/html" and resp.status_code == 200:
            _inject_preview_bridge(resp)
        return resp
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("X-Frame-Options", "SAMEORIGIN")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
    if resp.mimetype == "text/html":
        h.setdefault("Content-Security-Policy", _CSP_PLATFORM)
    if request.path.startswith("/api/"):
        h.setdefault("Cache-Control", "no-store")
    return resp


# 预览桥: 平台页面通过 postMessage 给预览 iframe 实时注入热补丁 CSS(预览与平台不同源, 不能直接改 DOM)
_PREVIEW_BRIDGE = (b'<script data-clonjurer="preview-bridge">(function(){addEventListener("message",function(e){'
                   b'var d=e.data;if(e.source!==parent||!d||d.type!=="clonjurer:hotpatch")return;'
                   b'var s=document.getElementById("live-hotpatch");if(!s){s=document.createElement("style");'
                   b's.id="live-hotpatch";(document.head||document.documentElement).appendChild(s);}'
                   b's.textContent=String(d.css||"");try{e.source.postMessage({type:"clonjurer:hotpatch-ok"},"*")}catch(_){}'
                   b'});})();</script>')


def _inject_preview_bridge(resp):
    try:
        resp.direct_passthrough = False
        body = resp.get_data()
    except Exception:
        return
    low = body[:65536].lower()
    idx = low.find(b"<head")
    if idx >= 0:
        idx = low.find(b">", idx) + 1
    else:
        idx = 0
    resp.set_data(body[:idx] + _PREVIEW_BRIDGE + body[idx:])


@app.before_request
def _rbac_gate():
    """开放模式全放行(只限本机, 见 _open_mode_remote)。有用户后: /api/* 需登录; 平台级运维接口需平台管理员;
    成员/审计/邀请/工作区设置需该区管理员; 写操作需该区操作员及以上。
    页面与 /preview 不在此拦截(预览按任务归属另行校验)。"""
    if not _auth_required():
        if _open_mode_remote() and request.path.startswith("/api/") \
                and not request.path.startswith("/api/auth/"):
            return jsonify({"error": "平台还没有初始化管理员, 只能在本机使用; 请用终端里打印的初始化口令创建管理员",
                            "auth": "required", "setup": "token"}), 401
        return None
    path = request.path
    if not path.startswith("/api/") or path.startswith("/api/auth/"):
        return None
    user = _current_user()
    if user is None:
        return jsonify({"error": "未登录或会话已过期", "auth": "required"}), 401
    if path.startswith(_PLATFORM_PATHS):
        if not _is_platform_admin(user):
            return jsonify({"error": "该操作需要平台管理员权限"}), 403
        return None
    if path.startswith("/api/v4/workspaces"):  # 列出/新建/切换自己的工作区
        return None
    wid, role = _ws_ctx()
    if wid is None:
        return jsonify({"error": "你还没有加入任何工作区, 请使用邀请链接加入或新建一个",
                        "workspace": "none"}), 403
    rank = WS_ROLE_RANK.get(role or "", 0)
    if path.startswith(_WS_ADMIN_PATHS):
        if rank < WS_ROLE_RANK["admin"]:
            return jsonify({"error": "该操作需要工作区管理员权限"}), 403
    elif request.method not in ("GET", "HEAD", "OPTIONS") and not path.startswith("/api/v4/archive/jobs"):
        # 打包存档是只读操作(只是用 POST 起后台任务), 只读成员可用; 导入仍需操作员
        if rank < WS_ROLE_RANK["operator"]:
            return jsonify({"error": "当前账号在本工作区为只读角色"}), 403
    return None

TEXT_PREVIEW_EXTS = {".html", ".css", ".js", ".mjs", ".json", ".svg",
                     ".txt", ".md", ".xml", ".map"}
TREE_NODE_CAP = 3000

PREVIEW_REFERER_RE = re.compile(r"/preview/(\d+)")
SCREENSHOT_FILES = {
    "original": "original_preview.png",
    "cloned": "cloned_preview.png",
    "diff": "diff_preview.png",
}
# 交互版渲染失败、改交静态版时, 引擎只留下静态版/保真轨截图: 依次回退, 卡片缩略图与对比视图不至于空白
SCREENSHOT_FALLBACKS = {
    "cloned": ("cloned_preview.png", "static_preview.png", "fidelity_preview.png"),
    "diff": ("diff_preview.png", "fidelity_diff.png"),
    "original": ("original_preview.png",),
}


def _shot_file(ws: Path, img_type: str):
    for name in SCREENSHOT_FALLBACKS.get(img_type, ()):
        if (ws / name).is_file():
            return ws / name
    return None

HOTPATCH_CSS_RE = re.compile(
    r'<style id="clone-hotpatch">.*?</style>', re.S)
HOTPATCH_HTML_RE = re.compile(
    r'<!--clone-hotpatch-html-start-->.*?<!--clone-hotpatch-html-end-->', re.S)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _sanitize_config(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    viewport = raw.get("viewport") if raw.get("viewport") in config.VIEWPORTS else "desktop"
    mode = raw.get("mode") if raw.get("mode") in ("single", "crawler") else "single"
    try:
        max_pages = int(raw.get("max_pages", 1) or 1)
    except (TypeError, ValueError):
        max_pages = 1
    max_pages = max(1, min(config.MAX_PAGES_LIMIT, max_pages))

    def flag(key, default):
        value = raw.get(key, default)
        return value is True or value == "true" or value == 1

    rt_raw = raw.get("render_timeout")
    try:
        render_timeout = max(5, min(120, int(float(rt_raw)))) if rt_raw not in (None, "", 0) else 0
    except (TypeError, ValueError):
        render_timeout = 0

    return {
        "mode": mode,
        "max_pages": max_pages,
        "viewport": viewport,
        "render_timeout": render_timeout,
        "cssom": flag("cssom", True),
        "mock_api": flag("mock_api", True),
        "proxy_fallback": flag("proxy_fallback", True),
        "disable_hydration": flag("disable_hydration", False),
        "similarity": flag("similarity", True),
        "interaction_sim": flag("interaction_sim", config.INTERACTION_SIM),
        "cookie": str(raw.get("cookie") or "").strip()[:8000],
        "bearer": str(raw.get("bearer") or "").strip()[:8000],
        "user_agent": str(raw.get("user_agent") or "").strip()[:8000],
        # V4: 大模型视觉闭环(任务级配置 > 环境变量 CLONER_AI_*)
        "ai_vision_repair": flag("ai_vision_repair", False),
        "ai_api_key": str(raw.get("ai_api_key") or "").strip()[:4096],
        "ai_base_url": str(raw.get("ai_base_url") or "").strip()[:4096],
        "ai_model": str(raw.get("ai_model") or "").strip()[:200],
        # 仿真交互层自定义: 登录提示文案(留空=默认池) 与 登录后跳转(留空=不跳转)
        "sim_login_message": str(raw.get("sim_login_message") or "").strip()[:120],
        "sim_login_redirect": str(raw.get("sim_login_redirect") or "").strip()[:500],
        # 交付形态: "" = 网站工程(Flask ZIP, 默认); desktop = 桌面应用(.app + .exe); apk = Android 应用
        "package": str(raw.get("package") or "") if str(raw.get("package") or "") in ("desktop", "apk") else "",
    }


def _normalize_urls(raw_urls):
    accepted, rejected = [], []
    if isinstance(raw_urls, str):
        raw_urls = [line for line in raw_urls.replace("\r", "\n").split("\n")]
    for item in raw_urls[: config.MAX_URLS_PER_BATCH * 3]:
        url = str(item).strip()
        if not url or url.startswith("#"):
            continue
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        domain = domain_of(url)
        if not domain or "." not in domain or " " in url:
            rejected.append({"url": url, "reason": "非法 URL"})
            continue
        try:
            why = websec.forbidden_target(urlsplit(url).hostname or "", resolve=True)
        except ValueError:
            why = "非法 URL"
        if why:
            rejected.append({"url": url, "reason": why})
            continue
        if any(u.lower() == url.lower() for u, _d in accepted):
            continue
        accepted.append((url, domain))
        if len(accepted) >= config.MAX_URLS_PER_BATCH:
            break
    return accepted, rejected


def _visible_task(task_id: int):
    """按工作区成员关系取任务; 别区的任务与不存在一视同仁(None), 不暴露存在性。"""
    task = db.get_task(task_id)
    if task is None:
        return None
    if _can_see_ws(task.get("workspace_id")) or _preview_share_ok(task_id):
        return task
    return None


def _task_or_404(task_id: int) -> dict:
    task = _visible_task(task_id)
    if task is None:
        abort(404)
    return task


def _project_or_404(project_id: int) -> dict:
    proj = db.get_project(project_id)
    if proj is None or proj.get("workspace_id") != _cur_ws():
        abort(404)
    return proj


# 预览分享: 带签名与到期时间的链接, 首次打开后写入仅作用于该预览路径的 cookie, 子资源随之放行
def _share_sig(task_id: int, exp: int) -> str:
    import hmac
    import hashlib
    msg = f"preview:{task_id}:{exp}".encode()
    return hmac.new(app.secret_key.encode() if isinstance(app.secret_key, str) else app.secret_key,
                    msg, hashlib.sha256).hexdigest()[:32]


def _share_token(task_id: int, hours: int) -> str:
    exp = int(time.time()) + max(1, min(24 * 30, int(hours))) * 3600
    return f"{exp}.{_share_sig(task_id, exp)}"


def _share_token_ok(task_id: int, token: str) -> bool:
    import hmac
    try:
        exp_s, sig = str(token or "").split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    return exp > time.time() and hmac.compare_digest(sig, _share_sig(task_id, exp))


def _preview_share_ok(task_id: int) -> bool:
    if not request.path.startswith(f"/preview/{task_id}/"):
        return False
    token = request.args.get("share") or request.cookies.get(f"pvs_{task_id}", "")
    return _share_token_ok(task_id, token)


def _inside(base: Path, target: Path) -> bool:
    """target 解析后是否仍在 base 目录内。按路径分段比较: 字符串前缀会把 workspaces/12 当成 workspaces/1 的子路径。"""
    try:
        return Path(target).resolve().is_relative_to(Path(base).resolve())
    except (OSError, ValueError):
        return False


def _workspace_of(task: dict) -> Path:
    ws = Path(task["workspace_path"]) if task.get("workspace_path") else Path()
    if not _inside(config.WORKSPACE_ROOT, ws) or ws.resolve() == config.WORKSPACE_ROOT.resolve():
        ws = config.WORKSPACE_ROOT / str(task["id"])
    return ws


def _load_site_meta(ws: Path) -> dict:
    site_file = ws / "site.json"
    if site_file.is_file():
        try:
            return json.loads(site_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def _read_mock_entries(ws: Path) -> list:
    mock_file = ws / "mock_data.json"
    if not mock_file.is_file():
        return []
    try:
        data = json.loads(mock_file.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


_PROXY_FORWARD_HEADERS = {"accept", "accept-language", "content-type", "user-agent", "if-none-match",
                          "if-modified-since", "range", "cache-control", "pragma"}


def _proxy_fallback(upstream: str, subpath: str):
    url = upstream.rstrip("/") + "/" + subpath.lstrip("/")
    if request.query_string:
        url += "?" + request.query_string.decode("latin-1")
    # 只转发内容协商类请求头: 浏览器带来的 Cookie / Authorization 是平台自己的会话与凭据, 绝不能发给原站
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() in _PROXY_FORWARD_HEADERS or (k.lower().startswith("x-") and not k.lower().startswith(
            ("x-forwarded", "x-real-ip", "x-csrf", "x-xsrf")))
    }
    headers["Referer"] = upstream.rstrip("/") + "/"
    headers.setdefault("User-Agent", config.DEFAULT_USER_AGENT)
    try:
        with websec.guard_session(upstream_http.Session()) as sess:   # 每一跳都拦元数据/链路本地地址
            upstream_resp = sess.request(
                request.method, url, headers=headers,
                data=request.get_data(), timeout=15, allow_redirects=True)
    except Exception as exc:
        return Response(f"Upstream proxy failed: {exc}", status=502,
                        content_type="text/plain; charset=utf-8")
    resp = Response(upstream_resp.content, status=upstream_resp.status_code)
    if upstream_resp.headers.get("content-type"):
        resp.headers["Content-Type"] = upstream_resp.headers["content-type"]
    resp.headers["X-Clone-Proxy"] = "upstream"
    return resp


def _build_tree(directory: Path, counter: list) -> list:
    nodes = []
    try:
        entries = sorted(directory.iterdir(),
                          key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        return nodes
    for entry in entries:
        if counter[0] <= 0:
            break
        if entry.name in ("__pycache__", ".venv", "flask_app", ".DS_Store"):
            continue
        counter[0] -= 1
        node = {"name": entry.name, "path": str(entry.relative_to(directory)),
                "type": "dir" if entry.is_dir() else "file", "size": 0}
        if entry.is_dir():
            node["children"] = _build_tree(entry, counter)  # type: ignore[assignment]
        else:
            try:
                node["size"] = entry.stat().st_size
            except OSError:
                node["size"] = 0
        nodes.append(node)
    return nodes


def _regenerate_project(task: dict, ws: Path) -> Path:
    """依据 site.json + mock_data.json 重建 Flask 工程 ZIP(热补丁/兜底用)。"""
    site = _load_site_meta(ws)
    mocks = _read_mock_entries(ws)
    tracker_cfg = db.get_tracker_config(task["id"])
    bake_cfg = None
    if tracker_cfg and tracker_cfg.get("enabled"):
        bake_cfg = {
            "token": security.decrypt_value(tracker_cfg.get("token", "")),
            "secret": security.decrypt_value(tracker_cfg.get("secret_enc", "")),
            "keyword": tracker_cfg.get("keyword", ""),
            "slots": tracker_cfg.get("slots") or [],
            "mask": bool(tracker_cfg.get("mask", True)),
        }
    return flask_generator.generate_project(
        task["id"], site.get("upstream", ""), ws,
        site.get("routes", {}), mocks, bool(site.get("proxy_enabled", True)),
        tracker_hook="1" if bake_cfg else "",
        tracker_cfg=bake_cfg,
    )


def _serve_from_task(task: dict, path: str):
    """Referer 嗅探兜底: 从任务工作空间解析静态文件 / 路由页 / Mock 响应。"""
    ws = _workspace_of(task)
    if not ws.is_dir():
        return None
    site = _load_site_meta(ws)
    normalized = "/" + (path or "").strip("/")
    routes = site.get("routes", {})
    if normalized in routes and (ws / routes[normalized]).is_file():
        return send_from_directory(ws, routes[normalized])
    rel = (path or "").strip("/")
    if rel:
        target = (ws / rel).resolve()
        if _inside(ws, target) and target.is_file():
            resp = send_from_directory(ws, rel)
            resp.headers["X-Clone-Referer"] = "workspace"
            return resp
    method = request.method.upper()
    same_path = [e for e in _read_mock_entries(ws) if "/" + (e.get("path") or "").strip("/") == normalized]
    # 同接口多条录制(靠请求体/参数区分内容)时按请求指纹挑最像的; 没有同方法的才退到 GET 录制
    cands = [e for e in same_path if str(e.get("method", "GET")).upper() == method] \
        or [e for e in same_path if str(e.get("method", "GET")).upper() == "GET"]
    if cands:
        body_bytes = b""
        if method != "GET" and len(cands) > 1:
            with suppress(Exception):
                body_bytes = request.get_data(cache=True)[:262144]
        entry = pick_entry(cands, request.query_string.decode("latin-1"), body_bytes)
        if entry is not None:
            raw = entry.get("response_body", "")
            try:
                body = json.loads(raw) if raw else None
            except (ValueError, TypeError):
                body = raw
            if isinstance(body, str):
                mock_resp = Response(body, content_type="text/plain; charset=utf-8")
            else:
                mock_resp = jsonify(body)
            mock_resp.status_code = int(entry.get("status") or 200)
            mock_resp.headers["X-Mock-Api"] = "referer-fallback"
            return mock_resp
    return None


# ---------------------------------------------------------------------------
# 页面与批次/任务 API
# ---------------------------------------------------------------------------
@app.get("/vendor/<path:filename>")
def vendor_asset(filename):
    """平台前端本地化静态资源(Vue/Lucide/Tailwind/字体), 免鉴权长缓存。

    页面曾引用 unpkg/cdn.tailwindcss/google fonts, 国内访问动辄数秒甚至
    超时; 全部钉版本下载至 static_vendor/ 后经此路由提供, 页面零外链。
    """
    resp = send_from_directory("static_vendor", filename)
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


@app.get("/favicon.ico")
def favicon():
    """浏览器/书签/RSS 阅读器会不看 <link> 直接请求根路径 /favicon.ico; 生成脚本见 tools/brand/。"""
    resp = send_from_directory("static_vendor/brand", "favicon.ico", mimetype="image/x-icon")
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


@app.get("/")
def index_page():
    # Referer 兜底: 克隆页 JS 整页导航逃逸到平台根时(其 Referer 必含
    # /preview/<id>), 回落服务该任务的克隆首页, 避免在预览 iframe 里渲染出平台 UI。
    if not _preview_isolated():   # 预览独立源时, 这类请求在预览端口上处理, 平台端口不再回落克隆页
        resp = _referer_fallback(allow_root=True)
        if resp is not None:
            return resp
    return render_template("index.html")


@app.post("/api/batches")
def create_batch():
    data = request.get_json(silent=True) or {}
    accepted, rejected = _normalize_urls(data.get("urls") or [])
    if not accepted:
        return jsonify({"error": "未提供合法 URL(每行一个, 支持 1~50 个)"}), 400
    clean_config = _sanitize_config(data.get("config") or {})
    project_id = data.get("project_id")
    project_id = int(project_id) if str(project_id or "").isdigit() else None
    if project_id is not None and (db.get_project(project_id) or {}).get("workspace_id") != _cur_ws():
        return jsonify({"error": f"项目 #{project_id} 不存在"}), 400
    blocked = _quota_block(_cur_ws(), "clone", len(accepted))
    if blocked:
        return blocked
    first_domain = domain_of(accepted[0][0])
    name = str(data.get("name") or "").strip() or (
        f"批次 {time.strftime('%m-%d %H:%M')} · {first_domain}"
        + (f" 等 {len(accepted)} 站" if len(accepted) > 1 else "")
    )
    batch_id = db.create_batch(name, accepted, clean_config, project_id,
                               created_by=(_current_user() or {}).get("username", ""),
                               workspace_id=_cur_ws())
    scheduler.start()
    bus.publish(None, "SCHED", f"新批次 #{batch_id} 已入队: {len(accepted)} 个站点",
                workspace_id=_cur_ws())
    _audit("batch.create", f"批次 #{batch_id}「{name}」提交 {len(accepted)} 个站点"
            + (f" 至项目 #{project_id}" if project_id else ""))
    return jsonify({"batch_id": batch_id, "name": name,
                    "accepted": len(accepted), "rejected": rejected}), 201


@app.get("/api/batches")
def list_batches():
    batches = db.list_batches(50, workspace_id=_cur_ws())
    for b in batches:  # 批次配置里的目标站 Cookie / Bearer / AI Key 不回给前端(只读成员也能看批次列表)
        if b.get("config_json"):
            b["config_json"] = json.dumps(_redact_config(_parse_json(b["config_json"])), ensure_ascii=False)
    return jsonify({"batches": batches})


@app.get("/api/tasks")
def list_tasks():
    batch_id = request.args.get("batch_id", type=int)
    project_id = request.args.get("project_id", type=int)
    status = request.args.get("status", "").strip()
    if status not in ("pending", "running", "completed", "failed"):
        status = ""
    keyword = request.args.get("q", "").strip()
    tasks = db.list_tasks(batch_id=batch_id or None, status=status or None,
                          q=keyword or None, project_id=project_id or None,
                          workspace_id=_cur_ws())
    for task in tasks:
        task["stats"] = _parse_json(task.pop("stats_json", "") or "{}")
        ws = _workspace_of(task)
        task["has_cloned_preview"] = _shot_file(ws, "cloned") is not None
        task["title"] = _display_title(task, ws)
    return jsonify({"tasks": tasks})


_TITLE_CACHE: dict = {}
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _display_title(task: dict, ws: Path) -> str:
    """任务显示名: 生成任务 = 任务记录里的标题(domain 列); 克隆任务 = 克隆页的 <title>(按文件修改时间缓存),
    取不到时返回空串(前端退回域名)。目录型目标的入口页只是跳转壳, 按 site.json 的路由找与目标路径对应的页面。
    用户改过名(alias)时一律以改的名字为准。"""
    if (task.get("alias") or "").strip():
        return task["alias"].strip()
    if str(task.get("target_url") or "").startswith("uir:generate:"):
        return task.get("domain") or ""
    site = ws / "site.json"
    try:
        key = (site.stat().st_mtime if site.is_file() else 0.0, (ws / "index.html").stat().st_mtime)
    except OSError:
        return ""
    hit = _TITLE_CACHE.get(task["id"])
    if hit and hit[0] == key:
        return hit[1]
    pages = ["index.html"]
    try:
        routes = (json.loads(site.read_text(encoding="utf-8")).get("routes") or {}) if site.is_file() else {}
        want = urlsplit(task.get("target_url") or "").path.rstrip("/") or "/"
        ordered = sorted(routes.items(), key=lambda kv: (kv[0].rstrip("/") or "/") != want)
        pages = [v for _, v in ordered if str(v).endswith(".html")] + pages
    except Exception:
        pass
    title = ""
    for rel in dict.fromkeys(pages):
        f = (ws / rel)
        if not _inside(ws, f) or not f.is_file():
            continue
        try:
            with open(f, "rb") as fh:
                head = fh.read(524288).decode("utf-8", errors="replace")
        except OSError:
            continue
        m = _TITLE_RE.search(head)
        if m:
            title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()[:80]
            if title:
                break
    _TITLE_CACHE[task["id"]] = (key, title)
    return title


@app.get("/api/tasks/<int:task_id>")
def task_detail(task_id):
    task = _task_or_404(task_id)
    task["stats"] = _parse_json(task.pop("stats_json", "") or "{}")
    ws = _workspace_of(task)
    site = _load_site_meta(ws)
    task["site"] = site
    task["has_original_preview"] = (ws / "original_preview.png").is_file()
    task["has_cloned_preview"] = _shot_file(ws, "cloned") is not None
    task["has_diff_preview"] = _shot_file(ws, "diff") is not None
    task["has_ai_report"] = (ws / "ai_repair_report.json").is_file()
    task["title"] = _display_title(task, ws)
    return jsonify({"task": task})


@app.post("/api/tasks/batch/rebuild")
def batch_rebuild_tasks():
    """批量重建: completed/failed 状态的任务重置为 pending; running 跳过。"""
    data = request.get_json(silent=True) or {}
    ids = data.get("task_ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "请提供 task_ids 数组"}), 400
    accepted, skipped = [], []
    for raw in ids[:100]:
        try:
            task_id = int(raw)
        except (TypeError, ValueError):
            continue
        task = _visible_task(task_id)
        if task is None:
            skipped.append({"id": task_id, "reason": "不存在"})
            continue
        if task["status"] == "running":
            skipped.append({"id": task_id, "reason": "执行中"})
            continue
        ws = _workspace_of(task)
        if ws.is_dir():
            shutil.rmtree(ws, ignore_errors=True)
        db.update_task(task_id, status="pending", progress=0, stage="",
                       similarity_score=None, stats_json="{}", error_msg="",
                       zip_path="")
        accepted.append(task_id)
    if accepted:
        scheduler.start()
        bus.publish(None, "SCHED", f"批量重建: {len(accepted)} 个任务已入队")
    _audit("task.batch_rebuild", f"批量重建 {len(accepted)} 个(跳过 {len(skipped)})")
    return jsonify({"ok": True, "accepted": accepted, "skipped": skipped}), 200


@app.post("/api/tasks/<int:task_id>/rebuild")
def rebuild_task(task_id):
    task = _task_or_404(task_id)
    if task["status"] == "running":
        return jsonify({"error": "任务执行中, 无法重建"}), 409
    ws = _workspace_of(task)
    if ws.is_dir():
        shutil.rmtree(ws, ignore_errors=True)
    db.update_task(task_id, status="pending", progress=0, stage="",
                   similarity_score=None, stats_json="{}", error_msg="",
                   zip_path="")
    scheduler.start()
    bus.publish(task_id, "SCHED", "任务已重置为 pending, 等待重新调度")
    _audit("task.rebuild", f"任务 #{task_id} 重建: {task['target_url']}")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 任务删除 / 编辑(改名、移动到其他项目)
# ---------------------------------------------------------------------------
def _task_ids_arg(data: dict):
    ids = data.get("task_ids")
    if not isinstance(ids, list) or not ids:
        return None
    out = []
    for raw in ids[:500]:
        with suppress(TypeError, ValueError):
            out.append(int(raw))
    return out or None


def _delete_tasks(task_ids) -> tuple:
    """删除一组任务: 运行中 / 正在视觉修复的跳过; 先下线部署, 再删记录, 最后删工作空间与 ZIP。
    返回 (已删 id 列表, 跳过 [{id, reason}])。"""
    skipped, cands = [], []
    for tid in dict.fromkeys(task_ids):
        task = _visible_task(tid)
        if task is None:
            skipped.append({"id": tid, "reason": "不存在"})
        elif task["status"] == "running":
            skipped.append({"id": tid, "reason": "执行中"})
        elif (_ai_jobs.get(tid) or {}).get("state") == "running":
            skipped.append({"id": tid, "reason": "正在视觉修复"})
        else:
            cands.append(task)
    for task in cands:
        if db.get_docker(task["id"]):   # 先下线部署(容器/本机进程), 否则会留下没人管的站点和端口
            with suppress(Exception):
                deployer.destroy(task["id"])
    gone = set(db.delete_tasks([t["id"] for t in cands]))
    for task in cands:
        if task["id"] not in gone:   # 删之前一瞬被调度器领走开跑了
            skipped.append({"id": task["id"], "reason": "执行中"})
            continue
        shutil.rmtree(_workspace_of(task), ignore_errors=True)
        if task.get("zip_path"):
            with suppress(OSError):
                Path(task["zip_path"]).unlink(missing_ok=True)
        _TITLE_CACHE.pop(task["id"], None)
        with _ai_jobs_lock:
            _ai_jobs.pop(task["id"], None)
    return sorted(gone), skipped


@app.delete("/api/tasks/<int:task_id>")
def delete_task(task_id):
    task = _task_or_404(task_id)
    deleted, skipped = _delete_tasks([task_id])
    if not deleted:
        reason = skipped[0]["reason"] if skipped else "无法删除"
        return jsonify({"error": f"任务{reason}, 暂时不能删除"}), 409
    _audit("task.delete", f"删除任务 #{task_id}: {task['target_url']}")
    return jsonify({"ok": True, "deleted": deleted})


@app.post("/api/tasks/batch/delete")
def batch_delete_tasks():
    ids = _task_ids_arg(request.get_json(silent=True) or {})
    if ids is None:
        return jsonify({"error": "请提供 task_ids 数组"}), 400
    deleted, skipped = _delete_tasks(ids)
    _audit("task.batch_delete", f"批量删除 {len(deleted)} 个任务(跳过 {len(skipped)})"
           + (f": #{', #'.join(map(str, deleted[:20]))}" if deleted else ""))
    return jsonify({"ok": True, "deleted": deleted, "skipped": skipped})


def _target_project(raw):
    """校验目标项目属于当前工作区, 返回项目 id; 非法返回 None。"""
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return None
    proj = db.get_project(pid)
    return pid if proj and proj.get("workspace_id") == _cur_ws() else None


@app.patch("/api/tasks/<int:task_id>")
def update_task_meta(task_id):
    """编辑任务: alias 自定义名称(空串 = 恢复自动标题), project_id 移到其他项目。"""
    task = _task_or_404(task_id)
    data = request.get_json(silent=True) or {}
    changed = []
    if "alias" in data:
        alias = re.sub(r"\s+", " ", str(data.get("alias") or "")).strip()
        if len(alias) > 80:
            return jsonify({"error": "名称最多 80 个字符"}), 400
        db.update_task(task_id, alias=alias)
        _TITLE_CACHE.pop(task_id, None)
        changed.append(f"名称「{alias}」" if alias else "名称恢复为自动")
    if data.get("project_id") not in (None, ""):
        pid = _target_project(data.get("project_id"))
        if pid is None:
            return jsonify({"error": "目标项目不存在"}), 400
        if db.move_tasks_to_project([task_id], pid):
            changed.append(f"移到项目 #{pid}")
    _audit("task.update", f"编辑任务 #{task_id}: {'; '.join(changed) or '无变更'}")
    out = _visible_task(task_id) or task
    out["stats"] = _parse_json(out.pop("stats_json", "") or "{}")
    out["title"] = _display_title(out, _workspace_of(out))
    return jsonify({"task": out})


@app.post("/api/tasks/batch/move")
def batch_move_tasks():
    data = request.get_json(silent=True) or {}
    ids = _task_ids_arg(data)
    if ids is None:
        return jsonify({"error": "请提供 task_ids 数组"}), 400
    pid = _target_project(data.get("project_id"))
    if pid is None:
        return jsonify({"error": "目标项目不存在"}), 400
    visible = [i for i in ids if _visible_task(i) is not None]
    moved = db.move_tasks_to_project(visible, pid)
    _audit("task.batch_move", f"{moved} 个任务移到项目 #{pid}")
    return jsonify({"ok": True, "moved": moved})


# ---------------------------------------------------------------------------
# Mock API 在线编辑与新增
# ---------------------------------------------------------------------------
@app.get("/api/tasks/<int:task_id>/mock")
def get_mock(task_id):
    task = _task_or_404(task_id)
    entries = _read_mock_entries(_workspace_of(task))
    for i, entry in enumerate(entries):
        entry["index"] = i
    return jsonify({"entries": entries})


@app.post("/api/tasks/<int:task_id>/mock")
def save_mock(task_id):
    task = _task_or_404(task_id)
    data = request.get_json(silent=True) or {}
    try:
        index = int(data.get("index", -1))
    except (TypeError, ValueError):
        index = -1
    body = str(data.get("response_body", ""))
    try:
        json.loads(body)
    except (ValueError, TypeError):
        return jsonify({"error": "响应体必须是合法 JSON"}), 400
    try:
        status_code = int(data.get("status", 200))
    except (TypeError, ValueError):
        status_code = 200
    ws = _workspace_of(task)
    entries = _read_mock_entries(ws)

    if index == -1:  # 新增自定义 Mock 路由
        path = str(data.get("path") or "").strip()
        if not path:
            return jsonify({"error": "新增 Mock 必须提供 path"}), 400
        if not path.startswith("/"):
            path = "/" + path
        method = str(data.get("method") or "GET").upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            method = "GET"
        entry = {"path": path, "method": method, "query": "",
                 "status": status_code, "response_body": body}
        entries.append(entry)
        new_index = len(entries) - 1
        action = f"新增 Mock {method} {path}"
    elif 0 <= index < len(entries):
        entries[index]["response_body"] = body
        entries[index]["status"] = status_code
        new_index = index
        action = f"更新 {entries[index]['path']}"
    else:
        return jsonify({"error": "接口索引越界"}), 400

    _write_mocks(task, ws, entries)
    bus.publish(task_id, "MOCK-API", f"已{action} 并重建 ZIP")
    _audit("mock.save", f"任务 #{task_id} {action}")
    return jsonify({"ok": True, "index": new_index, "entry": entries[new_index]})


def _write_mocks(task: dict, ws: Path, entries: list) -> None:
    """写回 mock_data.json 并同步到 Flask 工程 ZIP。"""
    task_id = task["id"]
    serialized = json.dumps(entries, ensure_ascii=False, indent=2)
    (ws / "mock_data.json").write_text(serialized, encoding="utf-8")
    build_dir = ws / "flask_app"
    if build_dir.is_dir():
        (build_dir / "mock_data.json").write_text(serialized, encoding="utf-8")
        zip_path = config.EXPORTS_DIR / f"{task_id}.zip"
        flask_generator.build_zip(build_dir, zip_path, f"clone_site_{task_id}")
    else:
        zip_path = _regenerate_project(task, ws)
    db.update_task(task_id, zip_path=str(zip_path))


@app.delete("/api/tasks/<int:task_id>/mock/<int:index>")
def delete_mock(task_id, index):
    task = _task_or_404(task_id)
    ws = _workspace_of(task)
    entries = _read_mock_entries(ws)
    if not 0 <= index < len(entries):
        return jsonify({"error": "接口索引越界"}), 400
    # 防止列表在别处被改过后按旧序号误删: 前端带上 method + path 做校验
    entry = entries[index]
    want_path = request.args.get("path")
    want_method = request.args.get("method")
    if (want_path is not None and want_path != entry.get("path")) or \
            (want_method is not None and want_method.upper() != str(entry.get("method", "GET")).upper()):
        return jsonify({"error": "接口列表已变化, 请刷新后再删"}), 409
    entries.pop(index)
    _write_mocks(task, ws, entries)
    action = f"删除 Mock {entry.get('method', 'GET')} {entry.get('path', '')}"
    bus.publish(task_id, "MOCK-API", f"已{action} 并重建 ZIP")
    _audit("mock.delete", f"任务 #{task_id} {action}")
    return jsonify({"ok": True, "count": len(entries)})


# ---------------------------------------------------------------------------
# 在线热补丁(CSS / HTML 片段注入并重打包)
# ---------------------------------------------------------------------------
def _read_hotpatch(ws: Path) -> dict:
    index_file = ws / "index.html"
    if not index_file.is_file():
        return {"css": "", "html": ""}
    text = index_file.read_text(encoding="utf-8", errors="replace")
    css_match = HOTPATCH_CSS_RE.search(text)
    html_match = HOTPATCH_HTML_RE.search(text)
    css = css_match.group(0)[len('<style id="clone-hotpatch">'):-len("</style>")] if css_match else ""
    html = ""
    if html_match:
        inner = html_match.group(0)
        html = inner[len("<!--clone-hotpatch-html-start-->"):-len("<!--clone-hotpatch-html-end-->")].strip()
    return {"css": css, "html": html}


@app.get("/api/tasks/<int:task_id>/patch")
def get_patch(task_id):
    task = _task_or_404(task_id)
    return jsonify(_read_hotpatch(_workspace_of(task)))


@app.post("/api/tasks/<int:task_id>/patch")
def post_patch(task_id):
    task = _task_or_404(task_id)
    data = request.get_json(silent=True) or {}
    css = data.get("css")
    html = data.get("html")
    if css is None and html is None:
        return jsonify({"error": "请提供 css 或 html 注入内容"}), 400
    ws = _workspace_of(task)
    index_file = ws / "index.html"
    if not index_file.is_file():
        return jsonify({"error": "任务尚未生成页面, 无法打补丁"}), 400
    text = index_file.read_text(encoding="utf-8", errors="replace")
    if css is not None:
        css = str(css).replace("</style", "<\\/")
        block = f'<style id="clone-hotpatch">{css}</style>'
        if HOTPATCH_CSS_RE.search(text):
            text = HOTPATCH_CSS_RE.sub(lambda _m: block, text, count=1)
        elif "<head" in text.lower():
            anchor = re.search(r"<head[^>]*>", text, re.I)
            insert_at = anchor.end() if anchor else 0
            text = text[:insert_at] + block + text[insert_at:]
        else:
            text = block + text
    if html is not None:
        html = str(html).replace("<!--", "<\\!--")
        block = f"<!--clone-hotpatch-html-start-->{html}<!--clone-hotpatch-html-end-->"
        if HOTPATCH_HTML_RE.search(text):
            text = HOTPATCH_HTML_RE.sub(lambda _m: block, text, count=1)
        elif "</body>" in text.lower():
            text = re.sub(r"</body>", block + "</body>", text, count=1, flags=re.I)
        else:
            text = text + block
    index_file.write_text(text, encoding="utf-8")

    zip_path = _regenerate_project(task, ws)
    db.update_task(task_id, zip_path=str(zip_path))
    bus.publish(task_id, "PATCH", "热补丁已注入并重新打包 Flask 工程")
    _audit("task.patch", f"任务 #{task_id} 注入热补丁(css={css is not None}, html={html is not None})")
    return jsonify({"ok": True, "zip": str(zip_path),
                    "patch": _read_hotpatch(ws)})


# ---------------------------------------------------------------------------
# 截图 / 资产审查 / 结构
# ---------------------------------------------------------------------------
@app.get("/api/tasks/<int:task_id>/screenshots/<img_type>")
def task_screenshot(task_id, img_type):
    _task_or_404(task_id)
    filename = SCREENSHOT_FILES.get(img_type)
    if filename is None:
        return jsonify({"error": "img_type 必须是 original / cloned / diff"}), 400
    task = db.get_task(task_id)
    ws = _workspace_of(task)
    target = _shot_file(ws, img_type)
    if target is None:
        return jsonify({"error": f"{filename} 尚未生成"}), 404
    # 评分截图可能是 JPEG 编码(config.SHOT_JPEG, 文件名仍 .png), 按内容给类型
    with open(target, "rb") as fh:
        is_jpeg = fh.read(3) == b"\xff\xd8\xff"
    return send_file(target, mimetype="image/jpeg" if is_jpeg else "image/png", max_age=0)


THUMB_WIDTH = 480
THUMB_MAX_HEIGHT = 400


@app.get("/api/tasks/<int:task_id>/thumb/<img_type>")
def task_thumb(task_id, img_type):
    """卡片缩略图: 整页截图按宽缩放并截取顶部, 缓存为 webp(源图更新后自动重建)。"""
    task = _task_or_404(task_id)
    filename = SCREENSHOT_FILES.get(img_type)
    if filename is None:
        return jsonify({"error": "img_type 必须是 original / cloned / diff"}), 400
    ws = _workspace_of(task)
    source = _shot_file(ws, img_type)
    if source is None:
        return jsonify({"error": f"{filename} 尚未生成"}), 404
    thumb = ws / f"{img_type}_thumb.webp"
    if not thumb.is_file() or thumb.stat().st_mtime < source.stat().st_mtime:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None  # 整页长截图可能超过默认像素上限
        with Image.open(source) as img:
            crop_h = min(img.height, round(THUMB_MAX_HEIGHT * img.width / THUMB_WIDTH))
            img = img.crop((0, 0, img.width, crop_h)).convert("RGB")
            height = max(1, round(crop_h * THUMB_WIDTH / img.width))
            img = img.resize((THUMB_WIDTH, height), Image.LANCZOS)
            tmp = thumb.with_suffix(f".{threading.get_ident()}.tmp")
            img.save(tmp, "WEBP", quality=80)
        tmp.replace(thumb)
    return send_file(thumb, mimetype="image/webp", max_age=86400)


@app.get("/api/tasks/<int:task_id>/assets")
def task_assets(task_id):
    task = _task_or_404(task_id)
    ws = _workspace_of(task)
    tree = _build_tree(ws, [TREE_NODE_CAP]) if ws.is_dir() else []
    return jsonify({"tree": tree, "workspace": str(ws)})


@app.get("/api/tasks/<int:task_id>/file")
def task_file(task_id):
    task = _task_or_404(task_id)
    ws = _workspace_of(task)
    rel = request.args.get("path", "").strip().lstrip("/")
    target = (ws / rel).resolve()
    if not _inside(ws, target):
        return jsonify({"error": "路径越界"}), 400
    if not target.is_file():
        return jsonify({"error": "文件不存在"}), 404
    if target.suffix.lower() not in TEXT_PREVIEW_EXTS:
        return jsonify({"error": f"不支持的预览类型 {target.suffix}"}), 400
    size = target.stat().st_size
    if size > 512 * 1024:
        return jsonify({"error": "文件超过 512KB, 暂不支持在线预览"}), 400
    content = target.read_text(encoding="utf-8", errors="replace")
    return jsonify({"path": rel, "size": size, "content": content})


@app.get("/api/tasks/<int:task_id>/tokens")
def task_tokens(task_id):
    """本任务的大模型 token 流水(界面生成各步 / AI 视觉修复各轮)与合计。"""
    _task_or_404(task_id)
    raw = db.task_token_usage(task_id)
    # 流水是每次请求一行(一次生成几十行): 详情页按「功能 · 步骤 · 模型」合并, 保持首次出现的顺序
    merged: dict = {}
    for r in raw:
        k = (r["kind"], r["step"], r["model"])
        m = merged.setdefault(k, {"kind": r["kind"], "kind_label": llm_usage.KINDS.get(r["kind"], r["kind"]),
                                  "step": r["step"], "model": r["model"], "calls": 0, "tokens_in": 0,
                                  "tokens_out": 0, "cost": 0.0, "unpriced": False})
        m["calls"] += 1
        m["tokens_in"] += r["tokens_in"]
        m["tokens_out"] += r["tokens_out"]
        if r.get("cost") is None:
            m["unpriced"] = m["unpriced"] or bool(r["tokens_in"] or r["tokens_out"])
        else:
            m["cost"] += r["cost"]
    rows = list(merged.values())
    for m in rows:
        m["cost"] = round(m["cost"], 6)
    tin = sum(r["tokens_in"] for r in rows)
    tout = sum(r["tokens_out"] for r in rows)
    return jsonify({"rows": rows, "tokens_in": tin, "tokens_out": tout, "total": tin + tout,
                    "calls": sum(r["calls"] for r in rows), "cost": round(sum(r["cost"] for r in rows), 6),
                    "unpriced": any(r["unpriced"] for r in rows), "currency": llm_usage.currency(),
                    "models": sorted({r["model"] for r in rows if r["model"]})})


@app.get("/api/usage/tokens")
def usage_tokens():
    """token 用量汇总: ?group=day|project|model &days=30(0 为全部)。数据来自 token_usage 流水。"""
    group = request.args.get("group", "day")
    if group not in ("day", "project", "model"):
        return jsonify({"error": "group 仅支持 day/project/model"}), 400
    try:
        days = max(0, min(3650, int(request.args.get("days", 30))))
    except ValueError:
        days = 30
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S") if days else ""
    rows = db.token_usage_summary(_cur_ws(), group, since)
    return jsonify({"group": group, "days": days, "rows": rows,
                    "tokens_in": sum(r["tokens_in"] for r in rows), "tokens_out": sum(r["tokens_out"] for r in rows)})


# ---------------------------------------------------------------------------
# 模型消耗: 总览(工作区管理员) / 项目消耗(成员可看) / 成员用量 / 月度预算 / 单价表(平台管理员)
# ---------------------------------------------------------------------------
LLM_PERIODS = (7, 30, 90)


def _utc_str(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _llm_days() -> int:
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        days = 30
    return days if days in LLM_PERIODS else 30


def _llm_series(wid: int, days: int, since: str, **flt) -> list:
    """近 N 天逐日序列(本机日期, 没有调用的日子补 0), 走势图用。"""
    got = {r["key"]: r for r in db.llm_usage_group(wid, "day", since, **flt)}
    today = datetime.now().date()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        r = got.get(d) or {}
        out.append({"date": d, "tokens": r.get("tokens", 0), "tokens_in": r.get("tokens_in", 0),
                    "tokens_out": r.get("tokens_out", 0), "cost": r.get("cost", 0), "calls": r.get("calls", 0)})
    return out


def _with_kind_labels(rows: list) -> list:
    for r in rows:
        r["label"] = llm_usage.KINDS.get(r["key"], r["key"] or "其他")
    return rows


def _with_price(rows: list) -> list:
    for r in rows:
        p = llm_usage.price_for(r["key"])
        r["price"] = {"input": p["input"], "output": p["output"], "match": p["match"]} if p else None
    return rows


def _budget_cfg(wid: int) -> dict:
    cfg = _parse_json(db.get_setting(f"llm_budget:{wid}", "") or "{}")
    try:
        monthly = max(0.0, float(cfg.get("monthly") or 0))
    except (TypeError, ValueError):
        monthly = 0.0
    try:
        pct = max(10, min(100, int(cfg.get("alert_pct") or 80)))
    except (TypeError, ValueError):
        pct = 80
    return {"monthly": monthly, "alert_pct": pct}


def _budget_status(wid: int) -> dict:
    """本月(每月 1 日起, UTC)估算费用 vs 月度预算。level: none(未设) / ok / warn(过提醒线) / over(超预算)。"""
    b = _budget_cfg(wid)
    spent = db.llm_usage_totals(wid, _month_start())
    used = spent["cost"]
    level, pct = "none", None
    if b["monthly"] > 0:
        pct = round(used / b["monthly"] * 100, 1)
        level = "over" if pct >= 100 else "warn" if pct >= b["alert_pct"] else "ok"
    return {"monthly": b["monthly"], "alert_pct": b["alert_pct"], "spent": round(used, 4),
            "tokens": spent["tokens"], "pct": pct, "level": level, "month": _month_start()[:7],
            "unpriced": spent["unpriced"]}


_budget_lock = threading.Lock()


def _budget_check(wid: int):
    """每记一笔就看一眼本月预算: 首次越过提醒线 / 超出预算时各提醒一次(事件流 + 审计), 同月不重复。"""
    if not db.get_setting(f"llm_budget:{wid}", ""):
        return
    st = _budget_status(wid)
    if st["level"] not in ("warn", "over"):
        return
    with _budget_lock:
        mark = f"{st['month']}:{st['level']}"
        key = f"llm_budget_note:{wid}"
        sent = db.get_setting(key, "")
        if sent == mark or (st["level"] == "warn" and sent == f"{st['month']}:over"):
            return
        db.set_setting(key, mark)
    cur = llm_usage.currency()
    msg = (f"本月模型消耗已超出预算: {cur}{st['spent']:.2f} / {cur}{st['monthly']:.2f}" if st["level"] == "over"
           else f"本月模型消耗已达预算的 {st['pct']:.0f}%: {cur}{st['spent']:.2f} / {cur}{st['monthly']:.2f}")
    with suppress(Exception):
        bus.publish(None, "BUDGET", msg, "WARN")
    with suppress(Exception):
        db.log_audit("system", "llm.budget", msg, "local", workspace_id=wid)


def _llm_sink(rec: dict):
    """core/llm_usage.SINK: 落库 + 预算提醒。"""
    wid = db.add_llm_call(rec)
    if rec.get("cost"):
        with suppress(Exception):
            _budget_check(wid or 1)


def _pricing_payload() -> dict:
    return {"currency": llm_usage.currency(), "prices": llm_usage.prices(), "unit": "每百万 token",
            "can_edit": (not _auth_required()) or _is_platform_admin(_current_user())}


@app.get("/api/v4/workspace/llm-usage")
def v4_llm_usage():
    """模型消耗总览: ?days=7|30|90。总量与上一周期对比、逐日走势、按模型 / 功能 / 项目 / 成员 / 任务。"""
    wid, days = _cur_ws(), _llm_days()
    now = datetime.now(timezone.utc)
    since, prev = _utc_str(now - timedelta(days=days)), _utc_str(now - timedelta(days=days * 2))
    by_model = _with_price(db.llm_usage_group(wid, "model", since))
    return jsonify({
        "days": days, "currency": llm_usage.currency(),
        "totals": db.llm_usage_totals(wid, since),
        "previous": db.llm_usage_totals(wid, prev, since),
        "all_time": db.llm_usage_totals(wid),
        "series": _llm_series(wid, days, since),
        "by_model": by_model,
        "by_kind": _with_kind_labels(db.llm_usage_group(wid, "kind", since)),
        "by_project": db.llm_usage_group(wid, "project", since),
        "by_user": db.llm_usage_group(wid, "user", since),
        "top_tasks": [r for r in db.llm_usage_group(wid, "task", since, limit=11) if r["key"]][:10],
        "unpriced_models": [r["key"] for r in by_model if r["price"] is None and r["tokens"]],
        "budget": _budget_status(wid),
        "pricing": _pricing_payload(),
        "open_mode": not _auth_required(),
    })


@app.get("/api/v4/workspace/llm-budget")
def v4_llm_budget_get():
    return jsonify(dict(_budget_status(_cur_ws()), currency=llm_usage.currency()))


@app.put("/api/v4/workspace/llm-budget")
def v4_llm_budget_put():
    """月度预算: {monthly: 金额(0 = 不设), alert_pct: 提醒线百分比 10~100}。"""
    wid = _cur_ws()
    data = request.get_json(silent=True) or {}
    try:
        monthly = float(data.get("monthly") or 0)
        pct = int(data.get("alert_pct") or 80)
    except (TypeError, ValueError):
        return jsonify({"error": "预算金额与提醒线须为数字"}), 400
    if monthly < 0 or monthly > 1e9:
        return jsonify({"error": "预算金额超出范围"}), 400
    if not 10 <= pct <= 100:
        return jsonify({"error": "提醒线须在 10% ~ 100% 之间"}), 400
    db.set_setting(f"llm_budget:{wid}", json.dumps({"monthly": round(monthly, 2), "alert_pct": pct}))
    db.set_setting(f"llm_budget_note:{wid}", "")      # 改了预算重新判断是否需要提醒
    cur = llm_usage.currency()
    _audit("llm.budget_update", f"月度模型预算: {cur}{monthly:.2f}, 提醒线 {pct}%" if monthly else "取消月度模型预算")
    return jsonify(dict(_budget_status(wid), currency=cur))


@app.get("/api/v4/users/llm-usage")
def v4_users_llm_usage():
    """用户管理页: 各成员近 N 天的模型用量(按用户名); ?username= 给出单人按模型 / 功能的明细。"""
    wid, days = _cur_ws(), _llm_days()
    since = _utc_str(datetime.now(timezone.utc) - timedelta(days=days))
    name = request.args.get("username")
    if name is not None:
        return jsonify({"days": days, "currency": llm_usage.currency(), "username": name,
                        "totals": db.llm_usage_totals(wid, since, username=name),
                        "by_model": db.llm_usage_group(wid, "model", since, username=name),
                        "by_kind": _with_kind_labels(db.llm_usage_group(wid, "kind", since, username=name)),
                        "series": _llm_series(wid, days, since, username=name)})
    rows = db.llm_usage_group(wid, "user", since)
    return jsonify({"days": days, "currency": llm_usage.currency(),
                    "users": {r["key"]: {k: r[k] for k in ("calls", "tokens", "cost", "unpriced")} for r in rows}})


@app.get("/api/projects/<int:project_id>/llm-usage")
def project_llm_usage(project_id):
    """项目页: 这个项目近 N 天的模型消耗(成员可看)。"""
    proj = db.get_project(project_id)
    wid = _cur_ws()
    if proj is None or proj.get("workspace_id") != wid:
        return jsonify({"error": "项目不存在"}), 404
    days = _llm_days()
    now = datetime.now(timezone.utc)
    since, prev = _utc_str(now - timedelta(days=days)), _utc_str(now - timedelta(days=days * 2))
    return jsonify({"days": days, "currency": llm_usage.currency(), "project_id": project_id,
                    "totals": db.llm_usage_totals(wid, since, project_id=project_id),
                    "previous": db.llm_usage_totals(wid, prev, since, project_id=project_id),
                    "series": _llm_series(wid, days, since, project_id=project_id),
                    "by_model": db.llm_usage_group(wid, "model", since, project_id=project_id),
                    "by_kind": _with_kind_labels(db.llm_usage_group(wid, "kind", since, project_id=project_id)),
                    "top_tasks": [r for r in db.llm_usage_group(wid, "task", since, project_id=project_id, limit=6)
                                  if r["key"]][:5]})


@app.get("/api/v4/llm-pricing")
def v4_llm_pricing_get():
    return jsonify(_pricing_payload())


@app.put("/api/v4/admin/llm-pricing")
def v4_llm_pricing_put():
    """单价表(平台级): {currency, prices: [{match, input, output}], recompute: bool}。
    未定价的旧调用总会按新表补算; recompute=true 时已有费用的历史流水也按新单价重算, 否则保留当时的估算。"""
    data = request.get_json(silent=True) or {}
    try:
        prices = llm_usage.normalize_prices(data.get("prices"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    cur = (str(data.get("currency") or "").strip() or llm_usage.currency())[:4]
    llm_usage.set_prices(prices, cur)
    db.set_setting("llm_pricing", json.dumps({"currency": cur, "prices": prices}, ensure_ascii=False))
    # 未定价的旧调用总会按新表补上费用; recompute=true 再把已有费用也按新单价重算
    n = db.recompute_token_costs(llm_usage.price_for, only_missing=not data.get("recompute"))
    _audit("llm.pricing_update", f"模型单价表: {len(prices)} 条, 货币 {cur}" + (f", 重算历史 {n} 行" if n and data.get("recompute") else ""))
    return jsonify(dict(_pricing_payload(), recomputed=n))


@app.get("/api/tasks/<int:task_id>/diff_regions")
def task_diff_regions(task_id):
    """差异视图的结论与差别区域(确定性计算, 带缓存): {area_pct, regions:[{x,y,w,h}(比例)], verdict}。"""
    from core import diff_regions
    task = _task_or_404(task_id)
    try:
        data = diff_regions.for_workspace(_workspace_of(task))
    except Exception as exc:
        return jsonify({"error": f"计算失败: {type(exc).__name__}"}), 500
    if data is None:
        return jsonify({"error": "这个任务没有可比较的截图"}), 404
    return jsonify(data)


@app.get("/api/tasks/<int:task_id>/live_code")
def task_live_code(task_id):
    """界面生成: 模型正在写的代码(增量)。?epoch=&since=&think_since= 为客户端已收到的位置;
    epoch 变了(进入新一轮写页面/修复)时从头返回 reset=true。非运行中或非生成任务返回 active=false。"""
    from core.uir import livecode
    task = _task_or_404(task_id)
    if task.get("status") != "running":
        return jsonify({"active": False})

    def _int(name):
        try:
            return max(0, int(request.args.get(name, 0)))
        except (TypeError, ValueError):
            return 0
    out = livecode.read(_workspace_of(task), _int("epoch"), _int("since"), _int("think_since"))
    return jsonify(out)


@app.get("/api/tasks/<int:task_id>/structure")
def task_structure(task_id):
    task = _task_or_404(task_id)
    ws = _workspace_of(task)
    build_dir = ws / "flask_app"
    if build_dir.is_dir():
        text = flask_generator.directory_tree_text(build_dir)
        has_flask = True
    else:
        text = flask_generator.directory_tree_text(ws)
        has_flask = False
    zip_name = Path(task["zip_path"]).name if task.get("zip_path") else ""
    return jsonify({"text": text, "has_flask": has_flask,
                    "zip_name": zip_name, "domain": task.get("domain", "")})


# ---------------------------------------------------------------------------
# AI 视觉闭环: 配置状态 / 报告查询 / 手动触发修复
# ---------------------------------------------------------------------------
@app.get("/api/ai/config")
def ai_config_state():
    from core.uir import llm as uir_llm
    gen_cfg = uir_llm.resolve_ai_cfg({})      # 生成侧另认仓库外的 Key 文件
    return jsonify({
        "key_configured": bool(config.AI_API_KEY),
        "generate_ready": bool(gen_cfg),
        "generate_model": (gen_cfg or {}).get("model", ""),
        "base_url": config.AI_API_BASE_URL,
        "model": config.AI_MODEL,
        "trigger_score": config.AI_REPAIR_TRIGGER_SCORE,
        "target_score": config.AI_REPAIR_TARGET_SCORE,
        "max_rounds": config.AI_REPAIR_MAX_ROUNDS,
    })


@app.get("/api/v4/ai/repairs")
def v4_ai_repairs():
    """AI 修复历史聚合: 扫描全部工作区的 ai_repair_report.json。"""
    items = []
    for task in db.list_tasks(status="completed", workspace_id=_cur_ws()):
        ws = Path(task["workspace_path"]) if task.get("workspace_path") else None
        report_file = ws / "ai_repair_report.json" if ws else None
        if report_file and report_file.is_file():
            try:
                report = json.loads(report_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            items.append({
                "task_id": task["id"], "domain": task["domain"],
                "trigger_score": report.get("trigger_score"),
                "final_score": report.get("final_score"),
                "rounds": len(report.get("rounds") or []),
                "model": report.get("model"),
                "updated_at": task["updated_at"],
            })
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return jsonify({"repairs": items, "total": len(items)})


@app.get("/api/tasks/<int:task_id>/ai_report")
def ai_report(task_id):
    _task_or_404(task_id)
    ws = _workspace_of(db.get_task(task_id))
    report_file = ws / "ai_repair_report.json"
    if not report_file.is_file():
        return jsonify({"exists": False})
    try:
        report = json.loads(report_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return jsonify({"exists": False})
    return jsonify({"exists": True, "report": report})


@app.post("/api/tasks/<int:task_id>/ai_repair")
def trigger_ai_repair(task_id):
    """对已完成任务手动触发 AI 视觉闭环(后台线程执行, 报告经 /ai_report 轮询)。"""
    task = _task_or_404(task_id)
    if task["status"] == "running":
        return jsonify({"error": "任务执行中, 请等待完成后再触发"}), 409
    ws = _workspace_of(task)
    if not (ws / "index.html").is_file():
        return jsonify({"error": "任务尚未生成页面"}), 400
    if not ((ws / "original_preview.png").is_file()
            and (ws / "cloned_preview.png").is_file()):
        return jsonify({"error": "缺少原站/克隆截图, 无法视觉比对"}), 400
    data = request.get_json(silent=True) or {}
    ai_cfg = {
        "base_url": (str(data.get("base_url") or "").strip()
                     or config.AI_API_BASE_URL).rstrip("/"),
        "api_key": str(data.get("api_key") or "").strip() or config.AI_API_KEY,
        "model": str(data.get("model") or "").strip() or config.AI_MODEL,
    }
    if not ai_cfg["api_key"]:
        return jsonify({"error": "未配置大模型 API Key(环境变量 CLONER_AI_KEY 或请求体 api_key)"}), 400
    ai_cfg = llm_usage.tag(ai_cfg, task_id=task_id, kind="ai_repair", username=_me_name())

    def _run():
        from core import ai_vision_repair, cloner_engine
        batch = db.get_task(task_id)
        try:
            cfg = json.loads((db.get_batch(batch["batch_id"]) or {}).get("config_json") or "{}")
        except ValueError:
            cfg = {}
        detail = {}
        detail_file = ws / "similarity_detail.json"
        if detail_file.is_file():
            with suppress(ValueError, OSError):
                detail = json.loads(detail_file.read_text(encoding="utf-8"))
        engine = cloner_engine.ClonerEngine(task_id, db, bus, pool)
        engine.cfg = cfg
        engine.bind_target(batch["target_url"])   # 目录型 URL: 补丁要落到影子页

        def log(stage, message):
            bus.publish(task_id, stage, message)

        try:
            db.update_task(task_id, stage="AI视觉修复")
            with pool.context(viewport={"width": 1920, "height": 1080}) as ctx:
                render_and_score = engine._make_render_and_score(ctx)
                report = ai_vision_repair.run_vision_repair(
                    ws, ai_cfg, detail, log, render_and_score,
                    page_file=engine._main_page_file(),
                    region_cells=engine._region_cells())
            final = report.get("final_score")
            if final is not None:
                db.update_task(task_id, similarity_score=final)
            _regenerate_project(batch, ws)
        except Exception as exc:  # noqa: BLE001 - 后台线程兜底
            bus.publish(task_id, "AI-VISION",
                        f"手动修复异常: {type(exc).__name__}: {exc}", "ERROR")
            db.update_task(task_id, stage="AI修复失败")

    threading.Thread(target=_run, name=f"ai-repair-{task_id}", daemon=True).start()
    bus.publish(task_id, "AI-VISION", "手动触发视觉闭环修复(后台执行中)")
    _audit("ai.repair", f"任务 #{task_id} 手动触发 AI 视觉修复")
    return jsonify({"ok": True, "poll": f"/api/tasks/{task_id}/ai_report"})


def _run_ai_repair_once(task_row: dict, ai_cfg: dict) -> dict:
    """对单个任务执行一次 AI 视觉修复(阻塞, 供手动/批量复用)。返回报告。"""
    from core import ai_vision_repair, cloner_engine   # 惰性导入: 曾漏导入, 手动 AI 修复恒 NameError
    task_id = task_row["id"]
    ws = _workspace_of(task_row)
    batch = db.get_batch(task_row["batch_id"])
    try:
        cfg = json.loads((batch or {}).get("config_json") or "{}")
    except ValueError:
        cfg = {}
    detail = {}
    detail_file = ws / "similarity_detail.json"
    if detail_file.is_file():
        with suppress(ValueError, OSError):
            detail = json.loads(detail_file.read_text(encoding="utf-8"))
    engine = cloner_engine.ClonerEngine(task_id, db, bus, pool)
    engine.cfg = cfg
    engine.bind_target(task_row["target_url"])

    def log(stage, message):
        bus.publish(task_id, stage, message)

    db.update_task(task_id, stage="AI视觉修复")
    with pool.context(viewport={"width": 1920, "height": 1080}) as ctx:
        render_and_score = engine._make_render_and_score(ctx)
        report = ai_vision_repair.run_vision_repair(
            ws, ai_cfg, detail, log, render_and_score,
            page_file=engine._main_page_file(), region_cells=engine._region_cells())
    final = report.get("final_score")
    if final is not None:
        db.update_task(task_id, similarity_score=final)
    _regenerate_project(task_row, ws)
    return report


@app.post("/api/v4/ai/mock-generate")
def v4_ai_mock_generate():
    """AI Mock 生成器: 按业务场景描述确定性生成接口 Mock 响应(离线可用)。"""
    data = request.get_json(silent=True) or {}
    scene = str(data.get("scene") or "").strip()[:500]
    path = str(data.get("path") or "/api/list").strip() or "/api/list"
    method = str(data.get("method") or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        method = "GET"
    entry = ai_mock_generator.generate_scene_mocks(
        scene, data.get("count", 5), path, method)
    return jsonify({"ok": True, "entry": entry})


@app.post("/api/v4/ai/batch_repair")
def v4_ai_batch_repair():
    """AI 批量修复队列: 多任务串行执行(每任务间隔 2s, 避免 browser 池打满)。

    Body: {task_ids: [...]}; 使用系统级已保存模型配置。
    """
    data = request.get_json(silent=True) or {}
    ids = data.get("task_ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "请提供 task_ids 数组"}), 400
    ai_cfg = {
        "base_url": config.AI_API_BASE_URL,
        "api_key": _effective_api_key(),
        "model": config.AI_MODEL,
    }
    if not ai_cfg["api_key"]:
        return jsonify({"error": "未配置模型 API Key(系统设置 → AI 供应商配置)"}), 400
    ai_cfg = llm_usage.tag(ai_cfg, kind="ai_repair", username=_me_name())

    queue = []
    for raw in ids[:50]:
        try:
            task_id = int(raw)
        except (TypeError, ValueError):
            continue
        task = _visible_task(task_id)
        if task is None or task["status"] == "running":
            continue
        ws = _workspace_of(task)
        if not (ws / "index.html").is_file() or not (ws / "original_preview.png").is_file():
            continue
        queue.append(task)

    def _run_queue():
        results = []
        for i, task in enumerate(queue):
            bus.publish(task["id"], "AI-VISION",
                        f"批量修复队列 {i + 1}/{len(queue)}")
            try:
                report = _run_ai_repair_once(task, ai_cfg)
                results.append({"task_id": task["id"], "ok": True,
                                "final_score": report.get("final_score")})
            except Exception as exc:  # noqa: BLE001
                results.append({"task_id": task["id"], "ok": False,
                                "error": f"{type(exc).__name__}: {exc}"[:200]})
                db.update_task(task["id"], stage="AI修复失败")
            time.sleep(2)
        bus.publish(None, "AI-VISION",
                    f"批量修复完成: {sum(1 for r in results if r['ok'])}/{len(results)} 成功")

    threading.Thread(target=_run_queue, name="ai-batch-repair", daemon=True).start()
    _audit("ai.batch_repair", f"批量修复队列: {len(queue)} 个任务")
    return jsonify({"ok": True, "queued": len(queue),
                    "task_ids": [t["id"] for t in queue]}), 202


# ---------------------------------------------------------------------------
# 通用 UI 重建引擎 · L1 生成(截图/描述 → UIR → HTML/CSS), 走现有任务调度与产物链路
# ---------------------------------------------------------------------------
@app.post("/api/v4/generate")
def api_v4_generate():
    """multipart/form-data 或 JSON:
    kind=screenshot|description; description=文字; image=文件(截图); viewport=desktop|laptop|mobile;
    target(默认 html_css); build(可选: apk 需 target=flutter, desktop 需 target=electron); target_score / max_rounds / time_budget(可选); ai_api_key/ai_model/ai_base_url(可选)。
    建一个 mode=generate 的批次 + 1 个任务, target_url 带 uir:generate: 前缀交由调度器分流。"""
    from core.uir import settings as gen_settings
    from core.uir.registry import list_inputs, list_outputs
    if request.content_type and request.content_type.startswith("multipart/"):
        form, files = request.form, request.files
    else:
        form, files = (request.get_json(silent=True) or {}), {}
    kind = str(form.get("kind") or "description").strip()
    if kind not in ("screenshot", "description"):
        return jsonify({"error": f"kind 仅支持 screenshot/description, 可用适配器: {list_inputs()}"}), 400
    target = str(form.get("target") or "html_css").strip()
    if target not in list_outputs():
        return jsonify({"error": f"未知输出目标 {target!r}, 可用: {list_outputs()}"}), 400
    description = str(form.get("description") or "").strip()[:gen_settings.GEN_MAX_DESC_CHARS]
    spec = {"kind": kind, "target": target, "description": description}
    build = str(form.get("build") or "").strip()
    need = {"apk": "flutter", "desktop": "electron"}
    if build and build not in need:
        return jsonify({"error": f"build 仅支持 apk/desktop, 收到 {build!r}"}), 400
    if build and need[build] != target:
        return jsonify({"error": f"build={build} 需要输出目标 {need[build]}"}), 400
    if build:
        spec["build"] = build
    for key, lo, hi in (("target_score", 1, 100), ("max_rounds", 1, 12), ("time_budget", 10, 3600)):
        raw = form.get(key)
        if raw not in (None, ""):
            try:
                spec[key] = max(lo, min(hi, float(raw) if key == "target_score" else int(float(raw))))
            except (TypeError, ValueError):
                return jsonify({"error": f"{key} 非法"}), 400
    vp_name = form.get("viewport")
    if vp_name in config.VIEWPORTS:
        spec["viewport"] = {"width": config.VIEWPORTS[vp_name]["width"], "height": config.VIEWPORTS[vp_name]["height"]}
    if kind == "screenshot":
        up = files.get("image") if files else None
        data_b64 = form.get("image_base64")
        gen_settings.GEN_INPUTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = f"{int(time.time() * 1000)}_{os.getpid()}"
        dest = gen_settings.GEN_INPUTS_DIR / f"{stamp}.png"
        try:
            if up is not None and up.filename:
                raw = up.read()
            elif data_b64:
                raw = base64.b64decode(str(data_b64).split(",", 1)[-1])
            else:
                return jsonify({"error": "截图输入需上传 image 文件或 image_base64"}), 400
            if len(raw) > gen_settings.GEN_MAX_IMAGE_BYTES:
                return jsonify({"error": "截图超过体积上限"}), 400
            from PIL import Image as _PILImage
            import io as _io
            with _PILImage.open(_io.BytesIO(raw)) as im:
                im = im.convert("RGB")
                if im.size[0] < 64 or im.size[1] < 64:
                    return jsonify({"error": "截图尺寸过小"}), 400
                im.save(dest, format="PNG")
        except Exception as exc:
            return jsonify({"error": f"截图不可解析: {type(exc).__name__}"}), 400
        spec["image_path"] = str(dest)
        spec["reference_text"] = str(form.get("reference_text") or "")[:20000]
    elif not description:
        return jsonify({"error": "描述输入不能为空"}), 400
    label = (description[:40] if description else f"截图 {time.strftime('%m-%d %H:%M')}")
    package = str(form.get("package") or "").strip()
    if package and package not in ("desktop", "apk"):
        return jsonify({"error": f"package 仅支持 desktop/apk, 收到 {package!r}"}), 400
    if package and target != "html_css":
        return jsonify({"error": "交付形态 desktop/apk 装壳需要输出目标 html_css(Flutter 原生 APK 用 build=apk)"}), 400
    cfg = {"mode": "generate", "gen": spec, "similarity": True, "package": package,
           "ai_api_key": str(form.get("ai_api_key") or "").strip()[:4096],
           "ai_base_url": str(form.get("ai_base_url") or "").strip()[:4096],
           "ai_model": str(form.get("ai_model") or "").strip()[:200]}
    if kind == "description" and gen_settings.use_code_track(kind, target, spec):
        from core.uir import llm as uir_llm
        if not uir_llm.resolve_ai_cfg(cfg):
            # 文字需求只能由模型来写; 没有模型时在提交处就说清楚, 不再入队后交付一张无关的模板页
            return jsonify({"error": "文字生成需要大模型: 请先在「系统设置 → AI 供应商」配置 Key, 或在提交时填写任务级 Key"}), 400
    project_id = form.get("project_id")
    project_id = int(project_id) if str(project_id or "").isdigit() else None
    if project_id is not None and (db.get_project(project_id) or {}).get("workspace_id") != _cur_ws():
        return jsonify({"error": f"项目 #{project_id} 不存在"}), 400
    name = str(form.get("name") or "").strip() or f"生成 · {label}"
    target_url = f"{gen_settings.TARGET_URL_PREFIX}{kind}:{label}"
    blocked = _quota_block(_cur_ws(), "clone", 1)
    if blocked:
        return blocked
    batch_id = db.create_batch(name, [(target_url, label[:60] or "生成页面")], cfg, project_id,
                               created_by=(_current_user() or {}).get("username", ""),
                               workspace_id=_cur_ws())
    task = db.list_tasks(batch_id=batch_id, limit=1, workspace_id=_cur_ws())
    task_id = task[0]["id"] if task else None
    scheduler.start()
    bus.publish(None, "SCHED", f"生成批次 #{batch_id} 已入队 ({kind} → {target})")
    _audit("generate.create", f"生成批次 #{batch_id}「{name}」({kind} → {target})")
    return jsonify({"batch_id": batch_id, "task_id": task_id, "name": name, "kind": kind, "target": target}), 201


@app.get("/api/v4/generate/<int:task_id>/report")
def api_v4_generate_report(task_id):
    task = _task_or_404(task_id)
    ws = _workspace_of(task)
    rep = ws / "generate_report.json"
    if not rep.is_file():
        return jsonify({"error": "生成报告尚未产出"}), 404
    payload = _parse_json(rep.read_text(encoding="utf-8"))
    uir = ws / "uir.json"
    payload["uir_stats"] = payload.get("uir_stats") or {}
    payload["has_uir"] = uir.is_file()
    return jsonify(payload)


@app.get("/api/tasks/<int:task_id>/artifact/<int:idx>")
@app.get("/api/v4/generate/<int:task_id>/artifact/<int:idx>")
def api_task_artifact(task_id, idx):
    """下载任务构建出的安装包(APK / macOS .app zip / Windows .exe zip), 只放行导出目录内、报告登记过的文件。
    来源: 交付形态后置构建(build_report.json, 克隆与生成任务通用) 或 界面生成的原生目标构建(generate_report.build)。"""
    task = _task_or_404(task_id)
    ws = _workspace_of(task)
    arts = []
    for rep, key in ((ws / "build_report.json", None), (ws / "generate_report.json", "build")):
        if rep.is_file():
            data = _parse_json(rep.read_text(encoding="utf-8")) or {}
            arts = (data.get(key) if key else data or {}) or {}
            arts = arts.get("artifacts") or []
            if arts:
                break
    if not 0 <= idx < len(arts):
        return jsonify({"error": "没有这个构建产物"}), 404
    name = Path(str(arts[idx].get("file") or "")).name
    path = (config.EXPORTS_DIR / name).resolve()
    if not name or path.parent != config.EXPORTS_DIR.resolve() or not path.is_file():
        return jsonify({"error": "构建产物文件不存在"}), 404
    return send_file(path, as_attachment=True, download_name=name)


@app.get("/api/v4/theme")
def v4_theme_get():
    user = _current_user()
    key = "theme:" + ((user or {}).get("username") or "guest")
    return jsonify({"theme": db.get_setting(key, "dark")})


@app.put("/api/v4/theme")
def v4_theme_put():
    data = request.get_json(silent=True) or {}
    mode = data.get("theme")
    if mode not in ("dark", "light", "system"):
        return jsonify({"error": "theme 需为 dark / light / system"}), 400
    user = _current_user()
    key = "theme:" + ((user or {}).get("username") or "guest")
    db.set_setting(key, mode)
    return jsonify({"ok": True, "theme": mode})


# ---------------------------------------------------------------------------
# AI 实验室: 交互式视觉修复(playground)
# ---------------------------------------------------------------------------
_ai_jobs: dict = {}   # task_id -> 视觉修复进度(内存, 平台重启即清)
_ai_jobs_lock = threading.Lock()


@app.get("/api/v4/ai/jobs")
def v4_ai_jobs():
    wid = _cur_ws()
    now = time.time()
    out = []
    for j in list(_ai_jobs.values()):
        if _auth_required() and j.get("workspace_id") != wid:
            continue
        if j["state"] != "running" and j.get("ended") and now - j["ended"] > 3600:
            continue
        out.append({k: v for k, v in j.items() if k != "workspace_id"}
                   | {"elapsed_s": round((j.get("ended") or now) - j["started"], 1)})
    out.sort(key=lambda j: -j["started"])
    return jsonify({"jobs": out})


@app.post("/api/v4/ai/playground-repair")
def v4_ai_playground_repair():
    """AI 实验室一键修复: 复用任务级视觉闭环, 支持附加用户 Prompt 提示。"""
    data = request.get_json(silent=True) or {}
    try:
        task_id = int(data.get("task_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "缺少 task_id"}), 400
    task = _visible_task(task_id)
    if task is None:
        return jsonify({"error": "任务不存在"}), 404
    ws = config.WORKSPACE_ROOT / str(task_id)
    if not (ws / "index.html").is_file():
        return jsonify({"error": "该任务尚未生成克隆页面"}), 400
    if not ((ws / "original_preview.png").is_file()
            and (ws / "cloned_preview.png").is_file()):
        return jsonify({"error": "缺少原站/克隆截图, 无法视觉比对"}), 400
    key = _effective_api_key()
    if not key:
        return jsonify({"error": "未配置大模型 API Key(系统设置 → AI 供应商)"}), 400
    prompt = str(data.get("prompt") or "").strip()[:2000]
    initiator = _me_name()
    with _ai_jobs_lock:   # 查重与登记须原子: 双击不会起两个修复线程改同一份工程
        job = _ai_jobs.get(task_id)
        if job and job.get("state") == "running":
            return jsonify({"error": "这个任务正在修复中"}), 409
        now = time.time()
        for tid, old in list(_ai_jobs.items()):   # 结束超过 1 小时的不再保留
            if old["state"] != "running" and now - (old.get("ended") or now) > 3600:
                _ai_jobs.pop(tid, None)
        job = {"task_id": task_id, "domain": task.get("domain") or "", "state": "running",
               "started": now, "ended": None, "before": task.get("similarity_score"),
               "after": None, "log": [], "prompt": prompt[:120],
               "workspace_id": task.get("workspace_id")}
        _ai_jobs[task_id] = job

    def _say(stage, msg):
        bus.publish(task_id, stage, msg)
        job["log"] = (job["log"] + [str(msg)[:160]])[-6:]

    def _run():
        from core import ai_vision_repair, cloner_engine
        try:
            db.update_task(task_id, stage="AI视觉修复")
            bus.publish(task_id, "AI-VISION", "AI 实验室: 视觉一键修复启动"
                        + (f"(自定义提示: {prompt[:60]}…)" if prompt else ""))
            detail = {}
            detail_file = ws / "similarity_detail.json"
            if detail_file.is_file():
                with suppress(ValueError, OSError):
                    detail = json.loads(detail_file.read_text(encoding="utf-8"))
            engine = cloner_engine.ClonerEngine(task_id, db, bus, pool)
            engine.bind_target(task["target_url"])
            ai_cfg = llm_usage.tag({"base_url": config.AI_API_BASE_URL, "api_key": key,
                                    "model": config.AI_MODEL, "user_prompt": prompt},
                                   task_id=task_id, kind="ai_repair", username=initiator)
            with pool.context(viewport={"width": 1920, "height": 1080}) as ctx:
                report = ai_vision_repair.run_vision_repair(
                    ws, ai_cfg, detail,
                    _say,
                    engine._make_render_and_score(ctx),
                    page_file=engine._main_page_file(),
                    region_cells=engine._region_cells())
            final = report.get("final_score")
            rounds = report.get("rounds") or []
            if rounds and all(r.get("error") for r in rounds):
                # 大模型一轮都没调通: 不是「修复完成」, 明确标失败并给出原因
                reason = str(rounds[-1].get("error") or "")[:200]
                db.update_task(task_id, stage="AI修复失败")
                job.update(state="failed", ended=time.time(),
                           log=(job["log"] + [f"大模型调用失败, 页面未改动: {reason}"])[-6:])
                return
            if final is not None:
                db.update_task(task_id, similarity_score=final)
            _regenerate_project(db.get_task(task_id), ws)
            job.update(state="done", after=final, ended=time.time())
        except Exception as exc:  # noqa: BLE001 - 后台线程兜底
            bus.publish(task_id, "AI-VISION",
                        f"AI 实验室修复异常: {type(exc).__name__}: {exc}", "ERROR")
            db.update_task(task_id, stage="AI修复失败")
            job.update(state="failed", ended=time.time(), log=(job["log"] + [f"{type(exc).__name__}: {exc}"[:200]])[-6:])

    threading.Thread(target=_run, name=f"ai-play-{task_id}", daemon=True).start()
    _audit("ai.playground", f"AI 实验室修复任务 #{task_id}")
    return jsonify({"ok": True, "poll": f"/api/tasks/{task_id}/ai_report"})


# ---------------------------------------------------------------------------
# 认证: 状态 / 初始化引导 / 登录 / 登出
# ---------------------------------------------------------------------------
def _workspace_payload(user) -> dict:
    """侧栏用: 当前工作区 + 我加入的全部工作区 + 当前区成员头像。"""
    wid, role = _ws_ctx()
    ws = db.get_workspace(wid) if wid else None
    out = {"id": wid, "name": (ws or {}).get("name") or "我的团队",
           "plan": (ws or {}).get("plan") or "free", "role": role,
           "members": [], "member_count": 0, "spaces": []}
    if user is not None and wid:
        members, total = db.workspace_members(wid)
        out["members"] = [{"name": m["display_name"] or m["username"], "role": m["role"]}
                          for m in members]
        out["member_count"] = total
        out["spaces"] = db.user_workspaces(user["id"])
    return out


@app.get("/api/auth/state")
def auth_state():
    user = _current_user()
    pub = _public_user(user)
    if pub is not None:
        pub["platform_admin"] = _is_platform_admin(user)
        pub["role"] = _ws_ctx()[1] or "viewer"  # 界面权限按当前工作区角色
    remote_setup = _open_mode_remote()
    return jsonify({"required": _auth_required() or remote_setup,
                    "open_mode": not _auth_required(),
                    "setup_token_required": remote_setup,
                    "captcha_required": _captcha_needed(None),
                    "session_hours": max(1, config.SESSION_HOURS),
                    "lock_policy": {"window_min": config.LOGIN_FAIL_WINDOW_MIN, "max": config.LOGIN_FAIL_MAX,
                                    "lock_min": config.LOGIN_LOCK_MIN},
                    "preview_base": _preview_base(),
                    "user": pub,
                    "workspace": _workspace_payload(user)})


# ---- 登录防护: 本地图形验证码 + 来源 IP 失败计数 ----
_ip_failures = websec.FailureCounter(max(1, config.LOGIN_FAIL_WINDOW_MIN) * 60)
_user_failures = websec.FailureCounter(max(1, config.LOGIN_FAIL_WINDOW_MIN) * 60)  # 按输入的用户名(小写)
# 用户不存在时也做一次同等代价的散列校验, 响应耗时不暴露账号是否存在
_DUMMY_HASH = security.hash_password(secrets.token_hex(16))


def _captcha_needed(username) -> bool:
    """auto: 这个用户名或这台设备最近输错够次数才要验证码; 正常登录不打扰。"""
    mode = config.CAPTCHA_MODE
    if mode == "off":
        return False
    if mode == "always":
        return True
    if config.CAPTCHA_IP_AFTER > 0 and _ip_failures.count(_client_ip()) >= config.CAPTCHA_IP_AFTER:
        return True
    if username and config.CAPTCHA_AFTER > 0 \
            and _user_failures.count(str(username).lower()) >= config.CAPTCHA_AFTER:
        return True
    return False


def _ip_blocked():
    if config.LOGIN_IP_MAX > 0 and _ip_failures.count(_client_ip()) >= config.LOGIN_IP_MAX:
        wait = _ip_failures.retry_after(_client_ip())
        resp = jsonify({"error": f"这台设备尝试登录失败次数过多, 请 {max(1, wait // 60)} 分钟后再试",
                        "retry_after": wait})
        resp.headers["Retry-After"] = str(wait)
        return resp, 429
    return None


@app.get("/api/auth/captcha")
def auth_captcha():
    """新的图形验证码(一次性, 5 分钟有效)。图片以 data URI 返回, 答案只存在服务端内存。"""
    resp = jsonify(captcha.new_challenge())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/auth/bootstrap")
def auth_bootstrap():
    """开放模式 -> 初始化首个管理员(同时完成登录)。其他机器上初始化须带终端里打印的一次性口令。"""
    if _auth_required():
        return jsonify({"error": "系统已初始化, 请直接登录"}), 409
    data = request.get_json(silent=True) or {}
    if _open_mode_remote():
        blocked = _ip_blocked()
        if blocked:
            return blocked
        import hmac as _hmac
        if not _hmac.compare_digest(str(data.get("setup_token") or "").strip(), _SETUP_TOKEN):
            _ip_failures.add(_client_ip())
            return jsonify({"error": "初始化口令不对。口令在启动平台的终端里, 也可以在本机打开平台直接初始化",
                            "setup": "token"}), 403
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not _USERNAME_RE.match(username):
        return jsonify({"error": "用户名需 2~32 位字母/数字/._-"}), 400
    err = _password_problem(password, username)
    if err:
        return jsonify({"error": err}), 400
    uid = db.create_user(username, security.hash_password(password),
                         "admin", str(data.get("display_name") or "").strip()[:60])
    db.add_member(1, uid, "owner")  # 首个管理员 = 平台管理员 + 默认工作区所有者
    _login_session(db.get_user(uid), ws=1)
    db.touch_login(uid, _client_ip())
    _audit("user.bootstrap", f"初始化管理员 {username}", username=username)
    return jsonify({"user": _public_user(db.get_user(uid))}), 201


@app.post("/api/auth/login")
def auth_login():
    if not _auth_required():
        return jsonify({"error": "系统处于开放模式, 请先初始化管理员账号"}), 400
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()[:64]
    password = str(data.get("password") or "")[:1024]
    blocked = _ip_blocked()
    if blocked:
        return blocked
    user = db.get_user_by_username(username)
    if _captcha_needed(username):
        if not captcha.verify(str(data.get("captcha_id") or ""), str(data.get("captcha") or "")):
            # 验证码不对不校验密码(不给撞库当密码对错的判定器), 也不累计账号失败, 只计来源 IP
            _ip_failures.add(_client_ip())
            given = bool(str(data.get("captcha") or "").strip())
            why = "验证码不正确" if given else "未填验证码"
            if user is not None:   # 留痕(记为登录失败, 计入审计的失败登录数); 不累计账号失败
                _audit_account(user, "login.failed", f"登录失败: {username}({why})")
            else:
                db.log_audit(username[:64] or "?", "login.failed", f"登录失败: {username[:64] or '(空)'}({why})",
                             _client_ip(), workspace_id=1)
            return jsonify({"error": "验证码不正确, 请重新输入" if given else "请输入验证码",
                            "captcha_required": True}), 400
    lock = iam.locked_until(user) if user else ""
    if lock:
        # 锁定期内不校验密码(也不再累计), 统一提示; 不泄露密码是否正确
        _audit_account(user, "login.failed", f"登录失败: {username}(账号锁定中)")
        return jsonify({"error": f"连续输错密码次数过多, 账号已临时锁定, {_lock_left(lock)}后自动解锁;"
                                 " 也可以联系管理员提前解锁", "locked_until": lock}), 423
    pw_ok = security.verify_password(password, user["password_hash"] if user else _DUMMY_HASH)
    if user is None or not pw_ok:
        _ip_failures.add(_client_ip())
        if user is None:
            db.log_audit(username[:64] or "?", "login.failed", f"登录失败: {username[:64] or '(空)'}(账号不存在)",
                         _client_ip(), workspace_id=1)
        else:
            _audit_account(user, "login.failed", f"登录失败: {username}(密码错误)")
            if iam.register_login_failure(db, user, config.LOGIN_FAIL_WINDOW_MIN,
                                          config.LOGIN_FAIL_MAX, config.LOGIN_LOCK_MIN):
                _audit_account(user, "login.locked",
                               f"{config.LOGIN_FAIL_WINDOW_MIN} 分钟内连续输错 {config.LOGIN_FAIL_MAX} 次,"
                               f" 锁定 {config.LOGIN_LOCK_MIN} 分钟")
        _user_failures.add(username.lower())  # 不管账号是否存在都计, 验证码出现与否不暴露账号存在性
        time.sleep(0.4)  # 暴力破解节流
        return jsonify({"error": "用户名或密码错误", "captcha_required": _captcha_needed(username)}), 401
    if not user["is_active"]:
        _audit_account(user, "login.blocked", "已停用账号尝试登录")
        return jsonify({"error": "账号已被停用, 请联系管理员"}), 403
    _user_failures.clear(username.lower())
    _login_session(user)
    db.touch_login(user["id"], _client_ip())
    _audit_account(user, "login", "登录成功")
    return jsonify({"user": _public_user(user)})


def _lock_left(lock: str) -> str:
    try:
        left = datetime.strptime(lock, "%Y-%m-%d %H:%M:%S") - datetime.utcnow()
    except ValueError:
        return "稍"
    return f"约 {max(1, int(left.total_seconds() // 60) + 1)} 分钟"


@app.post("/api/auth/logout")
def auth_logout():
    user = _current_user()
    if user:
        _audit_account(user, "logout", "退出登录")
    session.clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 多项目工作区
# ---------------------------------------------------------------------------
@app.get("/api/v4/projects")
def v4_projects():
    wid = _cur_ws()
    return jsonify({"projects": db.list_projects(workspace_id=wid),
                    "default_project_id": db.ensure_default_project(wid)})


@app.post("/api/v4/projects")
def v4_project_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not (2 <= len(name) <= 60):
        return jsonify({"error": "项目名称需 2~60 个字符"}), 400
    color = str(data.get("color") or "#6366f1").strip()
    if not re.match(r"^#[0-9A-Fa-f]{6}$", color):
        color = "#6366f1"
    pid = db.create_project(name, str(data.get("description") or "").strip()[:500], color,
                            created_by=(_current_user() or {}).get("username", ""),
                            workspace_id=_cur_ws())
    _audit("project.create", f"创建项目 #{pid}「{name}」")
    return jsonify({"project": db.get_project(pid)}), 201


@app.patch("/api/v4/projects/<int:project_id>")
def v4_project_update(project_id):
    _project_or_404(project_id)
    data = request.get_json(silent=True) or {}
    fields = {}
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not (2 <= len(name) <= 60):
            return jsonify({"error": "项目名称需 2~60 个字符"}), 400
        fields["name"] = name
    if "description" in data:
        fields["description"] = str(data.get("description") or "").strip()[:500]
    if "color" in data and re.match(r"^#[0-9A-Fa-f]{6}$", str(data.get("color"))):
        fields["color"] = data["color"]
    db.update_project(project_id, **fields)
    _audit("project.update", f"更新项目 #{project_id}: {', '.join(fields) or '无变更'}")
    return jsonify({"project": db.get_project(project_id)})


@app.delete("/api/v4/projects/<int:project_id>")
def v4_project_delete(project_id):
    _project_or_404(project_id)
    task_ids = db.project_task_ids(project_id)
    if task_ids:  # 级联: 先释放容器与工作空间, 再删数据
        for tid in task_ids:
            task = db.get_task(tid)
            if task is None:
                continue
            if db.get_docker(tid):   # 先下线部署(容器/本机进程), 否则会留下没人管的站点和端口
                with suppress(Exception):
                    deployer.destroy(tid)
            ws = _workspace_of(task)
            shutil.rmtree(ws, ignore_errors=True)
            if task.get("zip_path"):
                with suppress(OSError):
                    Path(task["zip_path"]).unlink(missing_ok=True)
    if not db.delete_project_cascade(project_id):
        return jsonify({"error": "默认项目不可删除"}), 409
    _audit("project.delete", f"级联删除项目 #{project_id}(含 {len(task_ids)} 个任务)")
    return jsonify({"ok": True, "removed_tasks": len(task_ids)})


@app.get("/api/v4/projects/search")
def v4_project_search():
    q = request.args.get("q", "").strip().lower()
    projects = [p for p in db.list_projects(workspace_id=_cur_ws())
                if not q or q in p["name"].lower()
                or q in (p.get("description") or "").lower()]
    return jsonify({"projects": projects})


@app.get("/api/v4/projects/<int:project_id>/export")
def v4_project_export(project_id):
    _project_or_404(project_id)
    task_ids = db.project_task_ids(project_id)
    zips = []
    for tid in task_ids:
        task = db.get_task(tid)
        if task and task.get("zip_path") and Path(task["zip_path"]).is_file():
            zips.append(task)
    if not zips:
        return jsonify({"error": "该项目暂无可导出的 ZIP"}), 404
    out = config.EXPORTS_DIR / f"project_{project_id}_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, task in enumerate(zips):
            zf.write(task["zip_path"],
                     arcname=f"{i + 1:02d}_{task['domain'] or 'site'}_{task['id']}.zip")
    _audit("project.export", f"导出项目 #{project_id} ZIP({len(zips)} 个任务)")
    return send_file(out, as_attachment=True,
                     download_name=f"project_{project_id}.zip")


# ---------------------------------------------------------------------------
# 网站记录存档: 打包(单个/批量/项目/全部) 与 导入(归属当前工作区)
# ---------------------------------------------------------------------------
def _archive_label(text: str) -> str:
    keep = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", text or "").strip("-")
    return keep[:40] or "records"


def _archive_select(args):
    """按 tasks=1,2,3 | project=<id> | all=1 选出当前工作区可打包的记录 -> (tasks, label) 或 (None, 错误响应)。
    运行中/排队的任务跳过(工作空间还在写)。"""
    wid = _cur_ws()
    label = ""
    if args.get("project"):
        try:
            proj = _project_or_404(int(args["project"]))
        except (TypeError, ValueError):
            abort(404)
        tasks = db.list_tasks(project_id=proj["id"], limit=archive.MAX_TASKS + 1, workspace_id=wid)
        label = proj["name"]
    elif args.get("tasks"):
        raw = args["tasks"]
        raw = raw.split(",") if isinstance(raw, str) else (raw if isinstance(raw, list) else [])
        ids = [int(x) for x in raw if str(x).strip().isdigit()][:archive.MAX_TASKS]
        tasks = [t for t in (db.get_task(i) for i in ids) if t and t.get("workspace_id") == wid]
        label = tasks[0]["domain"] if len(tasks) == 1 else f"{len(tasks)}-records"
    elif args.get("all"):
        tasks = db.list_tasks(limit=archive.MAX_TASKS + 1, workspace_id=wid)
        label = "all"
    else:
        return None, (jsonify({"error": "请指定要打包的记录"}), 400)
    tasks = [t for t in tasks if t.get("status") not in ("pending", "running")]
    tasks.sort(key=lambda t: t["id"])
    if not tasks:
        return None, (jsonify({"error": "没有可打包的记录(运行中的任务需等结束)"}), 404)
    if len(tasks) > archive.MAX_TASKS:
        return None, (jsonify({"error": f"单个存档最多 {archive.MAX_TASKS} 条记录, 请分批选择"}), 413)
    return (tasks, label), None


def _archive_prune():
    """清掉 1 小时前的存档临时包(下载中断/没人来取的)与对应的任务登记。"""
    now = time.time()
    with _archive_jobs_lock:
        for jid, j in list(_archive_jobs.items()):
            if j["state"] != "running" and now - j["ended"] > 3600:
                _archive_jobs.pop(jid, None)
        keep = {j["path"] for j in _archive_jobs.values()}
    for old in config.EXPORTS_DIR.glob("archive_*.zip"):
        with suppress(OSError):
            if str(old) not in keep and now - old.stat().st_mtime > 3600:
                old.unlink()


def _archive_build(out: Path, tasks: list, label: str, wid: int, username: str, progress=None):
    batches = {b: db.get_batch(b) for b in {t["batch_id"] for t in tasks}}
    projects = {p["id"]: p for p in db.list_projects(workspace_id=wid)}
    return archive.build(out, tasks, batches, projects, instance_id=db.instance_id(),
                         workspace_of=_workspace_of, label=label, exported_by=username, progress=progress)


def _archive_filename(label: str) -> str:
    return f"clonjurer-{_archive_label(label)}-{time.strftime('%Y%m%d')}.zip"


@app.get("/api/v4/archive")
def v4_archive_export():
    """同步打包并直接下载(脚本/小存档用); 界面走下面的后台任务。"""
    wid = _cur_ws()
    picked, err = _archive_select(request.args)
    if err:
        return err
    tasks, label = picked
    _archive_prune()
    out = config.EXPORTS_DIR / f"archive_{wid}_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}.zip"
    _archive_build(out, tasks, label, wid, (_current_user() or {}).get("username", ""))
    _audit("archive.export", f"打包存档 {len(tasks)} 条记录({label})")
    resp = send_file(out, as_attachment=True, mimetype="application/zip",
                     download_name=_archive_filename(label))
    resp.call_on_close(lambda: out.unlink(missing_ok=True))
    return resp


# 后台打包: 整库存档可达十几 GB、几分钟, 同步请求会占住一个工作线程且过不了反代超时
_archive_jobs: dict = {}   # job_id -> 进度(内存, 平台重启即清; 临时包留 1 小时)
_archive_jobs_lock = threading.Lock()


class _ArchiveCancelled(Exception):
    pass


def _archive_job_out(j: dict) -> dict:
    return {k: j[k] for k in ("id", "state", "label", "total", "done", "bytes", "size", "error", "filename")} | {
        "elapsed_s": round((j["ended"] or time.time()) - j["started"], 1)}


def _archive_job_or_404(job_id: str) -> dict:
    j = _archive_jobs.get(job_id)
    if j is None or j["workspace_id"] != _cur_ws():
        abort(404)
    return j


@app.get("/api/v4/archive/jobs")
def v4_archive_jobs():
    wid = _cur_ws()
    return jsonify({"jobs": [_archive_job_out(j) for j in list(_archive_jobs.values())
                             if j["workspace_id"] == wid]})


@app.post("/api/v4/archive/jobs")
def v4_archive_job_create():
    """body: {tasks:[id..]} | {project:id} | {all:true} -> 202 {job}。只读成员也可打包(只读操作)。"""
    wid = _cur_ws()
    picked, err = _archive_select(request.get_json(silent=True) or {})
    if err:
        return err
    tasks, label = picked
    _archive_prune()
    username = (_current_user() or {}).get("username", "")
    with _archive_jobs_lock:
        if any(j["workspace_id"] == wid and j["state"] == "running" for j in _archive_jobs.values()):
            return jsonify({"error": "已有一个存档在打包, 等它完成再开始"}), 409
        jid = secrets.token_hex(8)
        out = config.EXPORTS_DIR / f"archive_{wid}_{time.strftime('%Y%m%d_%H%M%S')}_{jid[:6]}.zip"
        job = {"id": jid, "workspace_id": wid, "state": "running", "label": label, "total": len(tasks),
               "done": 0, "bytes": 0, "size": 0, "error": "", "path": str(out), "cancel": False,
               "filename": _archive_filename(label), "started": time.time(), "ended": 0.0}
        _archive_jobs[jid] = job

    def tick(done, nbytes):
        job["done"], job["bytes"] = done, nbytes
        if job["cancel"]:
            raise _ArchiveCancelled()

    def run():
        try:
            _archive_build(out, tasks, label, wid, username, progress=tick)
            job["size"] = out.stat().st_size
            job["state"] = "done"
        except _ArchiveCancelled:
            job["state"], job["error"] = "cancelled", "已取消"
        except Exception as exc:  # noqa: BLE001 - 后台线程: 任何失败都要落到任务状态上
            job["state"], job["error"] = "error", str(exc)[:300]
        finally:
            job["ended"] = time.time()
            if job["state"] != "done":
                out.unlink(missing_ok=True)

    threading.Thread(target=run, name=f"archive-{jid[:6]}", daemon=True).start()
    _audit("archive.export", f"打包存档 {len(tasks)} 条记录({label})")
    return jsonify({"job": _archive_job_out(job)}), 202


@app.get("/api/v4/archive/jobs/<job_id>/download")
def v4_archive_job_download(job_id):
    j = _archive_job_or_404(job_id)
    if j["state"] != "done" or not Path(j["path"]).is_file():
        return jsonify({"error": "存档已过期, 请重新打包"}), 404
    return send_file(j["path"], as_attachment=True, mimetype="application/zip",
                     download_name=j["filename"])


@app.delete("/api/v4/archive/jobs/<job_id>")
def v4_archive_job_cancel(job_id):
    """取消进行中的打包, 或丢弃已完成的临时包。"""
    j = _archive_job_or_404(job_id)
    if j["state"] == "running":
        j["cancel"] = True
    else:
        Path(j["path"]).unlink(missing_ok=True)
        _archive_jobs.pop(job_id, None)
    return jsonify({"ok": True})


@app.post("/api/v4/archive/import")
def v4_archive_import():
    """multipart: file=存档 zip, project_id=可选(全部放进该项目)。只读成员被 _rbac_gate 拦在门外。"""
    wid = _cur_ws()
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "请选择存档文件"}), 400
    pid = request.form.get("project_id") or ""
    project_id = None
    if pid.isdigit():
        project_id = _project_or_404(int(pid))["id"]
    tmp = config.EXPORTS_DIR / f"import_{secrets.token_hex(6)}.zip"
    try:
        f.save(tmp)
        user = _current_user() or {}
        res = archive.import_archive(tmp, db=db, workspace_id=wid, workspace_root=config.WORKSPACE_ROOT,
                                     created_by=user.get("username", ""), project_id=project_id,
                                     sanitize_config=_sanitize_config)
    except archive.ArchiveError as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        tmp.unlink(missing_ok=True)
    _audit("archive.import", f"导入存档 {res['imported']} 条记录, 跳过已存在 {res['skipped']} 条"
           + (f"({res['label']})" if res.get("label") else ""))
    return jsonify(res), 201


# ---------------------------------------------------------------------------
# 用户管理(admin)
# ---------------------------------------------------------------------------
_ROLE_TEXT = {"owner": "所有者", "admin": "管理员", "operator": "成员", "viewer": "只读"}


def _member_out(m: dict, counts: dict = None) -> dict:
    return {"id": m["id"], "username": m["username"], "display_name": m["display_name"],
            "role": m["role"], "is_active": m["is_active"], "last_login_at": m["last_login_at"],
            "last_login_ip": m.get("last_login_ip") or "",
            "status": iam.status_of(m), "locked_until": iam.locked_until(m),
            "platform_admin": m.get("account_role") == "admin",
            "actions_30d": (counts or {}).get(m["username"], 0),
            "created_at": m["joined_at"] or m["created_at"]}


def _members_with_stats(wid: int) -> list:
    since = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    counts = db.audit_counts_by_user(wid, since)
    return [_member_out(m, counts) for m in db.list_members(wid)]


@app.get("/api/v4/users")
def v4_users():
    """当前工作区的成员(角色为本区角色) + 待接受邀请 + 概览计数。"""
    wid = _cur_ws()
    users = _members_with_stats(wid)
    _, my_role = _ws_ctx()
    summary = {"total": len(users),
               "active": sum(1 for u in users if u["status"] == "active"),
               "disabled": sum(1 for u in users if u["status"] == "disabled"),
               "locked": sum(1 for u in users if u["status"] == "locked"),
               "managers": sum(1 for u in users if u["role"] in ("owner", "admin")),
               "never_logged_in": sum(1 for u in users if not u["last_login_at"])}
    return jsonify({"users": users, "invites": db.list_invites(wid), "summary": summary,
                    "me": {"role": my_role, "platform_admin": _is_platform_admin(_current_user())
                           or not _auth_required()},
                    "policy": {"lock_after": config.LOGIN_FAIL_MAX,
                               "lock_window_min": config.LOGIN_FAIL_WINDOW_MIN,
                               "lock_minutes": config.LOGIN_LOCK_MIN,
                               "session_hours": int(app.permanent_session_lifetime.total_seconds() // 3600),
                               "audit_retention_days": config.AUDIT_RETENTION_DAYS}})


@app.get("/api/v4/users/export")
def v4_users_export():
    wid = _cur_ws()
    members = _members_with_stats(wid)
    body = iam.members_csv([{**m, "joined_at": m["created_at"]} for m in members],
                           lambda r: _ROLE_TEXT.get(r, r or ""))
    _audit("audit.export", f"导出成员名单 CSV({len(members)} 人)")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return Response(body, content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="members_{stamp}.csv"'})


@app.get("/api/v4/users/<int:user_id>")
def v4_user_detail(user_id):
    """成员详情: 资料、本区角色、本区内最近操作与登录记录(不跨区)。"""
    wid = _cur_ws()
    target = db.get_user(user_id)
    m = db.get_membership(wid, user_id) if target else None
    if m is None:
        return jsonify({"error": "用户不存在"}), 404
    row = next((x for x in db.list_members(wid) if x["id"] == user_id), None) or {}
    counts = db.audit_counts_by_user(
        wid, (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"))
    inviter = db.get_user(m["invited_by"]) if m.get("invited_by") else None
    auth_actions = ["login", "login.failed", "login.blocked", "login.locked", "logout"]
    others = [a for a in db.audit_actions(wid) if a not in auth_actions]
    return jsonify({
        "user": {**_member_out(row, counts),
                 "failed_logins": row.get("failed_logins") or 0,
                 "invited_by": (inviter or {}).get("username") or "",
                 "workspace_count": len(db.user_workspaces(user_id))},
        "logins": [iam.decorate(e) for e in
                   db.user_audit(target["username"], workspace_id=wid, actions=auth_actions, limit=15)],
        "activity": [iam.decorate(e) for e in
                     db.user_audit(target["username"], workspace_id=wid,
                                   actions=others or ["__none__"], limit=20)],
    })


def _member_change(wid: int, my_role: str, target: dict, m: dict, op: str, value=None):
    """对一位成员执行一项变更 -> (审计动作, 说明) 或抛 _MemberError(提示, 状态码)。
    单条 PATCH 与批量操作共用, 规则一致: 所有者只能由所有者改动; 不能降级/移除最后一个管理者;
    账号级动作(停用/启用/重置他人密码/强制下线)需要平台管理员。"""
    me = _current_user()
    platform = _is_platform_admin(me) or not _auth_required()
    is_self = bool(me) and me["id"] == target["id"]
    name = target["username"]
    if op == "role":
        if value not in WS_ROLE_RANK:
            raise _MemberError("非法角色", 400)
        if value == m["role"]:
            return None
        if "owner" in (value, m["role"]) and my_role != "owner":
            raise _MemberError("只有所有者可以转让或变更所有者", 403)
        if m["role"] in ("owner", "admin") and value not in ("owner", "admin") \
                and db.count_managers(wid) <= 1:
            raise _MemberError("不能降级最后一个管理员", 409)
        db.set_member_role(wid, target["id"], value)
        return "member.role", f"{name}: {_ROLE_TEXT.get(m['role'])} → {_ROLE_TEXT.get(value)}"
    if op in ("disable", "enable"):
        if not platform:
            raise _MemberError("停用账号需要平台管理员; 可改为把成员移出本工作区", 403)
        if op == "disable":
            if is_self:
                raise _MemberError("不能停用自己的账号", 409)
            if m["role"] in ("owner", "admin") and db.count_managers(wid) <= 1:
                raise _MemberError("不能停用最后一个管理员", 409)
            if not target["is_active"]:
                return None
            db.update_user(target["id"], is_active=0)
            db.bump_session_ver(target["id"])
            return "user.disable", f"停用账号 {name}(已登录的会话同时失效)"
        if target["is_active"]:
            return None
        db.update_user(target["id"], is_active=1)
        db.reset_login_failures(target["id"])
        _user_failures.clear(target["username"].lower())
        return "user.enable", f"启用账号 {name}"
    if op == "password":
        if not (platform or is_self):
            raise _MemberError("重置他人密码需要平台管理员", 403)
        err = _password_problem(str(value or ""), target["username"])
        if err:
            raise _MemberError(err, 400)
        db.update_user(target["id"], password_hash=security.hash_password(str(value)))
        db.reset_login_failures(target["id"])
        _user_failures.clear(target["username"].lower())
        sv = db.bump_session_ver(target["id"])
        if is_self:
            session["sv"] = sv  # 自己改密: 其他设备下线, 当前会话保留
        return "user.password", (f"{name} 修改了自己的密码" if is_self
                                 else f"重置 {name} 的密码(已登录的会话同时失效)")
    if op == "display_name":
        if not (platform or is_self):
            raise _MemberError("只能修改自己的显示名", 403)
        new = str(value or "").strip()[:60] or name
        if new == target.get("display_name"):
            return None
        db.update_user(target["id"], display_name=new)
        return "user.update", f"{name} 显示名改为「{new}」"
    if op == "unlock":
        if not iam.locked_until(target) and not target.get("failed_logins"):
            return None
        db.reset_login_failures(target["id"])
        _user_failures.clear(target["username"].lower())
        return "user.unlock", f"解除 {name} 的登录锁定"
    if op == "logout":
        if not platform:
            raise _MemberError("强制下线需要平台管理员; 可改为把成员移出本工作区", 403)
        if is_self:
            raise _MemberError("不能强制自己下线, 请直接退出登录", 409)
        db.bump_session_ver(target["id"])
        return "user.force_logout", f"强制 {name} 在所有设备下线"
    if op == "remove":
        if is_self:
            raise _MemberError("不能移除自己; 请改用离开工作区", 409)
        if m["role"] == "owner" and my_role != "owner":
            raise _MemberError("只有所有者可以移除所有者", 403)
        if m["role"] in ("owner", "admin") and db.count_managers(wid) <= 1:
            raise _MemberError("不能移除最后一个管理员", 409)
        db.remove_member(wid, target["id"])
        return "member.remove", f"移出成员 {name}({_ROLE_TEXT.get(m['role'])})"
    raise _MemberError("不支持的操作", 400)


class _MemberError(Exception):
    def __init__(self, msg: str, status: int):
        super().__init__(msg)
        self.status = status


@app.post("/api/v4/users/<int:user_id>/<op>")
def v4_user_action(user_id, op):
    """单个成员的动作: unlock 解锁 / logout 强制下线 / disable 停用 / enable 启用。"""
    if op not in ("unlock", "logout", "disable", "enable"):
        return jsonify({"error": "不支持的操作"}), 404
    wid, my_role = _ws_ctx()
    target = db.get_user(user_id)
    m = db.get_membership(wid, user_id) if target else None
    if m is None:
        return jsonify({"error": "用户不存在"}), 404
    try:
        res = _member_change(wid, my_role, target, m, op)
    except _MemberError as e:
        return jsonify({"error": str(e)}), e.status
    if res:
        _audit(*res)
    return jsonify({"ok": True, "changed": bool(res)})


@app.post("/api/v4/users/bulk")
def v4_users_bulk():
    """批量: {ids: [...], op: role|remove|disable|enable|unlock|logout, role?}。
    逐个按单条规则执行, 返回每个人的结果; 整批只记一条汇总审计 + 每人一条明细。"""
    data = request.get_json(silent=True) or {}
    op = data.get("op")
    if op not in ("role", "remove", "disable", "enable", "unlock", "logout"):
        return jsonify({"error": "不支持的批量操作"}), 400
    ids = [int(i) for i in (data.get("ids") or []) if str(i).isdigit()][:200]
    if not ids:
        return jsonify({"error": "请先勾选成员"}), 400
    wid, my_role = _ws_ctx()
    results, done = [], 0
    for uid in dict.fromkeys(ids):
        target = db.get_user(uid)
        m = db.get_membership(wid, uid) if target else None
        if m is None:
            results.append({"id": uid, "ok": False, "error": "用户不存在"})
            continue
        try:
            res = _member_change(wid, my_role, target, m, op, data.get("role"))
        except _MemberError as e:
            results.append({"id": uid, "username": target["username"], "ok": False, "error": str(e)})
            continue
        if res:
            _audit(*res)
            done += 1
        results.append({"id": uid, "username": target["username"], "ok": True, "changed": bool(res)})
    label = {"role": f"改为{_ROLE_TEXT.get(data.get('role'), '?')}", "remove": "移出",
             "disable": "停用", "enable": "启用", "unlock": "解锁", "logout": "强制下线"}[op]
    failed = sum(1 for r in results if not r["ok"])
    _audit("member.bulk", f"批量{label} {len(results)} 人: 生效 {done}, 失败 {failed}")
    return jsonify({"ok": failed == 0, "done": done, "failed": failed, "results": results})


@app.post("/api/v4/users")
def v4_user_create():
    """管理员直接给本区建账号(私有化/内网场景); SaaS 场景用邀请链接。
    已存在的账号直接加入本区。"""
    data = request.get_json(silent=True) or {}
    wid, my_role = _ws_ctx()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    role = data.get("role") if data.get("role") in ("viewer", "operator", "admin") else "viewer"
    if role == "admin" and WS_ROLE_RANK.get(my_role or "", 0) < WS_ROLE_RANK["admin"]:
        return jsonify({"error": "无权授予管理员"}), 403
    if not _USERNAME_RE.match(username):
        return jsonify({"error": "用户名需 2~32 位字母/数字/._-"}), 400
    blocked = _quota_block(wid, "member")
    if blocked:
        return blocked
    existing = db.get_user_by_username(username)
    if existing:
        if db.get_membership(wid, existing["id"]):
            return jsonify({"error": "该用户已是本工作区成员"}), 409
        if not _is_platform_admin(_current_user()):
            return jsonify({"error": "用户名已被占用; 已有账号请用邀请链接加入"}), 409
        db.add_member(wid, existing["id"], role, (_current_user() or {}).get("id"))
        _audit("member.add", f"加入成员 {username}({role})")
        return jsonify({"user": _public_user(existing)}), 201
    err = _password_problem(password, username)
    if err:
        return jsonify({"error": err}), 400
    uid = db.create_user(username, security.hash_password(password), "user",
                         str(data.get("display_name") or "").strip()[:60])
    db.add_member(wid, uid, role, (_current_user() or {}).get("id"))
    _audit("user.create", f"创建成员 {username}({role})")
    return jsonify({"user": _public_user(db.get_user(uid))}), 201


@app.patch("/api/v4/users/<int:user_id>")
def v4_user_update(user_id):
    wid, my_role = _ws_ctx()
    target = db.get_user(user_id)
    m = db.get_membership(wid, user_id) if target else None
    if m is None:
        return jsonify({"error": "用户不存在"}), 404
    data = request.get_json(silent=True) or {}
    # 逐项执行(角色 → 启停 → 显示名 → 密码), 每项单独记审计; 某项被拒时前面已生效的保留并如实返回
    steps = []
    if "role" in data:
        steps.append(("role", data.get("role")))
    if "is_active" in data:  # 账号停用是全局动作: 本区内语义为"移出本区"由 DELETE 承担
        steps.append(("enable" if data.get("is_active") in (True, 1, "1") else "disable", None))
    if "display_name" in data:
        steps.append(("display_name", data.get("display_name")))
    if data.get("password"):
        steps.append(("password", data.get("password")))
    applied = []
    for op, value in steps:
        try:
            res = _member_change(wid, my_role, db.get_user(user_id), m, op, value)
        except _MemberError as e:
            return jsonify({"error": str(e), "applied": applied}), e.status
        if res:
            _audit(*res)
            applied.append(op)
        m = db.get_membership(wid, user_id) or m
    u = _member_out({**(db.get_user(user_id) or {}), "role": m.get("role"),
                     "joined_at": m.get("joined_at"), "account_role": target.get("role")})
    return jsonify({"user": u, "applied": applied})


@app.delete("/api/v4/users/<int:user_id>")
def v4_user_delete(user_id):
    """把成员移出当前工作区(账号本身保留, 仍可属于其他工作区)。"""
    wid, my_role = _ws_ctx()
    target = db.get_user(user_id)
    m = db.get_membership(wid, user_id) if target else None
    if m is None:
        return jsonify({"error": "用户不存在"}), 404
    try:
        _audit(*_member_change(wid, my_role, target, m, "remove"))
    except _MemberError as e:
        return jsonify({"error": str(e)}), e.status
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 工作区: 列出 / 新建 / 切换 / 改名, 邀请链接
# ---------------------------------------------------------------------------
_INVITE_ROLES = ("viewer", "operator", "admin")


@app.get("/api/v4/workspaces")
def v4_workspaces():
    user = _current_user()
    if user is None:
        return jsonify({"spaces": [{"id": 1, "name": (db.get_workspace(1) or {}).get("name"),
                                    "role": "owner"}], "current": 1})
    return jsonify({"spaces": db.user_workspaces(user["id"]), "current": _ws_ctx()[0]})


@app.post("/api/v4/workspaces")
def v4_workspace_create():
    user = _current_user()
    if user is None:
        return jsonify({"error": "请先启用团队登录"}), 400
    name = str((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not (2 <= len(name) <= 40):
        return jsonify({"error": "工作区名称需 2~40 个字符"}), 400
    wid = db.create_workspace(name, user["id"], plan=config.NEW_WORKSPACE_PLAN)
    session["ws"] = wid
    g.pop("ws_ctx", None)
    _audit("workspace.create", f"新建工作区 #{wid}「{name}」", workspace_id=wid)
    return jsonify({"workspace": db.get_workspace(wid)}), 201


@app.post("/api/v4/workspaces/switch")
def v4_workspace_switch():
    user = _current_user()
    wid = (request.get_json(silent=True) or {}).get("id")
    if user is None or not str(wid or "").isdigit() or not db.get_membership(int(wid), user["id"]):
        return jsonify({"error": "工作区不存在"}), 404
    session["ws"] = int(wid)
    g.pop("ws_ctx", None)
    return jsonify({"ok": True, "workspace": db.get_workspace(int(wid))})


@app.post("/api/v4/workspaces/leave")
def v4_workspace_leave():
    user = _current_user()
    wid, role = _ws_ctx()
    if user is None or wid is None:
        return jsonify({"error": "工作区不存在"}), 404
    if role in ("owner", "admin") and db.count_managers(wid) <= 1:
        return jsonify({"error": "你是最后一个管理员, 请先把管理员交给其他成员"}), 409
    db.remove_member(wid, user["id"])
    session.pop("ws", None)
    _audit("member.leave", f"{user['username']} 离开工作区", workspace_id=wid)
    return jsonify({"ok": True})


@app.patch("/api/v4/workspace/settings")
def v4_workspace_update():
    name = str((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not (2 <= len(name) <= 40):
        return jsonify({"error": "工作区名称需 2~40 个字符"}), 400
    db.update_workspace(_cur_ws(), name=name)
    _audit("workspace.update", f"工作区改名为「{name}」")
    return jsonify({"workspace": db.get_workspace(_cur_ws())})


def _invite_hash(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


@app.post("/api/v4/invites")
def v4_invite_create():
    data = request.get_json(silent=True) or {}
    role = data.get("role") if data.get("role") in _INVITE_ROLES else "operator"
    days = max(1, min(30, int(data.get("days") or 7)))
    uses = max(1, min(50, int(data.get("max_uses") or 1)))
    token = secrets.token_urlsafe(24)
    exp = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    iid = db.create_invite(_cur_ws(), role, _invite_hash(token),
                           (_current_user() or {}).get("id"), exp, uses)
    _audit("invite.create", f"邀请链接 #{iid}({role}, {days} 天, {uses} 次)")
    return jsonify({"id": iid, "url": f"{request.host_url.rstrip('/')}/?invite={token}",
                    "role": role, "expires_at": exp, "max_uses": uses}), 201


@app.delete("/api/v4/invites/<int:invite_id>")
def v4_invite_revoke(invite_id):
    if not db.revoke_invite(_cur_ws(), invite_id):
        return jsonify({"error": "邀请不存在"}), 404
    _audit("invite.revoke", f"撤销邀请 #{invite_id}")
    return jsonify({"ok": True})


def _invite_or_error(token: str):
    inv = db.get_invite_by_hash(_invite_hash(str(token or "")))
    if inv is None or inv["revoked"] or inv["uses"] >= inv["max_uses"]:
        return None, "邀请链接无效或已被使用"
    if inv["expires_at"] <= datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"):
        return None, "邀请链接已过期"
    return inv, ""


@app.get("/api/auth/invite/<token>")
def auth_invite_info(token):
    inv, err = _invite_or_error(token)
    if inv is None:
        return jsonify({"error": err}), 404
    return jsonify({"workspace": inv["workspace_name"], "role": inv["role"],
                    "logged_in": _current_user() is not None})


@app.post("/api/auth/invite/<token>")
def auth_invite_accept(token):
    """接受邀请: 已登录直接加入; 未登录则注册新账号并加入(用户名/密码同初始化规则)。"""
    inv, err = _invite_or_error(token)
    if inv is None:
        return jsonify({"error": err}), 404
    user = _current_user()
    if user is None:
        blocked = _quota_block(inv["workspace_id"], "member")
        if blocked:
            return blocked
        data = request.get_json(silent=True) or {}
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        if not _USERNAME_RE.match(username):
            return jsonify({"error": "用户名需 2~32 位字母/数字/._-"}), 400
        err = _password_problem(password, username)
        if err:
            return jsonify({"error": err}), 400
        if db.get_user_by_username(username):
            return jsonify({"error": "用户名已存在; 已有账号请先登录再打开邀请链接"}), 409
        if not db.use_invite(inv["id"]):
            return jsonify({"error": "邀请链接已被使用"}), 409
        uid = db.create_user(username, security.hash_password(password), "user",
                             str(data.get("display_name") or "").strip()[:60])
        user = db.get_user(uid)
        _login_session(user)
        db.touch_login(uid, _client_ip())
    elif db.get_membership(inv["workspace_id"], user["id"]):
        session["ws"] = inv["workspace_id"]
        return jsonify({"ok": True, "already": True})
    elif _quota_block(inv["workspace_id"], "member"):
        return _quota_block(inv["workspace_id"], "member")
    elif not db.use_invite(inv["id"]):
        return jsonify({"error": "邀请链接已被使用"}), 409
    db.add_member(inv["workspace_id"], user["id"], inv["role"], inv["created_by"])
    session["ws"] = inv["workspace_id"]
    g.pop("ws_ctx", None)
    _audit("member.join", f"{user['username']} 通过邀请加入({inv['role']})",
           username=user["username"], workspace_id=inv["workspace_id"])
    return jsonify({"ok": True, "user": _public_user(user)})


def _month_start() -> str:
    now = datetime.utcnow()
    return now.strftime("%Y-%m-01 00:00:00")


def _plan_of(wid: int) -> tuple:
    plan = (db.get_workspace(wid) or {}).get("plan") or "self"
    return plan, (config.PLANS.get(plan) or config.PLANS["self"])


def _usage_payload(wid: int) -> dict:
    plan, lim = _plan_of(wid)
    q = db.queue_counts(wid)
    _, members = db.workspace_members(wid, limit=1)
    mine = db.workspace_task_ids(wid)
    deploys = sum(1 for c in (db.list_docker() or [])
                  if c.get("task_id") in mine and c.get("status") == "running")
    return {"plan": plan, "plan_label": lim.get("label"), "limits": lim,
            "clones_month": db.usage_since(wid, "clone", _month_start()),
            "queued": q["pending"] + q["running"], "running": q["running"],
            "members": members, "deploys": deploys}


def _quota_block(wid: int, kind: str, adding: int = 1):
    """超额返回 (json, 429), 否则 None。"""
    u = _usage_payload(wid)
    lim = u["limits"]
    if kind == "clone" and lim.get("clones_month") is not None:
        used = u["clones_month"] + u["queued"]
        if used + adding > lim["clones_month"]:
            return jsonify({"error": f"本月克隆额度不足: 已用 {used} / {lim['clones_month']} 次"
                                     f"(含排队中), 这次需要 {adding} 次。请升级套餐或下月再试",
                            "quota": "clones_month"}), 429
    if kind == "member" and lim.get("members") is not None and u["members"] + adding > lim["members"]:
        return jsonify({"error": f"成员数已达套餐上限 {lim['members']} 人", "quota": "members"}), 429
    if kind == "deploy" and lim.get("deploys") is not None and u["deploys"] + adding > lim["deploys"]:
        return jsonify({"error": f"同时运行的部署已达套餐上限 {lim['deploys']} 个",
                        "quota": "deploys"}), 429
    return None


@app.get("/api/v4/usage")
def v4_usage():
    return jsonify(_usage_payload(_cur_ws()))


@app.get("/api/v4/admin/workspaces")
def v4_admin_workspaces():
    """平台管理员: 全部工作区与套餐、本月用量(不含各区业务数据)。"""
    with db._lock:
        rows = [dict(r) for r in db._conn.execute(
            "SELECT id, name, plan, created_at FROM workspaces ORDER BY id").fetchall()]
    for r in rows:
        r["members"] = db.workspace_members(r["id"], limit=1)[1]
        r["clones_month"] = db.usage_since(r["id"], "clone", _month_start())
    return jsonify({"workspaces": rows, "plans": config.PLANS})


@app.patch("/api/v4/admin/workspaces/<int:wid>")
def v4_admin_workspace_plan(wid):
    plan = (request.get_json(silent=True) or {}).get("plan")
    if plan not in config.PLANS or db.get_workspace(wid) is None:
        return jsonify({"error": "工作区或套餐不存在"}), 404
    db.update_workspace(wid, plan=plan)
    _audit("workspace.plan", f"工作区 #{wid} 套餐改为 {plan}", workspace_id=wid)
    return jsonify({"workspace": db.get_workspace(wid)})


@app.post("/api/tasks/<int:task_id>/share")
def task_share(task_id):
    """生成带签名、会过期的预览分享链接(无需登录即可查看该克隆站)。"""
    _task_or_404(task_id)
    hours = (request.get_json(silent=True) or {}).get("hours") or 72
    token = _share_token(task_id, int(hours))
    _audit("task.share", f"任务 #{task_id} 生成 {hours} 小时预览分享链接")
    base = _preview_base() or request.host_url.rstrip('/')
    return jsonify({"url": f"{base}/preview/{task_id}/?share={token}"})


# ---------------------------------------------------------------------------
# 审计日志查询(admin)
# ---------------------------------------------------------------------------
@app.get("/api/v4/audit")
def v4_audit():
    """本区审计日志: 多条件筛选 + 分页。参数见 core.iam.parse_filters, 另有 page / size。
    兼容旧参数 limit(只取第一页)。"""
    wid = _cur_ws()
    known = db.audit_actions(wid)
    filters = iam.parse_filters(request.args, known)
    size = max(10, min(200, request.args.get("size", request.args.get("limit", 50, type=int), type=int)))
    page = max(1, request.args.get("page", 1, type=int))
    rows, total = db.query_audit(wid, limit=size, offset=(page - 1) * size, **filters)
    return jsonify({"entries": [iam.decorate(r) for r in rows],
                    "total": total, "page": page, "size": size,
                    "pages": max(1, -(-total // size)),
                    "actions": known,
                    "catalog": iam.catalog(known),
                    "usernames": db.audit_usernames(wid),
                    "retention_days": config.AUDIT_RETENTION_DAYS})


@app.get("/api/v4/audit/stats")
def v4_audit_stats():
    return jsonify(iam.stats(db, _cur_ws(), request.args.get("days", 7, type=int)))


@app.get("/api/v4/audit/<int:entry_id>")
def v4_audit_entry(entry_id):
    """单条记录详情 + 同一成员前后的 5 条操作(还原上下文)。"""
    wid = _cur_ws()
    e = db.get_audit(wid, entry_id)
    if e is None:
        return jsonify({"error": "记录不存在"}), 404
    around = db.user_audit(e["username"], workspace_id=wid, limit=200)
    idx = next((i for i, x in enumerate(around) if x["id"] == entry_id), 0)
    ctx = around[max(0, idx - 5): idx + 6]
    return jsonify({"entry": iam.decorate(e), "context": [iam.decorate(x) for x in ctx]})


@app.get("/api/v4/audit/export")
def v4_audit_export_ws():
    """按当前筛选导出本区审计 CSV(最多 20000 条), 导出本身也记一条审计。"""
    wid = _cur_ws()
    filters = iam.parse_filters(request.args, db.audit_actions(wid))
    rows, total = db.query_audit(wid, limit=5000, **filters)
    while len(rows) < min(total, 20000):
        more, _ = db.query_audit(wid, limit=5000, offset=len(rows), **filters)
        if not more:
            break
        rows += more
    _audit("audit.export", f"导出审计日志 {len(rows)} 条(共命中 {total} 条)")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return Response(iam.audit_csv(rows), content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="audit_{stamp}.csv"'})


# ---------------------------------------------------------------------------
# 系统设置中心(admin): AI 供应商(Fernet 加密入库) + 引擎阈值
# ---------------------------------------------------------------------------
def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••••••"
    return f"{value[:4]}••••••••{value[-4:]}"


def _effective_api_key() -> str:
    saved = security.decrypt_value(db.get_setting("ai_api_key_enc", ""))
    return saved or config.AI_API_KEY


def _apply_saved_settings():
    """设置中心 > 环境变量/内置默认; 进程内即时生效(引擎读 config 属性)。"""
    pairs = {
        "AI_API_BASE_URL": db.get_setting("ai_base_url", ""),
        "AI_API_KEY": _effective_api_key(),
        "AI_MODEL": db.get_setting("ai_model", ""),
        "AI_REPAIR_TRIGGER_SCORE": db.get_setting("ai_trigger_score", ""),
        "AI_REPAIR_TARGET_SCORE": db.get_setting("ai_target_score", ""),
        "AI_REPAIR_MAX_ROUNDS": db.get_setting("ai_max_rounds", ""),
    }
    for attr, value in pairs.items():
        if value in ("", None):
            continue
        if attr in ("AI_REPAIR_TRIGGER_SCORE", "AI_REPAIR_TARGET_SCORE"):
            with suppress(TypeError, ValueError):
                setattr(config, attr, float(value))
        elif attr == "AI_REPAIR_MAX_ROUNDS":
            with suppress(TypeError, ValueError):
                setattr(config, attr, max(0, min(5, int(value))))
        else:
            setattr(config, attr, str(value))


@app.get("/api/v4/admin/settings")
def v4_settings_get():
    return jsonify({
        "ai": {
            "base_url": config.AI_API_BASE_URL,
            "model": config.AI_MODEL,
            "key_configured": bool(_effective_api_key()),
            "key_masked": _mask_secret(_effective_api_key()),
            "trigger_score": config.AI_REPAIR_TRIGGER_SCORE,
            "target_score": config.AI_REPAIR_TARGET_SCORE,
            "max_rounds": config.AI_REPAIR_MAX_ROUNDS,
        },
    })


@app.put("/api/v4/admin/settings")
def v4_settings_put():
    data = request.get_json(silent=True) or {}
    ai = data.get("ai") if isinstance(data.get("ai"), dict) else {}
    changed = []
    if "base_url" in ai:
        base = str(ai.get("base_url") or "").strip().rstrip("/")
        if base and not base.startswith(("http://", "https://")):
            return jsonify({"error": "Base URL 必须以 http(s):// 开头"}), 400
        db.set_setting("ai_base_url", base)
        changed.append("base_url")
    if "model" in ai:
        db.set_setting("ai_model", str(ai.get("model") or "").strip()[:200])
        changed.append("model")
    if ai.get("api_key"):
        db.set_setting("ai_api_key_enc", security.encrypt_value(str(ai["api_key"]).strip()))
        changed.append("api_key")
    elif ai.get("api_key") == "":
        db.set_setting("ai_api_key_enc", "")
        changed.append("api_key(清空)")
    for key, attr in (("trigger_score", "ai_trigger_score"),
                      ("target_score", "ai_target_score"),
                      ("max_rounds", "ai_max_rounds")):
        if key in ai:
            db.set_setting(attr, str(ai.get(key)))
            changed.append(key)
    _apply_saved_settings()
    _audit("settings.update", f"系统设置变更: {', '.join(changed) or '无变更'}")
    return jsonify({"ok": True, "changed": changed})


@app.post("/api/v4/admin/settings/test_ai")
def v4_settings_test_ai():
    """连通性探测: GET {base}/models(Bearer); 部分网关仅支持 chat 探测则回退。"""
    data = request.get_json(silent=True) or {}
    base = (str(data.get("base_url") or "").strip().rstrip("/")
            or config.AI_API_BASE_URL)
    key = str(data.get("api_key") or "").strip() or _effective_api_key()
    if not base.startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "Base URL 非法"}), 400
    if not key:
        return jsonify({"ok": False, "error": "未配置 API Key"}), 400
    try:
        resp = upstream_http.get(f"{base}/models",
                                 headers={"Authorization": f"Bearer {key}"},
                                 timeout=8)
        if resp.status_code in (401, 403):
            return jsonify({"ok": False,
                            "error": f"鉴权失败(HTTP {resp.status_code}), 请检查 Key"}), 200
        models = []
        with suppress(ValueError, TypeError):
            payload = resp.json()
            models = [m.get("id") for m in payload.get("data", [])[:20]
                      if isinstance(m, dict) and m.get("id")]
        if resp.status_code >= 500 and not models:
            return jsonify({"ok": False,
                            "error": f"上游返回 HTTP {resp.status_code}"}), 200
        return jsonify({"ok": True, "status": resp.status_code, "models": models})
    except upstream_http.RequestException as exc:
        return jsonify({"ok": False, "error": f"连接失败: {type(exc).__name__}"}), 200


# ---------------------------------------------------------------------------
# 模型配置中心: Provider 预设 / 实时模型列表 / 三段式能力测试
# 登录即可读(GET), 测试(POST)需 operator+; 写入仍走 /api/v4/admin/settings。
# ---------------------------------------------------------------------------
AI_PROVIDER_PRESETS = [
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
     "models": ["deepseek-v4.1", "deepseek-chat", "deepseek-reasoner"],
     "note": "视觉闭环需选多模态模型"},
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1",
     "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1"], "note": "gpt-4o 系列自带多模态"},
    {"id": "zhipu", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "models": ["glm-4.5v", "glm-4v-flash", "glm-4-air"], "note": "glm-4.xv 系列为多模态"},
    {"id": "moonshot", "name": "月之暗面 Kimi", "base_url": "https://api.moonshot.cn/v1",
     "models": ["moonshot-v1-8k-vision-preview", "moonshot-v1-32k"], "note": "vision 版本支持图像"},
    {"id": "custom", "name": "自定义(OpenAI 兼容)", "base_url": "",
     "models": [], "note": "任意兼容 /chat/completions 的网关"},
]

# 1x1 透明 PNG, 用于多模态能力探测(最小合法图像输入)
_VISION_PROBE_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                     "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@app.get("/api/v4/ai/providers")
def v4_ai_providers():
    return jsonify({
        "providers": AI_PROVIDER_PRESETS,
        "current": {
            "base_url": config.AI_API_BASE_URL,
            "model": config.AI_MODEL,
            "key_configured": bool(_effective_api_key()),
            "key_masked": _mask_secret(_effective_api_key()),
        },
    })


@app.get("/api/v4/ai/models")
def v4_ai_models():
    """实时拉取上游模型列表(缺省用已保存配置; 供下拉选择)。"""
    base = (request.args.get("base_url") or "").strip().rstrip("/") or config.AI_API_BASE_URL
    key = (request.args.get("api_key") or "").strip() or _effective_api_key()
    if not base.startswith(("http://", "https://")):
        return jsonify({"error": "Base URL 非法"}), 400
    if not key:
        return jsonify({"error": "未配置 API Key"}), 400
    try:
        resp = upstream_http.get(f"{base}/models",
                                 headers={"Authorization": f"Bearer {key}"},
                                 timeout=10)
        if resp.status_code in (401, 403):
            return jsonify({"error": f"鉴权失败(HTTP {resp.status_code})"}), 200
        models = []
        with suppress(ValueError, TypeError):
            for item in resp.json().get("data", []):
                if isinstance(item, dict) and item.get("id"):
                    models.append(item["id"])
        return jsonify({"ok": resp.status_code < 400 or bool(models),
                        "status": resp.status_code,
                        "models": sorted(models)[:80]})
    except upstream_http.RequestException as exc:
        return jsonify({"error": f"连接失败: {type(exc).__name__}"}), 200


def _record_test_call(model: str, resp, step: str):
    """三段式测试的两次对话请求也按 token 计费, 记在发起测试的成员名下。"""
    usage = None
    with suppress(ValueError, TypeError, AttributeError):
        usage = resp.json().get("usage")
    llm_usage.record({"model": model}, usage, step_name=step, kind="ai_test",
                     username=_me_name(), workspace_id=_ws_ctx()[0] or 1)


@app.post("/api/v4/ai/test")
def v4_ai_test():
    """三段式能力测试: ① 连通(/models) ② 鉴权(1-token chat) ③ 多模态(图像输入)。

    三段独立判定并分别回报 —— 视觉闭环依赖 ③, 纯文本模型可正常用 Mock 润色。
    """
    data = request.get_json(silent=True) or {}
    base = (str(data.get("base_url") or "").strip().rstrip("/")
            or config.AI_API_BASE_URL)
    key = str(data.get("api_key") or "").strip() or _effective_api_key()
    model = (str(data.get("model") or "").strip() or config.AI_MODEL)
    if not base.startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "Base URL 非法"}), 400
    if not key:
        return jsonify({"ok": False, "error": "未配置 API Key"}), 400
    if not model:
        return jsonify({"ok": False, "error": "未填写模型名"}), 400

    result = {"ok": False, "latency_ms": None, "models_count": 0,
              "chat_ok": False, "vision_ok": False,
              "message": "", "stage": "conn"}

    def _err(msg):
        result["message"] = msg
        return jsonify(result), 200

    headers = {"Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}
    try:
        started = time.time()
        resp = upstream_http.get(f"{base}/models", headers=headers, timeout=10)
        result["latency_ms"] = int((time.time() - started) * 1000)
        if resp.status_code in (401, 403):
            return _err(f"鉴权失败(HTTP {resp.status_code}): API Key 无效或无权限")
        with suppress(ValueError, TypeError):
            result["models_count"] = len([m for m in resp.json().get("data", [])
                                          if isinstance(m, dict) and m.get("id")])
    except upstream_http.RequestException as exc:
        return _err(f"无法连接 {base}: {type(exc).__name__}(检查网络/URL)")

    result["stage"] = "chat"
    try:
        chat = upstream_http.post(
            f"{base}/chat/completions", headers=headers, timeout=30,
            json={"model": model, "max_tokens": 4, "temperature": 0,
                  "messages": [{"role": "user", "content": "ping"}]})
        if chat.status_code in (401, 403):
            return _err(f"鉴权失败(HTTP {chat.status_code}): 请检查 API Key")
        if chat.status_code == 404:
            return _err(f"模型或接口不存在(HTTP 404): 检查 Base URL 与模型名 {model}")
        if chat.status_code >= 400:
            detail = ""
            with suppress(ValueError, TypeError):
                detail = str(chat.json().get("error", {}).get("message", ""))[:120]
            return _err(f"对话接口异常(HTTP {chat.status_code}){(': ' + detail) if detail else ''}")
        result["chat_ok"] = True
        _record_test_call(model, chat, "连通测试")
    except upstream_http.RequestException as exc:
        return _err(f"对话接口请求失败: {type(exc).__name__}")

    result["stage"] = "vision"
    try:
        vision = upstream_http.post(
            f"{base}/chat/completions", headers=headers, timeout=45,
            json={"model": model, "max_tokens": 8, "temperature": 0,
                  "messages": [{"role": "user", "content": [
                      {"type": "text", "text": "这张图是什么颜色? 一个词回答。"},
                      {"type": "image_url", "image_url": {
                          "url": "data:image/png;base64," + _VISION_PROBE_PNG}},
                  ]}]})
        if vision.status_code < 400:
            _record_test_call(model, vision, "看图测试")
            result["vision_ok"] = True
            result["ok"] = True
            result["message"] = (f"连通 {result['latency_ms']}ms · 模型 {model} · "
                                 f"多模态可用(视觉闭环就绪)")
        else:
            # 纯文本模型: chat 可用即整体可用, 视觉闭环会自动跳过
            result["ok"] = True
            result["message"] = (f"连通 {result['latency_ms']}ms · 模型 {model} · "
                                 "仅支持文本(视觉闭环将不可用, Mock 润色正常)")
    except upstream_http.RequestException:
        result["ok"] = True
        result["message"] = (f"连通 {result['latency_ms']}ms · 模型 {model} · "
                             "多模态探测超时(不影响文本能力)")
    _audit("ai.test", f"模型连通测试: {model}@{base} -> "
                      f"chat={result['chat_ok']} vision={result['vision_ok']}")
    return jsonify(result), 200


# ---------------------------------------------------------------------------
@app.post("/api/v4/admin/audit/purge")
def v4_audit_purge():
    # 手动清理 N 天前审计日志(默认按配置)
    data = request.get_json(silent=True) or {}
    days = int(data.get("days") or config.AUDIT_RETENTION_DAYS)
    if days <= 0:
        return jsonify({"error": "days must be positive"}), 400
    n = db.purge_audit_before(days)
    _audit("audit.purge", f"手动清理 {n} 条超 {days} 天的审计日志")
    return jsonify({"ok": True, "purged": n, "days": days})


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            with suppress(OSError):
                total += os.lstat(os.path.join(root, f)).st_size
    return total


@app.get("/api/v4/admin/storage")
def v4_storage():
    """存储占用: 工作空间(克隆本体, 不自动删) / 导出包(可再生) / 资源缓存, 与所在卷剩余空间。"""
    out = {}
    for key, d in (("workspaces", config.WORKSPACE_ROOT), ("exports", config.EXPORTS_DIR),
                   ("cache", config.CACHE_ROOT)):
        out[key] = {"path": str(d), "bytes": _dir_size(d) if d.is_dir() else 0}
    try:
        du = shutil.disk_usage(str(config.WORKSPACE_ROOT))
        out["disk"] = {"free": du.free, "total": du.total, "min_free_mb": config.MIN_FREE_DISK_MB}
    except OSError:
        out["disk"] = None
    return jsonify(out)


@app.post("/api/v4/admin/storage/prune_exports")
def v4_prune_exports():
    """删除可再生的导出包(已完成且工作空间仍在的任务, 下载时按需重建)与汇总包; 不动工作空间。"""
    keep = set()
    freed = n = 0
    for t in db.list_tasks(limit=100000, workspace_id=ALL_WORKSPACES):
        zp = Path(t["zip_path"]) if t.get("zip_path") else None
        if not zp or not zp.is_file():
            continue
        if not (t.get("status") == "completed" and (_workspace_of(t) / "site.json").is_file()):
            keep.add(zp.resolve())   # 工作空间没了: 导出包是唯一副本, 不删
            continue
        with suppress(OSError):
            freed += zp.stat().st_size
            zp.unlink()
            n += 1
    for zp in config.EXPORTS_DIR.glob("*.zip"):
        if zp.resolve() in keep or not zp.name.startswith("all_clones_"):
            continue
        with suppress(OSError):
            freed += zp.stat().st_size
            zp.unlink()
            n += 1
    _audit("storage.prune_exports", f"清理可再生导出包 {n} 个, 释放 {freed // (1024 * 1024)}MB")
    return jsonify({"ok": True, "deleted": n, "freed_bytes": freed})


@app.get("/api/v4/admin/audit/export")
def v4_audit_export():
    """审计日志导出 CSV(BOM 防 Excel 乱码)。"""
    entries, _ = db.query_audit(None, limit=5000)
    _audit("audit.export", f"平台管理员导出全平台审计日志 {len(entries)} 条")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return Response(iam.audit_csv(entries),
                    content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="audit_all_{stamp}.csv"'})


# ---------------------------------------------------------------------------
_tracker_push_ts = {}  # task_id -> 最后推送时间戳(任务级 5s 去重)
def _tracker_config_for_page(url: str, referrer: str = "") -> dict:
    # 按上报来源匹配任务推送配置: 先按 Referer 的 /preview/<id>/ 或端口, 再回退第一个启用者
    candidates = []
    import re as _re
    m = _re.search(r"/preview/(\d+)", referrer or "")
    if m:
        candidates.append(int(m.group(1)))
    m2 = _re.search(r":(\d+)", url or "")
    port = m2.group(1) if m2 else None
    for task in db.list_tasks(status=None, workspace_id=ALL_WORKSPACES):
        cfg = db.get_tracker_config(task["id"])
        if cfg and cfg.get("enabled"):
            candidates.append(task["id"])
    for idx, tid in enumerate(candidates):
        cfg = db.get_tracker_config(tid)
        if cfg and cfg.get("enabled"):
            task = db.get_task(tid)
            if task is None:
                continue
            if port and str(task.get("target_url", "")).find(":" + port) >= 0:
                return cfg
            if idx == len(candidates) - 1:
                return cfg
    for tid in candidates:
        cfg = db.get_tracker_config(tid)
        if cfg and cfg.get("enabled"):
            return cfg
    return None


# ---------------------------------------------------------------------------
# 遥测 / SSE / 导出
# ---------------------------------------------------------------------------
@app.get("/api/ui-version")
def ui_version():
    """前端版本哨兵: 模板文件 mtime, 供长开窗口检测平台更新并提示刷新。"""
    try:
        mtime = int(Path(__file__).parent.joinpath(
            "templates", "index.html").stat().st_mtime)
    except OSError:
        mtime = 0
    return jsonify({"version": mtime})


@app.post("/api/scheduler/pause")
def scheduler_pause():
    scheduler.pause()
    _audit("scheduler.pause", "调度器已暂停")
    return jsonify({"ok": True, "paused": True})


@app.post("/api/scheduler/resume")
def scheduler_resume():
    scheduler.resume()
    _audit("scheduler.resume", "调度器已恢复")
    return jsonify({"ok": True, "paused": False})


@app.get("/api/v4/admin/settings/reveal-key")
def v4_settings_reveal_key():
    """管理员查看当前生效 API Key 明文(默认界面仅掩码显示)。"""
    return jsonify({"key": _effective_api_key()})


@app.post("/api/v4/admin/settings/reset")
def v4_settings_reset():
    """重置默认: 清除全部 AI 相关 KV 覆盖, 回落到 config 内置默认。"""
    for k in ("ai_base_url", "ai_model", "ai_api_key_enc",
              "ai_trigger_score", "ai_target_score", "ai_max_rounds"):
        db.set_setting(k, "")
    _apply_saved_settings()
    _audit("settings.reset", "系统设置已重置为内置默认")
    return jsonify({"ok": True, "ai": {
        "base_url": config.AI_API_BASE_URL,
        "model": config.AI_MODEL,
        "key_configured": bool(_effective_api_key()),
        "trigger_score": config.AI_REPAIR_TRIGGER_SCORE,
        "target_score": config.AI_REPAIR_TARGET_SCORE,
        "max_rounds": config.AI_REPAIR_MAX_ROUNDS,
    }})


@app.get("/api/v4/admin/config-limits")
def v4_config_limits():
    """并发与限制只读快照(修改需编辑 config.py 并重启)。"""
    return jsonify({"limits": {
        "max_concurrent_contexts": config.MAX_CONCURRENT_CONTEXTS,
        "max_pages_limit": config.MAX_PAGES_LIMIT,
        "render_repair_rounds": config.RENDER_REPAIR_ROUNDS,
        "download_workers": config.DOWNLOAD_WORKERS,
        "ai_repair_max_rounds": config.AI_REPAIR_MAX_ROUNDS,
        "audit_retention_days": config.AUDIT_RETENTION_DAYS,
        "pageload_timeout_ms": config.PAGELOAD_TIMEOUT,
        "network_idle_timeout_ms": config.NETWORK_IDLE_TIMEOUT,
        "session_hours": 12,
    }})


@app.get("/api/telemetry")
def telemetry():
    return jsonify({
        "browser": pool.stats(),
        "cas": db.cas_stats(),
        "queue": db.queue_counts(_cur_ws()),
        "queue_all": db.queue_counts(),
        "batches": db.list_batches(12, workspace_id=_cur_ws()),
        "scheduler": {"paused": scheduler.paused()},
    })


def _live_task(t: dict, run: dict, stages: dict, total: float) -> dict:
    trail = live.stage_trail(run) if run else []
    elapsed = round(time.time() - run["started"], 1) if run else None
    return {
        "id": t["id"], "domain": t.get("domain") or "", "target_url": t.get("target_url") or "",
        "batch_name": t.get("batch_name") or "", "creator": t.get("creator_name") or "",
        "status": t.get("status"), "stage": t.get("stage") or "", "progress": t.get("progress") or 0,
        "worker": run.get("worker") if run else None, "elapsed_s": elapsed,
        "eta_s": live.eta_seconds(trail, elapsed or 0.0, stages, total) if run else None,
        "trail": [{"stage": s, "secs": d, "current": c} for s, d, c in trail],
    }


@app.get("/api/v4/live")
def v4_live():
    """运行态快照(工作台/浏览器池/定时任务页 2s 轮询): 工位、运行中任务的阶段与预计剩余、队列、本机资源。"""
    wid = _cur_ws()
    runs = live.runs()
    stages, total = live.eta_model(db)
    running = db.list_tasks(status="running", workspace_id=wid, limit=50)
    mine = {t["id"]: t for t in running}
    tasks = [_live_task(t, runs.get(t["id"]) or {}, stages, total) for t in running]
    workers = []
    by_worker = {r["worker"]: tid for tid, r in runs.items()}
    for i in range(pool.max):
        tid = by_worker.get(i)
        if tid is None:
            workers.append({"index": i, "busy": False})
        elif tid in mine:
            workers.append({"index": i, "busy": True, "task_id": tid})
        else:   # 别的工作区的任务: 只说忙, 不露内容
            workers.append({"index": i, "busy": True, "task_id": None,
                            "elapsed_s": round(time.time() - runs[tid]["started"], 1)})
    pending = db.list_tasks(status="pending", workspace_id=wid, limit=200)
    pending.sort(key=lambda t: t["id"])
    queue = db.queue_counts(wid)
    return jsonify({
        "now": time.time(),
        "workers": workers,
        "tasks": tasks,
        "pending": [{"id": t["id"], "domain": t.get("domain") or "", "batch_name": t.get("batch_name") or ""}
                    for t in pending[:12]],
        "pending_total": max(len(pending), int(queue.get("pending") or 0)),
        "queue": queue,
        "throughput": db.throughput(wid, 60),
        "median_s": total,
        "browser": pool.stats(),
        "procs": live.browser_processes(),
        "host": live.host(config.WORKSPACE_ROOT),
        "scheduler": {"paused": scheduler.paused()},
    })


# ---------------------------------------------------------------------------
# 定时克隆: 按时间把一组网址自动重新克隆(批次入队), 用来盯竞品/客户站点的变化
_SECRET_CONFIG_KEYS = ("cookie", "bearer", "ai_api_key")


def _redact_config(cfg: dict) -> dict:
    """接口回显用: 凭据类字段只说明「已填写」, 不回明文。任务执行读的是库里的原值, 不受影响。"""
    out = dict(cfg or {})
    for k in _SECRET_CONFIG_KEYS:
        if out.get(k):
            out[k] = ""
            out[k + "_set"] = True
    return out


# ---------------------------------------------------------------------------
from core import schedules as sched_rules  # noqa: E402
from core import schedule_notify  # noqa: E402


def _mask_webhook(url: str) -> str:
    """通知地址常带 access_token 之类的密钥: 接口只回主机名 + 末 4 位。"""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "已设置"
    return f"{parts.scheme}://{parts.hostname or ''}/…{url[-4:]}"


def _schedule_view(sc: dict, briefs: dict) -> dict:
    hist = json.loads(sc.get("history_json") or "[]")
    urls = json.loads(sc.get("urls_json") or "[]")
    runs = [{"batch_id": h["batch_id"], "at": h["at"], **(briefs.get(h["batch_id"]) or {}),
             **{k: h[k] for k in ("changed", "failed_sites", "notified", "notify_msg") if k in h}}
            for h in hist[-8:]][::-1]
    return {
        "id": sc["id"], "name": sc["name"], "urls": urls, "project_id": sc.get("project_id"),
        "config": _redact_config(json.loads(sc.get("config_json") or "{}")),
        "freq": sc["freq"], "hour": sc["hour"], "minute": sc["minute"], "weekday": sc["weekday"],
        "rule": sched_rules.describe(sc["freq"], sc["hour"], sc["minute"], sc["weekday"]),
        "enabled": bool(sc["enabled"]), "next_run_at": sc["next_run_at"] if sc["enabled"] else "",
        "last_run_at": sc["last_run_at"], "runs_total": sc["runs"], "runs": runs,
        "created_by": sc.get("created_by") or "", "created_at": sc["created_at"],
        "notify": sc.get("notify") or "change", "webhook_set": bool(sc.get("webhook_url")),
        "webhook_masked": _mask_webhook(sc.get("webhook_url") or ""),
    }


def _fire_schedule(sc: dict, actor: str) -> dict:
    """把定时任务的网址作为一个新批次入队; 额度不足时跳过本次并记日志。"""
    wid = sc["workspace_id"]
    accepted, _rej = _normalize_urls(json.loads(sc.get("urls_json") or "[]"))
    nxt = sched_rules.next_run(sc["freq"], sc["hour"], sc["minute"], sc["weekday"])
    if not accepted:
        db.update_schedule(sc["id"], next_run_at=nxt)
        return {"error": "没有可克隆的网址"}
    with app.app_context():
        blocked = _quota_block(wid, "clone", len(accepted))
    if blocked:
        db.update_schedule(sc["id"], next_run_at=nxt)
        msg = blocked[0].get_json().get("error", "额度不足")
        bus.publish(None, "SCHED", f"定时任务「{sc['name']}」本次跳过: {msg}", "WARN", workspace_id=wid)
        return {"error": msg}
    name = f"定时 · {sc['name']} · {time.strftime('%m-%d %H:%M')}"
    cfg = json.loads(sc.get("config_json") or "{}")
    batch_id = db.create_batch(name, accepted, cfg, sc.get("project_id"), created_by=actor,
                               workspace_id=wid)
    hist = json.loads(sc.get("history_json") or "[]")
    hist.append({"batch_id": batch_id, "at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())})
    db.update_schedule(sc["id"], last_run_at=hist[-1]["at"], history_json=json.dumps(hist[-30:]),
                       runs=int(sc.get("runs") or 0) + 1, next_run_at=nxt)
    scheduler.start()
    bus.publish(None, "SCHED", f"定时任务「{sc['name']}」触发: 批次 #{batch_id} 入队 {len(accepted)} 个站点",
                workspace_id=wid)
    return {"batch_id": batch_id, "accepted": len(accepted)}


def _batch_summary(batch_id: int, wid: int):
    tasks = []
    for t in db.list_tasks(batch_id=batch_id, workspace_id=wid, limit=200):
        try:
            stats = json.loads(t.get("stats_json") or "{}")
        except (TypeError, ValueError):
            stats = {}
        tasks.append({**t, "stats": stats})
    return schedule_notify.summarize(tasks)


def _check_schedule_notifications(now: Optional[float] = None) -> int:
    """定时批次跑完后: 记下变化/失败数, 发一条事件, 配了通知地址的按规则推 webhook。返回本轮处理的批次数。
    每个批次只处理一次(history 里标 notified); 只看最近 8 次运行。"""
    handled = 0
    now = now if now is not None else time.time()
    for sc in db.list_schedules():
        hist = json.loads(sc.get("history_json") or "[]")
        dirty = False
        for h in hist[-8:]:
            if "notified" in h:
                continue
            summary = _batch_summary(h["batch_id"], sc["workspace_id"])
            if summary is None:
                try:   # 一直跑不完(如服务重启丢了队列)的批次 6 小时后不再等
                    started: float = calendar.timegm(time.strptime(h["at"], "%Y-%m-%d %H:%M:%S"))
                except (TypeError, ValueError):
                    started = now
                if now - started > 6 * 3600:
                    h.update(notified=False, notify_msg="批次长时间未完成, 未通知")
                    dirty = True
                continue
            h.update(changed=summary["changed"], failed_sites=summary["failed"])
            mode = sc.get("notify") or "change"
            url = sc.get("webhook_url") or ""
            verdict = schedule_notify.summary_text(sc["name"], summary).splitlines()[0]
            bus.publish(None, "SCHED", verdict, "WARN" if summary["failed"] else "INFO",
                        workspace_id=sc["workspace_id"])
            if url and schedule_notify.should_notify(mode, summary):
                link = f"{config.PUBLIC_URL}/" if config.PUBLIC_URL else ""
                ok, msg = schedule_notify.send(url, schedule_notify.build_payload(url, sc, h["batch_id"], summary, link))
                h.update(notified=ok, notify_msg=msg)
            else:
                h.update(notified=False, notify_msg="" if url else "未配置通知地址")
            dirty = True
            handled += 1
        if dirty:
            db.update_schedule(sc["id"], history_json=json.dumps(hist[-30:]))
    return handled


def _schedule_ticker():
    while True:
        time.sleep(20)
        with suppress(Exception):   # 通知出错不影响触发
            _check_schedule_notifications()
        try:
            due = db.due_schedules(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
        except Exception:  # noqa: BLE001 — 后台线程不得退出
            continue
        for sc in due:
            try:
                _fire_schedule(sc, sc.get("created_by") or "定时任务")
            except Exception as exc:  # noqa: BLE001 — 一个出错不拖累其他, 且推到下一次(不每 20 秒重试)
                with suppress(Exception):
                    db.update_schedule(sc["id"], next_run_at=sched_rules.next_run(
                        sc["freq"], sc["hour"], sc["minute"], sc["weekday"]))
                    bus.publish(None, "SCHED", f"定时任务「{sc['name']}」触发失败: {exc}", "ERROR",
                                workspace_id=sc["workspace_id"])


def _schedule_body(data: dict, partial: bool = False) -> tuple:
    out: dict = {}
    if not partial or "name" in data:
        name = str(data.get("name") or "").strip()[:60]
        if len(name) < 1:
            return None, "请填写名称"
        out["name"] = name
    if not partial or "urls" in data:
        accepted, _rej = _normalize_urls(data.get("urls") or [])
        if not accepted:
            return None, "至少需要 1 个合法网址"
        out["urls_json"] = json.dumps([u for u, _d in accepted], ensure_ascii=False)
    if not partial or "freq" in data:
        freq = data.get("freq", "daily")
        if freq not in sched_rules.FREQS:
            return None, "频率只能是 每小时 / 每天 / 工作日 / 每周"
        out["freq"] = freq
    for k, lo, hi in (("hour", 0, 23), ("minute", 0, 59), ("weekday", 0, 6)):
        if k in data or not partial:
            try:
                v = int(data.get(k, {"hour": 9, "minute": 0, "weekday": 0}[k]))
            except (TypeError, ValueError):
                return None, "时间格式不对"
            if not lo <= v <= hi:
                return None, "时间超出范围"
            out[k] = v
    if "enabled" in data:
        out["enabled"] = 1 if data.get("enabled") else 0
    if "notify" in data:
        if data.get("notify") not in schedule_notify.NOTIFY_MODES:
            return None, "通知方式只能是 有变化时 / 每次 / 不通知"
        out["notify"] = data["notify"]
    if data.get("webhook_clear"):
        out["webhook_url"] = ""
    elif data.get("webhook_url"):   # 留空 = 不改(编辑时接口只回掩码)
        try:
            out["webhook_url"] = schedule_notify.validate_webhook(data.get("webhook_url"))
        except ValueError as exc:
            return None, str(exc)
    if "config" in data or not partial:
        out["config_json"] = json.dumps(_sanitize_config(data.get("config") or {}), ensure_ascii=False)
    if "project_id" in data:
        pid = data.get("project_id")
        pid = int(pid) if str(pid or "").isdigit() else None
        if pid is not None and (db.get_project(pid) or {}).get("workspace_id") != _cur_ws():
            return None, f"项目 #{pid} 不存在"
        out["project_id"] = pid
    return out, ""


def _schedule_or_404(schedule_id: int) -> dict:
    sc = db.get_schedule(schedule_id)
    if not sc or sc["workspace_id"] != _cur_ws():
        abort(404)
    return sc


@app.get("/api/v4/schedules")
def v4_schedules():
    items = db.list_schedules(_cur_ws())
    ids = [h["batch_id"] for sc in items for h in json.loads(sc.get("history_json") or "[]")[-8:]]
    briefs = db.batch_brief(ids)
    return jsonify({"schedules": [_schedule_view(sc, briefs) for sc in items],
                    "tz": time.strftime("%z")})


@app.post("/api/v4/schedules")
def v4_schedule_create():
    body, err = _schedule_body(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    body["next_run_at"] = sched_rules.next_run(body["freq"], body["hour"], body["minute"], body["weekday"])
    sid = db.create_schedule(_cur_ws(), (_current_user() or {}).get("username", ""), **body)
    _audit("schedule.create", f"定时任务 #{sid}「{body['name']}」{sched_rules.describe(body['freq'], body['hour'], body['minute'], body['weekday'])}")
    return jsonify({"schedule": _schedule_view(db.get_schedule(sid) or {}, {})}), 201


@app.patch("/api/v4/schedules/<int:schedule_id>")
def v4_schedule_update(schedule_id):
    sc = _schedule_or_404(schedule_id)
    body, err = _schedule_body(request.get_json(silent=True) or {}, partial=True)
    if err:
        return jsonify({"error": err}), 400
    merged = {**sc, **body}
    if merged["enabled"]:
        body["next_run_at"] = sched_rules.next_run(merged["freq"], merged["hour"], merged["minute"],
                                                   merged["weekday"])
    db.update_schedule(schedule_id, **body)
    _audit("schedule.update", f"定时任务 #{schedule_id} {sorted(body)}")
    new = db.get_schedule(schedule_id) or {}
    hist = [h["batch_id"] for h in json.loads(new.get("history_json") or "[]")[-8:]]
    return jsonify({"schedule": _schedule_view(new, db.batch_brief(hist))})


@app.delete("/api/v4/schedules/<int:schedule_id>")
def v4_schedule_delete(schedule_id):
    sc = _schedule_or_404(schedule_id)
    db.delete_schedule(schedule_id)
    _audit("schedule.delete", f"定时任务 #{schedule_id}「{sc['name']}」")
    return jsonify({"ok": True})


@app.post("/api/v4/schedules/<int:schedule_id>/run")
def v4_schedule_run(schedule_id):
    sc = _schedule_or_404(schedule_id)
    res = _fire_schedule(sc, (_current_user() or {}).get("username", ""))
    if res.get("error"):
        return jsonify(res), 429 if "额度" in res["error"] else 400
    _audit("schedule.run", f"定时任务 #{schedule_id} 手动运行 → 批次 #{res['batch_id']}")
    return jsonify(res), 201


@app.post("/api/v4/schedules/<int:schedule_id>/test-notify")
def v4_schedule_test_notify(schedule_id):
    """往已保存的通知地址发一条示例消息, 用来确认机器人/接收端配置对不对。"""
    sc = _schedule_or_404(schedule_id)
    url = sc.get("webhook_url") or ""
    if not url:
        return jsonify({"error": "还没有填写通知地址"}), 400
    urls = json.loads(sc.get("urls_json") or "[]")[:3] or ["https://example.com/"]
    sample = {"total": len(urls), "changed": 1, "failed": 0,
              "sites": [{"task_id": 0, "url": u, "status": "completed", "score": None, "changed": i == 0,
                         "verdict": "测试消息: 与上次相比有 1 处变化, 约占 3.2% 面积" if i == 0 else "",
                         "area_pct": 3.2 if i == 0 else 0, "first_run": False} for i, u in enumerate(urls)]}
    ok, msg = schedule_notify.send(url, schedule_notify.build_payload(url, sc, 0, sample))
    _audit("schedule.notify_test", f"定时任务 #{schedule_id} 测试通知: {'成功' if ok else '失败'} {msg}")
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 502


@app.get("/api/stream_logs")
def stream_logs():
    task_id = request.args.get("task_id", type=int)
    if task_id is not None and _visible_task(task_id) is None:
        abort(404)
    if _auth_required():
        wid = _cur_ws()
        cache: dict = {}

        def _accept(entry) -> bool:
            if entry.get("workspace_id") is not None:
                return entry["workspace_id"] == wid
            tid = entry.get("task_id")
            if tid is None:
                return False  # 不带归属的系统级消息只对单机模式展示
            if tid not in cache:
                cache[tid] = db.task_workspace(tid)
            return cache[tid] == wid
    else:
        _accept = None
    return Response(
        bus.stream(task_id, accept=_accept),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},   # Connection 是逐跳头, WSGI 应用不能设
    )


@app.get("/api/download/<int:task_id>")
def download_zip(task_id):
    task = _task_or_404(task_id)
    zip_path = Path(task["zip_path"]) if task.get("zip_path") else Path()
    if not zip_path.is_file():
        # 导出包是工作空间的可再生缓存(存储清理会删): 工作空间还在就当场重建
        ws = _workspace_of(task)
        if task.get("status") != "completed" or not (ws / "site.json").is_file():
            return jsonify({"error": "ZIP 尚未生成"}), 404
        try:
            zip_path = Path(_regenerate_project(task, ws))
            db.update_task(task_id, zip_path=str(zip_path))
        except Exception as exc:
            return jsonify({"error": f"ZIP 重建失败: {exc}"}), 500
    return send_file(zip_path, as_attachment=True, download_name=_download_name(task, _workspace_of(task)))


_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _download_name(task: dict, ws: Path) -> str:
    """下载文件名: 页面标题(没有时用 域名+路径) + 任务号, 去掉文件名非法字符, 控制长度。"""
    title = _display_title(task, ws)
    if not title:
        u = urlsplit(task.get("target_url") or "")
        host = u.netloc or task.get("domain") or "site"
        title = host + (u.path.rstrip("/").replace("/", "_") if u.path and u.path != "/" else "")
    name = _UNSAFE_NAME.sub("_", title)
    name = re.sub(r"[\s_]+", " ", name).strip(" ._")[:60] or "site"
    return f"{name} #{task['id']}.zip"


@app.get("/api/export_all")
def export_all():
    completed = db.list_tasks(status="completed", workspace_id=_cur_ws())
    for t in completed:
        # 被存储清理删掉的导出包按工作空间重建(与单任务下载同)
        if not (t.get("zip_path") and Path(t["zip_path"]).is_file()) and (_workspace_of(t) / "site.json").is_file():
            with suppress(Exception):
                t["zip_path"] = str(_regenerate_project(t, _workspace_of(t)))
                db.update_task(t["id"], zip_path=t["zip_path"])
    zips = [t for t in completed
            if t.get("zip_path") and Path(t["zip_path"]).is_file()]
    if not zips:
        return jsonify({"error": "暂无已完成的 ZIP 可导出"}), 404
    # 一次性汇总包: 只留这一份, 以前每点一次留一份全量副本, 从不清理
    for old in config.EXPORTS_DIR.glob("all_clones_*.zip"):
        with suppress(OSError):
            old.unlink()
    out = config.EXPORTS_DIR / f"all_clones_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, task in enumerate(zips):
            zf.write(task["zip_path"],
                     arcname=f"{i + 1:02d}_{task['domain'] or 'site'}_{task['id']}.zip")
    return send_file(out, as_attachment=True)


@app.post("/api/pool/restart")
def pool_restart():
    """浏览器实例池一键清理重启: 各工作线程在下次任务前自行销毁并重建实例(线程安全)。"""
    before = pool.stats()
    pool.request_restart()
    _audit("pool.restart", f"浏览器实例池重启已调度 (原实例 {before.get('instances')} 个)")
    bus.publish(None, "SCHED", "浏览器实例池重启已调度: 各工作线程将在下次任务前重建实例")
    return jsonify({"ok": True, "scheduled": True, "before": before})


@app.post("/api/cache/clear")
def clear_cache():
    shutil.rmtree(config.CACHE_ROOT, ignore_errors=True)
    config.CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    db.clear_cas()
    bus.publish(None, "CAS", "全局内容寻址缓存池已清空")
    _audit("cache.clear", "清空 CAS 全局缓存池")
    return jsonify({"ok": True, "cas": db.cas_stats()})


# ---------------------------------------------------------------------------
# 短链管理: 任务制短链(一组服务各一条)+ 单独短链 + /s/<id> 跳转计数
# 服务层 core/link_shortener.py; 生成走后台线程(与存档打包一致), 前端轮询任务详情。
# ---------------------------------------------------------------------------
from core import link_shortener  # noqa: E402

_link_gen_running: set = set()
_link_gen_lock = threading.Lock()


def _public_base() -> str:
    """平台对外地址: CLONER_PUBLIC_URL 优先, 否则按这次访问的地址。"""
    return config.PUBLIC_URL or request.host_url.rstrip("/")


def _short_link_out(item: dict) -> dict:
    out = dict(item)
    out.pop("legacy_numeric", None)
    out["ok"] = bool(item.get("short_url"))
    if item.get("code"):
        path = link_shortener.local_path(item["code"])
    elif item.get("legacy_numeric"):
        path = f"/s/{item['id']}"
    else:
        path = ""
    out["local_url"] = _public_base() + path if out["ok"] and path else ""
    if out["ok"] and str(item.get("short_url") or "").startswith("/"):  # 本站短码存相对路径, 展示时补全
        out["short_url"] = _public_base() + item["short_url"]
    return out


def _short_task_out(task: dict, with_links: bool = False) -> dict:
    out = dict(task)
    out["running"] = task["id"] in _link_gen_running
    if out["running"]:
        out["status"] = "running"
    out["services"] = [s for s in (task.get("services") or "").split(",") if s]
    if with_links:
        links = [_short_link_out(l) for l in db.list_links_of_short_task(task["id"])]
        out["links"] = links
        out["link_count"] = sum(1 for l in links if l["ok"])
        out["failed_count"] = sum(1 for l in links if not l["ok"])
        out["total_clicks"] = sum(l.get("clicks") or 0 for l in links)
        out["services"] = sorted({l["service"] for l in links if l["ok"]})
        clone = _visible_task(task["clone_task_id"]) if task.get("clone_task_id") else None
        out["clone_domain"] = (clone or {}).get("domain", "")
    return out


def _short_task_or_404(task_row_id: int) -> dict:
    task = db.get_short_task(task_row_id)
    if task is None or int(task.get("workspace_id") or 1) != _cur_ws():
        abort(404)
    return task


def _start_link_generation(task_row_id: int) -> bool:
    """后台生成; 同一任务已在生成中返回 False。"""
    base_url = _public_base()  # 后台线程里没有请求上下文, 先取好
    with _link_gen_lock:
        if task_row_id in _link_gen_running:
            return False
        _link_gen_running.add(task_row_id)

    def run():
        try:
            res = link_shortener.generate_for_task(db, task_row_id, base_url=base_url)
            bus.publish(None, "LINK", f"短链任务 #{task_row_id} 生成完成: 成功 {res.get('created', 0)},"
                                      f" 失败 {res.get('failed', 0)}")
        except Exception as exc:  # noqa: BLE001 - 后台线程: 任何失败都要落到任务状态上
            with suppress(Exception):
                db.update_short_task(task_row_id, status="failed")
            bus.publish(None, "LINK", f"短链任务 #{task_row_id} 生成失败: {type(exc).__name__}")
        finally:
            with _link_gen_lock:
                _link_gen_running.discard(task_row_id)

    threading.Thread(target=run, name=f"link-gen-{task_row_id}", daemon=True).start()
    return True


@app.get("/api/v4/link-tasks")
def v4_link_tasks():
    q = (request.args.get("q") or "").strip()[:100] or None
    tasks = [_short_task_out(t) for t in db.list_short_tasks(q=q, workspace_id=_cur_ws())]
    return jsonify({"tasks": tasks, "kpis": db.link_kpis(workspace_id=_cur_ws())})


@app.post("/api/v4/link-tasks")
def v4_link_task_create():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    if not 1 <= len(name) <= 120:
        return jsonify({"error": "任务名称需 1~120 个字符"}), 400
    try:
        target = link_shortener.validate_target(body.get("target_url"))
        service = link_shortener.normalize_service(body.get("service"), link_shortener.LOCAL)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    clone_id = body.get("clone_task_id")
    if clone_id in ("", None):
        clone_id = None
    else:
        try:
            clone_id = int(clone_id)
        except (TypeError, ValueError):
            return jsonify({"error": "clone_task_id 非法"}), 400
        if _visible_task(clone_id) is None:
            return jsonify({"error": f"克隆任务 #{clone_id} 不存在"}), 400
    note = str(body.get("note") or "").strip()[:300]
    tid = db.create_short_task(name, target, service, clone_id, note, workspace_id=_cur_ws())
    _audit("links.task_create", f"新建短链任务 #{tid} {name} -> {target} ({service})")
    if body.get("generate"):
        _start_link_generation(tid)
    return jsonify({"task": _short_task_out(db.get_short_task(tid), with_links=True)}), 201


@app.get("/api/v4/link-tasks/<int:task_row_id>")
def v4_link_task_detail(task_row_id):
    return jsonify({"task": _short_task_out(_short_task_or_404(task_row_id), with_links=True)})


@app.post("/api/v4/link-tasks/<int:task_row_id>/generate")
def v4_link_task_generate(task_row_id):
    task = _short_task_or_404(task_row_id)
    if not _start_link_generation(task_row_id):
        return jsonify({"error": "该任务正在生成中"}), 409
    _audit("links.generate", f"生成短链任务 #{task_row_id} {task['name']}")
    return jsonify({"task": _short_task_out(db.get_short_task(task_row_id), with_links=True)}), 202


@app.patch("/api/v4/link-tasks/<int:task_row_id>")
def v4_link_task_update(task_row_id):
    """编辑短链任务: 名称 / 目标地址 / 偏好服务 / 关联克隆 / 备注。
    改目标地址不影响已生成的短链(各自记着生成时的地址), 重新生成后用新地址。"""
    task = _short_task_or_404(task_row_id)
    if task_row_id in _link_gen_running:
        return jsonify({"error": "该任务正在生成中, 请稍后再改"}), 409
    body = request.get_json(silent=True) or {}
    fields = {}
    if "name" in body:
        name = str(body.get("name") or "").strip()
        if not 1 <= len(name) <= 120:
            return jsonify({"error": "任务名称需 1~120 个字符"}), 400
        fields["name"] = name
    try:
        if "target_url" in body:
            fields["target_url"] = link_shortener.validate_target(body.get("target_url"))
        if "service" in body:
            fields["service"] = link_shortener.normalize_service(body.get("service"), link_shortener.LOCAL)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if "clone_task_id" in body:
        clone_id = body.get("clone_task_id")
        if clone_id in ("", None, 0):
            fields["clone_task_id"] = 0
        else:
            try:
                clone_id = int(clone_id)
            except (TypeError, ValueError):
                return jsonify({"error": "clone_task_id 非法"}), 400
            if _visible_task(clone_id) is None:
                return jsonify({"error": f"克隆任务 #{clone_id} 不存在"}), 400
            fields["clone_task_id"] = clone_id
    if "note" in body:
        fields["note"] = str(body.get("note") or "").strip()[:300]
    if fields:
        db.update_short_task(task_row_id, **fields)
    _audit("links.task_update", f"编辑短链任务 #{task_row_id} {task['name']}: {', '.join(fields) or '无变更'}")
    return jsonify({"task": _short_task_out(db.get_short_task(task_row_id), with_links=True)})


@app.delete("/api/v4/link-tasks/<int:task_row_id>")
def v4_link_task_delete(task_row_id):
    task = _short_task_or_404(task_row_id)
    if task_row_id in _link_gen_running:
        return jsonify({"error": "该任务正在生成中, 请稍后再删"}), 409
    db.delete_short_task(task_row_id, cascade=True)
    _audit("links.task_delete", f"删除短链任务 #{task_row_id} {task['name']}(级联短链)")
    return jsonify({"ok": True})


def _csv_cell(value) -> str:
    """CSV 单元格: 以 = + - @ 开头的值前加 ' 防表格公式注入。"""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@app.get("/api/v4/link-tasks/<int:task_row_id>/export")
def v4_link_task_export(task_row_id):
    task = _short_task_or_404(task_row_id)
    fmt = (request.args.get("format") or "txt").lower()
    if fmt not in ("txt", "csv"):
        return jsonify({"error": "format 仅支持 txt / csv"}), 400
    links = [_short_link_out(l) for l in db.list_links_of_short_task(task_row_id)]
    fname = f"SL-{task['seq']:03d}_{time.strftime('%Y%m%d_%H%M%S')}.{fmt}"
    if fmt == "txt":
        lines = [l["short_url"] for l in links if l["ok"]]
        return Response("\n".join(lines) + ("\n" if lines else ""), content_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["序号", "服务", "短链", "本地跳转(计数)", "目标地址", "点击", "状态", "说明", "生成时间"])
    for i, l in enumerate(links, 1):
        extras = l.get("extras") or {}
        if not l["ok"]:
            remark = extras.get("error", "")
        elif extras.get("fallback"):
            remark = f"指定 {extras.get('requested')} 失败, 顺延到 {l['service']}"
        else:
            remark = ""
        writer.writerow([_csv_cell(v) for v in (
            i, l["service"], l["short_url"], l["local_url"], l["target_url"], l.get("clicks") or 0,
            "成功" if l["ok"] else "失败", remark, l.get("created_at", ""))])
    return Response("\ufeff" + buf.getvalue(), content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/v4/links/providers")
def v4_link_providers():
    """四家服务的可用性预检(结果缓存 60s; ?refresh=1 强制重测)。"""
    force = request.args.get("refresh") in ("1", "true")
    return jsonify({"providers": link_shortener.preflight(force=force)})


@app.get("/api/v4/links")
def v4_links():
    q = (request.args.get("q") or "").strip()[:100] or None
    standalone = request.args.get("standalone") in ("1", "true")
    links = [_short_link_out(l) for l in db.list_short_links(q=q, standalone=standalone, workspace_id=_cur_ws())]
    return jsonify({"links": links})


@app.post("/api/v4/links")
def v4_link_create():
    """单独新建一条短链: service 指定服务(失败顺延), all 为按顺序自动适配。"""
    body = request.get_json(silent=True) or {}
    try:
        target = link_shortener.validate_target(body.get("target_url"))
        service = link_shortener.normalize_service(body.get("service"), link_shortener.LOCAL)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    label = str(body.get("label") or "").strip()[:120]
    res = link_shortener.shorten(db, target, service, label=label, workspace_id=_cur_ws(),
                                 base_url=_public_base())
    link = _short_link_out(db.get_short_link(res["link_id"]))
    _audit("links.create", f"单独短链 #{res['link_id']} {target} -> {res['short_url'] or '失败'}")
    if not res["ok"]:
        return jsonify({"error": res.get("error") or "生成失败", "link": link,
                        "attempts": res["attempts"]}), 502
    return jsonify({"link": link, "attempts": res["attempts"]}), 201


@app.delete("/api/v4/links/<int:link_id>")
def v4_link_delete(link_id):
    link = db.get_short_link(link_id)
    if link is None or int(link.get("workspace_id") or 1) != _cur_ws():
        return jsonify({"error": "短链不存在"}), 404
    db.delete_short_link(link_id)
    if link.get("link_task_id"):
        db.refresh_short_task_status(link["link_task_id"])
    _audit("links.delete", f"删除短链 #{link_id} {link.get('short_url') or ''}")
    return jsonify({"ok": True})


@app.get("/s/<code>")
def short_link_redirect(code):
    """短链跳转: 302 到目标地址并计一次点击。失败留痕行(没有短链)不跳转。
    新短链只认随机短码; 纯数字按老编号查, 且只对改版前的老短链生效(新短链不可枚举)。"""
    link = None
    if code.isdigit() and len(code) <= 12:
        link = db.get_short_link(int(code))
        if link is not None and not link.get("legacy_numeric"):
            link = None
    elif link_shortener.valid_code(code):
        link = db.get_short_link_by_code(code)
    if link is None or not link.get("short_url"):
        return Response("短链不存在", status=404, content_type="text/plain; charset=utf-8")
    try:
        target = link_shortener.validate_target(link.get("target_url"))
    except ValueError:
        return Response("短链目标地址无效", status=410, content_type="text/plain; charset=utf-8")
    db.bump_short_link_click(link["id"])
    resp = redirect(target, code=302)
    resp.headers["Cache-Control"] = "no-store"  # 每次点击都要回到这里计数
    return resp

# ---------------------------------------------------------------------------
# 远程发布: 把导出工程包经 SSH 发布到用户自己的服务器长期运行(服务层 core/remote_deploy.py)
# 与本机部署(上面的 /api/v4/docker, core/deploy.py)互不调用。凭据只以 Fernet 密文落库, 接口只回掩码。
# ---------------------------------------------------------------------------
from core import remote_deploy  # noqa: E402
from core.remote_deploy import RemoteDeployError  # noqa: E402


def _remote_package(clone_task_id: int) -> Path:
    """导出 ZIP 路径: 与「下载 ZIP」同源, 包被存储清理删掉时按同一逻辑重建。"""
    task = db.get_task(clone_task_id)
    if task is None:
        raise RemoteDeployError(f"关联的克隆任务 #{clone_task_id} 不存在")
    zip_path = Path(task["zip_path"]) if task.get("zip_path") else Path()
    if zip_path.is_file():
        return zip_path
    ws = _workspace_of(task)
    if task.get("status") != "completed" or not (ws / "site.json").is_file():
        raise RemoteDeployError(f"克隆任务 #{clone_task_id} 尚未完成或工作空间已清理, 没有可发布的工程包")
    zip_path = Path(_regenerate_project(task, ws))
    db.update_task(clone_task_id, zip_path=str(zip_path))
    return zip_path


remote_deployer = remote_deploy.RemoteDeployer(db, _remote_package, security.decrypt_value,
                                               config.EXPORTS_DIR / "remote")


def _remote_visible(rec: dict) -> bool:
    """档案经关联克隆任务归属工作区: 登录模式下只看得到本区任务的档案。"""
    if not _auth_required():
        return True
    return bool(rec.get("clone_task_id")) and _visible_task(rec["clone_task_id"]) is not None


def _remote_or_404(dep_id: int) -> dict:
    rec = db.get_remote_deploy(dep_id)
    if rec is None or not _remote_visible(rec):
        abort(404)
    return rec


def _remote_out(rec: dict, log_tail: int = 0) -> dict:
    out = remote_deploy.RemoteDeployer.present(rec, log_tail=log_tail)
    if remote_deployer.is_busy(rec["id"]):
        out["status"] = "deploying"
    clone = _visible_task(rec["clone_task_id"]) if rec.get("clone_task_id") else None
    out["clone_domain"] = (clone or {}).get("domain", "")
    return out


def _remote_form(body: dict, partial: bool) -> dict:
    """校验档案字段; 返回可直接落库的字段(凭据已加密)。partial=True 时只校验传了的字段。"""
    out: dict = {}
    if not partial or "name" in body:
        name = str(body.get("name") or "").strip()
        if not 1 <= len(name) <= 120:
            raise RemoteDeployError("名称需 1~120 个字符")
        out["name"] = name
    if not partial or "host" in body:
        out["host"] = remote_deploy.validate_host(body.get("host"))
    if not partial or "ssh_port" in body:
        out["ssh_port"] = remote_deploy.validate_port(body.get("ssh_port", 22), "SSH 端口")
    if not partial or "username" in body:
        out["username"] = remote_deploy.validate_username(body.get("username"))
    if not partial or "remote_port" in body:
        out["remote_port"] = remote_deploy.validate_port(body.get("remote_port", 8000), "服务端口")
    if not partial or "auth_type" in body:
        auth = str(body.get("auth_type") or "password")
        if auth not in remote_deploy.AUTH_TYPES:
            raise RemoteDeployError("认证方式只能是 password(口令)或 key(私钥)")
        out["auth_type"] = auth
    if not partial or "clone_task_id" in body:
        try:
            cid = int(body.get("clone_task_id"))
        except (TypeError, ValueError):
            raise RemoteDeployError("请选择要发布的克隆任务") from None
        task = _visible_task(cid)
        if task is None:
            raise RemoteDeployError(f"克隆任务 #{cid} 不存在")
        if task.get("status") != "completed":
            raise RemoteDeployError(f"克隆任务 #{cid} 还没有完成, 不能发布")
        out["clone_task_id"] = cid
    if not partial or "note" in body:
        out["note"] = str(body.get("note") or "").strip()[:300]
    secret = body.get("password")
    if secret not in (None, ""):
        secret = str(secret)
        if len(secret) > 16384:
            raise RemoteDeployError("口令/私钥过长")
        out["password_enc"] = security.encrypt_value(secret)
    elif not partial:
        raise RemoteDeployError("请填写 SSH 口令或私钥")
    return out


@app.get("/api/v4/remote/deploys")
def v4_remote_list():
    q = (request.args.get("q") or "").strip()[:100] or None
    rows = [r for r in db.list_remote_deploys(q=q) if _remote_visible(r)]
    return jsonify({"deploys": [_remote_out(r) for r in rows]})


@app.post("/api/v4/remote/deploys")
def v4_remote_create():
    body = request.get_json(silent=True) or {}
    try:
        f = _remote_form(body, partial=False)
    except RemoteDeployError as e:
        return jsonify({"error": str(e)}), 400
    dep_id = db.create_remote_deploy(f["name"], f["host"], f["ssh_port"], f["username"], f["password_enc"],
                                     f["clone_task_id"], f["remote_port"], f["note"], auth_type=f["auth_type"])
    _audit("remote.create", f"新建远程发布 #{dep_id} {f['name']} -> {f['username']}@{f['host']}:{f['ssh_port']}"
                            f"(克隆 #{f['clone_task_id']}, 端口 {f['remote_port']})")
    started = False
    if body.get("deploy_now"):
        try:
            remote_deployer.start_deploy_thread(dep_id)
            started = True
            _audit("remote.deploy", f"发布远程档案 #{dep_id}")
        except RemoteDeployError:
            pass
    return jsonify({"deploy": _remote_out(db.get_remote_deploy(dep_id), log_tail=80), "started": started}), 201


@app.get("/api/v4/remote/deploys/<int:dep_id>")
def v4_remote_detail(dep_id):
    tail = request.args.get("tail", "200")
    return jsonify({"deploy": _remote_out(_remote_or_404(dep_id), log_tail=int(tail) if tail.isdigit() else 200)})


@app.patch("/api/v4/remote/deploys/<int:dep_id>")
def v4_remote_update(dep_id):
    rec = _remote_or_404(dep_id)
    if remote_deployer.is_busy(dep_id):
        return jsonify({"error": "正在发布中, 请等发布结束再修改"}), 409
    body = request.get_json(silent=True) or {}
    try:
        f = _remote_form(body, partial=True)
    except RemoteDeployError as e:
        return jsonify({"error": str(e)}), 400
    reset_key = bool(body.get("reset_host_key"))
    if ("host" in f and f["host"] != rec["host"]) or ("ssh_port" in f and f["ssh_port"] != rec["ssh_port"]):
        reset_key = True   # 换了服务器: 旧指纹作废, 下次连接重新 TOFU
    if reset_key and rec.get("host_key"):
        f["host_key"] = ""
    if f:
        db.update_remote_deploy(dep_id, **f)
    changed = sorted(k if k != "password_enc" else "password" for k in f)
    _audit("remote.update", f"修改远程发布 #{dep_id}: {', '.join(changed) or '无变化'}")
    return jsonify({"deploy": _remote_out(db.get_remote_deploy(dep_id), log_tail=80)})


@app.delete("/api/v4/remote/deploys/<int:dep_id>")
def v4_remote_delete(dep_id):
    """删档案前先停远端服务(不存在也算成功); ?purge=1 同时删远端目录; 连不上时 ?force=1 只删档案。"""
    rec = _remote_or_404(dep_id)
    if remote_deployer.is_busy(dep_id):
        return jsonify({"error": "正在发布中, 请等发布结束再删除"}), 409
    purge = request.args.get("purge") in ("1", "true")
    force = request.args.get("force") in ("1", "true")
    cleaned = False
    if rec.get("status") != "idle" or purge:
        try:
            remote_deployer.teardown(dep_id, purge=purge)
            cleaned = True
        except RemoteDeployError as e:
            if not force:
                return jsonify({"error": f"清理远端失败: {e}", "can_force": True}), 502
    db.delete_remote_deploy(dep_id)
    _audit("remote.delete", f"删除远程发布 #{dep_id} {rec['name']}"
                            f"({'已清理远端目录' if cleaned and purge else '已停远端服务' if cleaned else '未清理远端'})")
    return jsonify({"ok": True, "cleaned": cleaned, "purged": cleaned and purge})


@app.post("/api/v4/remote/deploys/<int:dep_id>/deploy")
def v4_remote_deploy(dep_id):
    _remote_or_404(dep_id)
    try:
        remote_deployer.start_deploy_thread(dep_id)
    except RemoteDeployError as e:
        code = 409 if remote_deployer.is_busy(dep_id) else 400
        return jsonify({"error": str(e)}), code
    _audit("remote.deploy", f"发布远程档案 #{dep_id}")
    return jsonify({"started": True, "deploy": _remote_out(db.get_remote_deploy(dep_id), log_tail=80)}), 202


@app.post("/api/v4/remote/deploys/<int:dep_id>/<action>")
def v4_remote_action(dep_id, action):
    _remote_or_404(dep_id)
    if action not in ("start", "stop"):
        return jsonify({"error": "不支持的操作"}), 400
    try:
        res = remote_deployer.start(dep_id) if action == "start" else remote_deployer.stop(dep_id)
    except RemoteDeployError as e:
        return jsonify({"error": str(e)}), 409 if remote_deployer.is_busy(dep_id) else 502
    _audit("remote." + action, f"远程档案 #{dep_id} {action}")
    return jsonify(res)


@app.get("/api/v4/remote/deploys/<int:dep_id>/logs")
def v4_remote_logs(dep_id):
    rec = _remote_or_404(dep_id)
    tail = request.args.get("tail", "200")
    n = int(tail) if tail.isdigit() else 200
    if request.args.get("kind") == "runtime":
        try:
            return jsonify({"kind": "runtime", "logs": remote_deployer.runtime_logs(dep_id, n)})
        except RemoteDeployError as e:
            return jsonify({"kind": "runtime", "logs": "", "error": str(e)}), 502
    lines = (rec.get("log") or "").splitlines()[-max(1, min(n, 2000)):]
    return jsonify({"kind": "deploy", "logs": "\n".join(lines)})


@app.get("/api/v4/remote/deploys/<int:dep_id>/status")
def v4_remote_status(dep_id):
    rec = _remote_or_404(dep_id)
    if remote_deployer.is_busy(dep_id):
        return jsonify({"status": "deploying", "progress": rec.get("progress"), "stage": rec.get("stage")})
    try:
        res = remote_deployer.probe(dep_id)
    except RemoteDeployError as e:
        return jsonify({"status": rec.get("status"), "error": str(e)}), 502
    return jsonify({"status": rec.get("status"), **res})


@app.post("/api/v4/remote/test-connection")
def v4_remote_test_connection():
    """只验证能否登录。编辑已有档案时可传 id 且口令留空: 用库里的凭据与已记录指纹测。"""
    body = request.get_json(silent=True) or {}
    secret = str(body.get("password") or "")
    stored_key = ""
    auth = str(body.get("auth_type") or "password")
    if body.get("id"):
        try:
            rec = _remote_or_404(int(body["id"]))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "id 非法"}), 400
        if not secret:
            secret = security.decrypt_value(rec.get("password_enc") or "")
            auth = rec.get("auth_type") or "password"
        if (str(body.get("host") or rec["host"]).strip() == rec["host"]
                and str(body.get("ssh_port") or rec["ssh_port"]) == str(rec["ssh_port"])):
            stored_key = rec.get("host_key") or ""
    res = remote_deploy.RemoteDeployer.test_connection(body.get("host"), body.get("ssh_port", 22),
                                                       body.get("username"), secret, auth, stored_key)
    return jsonify(res), 200 if res["ok"] else 400


# ---------------------------------------------------------------------------
# 沙盒预览(静态文件 + 反向代理兜底)
# ---------------------------------------------------------------------------
@app.route("/preview/<int:task_id>/", defaults={"subpath": ""},
           methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.route("/preview/<int:task_id>/<path:subpath>",
           methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def preview(task_id, subpath=""):
    task = _task_or_404(task_id)
    ws = _workspace_of(task)
    if not ws.is_dir():
        abort(404)
    site = _load_site_meta(ws)
    routes = site.get("routes", {})
    if subpath == "":
        resp = _pv_cookie(send_from_directory(ws, routes.get("/", "index.html")), task_id)
        share = request.args.get("share", "")
        if share and _share_token_ok(task_id, share):  # 分享链接: 子资源靠路径级 cookie 放行
            exp = int(share.split(".", 1)[0])
            resp.set_cookie(f"pvs_{task_id}", share, path=f"/preview/{task_id}/",
                            max_age=max(60, exp - int(time.time())), samesite="Lax",
                            httponly=True)
        return resp
    normalized = "/" + subpath.strip("/")
    if normalized in routes:
        return _pv_cookie(send_from_directory(ws, routes[normalized]), task_id)
    target = (ws / subpath).resolve()
    if _inside(ws, target) and target.is_file():
        return send_from_directory(ws, subpath)
    # 沙盒内 Mock API(SPA 伪装后 XHR 以 /preview/<id>/ 为基准发请求)
    mock_resp = _serve_from_task(task, subpath)
    if mock_resp is not None:
        return mock_resp
    if site.get("proxy_enabled") and site.get("upstream"):
        return _proxy_fallback(site["upstream"], subpath)
    abort(404)


# ---------------------------------------------------------------------------
def _parse_json(text: str) -> dict:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _pv_cookie(resp, task_id):
    """预览页面响应带上当前任务号: 样式表里的根路径引用(字体/背景图)的 Referer 是样式表
    自身地址(/static/css/...), 不含 /preview/<id>, Referer 兜底认不出任务 —— 用它补位。"""
    with suppress(Exception):
        resp.set_cookie("clone_pv", str(task_id), path="/", samesite="Lax", httponly=True)
    return resp


def _referer_fallback(allow_root: bool = False):
    """按 Referer(/preview/<id>)或最近打开的预览任务 cookie 找到任务, 从其工作空间回应根路径请求。
    allow_root: 根路径 "/" 也回落(克隆页 JS 整页导航逃逸到根)。找不到返回 None。"""
    if request.path.startswith("/preview/"):
        return None
    referer = request.headers.get("Referer", "")
    match = PREVIEW_REFERER_RE.search(referer)
    task_ref = match.group(1) if match else None
    if request.path == "/" and not allow_root:
        return None
    if task_ref is None and request.method == "GET" and referer and request.path != "/" \
            and urlsplit(referer).netloc == request.host \
            and (_on_preview_side() or not request.path.startswith("/api/")):
        # 同源、Referer 是克隆页里的子资源(样式表)而非平台页: 退到最近打开的预览任务
        cookie_id = request.cookies.get("clone_pv", "")
        if cookie_id.isdigit() and not urlsplit(referer).path.rstrip("/") in ("", "/index.html"):
            task_ref = cookie_id
    if not task_ref:
        return None
    task = _visible_task(int(task_ref))
    if task is None:
        return None
    return _serve_from_task(task, request.path)


@app.errorhandler(404)
def not_found(_error):
    # Referer 智能沙盒路由兜底: 预览 iframe 内逃逸到根路径的静态资源 / Mock API
    # 请求(其 Referer 必含 /preview/<id>, 与平台自身 API 的根 Referer 天然可区分)
    # 自动转发至对应工作空间。
    if not _preview_isolated():
        resp = _referer_fallback()
        if resp is not None:
            return resp
    if request.path.startswith("/api/") or request.path.startswith("/preview/"):
        return jsonify({"error": "not found"}), 404
    return render_template("index.html"), 404


@app.errorhandler(500)
def server_error(error):
    return jsonify({"error": str(error)}), 500




def _audit_retention_worker():
    # 审计保留策略: 启动清一次, 之后每 6 小时清一次
    days = config.AUDIT_RETENTION_DAYS
    if days <= 0:
        return
    n = db.purge_audit_before(days)
    if n:
        bus.publish(None, "AUDIT", f"审计保留策略: 已清理 {n} 条超 {days} 天的日志")
    while True:
        time.sleep(6 * 3600)
        n = db.purge_audit_before(days)
        if n:
            bus.publish(None, "AUDIT", f"审计保留策略: 已清理 {n} 条超 {days} 天的日志")


# ---------------------------------------------------------------------------
# 部署(Docker): 极速启动 / 停止 / 重启 / 一键销毁 / 自动销毁
# ---------------------------------------------------------------------------
from core.deploy import DockerDeployer, DeployError  # noqa: E402

deployer = DockerDeployer(db, _workspace_of, _regenerate_project, db.get_task)


def _deploy_call(fn, *args, **kwargs):
    try:
        return jsonify(fn(*args, **kwargs) or {"ok": True})
    except DeployError as e:
        return jsonify({"error": str(e)}), 400


def _ws_containers(items: list) -> list:
    """部署列表按工作区过滤: 只保留本区任务的容器; 无主容器仅单机模式可见。"""
    if not _auth_required():
        return items
    mine = db.workspace_task_ids(_cur_ws())
    return [c for c in items if c.get("task_id") in mine]


@app.get("/api/v4/docker/summary")
def v4_docker_summary():
    out = deployer.summary()
    items = _ws_containers(out.get("containers") or [])
    out.update(containers=items, total=len(items),
               running=sum(1 for c in items if c["status"] == "running"),
               stopped=sum(1 for c in items if c["status"] in ("stopped", "failed", "missing")),
               ports=sum(1 for c in items if c["status"] == "running" and c["port"]))
    return jsonify(out)


@app.get("/api/v4/docker")
def v4_docker_list():
    st = deployer.status()
    return jsonify({"available": st["available"], "containers": _ws_containers(deployer.list())})


@app.get("/api/v4/docker/stats")
def v4_docker_stats():
    stats = deployer.stats()
    if _auth_required():
        mine = db.workspace_task_ids(_cur_ws())
        stats = {k: v for k, v in stats.items() if _stat_task_id(k) in mine}
    return jsonify({"stats": stats})


def _stat_task_id(key):
    m = re.search(r"(\d+)$", str(key))
    return int(m.group(1)) if m else None


@app.get("/api/v4/docker/<int:task_id>/logs")
def v4_docker_logs(task_id):
    _task_or_404(task_id)
    tail = request.args.get("tail", "200")
    return jsonify({"logs": deployer.logs(task_id, int(tail) if tail.isdigit() else 200)})


@app.post("/api/v4/docker/batch")
def v4_docker_batch():
    body = request.get_json(silent=True) or {}
    action = body.get("action", "")
    if action not in ("stop", "stop_all", "restart", "start", "destroy", "cleanup_stopped"):
        return jsonify({"error": "不支持的批量操作"}), 400
    ids = body.get("task_ids") or None
    if ids is not None and not all(isinstance(i, int) for i in ids):
        return jsonify({"error": "task_ids 必须是整数列表"}), 400
    if _auth_required():  # 只能动本区的容器; 不传 ids = 本区全部
        mine = db.workspace_task_ids(_cur_ws())
        ids = [i for i in (ids or mine) if i in mine]
        if not ids:
            return jsonify({"affected": 0})
    n = deployer.batch(action, ids)
    _audit("docker.batch", f"{action} {ids or 'all'} -> {n}")
    return jsonify({"affected": n})


@app.post("/api/tasks/<int:task_id>/docker/<action>")
def v4_docker_task_action(task_id, action):
    body = request.get_json(silent=True) or {}
    _task_or_404(task_id)
    if action == "deploy" or (action == "start" and not db.get_docker(task_id)) or \
            (action == "start" and body):
        blocked = _quota_block(_cur_ws(), "deploy") if not db.get_docker(task_id) else None
        if blocked:
            return blocked
        _audit("docker.deploy", f"task={task_id} {body}")
        kw = dict(mode=body.get("mode") or deployer.default_mode(),
                  ttl_hours=body.get("ttl_hours", 24),
                  bind=body.get("bind", "127.0.0.1"),
                  mem_limit=body.get("mem_limit", "512m"),
                  port=int(body["port"]) if str(body.get("port") or "").isdigit() else 0)
        if not body.get("async"):
            return _deploy_call(deployer.deploy, task_id, **kw)
        try:   # 异步: 参数错误立刻返回; 之后列表按 step 显示 准备工程 → 启动 → 健康检查
            deployer.precheck(task_id, kw["mode"], kw["ttl_hours"], kw["bind"], kw["mem_limit"])
        except DeployError as e:
            return jsonify({"error": str(e)}), 400
        if deployer.step(task_id):
            return jsonify({"error": "这个站点正在部署中"}), 409
        deployer.mark_starting(task_id, kw["mode"])

        def _bg():
            t0 = time.time()
            try:
                deployer.deploy(task_id, **kw)
                bus.publish(task_id, "DEPLOY", f"部署完成, 用时 {time.time() - t0:.1f}s")
            except Exception as e:  # noqa: BLE001 — 后台线程兜底
                bus.publish(task_id, "DEPLOY", f"部署失败: {str(e)[:200]}", "ERROR")
                if (db.get_docker(task_id) or {}).get("status") == "starting":
                    db.upsert_docker(task_id, status="failed", last_error=str(e)[:600])   # 不留「启动中」
        threading.Thread(target=_bg, daemon=True, name=f"deploy-{task_id}").start()
        return jsonify({"deploy": deployer.get(task_id), "async": True}), 202
    if action == "start":
        resp = _deploy_call(deployer.start, task_id)
    elif action == "stop":
        resp = _deploy_call(deployer.stop, task_id)
    elif action == "restart":
        resp = _deploy_call(deployer.restart, task_id)
    elif action in ("remove", "destroy"):
        resp = _deploy_call(deployer.destroy, task_id)
    elif action == "extend":
        resp = _deploy_call(deployer.extend, task_id, body.get("hours", 24))
    else:
        return jsonify({"error": "不支持的操作"}), 400
    _audit("docker." + action, f"task={task_id}")
    return resp


def _backfill_generate_meta():
    """老的生成任务: 任务名还是 generated 的按任务书标题回填; token 流水按 generate_report.json 的 calls 回填(幂等)。"""
    from core.uir.generate import task_title, token_rows
    try:
        rows = db._conn.execute(
            "SELECT t.id, t.domain, t.workspace_path, b.name AS batch_name, b.config_json FROM clone_tasks t"
            " JOIN batch_jobs b ON t.batch_id = b.id WHERE t.target_url LIKE 'uir:generate:%'").fetchall()
    except Exception:
        return
    have = {r[0] for r in db._conn.execute("SELECT DISTINCT task_id FROM token_usage WHERE kind = 'generate'").fetchall()}
    for r in rows:
        ws = Path(r["workspace_path"] or "")
        rep = {}
        with suppress(Exception):
            rep = json.loads((ws / "generate_report.json").read_text(encoding="utf-8"))
        if (r["domain"] or "") in ("", "generated"):
            spec = (_parse_json(r["config_json"] or "{}").get("gen") or {})
            title = task_title(rep.get("brief") or {}, spec) if rep.get("brief") else ""
            if not title:
                title = (r["batch_name"] or "").replace("生成 · ", "").strip()[:60] or task_title({}, spec)
            db.update_task(r["id"], domain=title)
        if r["id"] not in have and rep.get("calls"):
            with suppress(Exception):
                db.replace_token_usage(r["id"], "generate", token_rows(rep))


def _boot():
    db.ensure_default_project()
    _apply_saved_settings()
    # 模型消耗: 单价表 → 记账出口; 老流水(无费用列时写入的)按单价表补算一次
    llm_usage.load_saved(db.get_setting("llm_pricing", ""))
    llm_usage.SINK = _llm_sink
    with suppress(Exception):
        _backfill_generate_meta()
    with suppress(Exception):
        db.recompute_token_costs(llm_usage.price_for, only_missing=True)
    scheduler.start()
    threading.Thread(target=_audit_retention_worker, daemon=True,
                     name="audit-retention").start()
    threading.Thread(target=_schedule_ticker, daemon=True, name="schedule-ticker").start()
    deployer.start_background()
    with suppress(Exception):   # 上次进程留下的「发布中」记录: 线程已不在, 改为失败
        n = remote_deployer.recover_after_restart()
        if n:
            bus.publish(None, "REMOTE", f"平台重启: {n} 条中断的远程发布已标记为失败")

    def _shutdown():
        scheduler.shutdown()
        db.close()

    import atexit
    atexit.register(_shutdown)


_boot()

def _use_waitress() -> bool:
    """平台默认用 waitress(生产级 WSGI 服务器); CLONER_SERVER=werkzeug 或未安装 waitress 时用 Flask 自带服务器。"""
    if config.SERVER_KIND == "werkzeug":
        return False
    try:
        import waitress  # noqa: F401
        return True
    except ImportError:
        return False


def _waitress_opts() -> dict:
    # send_bytes=1: 日志流(SSE)每条事件立即发出, 不攒满 18KB 再发; ident 空: 不在响应头暴露服务器软件
    return {"threads": config.SERVER_THREADS, "send_bytes": 1, "ident": "", "channel_timeout": 300,
            "connection_limit": max(100, config.SERVER_THREADS * 8)}


def _start_preview_server() -> None:
    """克隆预览独立端口(同一个应用, 另一个监听口): 克隆站脚本与平台不同源, 碰不到平台会话与接口。"""
    if config.PREVIEW_PORT <= 0 or config.PREVIEW_PORT == config.PORT:
        return
    try:
        if _use_waitress():
            from waitress.server import create_server
            wsrv = create_server(app, host=config.HOST, port=config.PREVIEW_PORT, **_waitress_opts())
            run = wsrv.run
        else:
            from werkzeug.serving import make_server
            run = make_server(config.HOST, config.PREVIEW_PORT, app, threaded=True).serve_forever
    except OSError as e:
        print(f"[security] 预览端口 {config.PREVIEW_PORT} 无法监听({e}), 预览回到与平台同源的旧模式;"
              " 可用 CLONER_PREVIEW_PORT 换一个端口", flush=True)
        return
    _PREVIEW["on"] = True
    threading.Thread(target=run, daemon=True, name="preview-origin").start()
    print(f" * 克隆预览独立端口: http://{config.HOST}:{config.PREVIEW_PORT}", flush=True)


def _announce_setup_token() -> None:
    if not _auth_required() and config.OPEN_MODE_LOCAL_ONLY and not websec.is_loopback_bind(config.HOST):
        print(f" * 平台还没有管理员。本机打开可直接初始化; 从其他机器初始化需要这个一次性口令: {_SETUP_TOKEN}",
              flush=True)


if __name__ == "__main__":
    _start_preview_server()
    _announce_setup_token()
    if _use_waitress():
        from waitress import serve
        print(f" * 平台服务(waitress, {config.SERVER_THREADS} 线程): http://{config.HOST}:{config.PORT}", flush=True)
        serve(app, host=config.HOST, port=config.PORT, **_waitress_opts())
    else:
        app.run(host=config.HOST, port=config.PORT, threaded=True, use_reloader=False)
