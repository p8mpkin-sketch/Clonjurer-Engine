"""交付产物部署性能守卫(ROADMAP: 交付物加载性能)。

根因复盘(cgnpc 562): 采集期已 404/被跳过的外链计数脚本(cmsHitsCount.js)与跨域埋点图标
仍以经典阻塞脚本留在交付 HTML。本地部署时它们本地无文件 → 走 Flask 反代兜底(旧 timeout 20s)
或直连远端慢主机, 在解析期阻塞, 首屏被拖到 20s+。评分口径一直量不到: 离线渲染对非本地请求
一律瞬时 abort, 交付产物却等满 20s。

本守卫是**无浏览器**的静态不变量检查, 可在 CI 精简环境常驻运行:
  1. 去阻塞变换: 删埋点/计数信标, 其余未本地化脚本转 async, 本地 /static 脚本不动;
  2. 交付 HTML 不变量: 任何存活的 <script src> 要么已本地化, 要么 async/defer(永不阻塞首屏);
  3. 生成的 Flask 工程: 反代超时有界(远小于旧 20s)、开发服务器 threaded=True。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from core import flask_generator  # noqa: E402
from core.clone.support import (  # noqa: E402
    neutralize_deploy_hazard_scripts,
    _script_src_is_localized,
)

_SCRIPT_SRC = re.compile(
    r"<script\b(?P<attrs>[^>]*?)\bsrc\s*=\s*([\"'])(?P<src>.*?)\2(?P<rest>[^>]*)>",
    re.I | re.S)


def _surviving_blocking_foreign_scripts(html: str):
    """返回仍会阻塞首屏且未本地化的 <script src>(应为空)。"""
    bad = []
    for m in _SCRIPT_SRC.finditer(html):
        src = m.group("src")
        if _script_src_is_localized(src):
            continue
        flags = (m.group("attrs") + " " + m.group("rest")).lower()
        if "async" in flags or "defer" in flags:
            continue
        bad.append(src)
    return bad


class DeployHazardScripts(unittest.TestCase):
    PAGE_HOST = "www.example.com"

    def test_beacon_counter_scripts_removed(self):
        html = ('<head><script src="/static/js/app.js"></script>'
                '<script src="/cgnhit/js/cmsHitsCount.js"></script>'
                '<script src="https://stat.tracker.net/collect.js"></script>'
                '</head>')
        out = neutralize_deploy_hazard_scripts(html, self.PAGE_HOST)
        self.assertNotIn("cmsHitsCount.js", out)       # 计数脚本整条删除
        self.assertNotIn("collect.js", out)            # 埋点主机整条删除
        self.assertIn('src="/static/js/app.js"', out)  # 本地应用脚本保留

    def test_local_static_scripts_stay_blocking(self):
        html = '<script src="/static/js/a.js"></script><script src="/static/js/b.js"></script>'
        out = neutralize_deploy_hazard_scripts(html, self.PAGE_HOST)
        self.assertEqual(out, html)  # 已本地化: 一字不改, 保持同步顺序

    def test_relative_shadow_scripts_untouched(self):
        # 页面目录相对路径(影子文件随包落盘)不算危险
        html = '<script src="cgn/chunk.123.js"></script>'
        out = neutralize_deploy_hazard_scripts(html, self.PAGE_HOST)
        self.assertEqual(out, html)

    def test_uncaptured_foreign_lib_becomes_async(self):
        # 跨域非埋点脚本(可能是未采到的库): 不删除, 转 async, 永不阻塞首屏
        html = '<script src="https://cdn.other.com/lib.min.js"></script>'
        out = neutralize_deploy_hazard_scripts(html, self.PAGE_HOST)
        self.assertIn("cdn.other.com/lib.min.js", out)
        self.assertRegex(out, r"<script[^>]*\basync")

    def test_root_relative_uncaptured_becomes_async(self):
        html = '<script src="/cgn/xhtml/js/common.js"></script>'
        out = neutralize_deploy_hazard_scripts(html, self.PAGE_HOST)
        self.assertRegex(out, r"<script[^>]*\basync")

    def test_already_async_defer_not_double_tagged(self):
        html = '<script async src="https://cdn.x.com/a.js"></script>'
        out = neutralize_deploy_hazard_scripts(html, self.PAGE_HOST)
        self.assertEqual(out.count("async"), 1)

    def test_delivered_html_invariant_no_blocking_foreign_script(self):
        # 一张"脏"交付 HTML: 本地脚本 + 死外链计数 + 跨域库 + 根相对未捕获
        html = (
            "<html><head>"
            '<script src="/static/js/app.js"></script>'
            '<script type="text/javascript" src="/cgnhit/js/cmsHitsCount.js"></script>'
            '<script id="x" src="http://szcert.ebs.org.cn/govicon.js?id=1&width=36" charset="utf-8"></script>'
            '<script src="/cgn/xhtml/js/common.js"></script>'
            "</head><body></body></html>"
        )
        out = neutralize_deploy_hazard_scripts(html, "www.cgnpc.com.cn")
        self.assertEqual(_surviving_blocking_foreign_scripts(out), [],
                         "交付 HTML 不得残留会阻塞首屏的未本地化外链脚本")
        self.assertIn('src="/static/js/app.js"', out)  # 本地脚本仍同步


class GeneratedFlaskPerf(unittest.TestCase):
    """生成 app.py 模板的部署性能配置不变量(不实际起服务)。"""

    def setUp(self):
        self.src = flask_generator.GENERATED_APP_PY

    def test_proxy_timeout_is_bounded_and_not_twenty(self):
        connect = config.PROXY_FALLBACK_CONNECT_TIMEOUT
        read = config.PROXY_FALLBACK_READ_TIMEOUT
        self.assertLessEqual(read, 10, "反代读超时须远小于旧 20s")
        self.assertLessEqual(connect, 5)
        # 模板里是令牌, 渲染后才是数值 —— 确认旧硬编码 20 已不在
        self.assertNotIn("timeout=20", self.src)
        self.assertIn("@@PROXY_CONNECT_TIMEOUT@@", self.src)
        self.assertIn("@@PROXY_READ_TIMEOUT@@", self.src)

    def test_dev_server_is_threaded(self):
        # 单线程下一个慢回源会阻塞排队的所有静态资源 → 必须多线程
        self.assertRegex(self.src, r"app\.run\([^)]*threaded\s*=\s*True")


if __name__ == "__main__":
    unittest.main()
