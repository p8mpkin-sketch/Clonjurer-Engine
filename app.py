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
                   send_file, send_from_directory, session)

import config
from core import flask_generator, security
from core import ai_mock_generator
from core.browser_pool import SyncBrowserPool
from core.logger import LogBus
from core.scheduler import Scheduler
from models import Database, domain_of

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
ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}
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


def _audit(action: str, detail: str = "", username: str = None):
    user = _current_user() if username is None else {"username": username}
    db.log_audit((user or {}).get("username") or "anonymous",
                 action, detail, _client_ip())


@app.before_request
def _rbac_gate():
    """开放模式全放行; 有用户后: /api/* 需登录, 写操作需 operator+,
    /api/v4/users 与 /api/v4/admin/* 需 admin。页面与 /preview 免鉴权
    (克隆页沙盒 iframe 资源不应被会话拦截)。"""
    if not _auth_required():
        return None
    path = request.path
    if not path.startswith("/api/") or path.startswith("/api/auth/"):
        return None
    user = _current_user()
    if user is None:
        return jsonify({"error": "未登录或会话已过期", "auth": "required"}), 401
    rank = ROLE_RANK.get(user.get("role"), 1)
    if (path.startswith("/api/v4/users") or path.startswith("/api/v4/admin")):
        if rank < ROLE_RANK["admin"]:
            return jsonify({"error": "该操作需要管理员权限"}), 403
    elif request.method not in ("GET", "HEAD", "OPTIONS"):
        if rank < ROLE_RANK["operator"]:
            return jsonify({"error": "当前账号为只读角色(viewer)"}), 403
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


def _task_or_404(task_id: int) -> dict:
    task = db.get_task(task_id)
    if task is None:
        abort(404)
    return task


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
            node["children"] = _build_tree(entry, counter)
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
        tracker_hook=bool(bake_cfg),
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
    for entry in _read_mock_entries(ws):
        if "/" + (entry.get("path") or "").strip("/") == normalized:
            if str(entry.get("method", "GET")).upper() not in (method, "GET"):
                continue
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
        task = db.get_task(int(match.group(1)))
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
    if project_id is not None and db.get_project(project_id) is None:
        return jsonify({"error": f"项目 #{project_id} 不存在"}), 400
    first_domain = domain_of(accepted[0][0])
    name = str(data.get("name") or "").strip() or (
        f"批次 {time.strftime('%m-%d %H:%M')} · {first_domain}"
        + (f" 等 {len(accepted)} 站" if len(accepted) > 1 else "")
    )
    batch_id = db.create_batch(name, accepted, clean_config, project_id)
    scheduler.start()
    bus.publish(None, "SCHED", f"新批次 #{batch_id} 已入队: {len(accepted)} 个站点")
    _audit("batch.create", f"批次 #{batch_id}「{name}」提交 {len(accepted)} 个站点"
            + (f" 至项目 #{project_id}" if project_id else ""))
    return jsonify({"batch_id": batch_id, "name": name,
                    "accepted": len(accepted), "rejected": rejected}), 201


@app.get("/api/batches")
def list_batches():
    return jsonify({"batches": db.list_batches(50)})


@app.get("/api/tasks")
def list_tasks():
    batch_id = request.args.get("batch_id", type=int)
    project_id = request.args.get("project_id", type=int)
    status = request.args.get("status", "").strip()
    if status not in ("pending", "running", "completed", "failed"):
        status = ""
    keyword = request.args.get("q", "").strip()
    tasks = db.list_tasks(batch_id=batch_id or None, status=status or None,
                          q=keyword or None, project_id=project_id or None)
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
        task = db.get_task(task_id)
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
    return send_file(target, mimetype="image/png", max_age=0)


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
    for task in db.list_tasks(status="completed"):
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
        task = db.get_task(task_id)
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
    cfg = {"mode": "generate", "gen": spec, "similarity": True,
           "ai_api_key": str(form.get("ai_api_key") or "").strip()[:4096],
           "ai_base_url": str(form.get("ai_base_url") or "").strip()[:4096],
           "ai_model": str(form.get("ai_model") or "").strip()[:200]}
    project_id = form.get("project_id")
    project_id = int(project_id) if str(project_id or "").isdigit() else None
    if project_id is not None and db.get_project(project_id) is None:
        return jsonify({"error": f"项目 #{project_id} 不存在"}), 400
    name = str(form.get("name") or "").strip() or f"生成 · {label}"
    target_url = f"{gen_settings.TARGET_URL_PREFIX}{kind}:{label}"
    batch_id = db.create_batch(name, [(target_url, "generated")], cfg, project_id)
    task = db.list_tasks(batch_id=batch_id, limit=1)
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


@app.get("/api/v4/generate/<int:task_id>/artifact/<int:idx>")
def api_v4_generate_artifact(task_id, idx):
    """下载生成任务构建出的安装包(APK / macOS .app zip / Windows .exe zip), 只放行导出目录内、报告登记过的文件。"""
    task = _task_or_404(task_id)
    rep = _workspace_of(task) / "generate_report.json"
    if not rep.is_file():
        return jsonify({"error": "生成报告尚未产出"}), 404
    arts = ((_parse_json(rep.read_text(encoding="utf-8")) or {}).get("build") or {}).get("artifacts") or []
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
@app.post("/api/v4/ai/playground-repair")
def v4_ai_playground_repair():
    """AI 实验室一键修复: 复用任务级视觉闭环, 支持附加用户 Prompt 提示。"""
    data = request.get_json(silent=True) or {}
    try:
        task_id = int(data.get("task_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "缺少 task_id"}), 400
    task = db.get_task(task_id)
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
                    lambda stage, msg: bus.publish(task_id, stage, msg),
                    engine._make_render_and_score(ctx),
                    page_file=engine._main_page_file(),
                    region_cells=engine._region_cells())
            final = report.get("final_score")
            if final is not None:
                db.update_task(task_id, similarity_score=final)
            _regenerate_project(db.get_task(task_id), ws)
        except Exception as exc:  # noqa: BLE001 - 后台线程兜底
            bus.publish(task_id, "AI-VISION",
                        f"AI 实验室修复异常: {type(exc).__name__}: {exc}", "ERROR")
            db.update_task(task_id, stage="AI修复失败")

    threading.Thread(target=_run, name=f"ai-play-{task_id}", daemon=True).start()
    _audit("ai.playground", f"AI 实验室修复任务 #{task_id}")
    return jsonify({"ok": True, "poll": f"/api/tasks/{task_id}/ai_report"})


# ---------------------------------------------------------------------------
# 认证: 状态 / 初始化引导 / 登录 / 登出
# ---------------------------------------------------------------------------
@app.get("/api/auth/state")
def auth_state():
    user = _current_user()
    return jsonify({"required": _auth_required(),
                    "open_mode": not _auth_required(),
                    "user": _public_user(user)})


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
    session["uid"] = uid
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
    session.permanent = True
    db.touch_login(user["id"])
    _audit("login", "登录成功", username=username)
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
    return jsonify({"projects": db.list_projects(),
                    "default_project_id": db.ensure_default_project()})


@app.post("/api/v4/projects")
def v4_project_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not (2 <= len(name) <= 60):
        return jsonify({"error": "项目名称需 2~60 个字符"}), 400
    color = str(data.get("color") or "#6366f1").strip()
    if not re.match(r"^#[0-9A-Fa-f]{6}$", color):
        color = "#6366f1"
    pid = db.create_project(name, str(data.get("description") or "").strip()[:500], color)
    _audit("project.create", f"创建项目 #{pid}「{name}」")
    return jsonify({"project": db.get_project(pid)}), 201


@app.patch("/api/v4/projects/<int:project_id>")
def v4_project_update(project_id):
    if db.get_project(project_id) is None:
        return jsonify({"error": "项目不存在"}), 404
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
    if db.get_project(project_id) is None:
        return jsonify({"error": "项目不存在"}), 404
    task_ids = db.project_task_ids(project_id)
    if task_ids:  # 级联: 先释放容器与工作空间, 再删数据
        for tid in task_ids:
            task = db.get_task(tid)
            if task is None:
                continue
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
    projects = [p for p in db.list_projects()
                if not q or q in p["name"].lower()
                or q in (p.get("description") or "").lower()]
    return jsonify({"projects": projects})


@app.get("/api/v4/projects/<int:project_id>/export")
def v4_project_export(project_id):
    if db.get_project(project_id) is None:
        return jsonify({"error": "项目不存在"}), 404
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
# 用户管理(admin)
# ---------------------------------------------------------------------------
@app.get("/api/v4/users")
def v4_users():
    return jsonify({"users": db.list_users()})


@app.post("/api/v4/users")
def v4_user_create():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    role = data.get("role") if data.get("role") in ROLE_RANK else "viewer"
    if not _USERNAME_RE.match(username):
        return jsonify({"error": "用户名需 2~32 位字母/数字/._-"}), 400
    if len(password) < 8:
        return jsonify({"error": "密码至少 8 位"}), 400
    if db.get_user_by_username(username):
        return jsonify({"error": "用户名已存在"}), 409
    uid = db.create_user(username, security.hash_password(password), role,
                         str(data.get("display_name") or "").strip()[:60])
    _audit("user.create", f"创建用户 {username}({role})")
    return jsonify({"user": db.get_user(uid)}), 201


@app.patch("/api/v4/users/<int:user_id>")
def v4_user_update(user_id):
    target = db.get_user(user_id)
    if target is None:
        return jsonify({"error": "用户不存在"}), 404
    me = _current_user()
    data = request.get_json(silent=True) or {}
    fields = {}
    if "role" in data:
        role = data.get("role")
        if role not in ROLE_RANK:
            return jsonify({"error": "非法角色"}), 400
        if target["role"] == "admin" and role != "admin" and db.count_admins() <= 1:
            return jsonify({"error": "不能降级最后一个管理员"}), 409
        fields["role"] = role
    if "is_active" in data:
        active = 1 if data.get("is_active") in (True, 1, "1") else 0
        if active == 0:
            if me and me["id"] == user_id:
                return jsonify({"error": "不能停用自己的账号"}), 409
            if target["role"] == "admin" and db.count_admins() <= 1:
                return jsonify({"error": "不能停用最后一个管理员"}), 409
        fields["is_active"] = active
    if "display_name" in data:
        fields["display_name"] = str(data.get("display_name") or "").strip()[:60]
    if data.get("password"):
        password = str(data.get("password"))
        if len(password) < 8:
            return jsonify({"error": "密码至少 8 位"}), 400
        fields["password_hash"] = security.hash_password(password)
    db.update_user(user_id, **fields)
    _audit("user.update", f"更新用户 {target['username']}: {', '.join(fields)}")
    return jsonify({"user": db.get_user(user_id)})


@app.delete("/api/v4/users/<int:user_id>")
def v4_user_delete(user_id):
    target = db.get_user(user_id)
    if target is None:
        return jsonify({"error": "用户不存在"}), 404
    me = _current_user()
    if me and me["id"] == user_id:
        return jsonify({"error": "不能删除自己的账号"}), 409
    if target["role"] == "admin" and db.count_admins() <= 1:
        return jsonify({"error": "不能删除最后一个管理员"}), 409
    db.update_user(user_id, is_active=0,
                   username=f"{target['username']}#deleted{user_id}")
    _audit("user.delete", f"删除用户 {target['username']}")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 审计日志查询(admin)
# ---------------------------------------------------------------------------
@app.get("/api/v4/audit")
def v4_audit():
    limit = request.args.get("limit", 80, type=int)
    action = request.args.get("action", "").strip()
    keyword = request.args.get("q", "").strip()
    actions = [row["action"] for row in db.list_audit(limit=500)]
    return jsonify({"entries": db.list_audit(limit=limit, action=action, q=keyword),
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
    for task in db.list_tasks(status=None):
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
        "queue": db.queue_counts(),
        "batches": db.list_batches(12),
        "scheduler": {"paused": scheduler.paused()},
    })


@app.get("/api/stream_logs")
def stream_logs():
    task_id = request.args.get("task_id", type=int)
    return Response(
        bus.stream(task_id),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


@app.get("/api/download/<int:task_id>")
def download_zip(task_id):
    task = _task_or_404(task_id)
    zip_path = Path(task["zip_path"]) if task.get("zip_path") else Path()
    if not zip_path.is_file():
        return jsonify({"error": "ZIP 尚未生成"}), 404
    return send_file(zip_path, as_attachment=True,
                     download_name=f"clone_{task['domain'] or 'site'}_{task_id}.zip")


@app.get("/api/export_all")
def export_all():
    completed = db.list_tasks(status="completed")
    zips = [t for t in completed
            if t.get("zip_path") and Path(t["zip_path"]).is_file()]
    if not zips:
        return jsonify({"error": "暂无已完成的 ZIP 可导出"}), 404
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
        return _pv_cookie(send_from_directory(ws, routes.get("/", "index.html")), task_id)
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
        task = db.get_task(int(task_ref))
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


def _boot():
    db.ensure_default_project()
    _apply_saved_settings()
    scheduler.start()
    threading.Thread(target=_audit_retention_worker, daemon=True,
                     name="audit-retention").start()

    def _shutdown():
        scheduler.shutdown()
        db.close()

    import atexit
    atexit.register(_shutdown)


_boot()

if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT, threaded=True, use_reloader=False)
