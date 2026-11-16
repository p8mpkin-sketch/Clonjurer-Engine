"""平台品牌图标生成: 方向 A「叠影 C」(两道错位 C 形弧, 淡的是原件、实的是克隆件)。

产物写到 static_vendor/brand/:
  favicon.svg                矢量 favicon(赭橙底 + 白色字形, 深浅标签栏都清楚)
  favicon.ico                16/32/48 三档合一
  favicon-16.png / -32.png   老浏览器兜底
  apple-touch-icon.png       180, iOS 主屏
  icon-192.png / -512.png    site.webmanifest 用
  site.webmanifest
运行: python3 tools/brand/build_brand.py   (PNG 用 Playwright 自带 Chromium 栅格化, ICO 用 Pillow 合成)
侧栏/登录卡片里的同款字形直接内联在 templates/index.html, 改字形时两处一起改。
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "static_vendor" / "brand"
BRAND = "#c4532f"  # = --ui-brand, 白字对比 4.6:1

# 64 网格; 字形只用 currentColor, 侧栏/登录卡片内联时随主题取色
GLYPH = ('<path d="M39.1 17.9A15 15 0 1 0 39.1 39.1" fill="none" stroke="currentColor" stroke-width="6" '
         'stroke-linecap="round" opacity=".42"/>'
         '<path d="M46.1 24.9A15 15 0 1 0 46.1 46.1" fill="none" stroke="currentColor" stroke-width="6" '
         'stroke-linecap="round"/>')


def tile_svg(rx=14):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            f'<rect width="64" height="64" rx="{rx}" fill="{BRAND}"/>'
            f'<g color="#fff">{GLYPH}</g></svg>')


def rasterize(jobs):
    """jobs: [(svg, px, path)] 用 Chromium 截图出透明底 PNG。"""
    js = """
const {chromium}=require('playwright');(async()=>{const jobs=JSON.parse(process.argv[1]);
const b=await chromium.launch();for(const [svg,px,out] of jobs){const p=await b.newPage({viewport:{width:px,height:px}});
await p.setContent('<style>html,body{margin:0;background:transparent}svg{display:block;width:'+px+'px;height:'+px+'px}</style>'+svg);
await p.screenshot({path:out,omitBackground:true});await p.close();}await b.close();})();"""
    subprocess.run(["node", "-e", js, json.dumps(jobs)], check=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "favicon.svg").write_text(tile_svg() + "\n", encoding="utf-8")
    # iOS 主屏图标自带圆角裁切, 给满版方底; 其余保留圆角
    full = tile_svg(rx=0)
    jobs = [(tile_svg(), 16, OUT / "favicon-16.png"), (tile_svg(), 32, OUT / "favicon-32.png"),
            (tile_svg(), 48, OUT / "_48.png"), (full, 180, OUT / "apple-touch-icon.png"),
            (tile_svg(), 192, OUT / "icon-192.png"), (tile_svg(), 512, OUT / "icon-512.png")]
    rasterize([(s, px, str(p)) for s, px, p in jobs])
    from PIL import Image
    imgs = [Image.open(OUT / n).convert("RGBA") for n in ("favicon-16.png", "favicon-32.png", "_48.png")]
    imgs[2].save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)], append_images=imgs[:2])
    (OUT / "_48.png").unlink()
    manifest = {"name": "Clonjurer Engine", "short_name": "Clonjurer",
                "icons": [{"src": "/vendor/brand/icon-192.png", "sizes": "192x192", "type": "image/png"},
                          {"src": "/vendor/brand/icon-512.png", "sizes": "512x512", "type": "image/png"}],
                "theme_color": "#121110", "background_color": "#121110", "display": "standalone"}
    (OUT / "site.webmanifest").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
