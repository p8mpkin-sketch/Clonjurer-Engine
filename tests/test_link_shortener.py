"""短链服务层: 四家服务的响应解析、失败顺延、IP 目标排除 url.asia、目标校验、落库留痕。

所有外呼都 mock 在 requests.request 上(测试不打真实网络); 落库用临时 SQLite 的真 Database。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from core import link_shortener as ls
from models import Database


class FakeResp:
    def __init__(self, status=200, text="", reason="OK"):
        self.status_code, self.text, self.reason = status, text, reason


def router(table):
    """按请求地址的主机名派发假响应; 值可以是 FakeResp 或异常实例。记下每次调用。"""
    calls = []

    def fake(method, url, **kw):
        calls.append((method, url, kw))
        for host, resp in table.items():
            if host in url:
                if isinstance(resp, BaseException):
                    raise resp
                return resp
        raise AssertionError(f"未 mock 的外呼: {method} {url}")
    fake.calls = calls
    return fake


OK_ALL = {
    "tinyurl.com": FakeResp(text="https://tinyurl.com/abc123\n"),
    "clck.ru": FakeResp(text="https://clck.ru/3Fxyz"),
    "lnk.ua": FakeResp(text='{"result": {"lnk": "https://lnk.ua/bMen0KNgd", "key": "bMen0KNgd",'
                            ' "qr": "https://lnk.ua/qr/bMen0KNgd"}}'),
    "url.asia": FakeResp(text='{"short_url": "https://url.asia/Ab3"}'),
}


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def patch(self, table):
        fake = router(table)
        p = mock.patch.object(ls.requests, "request", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        return fake


class ValidateTargetTests(unittest.TestCase):
    def test_accepts_http_and_https(self):
        self.assertEqual(ls.validate_target("  https://a.example/x?y=1 "), "https://a.example/x?y=1")
        self.assertEqual(ls.validate_target("HTTP://10.0.0.5:8000/"), "HTTP://10.0.0.5:8000/")

    def test_rejects_non_http(self):
        for bad in ("ftp://a.example/", "javascript:alert(1)", "file:///etc/passwd", "data:text/html,x",
                    "//a.example/x", "a.example/x", "", "   ", None, 123, "https://", "http:///path",
                    "https://a.example/ x", "https://a.example:99999/", "mailto:a@b.c"):
            with self.subTest(bad=bad), self.assertRaises(ls.InvalidTarget):
                ls.validate_target(bad)

    def test_rejects_too_long(self):
        with self.assertRaises(ls.InvalidTarget):
            ls.validate_target("https://a.example/" + "x" * ls.MAX_URL_LEN)

    def test_shorten_rejects_before_any_request(self):
        with mock.patch.object(ls.requests, "request") as req, self.assertRaises(ls.InvalidTarget):
            ls.shorten(mock.Mock(), "ftp://a.example/")
        req.assert_not_called()


class ParseTests(unittest.TestCase):
    def test_providers_plain_and_json(self):
        with mock.patch.object(ls.requests, "request", side_effect=router(OK_ALL)):
            self.assertEqual(ls.gen_tinyurl("https://a.example/"), ("https://tinyurl.com/abc123", {}))
            self.assertEqual(ls.gen_clckru("https://a.example/"), ("https://clck.ru/3Fxyz", {}))
            short, extras = ls.gen_lnkua("https://a.example/")
            self.assertEqual(short, "https://lnk.ua/bMen0KNgd")
            self.assertEqual(extras, {"key": "bMen0KNgd", "qr": "https://lnk.ua/qr/bMen0KNgd"})
            self.assertEqual(ls.gen_urlasia("https://a.example/")[0], "https://url.asia/Ab3")

    def test_request_shape(self):
        fake = router(OK_ALL)
        with mock.patch.object(ls.requests, "request", side_effect=fake):
            ls.gen_tinyurl("https://a.example/?q=1&r=2")
            ls.gen_lnkua("https://a.example/")
        (m1, u1, kw1), (m2, u2, kw2) = fake.calls
        self.assertEqual((m1, u1, kw1["params"]), ("GET", "https://tinyurl.com/api-create.php",
                                                    {"url": "https://a.example/?q=1&r=2"}))
        self.assertEqual((m2, u2, kw2["data"]), ("POST", "https://lnk.ua/api/v1/link/create",
                                                  {"link": "https://a.example/"}))
        self.assertEqual(kw2["headers"]["Authorization"], "Bearer public")
        self.assertTrue(all(kw["timeout"] == 15 for _, _, kw in fake.calls))

    def test_html_or_foreign_host_is_not_a_short_link(self):
        cases = {
            "html page": FakeResp(text="<html><script>alert(1)</script></html>"),
            "error text": FakeResp(text="Error"),
            "foreign host": FakeResp(text="https://evil.example/x"),
            "look-alike host": FakeResp(text="https://tinyurl.com.evil.example/x"),
            "quote inject": FakeResp(text='https://tinyurl.com/a"onmouseover=alert(1)'),
            "js scheme": FakeResp(text="javascript://tinyurl.com/%0aalert(1)"),
            "http 400": FakeResp(status=400, text="Invalid URL", reason="Bad Request"),
            "http 500": FakeResp(status=503, text=""),
        }
        for name, resp in cases.items():
            with self.subTest(name), mock.patch.object(ls.requests, "request", return_value=resp), \
                    self.assertRaises(ls.ShortenError):
                ls.gen_tinyurl("https://a.example/")

    def test_json_error_and_missing_field(self):
        for text in ('{"error": "link is invalid"}', '{"result": {}}', '[1, 2]'):
            with self.subTest(text), mock.patch.object(ls.requests, "request", return_value=FakeResp(text=text)), \
                    self.assertRaises(ls.ShortenError):
                ls.gen_lnkua("https://a.example/")
        with mock.patch.object(ls.requests, "request",
                               return_value=FakeResp(422, '{"message": "bad link"}', "Unprocessable")):
            with self.assertRaisesRegex(ls.ShortenError, "422.*bad link"):
                ls.gen_lnkua("https://a.example/")

    def test_network_errors_become_shorten_error(self):
        for exc in (requests.Timeout(), requests.ConnectionError()):
            with self.subTest(type(exc).__name__), mock.patch.object(ls.requests, "request", side_effect=exc), \
                    self.assertRaises(ls.ShortenError):
                ls.gen_clckru("https://a.example/")


class IpTargetTests(DbCase):
    def test_ip_excludes_urlasia(self):
        self.assertTrue(ls.host_is_ip("http://47.92.75.70/cas/"))
        self.assertTrue(ls.host_is_ip("http://[::1]:8000/"))
        self.assertFalse(ls.host_is_ip("https://a.example/"))
        self.assertNotIn("urlasia", ls.usable_services("http://47.92.75.70/cas/"))
        self.assertIn("urlasia", ls.usable_services("https://a.example/"))

    def test_multi_never_calls_urlasia_for_ip(self):
        fake = self.patch(OK_ALL)
        tid = self.db.create_short_task("ip", "http://47.92.75.70/cas/", "all", None)
        res = ls.shorten_multi(self.db, "http://47.92.75.70/cas/", link_task_id=tid)
        self.assertFalse(any("url.asia" in u for _, u, _ in fake.calls))
        self.assertEqual(res["created"], 3)
        skipped = [r for r in res["results"] if r.get("skipped")]
        self.assertEqual([r["service"] for r in skipped], ["urlasia"])
        self.assertEqual(len(self.db.list_links_of_short_task(tid)), 3)

    def test_prefer_urlasia_with_ip_falls_through(self):
        fake = self.patch(OK_ALL)
        res = ls.shorten(self.db, "http://10.1.2.3/", "urlasia")
        self.assertTrue(res["ok"])
        self.assertEqual(res["service"], "tinyurl")
        self.assertFalse(any("url.asia" in u for _, u, _ in fake.calls))
        self.assertTrue(res["attempts"][0]["skipped"])
        link = self.db.get_short_link(res["link_id"])
        self.assertEqual(link["extras"]["requested"], "urlasia")
        self.assertTrue(link["extras"]["fallback"])

    def test_gen_urlasia_refuses_ip_directly(self):
        with mock.patch.object(ls.requests, "request") as req, self.assertRaises(ls.ShortenError):
            ls.gen_urlasia("http://10.1.2.3/")
        req.assert_not_called()


class FallbackTests(DbCase):
    def test_single_service_falls_through_and_records(self):
        self.patch({**OK_ALL, "clck.ru": FakeResp(500, "oops"), "lnk.ua": requests.Timeout()})
        res = ls.shorten(self.db, "https://a.example/", "clckru", label="L")
        self.assertTrue(res["ok"])
        self.assertEqual(res["service"], "urlasia")  # clckru -> lnkua -> urlasia(按顺序顺延)
        self.assertEqual([(a["service"], a["ok"]) for a in res["attempts"]],
                         [("clckru", False), ("lnkua", False), ("urlasia", True)])
        link = self.db.get_short_link(res["link_id"])
        self.assertEqual((link["service"], link["short_url"], link["label"]),
                         ("urlasia", "https://url.asia/Ab3", "L"))
        self.assertEqual(link["extras"]["requested"], "clckru")
        self.assertTrue(link["extras"]["fallback"])
        self.assertIn("超时", link["extras"]["attempts"][1]["error"])

    def test_wraps_around_order(self):
        self.patch({**OK_ALL, "url.asia": FakeResp(text="nope")})
        res = ls.shorten(self.db, "https://a.example/", "urlasia")
        self.assertEqual(res["service"], "tinyurl")

    def test_preferred_success_is_not_fallback(self):
        self.patch(OK_ALL)
        res = ls.shorten(self.db, "https://a.example/", "lnkua")
        self.assertEqual((res["service"], res["fallback"]), ("lnkua", False))
        self.assertEqual(self.db.get_short_link(res["link_id"])["extras"]["key"], "bMen0KNgd")

    def test_all_fail_records_failure_row(self):
        self.patch({k: FakeResp(502, "bad gateway") for k in OK_ALL})
        res = ls.shorten(self.db, "https://a.example/", "tinyurl")
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["attempts"]), 4)
        link = self.db.get_short_link(res["link_id"])
        self.assertEqual(link["short_url"], "")
        self.assertFalse(link["extras"]["ok"])
        self.assertEqual(self.db.link_kpis()["links"], 0)  # 失败行不算短链

    def test_unexpected_exception_in_one_provider_is_contained(self):
        self.patch(OK_ALL)
        with mock.patch.dict(ls.PROVIDERS, tinyurl=ls.Provider("tinyurl", "TinyURL", "https://tinyurl.com/", True,
                                                               mock.Mock(side_effect=KeyError("x")))):
            res = ls.shorten(self.db, "https://a.example/", "tinyurl")
        self.assertEqual(res["service"], "clckru")
        self.assertIn("内部错误", res["attempts"][0]["error"])

    def test_unknown_service(self):
        with self.assertRaises(ValueError):
            ls.normalize_service("bitly")
        self.assertEqual(ls.normalize_service("clck.ru"), "clckru")
        self.assertEqual(ls.normalize_service(""), "all")


class MultiAndTaskTests(DbCase):
    def test_partial_failure_recorded_per_provider(self):
        self.patch({**OK_ALL, "lnk.ua": FakeResp(text="<html>captcha</html>")})
        tid = self.db.create_short_task("t", "https://a.example/", "all", None)
        res = ls.generate_for_task(self.db, tid)
        self.assertEqual((res["created"], res["failed"]), (3, 1))
        rows = {l["service"]: l for l in self.db.list_links_of_short_task(tid)}
        self.assertEqual(set(rows), {"tinyurl", "clckru", "lnkua", "urlasia"})
        self.assertEqual(rows["lnkua"]["short_url"], "")
        self.assertIn("不是合法短链", rows["lnkua"]["extras"]["error"])
        self.assertEqual(self.db.get_short_task(tid)["status"], "partial")
        t = self.db.list_short_tasks()[0]
        self.assertEqual((t["link_count"], t["failed_count"]), (3, 1))
        self.assertEqual(set(t["services"].split(",")), {"tinyurl", "clckru", "urlasia"})

    def test_regenerate_only_retries_failed_services(self):
        fake = self.patch({**OK_ALL, "lnk.ua": FakeResp(500, "x")})
        tid = self.db.create_short_task("t", "https://a.example/", "all", None)
        ls.generate_for_task(self.db, tid)
        fake.calls.clear()
        with mock.patch.object(ls.requests, "request", side_effect=router(OK_ALL)) as req:
            res = ls.generate_for_task(self.db, tid)
        self.assertEqual([c.args[1] for c in req.call_args_list], ["https://lnk.ua/api/v1/link/create"])
        self.assertEqual((res["created"], res["failed"]), (1, 0))
        links = self.db.list_links_of_short_task(tid)
        self.assertEqual(len(links), 4)
        self.assertTrue(all(l["short_url"] for l in links))  # 上一轮失败行已清掉
        self.assertEqual(self.db.get_short_task(tid)["status"], "done")

    def test_all_fail_task_status(self):
        self.patch({k: requests.ConnectionError() for k in OK_ALL})
        tid = self.db.create_short_task("t", "https://a.example/", "all", None)
        res = ls.generate_for_task(self.db, tid)
        self.assertEqual((res["created"], res["failed"]), (0, 4))
        self.assertEqual(self.db.get_short_task(tid)["status"], "failed")
        self.assertEqual(self.db.link_kpis(), {"tasks": 1, "links": 0, "clicks": 0})

    def test_single_service_task(self):
        self.patch({**OK_ALL, "tinyurl.com": FakeResp(text="Error")})
        tid = self.db.create_short_task("t", "https://a.example/", "tinyurl", 7)
        ls.generate_for_task(self.db, tid)
        (link,) = self.db.list_links_of_short_task(tid)
        self.assertEqual((link["service"], link["task_id"], link["label"]), ("clckru", 7, "t"))
        self.assertEqual(self.db.get_short_task(tid)["status"], "done")

    def test_task_missing(self):
        with self.assertRaises(LookupError):
            ls.generate_for_task(self.db, 999)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        ls._preflight_cache.update(at=0.0, items=None)

    def test_preflight_and_cache(self):
        table = {"tinyurl.com": FakeResp(200), "clck.ru": FakeResp(301), "lnk.ua": FakeResp(503),
                 "url.asia": requests.ConnectionError()}
        with mock.patch.object(ls.requests, "request", side_effect=router(table)) as req:
            items = {p["key"]: p for p in ls.preflight()}
            self.assertEqual({k: v["available"] for k, v in items.items()},
                             {"tinyurl": True, "clckru": True, "lnkua": False, "urlasia": False})
            self.assertFalse(items["urlasia"]["supports_ip"])
            self.assertEqual(req.call_count, 4)
            self.assertTrue(all(c.kwargs["timeout"] == 15 for c in req.call_args_list))
            ls.preflight()
            self.assertEqual(req.call_count, 4)  # 缓存内不再外呼
            ls.preflight(force=True)
            self.assertEqual(req.call_count, 8)


if __name__ == "__main__":
    unittest.main()
