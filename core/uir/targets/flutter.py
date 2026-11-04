"""UIR → Flutter Widget 代码(L2 输出目标最小版, DESIGN.md §9.7)。

产物: out_dir/flutter_app/{pubspec.yaml, lib/main.dart, lib/theme.dart, README.md}。
- 令牌 → theme.dart(ColorScheme / TextStyle 常量);
- layout flex/grid/stack → Column/Row/GridView/Stack, box → Container(padding/margin/decoration);
- role: text→Text, image→占位 Container(或 Image.asset 若 content.drawable), button→ElevatedButton,
  input→TextField, divider→Divider, list→ListView/GridView, header→AppBar 风格 Container;
- 交互意图 → onPressed 桩(submit-form 弹 SnackBar, navigate/toggle 为 TODO 注释), 不含业务逻辑;
- raw 节点(Web 逃生舱)在 Flutter 侧忽略, 只读类型化字段。
不可被 Playwright 截图(shootable=False): 像素评分不覆盖此目标, 验收口径为「代码可读 + flutter analyze 可过」(工具链为缺口)。
"""
import re
from pathlib import Path

from core.uir.model import UIR, Node, Tokens
from core.uir.registry import register_output, RenderResult, BuildContext

_IDENT = re.compile(r"[^A-Za-z0-9_]")


def _dart_str(s) -> str:
    s = str(s if s is not None else "")
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("$", "\\$").replace("\n", "\\n") + "'"


def _hex_color(v) -> str:
    v = str(v or "").strip()
    m = re.match(r"^#([0-9a-fA-F]{6})$", v)
    if m:
        return f"const Color(0xFF{m.group(1).upper()})"
    m = re.match(r"^#([0-9a-fA-F]{3})$", v)
    if m:
        r, g, b = m.group(1)
        return f"const Color(0xFF{(r*2+g*2+b*2).upper()})"
    m = re.match(r"^rgba?\((\d+),\s*(\d+),\s*(\d+)", v)
    if m:
        return f"const Color(0xFF{int(m.group(1)):02X}{int(m.group(2)):02X}{int(m.group(3)):02X})"
    return "Colors.transparent"


class _Gen:
    def __init__(self, uir: UIR):
        self.uir, self.t = uir, uir.tokens
        self.colors = {}   # token key -> dart const name

    def color(self, v):
        if v is None:
            return None
        if isinstance(v, str) and v in self.t.color:
            name = "c" + _IDENT.sub("_", v)
            self.colors[name] = self.t.color[v]
            return f"AppColors.{name}"
        return _hex_color(self.t.resolve("color", v))

    def px(self, v, group="space"):
        v = self.t.resolve(group, v)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str) and v.endswith("px"):
            return float(v[:-2])
        return None

    def sides(self, vals):
        if vals is None:
            return None
        if not isinstance(vals, (list, tuple)):
            vals = [vals]
        p = [self.px(x) or 0.0 for x in vals[:4]]
        while len(p) < 4:
            p.append(p[-1] if p else 0.0)
        t, r, b, l = p
        if t == r == b == l:
            return f"const EdgeInsets.all({t:g})"
        return f"const EdgeInsets.fromLTRB({l:g}, {t:g}, {r:g}, {b:g})"

    def text_style(self, node: Node):
        ts = self.t.resolve("text_style", node.text_ref) if node.text_ref else None
        parts = []
        if isinstance(ts, dict):
            if ts.get("size") is not None:
                parts.append(f"fontSize: {float(ts['size']):g}")
            if ts.get("weight") is not None:
                w = int(ts["weight"])
                parts.append(f"fontWeight: FontWeight.w{min(900, max(100, round(w / 100) * 100))}")
            if ts.get("line") is not None:
                parts.append(f"height: {float(ts['line']):g}")
            col = ts.get("color")
            if node.box.get("color"):
                col = node.box["color"]
            if col:
                parts.append(f"color: {self.color(col)}")
        elif node.box.get("color"):
            parts.append(f"color: {self.color(node.box['color'])}")
        return f"TextStyle({', '.join(parts)})" if parts else None

    def align(self, v, main=True):
        m = {"start": "start", "center": "center", "end": "end", "between": "spaceBetween", "around": "spaceAround", "stretch": "stretch"}
        key = m.get(v, "start")
        return f"{'MainAxisAlignment' if main else 'CrossAxisAlignment'}.{key}"

    # ------------------------------------------------------------------
    def widget(self, node: Node, depth=0) -> str:
        ind = "  " * (depth + 2)
        c = node.content or {}
        role, L, B = node.role, node.layout or {}, node.box or {}
        if role == "text":
            st = self.text_style(node)
            w = f"Text({_dart_str(c.get('text', ''))}{', style: ' + st if st else ''})"
        elif role in ("badge",):
            st = self.text_style(node)
            w = f"Text({_dart_str(c.get('text', ''))}{', style: ' + st if st else ''})"
        elif role == "image":
            if c.get("drawable"):
                w = f"Image.asset('assets/{c['drawable']}.png', fit: BoxFit.cover)"
            else:
                w = ("Container(color: const Color(0xFFE5E7EB), alignment: Alignment.center, "
                     "child: const Icon(Icons.image_outlined, color: Color(0xFF9CA3AF)))")
        elif role == "icon":
            w = "const Icon(Icons.circle_outlined, size: 20)"
        elif role == "avatar":
            w = "const CircleAvatar(backgroundColor: Color(0xFFE5E7EB), child: Icon(Icons.person_outline))"
        elif role == "divider":
            w = "const Divider(height: 1)"
        elif role == "input":
            obscure = ", obscureText: true" if c.get("type") == "password" else ""
            w = (f"TextField(decoration: InputDecoration(hintText: {_dart_str(c.get('placeholder', ''))}, "
                 f"border: const OutlineInputBorder()){obscure})")
        elif role in ("button", "link"):
            intent = next((i.get("do") for i in node.intents if isinstance(i, dict)), "none")
            body = {"submit-form": "ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('网络繁忙，请稍后重试')));",
                    "countdown": "/* TODO: 倒计时意图 */", "navigate": "/* TODO: navigate 意图 */",
                    "toggle": "/* TODO: toggle 意图 */", "open-overlay": "/* TODO: open-overlay 意图 */"}.get(intent, "")
            label = f"Text({_dart_str(c.get('text', ''))})"
            if role == "link":
                w = f"TextButton(onPressed: () {{ {body} }}, child: {label})"
            else:
                bg = self.color(B.get("bg")) if B.get("bg") else None
                style = f", style: ElevatedButton.styleFrom(backgroundColor: {bg})" if bg else ""
                w = f"ElevatedButton(onPressed: () {{ {body} }}{style}, child: {label})"
        else:
            kids = [self.widget(ch, depth + 1) for ch in node.children]
            if c.get("text"):
                kids.insert(0, f"Text({_dart_str(c['text'])})")
            mode = L.get("mode") or ("flex" if kids else None)
            if mode == "grid":
                cols = int(L.get("cols") or 2)
                gap = self.px(L.get("gap")) or 0
                w = (f"GridView.count(crossAxisCount: {cols}, shrinkWrap: true, physics: const NeverScrollableScrollPhysics(), "
                     f"mainAxisSpacing: {gap:g}, crossAxisSpacing: {gap:g}, childAspectRatio: 1.2, children: [\n{ind}  "
                     + f",\n{ind}  ".join(kids) + f",\n{ind}])")
            elif mode == "stack":
                w = f"Stack(children: [\n{ind}  " + f",\n{ind}  ".join(kids) + f",\n{ind}])" if kids else "const SizedBox.shrink()"
            elif mode == "absolute":
                w = kids[0] if kids else "const SizedBox.shrink()"
            elif kids:
                row = L.get("direction") == "row"
                gap = self.px(L.get("gap"))
                if gap:
                    spaced = []
                    for i, k in enumerate(kids):
                        if i:
                            spaced.append(f"SizedBox({'width' if row else 'height'}: {gap:g})")
                        spaced.append(k)
                    kids = spaced
                main = self.align(L.get("justify"), True)
                cross = self.align(L.get("align") or ("center" if row else "stretch"), False)
                cls = "Row" if row else "Column"
                if role == "list" and not row:
                    cls = "Column"
                w = (f"{cls}(mainAxisSize: MainAxisSize.min, mainAxisAlignment: {main}, crossAxisAlignment: {cross}, "
                     f"children: [\n{ind}  " + f",\n{ind}  ".join(kids) + f",\n{ind}])")
            else:
                w = "const SizedBox.shrink()"
        # Container 包装: padding / margin / 背景 / 圆角 / 边框 / 阴影 / 尺寸
        deco = []
        if B.get("bg"):
            deco.append(f"color: {self.color(B['bg'])}")
        if B.get("radius") is not None:
            r = self.px(B["radius"], "radius")
            if r is not None:
                deco.append(f"borderRadius: BorderRadius.circular({min(r, 999):g})")
        if isinstance(B.get("border"), dict):
            bd = B["border"]
            deco.append(f"border: Border.all(width: {self.px(bd.get('w', 1)) or 1:g}, color: {self.color(bd.get('color', '#e5e7eb'))})")
        if B.get("shadow"):
            deco.append("boxShadow: const [BoxShadow(color: Color(0x14000000), blurRadius: 8, offset: Offset(0, 2))]")
        args = []
        pad, mg = self.sides(B.get("pad")), self.sides(B.get("margin"))
        if pad:
            args.append(f"padding: {pad}")
        if mg:
            args.append(f"margin: {mg}")
        for k, dk in (("w", "width"), ("h", "height")):
            v = B.get(k)
            if isinstance(v, (int, float)):
                args.append(f"{dk}: {float(v):g}")
            elif v == "100%" and k == "w":
                args.append("width: double.infinity")
        if deco:
            args.append(f"decoration: BoxDecoration({', '.join(deco)})")
        if args:
            w = f"Container({', '.join(args)}, child: {w})"
        if B.get("grow"):
            w = f"Expanded(child: {w})"
        return w

    def main_dart(self) -> str:
        body = self.widget(self.uir.root)
        title = self.uir.meta.get("title") or "Generated App"
        return f"""// 由 Clonjurer 通用 UI 重建引擎从 UIR v1 生成(OutputTarget: flutter)。
// 只含界面外观与交互意图桩, 不含业务逻辑。
import 'package:flutter/material.dart';
import 'theme.dart';

void main() => runApp(const GeneratedApp());

class GeneratedApp extends StatelessWidget {{
  const GeneratedApp({{super.key}});
  @override
  Widget build(BuildContext context) => MaterialApp(
        title: {_dart_str(title)},
        theme: AppTheme.light(),
        home: const GeneratedPage(),
      );
}}

class GeneratedPage extends StatelessWidget {{
  const GeneratedPage({{super.key}});
  @override
  Widget build(BuildContext context) {{
    return Scaffold(
      body: SafeArea(
        child: SingleChildScrollView(
          child: {body},
        ),
      ),
    );
  }}
}}
"""

    def theme_dart(self) -> str:
        lines = ["import 'package:flutter/material.dart';", "", "class AppColors {"]
        for name, hexv in sorted(self.colors.items()):
            lines.append(f"  static const Color {name} = {_hex_color(hexv).replace('const ', '')};")
        lines += ["}", "", "class AppTheme {", "  static ThemeData light() => ThemeData(", "        useMaterial3: true,"]
        primary = self.t.color.get("c2") or self.t.color.get("c0") or "#2563eb"
        lines.append(f"        colorSchemeSeed: {_hex_color(primary).replace('const ', '')},")
        lines += ["      );", "}", ""]
        return "\n".join(lines)


_PUBSPEC = """name: generated_ui
description: Generated from UIR v1 by Clonjurer universal UI engine.
publish_to: 'none'
version: 0.1.0
environment:
  sdk: '>=3.3.0 <4.0.0'
dependencies:
  flutter:
    sdk: flutter
flutter:
  uses-material-design: true
"""

_README = """# 生成的 Flutter 工程(最小版)

由 UIR v1 渲染而来, 只含界面外观与交互意图桩。构建需要本机具备 Flutter SDK + Android SDK(本仓库不含):

```
flutter create . --platforms=android   # 补齐平台工程骨架
flutter pub get
flutter analyze
flutter build apk --debug              # 发布包需配置签名(key.properties / keystore)
```

落地缺口(见 DESIGN.md §9.11): 工具链/构建 CI/签名链、资源(drawable)搬运、像素评分不覆盖 Flutter 渲染。
"""


@register_output("flutter")
class FlutterTarget:
    shootable = False

    def render(self, uir: UIR, out_dir: Path, ctx: BuildContext = None) -> RenderResult:
        out = Path(out_dir) / "flutter_app"
        (out / "lib").mkdir(parents=True, exist_ok=True)
        uir.normalize()
        g = _Gen(uir)
        main = g.main_dart()
        (out / "lib" / "main.dart").write_text(main, encoding="utf-8")
        (out / "lib" / "theme.dart").write_text(g.theme_dart(), encoding="utf-8")
        (out / "pubspec.yaml").write_text(_PUBSPEC, encoding="utf-8")
        (out / "README.md").write_text(_README, encoding="utf-8")
        (Path(out_dir) / "uir.json").write_text(uir.to_json(indent=2), encoding="utf-8")
        return RenderResult(entry_file=out / "lib" / "main.dart", shootable=False,
                            assets=["flutter_app/lib/theme.dart", "flutter_app/pubspec.yaml", "uir.json"],
                            meta={"nodes": uir.root.count(), "bytes": len(main), "widgets": main.count("(")})
