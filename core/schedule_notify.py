"""定时任务跑完后的变化通知: 汇总一次定时批次里各站点「与上次相比」的变化, 推到 webhook。

- 汇总(summarize): 逐任务读 stats.change_since_last(克隆引擎完成时已算好的确定性像素比较),
  有区域框或变化面积 ≥ NEAR_SAME 视为「有变化」; 失败的任务单列。
- 何时推送(should_notify): notify=change 只在有变化或有失败时推; always 每次都推; off 不推。
- 消息格式(build_payload): 按 webhook 主机名自动适配飞书 / 钉钉 / 企业微信 / Slack, 其余发通用 JSON
  (含 text 摘要与结构化 sites 明细)。
- 发送(send): 只发 http(s), 拒绝元数据 / 链路本地地址, 10 秒超时, 不跟随重定向。
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit

import requests

from core import diff_regions, websec

NOTIFY_MODES = ("change", "always", "off")
TIMEOUT = 10
MAX_URL_LEN = 1000
_FINISHED = ("completed", "failed", "cancelled")


def validate_webhook(url: Any) -> str:
    """校验 webhook 地址; 空串表示不通知。不合法抛 ValueError(信息给用户看)。"""
    url = str(url or "").strip()
    if not url:
        return ""
    if len(url) > MAX_URL_LEN or any(ch.isspace() for ch in url):
        raise ValueError("通知地址格式不对")
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        raise ValueError("通知地址格式不对") from None
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("通知地址必须以 http:// 或 https:// 开头")
    why = websec.forbidden_target(host)
    if why:
        raise ValueError(f"通知地址不可用: {why}")
    return url


def task_changed(stats: dict) -> Optional[dict]:
    """任务的「与上次相比」: 没有上次可比返回 None; 否则返回 {changed, verdict, area_pct, regions}。"""
    ch = (stats or {}).get("change_since_last")
    if not isinstance(ch, dict) or not ch.get("prev_task_id"):
        return None
    regions = ch.get("regions") or []
    area = float(ch.get("area_pct") or 0)
    return {"changed": bool(regions) or area >= diff_regions.NEAR_SAME,
            "verdict": ch.get("verdict") or "", "area_pct": round(area, 2), "regions": len(regions),
            "prev_task_id": ch.get("prev_task_id")}


def summarize(tasks: list[dict]) -> Optional[dict]:
    """一个批次的任务 -> 变化汇总; 还有任务没跑完返回 None(下次再看)。

    tasks 每项需含 id / target_url / status / similarity_score / stats(dict) / error_msg。"""
    if not tasks or any(t.get("status") not in _FINISHED for t in tasks):
        return None
    sites = []
    for t in tasks:
        item = {"task_id": t["id"], "url": t.get("target_url") or "", "status": t.get("status"),
                "score": t.get("similarity_score"), "changed": False, "verdict": "", "area_pct": None,
                "first_run": False}
        if t.get("status") == "completed":
            ch = task_changed(t.get("stats") or {})
            if ch is None:
                item["first_run"] = True
                item["verdict"] = "首次克隆, 没有上次可比"
            else:
                item.update(changed=ch["changed"], verdict=ch["verdict"], area_pct=ch["area_pct"],
                            prev_task_id=ch["prev_task_id"])
        else:
            item["verdict"] = "克隆失败" + (f": {str(t.get('error_msg'))[:120]}" if t.get("error_msg") else "")
        sites.append(item)
    return {"total": len(sites), "changed": sum(1 for s in sites if s["changed"]),
            "failed": sum(1 for s in sites if s["status"] != "completed"), "sites": sites}


def should_notify(mode: str, summary: dict) -> bool:
    if mode == "always":
        return True
    if mode == "change":
        return bool(summary["changed"] or summary["failed"])
    return False


def summary_text(schedule_name: str, summary: dict, link: str = "") -> str:
    head = f"【定时任务】{schedule_name}: {summary['total']} 个站点"
    parts = []
    if summary["changed"]:
        parts.append(f"{summary['changed']} 个有变化")
    if summary["failed"]:
        parts.append(f"{summary['failed']} 个失败")
    head += (", " + ", ".join(parts)) if parts else ", 都没有明显变化"
    lines = [head]
    for s in summary["sites"]:
        if s["changed"] or s["status"] != "completed" or len(summary["sites"]) <= 5:
            lines.append(f"- {s['url']}  {s['verdict'] or '与上次相比几乎没有变化'}")
    if link:
        lines.append(link)
    return "\n".join(lines)


def build_payload(webhook_url: str, schedule: dict, batch_id: int, summary: dict, link: str = "") -> dict:
    text = summary_text(schedule.get("name") or "", summary, link)
    host = (urlsplit(webhook_url).hostname or "").lower()
    if host.endswith("feishu.cn") or host.endswith("larksuite.com"):
        return {"msg_type": "text", "content": {"text": text}}
    if host.endswith("dingtalk.com"):
        return {"msgtype": "text", "text": {"content": text}}
    if host == "qyapi.weixin.qq.com":
        return {"msgtype": "text", "text": {"content": text}}
    if host.endswith("hooks.slack.com"):
        return {"text": text}
    return {"event": "schedule.run_finished", "text": text,
            "schedule": {"id": schedule.get("id"), "name": schedule.get("name")},
            "batch_id": batch_id, "total": summary["total"], "changed": summary["changed"],
            "failed": summary["failed"], "sites": summary["sites"], "link": link}


def send(webhook_url: str, payload: dict) -> tuple[bool, str]:
    """发一次; 返回 (成功, 说明)。不抛异常。"""
    try:
        url = validate_webhook(webhook_url)
    except ValueError as exc:
        return False, str(exc)
    if not url:
        return False, "未配置通知地址"
    try:
        resp = requests.post(url, json=payload, timeout=TIMEOUT, allow_redirects=False,
                             headers={"User-Agent": "Clonjurer-Schedule/1.0"})
    except requests.Timeout:
        return False, f"超时({TIMEOUT}s)"
    except requests.RequestException as exc:
        return False, f"网络错误: {type(exc).__name__}"
    if 200 <= resp.status_code < 300:
        return True, f"HTTP {resp.status_code}"
    return False, f"HTTP {resp.status_code}"
