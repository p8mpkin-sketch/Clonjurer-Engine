"""交付形态: 把任务产出的网站工程(flask_app)装进应用壳, 构建桌面应用 / Android 应用。

适用于所有产出网站工程的任务(网址克隆、界面生成 html_css), 不区分站点, 不改克隆管线:
调度器在任务完成后按批次配置 `package`(desktop | apk)调用 `package_task()`。

壳内置一个只监听 127.0.0.1 的微型本地服务器, 语义移植自导出的 Flask 工程(flask_generator 模板):
  页面路由表 → templates/; 录制接口 mock_data.json(精确方法+路径 → 同路径 → undefined 段折叠 → 后缀段匹配);
  影子文件直通(static/ 与页面相对资源); 开启反代兜底且有上游时, 未命中请求转发上游; 否则 404。
因此绝对路径资源(/static/...)、多页路由、录制接口在应用里与导出网站行为一致。
  - 桌面: Go + 系统 WebView(macOS WKWebView / Windows WebView2), 站点 go:embed 进单个可执行文件; Electron 壳保留为备选;
  - Android: 原生 Activity + 系统 WebView, 在 shouldInterceptRequest 里按同一语义作答(虚拟源 https://app.local, 不开端口)。
不在应用壳中加入埋点/上报; 只打包任务已有的产物, 不抓取任何新内容。
"""
import json
import re
import shutil
from pathlib import Path

import config

ELECTRON_VERSION = "45.2.2"
PACKAGER_VERSION = "20.3.0"

# flask_app 中属于 Flask 运行时而非站点内容的文件/目录, 不进应用壳
_INFRA = {"app.py", "Dockerfile", "docker-compose.yml", "requirements.txt", "run.sh", "run.bat",
          "README.md", "logs", "cache", "__pycache__", "tracker", ".dockerignore"}


def _safe_title(title: str) -> str:
    t = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "", str(title or "")).strip()
    return t[:60] or "Clonjurer App"


def site_meta(ws: Path) -> dict:
    site = {}
    try:
        site = json.loads((ws / "site.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    pages = site.get("routes") or {"/": "index.html"}
    return {"pages": pages, "upstream": str(site.get("upstream") or ""),
            "proxy": bool(site.get("proxy_enabled")) and bool(site.get("upstream"))}


def bundle_site(ws: Path, dest: Path) -> dict:
    """flask_app → dest/{templates, static, ..., site_meta.json}; 返回 meta。"""
    src = ws / "flask_app"
    if not (src / "templates").is_dir():
        raise FileNotFoundError("任务没有网站工程产物(flask_app/templates), 无法装壳")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for item in src.iterdir():
        if item.name in _INFRA or item.name.startswith("."):
            continue
        if item.is_dir():
            shutil.copytree(item, dest / item.name, symlinks=False)
        else:
            shutil.copy2(item, dest / item.name)
    meta = site_meta(ws)
    (dest / "site_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


# ---------------------------------------------------------------------------
# 桌面: Electron 壳
# ---------------------------------------------------------------------------
_ELECTRON_MAIN = r"""// Clonjurer 应用壳(桌面): 本地服务器语义与导出的 Flask 工程一致, 只监听 127.0.0.1。
const { app, BrowserWindow, shell } = require('electron');
const http = require('http');
const fs = require('fs');
const path = require('path');

const SITE = path.join(__dirname, 'site');
const META = JSON.parse(fs.readFileSync(path.join(SITE, 'site_meta.json'), 'utf8'));
const WIN = %(win)s;
let MOCKS = [];
try { const m = JSON.parse(fs.readFileSync(path.join(SITE, 'mock_data.json'), 'utf8')); if (Array.isArray(m)) MOCKS = m; } catch (e) {}

const MIME = { '.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
  '.webp': 'image/webp', '.avif': 'image/avif', '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.woff': 'font/woff',
  '.ttf': 'font/ttf', '.otf': 'font/otf', '.eot': 'application/vnd.ms-fontobject', '.mp4': 'video/mp4',
  '.webm': 'video/webm', '.mp3': 'audio/mpeg', '.wasm': 'application/wasm', '.txt': 'text/plain; charset=utf-8' };
function mimeOf(p) {
  const name = path.basename(p).split('@')[0];
  return MIME[path.extname(name).toLowerCase()] || 'application/octet-stream';
}
const norm = (p) => '/' + String(p || '').replace(/^\/+|\/+$/g, '');
function matchMock(p, method) {
  method = (method || 'GET').toUpperCase();
  const target = norm(p);
  const exact = (t) => { let loose = null;
    for (const m of MOCKS) { if (norm(m.path) !== t) continue;
      if (String(m.method || 'GET').toUpperCase() === method) return m; if (!loose) loose = m; }
    return loose; };
  let hit = exact(target);
  if (!hit && target.includes('undefined/')) hit = exact('/' + target.replace(/(^|\/)undefined(\/|$)/g, '/').replace(/^\/+|\/+$/g, ''));
  if (!hit) {
    const req = target.split('/').filter(Boolean); let best = null, bestLen = 0;
    for (const m of MOCKS) {
      const segs = norm(m.path).split('/').filter(Boolean);
      if (segs.length >= bestLen && req.length > segs.length && req.slice(-segs.length).join('/') === segs.join('/')) {
        if (String(m.method || 'GET').toUpperCase() === method) return m; best = m; bestLen = segs.length; }
    }
    hit = best;
  }
  return hit;
}
function inside(base, rel) {
  const full = path.resolve(base, rel);
  return full.startsWith(path.resolve(base) + path.sep) ? full : null;
}
function sendFile(res, file, status) {
  fs.readFile(file, (err, buf) => {
    if (err) { res.writeHead(404); res.end(); return; }
    res.writeHead(status || 200, { 'Content-Type': mimeOf(file) }); res.end(buf);
  });
}
async function proxy(req, res, sub, url) {
  try {
    const chunks = []; for await (const c of req) chunks.push(c);
    const headers = {}; for (const [k, v] of Object.entries(req.headers))
      if (!['host', 'connection', 'accept-encoding', 'content-length', 'transfer-encoding', 'origin', 'referer'].includes(k)) headers[k] = v;
    headers.referer = META.upstream + '/'; headers.origin = META.upstream;
    const r = await fetch(META.upstream + '/' + sub + url.search, { method: req.method, headers,
      body: ['GET', 'HEAD'].includes(req.method) ? undefined : Buffer.concat(chunks), redirect: 'follow' });
    const body = Buffer.from(await r.arrayBuffer());
    res.writeHead(r.status, { 'Content-Type': r.headers.get('content-type') || 'application/octet-stream' }); res.end(body);
  } catch (e) { res.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' }); res.end('Upstream proxy failed'); }
}
function handle(req, res) {
  const url = new URL(req.url, 'http://127.0.0.1');
  let sub; try { sub = decodeURIComponent(url.pathname).replace(/^\/+|\/+$/g, ''); } catch (e) { res.writeHead(400); res.end(); return; }
  const tpl = path.join(SITE, 'templates');
  if (sub === '') return sendFile(res, inside(tpl, META.pages['/'] || 'index.html'));
  const page = META.pages['/' + sub] || META.pages['/' + sub + '/'] || META.pages[sub];
  if (page && inside(tpl, page)) return sendFile(res, inside(tpl, page));
  const mock = matchMock(sub, req.method);
  if (mock) {
    const raw = mock.response_body || ''; let ctype = 'text/plain; charset=utf-8';
    try { if (raw) { JSON.parse(raw); ctype = 'application/json; charset=utf-8'; } } catch (e) {}
    res.writeHead(parseInt(mock.status || 200, 10) || 200, { 'Content-Type': ctype, 'X-Mock-Api': 'recorded' }); res.end(raw); return;
  }
  const shadow = inside(SITE, sub);
  if (shadow && fs.existsSync(shadow) && fs.statSync(shadow).isFile()) return sendFile(res, shadow);
  if (!('/' + sub).includes('/undefined/') && META.proxy && META.upstream) return proxy(req, res, sub, url);
  res.writeHead(404, { 'Content-Type': 'text/html; charset=utf-8' }); res.end('<h1>404 - Not Cloned</h1>');
}
function listen(server, port) {
  return new Promise((resolve, reject) => {
    server.once('error', reject); server.listen(port, '127.0.0.1', () => resolve(server.address().port));
  });
}
async function start() {
  const server = http.createServer(handle);
  let port;
  // 固定端口让页面的 localStorage/cookie 跨次启动保留; 被占用时退回随机端口
  try { port = await listen(server, WIN.port); } catch (e) { port = await listen(http.createServer(handle), 0); }
  const origin = 'http://127.0.0.1:' + port;
  const win = new BrowserWindow({ width: WIN.width, height: WIN.height, title: WIN.title,
    webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false } });
  win.setMenuBarVisibility(false);
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(origin)) return { action: 'allow' };
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (e, url) => {
    if (!url.startsWith(origin)) { e.preventDefault(); if (/^https?:/i.test(url)) shell.openExternal(url); }
  });
  win.loadURL(origin + '/');
}
app.whenReady().then(start);
app.on('window-all-closed', () => app.quit());
"""


def electron_shell(ws: Path, out: Path, title: str, viewport: dict = None) -> Path:
    app_dir = out / "electron_app"
    if app_dir.exists():
        shutil.rmtree(app_dir)
    app_dir.mkdir(parents=True)
    bundle_site(ws, app_dir / "site")
    vp = viewport or {}
    title = _safe_title(title)
    port = 47000 + (sum(title.encode("utf-8")) % 2000)
    win = {"width": max(400, min(int(vp.get("width") or 1280), 1440)),
           "height": max(600, min(int(vp.get("height") or 860), 900)), "title": title, "port": port}
    (app_dir / "main.js").write_text(_ELECTRON_MAIN % {"win": json.dumps(win, ensure_ascii=False)}, encoding="utf-8")
    pkg = {"name": "clonjurer-app", "productName": title, "version": "0.1.0", "main": "main.js", "private": True,
           "description": "Packaged by Clonjurer from a task's website output.",
           "scripts": {"start": "electron ."},
           "devDependencies": {"electron": ELECTRON_VERSION, "@electron/packager": PACKAGER_VERSION}}
    (app_dir / "package.json").write_text(json.dumps(pkg, ensure_ascii=False, indent=2), encoding="utf-8")
    return app_dir


# ---------------------------------------------------------------------------
# 桌面(默认): Go + 系统 WebView 壳(macOS WKWebView / Windows WebView2), 不内置浏览器内核
# ---------------------------------------------------------------------------
_GO_TEMPLATES = Path(__file__).resolve().parent / "shell_templates" / "desktop_go"


def _ascii_name(title: str, fallback: str) -> str:
    n = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")
    return (n[:40] or fallback)


def desktop_shell(ws: Path, out: Path, title: str, viewport: dict = None, bundle_id: str = "com.clonjurer.app") -> Path:
    """任务网站工程 → Go 桌面工程(server.go 与导出 Flask 工程同语义; 站点经 go:embed 打进单个可执行文件)。"""
    app_dir = out / "desktop_shell"
    if app_dir.exists():
        shutil.rmtree(app_dir)
    shutil.copytree(_GO_TEMPLATES, app_dir)
    bundle_site(ws, app_dir / "site")
    vp = viewport or {}
    title = _safe_title(title)
    port = 47000 + (sum(title.encode("utf-8")) % 2000)
    w = max(400, min(int(vp.get("width") or 1280), 1440))
    h = max(600, min(int(vp.get("height") or 860), 900))
    (app_dir / "config.go").write_text(
        "package main\n\n// 由 Clonjurer 生成\nconst (\n"
        f"\tAppTitle  = {json.dumps(title, ensure_ascii=False)}\n\tAppWidth  = {w}\n\tAppHeight = {h}\n\tAppPort   = {port}\n)\n",
        encoding="utf-8")
    # go.mod(依赖版本钉死)随模板目录复制
    (app_dir / "shell.json").write_text(json.dumps({"title": title, "name": _ascii_name(title, "ClonjurerApp"),
                                                    "bundle_id": bundle_id}, ensure_ascii=False), encoding="utf-8")
    return app_dir


# ---------------------------------------------------------------------------
# Android: 原生 WebView 壳(无 Flutter / 无 Gradle; 体积 ≈ 几十 KB 代码 + 站点资源)
# ---------------------------------------------------------------------------
# 不起本地端口: 用虚拟 https 源 https://app.local, 在 shouldInterceptRequest 里按导出 Flask 工程的语义作答
# (页面路由 / 录制接口 / 影子文件 / GET 反代兜底), 因此无需明文 http 放行, localStorage/Cookie 按该源持久。
_ANDROID_MAIN = r"""// Clonjurer 应用壳(Android 原生): 系统 WebView + 请求拦截, 语义与导出的 Flask 工程一致。
package %(pkg)s;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.json.JSONArray;
import org.json.JSONObject;

public class MainActivity extends Activity {
    static final String HOST = "app.local";
    static final String ORIGIN = "https://" + HOST;
    private WebView web;
    private JSONObject pages = new JSONObject();
    private JSONArray mocks = new JSONArray();
    private String upstream = "";
    private boolean proxy = false;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        try {
            JSONObject meta = new JSONObject(readText("site/site_meta.json"));
            JSONObject p = meta.optJSONObject("pages");
            if (p != null) pages = p;
            upstream = meta.optString("upstream", "");
            proxy = meta.optBoolean("proxy", false);
        } catch (Exception ignored) { }
        try { mocks = new JSONArray(readText("site/mock_data.json")); } catch (Exception ignored) { }
        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        web.setWebViewClient(new Client());
        setContentView(web);
        if (state == null) web.loadUrl(ORIGIN + "/"); else web.restoreState(state);
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack(); else super.onBackPressed();
    }

    // ------------------------------------------------------------------
    private InputStream open(String rel) {
        try { return getAssets().open(rel); } catch (IOException e) { return null; }
    }

    private String readText(String rel) throws IOException {
        InputStream in = open(rel);
        if (in == null) throw new IOException(rel);
        ByteArrayOutputStream bo = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
        in.close();
        return new String(bo.toByteArray(), StandardCharsets.UTF_8);
    }

    static String strip(String p) {
        return (p == null ? "" : p).replaceAll("^/+|/+$", "");
    }

    static String norm(Object p) {
        return "/" + strip(p == null ? "" : String.valueOf(p));
    }

    static final Map<String, String> MIME = new HashMap<>();
    static {
        String[] kv = {"html", "text/html", "htm", "text/html", "css", "text/css", "js", "text/javascript",
            "mjs", "text/javascript", "json", "application/json", "svg", "image/svg+xml", "png", "image/png",
            "jpg", "image/jpeg", "jpeg", "image/jpeg", "gif", "image/gif", "webp", "image/webp", "avif", "image/avif",
            "ico", "image/x-icon", "woff2", "font/woff2", "woff", "font/woff", "ttf", "font/ttf", "otf", "font/otf",
            "eot", "application/vnd.ms-fontobject", "mp4", "video/mp4", "webm", "video/webm", "mp3", "audio/mpeg",
            "wasm", "application/wasm", "txt", "text/plain"};
        for (int i = 0; i < kv.length; i += 2) MIME.put(kv[i], kv[i + 1]);
    }

    static String mimeOf(String rel) {
        String name = rel.substring(rel.lastIndexOf('/') + 1).split("@")[0];
        int dot = name.lastIndexOf('.');
        String m = dot < 0 ? null : MIME.get(name.substring(dot + 1).toLowerCase());
        return m == null ? "application/octet-stream" : m;
    }

    static boolean textual(String mime) {
        return mime.startsWith("text/") || mime.equals("application/json") || mime.equals("image/svg+xml");
    }

    private static WebResourceResponse resp(int code, String mime, InputStream body) {
        Map<String, String> h = new HashMap<>();
        h.put("Access-Control-Allow-Origin", ORIGIN);
        h.put("Cache-Control", "no-cache");
        String reason = code == 200 ? "OK" : code == 404 ? "Not Found" : code >= 500 ? "Error" : "Status";
        return new WebResourceResponse(mime, textual(mime) ? "utf-8" : null, code, reason, h, body);
    }

    private static WebResourceResponse text(int code, String mime, String s) {
        return resp(code, mime, new ByteArrayInputStream(s.getBytes(StandardCharsets.UTF_8)));
    }

    private WebResourceResponse file(String rel) {
        InputStream in = open("site/" + rel);
        if (in == null) return text(404, "text/html", "<h1>404 - Not Cloned</h1>");
        return resp(200, mimeOf(rel), in);
    }

    private JSONObject matchMock(String p, String method) {
        method = method == null ? "GET" : method.toUpperCase();
        String target = norm(p);
        JSONObject hit = exact(target, method);
        if (hit == null && target.contains("undefined/"))
            hit = exact(norm(target.replaceAll("(^|/)undefined(/|$)", "/")), method);
        if (hit == null) {
            List<String> req = segs(target);
            JSONObject best = null;
            int bestLen = 0;
            for (int i = 0; i < mocks.length(); i++) {
                JSONObject m = mocks.optJSONObject(i);
                if (m == null) continue;
                List<String> sg = segs(norm(m.optString("path")));
                if (sg.size() >= bestLen && req.size() > sg.size()
                        && req.subList(req.size() - sg.size(), req.size()).equals(sg)) {
                    if (m.optString("method", "GET").toUpperCase().equals(method)) return m;
                    best = m;
                    bestLen = sg.size();
                }
            }
            hit = best;
        }
        return hit;
    }

    private JSONObject exact(String t, String method) {
        JSONObject loose = null;
        for (int i = 0; i < mocks.length(); i++) {
            JSONObject m = mocks.optJSONObject(i);
            if (m == null || !norm(m.optString("path")).equals(t)) continue;
            if (m.optString("method", "GET").toUpperCase().equals(method)) return m;
            if (loose == null) loose = m;
        }
        return loose;
    }

    private static List<String> segs(String p) {
        List<String> out = new ArrayList<>();
        for (String x : p.split("/")) if (!x.isEmpty()) out.add(x);
        return out;
    }

    WebResourceResponse resolve(Uri u, String method) throws IOException {
        String sub = strip(u.getPath());
        if (sub.isEmpty()) return file("templates/" + pages.optString("/", "index.html"));
        String page = pages.optString("/" + sub, pages.optString("/" + sub + "/", pages.optString(sub, "")));
        if (!page.isEmpty() && !page.contains("..")) {
            InputStream probe = open("site/templates/" + page);
            if (probe != null) { probe.close(); return file("templates/" + page); }
        }
        JSONObject mock = matchMock(sub, method);
        if (mock != null) {
            String raw = mock.optString("response_body", "");
            String mime = "text/plain";
            try { if (!raw.isEmpty()) { new org.json.JSONTokener(raw).nextValue(); mime = "application/json"; } }
            catch (Exception ignored) { }
            return text(mock.optInt("status", 200), mime, raw);
        }
        if (!Arrays.asList(sub.split("/")).contains("..")) {
            InputStream in = open("site/" + sub);
            if (in != null) return resp(200, mimeOf(sub), in);
        }
        String m = method == null ? "GET" : method.toUpperCase();
        if (proxy && !upstream.isEmpty() && !("/" + sub).contains("/undefined/") && (m.equals("GET") || m.equals("HEAD")))
            return fetchUpstream(sub, u.getEncodedQuery(), m);
        return text(404, "text/html", "<h1>404 - Not Cloned</h1>");
    }

    private WebResourceResponse fetchUpstream(String sub, String query, String method) {
        try {
            URL url = new URL(upstream + "/" + sub + (query != null ? "?" + query : ""));
            HttpURLConnection c = (HttpURLConnection) url.openConnection();
            c.setRequestMethod(method);
            c.setConnectTimeout(15000);
            c.setReadTimeout(20000);
            c.setRequestProperty("Referer", upstream + "/");
            int code = c.getResponseCode();
            if (code >= 300 && code < 400) code = 502;
            String ct = c.getContentType();
            String mime = ct == null ? mimeOf(sub) : ct.split(";")[0].trim();
            InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
            return resp(code, mime, in != null ? in : new ByteArrayInputStream(new byte[0]));
        } catch (Exception e) {
            return text(502, "text/plain", "Upstream proxy failed");
        }
    }

    class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
            Uri u = r.getUrl();
            if (HOST.equals(u.getHost())) return false;
            try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (Exception ignored) { }
            return true;
        }

        @Override
        public WebResourceResponse shouldInterceptRequest(WebView v, WebResourceRequest r) {
            Uri u = r.getUrl();
            if (!HOST.equals(u.getHost())) return null;
            try { return resolve(u, r.getMethod()); }
            catch (Exception e) { return text(500, "text/plain", "shell error"); }
        }
    }
}
"""

_ANDROID_MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="%(pkg)s" android:versionCode="1" android:versionName="0.1.0">
    <uses-permission android:name="android.permission.INTERNET"/>
    <application android:label="%(label)s" android:allowBackup="false" android:hardwareAccelerated="true"
        android:usesCleartextTraffic="false" android:theme="@android:style/Theme.Material.Light.NoActionBar">
        <activity android:name=".MainActivity" android:exported="true"
            android:configChanges="orientation|screenSize|screenLayout|smallestScreenSize|keyboardHidden|uiMode">
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>
        </activity>
    </application>
</manifest>
"""


def android_shell(ws: Path, out: Path, title: str, package: str) -> Path:
    """任务网站工程 → 原生 Android 工程目录(AndroidManifest.xml + src/ + assets/site/), 由 toolchain.build_android_shell 构建。"""
    if not re.match(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", package):
        raise ValueError(f"非法包名 {package!r}")
    app_dir = out / "android_shell"
    if app_dir.exists():
        shutil.rmtree(app_dir)
    src = app_dir / "src" / Path(*package.split("."))
    src.mkdir(parents=True)
    bundle_site(ws, app_dir / "assets" / "site")
    title = _safe_title(title)
    label = title.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    (app_dir / "AndroidManifest.xml").write_text(_ANDROID_MANIFEST % {"pkg": package, "label": label}, encoding="utf-8")
    (src / "MainActivity.java").write_text(_ANDROID_MAIN % {"pkg": package}, encoding="utf-8")
    return app_dir


# ---------------------------------------------------------------------------
# 构建 + 导出
# ---------------------------------------------------------------------------
def export_artifacts(kind: str, res: dict, task_id, log=print) -> dict:
    """toolchain 结果 → 复制/压缩到 EXPORTS_DIR, 返回 {ok, artifacts[{platform,file,bytes,label}], gap, signing}。"""
    from core.uir import toolchain
    out = config.EXPORTS_DIR
    out.mkdir(parents=True, exist_ok=True)
    if not res.get("ok") and not res.get("artifacts"):
        return {"ok": False, "artifacts": [], "gap": res.get("gap") or "构建失败", "log": str(res.get("log", ""))[-2000:]}
    arts = []
    if kind == "apk":
        dest = out / f"{task_id}_app-release.apk"
        shutil.copyfile(res["artifact"], dest)
        arts.append({"platform": "android", "file": dest.name, "bytes": dest.stat().st_size, "label": "APK"})
    else:
        for a in res.get("artifacts", []):
            src = Path(a["dir"])
            dest = out / f"{task_id}_{src.name}.zip"
            toolchain.zip_dir(src, dest)
            label = {"darwin": "macOS .app", "win32": "Windows .exe", "linux": "Linux"}.get(a["platform"], a["platform"])
            arts.append({"platform": a["platform"], "arch": a["arch"], "file": dest.name,
                         "bytes": dest.stat().st_size, "label": f"{label} ({a['arch']})"})
    fails = res.get("failures") or {}
    gap = res.get("gap") or ("; ".join(f"{k} 打包失败" for k in fails) or None)
    for a in arts:
        log(f"产物 {a['label']}: {a['file']} ({a['bytes'] / 1048576:.1f} MB)")  # type: ignore[operator]
    return {"ok": bool(arts), "artifacts": arts, "gap": gap, "signing": res.get("signing"),
            "analyze_ok": res.get("analyze_ok"), "test_ok": res.get("test_ok")}


def package_task(task_id, ws: Path, kind: str, title: str = "", viewport: dict = None, log=print) -> dict:
    """任务网站产物 → 应用壳 → 构建 → 导出; 结果同时写入 ws/build_report.json。不抛异常。"""
    from core.uir import toolchain
    ws = Path(ws)
    try:
        if kind == "desktop":
            app_dir = desktop_shell(ws, ws / "app_shell", title, viewport, bundle_id=f"com.clonjurer.task{task_id}")
            host = toolchain.host_platform()
            res = toolchain.build_go_desktop(app_dir, [host] + (["win32"] if host != "win32" else []), log=log)
        elif kind == "apk":
            app_dir = android_shell(ws, ws / "app_shell", title, f"com.clonjurer.task{task_id}")
            res = toolchain.build_android_shell(app_dir, log=log)
        else:
            return {"ok": False, "artifacts": [], "gap": f"未知交付形态 {kind!r}"}
        result = export_artifacts(kind, res, task_id, log=log)
    except Exception as exc:
        log(f"打包异常: {type(exc).__name__}: {exc}")
        result = {"ok": False, "artifacts": [], "gap": f"打包异常: {type(exc).__name__}: {exc}"[:300]}
    result["kind"] = kind
    if result.get("gap"):
        log(f"缺口: {result['gap']}")
    (ws / "build_report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
