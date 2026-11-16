"""品牌图标与登录页底纹护栏: favicon 全套文件齐备且被页面引用; 登录底纹守 UI 规范(无渐变/滤镜, 不挡交互, 读屏忽略)。"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "static_vendor" / "brand"
HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "app.py").read_text(encoding="utf-8")


class BrandAssetTests(unittest.TestCase):
    def test_files_present(self):
        for n in ("favicon.svg", "favicon.ico", "favicon-16.png", "favicon-32.png", "apple-touch-icon.png",
                  "icon-192.png", "icon-512.png", "site.webmanifest"):
            self.assertTrue((BRAND / n).is_file(), n)
        self.assertEqual((BRAND / "favicon.ico").read_bytes()[:4], b"\x00\x00\x01\x00")
        for n, px in (("favicon-16.png", 16), ("favicon-32.png", 32), ("apple-touch-icon.png", 180),
                      ("icon-192.png", 192), ("icon-512.png", 512)):
            head = (BRAND / n).read_bytes()[:24]
            self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n", n)
            self.assertEqual((int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")), (px, px), n)
        manifest = json.loads((BRAND / "site.webmanifest").read_text(encoding="utf-8"))
        for icon in manifest["icons"]:
            self.assertTrue((ROOT / "static_vendor" / icon["src"].removeprefix("/vendor/")).is_file())

    def test_page_references(self):
        for ref in ('href="/favicon.ico"', 'href="/vendor/brand/favicon.svg"',
                    'href="/vendor/brand/apple-touch-icon.png"', 'href="/vendor/brand/site.webmanifest"'):
            self.assertIn(ref, HTML)
        self.assertNotIn('rel="icon" type="image/svg+xml" href="data:', HTML)
        self.assertIn('@app.get("/favicon.ico")', APP)

    def test_login_art_follows_ui_rules(self):
        m = re.search(r'<svg class="login-art".*?</svg>', HTML, re.S)
        self.assertIsNotNone(m)
        art = m.group(0)
        self.assertIn('aria-hidden="true"', art)
        for banned in ("Gradient", "<filter", "blur"):
            self.assertNotIn(banned, art)
        self.assertRegex(HTML, r"\.login-art\{[^}]*pointer-events:none")
        # 卡片仍是实底, 底纹只在卡片之外
        self.assertRegex(HTML, r"\.login-card\{[^}]*\n?\s*background:var\(--surface\)")


if __name__ == "__main__":
    unittest.main()
