"""引擎端到端(离线): 本地夹具站 → 调度器领取 → ClonerEngine 全流程 → 落盘/评分/导出。

夹具站覆盖引擎各主路径: 外链样式表(含 @import 与 url() 图片)、srcset/<picture>、懒加载 data-src、
同源 iframe、脚本插入内容(交互轨重放)、按视口换 class 的脚本(分视口 class)、媒体查询、第二个页面链接。
全部在 localhost 上, 不联网; 无大模型 Key。单测时长约 30~60s。
"""
import base64
import os
import shutil
import tempfile
import threading
import time
import unittest
import json
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
from tests._env import chromium_available, has_modules

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _png(w, h, rgb):
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (w, h), rgb).save(buf, format="PNG")
    return buf.getvalue()


SITE = {
    "index.html": """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fixture Home</title>
<link rel="stylesheet" href="/css/main.css">
<style>.hero{background:#123456;color:#fff;padding:48px}@media (max-width:700px){.hero{padding:16px}}</style>
</head><body>
<header class="hero"><h1 id="title" class="big">Fixture Engine Site</h1>
<nav><a href="/about.html">About us</a> <a href="#x">Anchor</a></nav></header>
<main>
  <section class="cards">
    <div class="card"><img src="/img/a.png" alt="a" width="200" height="120"><p>Card one description text</p></div>
    <div class="card"><img data-src="/img/b.png" class="lazy" alt="b" width="200" height="120"><p>Card two lazy image</p></div>
    <div class="card"><picture><source media="(max-width:700px)" srcset="/img/small.png 1x">
      <img src="/img/c.png" srcset="/img/c.png 1x, /img/c2.png 2x" alt="c" width="200" height="120"></picture>
      <p>Card three responsive picture</p></div>
  </section>
  <div id="feed"></div>
  <iframe src="/frame.html" width="400" height="120" title="frame"></iframe>
  <form><input name="q" placeholder="Search"><button type="submit">Go</button></form>
</main>
<footer><p>Footer copyright fixture</p></footer>
<script src="/js/app.js"></script>
<script>
function sz(){document.getElementById('title').className = innerWidth < 700 ? 'small' : 'big';}
addEventListener('resize', sz); sz();
</script>
</body></html>""",
    "about.html": """<!doctype html><html><head><meta charset="utf-8"><title>About</title>
<link rel="stylesheet" href="/css/main.css"></head><body><h1>About page</h1><p>Second page content.</p>
<a href="/index.html">Home</a></body></html>""",
    "frame.html": """<!doctype html><html><body style="margin:0;background:#eee"><p>Inside the frame</p></body></html>""",
    "css/main.css": """@import url("base.css");
body{font-family:Arial,sans-serif;margin:0;background:#fafafa}
.cards{display:flex;gap:16px;padding:24px;background:url(/img/bg.png) repeat}
.card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px;width:220px}
.big{font-size:40px}.small{font-size:22px}
@media (max-width:700px){.cards{flex-direction:column}}
""",
    "spa.html": """<!doctype html><html><head><meta charset="utf-8"><title>SPA</title>
<link rel="stylesheet" href="/css/main.css"></head><body><div id="app">Loading...</div>
<script type="module">
const r = await fetch('/api/items.json'); const data = await r.json();
document.getElementById('app').innerHTML = '<h1>SPA list</h1><ul>' + data.items.map(x => '<li class="card">' + x + '</li>').join('') + '</ul>';
history.replaceState(null, '', '/spa.html#/list');
</script></body></html>""",
    "rich.html": """<!doctype html><html><head><meta charset="utf-8"><title>Rich</title>
<link rel="stylesheet" href="/css/rich.css"><link rel="icon" href="/img/a.png"></head><body>
<header><h1 class="spin">Rich fixture</h1><a href="/about.html">About</a> <a href="/spa.html">SPA</a></header>
<div id="host"></div>
<canvas id="cv" width="300" height="80"></canvas>
<video poster="/img/b.png" width="320" height="120" muted></video>
<svg width="120" height="40"><rect width="120" height="40" fill="#e33"/></svg>
<div class="carousel"><div class="slide on">Slide one</div><div class="slide">Slide two</div></div>
<section class="io"><img class="io-img" data-src="/img/c.png" width="200" height="120" alt="io"></section>
<iframe src="CROSS/frame.html" width="300" height="80" title="x"></iframe>
<p style="font-family:FixtureFont">Custom font paragraph</p>
<script>
var h=document.getElementById('host').attachShadow({mode:'open'});h.innerHTML='<style>p{color:#07c}</style><p>Shadow content here</p>';
var c=document.getElementById('cv').getContext('2d');c.fillStyle='#0a0';c.fillRect(10,10,200,50);
var k=0;setInterval(function(){var s=document.querySelectorAll('.slide');s.forEach(function(e){e.classList.remove('on')});s[(++k)%2].classList.add('on');},3000);
new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){e.target.src=e.target.dataset.src;}})}).observe(document.querySelector('.io-img'));
localStorage.setItem('fixture','1');document.cookie='fx=1; path=/';
</script></body></html>""",
    "css/rich.css": """@font-face{font-family:FixtureFont;src:url(/fonts/f.woff2) format("woff2")}
body{margin:0;font-family:Arial}.spin{animation:sp 4s linear infinite}@keyframes sp{to{transform:rotate(360deg)}}
.slide{display:none}.slide.on{display:block}.io{margin-top:1400px}""",
    "fonts/f.woff2": "not-a-real-font",
    "api/items.json": '{"items": ["Alpha item", "Beta item", "Gamma item"], "banner": "/img/apibg.png"}',
    "css/base.css": "footer{padding:24px;color:#666;border-top:1px solid #ccc}",
    "js/app.js": """document.querySelectorAll('img.lazy').forEach(function(i){i.src=i.getAttribute('data-src');});
var f=document.getElementById('feed');
for(var k=0;k<3;k++){var d=document.createElement('div');d.className='item';d.textContent='Feed item number '+k;f.appendChild(d);}
""",
}


SITE["hang.html"] = """<!doctype html><html><head><meta charset="utf-8"><title>Hang</title></head>
<body><h1>Content ready early</h1><p>Body text that renders before a hung pixel.</p>
<img src="/hang/pixel.gif" width="1" height="1" alt=""></body></html>"""
SITE["fonts.html"] = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Fonts</title><link rel="stylesheet" href="/css/fonts.css"></head>
<body><h1 class="ff">Plain latin heading only</h1><p class="ff">Second paragraph</p></body></html>"""
SITE["css/fonts.css"] = """@font-face{font-family:Fx;src:url(/fonts/fx.eot);
src:url(/fonts/fx.eot?#iefix) format("embedded-opentype"),url(/fonts/fx.woff2) format("woff2"),
url(/fonts/fx.woff) format("woff"),url(/fonts/fx.ttf) format("truetype")}
@font-face{font-family:Fx;src:url(/fonts/fx-cyr.woff2) format("woff2");unicode-range:U+0400-045F}
@font-face{font-family:Fx;src:url(/fonts/fx-lat.woff2) format("woff2");unicode-range:U+0000-00FF}
.ff{font-family:Fx,sans-serif}"""
REQUESTED = []


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        REQUESTED.append(self.path)
        if self.path.startswith("/hang/"):
            time.sleep(8)   # 慢第三方像素: 拖住 load 事件
            self.send_response(204)
            self.end_headers()
            return None
        return super().do_GET()


@unittest.skipUnless(chromium_available() and has_modules("flask", "cryptography"), "需要 Playwright Chromium 与平台依赖")
class EngineEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="clone_e2e_"))
        site = cls.tmp / "site"
        for rel, body in SITE.items():
            p = site / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        img = site / "img"
        img.mkdir(parents=True, exist_ok=True)
        for name, rgb in (("a", (200, 60, 60)), ("b", (60, 200, 60)), ("c", (60, 60, 200)),
                          ("c2", (60, 60, 220)), ("small", (90, 90, 90))):
            (img / f"{name}.png").write_bytes(_png(200, 120, rgb))
        (img / "bg.png").write_bytes(PNG_1PX)
        (img / "apibg.png").write_bytes(_png(8, 8, (1, 2, 3)))
        fonts = site / "fonts"
        fonts.mkdir(parents=True, exist_ok=True)
        real = next((Path(__file__).resolve().parent.parent / "static_vendor" / "fonts").glob("*.woff2")).read_bytes()
        for name in ("fx.woff2", "fx.woff", "fx.ttf", "fx.eot", "fx-cyr.woff2", "fx-lat.woff2"):
            (fonts / name).write_bytes(real)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(site)))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://localhost:{cls.httpd.server_address[1]}/index.html"

        cls._saved = {k: getattr(config, k) for k in ("WORKSPACE_ROOT", "CACHE_ROOT", "EXPORTS_DIR")}
        config.WORKSPACE_ROOT = cls.tmp / "workspaces"
        config.CACHE_ROOT = cls.tmp / "cas"
        config.EXPORTS_DIR = cls.tmp / "exports"
        for d in (config.WORKSPACE_ROOT, config.CACHE_ROOT, config.EXPORTS_DIR):
            d.mkdir(parents=True, exist_ok=True)
        cls._env = {k: os.environ.pop(k, None) for k in ("CLONER_AI_KEY",)}
        cls._ai_key = getattr(config, "AI_API_KEY", "")
        config.AI_API_KEY = ""

    @classmethod
    def tearDownClass(cls):
        # 主线程里的 playwright 同步实例必须关掉, 否则后续用 sync_playwright() 的测试会撞上残留事件循环
        from core.browser_pool import SyncBrowserPool
        SyncBrowserPool.instance()._teardown_thread_browser()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for k, v in cls._saved.items():
            setattr(config, k, v)
        config.AI_API_KEY = cls._ai_key
        for k, v in cls._env.items():
            if v is not None:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_scheduler_runs_full_clone(self):
        from core.browser_pool import SyncBrowserPool
        from core.logger import LogBus
        from core.scheduler import Scheduler
        from models import Database

        db = Database(self.tmp / "e2e.db")
        bus = LogBus(2000)
        pool = SyncBrowserPool.instance(2)
        batch = db.create_batch("e2e", [(self.url, "localhost")], {"crawl_depth": 1})
        sched = Scheduler(db, bus, pool)
        sched.start()
        try:
            task = None
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                tasks = db.list_tasks(batch_id=batch)
                task = db.get_task(tasks[0]["id"]) if tasks else None
                if task and task["status"] in ("completed", "failed"):
                    break
                time.sleep(1)
        finally:
            sched.shutdown()
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "completed", task.get("error_message") or task.get("stage"))
        ws = config.WORKSPACE_ROOT / str(task["id"])
        html = (ws / "index.html").read_text(encoding="utf-8")
        # 资源本地化: 外链样式表/图片不再指向夹具站
        self.assertNotIn(f"localhost:{self.httpd.server_address[1]}/css", html)
        self.assertIn("Fixture Engine Site", html)
        self.assertTrue((ws / "original_preview.png").is_file())
        self.assertTrue((ws / "cloned_preview.png").is_file())
        # 本地夹具在克隆里应几乎逐像素还原
        self.assertGreaterEqual(float(task.get("similarity_score") or 0), 90.0)
        # 分视口 class: 脚本按视口换 class, 克隆里有 data-cv-cls 运行时接管
        self.assertIn('data-cv-cls-m="small"', html)
        # 交付物: Flask 工程 ZIP
        zips = list(config.EXPORTS_DIR.glob("*.zip"))
        self.assertTrue(zips, "未生成导出 ZIP")
        with zipfile.ZipFile(zips[0]) as z:
            names = z.namelist()
            self.assertTrue(any(n.endswith("app.py") for n in names))
            # 交付侧: 已知死路径清单随包(生成应用对其秒 404 不回源)
            self.assertTrue(any(n.endswith("known_dead.json") for n in names), "known_dead.json 未随包")
            gen_app = z.read(next(n for n in names if n.endswith("app.py"))).decode("utf-8")
            self.assertIn("threaded=True", gen_app)
            self.assertNotIn("timeout=20", gen_app)
        # 部署隐患审计进 stats, 且真实克隆产物首页 0 阻塞(评分口径之外的交付侧守卫)
        from core.clone.support import deploy_hazard_audit
        stats = json.loads(task.get("stats_json") or "{}")
        audit = stats.get("deploy_audit") or {}
        self.assertIn("blocking_scripts", audit, "stats 缺 deploy_audit")
        self.assertEqual((audit.get("blocking_scripts"), audit.get("blocking_styles")), (0, 0), audit)
        live = deploy_hazard_audit(html, "localhost")
        self.assertEqual((live["blocking_scripts"], live["blocking_styles"]), (0, 0), live)


    def _run_direct(self, url):
        from core.browser_pool import SyncBrowserPool
        from core.cloner_engine import ClonerEngine
        from core.logger import LogBus
        from models import Database
        db = Database(self.tmp / f"direct_{abs(hash(url))}.db")
        db.create_batch("direct", [(url, "localhost")], {})
        task = db.claim_next_task()
        try:
            ClonerEngine(task["id"], db, LogBus(2000), SyncBrowserPool.instance(2)).run()
        except Exception:
            pass
        return db, db.get_task(task["id"])

    def test_spa_fetch_rendered_and_mocked(self):
        port = self.httpd.server_address[1]
        db, task = self._run_direct(f"http://localhost:{port}/spa.html")
        self.assertEqual(task["status"], "completed", task.get("error_msg"))
        ws = Path(task["workspace_path"]) if task.get("workspace_path") else config.WORKSPACE_ROOT / str(task["id"])
        html = (ws / "index.html").read_text(encoding="utf-8")
        self.assertIn("Gamma item", html)
        # 响应体里下发的资产 URL: CAS 期间预取, Mock 资产收割直接取用
        log = (ws / "engine.log").read_text(encoding="utf-8")
        self.assertIn("回补预取命中", log)
        self.assertIn("Mock 资产收割", log)
        self.assertGreaterEqual(float(task.get("similarity_score") or 0), 90.0)

    def test_font_plan_downloads_only_used_files(self):
        """@font-face 兜底格式与页面无对应字符的 unicode-range 切片不下载, 首选与命中切片照常本地化。"""
        port = self.httpd.server_address[1]
        REQUESTED.clear()
        db, task = self._run_direct(f"http://localhost:{port}/fonts.html")
        self.assertEqual(task["status"], "completed", task.get("error_msg"))
        got = {p.split("?")[0] for p in REQUESTED if p.startswith("/fonts/")}
        self.assertIn("/fonts/fx.woff2", got)
        self.assertIn("/fonts/fx-lat.woff2", got)
        for never in ("/fonts/fx.woff", "/fonts/fx.ttf", "/fonts/fx-cyr.woff2"):
            self.assertNotIn(never, got, f"浏览器不会请求的字体被下载了: {never}")
        ws = config.WORKSPACE_ROOT / str(task["id"])
        css = "".join(p.read_text(encoding="utf-8", errors="replace") for p in (ws / "static" / "css").glob("*.css"))
        self.assertNotIn("/fonts/fx.woff2", css.replace(f"localhost:{port}", "X"))  # 首选已本地化
        self.assertIn("fx-cyr.woff2", css)   # 未下载的切片保留原地址

    def test_nav_not_blocked_by_hung_request(self):
        """load 只被挂起的第三方请求拖住时, 首导航静置不等满上限。"""
        port = self.httpd.server_address[1]
        db, task = self._run_direct(f"http://localhost:{port}/hang.html")
        self.assertEqual(task["status"], "completed", task.get("error_msg"))
        ws = config.WORKSPACE_ROOT / str(task["id"])
        log = (ws / "engine.log").read_text(encoding="utf-8")
        import re as _re
        m = _re.search(r"页面就绪 ([\d.]+)s", log)
        self.assertIsNotNone(m, log[:2000])
        self.assertLess(float(m.group(1)), 6.0, "\n".join(log.splitlines()[:12]))
        self.assertIn("Content ready early", (ws / "index.html").read_text(encoding="utf-8"))

    def test_rich_page_crawler_mode(self):
        from core.browser_pool import SyncBrowserPool
        from core.cloner_engine import ClonerEngine
        from core.logger import LogBus
        from models import Database
        port = self.httpd.server_address[1]
        rich = (self.tmp / "site" / "rich.html")
        rich.write_text(rich.read_text(encoding="utf-8").replace("CROSS", f"http://127.0.0.1:{port}"), encoding="utf-8")
        db = Database(self.tmp / "rich.db")
        db.create_batch("rich", [(f"http://localhost:{port}/rich.html", "localhost")],
                        {"mode": "crawler", "max_pages": 3, "cookie": "sid=abc", "bearer": "tok"})
        task = db.claim_next_task()
        ClonerEngine(task["id"], db, LogBus(4000), SyncBrowserPool.instance(2)).run()
        task = db.get_task(task["id"])
        self.assertEqual(task["status"], "completed", task.get("error_msg"))
        ws = config.WORKSPACE_ROOT / str(task["id"])
        html = (ws / "index.html").read_text(encoding="utf-8")
        self.assertIn("Shadow content here", html)          # 声明式影子根序列化
        self.assertTrue(list(ws.glob("p__*.html")), "爬虫模式未落第二页")
        self.assertGreaterEqual(float(task.get("similarity_score") or 0), 85.0)

    def test_unreachable_target_fails_fast(self):
        t0 = time.monotonic()
        db, task = self._run_direct("http://127.0.0.1:9/")
        self.assertEqual(task["status"], "failed")
        self.assertLess(time.monotonic() - t0, 120)


if __name__ == "__main__":
    unittest.main()
