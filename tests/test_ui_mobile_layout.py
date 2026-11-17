"""手机宽度下的列表布局守卫: 后加的断点按源码顺序会覆盖前面的手机端规则(成员列表曾因此在 390 宽下名字被挤没)。
用真实浏览器加载 ui-tokens.css, 量计算后的网格列数与名字列宽度。"""
import unittest
from pathlib import Path

from tests._env import chromium_available

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static_vendor" / "css" / "ui-tokens.css").read_text(encoding="utf-8")

IAM_ROW = """
<div class="mt-row iam-row">
  <span class="iam-ck"><input type="checkbox"></span>
  <button type="button" class="iam-who"><span class="av lg">测</span>
    <span class="min-w-0"><span class="iam-name">测试管理员</span><span class="iam-sub">@admin</span></span></button>
  <div><span class="iam-role-text">所有者</span></div>
  <div class="dp-state"><span class="dot"></span>正常</div>
  <div class="iam-c-login">刚刚</div>
  <div class="iam-c-act">3 次</div>
  <div class="iam-c-llm">¥0.00</div>
  <div class="iam-acts">…</div>
</div>"""


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class MobileIamRow(unittest.TestCase):
    def _measure(self, width):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            try:
                p = b.new_page(viewport={"width": width, "height": 800})
                p.set_content(f"<!doctype html><html><head><style>{CSS}</style></head><body>{IAM_ROW}</body></html>")
                return p.evaluate("""() => {
                    const row = document.querySelector('.iam-row');
                    const cols = getComputedStyle(row).gridTemplateColumns.split(' ').filter(Boolean).length;
                    const name = document.querySelector('.iam-name').getBoundingClientRect();
                    const role = document.querySelector('.iam-role-text').getBoundingClientRect();
                    return {cols, nameW: name.width, roleH: role.height};
                }""")
            finally:
                b.close()

    def test_phone_three_columns_name_visible(self):
        m = self._measure(390)
        self.assertEqual(m["cols"], 3)
        self.assertGreater(m["nameW"], 60)
        self.assertLess(m["roleH"], 30)   # 「所有者」不竖排

    def test_desktop_keeps_full_table(self):
        self.assertGreaterEqual(self._measure(1440)["cols"], 7)


if __name__ == "__main__":
    unittest.main()
