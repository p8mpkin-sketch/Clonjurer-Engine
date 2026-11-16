"""Web 安全加固: 验证码 / 登录限流 / 同源校验 / Host 白名单 / 预览独立源 / 开放模式只限本机 /
路径越界 / 短链按工作区隔离 / 凭据不回显 / 反向代理不转发平台 Cookie / 新密码规则。

纯函数部分直接测; 接口部分在独立子进程里导入 app(临时数据库与工作目录), 不污染其他测试与本机实例。"""
import importlib.util
import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import websec  # noqa: E402


class WebsecPureTest(unittest.TestCase):
    def test_host_allowed(self):
        self.assertTrue(websec.host_allowed("127.0.0.1:8899", "127.0.0.1", set()))
        self.assertTrue(websec.host_allowed("localhost:8899", "127.0.0.1", set()))
        self.assertTrue(websec.host_allowed("[::1]:8899", "127.0.0.1", set()))
        self.assertFalse(websec.host_allowed("evil.example:8899", "127.0.0.1", set()))   # DNS 重绑定
        self.assertTrue(websec.host_allowed("cloner.lan", "127.0.0.1", {"cloner.lan"}))
        self.assertTrue(websec.host_allowed("10.0.0.5:8899", "0.0.0.0", set()))           # 对外监听默认不限
        self.assertFalse(websec.host_allowed("10.0.0.5:8899", "0.0.0.0", {"cloner.lan"}))
        self.assertFalse(websec.host_allowed("", "127.0.0.1", set()))

    def test_same_origin(self):
        ok = websec.same_origin_ok
        self.assertTrue(ok("http://localhost:8899", "", "localhost:8899", set()))
        self.assertFalse(ok("http://localhost:8900", "", "localhost:8899", set()))  # 预览端口不算同源
        self.assertFalse(ok("https://evil.com", "", "localhost:8899", set()))
        self.assertFalse(ok("null", "", "localhost:8899", set()))
        self.assertFalse(ok("", "https://evil.com/x", "localhost:8899", set()))
        self.assertTrue(ok("", "http://localhost:8899/#/x", "localhost:8899", set()))
        self.assertTrue(ok("", "", "localhost:8899", set()))                        # curl 等非浏览器
        trusted = websec.origin_netlocs("https://cloner.example.com, http://10.0.0.5:8899")
        self.assertEqual(trusted, {"cloner.example.com", "10.0.0.5:8899"})
        self.assertTrue(ok("https://cloner.example.com", "", "127.0.0.1:8899", trusted))
        self.assertFalse(ok("http://10.0.0.5:8900", "", "127.0.0.1:8899", trusted))

    def test_client_ip(self):
        ip = websec.client_ip
        self.assertEqual(ip("203.0.113.9", "1.2.3.4", set()), "203.0.113.9")        # 直连: 不信 XFF
        self.assertEqual(ip("127.0.0.1", "1.2.3.4", set()), "1.2.3.4")              # 本机反代
        self.assertEqual(ip("127.0.0.1", "6.6.6.6, 1.2.3.4", set()), "1.2.3.4")     # 取最近一跳, 伪造的最左项无效
        self.assertEqual(ip("10.0.0.2", "1.2.3.4", {"10.0.0.2"}), "1.2.3.4")
        self.assertEqual(ip("", "", set()), "local")

    def test_forbidden_target(self):
        bad = ["169.254.169.254", "metadata.google.internal", "0.0.0.0", "0xa9.0xfe.0xa9.0xfe", "2852039166",
               "224.0.0.1", "fe80::1"]
        for h in bad:
            self.assertTrue(websec.forbidden_target(h), h)
        for h in ["example.com", "10.0.0.8", "192.168.1.20", "127.0.0.1", "8.8.8.8"]:
            self.assertEqual(websec.forbidden_target(h), "", h)

    def test_forbidden_target_resolves_hostnames(self):
        import socket
        from unittest import mock
        websec._resolve_cache.clear()
        evil = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
        fine = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        with mock.patch("socket.getaddrinfo", side_effect=lambda h, *a, **k: evil if h == "evil.example" else fine):
            self.assertEqual(websec.forbidden_target("evil.example"), "")          # 默认不解析
            self.assertIn("域名解析到", websec.forbidden_target("evil.example", resolve=True))
            self.assertEqual(websec.forbidden_target("good.example", resolve=True), "")
        # localhost 在多数 Linux 上先解析出 ::1(Python 里 ::1 也算 is_reserved), 本机地址必须放行
        loop6 = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        with mock.patch("socket.getaddrinfo", return_value=loop6):
            self.assertEqual(websec.forbidden_target("localhost", resolve=True), "")
        self.assertEqual(websec.forbidden_target("::1"), "")
        with mock.patch("socket.getaddrinfo", side_effect=OSError("nxdomain")):
            self.assertEqual(websec.forbidden_target("nx.example", resolve=True), "")
        websec._resolve_cache.clear()

    def test_guarded_session_blocks_metadata_and_redirect_hops(self):
        import requests
        sess = websec.guard_session(requests.Session())
        with self.assertRaises(requests.ConnectionError) as cm:
            sess.get("http://169.254.169.254/latest/meta-data/", timeout=2)
        self.assertIn("已拦截", str(cm.exception))
        # 每一跳都经适配器: 模拟一个 302 到元数据地址的响应
        from unittest import mock
        from requests.adapters import HTTPAdapter
        hop = requests.Response()
        hop.status_code, hop.headers["Location"], hop.url = 302, "http://169.254.169.254/x", "http://a.example/"
        class _Raw:   # 没有 _original_response: requests 不从它解析 Cookie
            def release_conn(self):
                pass

            def close(self):
                pass
        hop.raw = _Raw()
        hop._content = b""
        with mock.patch.object(HTTPAdapter, "send", side_effect=lambda req, *a, **k: setattr(hop, "request", req) or hop):
            with self.assertRaises(requests.ConnectionError):
                sess.get("http://a.example/", timeout=2)

    def test_failure_counter(self):
        c = websec.FailureCounter(60)
        self.assertEqual(c.count("a"), 0)
        c.add("a")
        self.assertEqual(c.add("a"), 2)
        self.assertGreater(c.retry_after("a"), 0)
        c.clear("a")
        self.assertEqual(c.count("a"), 0)


@unittest.skipUnless(importlib.util.find_spec("PIL"), "需要 pillow")
class CaptchaTest(unittest.TestCase):
    def test_one_time_and_case_insensitive(self):
        from core import captcha
        seen = {}
        orig = captcha.render_png
        captcha.render_png = lambda text, rng=None: seen.setdefault("t", text) and b"png"
        try:
            ch = captcha.new_challenge()
        finally:
            captcha.render_png = orig
        self.assertTrue(ch["image"].startswith("data:image/png;base64,"))
        self.assertFalse(captcha.verify(ch["id"], "zzzz"))           # 错一次即作废
        self.assertFalse(captcha.verify(ch["id"], seen["t"]))
        captcha.render_png = lambda text, rng=None: seen.__setitem__("t", text) or b"png"
        try:
            ch = captcha.new_challenge()
        finally:
            captcha.render_png = orig
        self.assertTrue(captcha.verify(ch["id"], seen["t"].lower()))
        self.assertFalse(captcha.verify(ch["id"], seen["t"]))        # 不能重放
        self.assertFalse(captcha.verify("", "x"))

    def test_render(self):
        from core import captcha
        png = captcha.render_png("K7PX")
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")


SCRIPT = textwrap.dedent(r'''
    import json, sys, tempfile
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    import config
    tmp = Path(tempfile.mkdtemp())
    config.DB_PATH = tmp / "t.db"
    config.WORKSPACE_ROOT = tmp / "ws"; config.WORKSPACE_ROOT.mkdir()
    config.EXPORTS_DIR = tmp / "ex"; config.EXPORTS_DIR.mkdir()
    config.LOGIN_IP_MAX = 8
    from core.scheduler import Scheduler as _Sched
    _Sched.start = lambda self: None   # 导入 app 时 _boot() 就会起调度线程, 事后改实例来不及
    import app as A
    from core import captcha
    A.scheduler.start = lambda *a, **k: None
    A.time.sleep = lambda s: None
    answers = {}
    _orig_render = captcha.render_png
    def _spy(text, rng=None):
        answers["last"] = text
        return _orig_render(text, rng)
    captcha.render_png = _spy
    out = {}
    H = {"Origin": "http://localhost"}

    # ---- 开放模式只限本机 ----
    config.HOST = "0.0.0.0"
    remote = A.app.test_client()
    env = {"REMOTE_ADDR": "10.0.0.9"}
    out["remote_open_api"] = remote.get("/api/tasks", environ_overrides=env).status_code
    st = remote.get("/api/auth/state", environ_overrides=env).get_json()
    out["remote_state"] = [st["required"], st["setup_token_required"]]
    out["remote_boot_no_token"] = remote.post("/api/auth/bootstrap", environ_overrides=env,
        json={"username": "alice", "password": "pw-alice-123"}).status_code
    out["local_open_api"] = A.app.test_client().get("/api/tasks").status_code
    out["remote_boot_token"] = remote.post("/api/auth/bootstrap", environ_overrides=env,
        json={"username": "alice", "password": "pw-alice-123", "setup_token": A._SETUP_TOKEN}).status_code
    config.HOST = "127.0.0.1"
    admin = remote

    # ---- Host 白名单(回环监听) / 同源校验 / 安全响应头 ----
    out["evil_host"] = A.app.test_client().get("/", headers={"Host": "evil.example"}).status_code
    out["csrf_foreign"] = admin.post("/api/v4/projects", json={"name": "x1"},
                                     headers={"Origin": "https://evil.com"}).status_code
    out["csrf_preview_port"] = admin.post("/api/v4/projects", json={"name": "x2"},
                                          headers={"Origin": "http://localhost:8900"}).status_code
    out["csrf_same"] = admin.post("/api/v4/projects", json={"name": "同源项目"}, headers=H).status_code
    r = A.app.test_client().get("/")
    out["headers"] = [r.headers.get("X-Frame-Options"), r.headers.get("X-Content-Type-Options"),
                      "frame-ancestors" in (r.headers.get("Content-Security-Policy") or "")]
    out["api_no_store"] = admin.get("/api/tasks").headers.get("Cache-Control")

    # ---- 新密码规则 ----
    out["weak_pw"] = [admin.post("/api/v4/users", json={"username": u, "password": p, "role": "operator"}).status_code
                      for u, p in (("w1", "12345678"), ("w2", "abcdefgh"), ("w3", "password123"), ("weakuser1", "WeakUser1"))]
    out["ok_pw"] = admin.post("/api/v4/users", json={"username": "bob", "password": "pw-bob-1234",
                                                     "role": "operator"}).status_code
    out["add_existing_no_hash"] = "password_hash" not in json.dumps(
        admin.post("/api/v4/users", json={"username": "bob", "password": "x"}).get_json() or {})

    # ---- 验证码: 同一用户名输错 2 次后要求; 不存在的用户名同样对待(不暴露账号存在性) ----
    eve = A.app.test_client()
    codes = []
    for _ in range(2):
        r = eve.post("/api/auth/login", json={"username": "bob", "password": "wrong-pass"})
        codes.append([r.status_code, r.get_json().get("captcha_required")])
    out["first_fails"] = codes
    r = eve.post("/api/auth/login", json={"username": "bob", "password": "pw-bob-1234"})
    out["need_captcha"] = [r.status_code, r.get_json().get("captcha_required")]
    ch = eve.get("/api/auth/captcha").get_json()
    r = eve.post("/api/auth/login", json={"username": "bob", "password": "pw-bob-1234",
                                          "captcha_id": ch["id"], "captcha": "nope"})
    out["bad_captcha"] = r.status_code
    out["replay_after_bad"] = eve.post("/api/auth/login", json={"username": "bob", "password": "pw-bob-1234",
        "captcha_id": ch["id"], "captcha": answers["last"]}).status_code
    ch = eve.get("/api/auth/captcha").get_json()
    out["captcha_ok_login"] = eve.post("/api/auth/login", json={"username": "bob", "password": "pw-bob-1234",
        "captcha_id": ch["id"], "captcha": answers["last"].lower()}).status_code
    ghost = A.app.test_client()
    g_codes = [ghost.post("/api/auth/login", json={"username": "nobody", "password": "x-1"},
                          environ_overrides={"REMOTE_ADDR": "192.0.2.1"}).get_json()
               .get("captcha_required") for _ in range(2)]
    out["ghost_captcha"] = g_codes

    # ---- 来源 IP 限流: 同一 IP 失败达上限后 429 ----
    spray = A.app.test_client()
    env2 = {"REMOTE_ADDR": "198.51.100.7"}
    last = None
    for i in range(12):
        last = spray.post("/api/auth/login", environ_overrides=env2,
                          json={"username": f"u{i}", "password": "nope-123"}).status_code
    out["ip_block"] = last
    out["other_ip_ok"] = A.app.test_client().post("/api/auth/login", environ_overrides={"REMOTE_ADDR": "198.51.100.8"},
        json={"username": "zz", "password": "nope-123"}).status_code
    out["xff_spoof_ignored"] = A.app.test_client().post("/api/auth/login",
        environ_overrides=env2, headers={"X-Forwarded-For": "1.1.1.1"},
        json={"username": "zz", "password": "nope-123"}).status_code

    # ---- 路径越界: 任务 1 的文件接口不能读到工作区 12 ----
    bid = A.db.create_batch("b", [("https://example.com", "example.com")], {"cookie": "SID=secret", "bearer": "tok"},
                            workspace_id=1)
    tid = A.db.list_tasks(batch_id=bid, workspace_id=1)[0]["id"]
    ws = config.WORKSPACE_ROOT / str(tid); ws.mkdir()
    (ws / "index.html").write_text("<html><head></head><body>hi</body></html>")
    (ws / "site.json").write_text('{"routes": {"/": "index.html"}}')
    other = config.WORKSPACE_ROOT / (str(tid) + "2"); other.mkdir()
    (other / "secret.txt").write_text("other-tenant")
    A.db.update_task(tid, status="completed", workspace_path=str(ws))
    r = admin.get(f"/api/tasks/{tid}/file?path=../{tid}2/secret.txt")
    out["traversal"] = [r.status_code, "other-tenant" in r.get_data(as_text=True)]
    out["file_ok"] = admin.get(f"/api/tasks/{tid}/file?path=index.html").status_code

    # ---- 批次配置不回显凭据 ----
    blob = json.dumps(admin.get("/api/batches").get_json(), ensure_ascii=False)
    out["batch_redacted"] = ["SID=secret" not in blob, "cookie_set" in blob]

    # ---- 克隆目标拦截元数据地址 ----
    r = admin.post("/api/batches", json={"urls": "http://169.254.169.254/latest/meta-data/"})
    out["metadata_blocked"] = r.status_code

    # ---- 短链按工作区隔离 ----
    l1 = A.db.create_short_link(None, "https://a.example", "isgd", "https://is.gd/a", "", {}, workspace_id=1)
    bob = A.app.test_client()
    ch = bob.get("/api/auth/captcha").get_json()
    bob.post("/api/auth/login", json={"username": "bob", "password": "pw-bob-1234",
                                      "captcha_id": ch["id"], "captcha": answers["last"]})
    bob.post("/api/v4/workspaces", json={"name": "乙团队"})
    out["b_sees_links"] = len(bob.get("/api/v4/links").get_json()["links"])
    out["b_delete_a_link"] = bob.delete(f"/api/v4/links/{l1}").status_code
    out["a_sees_links"] = len(admin.get("/api/v4/links").get_json()["links"])

    # ---- 反向代理兜底不转发平台 Cookie / Authorization ----
    seen = {}
    class _R:
        status_code = 200; content = b"ok"; headers = {"content-type": "text/plain"}
    def _fake(self, method, url, headers=None, **kw):
        seen.update(headers or {}); return _R()
    A.upstream_http.Session.request = _fake   # 兜底代理走带拦截适配器的 Session
    with A.app.test_request_context("/preview/1/x", headers={"Cookie": "clonjurer_session=abc",
                                    "Authorization": "Bearer t", "Accept": "*/*", "X-Requested-With": "XMLHttpRequest"}):
        A._proxy_fallback("https://origin.example", "x")
    low = {k.lower() for k in seen}
    out["proxy_headers"] = ["cookie" not in low, "authorization" not in low, "accept" in low, "x-requested-with" in low]

    # ---- 预览独立源 ----
    A._PREVIEW["on"] = True
    config.PREVIEW_PORT = 8900
    pv = {"SERVER_PORT": "8900"}
    r = admin.get(f"/preview/{tid}/")
    out["main_preview_redirect"] = [r.status_code, r.headers.get("Location", "")]
    r = admin.get(f"/preview/{tid}/", environ_overrides=pv)
    out["preview_side_page"] = [r.status_code, b"preview-bridge" in r.data]
    r = admin.get("/api/tasks", environ_overrides=pv, headers={"Referer": f"http://localhost:8900/preview/{tid}/"})
    out["preview_side_api"] = [r.status_code, b'"tasks"' in r.data]
    r = admin.get("/", environ_overrides=pv)
    out["preview_side_root"] = r.status_code
    out["state_preview_base"] = admin.get("/api/auth/state").get_json()["preview_base"]
    r = admin.get("/", headers={"Referer": f"http://localhost:8899/preview/{tid}/"})
    out["main_root_no_fallback"] = b"clonjurer_session" not in r.data and b"<body>hi</body>" not in r.data
    print(json.dumps(out, ensure_ascii=False))
''')


@unittest.skipUnless(importlib.util.find_spec("flask") and importlib.util.find_spec("PIL"), "需要 flask / pillow")
class WebsecApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True,
                           text=True, timeout=180, cwd=str(ROOT))
        if p.returncode != 0:
            raise AssertionError(p.stderr[-4000:])
        cls.out = json.loads(p.stdout.strip().splitlines()[-1])

    def test_open_mode_local_only(self):
        o = self.out
        self.assertEqual(o["remote_open_api"], 401)
        self.assertEqual(o["remote_state"], [True, True])
        self.assertEqual(o["remote_boot_no_token"], 403)
        self.assertEqual(o["local_open_api"], 200)
        self.assertEqual(o["remote_boot_token"], 201)

    def test_host_and_origin(self):
        o = self.out
        self.assertEqual(o["evil_host"], 421)
        self.assertEqual(o["csrf_foreign"], 403)
        self.assertEqual(o["csrf_preview_port"], 403)
        self.assertEqual(o["csrf_same"], 201)
        self.assertEqual(o["headers"], ["SAMEORIGIN", "nosniff", True])
        self.assertEqual(o["api_no_store"], "no-store")

    def test_password_policy(self):
        self.assertEqual(self.out["weak_pw"], [400, 400, 400, 400])
        self.assertEqual(self.out["ok_pw"], 201)
        self.assertTrue(self.out["add_existing_no_hash"])

    def test_captcha_flow(self):
        o = self.out
        self.assertEqual(o["first_fails"], [[401, False], [401, True]])
        self.assertEqual(o["need_captcha"], [400, True])
        self.assertEqual(o["bad_captcha"], 400)
        self.assertEqual(o["replay_after_bad"], 400)
        self.assertEqual(o["captcha_ok_login"], 200)
        self.assertEqual(o["ghost_captcha"], [False, True])

    def test_ip_throttle(self):
        self.assertEqual(self.out["ip_block"], 429)
        self.assertEqual(self.out["other_ip_ok"], 401)
        self.assertEqual(self.out["xff_spoof_ignored"], 429)

    def test_path_traversal(self):
        self.assertEqual(self.out["traversal"], [400, False])
        self.assertEqual(self.out["file_ok"], 200)

    def test_secrets_and_targets(self):
        self.assertEqual(self.out["batch_redacted"], [True, True])
        self.assertEqual(self.out["metadata_blocked"], 400)

    def test_short_links_isolated(self):
        self.assertEqual(self.out["b_sees_links"], 0)
        self.assertEqual(self.out["b_delete_a_link"], 404)
        self.assertEqual(self.out["a_sees_links"], 1)

    def test_proxy_strips_platform_credentials(self):
        self.assertEqual(self.out["proxy_headers"], [True, True, True, True])

    def test_preview_isolation(self):
        o = self.out
        self.assertEqual(o["main_preview_redirect"][0], 302)
        self.assertTrue(o["main_preview_redirect"][1].startswith("http://localhost:8900/preview/"))
        self.assertEqual(o["preview_side_page"], [200, True])
        self.assertEqual(o["preview_side_api"][1], False)
        self.assertEqual(o["preview_side_root"], 404)
        self.assertEqual(o["state_preview_base"], "http://localhost:8900")
        self.assertTrue(o["main_root_no_fallback"])


if __name__ == "__main__":
    unittest.main()
