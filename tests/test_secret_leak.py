"""接口不外泄凭据: 用户/成员/远程发布/系统设置/定时任务等接口的响应里不得出现
密码散列、Fernet 密文或明文 Key。新增接口若直接把 db 行 jsonify 出去, 这里会红。

在独立子进程里导入 app(临时数据库与工作目录), 不污染其他测试与本机实例。"""
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
    config.CAPTCHA_MODE = "off"
    from core.scheduler import Scheduler as _Sched
    _Sched.start = lambda self: None   # 导入 app 时 _boot() 就会起调度线程, 事后改实例来不及
    import app as A
    A.scheduler.start = lambda *a, **k: None
    A.time.sleep = lambda s: None
    AI_KEY = "sk-leakcheck-0123456789abcdef"
    bodies = {}
    c = A.app.test_client()
    def rec(name, resp):
        bodies[name] = {"status": resp.status_code, "text": resp.get_data(as_text=True)}
        return resp
    rec("bootstrap", c.post("/api/auth/bootstrap", json={"username": "alice", "password": "pw-alice-123"}))
    rec("user_create", c.post("/api/v4/users", json={"username": "bob", "password": "pw-bob-12345", "role": "operator"}))
    rec("user_create_admin", c.post("/api/v4/users", json={"username": "dave", "password": "pw-dave-12345", "role": "admin"}))
    rec("invite", c.post("/api/v4/invites", json={"role": "viewer"}))
    rec("settings_put", c.put("/api/v4/admin/settings", json={"ai": {"api_key": AI_KEY, "model": "deepseek-chat"}}))
    rec("schedule_create", c.post("/api/v4/schedules", json={"name": "s", "urls": ["https://example.com/"], "freq": "daily"}))
    ids = {u["username"]: u["id"] for u in c.get("/api/v4/users").get_json()["users"]}
    for name, path in [
        ("auth_state", "/api/auth/state"), ("users", "/api/v4/users"),
        ("user_detail", f"/api/v4/users/{ids['bob']}"), ("workspaces", "/api/v4/workspaces"),
        ("settings", "/api/v4/admin/settings"), ("admin_ws", "/api/v4/admin/workspaces"),
        ("remote", "/api/v4/remote/deploys"), ("schedules", "/api/v4/schedules"),
        ("ai_config", "/api/ai/config"), ("audit", "/api/v4/audit?size=200"),
        ("users_export", "/api/v4/users/export"), ("audit_export", "/api/v4/audit/export"),
    ]:
        rec(name, c.get(path))
    rec("user_patch", c.patch(f"/api/v4/users/{ids['bob']}", json={"role": "viewer"}))
    print(json.dumps({"ai_key": AI_KEY, "bodies": bodies}))
''')


@unittest.skipUnless(importlib.util.find_spec("flask") and importlib.util.find_spec("cryptography"),
                     "需要 flask / cryptography(平台依赖)")
class NoSecretInResponses(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True,
                              text=True, timeout=180, cwd=str(ROOT))
        if proc.returncode != 0:
            raise AssertionError(proc.stderr[-3000:])
        cls.out = json.loads(proc.stdout.strip().splitlines()[-1])

    def test_all_calls_succeeded(self):
        bad = {k: v["status"] for k, v in self.out["bodies"].items() if v["status"] >= 400}
        self.assertEqual(bad, {})

    def test_no_password_hash_or_ciphertext(self):
        for name, v in self.out["bodies"].items():
            text = v["text"]
            with self.subTest(endpoint=name):
                self.assertNotIn("password_hash", text)
                self.assertNotIn("password_enc", text)
                self.assertNotIn("pbkdf2:", text)
                self.assertNotIn("gAAAAA", text)  # Fernet 密文前缀

    def test_ai_key_never_echoed_in_plain(self):
        for name, v in self.out["bodies"].items():
            with self.subTest(endpoint=name):
                self.assertNotIn(self.out["ai_key"], v["text"])


if __name__ == "__main__":
    unittest.main()
