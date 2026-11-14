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
  GET  /api/tasks/<id>/screenshots/<img_type>   原站/克隆/差异图
  GET  /api/stream_logs             SSE 实时日志流
  GET  /api/telemetry               系统遥测(浏览器池/CAS/队列)
  GET  /preview/<id>/               沙盒预览(含反向代理兜底)
  GET  /api/download/<id>           单任务 ZIP 导出
  GET  /api/export_all              批量导出全部已完成 ZIP
  POST /api/cache/clear             清空 CAS 缓存池
"""
import io
import csv
import io
import json
import secrets
import os
import base64
import re
import shutil
import subprocess
import threading
import time
import zipfile
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import requests as upstream_http
from flask import (Flask, Response, abort, jsonify, render_template, request,
                   g, send_file, send_from_directory, session)

import config
from core import archive, flask_generator, live, security
from core import ai_mock_generator
from core.clone.api_match import pick_entry
from core.browser_pool import SyncBrowserPool
from core.logger import LogBus
from core.scheduler import Scheduler
from models import ALL_WORKSPACES, Database, domain_of

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True  # 模板改动即时生效
app.secret_key = security.session_secret()
app.permanent_session_lifetime = timedelta(hours=12)
db = Database(config.DB_PATH)
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
    if user is None or not user.get("is_active"):
        session.clear()
        return None
    return user


def _public_user(user: dict):
    if not user:
        return None
    return {"id": user["id"], "username": user["username"],
            "display_name": user.get("display_name") or user["username"],
            "role": user["role"]}


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    first = forwarded.split(",")[0].strip() if forwarded else ""
    return first or (request.remote_addr or "local")


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
        return True
    user = _current_user()
    return user is not None and db.get_membership(int(workspace_id), user["id"]) is not None


@app.before_request
def _rbac_gate():
    """开放模式全放行。有用户后: /api/* 需登录; 平台级运维接口需平台管理员;
    成员/审计/邀请/工作区设置需该区管理员; 写操作需该区操作员及以上。
    页面与 /preview 不在此拦截(预览按任务归属另行校验)。"""
    if not _auth_required():
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


def _workspace_of(task: dict) -> Path:
    ws = Path(task["workspace_path"]) if task.get("workspace_path") else Path()
    if not str(ws).startswith(str(config.WORKSPACE_ROOT)):
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


def _proxy_fallback(upstream: str, subpath: str):
    url = upstream.rstrip("/") + "/" + subpath.lstrip("/")
    if request.query_string:
        url += "?" + request.query_string.decode("latin-1")
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "connection", "accept-encoding",
                             "content-length", "transfer-encoding")
    }
    headers.setdefault("Referer", upstream.rstrip("/") + "/")
    headers.setdefault("User-Agent", config.DEFAULT_USER_AGENT)
    try:
        upstream_resp = upstream_http.request(
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
        if str(target).startswith(str(ws.resolve())) and target.is_file():
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


@app.get("/")
def index_page():
    # Referer 兜底: 克隆页 JS 整页导航逃逸到平台根时(其 Referer 必含
    # /preview/<id>), 回落服务该任务的克隆首页, 避免在预览 iframe 里渲染出平台 UI。
    referer = request.headers.get("Referer", "")
    match = PREVIEW_REFERER_RE.search(referer)
    if match:
        task = _visible_task(int(match.group(1)))
        if task is not None:
            resp = _serve_from_task(task, "/")
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
    return jsonify({"batches": db.list_batches(50, workspace_id=_cur_ws())})


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
        task["has_cloned_preview"] = (ws / "cloned_preview.png").is_file()
    return jsonify({"tasks": tasks})


@app.get("/api/tasks/<int:task_id>")
def task_detail(task_id):
    task = _task_or_404(task_id)
    task["stats"] = _parse_json(task.pop("stats_json", "") or "{}")
    ws = _workspace_of(task)
    site = _load_site_meta(ws)
    task["site"] = site
    task["has_original_preview"] = (ws / "original_preview.png").is_file()
    task["has_cloned_preview"] = (ws / "cloned_preview.png").is_file()
    task["has_diff_preview"] = (ws / "diff_preview.png").is_file()
    task["has_ai_report"] = (ws / "ai_repair_report.json").is_file()
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

    serialized = json.dumps(entries, ensure_ascii=False, indent=2)
    (ws / "mock_data.json").write_text(serialized, encoding="utf-8")

    build_dir = ws / "flask_app"
    if build_dir.is_dir():
        (build_dir / "mock_data.json").write_text(serialized, encoding="utf-8")
        zip_path = config.EXPORTS_DIR / f"{task_id}.zip"
        flask_generator.build_zip(build_dir, zip_path, f"clone_site_{task_id}")
        db.update_task(task_id, zip_path=str(zip_path))
    else:
        zip_path = _regenerate_project(task, ws)
        db.update_task(task_id, zip_path=str(zip_path))
    bus.publish(task_id, "MOCK-API", f"已{action} 并重建 ZIP")
    _audit("mock.save", f"任务 #{task_id} {action}")
    return jsonify({"ok": True, "index": new_index, "entry": entries[new_index]})


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
    target = ws / filename
    if not target.is_file():
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
    source = ws / filename
    if not source.is_file():
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
    if not str(target).startswith(str(ws.resolve())):
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
    return jsonify({
        "key_configured": bool(config.AI_API_KEY),
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
    project_id = form.get("project_id")
    project_id = int(project_id) if str(project_id or "").isdigit() else None
    if project_id is not None and (db.get_project(project_id) or {}).get("workspace_id") != _cur_ws():
        return jsonify({"error": f"项目 #{project_id} 不存在"}), 400
    name = str(form.get("name") or "").strip() or f"生成 · {label}"
    target_url = f"{gen_settings.TARGET_URL_PREFIX}{kind}:{label}"
    blocked = _quota_block(_cur_ws(), "clone", 1)
    if blocked:
        return blocked
    batch_id = db.create_batch(name, [(target_url, "generated")], cfg, project_id,
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
            ai_cfg = {"base_url": config.AI_API_BASE_URL, "api_key": key,
                      "model": config.AI_MODEL, "user_prompt": prompt}
            with pool.context(viewport={"width": 1920, "height": 1080}) as ctx:
                report = ai_vision_repair.run_vision_repair(
                    ws, ai_cfg, detail,
                    _say,
                    engine._make_render_and_score(ctx),
                    page_file=engine._main_page_file(),
                    region_cells=engine._region_cells())
            final = report.get("final_score")
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
    return jsonify({"required": _auth_required(),
                    "open_mode": not _auth_required(),
                    "user": pub,
                    "workspace": _workspace_payload(user)})


@app.post("/api/auth/bootstrap")
def auth_bootstrap():
    """开放模式 -> 初始化首个管理员(同时完成登录)。"""
    if _auth_required():
        return jsonify({"error": "系统已初始化, 请直接登录"}), 409
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not _USERNAME_RE.match(username):
        return jsonify({"error": "用户名需 2~32 位字母/数字/._-"}), 400
    if len(password) < 8:
        return jsonify({"error": "密码至少 8 位"}), 400
    uid = db.create_user(username, security.hash_password(password),
                         "admin", str(data.get("display_name") or "").strip())
    db.add_member(1, uid, "owner")  # 首个管理员 = 平台管理员 + 默认工作区所有者
    session["uid"] = uid
    session["ws"] = 1
    session.permanent = True
    db.touch_login(uid)
    _audit("user.bootstrap", f"初始化管理员 {username}", username=username)
    return jsonify({"user": _public_user(db.get_user(uid))}), 201


@app.post("/api/auth/login")
def auth_login():
    if not _auth_required():
        return jsonify({"error": "系统处于开放模式, 请先初始化管理员账号"}), 400
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    user = db.get_user_by_username(username)
    if user is None or not security.verify_password(password, user["password_hash"]):
        _audit("login.failed", f"登录失败: {username or '(空)'}", username=username or "?")
        time.sleep(0.4)  # 暴力破解节流
        return jsonify({"error": "用户名或密码错误"}), 401
    if not user["is_active"]:
        _audit("login.blocked", "已停用账号尝试登录", username=username)
        return jsonify({"error": "账号已被停用, 请联系管理员"}), 403
    session["uid"] = user["id"]
    session.pop("ws", None)
    session.permanent = True
    db.touch_login(user["id"])
    g.pop("ws_ctx", None)
    _audit("login", "登录成功", username=username, workspace_id=_ws_ctx()[0] or 1)
    return jsonify({"user": _public_user(user)})


@app.post("/api/auth/logout")
def auth_logout():
    user = _current_user()
    if user:
        _audit("logout", "退出登录", username=user["username"])
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
def _member_out(m: dict) -> dict:
    return {"id": m["id"], "username": m["username"], "display_name": m["display_name"],
            "role": m["role"], "is_active": m["is_active"], "last_login_at": m["last_login_at"],
            "created_at": m["joined_at"] or m["created_at"]}


@app.get("/api/v4/users")
def v4_users():
    """当前工作区的成员(角色为本区角色)。"""
    return jsonify({"users": [_member_out(m) for m in db.list_members(_cur_ws())],
                    "invites": db.list_invites(_cur_ws())})


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
        return jsonify({"user": existing}), 201
    if len(password) < 8:
        return jsonify({"error": "密码至少 8 位"}), 400
    uid = db.create_user(username, security.hash_password(password), "user",
                         str(data.get("display_name") or "").strip()[:60])
    db.add_member(wid, uid, role, (_current_user() or {}).get("id"))
    _audit("user.create", f"创建成员 {username}({role})")
    return jsonify({"user": db.get_user(uid)}), 201


@app.patch("/api/v4/users/<int:user_id>")
def v4_user_update(user_id):
    wid, my_role = _ws_ctx()
    target = db.get_user(user_id)
    m = db.get_membership(wid, user_id) if target else None
    if m is None:
        return jsonify({"error": "用户不存在"}), 404
    me = _current_user()
    platform = _is_platform_admin(me)
    data = request.get_json(silent=True) or {}
    fields, changed = {}, []
    if "role" in data:
        role = data.get("role")
        if role not in WS_ROLE_RANK:
            return jsonify({"error": "非法角色"}), 400
        if "owner" in (role, m["role"]) and my_role != "owner":
            return jsonify({"error": "只有所有者可以转让或变更所有者"}), 403
        if m["role"] in ("owner", "admin") and role not in ("owner", "admin") \
                and db.count_managers(wid) <= 1:
            return jsonify({"error": "不能降级最后一个管理员"}), 409
        db.set_member_role(wid, user_id, role)
        changed.append("role")
    if "is_active" in data:  # 账号停用是全局动作: 本区内语义为"移出本区"由 DELETE 承担
        if not platform:
            return jsonify({"error": "停用账号需要平台管理员; 可改为把成员移出本工作区"}), 403
        active = 1 if data.get("is_active") in (True, 1, "1") else 0
        if active == 0 and me and me["id"] == user_id:
            return jsonify({"error": "不能停用自己的账号"}), 409
        fields["is_active"] = active
    if "display_name" in data and (platform or (me and me["id"] == user_id)):
        fields["display_name"] = str(data.get("display_name") or "").strip()[:60]
    if data.get("password"):
        if not (platform or (me and me["id"] == user_id)):
            return jsonify({"error": "重置他人密码需要平台管理员"}), 403
        password = str(data.get("password"))
        if len(password) < 8:
            return jsonify({"error": "密码至少 8 位"}), 400
        fields["password_hash"] = security.hash_password(password)
    db.update_user(user_id, **fields)
    _audit("user.update", f"更新成员 {target['username']}: {', '.join(changed + list(fields))}")
    m = db.get_membership(wid, user_id) or {}
    return jsonify({"user": {**(db.get_user(user_id) or {}), "role": m.get("role")}})


@app.delete("/api/v4/users/<int:user_id>")
def v4_user_delete(user_id):
    """把成员移出当前工作区(账号本身保留, 仍可属于其他工作区)。"""
    wid, my_role = _ws_ctx()
    target = db.get_user(user_id)
    m = db.get_membership(wid, user_id) if target else None
    if m is None:
        return jsonify({"error": "用户不存在"}), 404
    me = _current_user()
    if me and me["id"] == user_id:
        return jsonify({"error": "不能移除自己; 请改用离开工作区"}), 409
    if m["role"] == "owner" and my_role != "owner":
        return jsonify({"error": "只有所有者可以移除所有者"}), 403
    if m["role"] in ("owner", "admin") and db.count_managers(wid) <= 1:
        return jsonify({"error": "不能移除最后一个管理员"}), 409
    db.remove_member(wid, user_id)
    _audit("member.remove", f"移出成员 {target['username']}")
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
        if len(password) < 8:
            return jsonify({"error": "密码至少 8 位"}), 400
        if db.get_user_by_username(username):
            return jsonify({"error": "用户名已存在; 已有账号请先登录再打开邀请链接"}), 409
        if not db.use_invite(inv["id"]):
            return jsonify({"error": "邀请链接已被使用"}), 409
        uid = db.create_user(username, security.hash_password(password), "user",
                             str(data.get("display_name") or "").strip()[:60])
        user = db.get_user(uid)
        session["uid"] = uid
        session.permanent = True
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
    return jsonify({"url": f"{request.host_url.rstrip('/')}/preview/{task_id}/?share={token}"})


# ---------------------------------------------------------------------------
# 审计日志查询(admin)
# ---------------------------------------------------------------------------
@app.get("/api/v4/audit")
def v4_audit():
    limit = request.args.get("limit", 80, type=int)
    action = request.args.get("action", "").strip()
    keyword = request.args.get("q", "").strip()
    wid = _cur_ws()
    actions = [row["action"] for row in db.list_audit(limit=500, workspace_id=wid)]
    return jsonify({"entries": db.list_audit(limit=limit, action=action, q=keyword,
                                             workspace_id=wid),
                    "actions": sorted(set(actions))})


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
    entries = db.list_audit(limit=5000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["时间", "用户", "动作", "详情", "IP"])
    for e in entries:
        writer.writerow([e.get("created_at", ""), e.get("username", ""),
                         e.get("action", ""), e.get("detail", ""), e.get("ip", "")])
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return Response("\ufeff" + buf.getvalue(),
                    mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="audit_{stamp}.csv"'})


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
# ---------------------------------------------------------------------------
from core import schedules as sched_rules  # noqa: E402


def _schedule_view(sc: dict, briefs: dict) -> dict:
    hist = json.loads(sc.get("history_json") or "[]")
    urls = json.loads(sc.get("urls_json") or "[]")
    runs = [{"batch_id": h["batch_id"], "at": h["at"], **(briefs.get(h["batch_id"]) or {})}
            for h in hist[-8:]][::-1]
    return {
        "id": sc["id"], "name": sc["name"], "urls": urls, "project_id": sc.get("project_id"),
        "config": json.loads(sc.get("config_json") or "{}"),
        "freq": sc["freq"], "hour": sc["hour"], "minute": sc["minute"], "weekday": sc["weekday"],
        "rule": sched_rules.describe(sc["freq"], sc["hour"], sc["minute"], sc["weekday"]),
        "enabled": bool(sc["enabled"]), "next_run_at": sc["next_run_at"] if sc["enabled"] else "",
        "last_run_at": sc["last_run_at"], "runs_total": sc["runs"], "runs": runs,
        "created_by": sc.get("created_by") or "", "created_at": sc["created_at"],
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


def _schedule_ticker():
    while True:
        time.sleep(20)
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
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
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
    return send_file(zip_path, as_attachment=True,
                     download_name=f"clone_{task['domain'] or 'site'}_{task_id}.zip")


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
    if str(target).startswith(str(ws.resolve())) and target.is_file():
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


@app.errorhandler(404)
def not_found(_error):
    # Referer 智能沙盒路由兜底: 预览 iframe 内逃逸到根路径的静态资源 / Mock API
    # 请求(其 Referer 必含 /preview/<id>, 与平台自身 API 的根 Referer 天然可区分)
    # 自动转发至对应工作空间。
    referer = request.headers.get("Referer", "")
    match = PREVIEW_REFERER_RE.search(referer)
    task_ref = match.group(1) if match else None
    if task_ref is None and request.method == "GET" and referer \
            and urlsplit(referer).netloc == request.host \
            and not request.path.startswith(("/api/", "/preview/")):
        # 同源、Referer 是克隆页里的子资源(样式表)而非平台页: 退到最近打开的预览任务
        cookie_id = request.cookies.get("clone_pv", "")
        if cookie_id.isdigit() and not urlsplit(referer).path.rstrip("/") in ("", "/index.html"):
            task_ref = cookie_id
    if task_ref and not request.path.startswith("/preview/"):
        task = _visible_task(int(task_ref))
        if task is not None:
            resp = _serve_from_task(task, request.path)
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


def _boot():
    db.ensure_default_project()
    _apply_saved_settings()
    scheduler.start()
    threading.Thread(target=_audit_retention_worker, daemon=True,
                     name="audit-retention").start()
    threading.Thread(target=_schedule_ticker, daemon=True, name="schedule-ticker").start()
    deployer.start_background()

    def _shutdown():
        scheduler.shutdown()
        db.close()

    import atexit
    atexit.register(_shutdown)


_boot()

if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT, threaded=True, use_reloader=False)
