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
SITE["repair.html"] = """<!doctype html><html><head><meta charset="utf-8"><title>Repair</title>
<link rel="stylesheet" href="/css/dead-404.css"></head>
<body><h1>Repair loop fixture</h1><div id="slot"></div>
<script>var i=document.createElement('img');i.width=900;i.height=600;
i.src='/img/rt_'+(location.hostname==='localhost'?'a':'b')+'.png';document.getElementById('slot').appendChild(i);</script>
</body></html>"""
SITE["fonts.html"] = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Fonts</title><link rel="stylesheet" href="/css/fonts.css"></head>
<body><h1 class="ff">Plain latin heading only</h1><p class="ff">Second paragraph</p></body></html>"""
SITE["css/fonts.css"] = """@font-face{font-family:Fx;src:url(/fonts/fx.eot);
src:url(/fonts/fx.eot?#iefix) format("embedded-opentype"),url(/fonts/fx.woff2) format("woff2"),
url(/fonts/fx.woff) format("woff"),url(/fonts/fx.ttf) format("truetype")}
@font-face{font-family:Fx;src:url(/fonts/fx-cyr.woff2) format("woff2");unicode-range:U+0400-045F}
@font-face{font-family:Fx;src:url(/fonts/fx-lat.woff2) format("woff2");unicode-range:U+0000-00FF}
.ff{font-family:Fx,sans-serif}"""
SITE["xh.html"] = """<!doctype html><html><head><meta charset="utf-8"><title>XH</title></head>
<body><h1>Cross host api</h1><ul id="feed"></ul>
<script>(async () => { const r = await fetch('//127.0.0.1:' + location.port + '/xapi/feed.json');
const d = await r.json(); const ul = document.getElementById('feed'); ul.innerHTML = '';
for (const t of d.items) { const li = document.createElement('li'); li.textContent = t; ul.appendChild(li); }
document.body.setAttribute('data-feed', String(d.items.length)); })();</script></body></html>"""
SITE["xapi/feed.json"] = '{"items": ["Remote one", "Remote two"]}'
SITE["thin.html"] = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Thin</title>
<style>body{background:#eee;width:60vw;margin:15vh auto;font-family:sans-serif}</style></head>
<body><div><h1>Thin static document</h1><p>A tiny page without any script.</p></div></body></html>"""
SITE["thin_js.html"] = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>ThinJS</title></head>
<body><div id="app"><h1>Thin scripted shell</h1></div><script>window.__shell = 1;</script></body></html>"""
SITE["thin_text_js.html"] = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>ThinText</title></head>
<body><main><h1>Small scripted document</h1><p>""" + ("A paragraph of visible copy for a small page. " * 8) + """</p></main>
<script>document.documentElement.setAttribute('data-ready', '1');</script></body></html>"""
SITE["ins.html"] = """<!doctype html><html lang=en><head><meta charset=utf-8><title>Insert</title>
<style>body{font:16px/1.6 sans-serif;max-width:26em;margin:auto;padding:25vh 2em 2em;text-align:center}</style></head>
<body><p>This page keeps one paragraph in its markup and builds the rest with a tiny script.</p><script src=/s.js></script></body></html>"""
SITE["s.js"] = """(function(){var b=document.body,p=b.querySelector('p');p.lang='en';
var st=document.createElement('style');st.textContent='svg{display:block;margin:-2.75em auto 0;opacity:.55}p+p{font-size:.875em;opacity:.6}';
b.insertBefore(st,p);
p.insertAdjacentHTML('beforebegin','<svg viewBox="0,0,20,20" width="44" height="44"><rect width="20" height="20" fill="#888"/></svg>');
['Second paragraph inserted by the script, in another language.','Third paragraph inserted by the script as well.',
 'Fourth paragraph, also built at parse time by the same script.'].forEach(function(t,i){
  var q=document.createElement('p');q.lang='x'+i;q.textContent=t;b.insertBefore(q,document.currentScript);});
var a=document.createElement('a');a.href='https://iana.org/help/example-domains';a.textContent='Learn more';
b.insertBefore(a,document.currentScript);})();"""
SITE["host.html"] = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Host</title></head>
<body><h1>Host page with a cross origin widget</h1><p>""" + ("Body copy that makes the host page thick enough. " * 120) + """</p>
<iframe src="CROSS/widget.html" width="320" height="90" title="w"></iframe></body></html>"""
SITE["widget.html"] = """<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0">
<p id="w">Subscribe widget</p><script>document.getElementById('w').className = 'ready';</script></body></html>"""
REQUESTED = []


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        if self.path.startswith("/xapi/"):
            self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

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
        (img / "rt_a.png").write_bytes(_png(900, 600, (200, 0, 0)))
        (img / "rt_b.png").write_bytes(_png(900, 600, (200, 0, 0)))
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

    def _log_of(self, task):
        """本次任务的日志: _run_direct 每次新建库, 任务号都从 1 起, 同一工作区的 engine.log 是追加的。"""
        ws = Path(task["workspace_path"]) if task.get("workspace_path") else config.WORKSPACE_ROOT / str(task["id"])
        text = (ws / "engine.log").read_text(encoding="utf-8")
        return text[text.rfind("启动: "):]

    def test_thin_static_page_not_renavigated(self):
        """没有脚本/子框架的纯静态小页: 小体量即原貌, 不做「序列化过薄」重导航; 有脚本的薄壳照旧重试。"""
        port = self.httpd.server_address[1]
        REQUESTED.clear()
        _db, task = self._run_direct(f"http://localhost:{port}/thin.html")
        self.assertEqual(task["status"], "completed", task.get("error_msg"))
        log = self._log_of(task)
        self.assertIn("小体量即原貌, 不重导航", log)
        self.assertNotIn("判定采集退化", log)
        self.assertGreaterEqual(float(task.get("similarity_score") or 0), 95.0)
        _db, task = self._run_direct(f"http://localhost:{port}/thin_js.html")
        self.assertEqual(task["status"], "completed", task.get("error_msg"))
        self.assertIn("判定采集退化, 重导航重试", self._log_of(task))
        # 带脚本但画面上有成段文字、元素没减少的小页: 同样不重导航
        _db, task = self._run_direct(f"http://localhost:{port}/thin_text_js.html")
        self.assertEqual(task["status"], "completed", task.get("error_msg"))
        log = self._log_of(task)
        self.assertIn("有成段可见文字且自就绪以来未被清空, 不重导航", log)
        self.assertNotIn("判定采集退化", log)

    def test_thin_static_retry_switch_off(self):
        port = self.httpd.server_address[1]
        saved = config.THIN_STATIC_NO_RETRY
        config.THIN_STATIC_NO_RETRY = False
        try:
            _db, task = self._run_direct(f"http://localhost:{port}/thin.html?off=1")
        finally:
            config.THIN_STATIC_NO_RETRY = saved
        self.assertIn("判定采集退化, 重导航重试", self._log_of(task))

    def test_parse_time_insert_script_not_duplicated(self):
        """页尾同源短路径脚本(/s.js, 不进字面替换, 按原路径镜像落盘)在解析期插入内容: 克隆里它仍同步执行,
        重放去重守卫去掉重复副本, 交互版正文不出现两遍。此前交付去阻塞把它当「未本地化」补了 async。"""
        port = self.httpd.server_address[1]
        _db, task = self._run_direct(f"http://localhost:{port}/ins.html")
        self.assertEqual(task["status"], "completed", task.get("error_msg"))
        ws = Path(task["workspace_path"]) if task.get("workspace_path") else config.WORKSPACE_ROOT / str(task["id"])
        self.assertTrue((ws / "s.js").is_file(), "脚本未按原路径镜像落盘")
        stats = json.loads(task.get("stats_json") or "{}")
        html = (ws / "index.html").read_text(encoding="utf-8")
        inter = ws / "index.interactive.html"
        if inter.is_file():
            html = inter.read_text(encoding="utf-8")
        self.assertRegex(html, r'<script[^>]*src="?/s\.js"?[^>]*>')
        self.assertNotRegex(html, r'<script[^>]*src="?/s\.js"?[^>]*\basync\b')
        self.assertGreaterEqual(float(stats.get("interactive_score") or 0), 95.0, self._log_of(task)[-1500:])
        self.assertEqual((stats.get("deploy_audit") or {}).get("blocking_scripts"), 0)

    def test_small_frame_widget_not_renavigated(self):
        """跨域 iframe 小部件(有脚本、序列化 <5KB)自就绪以来没被清空: 固化为本地子页但不为它重导航。"""
        port = self.httpd.server_address[1]
        site = self.tmp / "site"
        (site / "host_x.html").write_text(
            SITE["host.html"].replace("CROSS", f"http://127.0.0.1:{port}"), encoding="utf-8")
        _db, task = self._run_direct(f"http://localhost:{port}/host_x.html")
        self.assertEqual(task["status"], "completed", task.get("error_msg"))
        log = self._log_of(task)
        self.assertIn("跨域 iframe 已固化为本地子页", log)
        self.assertIn("iframe 小部件自就绪以来未被清空, 不重导航", log)
        self.assertNotIn("判定采集退化", log)

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

    def test_repair_loop_stops_when_structural_missing_unchanged(self):
        """结构性缺失(死样式表)回补补不上、每轮只补回非结构资源时, 第 2 轮即收敛。"""
        port = self.httpd.server_address[1]
        db, task = self._run_direct(f"http://localhost:{port}/repair.html")
        self.assertEqual(task["status"], "completed", task.get("error_msg"))
        ws = config.WORKSPACE_ROOT / str(task["id"])
        log = (ws / "engine.log").read_text(encoding="utf-8")
        log = log[log.rfind(f"导航 http://localhost:{port}/repair.html"):]   # 同号任务工作区可能复用, 只看本次
        tail = "\n".join(l for l in log.splitlines() if "RENDER" in l or "SMART" in l)[-3000:]
        # 默认: 第 1 轮先只回补死样式表, 补不回 → 不再回补图片、不再重渲染, 只渲染评分一次
        self.assertIn("回补一个没补回", log, tail)
        self.assertEqual(log.count("视觉还原度"), 1, tail)
        self.assertNotIn("rt_b", "\n".join(l for l in log.splitlines() if "渲染缺失回补" in l), tail)

    def test_repair_loop_struct_same_stop_without_gain_gate(self):
        """关掉"回补须补回结构性资源"闸门时, 由"结构性缺失与上一轮相同"在第 2 轮收敛。"""
        from unittest import mock
        port = self.httpd.server_address[1]
        with mock.patch.object(config, "REPAIR_NEED_STRUCT_GAIN", False):
            db, task = self._run_direct(f"http://localhost:{port}/repair.html")
        self.assertEqual(task["status"], "completed", task.get("error_msg"))
        log = (config.WORKSPACE_ROOT / str(task["id"]) / "engine.log").read_text(encoding="utf-8")
        self.assertIn("与上一轮相同", log, "\n".join(l for l in log.splitlines() if "RENDER" in l or "SMART" in l)[-3000:])

    def test_cross_host_api_replayed_from_mock(self):
        """跨域数据接口(协议相对 //另一主机)录到 Mock 后, 离线回放经垫片改发同源由本地 Mock 接住。"""
        from core.browser_pool import SyncBrowserPool
        from core.clone.support import _LocalPreviewServer
        port = self.httpd.server_address[1]
        db, task = self._run_direct(f"http://localhost:{port}/xh.html")
        self.assertEqual(task["status"], "completed", task.get("error_msg"))
        ws = config.WORKSPACE_ROOT / str(task["id"])
        html = (ws / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-clone-preview="xhost-mock"', html)
        self.assertIn(f"127.0.0.1:{port}", html.split('data-clone-preview="xhost-mock"', 1)[1][:300])
        srv = _LocalPreviewServer(ws)
        srv.start()
        try:
            base = f"http://127.0.0.1:{srv.port}"
            with SyncBrowserPool.instance(2).context(viewport={"width": 1280, "height": 800}) as ctx:
                pg = ctx.new_page()
                pg.route("**/*", lambda r: r.continue_() if r.request.url.startswith(base) else r.abort())
                pg.goto(base + "/index.html", wait_until="load")
                pg.wait_for_function("document.body.getAttribute('data-feed') === '2'", timeout=8000)
                self.assertIn("Remote two", pg.inner_text("#feed"))
                pg.close()
        finally:
            srv.stop()

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
