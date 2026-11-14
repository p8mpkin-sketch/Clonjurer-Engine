"""界面生成: 模型正在写的代码的实时缓冲(进程内)。

codegen 在调用模型写页面 / 修复时以流式回调把增量写进这里, Web 接口按任务工作区读取增量,
任务卡与详情页据此实时展示「代码正在写入」。纯展示用途: 任何异常都不影响生成本身。

一个工作区一条通道; 每次新的模型调用(写页面、修复、或同一调用升档重试)epoch 加一, 前端据此清屏重放。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_CHANNELS: dict = {}
_CAP = 400_000          # 单条通道最多保留的字符数(整页 HTML 通常 2–4 万字符)


def _key(ws) -> str:
    try:
        return str(Path(ws).resolve())
    except Exception:
        return str(ws)


class Channel:
    def __init__(self, ws):
        self.key = _key(ws)

    def begin(self, label: str):
        with _LOCK:
            ch = _CHANNELS.setdefault(self.key, {"epoch": 0})
            ch.update(epoch=ch["epoch"] + 1, label=label, code=[], code_len=0, think=[], think_len=0,
                      done=False, updated=time.time())

    def feed(self, kind: str, text: str):
        if kind == "start":                 # 同一调用升档重试: 从头再写
            with _LOCK:
                ch = _CHANNELS.get(self.key)
                label = ch.get("label", "") if ch else ""
            self.begin(label)
            return
        if not text:
            return
        with _LOCK:
            ch = _CHANNELS.get(self.key)
            if ch is None:
                return
            if kind == "reasoning":
                if ch["think_len"] < _CAP:
                    ch["think"].append(text)
                    ch["think_len"] += len(text)
            elif ch["code_len"] < _CAP:
                ch["code"].append(text)
                ch["code_len"] += len(text)
            ch["updated"] = time.time()

    def done(self):
        with _LOCK:
            ch = _CHANNELS.get(self.key)
            if ch is not None:
                ch["done"] = True
                ch["updated"] = time.time()

    def close(self):
        with _LOCK:
            _CHANNELS.pop(self.key, None)


def read(ws, epoch: int = 0, since: int = 0, think_since: int = 0) -> dict:
    """读取增量。epoch 与当前不一致时从头返回(reset=True)。没有通道返回 {"active": False}。"""
    with _LOCK:
        ch = _CHANNELS.get(_key(ws))
        if ch is None:
            return {"active": False}
        if "code" not in ch:
            return {"active": True, "epoch": ch["epoch"], "label": "", "code": "", "think": "", "code_len": 0,
                    "think_len": 0, "done": False, "reset": True}
        reset = int(epoch or 0) != ch["epoch"]
        if reset:
            since = think_since = 0
        code = "".join(ch["code"])
        think = "".join(ch["think"])
        ch["code"], ch["think"] = [code], [think]          # 合并分片, 后续读取更快
        since = max(0, min(int(since or 0), len(code)))
        think_since = max(0, min(int(think_since or 0), len(think)))
        return {"active": True, "epoch": ch["epoch"], "label": ch.get("label", ""), "reset": reset,
                "code": code[since:], "code_len": len(code), "think": think[think_since:],
                "think_len": len(think), "done": ch["done"], "updated": ch["updated"]}
