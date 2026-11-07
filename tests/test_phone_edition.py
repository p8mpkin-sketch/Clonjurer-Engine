"""手机版(按 UA 分发的站): 判定 + 端到端(手机 UA 打开克隆落到手机版, 桌面 UA 不受影响)。"""
import shutil
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
from core.clone.support import (EDITION_ROUTER_SCRIPT, dom_signature, inject_edition_router,
                                separate_phone_edition)
from tests._env import chromium_available, has_modules

DESKTOP = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Desk</title>
<link rel="stylesheet" href="/css/d.css"></head><body>
<div class="topbar"><ul class="nav-list"><li class="nav-item">News</li><li class="nav-item">Sports</li></ul></div>
<div class="layout"><div class="col-left"><div class="news-block"><h2 class="block-title">Desktop headline</h2>
<ul class="news-list"><li class="news-item">Desktop story one</li><li class="news-item">Desktop story two</li></ul></div></div>
<div class="col-right"><div class="ad-slot"></div><table class="rank"><tr><td class="rank-cell">1</td></tr></table></div></div>
<div class="footer-links"><a class="flink">About</a></div></body></html>"""
PHONE = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phone</title><link rel="stylesheet" href="/m/m.css"></head><body>
<header class="m-header"><span class="m-logo">LOGO</span></header>
<section class="m-feed"><article class="m-card"><img class="m-thumb" src="/m/t.png" width="120" height="80">
<p class="m-title">Phone headline card</p></article><article class="m-card"><p class="m-title">Phone story two</p></article></section>
<nav class="m-tabbar"><a class="m-tab">Home</a><a class="m-tab">Me</a></nav></body></html>"""
FILES = {
    "css/d.css": "body{margin:0;font:14px Arial}.layout{display:flex}.col-left{width:70%}.col-right{width:30%}",
    "m/m.css": "body{margin:0;font:16px Arial;background:#f4f4f4}.m-card{background:#fff;margin:8px;padding:8px;border-radius:8px}"
               ".m-header{background:#c00;color:#fff;padding:12px}.m-tabbar{position:fixed;bottom:0;left:0;right:0;background:#fff}",
}


class EditionDecision(unittest.TestCase):
    def test_redirect_to_other_host_is_separate(self):
        ok, why = separate_phone_edition("https://www.a.test/", DESKTOP, "https://m.a.test/", PHONE)
        self.assertTrue(ok, why)

    def test_same_url_different_template_is_separate(self):
        ok, why = separate_phone_edition("https://a.test/", DESKTOP, "https://a.test/", PHONE)
        self.assertTrue(ok, why)

    def test_responsive_same_html_not_separate(self):
        # 同一份 HTML(脚本可能多插几个节点), 骨架高度重合
        extra = DESKTOP.replace("</body>", '<div class="m-menu-btn"></div></body>')
        ok, why = separate_phone_edition("https://a.test/", DESKTOP, "https://www.a.test", extra)
        self.assertFalse(ok, why)

    def test_signature_ignores_state_and_numeric_classes(self):
        sig = dom_signature('<body><div class="item active x12345 card"></div></body>')
        self.assertIn("div.card", sig)
        self.assertNotIn("div.active", sig)
        self.assertFalse(any("12345" in w for w in sig))

    def test_router_injected_first_and_once(self):
        html = '<!DOCTYPE html><html><head><script>var a=1</script></head><body></body></html>'
        out = inject_edition_router(html)
        self.assertTrue(out.index("edition-router") < out.index("var a=1"))
        self.assertEqual(inject_edition_router(out).count("edition-router"), 1)
        self.assertIn("index.m.html", EDITION_ROUTER_SCRIPT)


class LocalNameShape(unittest.TestCase):
    def test_token_dot_in_last_segment_is_not_extension(self):
        from core.cloner_engine import ClonerEngine
        eng = ClonerEngine.__new__(ClonerEngine)
        out = eng._normalize_out({"ok": True, "url": "https://cdn.test/pong/50a3.hcuwe%2fqln5%2bbe8%3d",
                                  "content_type": "image/png", "content": b"\x89PNG...."})
        self.assertEqual(out["ext"], ".png")
        out = eng._normalize_out({"ok": True, "url": "https://cdn.test/a/b.CSS?v=1",
                                  "content_type": "", "content": b"body{}"})
        self.assertEqual(out["ext"], ".css")


class _Static(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class _UASite(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        ua = self.headers.get("User-Agent", "")
        if self.path in ("/", "/index.html"):
            if "iPhone" in ua:
                self.send_response(302)
                self.send_header("Location", "/m/")
                self.end_headers()
                return
            return self._html(DESKTOP)
        if self.path in ("/m/", "/m/index.html"):
            return self._html(PHONE)
        return super().do_GET()

    def _html(self, body):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(chromium_available() and has_modules("flask", "cryptography"), "需要 Playwright Chromium 与平台依赖")
class PhoneEditionEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PIL import Image
        cls.tmp = Path(tempfile.mkdtemp(prefix="clone_phone_"))
        site = cls.tmp / "site"
        for rel, body in FILES.items():
            p = site / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        Image.new("RGB", (120, 80), (30, 120, 200)).save(site / "m" / "t.png")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_UASite, directory=str(site)))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://localhost:{cls.httpd.server_address[1]}/"
        cls._saved = {k: getattr(config, k) for k in ("WORKSPACE_ROOT", "CACHE_ROOT", "EXPORTS_DIR", "AI_API_KEY")}
        config.WORKSPACE_ROOT = cls.tmp / "workspaces"
        config.CACHE_ROOT = cls.tmp / "cas"
        config.EXPORTS_DIR = cls.tmp / "exports"
        config.AI_API_KEY = ""
        for d in (config.WORKSPACE_ROOT, config.CACHE_ROOT, config.EXPORTS_DIR):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        from core.browser_pool import SyncBrowserPool
        SyncBrowserPool.instance()._teardown_thread_browser()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for k, v in cls._saved.items():
            setattr(config, k, v)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_phone_ua_lands_on_phone_edition(self):
        from playwright.sync_api import sync_playwright
        from core.browser_pool import SyncBrowserPool
        from core.cloner_engine import ClonerEngine
        from core.logger import LogBus
        from models import Database
        db = Database(self.tmp / "p.db")
        db.create_batch("phone", [(self.url, "localhost")], {})
        task = db.claim_next_task()
        ClonerEngine(task["id"], db, LogBus(4000), SyncBrowserPool.instance(2)).run()
        SyncBrowserPool.instance()._teardown_thread_browser()
        task = db.get_task(task["id"])
        self.assertEqual(task["status"], "completed", task.get("error_msg"))
        ws = config.WORKSPACE_ROOT / str(task["id"])
        self.assertIn("Desktop headline", (ws / "index.html").read_text(encoding="utf-8"))
        mpage = (ws / "index.m.html").read_text(encoding="utf-8")
        self.assertIn("Phone headline card", mpage)
        self.assertNotIn(f":{self.httpd.server_address[1]}/m/", mpage)   # 手机版资源已本地化
        import json
        self.assertIn("phone_edition", json.loads(task["stats_json"]))

        srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Static, directory=str(ws)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}/"
        try:
            with sync_playwright() as p:
                b = p.chromium.launch()
                try:
                    phone = b.new_context(viewport={"width": 420, "height": 912}, user_agent=config.PHONE_USER_AGENT,
                                          **config.PHONE_DEVICE)
                    pg = phone.new_page()
                    pg.goto(base, wait_until="load")
                    pg.wait_for_function("document.body && document.body.innerText.includes('Phone headline card')",
                                         timeout=10000)
                    bg = pg.evaluate("getComputedStyle(document.querySelector('.m-header')).backgroundColor")
                    self.assertEqual(bg, "rgb(204, 0, 0)")      # 手机版样式表随包
                    desk = b.new_context(viewport={"width": 390, "height": 844})
                    pg2 = desk.new_page()
                    pg2.goto(base, wait_until="load")
                    self.assertIn("Desktop headline", pg2.evaluate("document.body.innerText"))
                finally:
                    b.close()
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
