"""网站记录存档: 把任务(克隆产物 + 截图 + 还原度 + 元数据 + 日志)打成一个 zip, 可在任意实例/工作区导入还原。

存档布局(format=clonjurer-archive, version=1):
    manifest.json            项目 / 批次 / 任务元数据
    tasks/<n>/...            第 n 个任务的工作空间原样文件(不含可再生的 flask_app 与缓存)

约束:
- 导出不带凭据: 批次配置里的 cookie / bearer / AI Key 一律去掉(仓库公开 + 存档会外发)
- 导入只认 manifest 列出的 tasks/<n>/ 下的普通文件; 拒绝绝对路径、..、符号链接, 限总大小与文件数
- 导入的记录不进调度队列; 未完成(pending/running)的记录按失败落库
- 同一条记录(uid = 实例:原任务号)在同一工作区只导入一次
"""
from __future__ import annotations

import json
import shutil
import stat
import time
import zipfile
from contextlib import suppress
from pathlib import Path, PurePosixPath

FORMAT = "clonjurer-archive"
VERSION = 1
SKIP_NAMES = {"flask_app", "__pycache__", ".venv", ".DS_Store", "node_modules"}
SECRET_CONFIG_KEYS = ("cookie", "bearer", "ai_api_key")
STORED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".woff", ".woff2",
               ".zip", ".gz", ".br", ".mp4", ".webm", ".mp3", ".ico"}
KEEP_STATUS = ("completed", "partial", "failed")

MAX_TASKS = 500
MAX_FILES = 300_000
MAX_BYTES = 20 * 1024 ** 3      # 解压后总量上限 20 GiB
MAX_RATIO = 200                  # 单文件压缩比上限(防 zip 炸弹)


class ArchiveError(ValueError):
    """存档不合法(给用户看的原因)。"""


def _clean_config(raw) -> dict:
    cfg = dict(raw) if isinstance(raw, dict) else {}
    for k in SECRET_CONFIG_KEYS:
        cfg.pop(k, None)
    return cfg


def _iter_files(root: Path):
    if not root.is_dir():
        return
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for e in entries:
            if e.name in SKIP_NAMES or e.is_symlink():
                continue
            if e.is_dir():
                stack.append(e)
            elif e.is_file():
                yield e


def build(out_path: Path, tasks: list, batches: dict, projects: dict, *,
          instance_id: str, workspace_of, label: str = "", exported_by: str = "") -> dict:
    """tasks: db 任务行; batches/projects: id -> 行; workspace_of(task) -> 工作空间目录。
    写出 out_path, 返回 manifest。"""
    man = {"format": FORMAT, "version": VERSION, "label": label,
           "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "exported_by": exported_by, "instance": instance_id,
           "projects": [], "batches": [], "tasks": []}
    seen_p, seen_b = {}, {}
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for n, task in enumerate(tasks, 1):
            b = batches.get(task["batch_id"]) or {}
            pid = b.get("project_id")
            if pid not in seen_p:
                p = projects.get(pid) or {}
                seen_p[pid] = f"p{len(seen_p) + 1}"
                man["projects"].append({"key": seen_p[pid], "name": p.get("name") or "导入的项目",
                                        "description": p.get("description") or "",
                                        "color": p.get("color") or "#6366f1"})
            bid = task["batch_id"]
            if bid not in seen_b:
                seen_b[bid] = f"b{len(seen_b) + 1}"
                try:
                    cfg = json.loads(b.get("config_json") or "{}")
                except ValueError:
                    cfg = {}
                man["batches"].append({"key": seen_b[bid], "project": seen_p[pid],
                                       "name": b.get("name") or "", "created_at": b.get("created_at") or "",
                                       "created_by": b.get("created_by") or "", "config": _clean_config(cfg)})
            try:
                stats = json.loads(task.get("stats_json") or "{}")
            except ValueError:
                stats = {}
            prev = stats.get("archive") if isinstance(stats.get("archive"), dict) else {}
            uid = prev.get("uid") or f"{instance_id}:{task['id']}"
            ws = workspace_of(task)
            prefix = f"tasks/{n}/"
            files = size = 0
            for f in _iter_files(ws):
                rel = f.relative_to(ws).as_posix()
                ctype = zipfile.ZIP_STORED if f.suffix.lower() in STORED_EXTS else zipfile.ZIP_DEFLATED
                with suppress(OSError):
                    zf.write(f, prefix + rel, compress_type=ctype)
                    files += 1
                    size += f.stat().st_size
            man["tasks"].append({
                "key": str(n), "uid": uid, "src_id": task["id"], "batch": seen_b[bid],
                "target_url": task.get("target_url") or "", "domain": task.get("domain") or "",
                "status": task.get("status") or "", "progress": task.get("progress") or 0,
                "stage": task.get("stage") or "", "similarity_score": task.get("similarity_score"),
                "stats": stats, "error_msg": task.get("error_msg") or "",
                "created_at": task.get("created_at") or "", "updated_at": task.get("updated_at") or "",
                "files": files, "bytes": size})
        zf.writestr("manifest.json", json.dumps(man, ensure_ascii=False, indent=1))
    return man


def read_manifest(zf: zipfile.ZipFile) -> dict:
    try:
        man = json.loads(zf.read("manifest.json").decode("utf-8"))
    except KeyError:
        raise ArchiveError("不是 Clonjurer 存档(缺少 manifest.json)") from None
    except (ValueError, UnicodeDecodeError):
        raise ArchiveError("存档清单已损坏") from None
    if not isinstance(man, dict) or man.get("format") != FORMAT:
        raise ArchiveError("不是 Clonjurer 存档")
    if int(man.get("version") or 0) > VERSION:
        raise ArchiveError("存档版本较新, 请先升级本实例")
    tasks = man.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ArchiveError("存档里没有记录")
    if len(tasks) > MAX_TASKS:
        raise ArchiveError(f"单个存档最多 {MAX_TASKS} 条记录")
    return man


def _members_by_task(zf: zipfile.ZipFile, keys: set) -> dict:
    """校验全部成员并按任务分组: key -> [(ZipInfo, 相对路径)]。"""
    infos = zf.infolist()
    if len(infos) > MAX_FILES:
        raise ArchiveError("存档文件数过多")
    total = 0
    groups: dict = {k: [] for k in keys}
    for info in infos:
        name = info.filename
        if name == "manifest.json" or name.endswith("/"):
            continue
        mode = info.external_attr >> 16
        if mode and stat.S_ISLNK(mode):
            raise ArchiveError("存档含符号链接, 已拒绝")
        pp = PurePosixPath(name)
        parts = pp.parts
        if pp.is_absolute() or "\\" in name or any(p in ("..", "") for p in parts) \
                or len(parts) < 3 or parts[0] != "tasks" or parts[1] not in groups:
            raise ArchiveError(f"存档含非法路径: {name[:80]}")
        if parts[2] in SKIP_NAMES:
            continue
        total += info.file_size
        if total > MAX_BYTES:
            raise ArchiveError("存档解压后过大")
        if info.compress_size and info.file_size / max(1, info.compress_size) > MAX_RATIO \
                and info.file_size > 64 * 1024 * 1024:
            raise ArchiveError("存档压缩比异常, 已拒绝")
        groups[parts[1]].append((info, "/".join(parts[2:])))
    return groups


def import_archive(zip_path: Path, *, db, workspace_id: int, workspace_root: Path,
                   created_by: str, project_id=None, sanitize_config=None) -> dict:
    """导入到 workspace_id。project_id 给定则全部进该项目, 否则按存档里的项目名
    复用本区同名项目或新建。返回 {imported, skipped, task_ids, projects}。"""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        raise ArchiveError("文件不是有效的 zip") from None
    with zf:
        man = read_manifest(zf)
        tasks = [t for t in man["tasks"] if isinstance(t, dict)]
        keys = {str(t.get("key")) for t in tasks}
        groups = _members_by_task(zf, keys)

        todo, skipped = [], []
        for t in tasks:
            uid = str(t.get("uid") or "")[:120]
            url = str(t.get("target_url") or "")
            if not url.startswith(("http://", "https://")):
                raise ArchiveError("存档记录缺少有效的目标网址")
            hit = db.archive_task_for_uid(workspace_id, uid) if uid else None
            if hit:
                skipped.append(hit)
            else:
                todo.append(t)

        proj_map: dict = {}
        made_projects = []
        if todo and project_id is None:
            existing = {p["name"]: p["id"] for p in db.list_projects(workspace_id=workspace_id)}
            for p in man.get("projects") or []:
                if not isinstance(p, dict):
                    continue
                name = (str(p.get("name") or "").strip() or "导入的项目")[:60]
                if len(name) < 2:
                    name = "导入的项目"
                if name in existing:
                    proj_map[p.get("key")] = existing[name]
                else:
                    color = str(p.get("color") or "")
                    if not (len(color) == 7 and color.startswith("#")):
                        color = "#6366f1"
                    pid = db.create_project(name, str(p.get("description") or "")[:500], color,
                                            created_by=created_by, workspace_id=workspace_id)
                    existing[name] = pid
                    proj_map[p.get("key")] = pid
                    made_projects.append(pid)
        fallback_pid = project_id or db.ensure_default_project(workspace_id)
        batch_meta = {b.get("key"): b for b in (man.get("batches") or []) if isinstance(b, dict)}

        by_batch: dict = {}
        for t in todo:
            by_batch.setdefault(str(t.get("batch") or ""), []).append(t)

        new_ids = []
        exported_at = str(man.get("exported_at") or "")[:40]
        for bkey, items in by_batch.items():
            b = batch_meta.get(bkey) or {}
            cfg = b.get("config") if isinstance(b.get("config"), dict) else {}
            cfg = sanitize_config(cfg) if sanitize_config else dict(cfg)
            cfg = _clean_config(cfg)
            pid = project_id or proj_map.get(b.get("project")) or fallback_pid
            rows = []
            for t in items:
                status = t.get("status") if t.get("status") in KEEP_STATUS else "failed"
                stats = t.get("stats") if isinstance(t.get("stats"), dict) else {}
                stats = dict(stats)
                stats["archive"] = {"uid": str(t.get("uid") or "")[:120], "src_id": t.get("src_id"),
                                    "exported_at": exported_at,
                                    "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                score = t.get("similarity_score")
                try:
                    score = None if score is None else float(score)
                except (TypeError, ValueError):
                    score = None
                err = str(t.get("error_msg") or "")[:2000]
                if status == "failed" and t.get("status") not in KEEP_STATUS:
                    err = err or "导出时任务尚未完成"
                url = str(t.get("target_url"))[:2000]
                rows.append({"uid": stats["archive"]["uid"], "target_url": url,
                             "domain": str(t.get("domain") or "")[:255], "status": status,
                             "progress": 100 if status != "failed" else int(t.get("progress") or 0),
                             "stage": str(t.get("stage") or "")[:120], "similarity_score": score,
                             "stats_json": json.dumps(stats, ensure_ascii=False), "error_msg": err,
                             "created_at": str(t.get("created_at") or "")[:40],
                             "updated_at": str(t.get("updated_at") or "")[:40]})
            ids = db.insert_archived_batch(
                workspace_id=workspace_id, project_id=pid,
                name=(str(b.get("name") or "") or "存档导入")[:120], config=cfg,
                created_by=created_by, created_at=str(b.get("created_at") or "")[:40], tasks=rows)
            for t, tid in zip(items, ids):
                dest = workspace_root / str(tid)
                tmp = workspace_root / f".import_{tid}"
                shutil.rmtree(tmp, ignore_errors=True)
                try:
                    tmp.mkdir(parents=True)
                    for info, rel in groups.get(str(t.get("key")), []):
                        target = tmp / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst, 1024 * 1024)
                    shutil.rmtree(dest, ignore_errors=True)
                    tmp.rename(dest)
                except (OSError, zipfile.BadZipFile, EOFError) as exc:
                    shutil.rmtree(tmp, ignore_errors=True)
                    db.update_task(tid, status="failed", stage="导入",
                                   error_msg=f"存档文件写入失败: {exc}"[:500])
                    new_ids.append(tid)
                    continue
                db.update_task(tid, workspace_path=str(dest), zip_path="")
                new_ids.append(tid)
    return {"imported": len(new_ids), "skipped": len(skipped), "task_ids": new_ids,
            "skipped_ids": skipped, "projects_created": made_projects,
            "label": str(man.get("label") or "")[:120]}
