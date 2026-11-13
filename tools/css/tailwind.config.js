// 平台前端 Tailwind 预编译配置(原运行时 tailwind.js 内联 tailwind.config 的同一份主题)。
// 改了 templates/index.html 的 class 后运行 tools/css/build.sh 重新生成 static_vendor/css/app.css。
const path = require('path');
module.exports = {
  content: [path.join(__dirname, '../../templates/index.html')],
  theme: { extend: { fontFamily: {
    sans: ['Inter', 'Plus Jakarta Sans', 'Noto Sans SC', '-apple-system', 'sans-serif'],
    display: ['Plus Jakarta Sans', 'Noto Sans SC', 'sans-serif'],
    mono: ['JetBrains Mono', 'Fira Code', 'monospace'],
  } } },
};
