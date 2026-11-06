"""测试环境探测: CI 的 engine-gate 只装评分路径依赖(pillow/numpy/requests), 需要浏览器或平台依赖的测试据此跳过。"""
import importlib.util
from functools import lru_cache


@lru_cache(maxsize=1)
def chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


def has_modules(*names) -> bool:
    return all(importlib.util.find_spec(n) is not None for n in names)
