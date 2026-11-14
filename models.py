"""
SQLite(WAL 高并发模式)数据层。

管理:
  - batch_jobs  : 批量任务批次
  - clone_tasks : 单站点克隆任务(状态机 pending -> running -> completed/failed)
  - cas_assets  : 全局 CAS 内容寻址资源池登记表(用于命中率统计)
  - cas_counters: CAS 命中/未命中计数器

线程安全策略: 单连接 + RLock 串行化(Flask 请求线程与调度器线程共用)。
"""
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def domain_of(url: str) -> str:
    try:
        return (urlsplit(url).netloc or "").lower()
    except ValueError:
        return ""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    color       TEXT    NOT NULL DEFAULT '#6366f1',
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER NOT NULL DEFAULT 1,
    name           TEXT    NOT NULL DEFAULT '',
    total_urls     INTEGER NOT NULL DEFAULT 0,
    completed_urls INTEGER NOT NULL DEFAULT 0,
    config_json    TEXT    NOT NULL DEFAULT '{}',
    created_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS clone_tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         INTEGER NOT NULL,
    target_url       TEXT    NOT NULL,
    domain           TEXT    NOT NULL DEFAULT '',
    status           TEXT    NOT NULL DEFAULT 'pending',
    progress         INTEGER NOT NULL DEFAULT 0,
    stage            TEXT    NOT NULL DEFAULT '',
    similarity_score REAL,
    stats_json       TEXT    NOT NULL DEFAULT '{}',
    error_msg        TEXT    NOT NULL DEFAULT '',
    workspace_path   TEXT    NOT NULL DEFAULT '',
    zip_path         TEXT    NOT NULL DEFAULT '',
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON clone_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_batch  ON clone_tasks(batch_id);

CREATE TABLE IF NOT EXISTS short_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER,
    link_task_id INTEGER,
    target_url  TEXT    NOT NULL,
    service     TEXT    NOT NULL DEFAULT '',
    short_url   TEXT    NOT NULL,
    label       TEXT    NOT NULL DEFAULT '',
    extras_json TEXT    NOT NULL DEFAULT '{}',
    clicks      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_short_links_task ON short_links(task_id);

CREATE TABLE IF NOT EXISTS remote_deploys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    seq           INTEGER NOT NULL,
    name          TEXT    NOT NULL,
    host          TEXT    NOT NULL,
    ssh_port      INTEGER NOT NULL DEFAULT 22,
    username      TEXT    NOT NULL DEFAULT 'root',
    password_enc  TEXT    NOT NULL DEFAULT '',
    clone_task_id INTEGER,
    remote_port   INTEGER NOT NULL DEFAULT 8000,
    note          TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'idle',   -- idle/deploying/running/stopped/failed
    progress      INTEGER NOT NULL DEFAULT 0,
    stage         TEXT    NOT NULL DEFAULT '',
    log           TEXT    NOT NULL DEFAULT '',
    host_key      TEXT    NOT NULL DEFAULT '',
    service_url   TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_remote_deploys_seq ON remote_deploys(seq);

CREATE TABLE IF NOT EXISTS tracker_configs (
    task_id    INTEGER PRIMARY KEY,
    enabled    INTEGER NOT NULL DEFAULT 0,
    token      TEXT    NOT NULL DEFAULT '',
    secret_enc TEXT    NOT NULL DEFAULT '',
    keyword    TEXT    NOT NULL DEFAULT '',
    slots_json TEXT    NOT NULL DEFAULT '[]',
    mask       INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS short_link_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    seq          INTEGER NOT NULL,            -- 展示序号 SL-<seq>
    name         TEXT    NOT NULL,
    target_url   TEXT    NOT NULL DEFAULT '',
    service      TEXT    NOT NULL DEFAULT 'all',
    clone_task_id INTEGER,
    status       TEXT    NOT NULL DEFAULT 'pending',  -- pending/partial/done/failed
    note         TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_short_link_tasks_seq ON short_link_tasks(seq);

CREATE TABLE IF NOT EXISTS cas_assets (
    sha        TEXT PRIMARY KEY,
    size       INTEGER NOT NULL DEFAULT 0,
    ext        TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);

-- 全局资产名镜像索引: 原站构建产物文件名(name.hash8.ext) -> CAS sha。
-- 同一 SPA 骨架常跨子域复用同名 chunk; 源站失效(302/下线)后,
-- 回补仍可凭名字从 CAS 取同字节。
CREATE TABLE IF NOT EXISTS asset_names (
    name       TEXT PRIMARY KEY,
    sha        TEXT    NOT NULL,
    size       INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS cas_counters (
    key  TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS workspaces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    plan        TEXT    NOT NULL DEFAULT 'free',
    created_by  INTEGER,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    workspace_id INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    role         TEXT    NOT NULL DEFAULT 'viewer',
    invited_by   INTEGER,
    joined_at    TEXT    NOT NULL,
    PRIMARY KEY (workspace_id, user_id)
);

CREATE TABLE IF NOT EXISTS invites (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    role         TEXT    NOT NULL DEFAULT 'operator',
    token_hash   TEXT    NOT NULL UNIQUE,
    created_by   INTEGER,
    expires_at   TEXT    NOT NULL,
    max_uses     INTEGER NOT NULL DEFAULT 1,
    uses         INTEGER NOT NULL DEFAULT 0,
    revoked      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id  INTEGER NOT NULL,
    project_id    INTEGER,
    name          TEXT    NOT NULL,
    urls_json     TEXT    NOT NULL DEFAULT '[]',
    config_json   TEXT    NOT NULL DEFAULT '{}',
    freq          TEXT    NOT NULL DEFAULT 'daily',
    hour          INTEGER NOT NULL DEFAULT 9,
    minute        INTEGER NOT NULL DEFAULT 0,
    weekday       INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    next_run_at   TEXT    NOT NULL DEFAULT '',
    last_run_at   TEXT    NOT NULL DEFAULT '',
    history_json  TEXT    NOT NULL DEFAULT '[]',
    runs          INTEGER NOT NULL DEFAULT 0,
    created_by    TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    kind         TEXT    NOT NULL,
    amount       INTEGER NOT NULL DEFAULT 1,
    task_id      INTEGER,
    at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_ws_time ON usage_events(workspace_id, kind, at);

CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT    NOT NULL UNIQUE,
    password_hash  TEXT    NOT NULL,
    display_name   TEXT    NOT NULL DEFAULT '',
    role           TEXT    NOT NULL DEFAULT 'viewer',
    is_active      INTEGER NOT NULL DEFAULT 1,
    last_login_at  TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT    NOT NULL DEFAULT '',
    action     TEXT    NOT NULL,
    detail     TEXT    NOT NULL DEFAULT '',
    ip         TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_logs(id DESC);

CREATE TABLE IF NOT EXISTS system_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS archive_imports (
    workspace_id INTEGER NOT NULL,
    uid          TEXT    NOT NULL,
    task_id      INTEGER NOT NULL,
    created_at   TEXT    NOT NULL,
    PRIMARY KEY (workspace_id, uid)
);

CREATE TABLE IF NOT EXISTS docker_containers (
    task_id      INTEGER PRIMARY KEY,
    container_id TEXT    NOT NULL DEFAULT '',
    image_tag    TEXT    NOT NULL DEFAULT '',
    port         INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'created',
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);
"""

_TASK_COLUMNS = {
    "batch_id", "target_url", "domain", "status", "progress", "stage",
    "similarity_score", "stats_json", "error_msg", "workspace_path",
    "zip_path", "created_at", "updated_at",
}


ALL_WORKSPACES = object()  # 仅平台级维护(存储清理/全量导出)可用的"不限工作区"哨兵


class Database:
    """全平台共享的单一数据库门面。"""

    def __init__(self, db_path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self):
        """存量库平滑升级: 缺列补列(V1 库的 batch_jobs 无 project_id 等)。"""
        cols = {r["name"] for r in
                self._conn.execute("PRAGMA table_info(batch_jobs)").fetchall()}
        if "project_id" not in cols:
            self._conn.execute(
                "ALTER TABLE batch_jobs ADD COLUMN project_id INTEGER NOT NULL DEFAULT 1")
        # 多用户: 记录项目负责人 / 批次提交人(存用户名; 开放模式为空)
        if "created_by" not in cols:
            self._conn.execute(
                "ALTER TABLE batch_jobs ADD COLUMN created_by TEXT NOT NULL DEFAULT ''")
        pj_cols = {r["name"] for r in
                   self._conn.execute("PRAGMA table_info(projects)").fetchall()}
        if pj_cols and "created_by" not in pj_cols:
            self._conn.execute(
                "ALTER TABLE projects ADD COLUMN created_by TEXT NOT NULL DEFAULT ''")
        # 多租户: 项目 / 批次 / 审计归属工作区(任务经批次归属, 部署经任务归属)
        for table in ("projects", "batch_jobs", "audit_logs"):
            tcols = {r["name"] for r in
                     self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if tcols and "workspace_id" not in tcols:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN workspace_id INTEGER NOT NULL DEFAULT 1")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_ws ON projects(workspace_id)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_batches_ws ON batch_jobs(workspace_id)")
        if self._conn.execute("SELECT COUNT(*) AS c FROM workspaces").fetchone()["c"] == 0:
            # 存量库: 全部老数据归入默认工作区 #1, 已有用户按原角色成为成员(首个管理员为所有者)
            row = self._conn.execute(
                "SELECT value FROM system_settings WHERE key = 'workspace_name'").fetchone()
            self._conn.execute(
                "INSERT INTO workspaces (id, slug, name, plan, created_at)"
                " VALUES (1, 'default', ?, 'self', ?)",
                ((row["value"] if row and row["value"] else "我的团队"), _now()))
            owner_done = False
            for u in self._conn.execute(
                    "SELECT id, role FROM users ORDER BY id").fetchall():
                role = u["role"] if u["role"] in ("admin", "operator", "viewer") else "viewer"
                if role == "admin" and not owner_done:
                    role, owner_done = "owner", True
                self._conn.execute(
                    "INSERT OR IGNORE INTO memberships (workspace_id, user_id, role, joined_at)"
                    " VALUES (1, ?, ?, ?)", (u["id"], role, _now()))
        sl_cols = {r["name"] for r in
                   self._conn.execute("PRAGMA table_info(short_links)").fetchall()}
        if sl_cols and "link_task_id" not in sl_cols:
            self._conn.execute("ALTER TABLE short_links ADD COLUMN link_task_id INTEGER")
        # 部署(Docker)扩展列: 启动方式 / 绑定地址 / 自动销毁时间 / 内存上限 / 最近错误
        dk_cols = {r["name"] for r in
                   self._conn.execute("PRAGMA table_info(docker_containers)").fetchall()}
        for col, ddl in (("mode", "TEXT NOT NULL DEFAULT 'fast'"),
                         ("bind", "TEXT NOT NULL DEFAULT '127.0.0.1'"),
                         ("expires_at", "TEXT NOT NULL DEFAULT ''"),
                         ("mem_limit", "TEXT NOT NULL DEFAULT '512m'"),
                         ("last_error", "TEXT NOT NULL DEFAULT ''")):
            if dk_cols and col not in dk_cols:
                self._conn.execute(f"ALTER TABLE docker_containers ADD COLUMN {col} {ddl}")

    # ------------------------------------------------------------------
    # 批次
    # ------------------------------------------------------------------
    def create_batch(self, name: str, items, config: dict,
                     project_id: int = None, created_by: str = "", *,
                     workspace_id: int) -> int:
        """items: 由 (target_url, domain) 元组组成的列表 -> 新批次 id。
        批次归属 = 所属项目的工作区; 项目不属于该工作区时落到该工作区的默认项目。"""
        now = _now()
        proj = self.get_project(project_id) if project_id else None
        if proj is None or proj.get("workspace_id") != workspace_id:
            project_id = self.ensure_default_project(workspace_id)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO batch_jobs (project_id, name, total_urls, completed_urls,"
                " config_json, created_at, created_by, workspace_id) VALUES (?, ?, ?, 0, ?, ?, ?, ?)",
                (project_id, name, len(items),
                 json.dumps(config, ensure_ascii=False), now, created_by or "", workspace_id),
            )
            batch_id = cur.lastrowid
            self._conn.executemany(
                "INSERT INTO clone_tasks (batch_id, target_url, domain, status, created_at, updated_at)"
                " VALUES (?, ?, ?, 'pending', ?, ?)",
                [(batch_id, url, dom, now, now) for url, dom in items],
            )
            self._conn.commit()
            return batch_id

    def get_batch(self, batch_id: int):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM batch_jobs WHERE id = ?", (batch_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_batches(self, limit: int = 30, *, workspace_id: int):
        with self._lock:
            rows = self._conn.execute(
                "SELECT b.*,"
                " (SELECT COUNT(*) FROM clone_tasks t WHERE t.batch_id = b.id AND t.status = 'completed') AS ok_cnt,"
                " (SELECT COUNT(*) FROM clone_tasks t WHERE t.batch_id = b.id AND t.status = 'failed')  AS fail_cnt"
                " FROM batch_jobs b WHERE b.workspace_id = ? ORDER BY b.id DESC LIMIT ?",
                (workspace_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def increment_completed(self, batch_id: int):
        with self._lock:
            self._conn.execute(
                "UPDATE batch_jobs SET completed_urls = completed_urls + 1 WHERE id = ?",
                (batch_id,),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------
    def create_tasks(self, batch_id: int, items):
        now = _now()
        with self._lock:
            self._conn.executemany(
                "INSERT INTO clone_tasks (batch_id, target_url, domain, status, created_at, updated_at)"
                " VALUES (?, ?, ?, 'pending', ?, ?)",
                [(batch_id, url, dom, now, now) for url, dom in items],
            )
            self._conn.commit()

    def claim_next_task(self, concurrency=None):
        """原子领取一个 pending 任务(置为 running), 无任务返回 None。

        多租户公平调度: 各工作区取最早的 pending, 优先给当前运行数最少的区;
        concurrency(plan) -> 该套餐同时运行上限(None 不限), 到顶的区本轮跳过。"""
        with self._lock:
            pend = self._conn.execute(
                "SELECT MIN(t.id) AS id, b.workspace_id AS w, w.plan AS plan FROM clone_tasks t"
                " JOIN batch_jobs b ON t.batch_id = b.id"
                " LEFT JOIN workspaces w ON w.id = b.workspace_id"
                " WHERE t.status = 'pending' GROUP BY b.workspace_id").fetchall()
            if not pend:
                return None
            running = {r["w"]: r["c"] for r in self._conn.execute(
                "SELECT b.workspace_id AS w, COUNT(*) AS c FROM clone_tasks t"
                " JOIN batch_jobs b ON t.batch_id = b.id WHERE t.status = 'running'"
                " GROUP BY b.workspace_id").fetchall()}
            cands = []
            for r in pend:
                cap = concurrency(r["plan"] or "self") if concurrency else None
                if cap is not None and running.get(r["w"], 0) >= cap:
                    continue
                cands.append((running.get(r["w"], 0), r["id"]))
            if not cands:
                return None
            row = {"id": min(cands)[1]}
            cur = self._conn.execute(
                "UPDATE clone_tasks SET status = 'running', stage = '排队调度',"
                " progress = 2, updated_at = ? WHERE id = ? AND status = 'pending'",
                (_now(), row["id"]),
            )
            self._conn.commit()
            if cur.rowcount != 1:
                return None  # 恰好被其他 worker 抢走
            return self.get_task(row["id"])

    def recover_orphan_tasks(self, reason: str = "实例重启, 任务中断(可重建)") -> list:
        """启动时收回上次进程留下的 running 任务(进程崩溃/被杀/机器休眠断电): 置为 failed 并写明原因。
        不收回就永远卡在「运行中」, 前端连「重建」都被 409 拒绝。返回被收回的任务 id。"""
        with self._lock:
            ids = [r["id"] for r in self._conn.execute(
                "SELECT id FROM clone_tasks WHERE status = 'running'").fetchall()]
            if ids:
                self._conn.execute(
                    "UPDATE clone_tasks SET status = 'failed', stage = '中断', error_msg = ?, updated_at = ?"
                    " WHERE status = 'running'", (reason, _now()))
                self._conn.commit()
        return ids

    def stale_running_tasks(self, older_than_s: float) -> list:
        """running 且 updated_at 早于 older_than_s 秒前的任务 id(心跳停了)。"""
        cutoff = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - older_than_s,
                                        timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            return [r["id"] for r in self._conn.execute(
                "SELECT id FROM clone_tasks WHERE status = 'running' AND updated_at < ?", (cutoff,)).fetchall()]

    def touch_task(self, task_id: int) -> None:
        """心跳触摸: 仅刷新 updated_at(重型站长阶段防误收割)。"""
        with self._lock:
            self._conn.execute(
                "UPDATE clone_tasks SET updated_at = ? WHERE id = ?",
                (_now(), task_id),
            )
            self._conn.commit()

    def update_task(self, task_id: int, **fields):
        allowed = {k: v for k, v in fields.items() if k in _TASK_COLUMNS}
        if not allowed:
            return
        allowed["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in allowed)
        with self._lock:
            self._conn.execute(
                f"UPDATE clone_tasks SET {cols} WHERE id = ?",
                (*allowed.values(), task_id),
            )
            self._conn.commit()

    def get_task(self, task_id: int):
        with self._lock:
            row = self._conn.execute(
                "SELECT t.*, b.name AS batch_name, b.created_by AS created_by, b.workspace_id AS workspace_id,"
                " COALESCE((SELECT NULLIF(u.display_name, '') FROM users u WHERE u.username = b.created_by), b.created_by) AS creator_name FROM clone_tasks t"
                " LEFT JOIN batch_jobs b ON t.batch_id = b.id WHERE t.id = ?",
                (task_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_tasks(self, batch_id=None, status=None, q=None, limit=400,
                   project_id=None, *, workspace_id):
        """workspace_id 必填(多租户隔离); 仅平台级维护任务可显式传 ALL_WORKSPACES。"""
        where, params = [], []
        if workspace_id is not ALL_WORKSPACES:
            where.append("b.workspace_id = ?")
            params.append(int(workspace_id))
        if project_id:
            where.append("b.project_id = ?")
            params.append(project_id)
        if batch_id:
            where.append("t.batch_id = ?")
            params.append(batch_id)
        if status:
            where.append("t.status = ?")
            params.append(status)
        if q:
            where.append("(t.target_url LIKE ? OR t.domain LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                "SELECT t.*, b.name AS batch_name, b.created_by AS created_by, b.workspace_id AS workspace_id,"
                " COALESCE((SELECT NULLIF(u.display_name, '') FROM users u WHERE u.username = b.created_by), b.created_by) AS creator_name FROM clone_tasks t"
                f" LEFT JOIN batch_jobs b ON t.batch_id = b.id{clause}"
                " ORDER BY t.id DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def queue_counts(self, workspace_id=None) -> dict:
        counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
        with self._lock:
            if workspace_id is None:
                rows = self._conn.execute(
                    "SELECT status, COUNT(*) AS c FROM clone_tasks GROUP BY status"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT t.status, COUNT(*) AS c FROM clone_tasks t JOIN batch_jobs b"
                    " ON t.batch_id = b.id WHERE b.workspace_id = ? GROUP BY t.status",
                    (workspace_id,)).fetchall()
        for r in rows:
            if r["status"] in counts:
                counts[r["status"]] = r["c"]
        return counts

    # ------------------------------------------------------------------
    # CAS 全局缓存池统计
    # ------------------------------------------------------------------
    def record_asset_name(self, name: str, sha: str, size: int = 0) -> None:
        """登记"原站文件名 -> CAS sha"映射(同名新字节覆盖旧映射)。"""
        if not name or not sha:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO asset_names (name, sha, size, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET sha=excluded.sha, size=excluded.size, "
                "updated_at=excluded.updated_at",
                (name, sha, int(size or 0), _now()),
            )
            self._conn.commit()

    def find_asset_name(self, name: str):
        """查文件名对应的 CAS sha; 无映射返回 None。"""
        if not name:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT sha FROM asset_names WHERE name = ?", (name,)
            ).fetchone()
        return row[0] if row else None

    def cas_ext(self, sha: str):
        """查 CAS 资源扩展名(含点); 无记录返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT ext FROM cas_assets WHERE sha = ?", (sha,)
            ).fetchone()
        return row[0] if row else None

    def record_cas(self, sha: str, size: int, ext: str) -> bool:
        """登记资源; 返回 True=全新写入(miss), False=池内命中(hit)。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO cas_assets (sha, size, ext, created_at) VALUES (?, ?, ?, ?)",
                (sha, size, ext, _now()),
            )
            if cur.rowcount > 0:
                self._bump("misses")
                self._conn.commit()
                return True
            self._bump("hits")
            self._conn.commit()
            return False

    def _bump(self, key: str):
        self._conn.execute(
            "INSERT INTO cas_counters (key, value) VALUES (?, 1)"
            " ON CONFLICT(key) DO UPDATE SET value = value + 1",
            (key,),
        )

    def cas_stats(self) -> dict:
        with self._lock:
            assets = self._conn.execute("SELECT COUNT(*) AS c FROM cas_assets").fetchone()["c"]
            rows = self._conn.execute("SELECT key, value FROM cas_counters").fetchall()
        counters = {r["key"]: r["value"] for r in rows}
        hits, misses = counters.get("hits", 0), counters.get("misses", 0)
        total = hits + misses
        hit_rate = round(hits * 100.0 / total, 1) if total else 0.0
        return {"assets": assets, "hits": hits, "misses": misses, "hit_rate": hit_rate}

    def clear_cas(self):
        with self._lock:
            self._conn.execute("DELETE FROM cas_assets")
            self._conn.execute("DELETE FROM cas_counters")
            self._conn.commit()

    # ------------------------------------------------------------------
    # 多项目工作区
    # ------------------------------------------------------------------
    def ensure_default_project(self, workspace_id: int = 1) -> int:
        """每个工作区的默认项目保底(该区 id 最小的项目): 首次调用创建,
        并回填该区存量批次的 project_id。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM projects WHERE workspace_id = ? ORDER BY id LIMIT 1",
                (workspace_id,)).fetchone()
            if row is not None:
                pid = row["id"]
            else:
                cur = self._conn.execute(
                    "INSERT INTO projects (name, description, color, created_at, workspace_id)"
                    " VALUES ('默认项目', '未分组的历史批次与任务', '#6366f1', ?, ?)",
                    (_now(), workspace_id))
                pid = cur.lastrowid
            self._conn.execute(
                "UPDATE batch_jobs SET project_id = ? WHERE workspace_id = ? AND (project_id IS NULL"
                " OR project_id NOT IN (SELECT id FROM projects WHERE workspace_id = ?))",
                (pid, workspace_id, workspace_id))
            self._conn.commit()
            return pid

    def create_project(self, name: str, description: str, color: str,
                       created_by: str = "", *, workspace_id: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO projects (name, description, color, created_at, created_by, workspace_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name, description, color, _now(), created_by or "", workspace_id))
            self._conn.commit()
            return cur.lastrowid

    def update_project(self, project_id: int, **fields):
        allowed = {k: v for k, v in fields.items() if k in ("name", "description", "color")}
        if not allowed:
            return
        cols = ", ".join(f"{k} = ?" for k in allowed)
        with self._lock:
            self._conn.execute(f"UPDATE projects SET {cols} WHERE id = ?",
                               (*allowed.values(), project_id))
            self._conn.commit()

    def get_project(self, project_id: int):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return dict(row) if row else None

    def list_projects(self, *, workspace_id: int) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.*,"
                " (SELECT COUNT(*) FROM batch_jobs b WHERE b.project_id = p.id) AS batch_cnt,"
                " (SELECT COUNT(*) FROM clone_tasks t JOIN batch_jobs b2 ON t.batch_id = b2.id"
                "  WHERE b2.project_id = p.id) AS task_cnt,"
                " (SELECT COUNT(*) FROM clone_tasks t JOIN batch_jobs b2 ON t.batch_id = b2.id"
                "  WHERE b2.project_id = p.id AND t.status IN ('pending','running')) AS running_cnt,"
                " (SELECT COUNT(*) FROM clone_tasks t JOIN batch_jobs b2 ON t.batch_id = b2.id"
                "  WHERE b2.project_id = p.id AND t.status = 'completed') AS ok_cnt,"
                " (SELECT COUNT(*) FROM clone_tasks t JOIN batch_jobs b2 ON t.batch_id = b2.id"
                "  WHERE b2.project_id = p.id AND t.status = 'failed') AS fail_cnt,"
                " (SELECT AVG(t.similarity_score) FROM clone_tasks t"
                "  JOIN batch_jobs b2 ON t.batch_id = b2.id"
                "  WHERE b2.project_id = p.id AND t.status = 'completed'"
                "  AND t.similarity_score IS NOT NULL) AS avg_score,"
                " COALESCE((SELECT NULLIF(u.display_name, '') FROM users u WHERE u.username = p.created_by), p.created_by) AS owner_name,"
                " (SELECT GROUP_CONCAT(nm, char(31)) FROM (SELECT DISTINCT "
                "COALESCE((SELECT NULLIF(u.display_name, '') FROM users u WHERE u.username = b3.created_by), b3.created_by) AS nm"
                "  FROM batch_jobs b3 WHERE b3.project_id = p.id AND b3.created_by != '')) AS contributors"
                " FROM projects p WHERE p.workspace_id = ? ORDER BY p.id ASC",
                (workspace_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["contributors"] = [x for x in (d.get("contributors") or "").split("\x1f") if x]
            out.append(d)
        return out

    def delete_project(self, project_id: int) -> bool:
        """仅允许删除不含任务的项目; 批次先迁回默认项目。"""
        proj = self.get_project(project_id)
        if proj is None:
            return False
        default_id = self.ensure_default_project(proj["workspace_id"])
        if project_id == default_id:
            return False
        with self._lock:
            used = self._conn.execute(
                "SELECT COUNT(*) AS c FROM clone_tasks t JOIN batch_jobs b"
                " ON t.batch_id = b.id WHERE b.project_id = ?",
                (project_id,)).fetchone()["c"]
            if used:
                return False
            self._conn.execute(
                "UPDATE batch_jobs SET project_id = ? WHERE project_id = ?",
                (default_id, project_id))
            self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            self._conn.commit()
            return True

    def project_task_ids(self, project_id: int) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT t.id FROM clone_tasks t JOIN batch_jobs b ON t.batch_id = b.id"
                " WHERE b.project_id = ? ORDER BY t.id", (project_id,)).fetchall()
        return [r["id"] for r in rows]

    def delete_project_cascade(self, project_id: int) -> bool:
        """级联删除: 任务/批次/项目一并清除(默认项目不可删)。"""
        proj = self.get_project(project_id)
        if proj is None:
            return False
        default_id = self.ensure_default_project(proj["workspace_id"])
        if project_id == default_id:
            return False
        with self._lock:
            self._conn.execute(
                "DELETE FROM clone_tasks WHERE batch_id IN"
                " (SELECT id FROM batch_jobs WHERE project_id = ?)", (project_id,))
            self._conn.execute(
                "DELETE FROM batch_jobs WHERE project_id = ?", (project_id,))
            self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            self._conn.commit()
        return True

    # ------------------------------------------------------------------
    # 用户 / RBAC
    # ------------------------------------------------------------------
    def create_user(self, username: str, password_hash: str, role: str,
                    display_name: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO users (username, password_hash, display_name, role,"
                " is_active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                (username, password_hash, display_name or username, role, _now()))
            self._conn.commit()
            return cur.lastrowid

    def get_user(self, user_id: int):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def get_user_by_username(self, username: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            return dict(row) if row else None

    def list_users(self) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, username, display_name, role, is_active,"
                " last_login_at, created_at FROM users ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def workspace_members(self, workspace_id: int, limit: int = 8) -> tuple:
        """工作区成员(启用中), 供侧栏头像与成员数展示。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.username, u.display_name, m.role FROM memberships m"
                " JOIN users u ON u.id = m.user_id WHERE m.workspace_id = ? AND u.is_active = 1"
                " ORDER BY m.joined_at, u.id LIMIT ?", (workspace_id, limit)).fetchall()
            total = self._conn.execute(
                "SELECT COUNT(*) AS c FROM memberships m JOIN users u ON u.id = m.user_id"
                " WHERE m.workspace_id = ? AND u.is_active = 1", (workspace_id,)).fetchone()["c"]
            return [dict(r) for r in rows], total

    # ------------------------------------------------------------------
    # 工作区 / 成员关系 / 邀请 / 用量
    # ------------------------------------------------------------------
    def create_workspace(self, name: str, owner_id, plan: str = "free") -> int:
        with self._lock:
            slug = "ws-" + secrets.token_hex(4)
            cur = self._conn.execute(
                "INSERT INTO workspaces (slug, name, plan, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?)", (slug, name, plan, owner_id, _now()))
            wid = cur.lastrowid
            if owner_id:
                self._conn.execute(
                    "INSERT OR IGNORE INTO memberships (workspace_id, user_id, role, joined_at)"
                    " VALUES (?, ?, 'owner', ?)", (wid, owner_id, _now()))
            self._conn.commit()
        self.ensure_default_project(wid)
        return wid

    def get_workspace(self, workspace_id: int):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
            return dict(row) if row else None

    def update_workspace(self, workspace_id: int, **fields):
        allowed = {k: v for k, v in fields.items() if k in ("name", "plan")}
        if not allowed:
            return
        cols = ", ".join(f"{k} = ?" for k in allowed)
        with self._lock:
            self._conn.execute(f"UPDATE workspaces SET {cols} WHERE id = ?",
                               (*allowed.values(), workspace_id))
            self._conn.commit()

    def user_workspaces(self, user_id: int) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT w.id, w.name, w.slug, w.plan, m.role,"
                " (SELECT COUNT(*) FROM memberships m2 JOIN users u ON u.id = m2.user_id"
                "  WHERE m2.workspace_id = w.id AND u.is_active = 1) AS member_count"
                " FROM memberships m JOIN workspaces w ON w.id = m.workspace_id"
                " WHERE m.user_id = ? ORDER BY w.id", (user_id,)).fetchall()
            return [dict(r) for r in rows]

    def get_membership(self, workspace_id: int, user_id: int):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memberships WHERE workspace_id = ? AND user_id = ?",
                (workspace_id, user_id)).fetchone()
            return dict(row) if row else None

    def add_member(self, workspace_id: int, user_id: int, role: str, invited_by=None):
        with self._lock:
            self._conn.execute(
                "INSERT INTO memberships (workspace_id, user_id, role, invited_by, joined_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(workspace_id, user_id) DO NOTHING",
                (workspace_id, user_id, role, invited_by, _now()))
            self._conn.commit()

    def set_member_role(self, workspace_id: int, user_id: int, role: str):
        with self._lock:
            self._conn.execute(
                "UPDATE memberships SET role = ? WHERE workspace_id = ? AND user_id = ?",
                (role, workspace_id, user_id))
            self._conn.commit()

    def remove_member(self, workspace_id: int, user_id: int):
        with self._lock:
            self._conn.execute(
                "DELETE FROM memberships WHERE workspace_id = ? AND user_id = ?",
                (workspace_id, user_id))
            self._conn.commit()

    def list_members(self, workspace_id: int) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.id, u.username, u.display_name, u.is_active, u.last_login_at,"
                " u.created_at, m.role, m.joined_at FROM memberships m"
                " JOIN users u ON u.id = m.user_id WHERE m.workspace_id = ?"
                " ORDER BY m.joined_at, u.id", (workspace_id,)).fetchall()
            return [dict(r) for r in rows]

    def count_managers(self, workspace_id: int) -> int:
        """该区启用中的所有者 + 管理员人数(防止把最后一个管理者降级/移除)。"""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) AS c FROM memberships m JOIN users u ON u.id = m.user_id"
                " WHERE m.workspace_id = ? AND m.role IN ('owner', 'admin') AND u.is_active = 1",
                (workspace_id,)).fetchone()["c"]

    def create_invite(self, workspace_id: int, role: str, token_hash: str,
                      created_by, expires_at: str, max_uses: int = 1) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO invites (workspace_id, role, token_hash, created_by, expires_at,"
                " max_uses, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (workspace_id, role, token_hash, created_by, expires_at, max_uses, _now()))
            self._conn.commit()
            return cur.lastrowid

    def get_invite_by_hash(self, token_hash: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT i.*, w.name AS workspace_name FROM invites i"
                " JOIN workspaces w ON w.id = i.workspace_id WHERE i.token_hash = ?",
                (token_hash,)).fetchone()
            return dict(row) if row else None

    def use_invite(self, invite_id: int) -> bool:
        """原子消耗一次邀请; 已撤销/用尽返回 False。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE invites SET uses = uses + 1 WHERE id = ? AND revoked = 0"
                " AND uses < max_uses", (invite_id,))
            self._conn.commit()
            return cur.rowcount == 1

    def list_invites(self, workspace_id: int) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role, expires_at, max_uses, uses, revoked, created_at FROM invites"
                " WHERE workspace_id = ? AND revoked = 0 AND uses < max_uses AND expires_at > ?"
                " ORDER BY id DESC", (workspace_id, _now())).fetchall()
            return [dict(r) for r in rows]

    def revoke_invite(self, workspace_id: int, invite_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE invites SET revoked = 1 WHERE id = ? AND workspace_id = ?",
                (invite_id, workspace_id))
            self._conn.commit()
            return cur.rowcount == 1

    # ------------------------------------------------------------------
    # 定时任务
    # ------------------------------------------------------------------
    _SCHEDULE_COLS = ("project_id", "name", "urls_json", "config_json", "freq", "hour", "minute",
                      "weekday", "enabled", "next_run_at", "last_run_at", "history_json", "runs")

    def create_schedule(self, workspace_id: int, created_by: str = "", **fields) -> int:
        cols = {k: v for k, v in fields.items() if k in self._SCHEDULE_COLS}
        names = ["workspace_id", "created_by", "created_at", *cols]
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO schedules ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                (workspace_id, created_by, _now(), *cols.values()))
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def update_schedule(self, schedule_id: int, **fields):
        cols = {k: v for k, v in fields.items() if k in self._SCHEDULE_COLS}
        if not cols:
            return
        with self._lock:
            self._conn.execute(f"UPDATE schedules SET {', '.join(k + ' = ?' for k in cols)} WHERE id = ?",
                               (*cols.values(), schedule_id))
            self._conn.commit()

    def get_schedule(self, schedule_id: int):
        with self._lock:
            row = self._conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
            return dict(row) if row else None

    def list_schedules(self, workspace_id=None) -> list:
        with self._lock:
            if workspace_id is None:
                rows = self._conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM schedules WHERE workspace_id = ? ORDER BY id DESC",
                                          (workspace_id,)).fetchall()
            return [dict(r) for r in rows]

    def delete_schedule(self, schedule_id: int):
        with self._lock:
            self._conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            self._conn.commit()

    def due_schedules(self, now_str: str) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at != '' AND next_run_at <= ?",
                (now_str,)).fetchall()
            return [dict(r) for r in rows]

    def batch_brief(self, batch_ids: list) -> dict:
        """批次 id → {total, completed, failed, running, pending, avg_score}。"""
        ids = [int(i) for i in batch_ids if i]
        if not ids:
            return {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT batch_id, status, COUNT(*), AVG(similarity_score) FROM clone_tasks"
                f" WHERE batch_id IN ({', '.join('?' * len(ids))}) GROUP BY batch_id, status", ids).fetchall()
        out: dict = {}
        for bid, st, n, avg in rows:
            d = out.setdefault(bid, {"total": 0, "completed": 0, "failed": 0, "running": 0,
                                     "pending": 0, "avg_score": None})
            d["total"] += n
            d[st] = d.get(st, 0) + n
            if st == "completed" and avg is not None:
                d["avg_score"] = round(avg, 1)
        return out

    def recent_stage_times(self, limit: int = 60) -> list:
        """最近完成任务的 stats_json(只取含 stage_times 的), 供预计剩余时间统计。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT stats_json FROM clone_tasks WHERE status = 'completed'"
                " AND stats_json LIKE '%stage_times%' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for (raw,) in rows:
            try:
                out.append(json.loads(raw or "{}"))
            except ValueError:
                continue
        return out

    def throughput(self, workspace_id=None, minutes: int = 60) -> dict:
        """最近 N 分钟完成/失败数(按 updated_at)。"""
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        sql = ("SELECT t.status, COUNT(*) FROM clone_tasks t JOIN batch_jobs b ON b.id = t.batch_id"
               " WHERE t.updated_at >= ? AND t.status IN ('completed', 'failed')")
        args: list = [since]
        if workspace_id is not None:
            sql += " AND b.workspace_id = ?"
            args.append(workspace_id)
        with self._lock:
            rows = self._conn.execute(sql + " GROUP BY t.status", args).fetchall()
        d = {r[0]: r[1] for r in rows}
        return {"completed": d.get("completed", 0), "failed": d.get("failed", 0), "minutes": minutes}

    def record_usage(self, workspace_id: int, kind: str, amount: int = 1, task_id=None):
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage_events (workspace_id, kind, amount, task_id, at)"
                " VALUES (?, ?, ?, ?, ?)", (workspace_id, kind, amount, task_id, _now()))
            self._conn.commit()

    def usage_since(self, workspace_id: int, kind: str, since: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS n FROM usage_events"
                " WHERE workspace_id = ? AND kind = ? AND at >= ?",
                (workspace_id, kind, since)).fetchone()
            return int(row["n"])

    def workspace_task_ids(self, workspace_id: int) -> set:
        with self._lock:
            rows = self._conn.execute(
                "SELECT t.id FROM clone_tasks t JOIN batch_jobs b ON t.batch_id = b.id"
                " WHERE b.workspace_id = ?", (workspace_id,)).fetchall()
        return {r["id"] for r in rows}

    def task_workspace(self, task_id: int):
        with self._lock:
            row = self._conn.execute(
                "SELECT b.workspace_id AS w FROM clone_tasks t JOIN batch_jobs b"
                " ON t.batch_id = b.id WHERE t.id = ?", (task_id,)).fetchone()
        return row["w"] if row else None

    def count_users(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]

    def count_admins(self, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) AS c FROM users WHERE role = 'admin'"
        if active_only:
            sql += " AND is_active = 1"
        with self._lock:
            return self._conn.execute(sql).fetchone()["c"]

    def update_user(self, user_id: int, **fields):
        allowed = {k: v for k, v in fields.items()
                   if k in ("password_hash", "role", "is_active", "display_name")}
        if not allowed:
            return
        cols = ", ".join(f"{k} = ?" for k in allowed)
        with self._lock:
            self._conn.execute(f"UPDATE users SET {cols} WHERE id = ?",
                               (*allowed.values(), user_id))
            self._conn.commit()

    def touch_login(self, user_id: int):
        with self._lock:
            self._conn.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (_now(), user_id))
            self._conn.commit()

    # ------------------------------------------------------------------
    # 审计日志
    # ------------------------------------------------------------------
    def log_audit(self, username: str, action: str, detail: str, ip: str,
                  workspace_id: int = 1):
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_logs (username, action, detail, ip, created_at, workspace_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (username, action, detail[:2000], ip, _now(), workspace_id))
            self._conn.execute(
                "DELETE FROM audit_logs WHERE id < ("
                " SELECT MIN(id) FROM (SELECT id FROM audit_logs"
                "  ORDER BY id DESC LIMIT 5000))")
            self._conn.commit()

    def list_audit(self, limit: int = 100, action: str = "", q: str = "",
                   workspace_id=None) -> list:
        where, params = [], []
        if workspace_id is not None:
            where.append("workspace_id = ?")
            params.append(workspace_id)
        if action:
            where.append("action = ?")
            params.append(action)
        if q:
            where.append("(username LIKE ? OR detail LIKE ? OR ip LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(max(1, min(500, limit)))  # type: ignore[arg-type]
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM audit_logs{clause}"
                " ORDER BY id DESC LIMIT ?", params).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 存档导入(网站记录跨实例/工作区迁移)
    # ------------------------------------------------------------------
    def instance_id(self) -> str:
        """本实例的稳定随机标识: 存档里的记录 uid = 实例:任务号, 用于导入去重。"""
        iid = self.get_setting("instance_id")
        if not iid:
            import secrets
            iid = secrets.token_hex(6)
            self.set_setting("instance_id", iid)
        return iid

    def archive_task_for_uid(self, workspace_id: int, uid: str):
        """该区里已存在的同一条记录(先前导入过的, 或就是本实例本区的原任务) -> 任务 id。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT a.task_id FROM archive_imports a JOIN clone_tasks t ON t.id = a.task_id"
                " JOIN batch_jobs b ON b.id = t.batch_id"
                " WHERE a.workspace_id = ? AND a.uid = ? AND b.workspace_id = ?",
                (workspace_id, uid, workspace_id)).fetchone()
            if row:
                return row["task_id"]
        iid, _, tid = uid.partition(":")
        if iid == self.instance_id() and tid.isdigit():
            t = self.get_task(int(tid))
            if t and t.get("workspace_id") == workspace_id:
                return t["id"]
        return None

    def insert_archived_batch(self, *, workspace_id: int, project_id: int, name: str,
                              config: dict, created_by: str, created_at: str, tasks: list) -> list:
        """按存档原样写入一个批次及其任务(不入调度队列: 状态只会是 completed/partial/failed)。
        tasks: [{uid, target_url, domain, status, progress, stage, similarity_score,
        stats_json, error_msg, created_at, updated_at}] -> 新任务 id 列表(同序)。"""
        now = _now()
        done = sum(1 for t in tasks if t["status"] in ("completed", "partial", "failed"))
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO batch_jobs (project_id, name, total_urls, completed_urls,"
                " config_json, created_at, created_by, workspace_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, name, len(tasks), done, json.dumps(config, ensure_ascii=False),
                 created_at or now, created_by or "", workspace_id))
            batch_id = cur.lastrowid
            ids = []
            for t in tasks:
                c = self._conn.execute(
                    "INSERT INTO clone_tasks (batch_id, target_url, domain, status, progress, stage,"
                    " similarity_score, stats_json, error_msg, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (batch_id, t["target_url"], t["domain"], t["status"], int(t.get("progress") or 0),
                     t.get("stage") or "", t.get("similarity_score"), t.get("stats_json") or "{}",
                     t.get("error_msg") or "", t.get("created_at") or now, t.get("updated_at") or now))
                ids.append(c.lastrowid)
                if t.get("uid"):
                    self._conn.execute(
                        "INSERT OR REPLACE INTO archive_imports (workspace_id, uid, task_id, created_at)"
                        " VALUES (?, ?, ?, ?)", (workspace_id, t["uid"], c.lastrowid, now))
            self._conn.commit()
            return ids

    # ------------------------------------------------------------------
    # 系统设置(KV)
    # ------------------------------------------------------------------
    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM system_settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        with self._lock:
            self._conn.execute(
                "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (key, value, _now()))
            self._conn.commit()

    # ------------------------------------------------------------------
    # Docker 容器登记
    # ------------------------------------------------------------------
    _DOCKER_COLUMNS = {"container_id", "image_tag", "port", "status",
                       "mode", "bind", "expires_at", "mem_limit", "last_error"}

    def upsert_docker(self, task_id: int, **fields):
        allowed = {k: v for k, v in fields.items() if k in self._DOCKER_COLUMNS}
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO docker_containers (task_id, created_at, updated_at)"
                " VALUES (?, ?, ?) ON CONFLICT(task_id) DO NOTHING",
                (task_id, now, now))
            if allowed:
                cols = ", ".join(f"{k} = ?" for k in allowed)
                self._conn.execute(
                    f"UPDATE docker_containers SET {cols}, updated_at = ?"
                    " WHERE task_id = ?", (*allowed.values(), now, task_id))
            self._conn.commit()

    def get_docker(self, task_id: int):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM docker_containers WHERE task_id = ?",
                (task_id,)).fetchone()
            return dict(row) if row else None

    def list_docker(self) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.*, t.domain, t.target_url FROM docker_containers d"
                " LEFT JOIN clone_tasks t ON t.id = d.task_id"
                " ORDER BY d.updated_at DESC").fetchall()
            return [dict(r) for r in rows]

    def delete_docker(self, task_id: int):
        with self._lock:
            self._conn.execute("DELETE FROM docker_containers WHERE task_id = ?",
                               (task_id,))
            self._conn.commit()

    def docker_used_ports(self) -> set:
        with self._lock:
            rows = self._conn.execute(
                "SELECT port FROM docker_containers WHERE port > 0").fetchall()
        return {r["port"] for r in rows}

    # ------------------------------------------------------------------
    # 短链接
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 表单埋点推送配置
    # ------------------------------------------------------------------
    def delete_tracker_config(self, task_id: int):
        with self._lock:
            self._conn.execute("DELETE FROM tracker_configs WHERE task_id = ?", (task_id,))
            self._conn.commit()

    def get_tracker_config(self, task_id: int):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tracker_configs WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            item = dict(row)
            try:
                item["slots"] = json.loads(item.pop("slots_json") or "[]")
            except (ValueError, TypeError):
                item["slots"] = []
            return item

    def upsert_tracker_config(self, task_id: int, enabled: bool, token: str,
                              secret_enc: str, keyword: str, slots: list,
                              mask: bool) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO tracker_configs (task_id, enabled, token, secret_enc,"
                " keyword, slots_json, mask, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(task_id) DO UPDATE SET enabled=?, token=?, secret_enc=?,"
                " keyword=?, slots_json=?, mask=?, updated_at=?",
                (task_id, int(enabled), token, secret_enc, keyword,
                 json.dumps(slots, ensure_ascii=False), int(mask), now, now,
                 int(enabled), token, secret_enc, keyword,
                 json.dumps(slots, ensure_ascii=False), int(mask), now))
            self._conn.commit()

    def next_short_link_seq(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM short_link_tasks").fetchone()
            return row["n"]

    def create_short_task(self, name: str, target_url: str, service: str,
                          clone_task_id, note: str = "") -> int:
        now = _now()
        with self._lock:
            seq = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM short_link_tasks").fetchone()["n"]
            cur = self._conn.execute(
                "INSERT INTO short_link_tasks (seq, name, target_url, service,"
                " clone_task_id, status, note, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (seq, name, target_url, service, clone_task_id, note, now, now))
            self._conn.commit()
            return cur.lastrowid

    def link_kpis(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS links, COALESCE(SUM(clicks), 0) AS clicks FROM short_links").fetchone()
            return {"links": row["links"], "clicks": row["clicks"]}

    def list_short_tasks(self, q: str = None, limit: int = 200):
        where, params = [], []
        if q:
            where.append("(name LIKE ? OR target_url LIKE ? OR note LIKE ?)")
            params.extend([f"%{q}%"] * 3)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)  # type: ignore[arg-type]
        with self._lock:
            rows = self._conn.execute(
                f"SELECT t.*, (SELECT COUNT(*) FROM short_links s WHERE s.link_task_id = t.id) AS link_count,"
                f" (SELECT COALESCE(SUM(s.clicks), 0) FROM short_links s WHERE s.link_task_id = t.id) AS total_clicks"
                f" FROM short_link_tasks t{clause} ORDER BY t.id DESC LIMIT ?",
                params).fetchall()
            return [dict(r) for r in rows]

    def get_short_task(self, task_row_id: int):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM short_link_tasks WHERE id = ?", (task_row_id,)).fetchone()
            return dict(row) if row else None

    def update_short_task(self, task_row_id: int, **fields) -> bool:
        allowed = {k: v for k, v in {
            "name": fields.get("name"),
            "target_url": fields.get("target_url"),
            "service": fields.get("service"),
            "clone_task_id": fields.get("clone_task_id"),
            "note": fields.get("note"),
            "status": fields.get("status"),
        }.items() if v is not None}
        if not allowed:
            return False
        allowed["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in allowed)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE short_link_tasks SET {cols} WHERE id = ?",
                (*allowed.values(), task_row_id))
            self._conn.commit()
            return cur.rowcount > 0

    def refresh_short_task_status(self, task_row_id: int):
        """按短链生成结果刷新任务状态: pending -> partial/done/failed。"""
        task = self.get_short_task(task_row_id)
        if task is None:
            return
        with self._lock:
            n = self._conn.execute(
                "SELECT COUNT(*) AS c FROM short_links WHERE link_task_id = ?",
                (task_row_id,)).fetchone()
        total = n["c"] or 0
        if total == 0:
            status = "failed" if task["status"] == "pending" else task["status"]
        else:
            status = "done"
        self.update_short_task(task_row_id, status=status)

    def delete_short_task(self, task_row_id: int, cascade: bool = True) -> bool:
        with self._lock:
            if cascade:
                self._conn.execute(
                    "DELETE FROM short_links WHERE link_task_id = ?", (task_row_id,))
            cur = self._conn.execute(
                "DELETE FROM short_link_tasks WHERE id = ?", (task_row_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def list_links_of_short_task(self, task_row_id: int):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM short_links WHERE link_task_id = ? ORDER BY id",
                (task_row_id,)).fetchall()
            out = []
            for r in rows:
                item = dict(r)
                try:
                    item["extras"] = json.loads(item.pop("extras_json") or "{}")
                except (ValueError, TypeError):
                    item["extras"] = {}
                out.append(item)
            return out

    def create_short_link(self, task_id, target_url: str, service: str,
                          short_url: str, label: str, extras: dict,
                          link_task_id=None) -> int:
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO short_links (task_id, link_task_id, target_url, service,"
                " short_url, label, extras_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, link_task_id, target_url, service, short_url, label,
                 json.dumps(extras or {}, ensure_ascii=False), now, now))
            self._conn.commit()
            return cur.lastrowid

    def list_short_links(self, task_id=None, q: str = None, limit: int = 200):
        where, params = [], []
        if task_id:
            where.append("task_id = ?")
            params.append(task_id)
        if q:
            where.append("(target_url LIKE ? OR short_url LIKE ? OR label LIKE ?)")
            params.extend([f"%{q}%"] * 3)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM short_links{clause} ORDER BY id DESC LIMIT ?",
                params).fetchall()
            out = []
            for r in rows:
                item = dict(r)
                try:
                    item["extras"] = json.loads(item.pop("extras_json") or "{}")
                except (ValueError, TypeError):
                    item["extras"] = {}
                out.append(item)
            return out

    def get_short_link(self, link_id: int):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM short_links WHERE id = ?", (link_id,)).fetchone()
            if row is None:
                return None
            item = dict(row)
            try:
                item["extras"] = json.loads(item.pop("extras_json") or "{}")
            except (ValueError, TypeError):
                item["extras"] = {}
            return item

    def update_short_link(self, link_id: int, **fields) -> bool:
        allowed = {k: v for k, v in {
            "target_url": fields.get("target_url"),
            "service": fields.get("service"),
            "short_url": fields.get("short_url"),
            "label": fields.get("label"),
            "task_id": fields.get("task_id"),
        }.items() if v is not None}
        if not allowed:
            return False
        allowed["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in allowed)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE short_links SET {cols} WHERE id = ?",
                (*allowed.values(), link_id))
            self._conn.commit()
            return cur.rowcount > 0

    def bump_short_link_click(self, link_id: int):
        with self._lock:
            self._conn.execute(
                "UPDATE short_links SET clicks = clicks + 1, updated_at = ?"
                " WHERE id = ?", (_now(), link_id))
            self._conn.commit()

    def delete_short_link(self, link_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM short_links WHERE id = ?", (link_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 远程部署
    # ------------------------------------------------------------------
    def next_remote_seq(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM remote_deploys").fetchone()
            return row["n"]

    def create_remote_deploy(self, name: str, host: str, ssh_port: int,
                             username: str, password_enc: str,
                             clone_task_id, remote_port: int, note: str) -> int:
        now = _now()
        with self._lock:
            seq = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM remote_deploys").fetchone()["n"]
            cur = self._conn.execute(
                "INSERT INTO remote_deploys (seq, name, host, ssh_port, username,"
                " password_enc, clone_task_id, remote_port, note, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?)",
                (seq, name, host, ssh_port, username, password_enc,
                 clone_task_id, remote_port, note, now, now))
            self._conn.commit()
            return cur.lastrowid

    def list_remote_deploys(self, q: str = None, limit: int = 200):
        where, params = [], []
        if q:
            where.append("(name LIKE ? OR host LIKE ? OR note LIKE ?)")
            params.extend([f"%{q}%"] * 3)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)  # type: ignore[arg-type]
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM remote_deploys{clause} ORDER BY id DESC LIMIT ?",
                params).fetchall()
            out = []
            for r in rows:
                item = dict(r)
                item.pop("password_enc", None)   # 凭据不出后端
                out.append(item)
            return out

    def get_remote_deploy(self, dep_id: int):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM remote_deploys WHERE id = ?", (dep_id,)).fetchone()
            return dict(row) if row else None

    def update_remote_deploy(self, dep_id: int, **fields) -> bool:
        allowed = {k: v for k, v in {
            "name": fields.get("name"),
            "host": fields.get("host"),
            "ssh_port": fields.get("ssh_port"),
            "username": fields.get("username"),
            "password_enc": fields.get("password_enc"),
            "clone_task_id": fields.get("clone_task_id"),
            "remote_port": fields.get("remote_port"),
            "note": fields.get("note"),
            "status": fields.get("status"),
            "progress": fields.get("progress"),
            "stage": fields.get("stage"),
            "log": fields.get("log"),
            "host_key": fields.get("host_key"),
            "service_url": fields.get("service_url"),
        }.items() if v is not None}
        if not allowed:
            return False
        allowed["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in allowed)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE remote_deploys SET {cols} WHERE id = ?",
                (*allowed.values(), dep_id))
            self._conn.commit()
            return cur.rowcount > 0

    def append_remote_log(self, dep_id: int, line: str, cap: int = 12000):
        with self._lock:
            row = self._conn.execute(
                "SELECT log FROM remote_deploys WHERE id = ?", (dep_id,)).fetchone()
            if row is None:
                return
            text = (row["log"] or "")
            stamp = _now()
            text = (text + f"[{stamp}] {line}\n")[-cap:]
            self._conn.execute(
                "UPDATE remote_deploys SET log = ?, updated_at = ? WHERE id = ?",
                (text, _now(), dep_id))
            self._conn.commit()

    def delete_remote_deploy(self, dep_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM remote_deploys WHERE id = ?", (dep_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def purge_audit_before(self, days: int) -> int:
        """清理 N 天前的审计日志, 返回删除行数(days<=0 不清理)。"""
        if days <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM audit_logs WHERE created_at < datetime('now', ?)",
                (f"-{days} days",))
            self._conn.commit()
            return cur.rowcount

    def close(self):
        with self._lock:
            self._conn.close()
