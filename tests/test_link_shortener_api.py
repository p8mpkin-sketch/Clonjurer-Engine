"""短链管理 API: 新建 -> 后台生成 -> 列表/详情 -> 导出 -> /s/<短码> 跳转计数 -> 删除级联;
默认本站短码不外呼; 平台对外地址可达时公共短链指向本站短码; 老短链 /s/<编号> 兼容, 新短链不可按编号枚举。

在独立子进程里导入 app(临时数据库与工作目录), 外呼全部 mock 在 requests.request 上, 不打真实网络。"""
import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from tests._env import has_modules

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = textwrap.dedent(r'''
    import json, sys, tempfile, time
    from pathlib import Path
    from unittest import mock
    sys.path.insert(0, sys.argv[1])
    import config
    tmp = Path(tempfile.mkdtemp())
    config.DB_PATH = tmp / "t.db"
    config.WORKSPACE_ROOT = tmp / "ws"; config.WORKSPACE_ROOT.mkdir()
    config.EXPORTS_DIR = tmp / "ex"; config.EXPORTS_DIR.mkdir()
    import app as A
    from core import link_shortener as ls
    A.scheduler.start = lambda *a, **k: None

    class R:
        def __init__(self, status=200, text=""):
            self.status_code, self.text, self.reason = status, text, ""
    TABLE = {"tinyurl.com": R(text="https://tinyurl.com/t1"), "clck.ru": R(text="https://clck.ru/c1"),
             "lnk.ua": R(500, "down"), "url.asia": R(text='{"short_url": "https://url.asia/u1"}')}
    calls = []
    sent = []   # 交给公共服务缩的地址
    def fake(method, url, **kw):
        calls.append(url)
        sent.append((kw.get("params") or kw.get("data") or {}).get("url") or (kw.get("data") or {}).get("link"))
        for host, resp in TABLE.items():
            if host in url:
                return resp
        raise AssertionError("unmocked " + url)
    mock.patch.object(ls.requests, "request", side_effect=fake).start()

    out = {}
    c = A.app.test_client()
    out["bad_scheme"] = c.post("/api/v4/link-tasks", json={"name": "x", "target_url": "javascript:alert(1)"}).status_code
    out["bad_service"] = c.post("/api/v4/link-tasks", json={"name": "x", "target_url": "https://a.example/",
                                                           "service": "bitly"}).status_code
    out["no_name"] = c.post("/api/v4/link-tasks", json={"target_url": "https://a.example/"}).status_code
    out["bad_clone"] = c.post("/api/v4/link-tasks", json={"name": "x", "target_url": "https://a.example/",
                                                         "clone_task_id": 999}).status_code
    r = c.post("/api/v4/link-tasks", json={"name": "活动页", "target_url": "https://a.example/landing",
                                           "service": "all", "note": "n", "generate": True})
    out["create"] = r.status_code
    tid = r.get_json()["task"]["id"]
    for _ in range(200):
        d = c.get(f"/api/v4/link-tasks/{tid}").get_json()["task"]
        if d["status"] != "running":
            break
        time.sleep(0.02)
    out["detail"] = [d["status"], d["link_count"], d["failed_count"], d["services"]]
    lst = c.get("/api/v4/link-tasks").get_json()
    out["list"] = [len(lst["tasks"]), lst["kpis"]]
    out["search_hit"] = len(c.get("/api/v4/link-tasks?q=活动").get_json()["tasks"])
    out["search_miss"] = len(c.get("/api/v4/link-tasks?q=nothing").get_json()["tasks"])
    ok = [l for l in d["links"] if l["ok"]]
    bad = [l for l in d["links"] if not l["ok"]][0]
    out["local_url"] = ok[0]["local_url"].endswith(f"/s/{ok[0]['code']}") and len(ok[0]["code"]) == 8
    local_row = [l for l in ok if l["service"] == "local"][0]
    out["local_row"] = [local_row["short_url"] == local_row["local_url"], local_row["short_url"].startswith("http://localhost/s/")]
    out["via_local"] = sorted({bool((l.get("extras") or {}).get("via_local")) for l in ok if l["service"] != "local"})
    out["sent_target"] = sorted(set(u for u in sent if u))
    pub = [l for l in ok if l["service"] != "local"]
    txt = c.get(f"/api/v4/link-tasks/{tid}/export?format=txt")
    out["txt"] = txt.data.decode().split()
    csvr = c.get(f"/api/v4/link-tasks/{tid}/export?format=csv")
    out["csv"] = [csvr.status_code, csvr.mimetype, csvr.data.decode("utf-8-sig").count("\n"),
                  "失败" in csvr.data.decode("utf-8-sig")]
    out["bad_fmt"] = c.get(f"/api/v4/link-tasks/{tid}/export?format=xls").status_code
    s = c.get(f"/s/{pub[0]['code']}")
    out["redirect"] = [s.status_code, s.headers.get("Location")]
    c.get(f"/s/{pub[0]['code']}")
    out["clicks"] = A.db.get_short_link(pub[0]["id"])["clicks"]
    out["numeric_new"] = c.get(f"/s/{pub[0]['id']}").status_code   # 新短链不认编号
    out["failed_redirect"] = c.get(f"/s/{bad['code']}").status_code
    out["missing_redirect"] = [c.get("/s/99999").status_code, c.get("/s/zzzzzzzz").status_code,
                               c.get("/s/bad..code").status_code]
    out["kpis_after"] = c.get("/api/v4/link-tasks").get_json()["kpis"]
    # 再生成: 只重试失败的 lnk.ua
    TABLE["lnk.ua"] = R(text='{"result": {"lnk": "https://lnk.ua/k1"}}')
    calls.clear()
    out["regen"] = c.post(f"/api/v4/link-tasks/{tid}/generate").status_code
    for _ in range(200):
        d = c.get(f"/api/v4/link-tasks/{tid}").get_json()["task"]
        if d["status"] != "running":
            break
        time.sleep(0.02)
    out["regen_calls"] = calls[:]
    out["regen_detail"] = [d["status"], d["link_count"], d["failed_count"]]
    # IP 目标 + 单一服务 url.asia: 自动顺延, 不碰 url.asia
    calls.clear()
    r = c.post("/api/v4/links", json={"target_url": "http://47.92.75.70/cas/", "service": "urlasia"})
    out["ip_single"] = [r.status_code, r.get_json()["link"]["service"], any("url.asia" in u for u in calls)]
    out["standalone"] = len(c.get("/api/v4/links?standalone=1").get_json()["links"])
    out["all_links"] = len(c.get("/api/v4/links").get_json()["links"])
    # 默认 = 本站短码, 不外呼
    calls.clear()
    r = c.post("/api/v4/links", json={"target_url": "https://c.example/"})
    lk = r.get_json()["link"]
    out["default_local"] = [r.status_code, lk["service"], lk["ok"], calls[:], lk["short_url"].endswith("/s/" + lk["code"])]
    # 平台对外地址可达: 公共短链缩的是本站 /s/<短码>
    config.PUBLIC_URL = "https://clone.example.com"
    sent.clear()
    r = c.post("/api/v4/links", json={"target_url": "https://d.example/", "service": "tinyurl"})
    lk = r.get_json()["link"]
    out["public_base"] = [r.status_code, sent[:], "https://clone.example.com/s/" + lk["code"], lk["extras"]["via_local"],
                          lk["local_url"]]
    config.PUBLIC_URL = ""
    # 老短链(改版前): 迁移补了短码, 且仍认 /s/<编号>
    with A.db._lock:
        A.db._conn.execute("UPDATE short_links SET legacy_numeric = 1 WHERE id = ?", (pub[1]["id"],))
        A.db._conn.commit()
    out["legacy_numeric"] = c.get(f"/s/{pub[1]['id']}").status_code
    # 全部失败: 502 且留痕
    for k in TABLE:
        TABLE[k] = R(503, "")
    r = c.post("/api/v4/links", json={"target_url": "https://b.example/", "service": "tinyurl"})
    out["all_fail"] = [r.status_code, r.get_json()["link"]["ok"], len(r.get_json()["attempts"])]
    ls._preflight_cache.update(at=0.0, items=None)
    prov = c.get("/api/v4/links/providers").get_json()["providers"]
    out["providers"] = [[p["key"], p["available"], p["supports_ip"]] for p in prov]
    out["del_link"] = c.delete(f"/api/v4/links/{ok[1]['id']}").status_code
    out["del_task"] = c.delete(f"/api/v4/link-tasks/{tid}").status_code
    out["after_del"] = [c.get(f"/api/v4/link-tasks/{tid}").status_code, c.get(f"/s/{pub[0]['code']}").status_code,
                        len(A.db.list_links_of_short_task(tid))]
    print("RESULT" + json.dumps(out, ensure_ascii=False))
''')


@unittest.skipUnless(has_modules("flask"), "未安装 flask")
class LinkShortenerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], capture_output=True, text=True,
                              timeout=180, cwd=ROOT)
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")]
        if not line:
            raise AssertionError(proc.stdout[-3000:] + proc.stderr[-3000:])
        cls.out = json.loads(line[-1][6:])

    def test_validation(self):
        o = self.out
        self.assertEqual([o["bad_scheme"], o["bad_service"], o["no_name"], o["bad_clone"]], [400] * 4)

    def test_create_generate_list(self):
        o = self.out
        self.assertEqual(o["create"], 201)
        self.assertEqual(o["detail"][:3], ["partial", 4, 1])
        self.assertEqual(sorted(o["detail"][3]), ["clckru", "local", "tinyurl", "urlasia"])
        self.assertEqual(o["list"], [1, {"tasks": 1, "links": 4, "clicks": 0}])
        self.assertEqual([o["search_hit"], o["search_miss"]], [1, 0])
        self.assertTrue(o["local_url"])
        self.assertEqual(o["local_row"], [True, True])
        # 测试客户端的地址是 localhost: 公共服务连不回来, 直接缩目标并注明
        self.assertEqual(o["via_local"], [False])
        self.assertEqual(o["sent_target"], ["https://a.example/landing"])

    def test_export(self):
        o = self.out
        self.assertEqual(o["txt"][1:], ["https://tinyurl.com/t1", "https://clck.ru/c1", "https://url.asia/u1"])
        self.assertTrue(o["txt"][0].startswith("http://localhost/s/"))
        self.assertEqual(o["csv"], [200, "text/csv", 6, True])
        self.assertEqual(o["bad_fmt"], 400)

    def test_redirect_counts_clicks(self):
        o = self.out
        self.assertEqual(o["redirect"], [302, "https://a.example/landing"])
        self.assertEqual(o["clicks"], 2)
        self.assertEqual(o["numeric_new"], 404)
        self.assertEqual(o["failed_redirect"], 404)
        self.assertEqual(o["missing_redirect"], [404, 404, 404])
        self.assertEqual(o["kpis_after"]["clicks"], 2)

    def test_regenerate_retries_only_failed(self):
        o = self.out
        self.assertEqual(o["regen"], 202)
        self.assertEqual(o["regen_calls"], ["https://lnk.ua/api/v1/link/create"])
        self.assertEqual(o["regen_detail"], ["done", 5, 0])

    def test_standalone_and_ip(self):
        o = self.out
        self.assertEqual(o["ip_single"], [201, "tinyurl", False])
        self.assertEqual(o["standalone"], 1)
        self.assertEqual(o["all_links"], 6)
        self.assertEqual(o["all_fail"], [502, False, 4])

    def test_default_is_local_without_network(self):
        o = self.out
        self.assertEqual(o["default_local"], [201, "local", True, [], True])

    def test_public_link_points_to_local_code(self):
        status, sent, expect, via_local, local_url = self.out["public_base"]
        self.assertEqual(status, 201)
        self.assertEqual(sent, [expect])
        self.assertTrue(via_local)
        self.assertEqual(local_url, expect)

    def test_legacy_numeric_still_works(self):
        self.assertEqual(self.out["legacy_numeric"], 302)

    def test_providers(self):
        self.assertEqual(self.out["providers"], [["tinyurl", False, True], ["clckru", False, True],
                                                 ["lnkua", False, True], ["urlasia", False, False]])

    def test_delete(self):
        o = self.out
        self.assertEqual([o["del_link"], o["del_task"]], [200, 200])
        self.assertEqual(o["after_del"], [404, 404, 0])


if __name__ == "__main__":
    unittest.main()
