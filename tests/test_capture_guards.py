"""采集/落盘护栏(真实语料 2026-11-02 归因后的整类修复):

- 落盘前水合剥离: 策略包 disable_hydration 的模式, 应用脚本整体剥离, JSON 数据脚本
  与引擎注入物保留(solidjs.com / emberjs.com 整页重复渲染根因);
- 策略包开关面: SSR 水合型 UI 库(Solid/Ember/Svelte)纳入剥离集, 纯静态/jQuery 不剥离;
- DEEP_FREEZE_JS 文本契约: 样式表媒体条件随内联保留(wordpress.org 打印样式隐藏页眉页脚
  根因)、文档级 adoptedStyleSheets 序列化(remix.run 整页无样式根因)。
  浏览器级验证见 engine_bench/real_sites/README.md 的复现脚本说明。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import cloner_engine as ce  # noqa: E402
from core import strategy_packs  # noqa: E402


class HydrationStripHtml(unittest.TestCase):
    HTML = ('<html><head>'
            '<script data-clone-preview="time-freeze">x()</script>'
            '<script type="application/json" id="__DATA__">{"a":1}</script>'
            '<script type="module" src="/static/js/app.js"></script>'
            '<script>\nwindow.app = 1;\n</script>'
            '<script type="application/ld+json">{}</script>'
            '<SCRIPT SRC="/static/js/vendor.js" async></SCRIPT>'
            '</head><body><div data-hk="0-1">x</div>'
            '<script type="text/javascript">boot()</script></body></html>')

    def test_strips_app_scripts_keeps_json_and_injected(self):
        out, n = ce._strip_hydration_html(self.HTML)
        self.assertEqual(n, 4)
        self.assertIn('data-clone-preview="time-freeze"', out)
        self.assertIn('id="__DATA__"', out)
        self.assertIn('application/ld+json', out)
        self.assertNotIn('app.js', out)
        self.assertNotIn('vendor.js', out)
        self.assertNotIn('boot()', out)
        self.assertIn('<div data-hk="0-1">x</div>', out)

    def test_idempotent_and_noop_without_scripts(self):
        once, _ = ce._strip_hydration_html(self.HTML)
        twice, n = ce._strip_hydration_html(once)
        self.assertEqual((twice, n), (once, 0))
        plain = "<html><body><p>静态</p></body></html>"
        self.assertEqual(ce._strip_hydration_html(plain), (plain, 0))


class StrategyPackHydrationFlags(unittest.TestCase):
    def test_hydrating_frameworks_strip(self):
        for name in ("M-Next SSR", "M-Nuxt SSR", "M-Remix SSR", "M-SvelteKit SSR",
                     "M-Solid SPA", "M-Ember SPA", "M-Svelte SPA", "M-Astro 静态岛",
                     "M-Qwik 可恢复", "M-React SPA", "M-Wix CMS"):
            self.assertTrue(strategy_packs.for_pattern(name).disable_hydration, name)

    def test_static_and_cms_keep_scripts(self):
        for name in ("M-STATIC 纯静态", "M-jQuery传统MPA", "M-WordPress CMS",
                     "M-Shopify CMS", "M-Angular SPA", "M-Vue/Vite SPA", "DEFAULT"):
            self.assertFalse(strategy_packs.for_pattern(name).disable_hydration, name)


class DeepFreezeContract(unittest.TestCase):
    def test_media_and_adopted_sheets_serialized(self):
        js = ce.DEEP_FREEZE_JS
        self.assertIn("adoptedStyleSheets", js)
        self.assertIn("'@media ' + media", js)
        self.assertIn("if (sheet.disabled) return;", js)
        self.assertIn("'adopted'", js)


if __name__ == "__main__":
    unittest.main()
