"""平台前端预编译样式守卫: templates/index.html 用到的 Tailwind 工具类都要在 static_vendor/css/app.css 里。

原先用 451KB 的运行时 tailwind.js 在浏览器里现扫 DOM 生成样式; 改为 tools/css/build.sh 预编译后,
改模板忘了重编会让新 class 静默无样式。另守 UI 规范: 正文不得再出现 12px 以下的任意字号以外的新写法。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static_vendor" / "css" / "app.css").read_text(encoding="utf-8")
TOKENS_CSS = (ROOT / "static_vendor" / "css" / "ui-tokens.css").read_text(encoding="utf-8")

PREFIX = re.compile(
    r"(?:[a-z-]+:)*-?(?:p[xytblr]?|m[xytblr]?|w|h|min-w|max-w|min-h|max-h|text|bg|border(?:-[tblrxy])?|"
    r"rounded(?:-[a-z]+)?|gap(?:-[xy])?|grid-cols|col-span|flex|items|justify|self|font|leading|tracking|"
    r"shadow|ring(?:-offset)?|from|to|via|opacity|z|top|left|right|bottom|inset(?:-[xy])?|space-[xy]|"
    r"translate-[xy]|scale|rotate|blur|backdrop-blur|overflow(?:-[xy])?|whitespace|cursor|accent|outline|"
    r"divide(?:-[xy])?|object|aspect|line-clamp|duration|ease|delay|animate|order|basis|grow|shrink|"
    r"select|pointer-events|transition)(?:-|$)")
# 模板里历史遗留的无效写法(运行时 Tailwind 同样不生成, 对样式无影响)
KNOWN_INVALID = {"bg-[#0A1120]/92", "bg-[#0B1020]/97", "bg-slate-960/60", "h-4.5", "w-4.5",
                 "bg-[rgba(15,23,42,0.55)]"}


def _escape(tok):
    tok = re.sub(r"([\[\].\/:#%()!&>+~*'\"=@])", r"\\\1", tok)
    return tok.replace(",", "\\2c ")


def _template_tokens():
    toks = set()
    for m in re.finditer(r'\bclass="([^"]*)"', HTML):
        toks.update(m.group(1).split())
    for m in re.finditer(r':class="([^"]*)"', HTML):
        for s in re.findall(r"'([^']*)'", m.group(1)):
            toks.update(s.split())
    style = HTML[HTML.index("<style>"):HTML.index("</style>")] + TOKENS_CSS
    custom = set(re.findall(r"\.([A-Za-z][\w-]*)", style))
    out = set()
    for t in toks:
        t = t.strip("',)")
        if t and t not in custom and "[[" not in t and "=" not in t and PREFIX.match(t.lstrip("!")):
            out.add(t)
    return out


class PrecompiledCssTests(unittest.TestCase):
    def test_runtime_tailwind_removed(self):
        self.assertNotIn("/vendor/js/tailwind.js", HTML)
        self.assertIn("/vendor/css/app.css", HTML)

    def test_every_utility_class_is_compiled(self):
        missing = sorted(t for t in _template_tokens() - KNOWN_INVALID if "." + _escape(t) not in CSS)
        self.assertEqual(missing, [], "模板新增的 class 未编进 app.css, 请运行 tools/css/build.sh")

    def test_precompiled_css_after_inline_style(self):
        # 运行时 Tailwind 的样式注入在 head 末尾: 预编译样式也要在内联 <style> 之后, 层叠顺序才不变
        self.assertGreater(HTML.index("/vendor/css/app.css"), HTML.index("</style>"))


if __name__ == "__main__":
    unittest.main()


class IconSubsetTests(unittest.TestCase):
    """图标改为子集(tools/icons/build_lucide_subset.js): 模板里出现的图标名都要在子集里, 否则图标静默空白。"""

    def test_template_icons_in_subset(self):
        sub = (ROOT / "static_vendor" / "js" / "lucide.subset.js").read_text(encoding="utf-8")
        have = set(re.findall(r'"([A-Z][A-Za-z0-9]*)":\[\[', sub))

        def pascal(n):
            return re.sub(r"(\w)(\w*)(_|-|\s*)", lambda m: m.group(1).upper() + m.group(2).lower(), n)
        names = set(re.findall(r'data-lucide="([a-z0-9-]+)"', HTML))
        names |= set(re.findall(r"\bicon:\s*'([a-z0-9-]+)'", HTML))
        missing = sorted(n for n in names if pascal(n) not in have)
        self.assertEqual(missing, [], "模板用到的图标不在子集里, 请运行 node tools/icons/build_lucide_subset.js")
        self.assertIn("/vendor/js/lucide.subset.js", HTML)
