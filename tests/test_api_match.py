"""接口录制的请求指纹与回放挑选(core/clone/api_match.py): 同一接口靠请求体/参数区分内容时挑对那一份。"""
import json
import re
import unittest

from tests import _env  # noqa: F401
from core import flask_generator
from core.clone.api_match import pick_entry, request_pairs


def _post(key, rid):
    return json.dumps({"requestId": rid, "pv": 1731340000000 + len(rid), "resourceList": [{"tplCompKey": key, "size": 20}]})


class ApiMatch(unittest.TestCase):
    def setUp(self):
        self.entries = [{"id": k, "req_sig": request_pairs("", _post(k, "r" + k))} for k in ("news", "sports", "local")]

    def test_picks_by_body_key_ignoring_volatile_fields(self):
        for k in ("news", "sports", "local"):
            self.assertEqual(pick_entry(self.entries, "", _post(k, "another-request-id"))["id"], k)

    def test_unknown_key_falls_back_to_first(self):
        # 全都只对上 size 这一对: 平分取录制顺序靠前的(与原先"先到先得"一致)
        self.assertEqual(pick_entry(self.entries, "", _post("weather", "x"))["id"], "news")

    def test_legacy_entries_without_sig_keep_first_wins(self):
        legacy = [{"id": 1}, {"id": 2}]
        self.assertEqual(pick_entry(legacy, "a=1", "{}")["id"], 1)
        self.assertIsNone(pick_entry([], "", ""))

    def test_query_variants_and_cache_buster(self):
        es = [{"id": t, "req_sig": request_pairs(f"tab={t}&page=1&_=17313400{i}")} for i, t in enumerate(("hot", "new", "top"))]
        self.assertEqual(pick_entry(es, "tab=top&page=1&_=999")["id"], "top")

    def test_form_and_raw_bodies(self):
        self.assertIn("b:key=a", request_pairs("", "key=a&ts=1"))
        self.assertEqual(request_pairs("", b"plain text"), ["b:raw=plain text"])
        self.assertEqual(request_pairs("", "x" * 40000), [])        # 超长请求体不解析

    def test_list_order_does_not_matter(self):
        a = request_pairs("", json.dumps({"ids": [{"k": "a"}, {"k": "b"}]}))
        b = request_pairs("", json.dumps({"ids": [{"k": "b"}, {"k": "a"}]}))
        self.assertEqual(set(a), set(b))

    def test_generated_app_embeds_matcher(self):
        src = flask_generator.GENERATED_APP_PY.replace("__API_MATCH_SRC__", flask_generator._api_match_src())
        self.assertIn("def pick_entry(", src)
        compile(re.sub(r"@@[A-Z_]+@@", "None", src), "generated_app", "exec")


if __name__ == "__main__":
    unittest.main()
