// 平台前端图标子集: 从完整 lucide.min.js(356KB, 1737 个图标)里只取 templates/index.html 用到的图标,
// 生成 static_vendor/js/lucide.subset.js(createIcons 与官方 0.460 同语义: 同属性/同 class/同替换方式)。
// 用法: node tools/icons/build_lucide_subset.js   (改了模板里的图标名后重跑; tests/test_ui_css.py 会检查)
const fs = require('fs'), path = require('path'), vm = require('vm');
const root = path.join(__dirname, '../..');
const full = fs.readFileSync(path.join(__dirname, 'lucide.full.min.js'), 'utf8');
const ctx = {}; ctx.globalThis = ctx; ctx.self = ctx; vm.createContext(ctx); vm.runInContext(full, ctx);
const L = ctx.lucide;
const pascal = t => t.replace(/(\w)(\w*)(_|-|\s*)/g, (d, c, p) => c.toUpperCase() + p.toLowerCase());
const html = fs.readFileSync(path.join(root, 'templates/index.html'), 'utf8');
const names = new Set();
for (const m of html.matchAll(/["'`]([a-z][a-z0-9]*(?:-[a-z0-9]+)*)["'`]/g)) if (L.icons[pascal(m[1])]) names.add(m[1]);
for (const m of html.matchAll(/data-lucide="([a-z0-9-]+)"/g)) names.add(m[1]);
const icons = {};
for (const n of [...names].sort()) { const k = pascal(n); if (L.icons[k]) icons[k] = L.icons[k][2]; }
const out = `/* lucide v0.460.0 子集(ISC license), 由 tools/icons/build_lucide_subset.js 生成, 勿手改; ${Object.keys(icons).length} 个图标 */
(function () {
  var A = {xmlns: "http://www.w3.org/2000/svg", width: 24, height: 24, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round"};
  var I = ${JSON.stringify(icons)};
  var icons = {}; Object.keys(I).forEach(function (k) { icons[k] = ["svg", A, I[k]]; });
  function el(t, a, c) { var e = document.createElementNS("http://www.w3.org/2000/svg", t); Object.keys(a).forEach(function (k) { e.setAttribute(k, String(a[k])); }); (c || []).forEach(function (x) { e.appendChild(el(x[0], x[1], x[2])); }); return e; }
  function pascal(t) { return t.replace(/(\\w)(\\w*)(_|-|\\s*)/g, function (d, c, p) { return c.toUpperCase() + p.toLowerCase(); }); }
  function cls(t) { return typeof t == "string" ? t : !t || !t.class ? "" : typeof t.class == "string" ? t.class.split(" ") : Array.isArray(t.class) ? t.class : ""; }
  function join(l) { return l.flatMap(cls).map(function (d) { return d.trim(); }).filter(Boolean).filter(function (d, i, a) { return a.indexOf(d) === i; }).join(" "); }
  function one(t, o) {
    var n = t.getAttribute(o.nameAttr); if (n == null) return;
    var ic = o.icons[pascal(n)]; if (!ic) return console.warn(t.outerHTML + " icon name was not found in the provided icons object.");
    var own = Array.from(t.attributes).reduce(function (d, c) { d[c.name] = c.value; return d; }, {});
    var at = Object.assign({}, ic[1], {"data-lucide": n}, o.attrs, own), c = join(["lucide", "lucide-" + n, own, o.attrs]);
    if (c) at.class = c;
    if (t.parentNode) t.parentNode.replaceChild(el(ic[0], at, ic[2]), t);
  }
  function createIcons(opt) {
    opt = opt || {}; var o = {icons: opt.icons || icons, nameAttr: opt.nameAttr || "data-lucide", attrs: opt.attrs || {}};
    Array.from(document.querySelectorAll("[" + o.nameAttr + "]")).forEach(function (t) { one(t, o); });
  }
  window.lucide = {icons: icons, createIcons: createIcons, createElement: function (i) { return el(i[0], i[1], i[2]); }};
})();
`;
fs.writeFileSync(path.join(root, 'static_vendor/js/lucide.subset.js'), out);
console.log('icons', Object.keys(icons).length, 'bytes', out.length);
