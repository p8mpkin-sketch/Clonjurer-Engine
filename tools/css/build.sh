#!/usr/bin/env bash
# 重新生成平台前端样式 static_vendor/css/app.css(Tailwind 3.4 预编译, 约 50KB / gzip 9KB)。
# 用法: tools/css/build.sh        需要 node/npx; tests/test_ui_css.py 会检查漏编的 class。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
printf '@tailwind base;\n@tailwind components;\n@tailwind utilities;\n' > "$here/.in.css"
npx -y tailwindcss@3.4.19 -c "$here/tailwind.config.js" -i "$here/.in.css" -o "$here/../../static_vendor/css/app.css" --minify
rm -f "$here/.in.css"
