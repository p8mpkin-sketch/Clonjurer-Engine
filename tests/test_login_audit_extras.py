"""登录审计补漏: 被验证码拦下的登录尝试也记为登录失败; 导出类响应的 Content-Type 只带一个 charset。

在独立子进程里导入 app(临时数据库与工作目录)。"""
import importlib.util
import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = textwrap.dedent(r'''
    import json, sys, tempfile
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    import config
    tmp = Path(tempfile.mkdtemp())
    config.DB_PATH = tmp / "t.db"
    config.WORKSPACE_ROOT = tmp / "ws"; config.WORKSPACE_ROOT.mkdir()
    config.EXPORTS_DIR = tmp / "ex"; config.EXPORTS_DIR.mkdir()
    config.CAPTCHA_MODE = "always"
    import app as A
    A.scheduler.start = lambda *a, **k: None
    A.time.sleep = lambda s: None
    out = {}
    admin = A.app.test_client()
    admin.post("/api/auth/bootstrap", json={"username": "alice", "password": "pw-alice-123"})
    admin.post("/api/v4/users", json={"username": "bob", "password": "pw-bob-12345", "role": "operator"})
    x = A.app.test_client()
    out["no_captcha"] = x.post("/api/auth/login", json={"username": "bob", "password": "pw-bob-12345"}).status_code
    out["bad_captcha"] = x.post("/api/auth/login", json={"username": "bob", "password": "pw-bob-12345",
                                                         "captcha_id": "nope", "captcha": "1234"}).status_code
    out["ghost"] = x.post("/api/auth/login", json={"username": "ghost", "password": "x"}).status_code
    rows = admin.get("/api/v4/audit?size=200").get_json()
    items = rows.get("items") or rows.get("entries") or rows.get("rows") or []
    out["failed_details"] = sorted(e["detail"] for e in items if e["action"] == "login.failed")
    out["ctypes"] = [admin.get(p).headers.get("Content-Type") for p in
                     ("/api/v4/audit/export", "/api/v4/users/export")]
    print("RESULT" + json.dumps(out, ensure_ascii=False))
''')


@unittest.skipUnless(importlib.util.find_spec("flask"), "需要 flask(平台依赖)")
class LoginAuditExtras(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True, text=True,
                              timeout=180, cwd=ROOT)
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")]
        if not line:
            raise AssertionError(proc.stdout[-3000:] + proc.stderr[-3000:])
        cls.out = json.loads(line[-1][6:])

    def test_captcha_rejections_are_audited(self):
        o = self.out
        self.assertEqual([o["no_captcha"], o["bad_captcha"], o["ghost"]], [400, 400, 400])
        d = o["failed_details"]
        self.assertTrue(any("bob" in x and "未填验证码" in x for x in d), d)
        self.assertTrue(any("bob" in x and "验证码不正确" in x for x in d), d)
        self.assertTrue(any("ghost" in x for x in d), d)

    def test_single_charset(self):
        for ct in self.out["ctypes"]:
            self.assertEqual(ct, "text/csv; charset=utf-8")


if __name__ == "__main__":
    unittest.main()
