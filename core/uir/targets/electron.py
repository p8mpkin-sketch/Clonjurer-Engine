"""UIR → Electron 桌面工程(L3 输出目标最小版, DESIGN.md §9.8)。

复用 html_css 目标: UIR → index.html + app.css 原样装进 Electron 壳, 因此桌面产物与 Web 产物
渲染同一份 HTML(同为 Chromium), 可以被 Playwright 截图参与像素评分(shootable=True, 入口即 app/index.html)。

产物: out_dir/electron_app/{package.json, main.js, app/index.html, app/static/css/app.css, README.md}。
- 窗口尺寸取 UIR 视口(无则 1280×800); 安全默认: contextIsolation、sandbox、无 nodeIntegration;
- 只加载包内文件, 外部链接交给系统浏览器打开, 不在应用内导航到外站;
- 只含界面外观与交互意图(html_css 的最小内联行为), 不含业务逻辑。
构建(打包成 .app / .exe)是显式步骤, 见 core/uir/toolchain.py, 不在 render 里联网或调用工具链。
"""
import json
import re
from pathlib import Path

from core.uir.model import UIR
from core.uir.registry import register_output, RenderResult, BuildContext
from core.uir.targets.html_css import HtmlCssTarget

ELECTRON_VERSION = "45.2.2"
PACKAGER_VERSION = "20.3.0"

_MAIN_JS = """// 由 Clonjurer 通用 UI 重建引擎从 UIR v1 生成(OutputTarget: electron)。只含界面外观与交互意图。
const { app, BrowserWindow, shell } = require('electron');
const path = require('path');

const WIN = %(win)s;

function createWindow() {
  const win = new BrowserWindow({
    width: WIN.width,
    height: WIN.height,
    title: WIN.title,
    backgroundColor: WIN.bg,
    webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false },
  });
  win.setMenuBarVisibility(false);
  // 只渲染包内页面; 外链交给系统浏览器
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (e, url) => {
    if (!url.startsWith('file:')) {
      e.preventDefault();
      if (/^https?:/i.test(url)) shell.openExternal(url);
    }
  });
  win.loadFile(path.join(__dirname, 'app', 'index.html'));
}

app.whenReady().then(() => {
  createWindow();
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
"""

_README = """# 生成的 Electron 桌面工程(最小版)

由 UIR v1 经 html_css 目标渲染后装进 Electron 壳, 只含界面外观与交互意图。

```
npm install                       # 拉取 electron / @electron/packager(本仓库不含)
npm start                         # 本机直接运行
npx @electron/packager . --platform=darwin --arch=arm64 --out=dist   # macOS .app
npx @electron/packager . --platform=win32  --arch=x64   --out=dist   # Windows .exe(macOS 上可交叉打包)
```

或在引擎侧调用 `python -m core.uir.toolchain desktop <本目录>`。
未做: 代码签名/公证(macOS notarization、Windows Authenticode)、安装器(dmg/msi/nsis)、自动更新。
"""


def _pkg_name(title: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", (title or "").lower()).strip("-")
    return s[:40] or "generated-ui"


@register_output("electron")
class ElectronTarget:
    shootable = True

    def render(self, uir: UIR, out_dir: Path, ctx: BuildContext = None) -> RenderResult:
        out = Path(out_dir) / "electron_app"
        res = HtmlCssTarget().render(uir, out / "app", ctx)
        title = str(uir.meta.get("title") or "Generated App")
        vp = uir.meta.get("viewport") if isinstance(uir.meta.get("viewport"), dict) else {}
        w = int(vp.get("w") or vp.get("width") or 1280)
        h = int(vp.get("h") or vp.get("height") or 800)
        bg = (uir.root.box or {}).get("bg")
        bg = uir.tokens.resolve("color", bg) if bg else None
        if not (isinstance(bg, str) and re.match(r"^#[0-9a-fA-F]{6}$", bg)):
            bg = "#ffffff"
        win = {"width": max(480, min(w, 1920)), "height": max(360, min(h, 1200)), "title": title, "bg": bg}
        (out / "main.js").write_text(_MAIN_JS % {"win": json.dumps(win, ensure_ascii=False)}, encoding="utf-8")
        pkg = {
            "name": _pkg_name(title),
            "productName": re.sub(r"[\\/:*?\"<>|]", "", title)[:60] or "Generated App",
            "version": "0.1.0",
            "description": "Generated from UIR v1 by Clonjurer universal UI engine.",
            "main": "main.js",
            "private": True,
            "scripts": {"start": "electron ."},
            "devDependencies": {"electron": ELECTRON_VERSION, "@electron/packager": PACKAGER_VERSION},
        }
        (out / "package.json").write_text(json.dumps(pkg, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "README.md").write_text(_README, encoding="utf-8")
        return RenderResult(entry_file=out / "app" / "index.html", shootable=True,
                            assets=["electron_app/main.js", "electron_app/package.json"]
                            + [f"electron_app/app/{a}" for a in res.assets],
                            meta={**res.meta, "window": win})
