"""交付形态: 把任务产出的网站工程(flask_app)装进应用壳, 构建桌面应用 / Android 应用。

适用于所有产出网站工程的任务(网址克隆、界面生成 html_css), 不区分站点, 不改克隆管线:
调度器在任务完成后按批次配置 `package`(desktop | apk)调用 `package_task()`。

壳内置一个只监听 127.0.0.1 的微型本地服务器, 语义移植自导出的 Flask 工程(flask_generator 模板):
  页面路由表 → templates/; 录制接口 mock_data.json(精确方法+路径 → 同路径 → undefined 段折叠 → 后缀段匹配);
  影子文件直通(static/ 与页面相对资源); 开启反代兜底且有上游时, 未命中请求转发上游; 否则 404。
因此绝对路径资源(/static/...)、多页路由、录制接口在应用里与导出网站行为一致。
  - 桌面: Electron(main.js 里 Node http 服务 + BrowserWindow 加载本地地址);
  - Android: Flutter(dart:io HttpServer 从单文件资源包读内容 + webview_flutter 显示)。
不在应用壳中加入埋点/上报; 只打包任务已有的产物, 不抓取任何新内容。
"""
import json
import re
import shutil
from pathlib import Path

import config

ELECTRON_VERSION = "45.2.2"
PACKAGER_VERSION = "20.3.0"
WEBVIEW_FLUTTER_VERSION = "^4.14.1"

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
# Android: Flutter + WebView 壳
# ---------------------------------------------------------------------------
_DART_SERVER = r"""// Clonjurer 应用壳(Android)本地服务器: 语义与导出的 Flask 工程一致, 只监听 127.0.0.1。
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

class SiteServer {
  SiteServer(this.pack, Map<String, dynamic> index)
      : files = Map<String, dynamic>.from(index['files'] as Map),
        meta = Map<String, dynamic>.from(index['meta'] as Map) {
    final m = read('mock_data.json');
    if (m != null) {
      try {
        final d = jsonDecode(utf8.decode(m));
        if (d is List) mocks = d;
      } catch (_) {}
    }
  }

  final Uint8List pack;
  final Map<String, dynamic> files;
  final Map<String, dynamic> meta;
  List<dynamic> mocks = const [];

  Uint8List? read(String rel) {
    final e = files[rel];
    if (e is! List) return null;
    final off = e[0] as int, len = e[1] as int;
    return Uint8List.sublistView(pack, off, off + len);
  }

  static String norm(Object? p) => '/' + (p ?? '').toString().replaceAll(RegExp(r'^/+|/+$'), '');

  Map? matchMock(String p, String method) {
    method = method.toUpperCase();
    final target = norm(p);
    Map? exact(String t) {
      Map? loose;
      for (final m in mocks) {
        if (m is! Map || norm(m['path']) != t) continue;
        if ((m['method'] ?? 'GET').toString().toUpperCase() == method) return m;
        loose ??= m;
      }
      return loose;
    }

    var hit = exact(target);
    if (hit == null && target.contains('undefined/')) {
      hit = exact(norm(target.replaceAll(RegExp(r'(^|/)undefined(/|$)'), '/')));
    }
    if (hit == null) {
      final req = target.split('/').where((s) => s.isNotEmpty).toList();
      Map? best;
      var bestLen = 0;
      for (final m in mocks) {
        if (m is! Map) continue;
        final segs = norm(m['path']).split('/').where((s) => s.isNotEmpty).toList();
        if (segs.length >= bestLen && req.length > segs.length &&
            req.sublist(req.length - segs.length).join('/') == segs.join('/')) {
          if ((m['method'] ?? 'GET').toString().toUpperCase() == method) return m;
          best = m;
          bestLen = segs.length;
        }
      }
      hit = best;
    }
    return hit;
  }

  /// 解析一个请求: 返回 (状态码, Content-Type, 内容); null 表示需要转发上游。
  (int, String, List<int>)? resolve(String rawPath, String method) {
    final sub = rawPath.replaceAll(RegExp(r'^/+|/+$'), '');
    final pages = Map<String, dynamic>.from(meta['pages'] as Map);
    if (sub.isEmpty) return _file('templates/${pages['/'] ?? 'index.html'}');
    final page = pages['/$sub'] ?? pages['/$sub/'] ?? pages[sub];
    if (page != null && read('templates/$page') != null) return _file('templates/$page');
    final mock = matchMock(sub, method);
    if (mock != null) {
      final raw = (mock['response_body'] ?? '').toString();
      var ctype = 'text/plain; charset=utf-8';
      try {
        if (raw.isNotEmpty) {
          jsonDecode(raw);
          ctype = 'application/json; charset=utf-8';
        }
      } catch (_) {}
      return (int.tryParse('${mock['status'] ?? 200}') ?? 200, ctype, utf8.encode(raw));
    }
    if (!sub.split('/').contains('..') && read(sub) != null) return _file(sub);
    if (!'/$sub'.contains('/undefined/') && meta['proxy'] == true && '${meta['upstream']}'.isNotEmpty) return null;
    return (404, 'text/html; charset=utf-8', utf8.encode('<h1>404 - Not Cloned</h1>'));
  }

  (int, String, List<int>) _file(String rel) {
    final b = read(rel);
    if (b == null) return (404, 'text/plain', const []);
    return (200, mimeOf(rel), b);
  }

  static const _mime = {
    'html': 'text/html; charset=utf-8', 'htm': 'text/html; charset=utf-8', 'css': 'text/css; charset=utf-8',
    'js': 'text/javascript; charset=utf-8', 'mjs': 'text/javascript; charset=utf-8',
    'json': 'application/json; charset=utf-8', 'svg': 'image/svg+xml', 'png': 'image/png', 'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg', 'gif': 'image/gif', 'webp': 'image/webp', 'avif': 'image/avif', 'ico': 'image/x-icon',
    'woff2': 'font/woff2', 'woff': 'font/woff', 'ttf': 'font/ttf', 'otf': 'font/otf',
    'eot': 'application/vnd.ms-fontobject', 'mp4': 'video/mp4', 'webm': 'video/webm', 'mp3': 'audio/mpeg',
    'wasm': 'application/wasm', 'txt': 'text/plain; charset=utf-8',
  };

  static String mimeOf(String rel) {
    final name = rel.split('/').last.split('@').first;
    final dot = name.lastIndexOf('.');
    return _mime[dot < 0 ? '' : name.substring(dot + 1).toLowerCase()] ?? 'application/octet-stream';
  }

  Future<int> serve({int port = 0}) async {
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, port);
    server.listen((req) async {
      try {
        String path;
        try {
          path = Uri.decodeComponent(req.uri.path);
        } catch (_) {
          path = req.uri.path;
        }
        final r = resolve(path, req.method);
        if (r == null) {
          await _proxy(req, path);
          return;
        }
        req.response.statusCode = r.$1;
        req.response.headers.set('Content-Type', r.$2);
        req.response.add(r.$3);
      } catch (_) {
        req.response.statusCode = 500;
      }
      await req.response.close();
    });
    return server.port;
  }

  Future<void> _proxy(HttpRequest req, String path) async {
    final upstream = '${meta['upstream']}';
    final client = HttpClient();
    try {
      final body = await req.fold<List<int>>(<int>[], (a, b) => a..addAll(b));
      final out = await client.openUrl(req.method, Uri.parse('$upstream/${path.replaceAll(RegExp(r'^/+'), '')}${req.uri.hasQuery ? '?${req.uri.query}' : ''}'));
      req.headers.forEach((k, v) {
        if (!const ['host', 'connection', 'accept-encoding', 'content-length', 'transfer-encoding', 'origin', 'referer'].contains(k)) {
          out.headers.set(k, v);
        }
      });
      out.headers.set('referer', '$upstream/');
      out.headers.set('origin', upstream);
      if (body.isNotEmpty) out.add(body);
      final resp = await out.close();
      req.response.statusCode = resp.statusCode;
      final ct = resp.headers.value('content-type');
      if (ct != null) req.response.headers.set('Content-Type', ct);
      await req.response.addStream(resp);
    } catch (_) {
      req.response.statusCode = 502;
    } finally {
      client.close();
      await req.response.close();
    }
  }
}
"""

_DART_MAIN = r"""// Clonjurer 应用壳(Android): 本地服务器 + WebView 显示任务产出的网站。
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:webview_flutter/webview_flutter.dart';

import 'site_server.dart';

const kTitle = %(title)s;
const kPort = %(port)d;

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const ShellApp());
}

class ShellApp extends StatelessWidget {
  const ShellApp({super.key});
  @override
  Widget build(BuildContext context) =>
      const MaterialApp(title: kTitle, debugShowCheckedModeBanner: false, home: ShellPage());
}

class ShellPage extends StatefulWidget {
  const ShellPage({super.key});
  @override
  State<ShellPage> createState() => _ShellPageState();
}

class _ShellPageState extends State<ShellPage> {
  WebViewController? _web;
  String? _error;

  @override
  void initState() {
    super.initState();
    _start();
  }

  Future<void> _start() async {
    try {
      final data = await rootBundle.load('assets/site.pack');
      final index = jsonDecode(await rootBundle.loadString('assets/site_index.json')) as Map<String, dynamic>;
      final server = SiteServer(data.buffer.asUint8List(data.offsetInBytes, data.lengthInBytes), index);
      int port;
      try {
        port = await server.serve(port: kPort); // 固定端口: 页面的 localStorage/cookie 跨次启动保留
      } catch (_) {
        port = await server.serve();
      }
      final origin = 'http://127.0.0.1:$port';
      final web = WebViewController()
        ..setJavaScriptMode(JavaScriptMode.unrestricted)
        ..setNavigationDelegate(NavigationDelegate(
            onNavigationRequest: (r) =>
                r.url.startsWith(origin) ? NavigationDecision.navigate : NavigationDecision.prevent))
        ..loadRequest(Uri.parse('$origin/'));
      setState(() => _web = web);
    } catch (e) {
      setState(() => _error = '$e');
    }
  }

  @override
  Widget build(BuildContext context) {
    final web = _web;
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) async {
        if (didPop) return;
        if (web != null && await web.canGoBack()) {
          await web.goBack();
        } else {
          await SystemNavigator.pop();
        }
      },
      child: Scaffold(
        body: SafeArea(
          child: web != null
              ? WebViewWidget(controller: web)
              : Center(child: _error != null ? Text(_error!) : const CircularProgressIndicator()),
        ),
      ),
    );
  }
}
"""

_DART_TEST = r"""// 冒烟测试: 本地服务器的路由 / 录制接口 / 静态资源解析(纯 Dart, 不依赖平台 WebView)。
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:generated_ui/site_server.dart';

void main() {
  test('site server resolves index, static and recorded api', () {
    final pack = File('assets/site.pack').readAsBytesSync();
    final index = jsonDecode(File('assets/site_index.json').readAsStringSync()) as Map<String, dynamic>;
    final s = SiteServer(pack, index);
    final home = s.resolve('/', 'GET')!;
    expect(home.$1, 200);
    expect(home.$2, startsWith('text/html'));
    expect(utf8.decode(home.$3, allowMalformed: true), contains('<'));
    expect(s.resolve('/__definitely_missing__/x.png', 'GET')?.$1 ?? 404, anyOf(404, 502));
    for (final m in s.mocks) {
      final r = s.resolve('${m['path']}', '${m['method'] ?? 'GET'}');
      expect(r, isNotNull);
      expect(r!.$1, int.tryParse('${m['status'] ?? 200}') ?? 200);
    }
  });
}
"""


def _pack_dir(site: Path, pack: Path, index: Path, meta: dict):
    files, off = {}, 0
    with open(pack, "wb") as fh:
        for p in sorted(site.rglob("*")):
            if not p.is_file() or p.name == "site_meta.json":
                continue
            data = p.read_bytes()
            fh.write(data)
            files[p.relative_to(site).as_posix()] = [off, len(data)]
            off += len(data)
    index.write_text(json.dumps({"meta": meta, "files": files}, ensure_ascii=False), encoding="utf-8")
    return off, len(files)


def flutter_shell(ws: Path, out: Path, title: str) -> Path:
    app_dir = out / "flutter_shell"
    if app_dir.exists():
        shutil.rmtree(app_dir)
    (app_dir / "lib").mkdir(parents=True)
    (app_dir / "assets").mkdir()
    (app_dir / "test").mkdir()
    tmp = out / "_site_tmp"
    meta = bundle_site(ws, tmp)
    _pack_dir(tmp, app_dir / "assets" / "site.pack", app_dir / "assets" / "site_index.json", meta)
    shutil.rmtree(tmp, ignore_errors=True)
    title = _safe_title(title)
    port = 47000 + (sum(title.encode("utf-8")) % 2000)
    dart_title = "'" + title.replace("\\", "\\\\").replace("'", "\\'").replace("$", "\\$") + "'"
    (app_dir / "lib" / "main.dart").write_text(_DART_MAIN % {"title": dart_title, "port": port}, encoding="utf-8")
    (app_dir / "lib" / "site_server.dart").write_text(_DART_SERVER, encoding="utf-8")
    (app_dir / "test" / "site_server_test.dart").write_text(_DART_TEST, encoding="utf-8")
    (app_dir / "pubspec.yaml").write_text(f"""name: generated_ui
description: Packaged by Clonjurer from a task's website output.
publish_to: 'none'
version: 0.1.0
environment:
  sdk: '>=3.3.0 <4.0.0'
dependencies:
  flutter:
    sdk: flutter
  webview_flutter: {WEBVIEW_FLUTTER_VERSION}
dev_dependencies:
  flutter_test:
    sdk: flutter
flutter:
  uses-material-design: true
  assets:
    - assets/site.pack
    - assets/site_index.json
""", encoding="utf-8")
    (app_dir / "analysis_options.yaml").write_text("analyzer:\n  exclude: [build/**]\n", encoding="utf-8")
    # 交给 toolchain.build_apk: 需要联网权限访问本机服务器, 且只对 127.0.0.1 放行明文 http
    (app_dir / "clonjurer_android.json").write_text(json.dumps(
        {"label": title, "internet": True, "cleartext_hosts": ["127.0.0.1"]}, ensure_ascii=False), encoding="utf-8")
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
        log(f"产物 {a['label']}: {a['file']} ({a['bytes'] / 1048576:.1f} MB)")
    return {"ok": bool(arts), "artifacts": arts, "gap": gap, "signing": res.get("signing"),
            "analyze_ok": res.get("analyze_ok"), "test_ok": res.get("test_ok")}


def package_task(task_id, ws: Path, kind: str, title: str = "", viewport: dict = None, log=print) -> dict:
    """任务网站产物 → 应用壳 → 构建 → 导出; 结果同时写入 ws/build_report.json。不抛异常。"""
    from core.uir import toolchain
    ws = Path(ws)
    try:
        if kind == "desktop":
            app_dir = electron_shell(ws, ws / "app_shell", title, viewport)
            host = toolchain.host_platform()
            res = toolchain.build_desktop(app_dir, [host] + (["win32"] if host != "win32" else []), log=log)
        elif kind == "apk":
            app_dir = flutter_shell(ws, ws / "app_shell", title)
            res = toolchain.build_apk(app_dir, release=True, log=log, org=f"com.clonjurer.task{task_id}")
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
