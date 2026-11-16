"""大模型调用计量: 每一次发往上游的 /chat/completions 请求都在这里记一笔(token、模型、任务、发起人、估算费用)。

为什么在请求层记账而不是在各功能里各记各的:
- 一次功能调用常常发出多次请求(瞬时网络错误重试、max_tokens 升档、去掉不被接受的字段重发), 每次都按 token 计费;
  只记最后一次成功的 usage 会系统性少算;
- 失败的任务同样花了钱, 以前界面生成只在任务成功打包时落库, 失败的整次消耗都不见了。

归属(哪个任务、谁发起、算哪类功能)走两条路, 互为补充:
- 线程上下文 scope(): 调度器在执行任务的线程里打开, 手动触发的接口在自己的后台线程里打开;
- ai_cfg 上的 "_usage" 标签 tag(): 生成管线里有好几处线程池(并行读图、多稿并行), 线程上下文传不过去,
  但 ai_cfg 会一路传到每次请求, 所以 resolve 出 ai_cfg 时顺手把当前上下文贴上。
请求层取二者合并(线程上下文优先), 再加上当前步骤 step()。

记账失败绝不影响调用本身: SINK 抛什么都吞掉。"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager

# (rec: dict) -> None; 由 app 启动时注册(写 token_usage 表)。未注册时(单测/离线脚本)只算不记。
SINK = None

# 功能类别: 落库的 kind 与界面上的中文名
KINDS = {
    "generate": "界面生成",
    "ai_repair": "视觉修复",
    "ai_mock": "Mock 润色",
    "ai_test": "模型测试",
    "clone": "克隆",
    "other": "其他",
}

# 单价表: 每百万 token 的价格(输入 / 输出), 货币见 CURRENCY。match 为模型名前缀(不区分大小写), 取最长匹配。
# 这是参考价, 供应商随时会调价, 以账单为准; 管理员可在「模型消耗 → 单价表」里改, 改后存进系统设置。
DEFAULT_CURRENCY = "¥"
DEFAULT_PRICES: list = [
    {"match": "deepseek", "input": 2.0, "output": 3.0},
    {"match": "gpt-4o-mini", "input": 1.1, "output": 4.4},
    {"match": "gpt-4o", "input": 18.0, "output": 72.0},
    {"match": "gpt-4.1-mini", "input": 2.9, "output": 11.5},
    {"match": "gpt-4.1", "input": 14.4, "output": 57.6},
]

_prices: list = [dict(p) for p in DEFAULT_PRICES]
_currency = DEFAULT_CURRENCY
_tl = threading.local()


# ---------------------------------------------------------------------------
# 单价
# ---------------------------------------------------------------------------
def normalize_prices(raw) -> list:
    """清洗单价表(来自设置页): 去空行、去重(后写的覆盖)、价格非负、最多 100 条。非法输入抛 ValueError。"""
    if not isinstance(raw, list):
        raise ValueError("单价表应为列表")
    out: dict = {}
    for row in raw[:100]:
        if not isinstance(row, dict):
            raise ValueError("单价表每一行应为对象")
        match = str(row.get("match") or "").strip().lower()[:80]
        if not match:
            continue
        try:
            pin, pout = float(row.get("input") or 0), float(row.get("output") or 0)
        except (TypeError, ValueError):
            raise ValueError(f"「{match}」的单价不是数字")
        if pin < 0 or pout < 0 or pin > 1e6 or pout > 1e6:
            raise ValueError(f"「{match}」的单价超出范围")
        out[match] = {"match": match, "input": round(pin, 6), "output": round(pout, 6)}
    return sorted(out.values(), key=lambda p: p["match"])


def set_prices(prices=None, currency: str = None):
    global _prices, _currency
    if prices is not None:
        _prices = normalize_prices(prices)
    if currency is not None:
        _currency = (str(currency).strip() or DEFAULT_CURRENCY)[:4]


def load_saved(raw_json: str):
    """启动时从系统设置恢复: {"currency": "¥", "prices": [...]}; 坏数据保留默认表。"""
    if not raw_json:
        return
    try:
        data = json.loads(raw_json)
        set_prices(data.get("prices"), data.get("currency"))
    except (ValueError, TypeError, AttributeError):
        pass


def prices() -> list:
    return [dict(p) for p in _prices]


def currency() -> str:
    return _currency


def price_for(model: str):
    """模型名 → 单价行(最长前缀匹配); 没有匹配返回 None(界面显示「未定价」)。"""
    m = (model or "").strip().lower()
    if not m:
        return None
    # 带供应商前缀的写法(openai/gpt-4o、deepseek/deepseek-chat)也认
    names = {m, m.rsplit("/", 1)[-1]}
    best = None
    for p in _prices:
        if any(n.startswith(p["match"]) for n in names) and (best is None or len(p["match"]) > len(best["match"])):
            best = p
    return best


def estimate_cost(model: str, tokens_in: int, tokens_out: int):
    """估算费用; 模型未定价返回 None(与「免费」区分开)。"""
    p = price_for(model)
    if p is None:
        return None
    return round((int(tokens_in or 0) * p["input"] + int(tokens_out or 0) * p["output"]) / 1_000_000, 6)


# ---------------------------------------------------------------------------
# 归属上下文
# ---------------------------------------------------------------------------
_FIELDS = ("task_id", "kind", "username", "workspace_id", "project_id")


def _stack() -> list:
    st = getattr(_tl, "stack", None)
    if st is None:
        st = _tl.stack = []
    return st


def current() -> dict:
    """当前线程的归属(外层到内层依次合并, 内层覆盖外层)。"""
    out: dict = {}
    for layer in _stack():
        out.update(layer)
    return out


@contextmanager
def scope(**kw):
    """在当前线程上标注后续模型调用的归属: task_id / kind / username / workspace_id / project_id。值为 None 的键忽略。"""
    layer = {k: v for k, v in kw.items() if k in _FIELDS and v not in (None, "")}
    st = _stack()
    st.append(layer)
    try:
        yield
    finally:
        st.pop()


@contextmanager
def step(name: str):
    """当前线程上后续调用记在哪一步(规划/写页面/验收/视觉修复第 N 轮…)。"""
    prev = getattr(_tl, "step", "")
    _tl.step = str(name or "")[:40]
    try:
        yield
    finally:
        _tl.step = prev


def tag(ai_cfg, **kw):
    """返回贴了归属标签的 ai_cfg 副本: 已有标签 < 当前线程上下文 < 显式参数。ai_cfg 为空原样返回。"""
    if not ai_cfg:
        return ai_cfg
    ctx = dict(ai_cfg.get("_usage") or {})
    ctx.update(current())
    ctx.update({k: v for k, v in kw.items() if k in _FIELDS and v not in (None, "")})
    if not ctx:
        return ai_cfg
    out = dict(ai_cfg)
    out["_usage"] = ctx
    return out


def tokens_of(usage) -> tuple:
    """上游 usage → (输入, 输出)。兼容 OpenAI(prompt/completion_tokens)与 Anthropic 风格(input/output_tokens)。"""
    u = usage if isinstance(usage, dict) else {}
    tin = u.get("prompt_tokens", u.get("input_tokens")) or 0
    tout = u.get("completion_tokens", u.get("output_tokens")) or 0
    try:
        return max(0, int(tin)), max(0, int(tout))
    except (TypeError, ValueError):
        return 0, 0


def record(ai_cfg, usage, step_name: str = None, kind: str = None, **extra) -> dict:
    """记一次上游请求。usage 为上游返回的 usage(没有也记一次调用, token 为 0)。返回写出的记录(测试用)。"""
    cfg = ai_cfg or {}
    ctx = dict(cfg.get("_usage") or {})
    ctx.update(current())
    ctx.update({k: v for k, v in extra.items() if k in _FIELDS and v not in (None, "")})
    if kind:
        ctx["kind"] = kind
    tin, tout = tokens_of(usage)
    model = str(cfg.get("model") or "")[:80]
    rec = {
        "task_id": ctx.get("task_id"),
        "kind": ctx.get("kind") or "other",
        "username": str(ctx.get("username") or "")[:64],
        "workspace_id": ctx.get("workspace_id"),
        "project_id": ctx.get("project_id"),
        "model": model,
        "step": str(step_name if step_name is not None else getattr(_tl, "step", "") or "")[:40],
        "tokens_in": tin,
        "tokens_out": tout,
        "cost": estimate_cost(model, tin, tout),
    }
    sink = SINK
    if sink is not None:
        try:
            sink(rec)
        except Exception:  # noqa: BLE001 - 记账失败不影响调用
            pass
    return rec
