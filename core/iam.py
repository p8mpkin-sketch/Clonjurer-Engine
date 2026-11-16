"""用户管理与日志审计的服务层(SaaS 式 IAM 模块)。

和 Flask 路由解耦, 只依赖 models.Database:
  - 审计动作目录: 动作码 -> 中文名 / 分类 / 是否敏感 / 是否失败, 界面、筛选与 CSV 导出共用一份
  - 审计查询: 多条件筛选 + 分页 + 统计(按天走势、分类占比、活跃成员、失败登录、来源 IP)
  - 登录防暴力破解: 账号级失败计数与临时锁定
  - 成员画像: 某成员在本工作区的最近操作与登录记录

角色模型沿用平台既有的两层: 账号级 role(admin = 平台管理员, user = 普通账号)
+ 工作区内角色 owner / admin / operator / viewer, 这里不另起一套。
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# 审计动作目录
# ---------------------------------------------------------------------------
CATEGORIES = {
    "auth": "登录与账号",
    "member": "成员与权限",
    "workspace": "工作区",
    "task": "克隆与任务",
    "deploy": "部署与发布",
    "ai": "AI 与生成",
    "links": "短链",
    "schedule": "定时任务",
    "data": "导入导出",
    "system": "系统设置",
    "other": "其他",
}

# 动作码 -> (中文名, 分类, 敏感)。敏感 = 权限/凭据/数据外流/大范围删除, 统计里单独计数并在界面高亮
ACTIONS: dict[str, tuple[str, str, bool]] = {
    "login": ("登录成功", "auth", False),
    "login.failed": ("登录失败", "auth", True),
    "login.blocked": ("停用账号尝试登录", "auth", True),
    "login.locked": ("账号被临时锁定", "auth", True),
    "logout": ("退出登录", "auth", False),
    "user.bootstrap": ("初始化管理员", "auth", True),
    "user.create": ("创建账号", "member", True),
    "user.update": ("修改成员资料", "member", False),
    "user.password": ("重置密码", "member", True),
    "user.disable": ("停用账号", "member", True),
    "user.enable": ("启用账号", "member", True),
    "user.unlock": ("解除登录锁定", "member", True),
    "user.force_logout": ("强制下线", "member", True),
    "member.add": ("加入成员", "member", True),
    "member.role": ("变更角色", "member", True),
    "member.remove": ("移出成员", "member", True),
    "member.join": ("通过邀请加入", "member", False),
    "member.leave": ("离开工作区", "member", False),
    "member.bulk": ("批量操作成员", "member", True),
    "invite.create": ("生成邀请链接", "member", True),
    "invite.revoke": ("撤销邀请", "member", False),
    "workspace.create": ("新建工作区", "workspace", False),
    "workspace.update": ("修改工作区", "workspace", False),
    "workspace.plan": ("变更套餐", "workspace", True),
    "project.create": ("新建项目", "task", False),
    "project.update": ("修改项目", "task", False),
    "project.delete": ("删除项目", "task", True),
    "project.export": ("导出项目", "data", True),
    "batch.create": ("提交克隆批次", "task", False),
    "task.rebuild": ("重建任务", "task", False),
    "task.batch_rebuild": ("批量重建", "task", False),
    "task.patch": ("注入热补丁", "task", False),
    "task.share": ("生成分享链接", "data", True),
    "task.delete": ("删除任务", "task", True),
    "task.batch_delete": ("批量删除任务", "task", True),
    "task.update": ("编辑任务", "task", False),
    "task.batch_move": ("批量移动任务", "task", False),
    "mock.delete": ("删除 Mock 规则", "task", False),
    "links.task_update": ("编辑短链任务", "links", False),
    "mock.save": ("保存 Mock 数据", "task", False),
    "generate.create": ("界面生成", "ai", False),
    "ai.repair": ("视觉修复", "ai", False),
    "ai.batch_repair": ("批量视觉修复", "ai", False),
    "ai.playground": ("AI 实验室", "ai", False),
    "ai.test": ("模型连通测试", "system", False),
    "llm.budget": ("模型消耗预算提醒", "ai", True),
    "llm.budget_update": ("修改月度模型预算", "ai", True),
    "llm.pricing_update": ("修改模型单价表", "system", True),
    "archive.export": ("打包存档", "data", True),
    "archive.import": ("导入存档", "data", False),
    "audit.export": ("导出审计日志", "data", True),
    "audit.purge": ("清理审计日志", "system", True),
    "settings.update": ("修改系统设置", "system", True),
    "settings.reset": ("重置系统设置", "system", True),
    "storage.prune_exports": ("清理导出包", "system", False),
    "scheduler.pause": ("暂停调度器", "system", False),
    "scheduler.resume": ("恢复调度器", "system", False),
    "pool.restart": ("重启浏览器池", "system", False),
    "cache.clear": ("清空资源缓存", "system", True),
    "schedule.create": ("新建定时任务", "schedule", False),
    "schedule.update": ("修改定时任务", "schedule", False),
    "schedule.delete": ("删除定时任务", "schedule", False),
    "schedule.run": ("手动运行定时任务", "schedule", False),
    "schedule.notify_test": ("测试定时任务通知", "schedule", False),
    "links.task_create": ("新建短链任务", "links", False),
    "links.generate": ("生成短链", "links", False),
    "links.task_delete": ("删除短链任务", "links", False),
    "links.create": ("单独生成短链", "links", False),
    "links.delete": ("删除短链", "links", False),
}

# 动作码前缀 -> 分类(目录里没登记的新动作也能归类)
_PREFIX_CATEGORY = {
    "login": "auth", "logout": "auth", "user": "member", "member": "member", "invite": "member",
    "workspace": "workspace", "project": "task", "batch": "task", "task": "task", "mock": "task",
    "generate": "ai", "ai": "ai", "llm": "ai", "archive": "data", "audit": "data", "settings": "system",
    "storage": "system", "scheduler": "system", "pool": "system", "cache": "system",
    "schedule": "schedule", "links": "links", "docker": "deploy", "remote": "deploy",
}

_DEPLOY_LABELS = {
    "docker.deploy": "本机部署", "docker.batch": "批量部署操作", "docker.start": "启动部署",
    "docker.stop": "停止部署", "docker.remove": "删除部署", "docker.restart": "重启部署",
    "remote.create": "新建远程发布", "remote.update": "修改远程发布", "remote.delete": "删除远程发布",
    "remote.deploy": "远程发布", "remote.start": "启动远程服务", "remote.stop": "停止远程服务",
    "remote.restart": "重启远程服务",
}

FAIL_ACTIONS = frozenset({"login.failed", "login.blocked", "login.locked"})


def category_of(action: str) -> str:
    if action in ACTIONS:
        return ACTIONS[action][1]
    return _PREFIX_CATEGORY.get((action or "").split(".", 1)[0], "other")


def describe(action: str) -> dict:
    """动作码 -> {label, category, category_label, sensitive, failed}。"""
    if action in ACTIONS:
        label, cat, sensitive = ACTIONS[action]
    else:
        cat = category_of(action)
        label = _DEPLOY_LABELS.get(action) or action
        sensitive = action in ("remote.delete", "docker.remove")
    return {"label": label, "category": cat, "category_label": CATEGORIES.get(cat, "其他"),
            "sensitive": bool(sensitive), "failed": action in FAIL_ACTIONS}


def decorate(entry: dict) -> dict:
    return {**entry, **describe(entry.get("action") or "")}


def catalog(known: tuple | list = ()) -> dict:
    """给前端的筛选目录: 分类 + 动作(含中文名); known = 库里实际出现过的动作码。"""
    codes = list(ACTIONS) + [a for a in _DEPLOY_LABELS if a not in ACTIONS]
    codes += [a for a in known if a not in codes]
    return {"categories": [{"id": k, "label": v} for k, v in CATEGORIES.items()],
            "actions": [{"action": a, **describe(a)} for a in codes]}


# ---------------------------------------------------------------------------
# 查询条件
# ---------------------------------------------------------------------------
def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _is_date(v: str) -> bool:
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def parse_filters(args, known_actions: list) -> dict:
    """请求参数 -> Database.query_audit 的关键字参数。
    支持: q / action / category / username / ip / result(ok|fail) / sensitive(1) /
    range(24h|7d|30d|90d) 或 since/until(YYYY-MM-DD)。
    动作类条件(action/category/sensitive/result)取交集; 交集为空返回一个不可能命中的动作码。"""
    def s(key):
        return str(args.get(key) or "").strip()

    universe = set(ACTIONS) | set(_DEPLOY_LABELS) | set(known_actions)
    pick = None  # None = 不限动作
    action, category = s("action"), s("category")
    if action:
        pick = {action}
    if category in CATEGORIES:
        cat_set = {a for a in universe if category_of(a) == category}
        pick = cat_set if pick is None else pick & cat_set
    if s("sensitive") in ("1", "true"):
        sens = {a for a in universe if describe(a)["sensitive"]}
        pick = sens if pick is None else pick & sens
    result = s("result")
    if result == "fail":
        pick = set(FAIL_ACTIONS) if pick is None else pick & FAIL_ACTIONS
    f: dict = {"q": s("q")[:120], "username": s("username")[:64], "ip": s("ip")[:64],
               "actions": None if pick is None else (sorted(pick) or ["__none__"]),
               "exclude_actions": sorted(FAIL_ACTIONS) if result == "ok" else [],
               "since": "", "until": ""}
    days = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}.get(s("range"))
    if days:
        f["since"] = _fmt(_utc_now() - timedelta(days=days))
    else:
        if _is_date(s("since")):
            f["since"] = s("since") + " 00:00:00"
        if _is_date(s("until")):
            f["until"] = s("until") + " 23:59:59"
    return f


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def stats(db, workspace_id: int, days: int = 7) -> dict:
    """近 N 天(含今天, UTC)的审计概览: 总量、按天走势、分类占比、活跃成员、失败登录、敏感操作、来源 IP 数。"""
    days = max(1, min(90, int(days or 7)))
    now = _utc_now()
    start_day = (now - timedelta(days=days - 1)).date()
    rows = db.audit_rows_since(workspace_id, start_day.strftime("%Y-%m-%d 00:00:00"))
    by_day = {(start_day + timedelta(days=i)).isoformat(): 0 for i in range(days)}
    by_cat: dict = {}
    users: dict = {}
    ips = set()
    failed = sensitive = failed_24h = 0
    cut_24h = _fmt(now - timedelta(hours=24))
    for r in rows:
        d = describe(r["action"])
        day = (r["created_at"] or "")[:10]
        if day in by_day:
            by_day[day] += 1
        by_cat[d["category"]] = by_cat.get(d["category"], 0) + 1
        if r["username"] and r["username"] not in ("anonymous", "?") and not d["failed"]:
            users[r["username"]] = users.get(r["username"], 0) + 1
        if r["ip"]:
            ips.add(r["ip"])
        if d["failed"]:
            failed += 1
            if (r["created_at"] or "") >= cut_24h:
                failed_24h += 1
        if d["sensitive"]:
            sensitive += 1
    cats = sorted(by_cat.items(), key=lambda kv: -kv[1])
    top = sorted(users.items(), key=lambda kv: -kv[1])[:5]
    return {
        "days": days, "total": len(rows),
        "by_day": [{"day": k, "count": v} for k, v in by_day.items()],
        "by_category": [{"id": k, "label": CATEGORIES.get(k, "其他"), "count": v} for k, v in cats],
        "top_users": [{"username": k, "count": v} for k, v in top],
        "active_users": len(users), "ip_count": len(ips),
        "failed": failed, "failed_24h": failed_24h, "sensitive": sensitive,
    }


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def _csv_safe(v) -> str:
    """防 CSV 公式注入: 以 = + - @ 等开头的单元格前加单引号。"""
    s = str(v or "")
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def audit_csv(entries: list) -> str:
    """带 BOM 的 CSV(Excel 直接打开不乱码)。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["编号", "时间(UTC)", "用户", "分类", "动作", "动作码", "结果", "敏感", "详情", "来源 IP"])
    for e in entries:
        d = describe(e.get("action") or "")
        w.writerow([e.get("id", ""), e.get("created_at", ""), _csv_safe(e.get("username", "")),
                    d["category_label"], d["label"], e.get("action", ""),
                    "失败" if d["failed"] else "成功", "是" if d["sensitive"] else "",
                    _csv_safe(e.get("detail", "")), e.get("ip", "")])
    return "﻿" + buf.getvalue()


def members_csv(members: list, role_text) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["用户名", "显示名", "角色", "状态", "加入时间(UTC)", "最近登录(UTC)", "近 30 天操作数"])
    for m in members:
        w.writerow([m.get("username", ""), _csv_safe(m.get("display_name", "")),
                    role_text(m.get("role")), status_text(m),
                    m.get("joined_at") or m.get("created_at") or "", m.get("last_login_at") or "",
                    m.get("actions_30d", 0)])
    return "﻿" + buf.getvalue()


# ---------------------------------------------------------------------------
# 账号状态与登录锁定
# ---------------------------------------------------------------------------
def locked_until(user: dict) -> str:
    """仍在锁定期则返回解锁时间(UTC 字符串), 否则空串。"""
    lu = (user or {}).get("locked_until") or ""
    return lu if lu and lu > _fmt(_utc_now()) else ""


def status_of(user: dict) -> str:
    if not user.get("is_active"):
        return "disabled"
    if locked_until(user):
        return "locked"
    return "active"


def status_text(user: dict) -> str:
    return {"disabled": "已停用", "locked": "登录锁定中", "active": "正常"}[status_of(user)]


def register_login_failure(db, user: dict, window_min: int, max_fail: int, lock_min: int) -> bool:
    """记一次密码错误: 距上次失败超过窗口则从 1 重新计数; 达上限即锁定, 返回是否刚被锁。
    成功登录与管理员解锁会把计数清零(Database.reset_login_failures)。"""
    if not user or max_fail <= 0:
        return False
    now = _utc_now()
    last = user.get("last_failed_at") or ""
    fresh = not last or last < _fmt(now - timedelta(minutes=window_min))
    count = 1 if fresh else int(user.get("failed_logins") or 0) + 1
    lock = _fmt(now + timedelta(minutes=lock_min)) if count >= max_fail else ""
    db.record_login_failure(user["id"], count, _fmt(now), lock)
    return bool(lock)
