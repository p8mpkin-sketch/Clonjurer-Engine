// 平台前端 Tailwind 预编译配置。改了 templates/index.html 的 class 后运行 tools/css/build.sh 重新生成 static_vendor/css/app.css。
//
// 视觉定稿(2026-11-14): 中性灰 + 墨色主按钮 + 一个克制的钴蓝, 语义色只留 成功/警告/错误。
// 模板里原有的 9 个色系不逐个改 class, 而是在这里把色阶重映射到 4 组 CSS 变量(定义在 ui-tokens.css):
//   slate                                   → 中性灰 --n-*   (浅色主题整段反转: 950 白底 ↔ 50 墨色)
//   indigo violet purple fuchsia sky cyan blue → 强调色 --a-*  (浅色主题以 600 为轴反转, 浅字变深字)
//   emerald green teal                      → 成功 --ok-*
//   amber yellow orange                     → 警告 --warn-*
//   rose red pink                           → 错误 --bad-*
// 这样深浅主题由变量切换, 不再需要几十条 [data-theme=light] 覆盖规则。
const path = require('path');
const STEPS = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950];
const scale = (v) => Object.fromEntries(STEPS.map((s) => [s, `rgb(var(--${v}-${s}) / <alpha-value>)`]));
const n = scale('n'), a = scale('a'), ok = scale('ok'), warn = scale('warn'), bad = scale('bad');
module.exports = {
  content: [path.join(__dirname, '../../templates/index.html')],
  theme: { extend: {
    colors: {
      slate: n, gray: n, zinc: n, neutral: n,
      indigo: a, violet: a, purple: a, fuchsia: a, sky: a, cyan: a, blue: a,
      emerald: ok, green: ok, teal: ok,
      amber: warn, yellow: warn, orange: warn,
      rose: bad, red: bad, pink: bad,
    },
    fontFamily: {
      sans: ['Inter', '"PingFang SC"', '"Hiragino Sans GB"', '"Noto Sans SC"', '"Microsoft YaHei"', '-apple-system', 'sans-serif'],
      display: ['Inter', '"PingFang SC"', '"Hiragino Sans GB"', '"Noto Sans SC"', '"Microsoft YaHei"', 'sans-serif'],
      mono: ['"JetBrains Mono"', 'ui-monospace', 'Menlo', 'monospace'],
    },
  } },
};
