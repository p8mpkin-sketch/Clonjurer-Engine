"""交付产物部署性能守卫(ROADMAP: 交付物加载性能)。

根因复盘(cgnpc 562): 采集期已 404/被跳过的外链计数脚本(cmsHitsCount.js)与跨域埋点图标
仍以经典阻塞脚本留在交付 HTML。本地部署时它们本地无文件 → 走 Flask 反代兜底(旧 timeout 20s)
或直连远端慢主机, 在解析期阻塞, 首屏被拖到 20s+。评分口径一直量不到: 离线渲染对非本地请求
一律瞬时 abort, 交付产物却等满 20s。

举一反三(全语料 554 页复盘): 渲染阻塞的未本地化 <link rel=stylesheet> 59 处/45 站(脚本类的 4 倍),
阻塞的是首次绘制(白屏等超时); 已知死路径(favicon/缺图/计数接口)每次加载都回源白等。

本守卫是**无浏览器**的检查, 可在 CI 精简环境常驻运行:
  1. 去阻塞变换: 删埋点/计数信标, 其余未本地化脚本转 async; 未本地化样式表转异步加载;
     本地 /static 引用一字不改;
  2. 交付 HTML 不变量: 任何存活的未本地化 <script src>/<link stylesheet> 不得阻塞首屏;
  3. 部署审计 deploy_hazard_audit 对脏/净 HTML 的计数正确;
  4. 生成的 Flask 工程: 反代超时有界、threaded=True、已知死路径秒 404 不回源、文本 gzip。
"""
import gzip
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from core import flask_generator  # noqa: E402
from core.clone.support import (  # noqa: E402
    deploy_hazard_audit,
    fontface_swap_for_remote,
    neutralize_deploy_hints,
    neutralize_deploy_hazard_scripts,
    neutralize_deploy_hazard_styles,
    neutralize_deploy_hazards,
    _script_src_is_localized,
)

PAGE_HOST = "www.example.com"


class DeployHazardScripts(unittest.TestCase):
    def test_beacon_counter_scripts_removed(self):
        html = ('<head><script src="/static/js/app.js"></script>'
                '<script src="/cgnhit/js/cmsHitsCount.js"></script>'
                '<script src="https://stat.tracker.net/collect.js"></script>'
                '</head>')
        out = neutralize_deploy_hazard_scripts(html, PAGE_HOST)
        self.assertNotIn("cmsHitsCount.js", out)       # 计数脚本整条删除
        self.assertNotIn("collect.js", out)            # 埋点主机整条删除
        self.assertIn('src="/static/js/app.js"', out)  # 本地应用脚本保留

    def test_local_static_scripts_stay_blocking(self):
        html = '<script src="/static/js/a.js"></script><script src="/static/js/b.js"></script>'
        self.assertEqual(neutralize_deploy_hazard_scripts(html, PAGE_HOST), html)

    def test_relative_shadow_scripts_untouched(self):
        html = '<script src="cgn/chunk.123.js"></script>'
        self.assertEqual(neutralize_deploy_hazard_scripts(html, PAGE_HOST), html)

    def test_uncaptured_foreign_lib_becomes_async(self):
        html = '<script src="https://cdn.other.com/lib.min.js"></script>'
        out = neutralize_deploy_hazard_scripts(html, PAGE_HOST)
        self.assertIn("cdn.other.com/lib.min.js", out)
        self.assertRegex(out, r"<script[^>]*\basync")

    def test_root_relative_uncaptured_becomes_async(self):
        out = neutralize_deploy_hazard_scripts('<script src="/cgn/xhtml/js/common.js"></script>', PAGE_HOST)
        self.assertRegex(out, r"<script[^>]*\basync")

    def test_already_async_defer_not_double_tagged(self):
        html = '<script async src="https://cdn.x.com/a.js"></script>'
        self.assertEqual(neutralize_deploy_hazard_scripts(html, PAGE_HOST).count("async"), 1)


class DeployHazardStyles(unittest.TestCase):
    """渲染阻塞样式表(举一反三: 语料里比脚本类多 4 倍)。"""

    def test_uncaptured_root_relative_css_becomes_async(self):
        out = neutralize_deploy_hazard_styles('<link rel="stylesheet" href="/vp-icons.css">')
        self.assertIn('media="print"', out)
        self.assertIn("onload=\"this.media='all'\"", out)
        self.assertIn("data-clone-async-css", out)
        self.assertIn('href="/vp-icons.css"', out)   # 地址保留, 到了仍生效

    def test_uncaptured_cross_origin_css_becomes_async(self):
        out = neutralize_deploy_hazard_styles(
            '<link rel="stylesheet" href="https://github.githubassets.com/x.css">')
        self.assertIn('media="print"', out)

    def test_original_media_query_is_restored_on_load(self):
        html = '<link media="(max-width:767px)" href="https://cdn.x.com/m.css" rel="stylesheet">'
        out = neutralize_deploy_hazard_styles(html)
        self.assertEqual(out.count(' media="'), 1)                     # 原 media 属性被替换而非叠加
        self.assertIn("onload=\"this.media='(max-width:767px)'\"", out)  # 原查询在加载后还原

    def test_localized_static_css_untouched(self):
        html = ('<link rel="stylesheet" href="/static/css/a.css">'
                '<link media="(max-width:767px)" href="/static/css/m.css" rel="stylesheet">')
        self.assertEqual(neutralize_deploy_hazard_styles(html), html)

    def test_non_stylesheet_links_untouched(self):
        html = ('<link rel="icon" href="https://x.com/f.ico">'
                '<link rel="preconnect" href="https://fonts.gstatic.com">'
                '<link rel="alternate" media="handheld" href="#">')
        self.assertEqual(neutralize_deploy_hazard_styles(html), html)

    def test_idempotent(self):
        html = '<link rel="stylesheet" href="/vp-icons.css">'
        once = neutralize_deploy_hazard_styles(html)
        self.assertEqual(neutralize_deploy_hazard_styles(once), once)


class DeployAuditAndInvariant(unittest.TestCase):
    DIRTY = (
        "<html><head>"
        '<link rel="stylesheet" href="/static/css/a.css">'
        '<link rel="stylesheet" href="/vp-icons.css">'
        '<link rel="stylesheet" href="https://github.githubassets.com/x.css">'
        '<script src="/static/js/app.js"></script>'
        '<script type="text/javascript" src="/cgnhit/js/cmsHitsCount.js"></script>'
        '<script id="x" src="http://szcert.ebs.org.cn/govicon.js?id=1&width=36" charset="utf-8"></script>'
        '<script src="/cgn/xhtml/js/common.js"></script>'
        "</head><body></body></html>"
    )

    def test_audit_counts_dirty_html(self):
        a = deploy_hazard_audit(self.DIRTY, "www.cgnpc.com.cn")
        self.assertEqual(a["blocking_scripts"], 3)
        self.assertEqual(a["blocking_styles"], 2)
        self.assertEqual(a["uncaptured_refs"], 5)
        self.assertIn("github.githubassets.com", a["external_hosts"])
        self.assertIn("szcert.ebs.org.cn", a["external_hosts"])

    def test_delivered_html_invariant_after_neutralize(self):
        out = neutralize_deploy_hazards(self.DIRTY, "www.cgnpc.com.cn")
        a = deploy_hazard_audit(out, "www.cgnpc.com.cn")
        self.assertEqual((a["blocking_scripts"], a["blocking_styles"]), (0, 0),
                         "交付 HTML 不得残留会阻塞首屏的未本地化脚本/样式")
        self.assertIn('src="/static/js/app.js"', out)            # 本地脚本仍同步
        self.assertIn('<link rel="stylesheet" href="/static/css/a.css">', out)  # 本地样式原样

    def test_localized_classifier(self):
        for s, exp in [("/static/js/x.js", True), ("/cgnhit/js/x.js", False),
                       ("http://a.com/b.js", False), ("//cdn/x.js", False),
                       ("cgn/a/b.js", True), ("data:text/css,", True), ("", True)]:
            self.assertEqual(_script_src_is_localized(s), exp, s)


class DeployHintsAndFonts(unittest.TestCase):
    """资源提示/外部 iframe/远程字体(全语料: preload 275 处、preconnect 1010 处、跨域 iframe 168 处、
    远程字体切片 10 万条)。"""

    def test_uncaptured_preload_and_all_preconnect_removed(self):
        html = ('<link rel="preload" as="script" href="https://cdn.x.com/a.js">'
                '<link rel="modulepreload" href="/_next/chunk.js">'
                '<link rel="preconnect" href="https://fonts.gstatic.com">'
                '<link rel="dns-prefetch" href="//x.com">')
        self.assertEqual(neutralize_deploy_hints(html), "")

    def test_localized_preload_and_stylesheet_kept(self):
        html = ('<link rel="preload" as="style" href="/static/css/a.css">'
                '<link rel="stylesheet" href="/static/css/a.css">'
                '<link rel="icon" href="/static/img/f.ico">')
        self.assertEqual(neutralize_deploy_hints(html), html)

    def test_external_iframe_lazy_local_iframe_untouched(self):
        html = '<iframe src="https://www.youtube.com/embed/x" width="1"></iframe><iframe src="/static/misc/l.html"></iframe>'
        out = neutralize_deploy_hints(html)
        self.assertIn('<iframe src="https://www.youtube.com/embed/x" width="1" loading="lazy">', out)
        self.assertIn('<iframe src="/static/misc/l.html">', out)
        self.assertEqual(neutralize_deploy_hints(out), out)   # 幂等

    def test_fontface_swap_only_for_remote_without_display(self):
        css = ('@font-face{font-family:A;src:url(https://cdn.x.com/f.woff2) format("woff2")}'
               '@font-face{font-family:B;src:url(/static/fonts/b.woff2);}'
               '@font-face{font-family:C;font-display:optional;src:url(https://x/c.woff)}')
        out = fontface_swap_for_remote(css)
        self.assertIn('format("woff2");font-display:swap;}', out)       # 远程且未声明 → 补 swap
        self.assertIn('@font-face{font-family:B;src:url(/static/fonts/b.woff2);}', out)  # 本地不动
        self.assertEqual(out.count("font-display"), 2)                  # 已声明 optional 的不覆盖
        self.assertEqual(fontface_swap_for_remote(out), out)            # 幂等

    def test_inline_style_fontface_swapped_via_total_entry(self):
        html = '<style>@font-face{font-family:A;src:url(https://cdn.x.com/f.woff2)}</style>'
        out = neutralize_deploy_hazards(html, PAGE_HOST)
        self.assertIn("font-display:swap", out)


class GeneratedFlaskPerf(unittest.TestCase):
    """生成 app.py 的部署性能配置: 模板不变量 + 真实生成工程用 test_client 实测。"""

    def test_proxy_timeout_is_bounded_and_not_twenty(self):
        self.assertLessEqual(config.PROXY_FALLBACK_READ_TIMEOUT, 10, "反代读超时须远小于旧 20s")
        self.assertLessEqual(config.PROXY_FALLBACK_CONNECT_TIMEOUT, 5)
        src = flask_generator.GENERATED_APP_PY
        self.assertNotIn("timeout=20", src)
        self.assertIn("@@PROXY_CONNECT_TIMEOUT@@", src)
        self.assertIn("@@PROXY_READ_TIMEOUT@@", src)

    def test_dev_server_is_threaded(self):
        self.assertRegex(flask_generator.GENERATED_APP_PY, r"app\.run\([^)]*threaded\s*=\s*True")

    def test_generated_app_dead_paths_gzip_cache(self):
        """真实生成工程: 已知死路径秒 404 不回源; 文本 gzip; 静态长缓存。"""
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"
            (ws / "static" / "css").mkdir(parents=True)
            (ws / "static" / "css" / "a.css").write_text("body{color:red}" * 200, encoding="utf-8")
            (ws / "index.html").write_text(
                "<html><head><link rel=stylesheet href=/static/css/a.css></head><body>"
                + "x" * 3000 + "</body></html>", encoding="utf-8")
            (ws / "known_dead.json").write_text(
                json.dumps(["/cgnhit/js/cmsHitsCount.js", "/favicon.ico"]), encoding="utf-8")
            old_exports = config.EXPORTS_DIR
            config.EXPORTS_DIR = Path(d) / "exports"
            try:
                # 上游指向黑洞地址: 若死路径仍回源, 这里会等到连接超时而非秒回
                flask_generator.generate_project(7, "http://10.255.255.1", ws,
                                                 {"/": "index.html"}, [], True)
            finally:
                config.EXPORTS_DIR = old_exports
            app_dir = ws / "flask_app"
            self.assertTrue((app_dir / "known_dead.json").is_file(), "known_dead.json 须随工程落盘")
            spec = importlib.util.spec_from_file_location("gen_app_t", app_dir / "app.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["gen_app_t"] = mod
            spec.loader.exec_module(mod)
            try:
                self.assertEqual(mod.DEAD_PATHS, {"/cgnhit/js/cmsHitsCount.js", "/favicon.ico"})
                c = mod.app.test_client()
                import time
                t = time.time()
                r = c.get("/cgnhit/js/cmsHitsCount.js")
                self.assertEqual(r.status_code, 404)
                self.assertIsNone(r.headers.get("X-Clone-Proxy"), "死路径不得走反代")
                self.assertLess(time.time() - t, 1.0, "死路径须秒 404(不回源)")
                self.assertEqual(c.get("/favicon.ico").status_code, 404)
                plain = c.get("/")
                gz = c.get("/", headers={"Accept-Encoding": "gzip"})
                self.assertEqual(gz.headers.get("Content-Encoding"), "gzip")
                self.assertLess(len(gz.data), len(plain.data))
                self.assertEqual(gzip.decompress(gz.data), plain.data)
                self.assertIn("Accept-Encoding", gz.headers.get("Vary", ""))
                css = c.get("/static/css/a.css", headers={"Accept-Encoding": "gzip"})
                self.assertEqual(css.headers.get("Content-Encoding"), "gzip")
                self.assertIn("max-age", css.headers.get("Cache-Control", ""))
                self.assertEqual(c.get("/__healthz").status_code, 200)
                # 小响应不压(得不偿失)
                self.assertIsNone(c.get("/__healthz", headers={"Accept-Encoding": "gzip"})
                                  .headers.get("Content-Encoding"))
            finally:
                sys.modules.pop("gen_app_t", None)


if __name__ == "__main__":
    unittest.main()
