"""录制接口的"请求指纹"与回放挑选(纯函数, 无第三方依赖; 生成的 Flask 工程内嵌本文件源码)。

背景: 录制/回放原先只认 (方法, 路径, 查询串)。门户类页面常把全页区块都打到同一个 POST 接口,
靠请求体里的键区分区块, 并且带每次变化的请求号/时间戳 —— 只按路径回放时所有区块拿到同一份
响应。这里把请求(查询参数 + 请求体)摊平成"键=值"对的集合作指纹, 回放时在同方法同路径的多条
录制里挑与当前请求重合对数最多的一条; 每次都变的字段在任何录制里都对不上, 自然不参与。
"""
import json
from urllib.parse import parse_qsl

MAX_PAIRS = 160        # 单条录制保留的键值对上限
MAX_VALUE_LEN = 96     # 值截断(长文本/令牌不值得存)
MAX_BODY_BYTES = 32768  # 超过的请求体不解析(上传类)


def _flat(node, prefix, out):
    if len(out) >= MAX_PAIRS:
        return
    if isinstance(node, dict):
        for k in sorted(node, key=str):
            _flat(node[k], f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(node, list):
        # 列表不带下标: 同一个键换了位置仍算同一对
        for item in node[:40]:
            _flat(item, prefix + "[]", out)
    else:
        val = "" if node is None else str(node)
        out.append(f"{prefix}={val[:MAX_VALUE_LEN]}")


def request_pairs(query="", body=""):
    """请求 → 有序去重的"键=值"对列表。查询参数前缀 q:, 请求体前缀 b:。"""
    out = []
    for k, v in parse_qsl(query or "", keep_blank_values=True)[:MAX_PAIRS]:
        out.append(f"q:{k}={v[:MAX_VALUE_LEN]}")
    if isinstance(body, bytes):
        body = body[:MAX_BODY_BYTES].decode("utf-8", "ignore")
    body = (body or "").strip()
    if body and len(body) <= MAX_BODY_BYTES:
        flat = []
        if body[:1] in "{[":
            try:
                _flat(json.loads(body), "", flat)
            except ValueError:
                flat = []
        if not flat and "=" in body and "\n" not in body:
            flat = [f"{k}={v[:MAX_VALUE_LEN]}" for k, v in parse_qsl(body, keep_blank_values=True)[:MAX_PAIRS]]
        if not flat:
            flat = ["raw=" + body[:MAX_VALUE_LEN]]
        out.extend("b:" + p for p in flat)
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq[:MAX_PAIRS]


def pick_entry(candidates, query="", body=""):
    """在同方法同路径的多条录制里挑与当前请求最像的一条。

    只有一条候选、或候选都没有指纹(旧录制)时返回第一条 —— 与原先"先到先得"的行为一致。
    分数 = 重合的键值对数; 平分时取"多余对"更少的(更具体的那条), 再平分取录制顺序靠前的。"""
    cands = list(candidates)
    if not cands:
        return None
    if len(cands) == 1 or not any(c.get("req_sig") for c in cands):
        return cands[0]
    want = set(request_pairs(query, body))
    best, best_key = cands[0], None
    for idx, c in enumerate(cands):
        sig = set(c.get("req_sig") or ())
        key = (len(want & sig), -len(sig - want), -idx)
        if best_key is None or key > best_key:
            best, best_key = c, key
    return best
