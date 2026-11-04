"""禁 per-site 特判守卫(ROADMAP_95.md 约束 1)。

引擎代码(core/)的**可执行字符串字面量**里不得出现跑分语料(Web 真实语料 + L1 生成用例)的站点域名:
以域名分支 = 为单站调参, 分数不可信。注释、文档字符串、内嵌 JS 的 // 与 /* */ 注释里引用实证站点是允许的
(那是归因记录, 不是分支条件)。example.com 是 RFC 2606 保留域名, 不算语料。
"""
import io
import json
import re
import tokenize
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOW = {"example.com"}


def corpus_hosts() -> set:
    out = set()
    blobs = [(ROOT / "engine_bench/real_sites/corpus.json").read_text(encoding="utf-8")]
    gen = ROOT / "engine_bench/gen_cases/cases.json"
    if gen.exists():
        blobs.append(gen.read_text(encoding="utf-8"))
        for grp in json.loads(blobs[-1]).values():
            if isinstance(grp, list):
                for c in grp:
                    note = str(c.get("note", "")) if isinstance(c, dict) else ""
                    if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", note):
                        out.add(note)
    for b in blobs:
        for u in re.findall(r"https?://[^\"'\s]+", b):
            h = urllib.parse.urlsplit(u).hostname or ""
            out.add(h[4:] if h.startswith("www.") else h)
    return {h for h in out if h and h not in ALLOW}


_JS_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


def code_strings(path: Path):
    """生成 (行号, 字符串字面量); 跳过文档字符串(独立表达式语句的字符串)。"""
    src = path.read_text(encoding="utf-8", errors="ignore")
    prev = tokenize.NEWLINE
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.NL):
            continue
        if tok.type == tokenize.STRING and prev in (tokenize.NEWLINE, tokenize.INDENT,
                                                     tokenize.DEDENT, tokenize.ENCODING):
            prev = tok.type  # 文档字符串
            continue
        if tok.type == tokenize.STRING or tok.type == getattr(tokenize, "FSTRING_MIDDLE", -1):
            yield tok.start[0], _JS_COMMENT.sub("", tok.string)
        prev = tok.type


class NoSiteSpecialCase(unittest.TestCase):
    def test_core_has_no_corpus_domain_literals(self):
        hosts = corpus_hosts()
        self.assertGreater(len(hosts), 20)
        hits = []
        for p in sorted((ROOT / "core").rglob("*.py")):
            for line, s in code_strings(p):
                low = s.lower()
                for h in hosts:
                    if re.search(r"(?<![a-z0-9-])" + re.escape(h) + r"(?![a-z0-9-])", low):
                        hits.append(f"{p.relative_to(ROOT)}:{line} {h}")
        self.assertEqual(hits, [], "引擎代码出现语料站点域名字面量(疑似单站特判):\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
