"""克隆引擎注入到浏览器页面的脚本与样式片段(自 core/cloner_engine.py 纯搬迁, 文本逐字不变)。"""
# flake8: noqa
import json
# ---------------------------------------------------------------------------
# 浏览器端注入脚本
# ---------------------------------------------------------------------------
LAZY_JS = r"""
() => {
  let n = 0;
  document.querySelectorAll('[data-src],[data-original],[data-lazy-src],[data-lazy]').forEach(el => {
    const v = el.getAttribute('data-src') || el.getAttribute('data-original')
            || el.getAttribute('data-lazy-src') || el.getAttribute('data-lazy');
    if (!v) return;
    const tag = el.tagName;
    if (tag === 'IMG' || tag === 'SOURCE' || tag === 'IFRAME' || tag === 'VIDEO' || tag === 'AUDIO') {
      if (!el.getAttribute('src')) { el.setAttribute('src', v); n++; }
    } else {
      const s = el.getAttribute('style') || '';
      if (!/background-image/.test(s)) {
        el.setAttribute('style', s + 'background-image:url(' + v + ');');
        n++;
      }
    }
  });
  return n;
}
"""

# 冻结前等未加载完的样式表: 媒体条件不匹配当前视口的 <link>(手机断点表)浏览器按最低优先级
# 排在大量图片之后, 冻结时 sheet 常为空, CSSOM 副本缺失且随机(cgnpc 4 次采集 0~3 张不等)。封顶等待。
WAIT_SHEETS_JS = r"""
(capMs) => new Promise(res => {
  const t0 = performance.now();
  const isPending = l => {
    if (l.disabled) return false;
    if (!l.sheet) return true;
    try { return !l.sheet.cssRules; } catch (e) { return false; }   // 跨域可读性无关加载
  };
  const links = Array.from(document.querySelectorAll('link[rel~="stylesheet" i][href]')).filter(isPending);
  // 不指望浏览器的最低优先级队列: 同时直接 fetch 一份, 先到先用(到了就紧跟原链接插副本)
  let fetched = 0, done = false;   // 封顶返回后迟到的 fetch 不再插(冻结已按浏览器的表序列化, 再插即重复)
  const absolutize = (text, base) => text.replace(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^'")\s]+))\s*\)/g, (m, d, s1, b) => {
    const q = d !== undefined ? '"' : (s1 !== undefined ? "'" : '');
    const u = (d !== undefined ? d : (s1 !== undefined ? s1 : b)).trim();
    if (/^(data:|blob:|about:|#|%23|https?:)/i.test(u)) return m;
    try { return 'url(' + q + new URL(u, base).href + q + ')'; } catch (e) { return m; }
  });
  const jobs = links.map(l => fetch(l.href, {credentials: 'include'}).then(r => r.ok ? r.text() : '').then(text => {
    if (done || !text || !isPending(l) || !l.isConnected || /<\/?(html|body|style)\b/i.test(text)) return;
    const media = (l.getAttribute('media') || '').trim();
    const st = document.createElement('style');
    st.setAttribute('data-cssom', 'fetched');
    text = absolutize(text, l.href);
    st.textContent = (media && media.toLowerCase() !== 'all') ? '@media ' + media + ' {\n' + text + '\n}\n' : text;
    l.after(st);
    fetched++;
  }).catch(() => 0));
  const tick = () => {
    const n = links.filter(l => isPending(l) && !(l.nextElementSibling && l.nextElementSibling.getAttribute('data-cssom') === 'fetched')).length;
    if (!n || performance.now() - t0 > capMs) { done = true; res({pending: n, fetched: fetched, ms: Math.round(performance.now() - t0)}); }
    else setTimeout(tick, 100);
  };
  if (!links.length) return res({pending: 0, fetched: 0, ms: 0});
  tick();
})
"""

# Canvas 固化成 <img>: 保留原 class/id/内联样式, width/height 属性取位图尺寸(给出固有宽高比), 未内联定高时
# height:auto —— 响应式 canvas(class 给 w-full / calc 宽)在别的视口仍按比例伸缩。只有原地试放后尺寸与 canvas
# 不符(样式靠 canvas 标签选择器等)才回落成采集视口下的固定 px(旧做法: 窄屏变体里写死 390 宽的 px,
# 420/820 宽下逐图偏短、下文累积上移)
CANVAS_IMG_FN = r"""(cv, data) => {
  const img = document.createElement('img');
  img.src = data;
  img.setAttribute('data-canvas-frozen', 'true');
  // 原 canvas 的全部属性照搬(class/id/style 之外还有引擎此前打的 data-cb/data-cv 等, 烘焙/分视口规则靠它们命中)
  for (const a of cv.attributes) if (a.name !== 'width' && a.name !== 'height') img.setAttribute(a.name, a.value);
  if (cv.width && cv.height) { img.setAttribute('width', String(cv.width)); img.setAttribute('height', String(cv.height)); }
  if (!cv.style.height) img.style.height = 'auto';
  // 站点基础样式只对 img 生效的规则(img{max-width:100%} / .img-fluid 等)不该作用在原本是 canvas 的位置:
  // 把 canvas 的计算值 min/max 尺寸内联到 img(vite 比容器宽 2.5rem 的 canvas 被 max-width:100% 截短)
  try {
    const cs = getComputedStyle(cv);
    for (const k of ['max-width', 'min-width', 'max-height', 'min-height']) if (!cv.style.getPropertyValue(k)) img.style.setProperty(k, cs.getPropertyValue(k));
  } catch (e) {}
  let fit = false;
  try {
    const r0 = cv.getBoundingClientRect(), st = cv.getAttribute('style');
    cv.before(img);
    cv.style.setProperty('display', 'none', 'important');
    const r1 = img.getBoundingClientRect();
    img.remove();
    if (st === null) cv.removeAttribute('style'); else cv.setAttribute('style', st);
    fit = r0.width > 0 && r0.height > 0 && Math.abs(r1.width - r0.width) <= 1 && Math.abs(r1.height - r0.height) <= 1;
  } catch (e) {}
  if (!fit) {
    if (cv.clientWidth) img.style.width = cv.clientWidth + 'px';
    if (cv.clientHeight) img.style.height = cv.clientHeight + 'px';
    // 未渲染(隐藏面板/未激活 tab/窄屏页尚未布局)量不到尺寸: 用样式表给 canvas 的计算值(px 才可信)
    if (!cv.clientWidth || !cv.clientHeight) {
      try {
        const cs = getComputedStyle(cv);
        if (!cv.clientWidth && /^[\d.]+px$/.test(cs.width)) img.style.width = cs.width;
        if (!cv.clientHeight && /^[\d.]+px$/.test(cs.height)) img.style.height = cs.height;
      } catch (e) {}
    }
  }
  return img;
}"""

DEEP_FREEZE_JS = r"""
() => {
  const result = { lazy: 0, css: 0, crossOriginSheets: 0, canvas: 0, forms: 0, revealed: 0 };

  // 1) 懒加载属性标准化
  document.querySelectorAll('[data-src],[data-original],[data-lazy-src],[data-lazy]').forEach(el => {
    const v = el.getAttribute('data-src') || el.getAttribute('data-original')
            || el.getAttribute('data-lazy-src') || el.getAttribute('data-lazy');
    if (!v) return;
    const tag = el.tagName;
    if (tag === 'IMG' || tag === 'SOURCE' || tag === 'IFRAME' || tag === 'VIDEO' || tag === 'AUDIO') {
      el.setAttribute('src', v);
    } else {
      const s = el.getAttribute('style') || '';
      if (!/background-image/.test(s)) el.setAttribute('style', s + 'background-image:url(' + v + ');');
    }
    result.lazy++;
  });

  // 2) 滚动入场动画强制显形: AOS / WOW(GSAP) / ScrollReveal / 通用 data-reveal
  document.querySelectorAll('[data-aos], .wow, [data-scroll], [data-sr-id], [data-reveal], [data-animate]').forEach(el => {
    el.style.setProperty('opacity', '1', 'important');
    el.style.setProperty('visibility', 'visible', 'important');
    el.style.setProperty('transform', 'none', 'important');
    el.style.setProperty('filter', 'none', 'important');
    el.style.setProperty('transition', 'none', 'important');
    el.classList.add('aos-animate');
    el.classList.remove('wow');
    result.revealed++;
  });

  // 3) 安全属性清理
  document.querySelectorAll('base').forEach(el => el.remove());
  document.querySelectorAll('meta[http-equiv="Content-Security-Policy" i]').forEach(el => el.remove());
  document.querySelectorAll('[integrity]').forEach(el => el.removeAttribute('integrity'));
  document.querySelectorAll('[crossorigin]').forEach(el => el.removeAttribute('crossorigin'));

  // 4) CSSOM 运行时样式序列化(Styled-Components/Emotion/Tailwind JIT)
  //    关键: 相对 url() 必须按样式表自身地址(sheet.href)绝对化。
  //    否则内联固化后解析基准从 CSS 文件目录漂移到页面目录,
  //    ../images/x.png 之类引用会 404 且级联覆盖正确的外部 CSS 规则。
  const head = document.head || document.documentElement;
  //    样式表的媒体条件必须随内联保留: 旧实现把 media="print" 的表当普通
  //    表内联, 打印样式(常见 .header,.footer{display:none!important})在屏幕
  //    上生效, 克隆页整块页眉页脚消失(wordpress.org 实证 28 分)。
  const serializeSheet = (sheet, kind) => {
    try {
      if (sheet.disabled) return;
      if (sheet.ownerNode && sheet.ownerNode.hasAttribute && (sheet.ownerNode.hasAttribute('data-clone-bake')
          || sheet.ownerNode.hasAttribute('data-cssom'))) return;   // 引擎自己插的副本(含冻结前直取的)不再复制
      // 冻结前已 fetch 补过副本(WAIT_SHEETS_JS)的表不再重复
      { const nx = sheet.ownerNode && sheet.ownerNode.nextElementSibling;
        if (nx && nx.getAttribute && nx.getAttribute('data-cssom') === 'fetched') return; }
      const rules = sheet.cssRules;
      if (!rules || !rules.length) return;
      let text = '';
      for (let i = 0; i < rules.length; i++) text += rules[i].cssText + '\n';
      if (!text.trim()) return;
      const base = sheet.href || document.baseURI;
      //    引号内整体匹配: url("data:image/svg+xml,...fill='url(%23id)'...") 外层含单引号时,
      //    旧正则会钻进 data: 内部把 url(%23id) 改写成远程地址(SVG 渐变失填充 + 无效下载)
      text = text.replace(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^'")\s]+))\s*\)/g,
        function (m, d, s1, b) {
          const q = d !== undefined ? '"' : (s1 !== undefined ? "'" : '');
          const u = (d !== undefined ? d : (s1 !== undefined ? s1 : b)).trim();
          if (/^(data:|blob:|about:|#|%23|https?:)/i.test(u)) return m;
          try { return 'url(' + q + new URL(u, base).href + q + ')'; }
          catch (e) { return m; }
        });
      let media = '';
      try { media = (sheet.media && sheet.media.mediaText || '').trim(); } catch (e) { media = ''; }
      if (!media && sheet.ownerNode && sheet.ownerNode.getAttribute) media = (sheet.ownerNode.getAttribute('media') || '').trim();
      if (media && media.toLowerCase() !== 'all') text = '@media ' + media + ' {\n' + text + '}\n';
      const style = document.createElement('style');
      style.setAttribute('data-cssom', kind);
      style.textContent = text;
      // 副本紧跟原节点: 保持与其它样式表的层叠先后。全部追加到 head 末尾时, 某张表的副本缺失
      // (媒体条件不匹配的表最低优先级加载, 冻结时可能还没到)会让其它表的副本反压它的原链接规则
      const own = sheet.ownerNode;
      // (body 里的原节点同样就地插: 副本与原表语义等价, 不改层叠; 窄屏变体的结构比对不计 <style>, 见 CV_PATHS_JS)
      if (kind === 'true' && own && own.isConnected && own.parentNode) own.after(style);
      else head.appendChild(style);
      result.css++;
    } catch (err) {
      result.crossOriginSheets++;
    }
  };
  Array.from(document.styleSheets).forEach(sheet => serializeSheet(sheet, 'true'));
  //    构造样式表(CSSStyleSheet + adoptedStyleSheets): CSS-in-JS 运行时/Vite 注入的
  //    样式不在 document.styleSheets, outerHTML 与 MHTML 都带不走(remix.run 实证:
  //    两轨整页无样式 34 分); 文档级采纳表级联在普通表之后, 按序追加到 head 末尾。
  try {
    (document.adoptedStyleSheets || []).forEach(sheet => serializeSheet(sheet, 'adopted'));
  } catch (err) {
    result.crossOriginSheets++;
  }

  // 5) Canvas 固化为位图
  document.querySelectorAll('canvas').forEach(cv => {
    try {
      const data = cv.toDataURL('image/png');
      if (data && data.length > 128) {
        cv.replaceWith((__CANVAS_IMG__)(cv, data));
        result.canvas++;
      }
    } catch (err) {
      result.canvas += 0;
    }
  });

  // 6) 表单状态固化
  document.querySelectorAll('input').forEach(el => {
    if (el.type === 'file') return;
    if (el.type === 'checkbox' || el.type === 'radio') {
      if (el.checked) el.setAttribute('checked', ''); else el.removeAttribute('checked');
    }
    el.setAttribute('value', el.value || '');
    result.forms++;
  });
  document.querySelectorAll('textarea').forEach(el => { el.textContent = el.value || ''; result.forms++; });
  document.querySelectorAll('option').forEach(op => {
    if (op.selected) op.setAttribute('selected', ''); else op.removeAttribute('selected');
  });
  document.querySelectorAll('select').forEach(sel => {
    if (sel.selectedIndex >= 0 && sel.options[sel.selectedIndex]) {
      sel.setAttribute('data-selected', sel.options[sel.selectedIndex].value);
    }
  });
  return result;
}
""".replace("__CANVAS_IMG__", CANVAS_IMG_FN)

# 表单激活: focus/blur 触发懒加载样式与占位符逻辑(不点击, 防止误提交/误导航)
# 视觉媒体快照(纯净基准截图之后、DOM 改动之前执行):
# - 空白/跨域污染的 Canvas(WebGL 未开 preserveDrawingBuffer 时 toDataURL 恒为空白,
#   跨域贴图会让读像素直接抛错): DEEP_FREEZE 的 toDataURL 固化只能得到透明图;
# - 无 poster 的 <video>: 离线回放时剥了脚本不会播放, 编解码不支持时整块空白。
# 能直接读到当前帧的视频就地写 poster; 其余打 data-clone-shot 标记并返回文档坐标,
# 由引擎从纯净基准截图裁出对应区域回填(MEDIA_SHOT_APPLY_JS)。
MEDIA_SHOT_JS = r"""
() => {
  const out = [];
  const sx = window.scrollX || 0, sy = window.scrollY || 0;
  const rectOf = el => { const r = el.getBoundingClientRect();
                         return {x: r.left + sx, y: r.top + sy, w: r.width, h: r.height}; };
  const blank = cv => {
    try {
      const w = Math.min(cv.width, 64), h = Math.min(cv.height, 64);
      if (!w || !h) return true;
      const t = document.createElement('canvas'); t.width = w; t.height = h;
      const c = t.getContext('2d'); c.drawImage(cv, 0, 0, w, h);
      const d = c.getImageData(0, 0, w, h).data;
      for (let i = 3; i < d.length; i += 4) { if (d[i] !== 0) return false; }
      return true;
    } catch (err) { return true; }   // 跨域污染: 读不到像素, 只能走截图
  };
  let n = 0, posters = 0;
  document.querySelectorAll('canvas').forEach(cv => {
    const r = rectOf(cv);
    if (r.w < 8 || r.h < 8 || !blank(cv)) return;
    cv.setAttribute('data-clone-shot', String(n));
    out.push(Object.assign({n: n++, kind: 'canvas'}, r));
  });
  let pinned = 0;
  document.querySelectorAll('video').forEach(v => {
    const r = rectOf(v);
    if (r.w < 8 || r.h < 8) return;
    // 播放后才显形的视频(脚本在 playing/canplay 时把 opacity 从 0 拉到 1): 克隆里视频
    // 不播, 显形逻辑不触发, 整块按样式表初值透明(wix 首屏光晕整片丢失)。基准时刻的
    // 不透明度钉成内联, 封面帧才看得见
    try {
      const op = parseFloat(getComputedStyle(v).opacity);
      if (op > 0) { v.style.setProperty('opacity', String(op), 'important'); pinned++; }
    } catch (err) {}
    // 基准时刻的播放进度: 克隆评分前跳到同一帧并暂停(录屏式预览视频逐帧都在变,
    // 克隆从头播放到截图那一刻与基准是两帧, nuxt 展示墙整排失配)
    try { if (v.readyState >= 2 && v.currentTime > 0) v.setAttribute('data-clone-vt', v.currentTime.toFixed(3)); } catch (err) {}
    if (v.getAttribute('poster')) return;
    try {
      if (v.readyState >= 2 && v.videoWidth) {
        const t = document.createElement('canvas');
        t.width = v.videoWidth; t.height = v.videoHeight;
        t.getContext('2d').drawImage(v, 0, 0);
        // 带 alpha 的视频(VP9/HEVC 透明通道, 常用作光晕/悬浮物件叠层): JPEG 丢透明,
        // 封面成了整块不透明底色盖住下层。WebP 保留 alpha, 不支持时才退回 JPEG
        let data = t.toDataURL('image/webp', 0.85);
        if (!data || !data.startsWith('data:image/webp')) data = t.toDataURL('image/jpeg', 0.85);
        if (data && data.length > 2000) { v.setAttribute('poster', data); v.setAttribute('data-clone-poster', '1'); posters++; return; }
      }
    } catch (err) { /* 跨域视频帧不可读 → 截图 */ }
    v.setAttribute('data-clone-shot', String(n));
    out.push(Object.assign({n: n++, kind: 'video'}, r));
  });
  return {shots: out, posters: posters, pinned: pinned,
          docW: Math.max(document.documentElement.scrollWidth, window.innerWidth || 0)};
}
"""

MEDIA_SHOT_APPLY_JS = r"""
(shots) => {
  let applied = 0;
  document.querySelectorAll('[data-clone-shot]').forEach(el => {
    const src = shots[el.getAttribute('data-clone-shot')];
    el.removeAttribute('data-clone-shot');
    if (!src) return;
    if (el.tagName === 'VIDEO') { el.setAttribute('poster', src); el.setAttribute('data-clone-poster', '1'); applied++; return; }
    const img = document.createElement('img');
    for (const a of Array.from(el.attributes)) {
      if (a.name === 'width' || a.name === 'height') continue;
      img.setAttribute(a.name, a.value);
    }
    img.src = src;
    img.setAttribute('data-canvas-frozen', 'shot');
    img.style.width = (el.clientWidth || el.getBoundingClientRect().width) + 'px';
    img.style.height = (el.clientHeight || el.getBoundingClientRect().height) + 'px';
    el.replaceWith(img);
    applied++;
  });
  return applied;
}
"""

FORM_ACTIVATE_JS = r"""
async () => {
  let n = 0;
  // focus 默认把输入框滚进视口: 滚动触发的 IntersectionObserver/滚动监听会改版面
  // (wix 模板轮播进视口即换页, 序列化的 DOM 与基准截图不是同一页)。不滚, 滚了也回原位
  const sx = window.scrollX, sy = window.scrollY;
  document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]), textarea, select').forEach(el => {
    if (n >= 60) return;
    try { el.focus({preventScroll: true}); el.blur(); n++; } catch (e) {}
  });
  try { if (window.scrollX !== sx || window.scrollY !== sy) window.scrollTo(sx, sy); } catch (e) {}
  if (n) {
    // "点外部关闭"类浮层(搜索下拉/联想面板)focus 即展开、blur 关不掉, 只认外部 mousedown:
    // 在 body 上派发一次外部点击序列收起它们(aliyun 顶栏搜索面板被固化进克隆)
    try {
      const o = {bubbles: true, cancelable: true, view: window, clientX: 1, clientY: 1};
      for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        const E = t.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
        document.body.dispatchEvent(new E(t, o));
      }
      await new Promise(r => setTimeout(r, 120));
    } catch (e) {}
  }
  return n;
}
"""

# SPA 版本升级弹层探测: 部分企业应用(灰度发布体系)首访弹"检测到有新版本"
# 全屏对话框并 4~5s 自动 reload, 需在活体渲染期主动走完升级流程再固化,
# 否则克隆页会被弹层永久接管(冻结的 HTML 无法通过 reload 获得新版本)。
UPDATE_PROMPT_PROBE_JS = r"""
() => {
  try {
    const scopes = document.querySelectorAll(
      '.el-dialog__wrapper,.el-message-box__wrapper,.ant-modal,.modal-mask,.v-modal,[class*="version-mask"]');
    for (const m of scopes) {
      const style = getComputedStyle(m);
      if (style.display === 'none' || style.visibility === 'hidden') continue;
      const t = (m.innerText || '').slice(0, 240);
      if (/新版本|自动更新|立即更新|版本升级|new version|update available|refresh to get|please refresh/i.test(t)) {
        return { found: true, text: t.replace(/\s+/g, ' ').slice(0, 100) };
      }
    }
  } catch (e) {}
  return { found: false };
}
"""

UPDATE_PROMPT_CLICK_JS = r"""
() => {
  const nodes = document.querySelectorAll('button, a, span, div');
  for (const b of nodes) {
    if (!b.offsetParent) continue;
    const bt = (b.innerText || '').trim();
    if (/^(立即自动更新|立即更新|立即刷新|马上更新|update now|refresh now|reload)$/i.test(bt) && bt.length <= 16) {
      b.click();
      return bt;
    }
  }
  return null;
}
"""

STORAGE_SNAPSHOT_JS = r"""
() => {
  const out = { local: {}, session: {} };
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      out.local[k] = localStorage.getItem(k);
    }
  } catch (e) {}
  try {
    for (let i = 0; i < sessionStorage.length; i++) {
      const k = sessionStorage.key(i);
      out.session[k] = sessionStorage.getItem(k);
    }
  } catch (e) {}
  return out;
}
"""

# 克隆页状态恢复: Cookie / localStorage / sessionStorage 快照前置写入(仅缺失键),
# 使"版本守卫/引导步骤/语言偏好"类 SPA 在克隆中自洽启动, 不弹升级框。
# 埋点引擎观察器胶水(随交互层注入): 点击登录/验证码时由交互层回调,
# 填探测值触发引擎 fire 上报, 上报后恢复原值 —— 事件被交互层拦截时埋点仍能工作。
TRACKER_OBSERVE_JS = r"""<script data-clone-preview="tracker-observe">(function(){
  if (window.__ft_observe) return;
  var last = 0;
  window.__ft_observe = function (el, intent) {
    if (el.__ftBusy || Date.now() - last < 1500) return;
    el.__ftBusy = 1; last = Date.now();
    try {
      var inputs = Array.prototype.slice.call(
        document.querySelectorAll('input')).filter(function (i) {
          return i.offsetParent && !/^(button|submit|reset|file|hidden)$/i.test(i.type);
        });
      // 用户已填真实值时不覆盖(保真优先); 全空才填探测值驱动引擎 fire
      var hasUserInput = inputs.some(function (i) { return (i.value || '').length > 0; });
      if (hasUserInput) { el.__ftBusy = 0; return; }
      var saved = inputs.map(function (i) { return i.value; });
      inputs.forEach(function (i, idx) {
        i.value = 'ft_' + (idx + 1);
        i.dispatchEvent(new Event('input', {bubbles: true}));
      });
      setTimeout(function () {
        inputs.forEach(function (i, idx) { i.value = saved[idx]; });
        el.__ftBusy = 0;
      }, 600);
    } catch (e) { el.__ftBusy = 0; }
  };
})();
</script>"""

# 视口作用域烘焙: 引擎按采集视口(1920)的计算值做的固化(字体度量/WAAPI 终态)以前写成内联
# !important, 任何宽度下都压过站点自己的媒体查询 —— 克隆在平板/手机视口下仍是桌面字号与
# 版式(cn.vuejs 手机视口 45 分, 去掉后 100)。改为: 相同声明集合并成一个令牌, 元素打
# data-cb 令牌, 规则写进 <style data-clone-bake>, 外包 "@media all" 占位; 落盘时
# (_scope_bake_css)按站点样式表断点换成"采集视口所在断点区间", 区间外回落到站点 CSS。
# 选择器带 3 个 :not(#\#) 提到 (3,1,0) 特异度 + !important, 采集视口下与内联 !important 等效。
SCOPED_BAKE_FN = r"""(kind, plan) => {
  const SPEC = ':not(#\\#):not(#\\#):not(#\\#)';
  const seq = (window.__cloneBakeSeq = (window.__cloneBakeSeq || 0) + 1);
  const sets = new Map(), rules = [];
  let n = 0;
  for (const [el, decls] of plan) {
    const body = decls.filter(d => d[1] && !/[{}<>]/.test(d[1]))
                      .map(d => d[0] + ':' + d[1] + '!important').join(';');
    if (!body) continue;
    let tok = sets.get(body);
    if (!tok) {
      tok = kind + seq + '-' + sets.size;
      sets.set(body, tok);
      rules.push('[data-cb~="' + tok + '"]' + SPEC + '{' + body + '}');
    }
    const cur = el.getAttribute('data-cb');
    el.setAttribute('data-cb', cur ? cur + ' ' + tok : tok);
    n++;
  }
  if (!rules.length) return 0;
  const st = document.createElement('style');
  st.setAttribute('data-clone-bake', kind);
  st.textContent = '@media all{\n' + rules.join('\n') + '\n}';
  (document.head || document.documentElement).appendChild(st);
  return n;
}"""

# A2 字体度量固化 v2(逐元素差分继承): 旧版取每类标签"第一个元素"的
# computed font/color/background 以 `tag{...!important}` 套到全部同类元素
# (第一个 span 是 12px 角标 → 全页 span 变 12px), 真实改坏页面。
# v2: 仅对可继承的文本度量属性, 在"与父元素 computed 值不同"的元素上
# 以内联 !important 固化自身值; 与父相同者靠继承自然得到同值(根元素全量固化)。
# 继承属性下该差分固化是精确的, 且产物体积只随"样式变化点"增长。
FONT_BAKE_JS = r"""
() => {
  try {
    const PROPS = ['font-family', 'font-size', 'font-weight', 'font-style',
                   'line-height', 'letter-spacing', 'color'];
    const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG', 'svg',
                          'PATH', 'path', 'IFRAME', 'CANVAS', 'VIDEO', 'IMG', 'BR', 'HEAD']);
    const MAX = 8000;
    const cache = new Map();
    const comp = (el) => {
      let v = cache.get(el);
      if (!v) {
        const cs = getComputedStyle(el);
        v = PROPS.map(p => cs.getPropertyValue(p));
        cache.set(el, v);
      }
      return v;
    };
    const root = document.body;
    if (!root) return 0;
    // 浏览器默认字体(没写 font-family 时的计算值, 如 "Times New Roman")不能烘焙成显式族名:
    // 默认族是按文字脚本/lang 分派的(zh 页的数字/拉丁实际走中文默认字体), 写成显式
    // "Times New Roman" 后数字与英文全变衬线(aliyun 页脚)
    let defFam = null;
    try {
      const pr = document.createElement('span');
      pr.style.setProperty('font-family', 'initial', 'important');
      root.appendChild(pr);
      defFam = getComputedStyle(pr).fontFamily;
      pr.remove();
    } catch (e) {}
    // 先全量读取(写入前), 避免写内联样式触发的重算影响后续读取
    const els = [root];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, {
      acceptNode: (n) => SKIP.has(n.tagName) || n.closest('svg')
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT
    });
    while (walker.nextNode() && els.length < MAX) els.push(walker.currentNode);
    const plan = [];
    for (const el of els) {
      const mine = comp(el);
      const parent = el === root ? null : el.parentElement;
      const pv = parent ? comp(parent) : null;
      const diff = [];
      for (let i = 0; i < PROPS.length; i++) {
        if (i === 0 && mine[0] === defFam) continue;
        if (!pv || pv[i] !== mine[i]) diff.push([PROPS[i], mine[i]]);
      }
      if (diff.length) plan.push([el, diff]);
    }
    return __SCOPED_BAKE__('f', plan);
  } catch (e) { return 0; }
}
""".replace("__SCOPED_BAKE__", "(" + SCOPED_BAKE_FN + ")")


REMOVE_SCRIPTS_JS = r"""
() => {
  let n = 0;
  document.querySelectorAll('script').forEach(s => {
    const t = (s.type || '').toLowerCase();
    if (t.includes('json')) return;
    s.remove();
    n++;
  });
  return n;
}
"""

RESPONSIVE_CSS = """<style data-clone-preview="responsive">
/* 移动端适配兜底: 只对原站无 viewport meta 的定宽老站注入(桌面不受影响)。
   1) 视口按设备宽渲染(viewport meta 已注入);
   2) 固定宽度老站可横向平移查看而不破版(只设在 html 上: 设在 body 上会让 body
      变成滚动容器, 文档高度塌成一屏)。 */
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
@media screen and (max-width: 768px) {
  html { overflow-x: auto !important; }
  img, video { max-width: 100% !important; }
  img { height: auto; }
  iframe { max-width: 100% !important; }  /* 不动 height: 登录 iframe 依赖定高 */
  input, select, textarea { font-size: 16px !important; }
  table { max-width: 100%; }
}
</style>"""


# 多视口内联样式采集: 站点脚本按视口写的内联样式(jQuery 宽屏才写 float:right、React
# useBreakpoint 渲染出的 font-size:38px、按窗口宽算的尺寸)只在采集视口下序列化, 换视口
# 后不再有脚本去改(runoob 手机视口整列下移、ant.design 手机视口标题仍是桌面字号)。
# 采集期在基准截图前把活体页依次缩到平板/手机宽再还原, 记录每个元素各视口下的内联样式:
# - 与视口宽成正比的 px 值 → 改写成 vw(连续依赖);
# - 其余在视口间取值不同(且还原后回到原值)的属性 → 从内联移到按视口分段的样式表
#   (离散依赖)。分段边界由落盘时按样式表断点换掉占位(scope_viewport_css)。
CV_RECORD_JS = r"""
(tag) => {
  const m = window.__cvRec || (window.__cvRec = {els: [], snaps: {}});
  if (tag === 'd') {
    m.els = Array.from(document.body ? document.body.querySelectorAll('*') : []).slice(0, 20000);
    m.paths = m.els.map(__CV_PATH__);
  }
  m.snaps[tag] = m.els.map(e => e.getAttribute('style') || '');
  // 脚本按视口切换的 class(React useMediaQuery / matchMedia 回调换 class): 样式表按 class 生效,
  // 换视口后克隆里没有脚本再去换, 一并记录
  m.cls = m.cls || {};
  m.cls[tag] = m.els.map(e => e.getAttribute('class') || '');
  // 脚本按视口写的尺寸属性(svg/img/canvas 的 width/height 属性, 如按容器宽算出的 logo 尺寸)
  // 不在 style 里, 记成等价 CSS 一并参与分视口比较(CSS 宽高覆盖展示属性)
  m.attrs = m.attrs || {};
  m.attrs[tag] = m.els.map(__CV_ATTR__);
  // 脚本按视口写的非 class 属性(data-*/aria-* 被样式表属性选择器引用, 如 [data-media-position=...];
  // svg 的 viewBox 按容器宽重算): 一并记录, 落盘前按视口分段换(见 CV_EMIT_JS)
  m.xattr = m.xattr || {};
  m.xattr[tag] = m.els.map(__CV_XATTR__);
  m.widths = m.widths || {};
  m.widths[tag] = window.innerWidth;
  return m.els.length;
}
"""

# 结构路径(tag:同类序号 链): 主页与窄屏重载页按它对齐元素
CV_PATH_FN = r"""(e) => {
  const p = [];
  while (e && e.parentElement && e !== document.body) {
    let i = 1, s = e;
    while ((s = s.previousElementSibling)) if (s.tagName === e.tagName) i++;
    p.unshift(e.tagName.toLowerCase() + ':' + i);
    e = e.parentElement;
  }
  return p.join('>');
}"""
CV_ATTR_FN = r"""(e) => {
  if (!(e instanceof SVGSVGElement) && e.tagName !== 'IMG' && e.tagName !== 'CANVAS') return '';
  let s = '';
  for (const a of ['width', 'height']) {
    const v = e.getAttribute(a);
    if (v && /^\d+(\.\d+)?$/.test(v.trim())) s += a + ':' + (+v).toFixed(2).replace(/\.?0+$/, '') + 'px;';
  }
  return s;
}"""
# 可随视口换的非 class 属性快照: 名=值 JSON(按名排序), 无则空串。引擎自加属性不计; 值过长/含尖括号引号的不计
CV_XATTR_FN = r"""(e) => {
  const o = {};
  let n = 0;
  for (const a of e.attributes) {
    const k = a.name;
    if (!(/^(data|aria)-/.test(k) || (k === 'viewBox' && e instanceof SVGSVGElement))) continue;
    if (/^data-(cv|cb|cssom|clone)/.test(k) || a.value.length > 80 || /[<>"]/.test(a.value)) continue;
    o[k] = a.value; n++;
  }
  if (!n) return '';
  const r = {};
  for (const k of Object.keys(o).sort()) r[k] = o[k];
  return JSON.stringify(r);
}"""
CV_RECORD_JS = (CV_RECORD_JS.replace("__CV_PATH__", CV_PATH_FN).replace("__CV_ATTR__", CV_ATTR_FN)
                .replace("__CV_XATTR__", CV_XATTR_FN))

# 窄屏重载页的内联样式表: {路径: [标签, class, style]}
CV_PATHS_JS = r"""() => {
  // 子元素结构签名不计样式/脚本类节点: 引擎就地插的 CSSOM 副本(<style data-cssom>)会让桌面页
  // 与窄屏首载页同一容器的子元素序列不同, 被误判为"随视口换结构", 整页并列插入窄屏版(aliyun 首页 ×3)
  const NONVIS = new Set(['STYLE', 'LINK', 'SCRIPT', 'NOSCRIPT', 'META', 'TEMPLATE']);
  const out = {}, pathOf = """ + CV_PATH_FN + r""";
  const els = Array.from(document.body ? document.body.querySelectorAll('*') : []).slice(0, 20000);
  for (const e of els) out[pathOf(e)] = [e.tagName, String(e.className && e.className.baseVal !== undefined ? e.className.baseVal : e.className || ''), e.getAttribute('style') || '',
    Array.from(e.children, c => c.tagName).filter(t => !NONVIS.has(t)).join(',').slice(0, 400), (""" + CV_ATTR_FN + r""")(e),
    (""" + CV_XATTR_FN + r""")(e)];
  return out;
}"""

# ---- 窄屏 DOM 变体 ----
# React/Vue 站按断点渲染不同的 DOM(ant.design 手机宽新闻卡片换成单卡轮播): 样式怎么
# 改都救不回结构。窄屏首载页与主页按结构路径对齐, 子元素结构不同的最上层子树把窄屏版
# 也序列化进克隆, 两版各打 data-cv-vp 视口令牌, 由分视口样式表只显示当前视口那一版。
ALT_EXTRACT_JS = r"""(paths) => {
  const resolve = (path) => {
    let e = document.body;
    for (const seg of path ? path.split('>') : []) {
      const [tag, n] = seg.split(':');
      let k = 0, hit = null;
      for (const c of e.children) if (c.tagName.toLowerCase() === tag && ++k === +n) { hit = c; break; }
      if (!hit) return null;
      e = hit;
    }
    return e;
  };
  const out = {};
  for (const p of paths) {
    const el = resolve(p);
    if (!el) continue;
    const c = el.cloneNode(true);
    // cloneNode 不带 canvas 位图: 按原节点逐个固化成 <img>(与 DEEP_FREEZE 第 5 步同口径), 否则窄屏版
    // 的图表/插画整块空白
    const cvs = el.querySelectorAll('canvas'), cvc = c.querySelectorAll('canvas');
    cvs.forEach((cv, k) => {
      try {
        const data = cv.toDataURL('image/png'), dst = cvc[k];
        if (!dst || !data || data.length <= 128) return;
        dst.replaceWith((__CANVAS_IMG__)(cv, data));
      } catch (e) {}
    });
    c.querySelectorAll('script,noscript').forEach(x => x.remove());
    c.querySelectorAll('[data-src],[data-original],[data-lazy-src]').forEach(x => {
      const v = x.getAttribute('data-src') || x.getAttribute('data-original') || x.getAttribute('data-lazy-src');
      if (v && /^(IMG|SOURCE|IFRAME|VIDEO)$/.test(x.tagName) && !x.getAttribute('src')) x.setAttribute('src', v);
    });
    c.querySelectorAll('img[loading]').forEach(x => x.removeAttribute('loading'));
    // 窄屏页没滚动过: 滚动入场库的初始隐藏态与主页 DEEP_FREEZE 同口径显形
    c.querySelectorAll('[data-aos], .wow, [data-scroll], [data-sr-id], [data-reveal], [data-animate]').forEach(x => {
      x.style.setProperty('opacity', '1', 'important');
      x.style.setProperty('visibility', 'visible', 'important');
      x.style.setProperty('transform', 'none', 'important');
    });
    // 相对地址按窄屏页绝对化(插入主页后基准相同, 但防 <base> 差异)
    c.querySelectorAll('[src],[href],[srcset],[poster]').forEach(x => {
      for (const a of ['src', 'href', 'poster']) {
        const v = x.getAttribute(a);
        if (v && !/^(data:|blob:|#|javascript:|mailto:|tel:)/i.test(v)) { try { x.setAttribute(a, new URL(v, document.baseURI).href); } catch (e) {} }
      }
    });
    out[p] = [c.outerHTML, (el.innerText || '').trim().length, el.getElementsByTagName('*').length];
  }
  return out;
}""".replace("__CANVAS_IMG__", CANVAS_IMG_FN)

# 主页同路径子树的体量(文本长度/元素数), 判断窄屏版是"换了结构"还是"还没加载完"
SUBTREE_SIZE_JS = r"""(paths) => {
  const resolve = (path) => {
    let e = document.body;
    for (const seg of path ? path.split('>') : []) {
      const [tag, n] = seg.split(':');
      let k = 0, hit = null;
      for (const c of e.children) if (c.tagName.toLowerCase() === tag && ++k === +n) { hit = c; break; }
      if (!hit) return null;
      e = hit;
    }
    return e;
  };
  const out = {};
  for (const p of paths) { const el = resolve(p); if (el) out[p] = [(el.innerText || '').trim().length, el.getElementsByTagName('*').length]; }
  return out;
}"""

# 运行时样式文本(<style> 与文档级构造样式表): 窄屏版组件的 CSS-in-JS 样式只在窄屏页里有
STYLE_TEXTS_JS = r"""() => {
  const out = [];
  const txt = (sheet) => { try { let t = ''; for (const r of sheet.cssRules) t += r.cssText + '\n'; return t; } catch (e) { return ''; } };
  for (const st of document.querySelectorAll('style')) {
    if (st.hasAttribute('data-clone-bake') || st.hasAttribute('data-clone-vp') || st.hasAttribute('data-cssom')) continue;
    const t = st.sheet ? txt(st.sheet) : (st.textContent || '');
    if (t.trim()) out.push(t);
  }
  try { for (const sh of document.adoptedStyleSheets || []) { const t = txt(sh); if (t.trim()) out.push(t); } } catch (e) {}
  return out;
}"""

ALT_INJECT_JS = r"""({items, css}) => {
  const resolve = (path) => {
    let e = document.body;
    for (const seg of path ? path.split('>') : []) {
      const [tag, n] = seg.split(':');
      let k = 0, hit = null;
      for (const c of e.children) if (c.tagName.toLowerCase() === tag && ++k === +n) { hit = c; break; }
      if (!hit) return null;
      e = hit;
    }
    return e;
  };
  // 先全部解析出主页元素再插入(插入会改变后续兄弟的同类序号)
  const plan = items.map(it => [resolve(it.path), it]).filter(x => x[0]);
  let n = 0;
  for (const [el, it] of plan) {
    el.setAttribute('data-cv-vp', it.own);
    let anchor = el;
    for (const alt of it.alts) {
      const t = document.createElement('template');
      t.innerHTML = alt.html;
      const node = t.content.firstElementChild;
      if (!node) continue;
      node.setAttribute('data-cv-vp', alt.bands);
      anchor.after(node);
      anchor = node;
      n++;
    }
  }
  if (n) {
    const st = document.createElement('style');
    st.setAttribute('data-clone-vp', 'alt');
    const hide = (b) => '[data-cv-vp]:not([data-cv-vp~="' + b + '"]){display:none!important}';
    st.textContent = '@media all/*cv-d*/{' + hide('d') + '}\n'
      + '@media (min-width: 99999px)/*cv-t*/{' + hide('t') + '}\n'
      + '@media (min-width: 99999px)/*cv-m*/{' + hide('m') + '}\n'
      + (css ? '@media (min-width: 99999px)/*cv-x*/{\n' + css + '\n}' : '');
    (document.head || document.documentElement).appendChild(st);
  }
  return n;
}"""

CV_QUIET_JS = r"""
(ms) => new Promise(res => {
  let last = performance.now();
  const ob = new MutationObserver(() => { last = performance.now(); });
  try { ob.observe(document.body, {attributes: true, attributeFilter: ['style', 'class'], subtree: true, childList: true}); } catch (e) {}
  const t0 = performance.now();
  const tick = () => {
    const now = performance.now();
    if (now - last >= ms || now - t0 > 1500) { ob.disconnect(); res(Math.round(now - t0)); }
    else setTimeout(tick, 50);
  };
  requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(tick, 50)));
})
"""

CV_EMIT_JS = r"""
(loads) => {
  const m = window.__cvRec;
  const out = {vw: 0, banded: 0, els: 0, reload: 0};
  if (!m || !m.snaps.d || !m.snaps.b) return out;
  // 只在页面加载时按宽度写一次的内联样式(resize 不再改它): 以窄屏重载页同路径元素的取值
  // 补进对应视口快照。只认几何类属性 —— 重载页没滚动过, 滚动入场的 opacity/transform 等
  // 初始态不能当成"该视口的取值"
  const GEOM = /^(float|clear|width|height|min-width|max-width|min-height|max-height|margin-|padding-|top|right|bottom|left|position|font-size|line-height|letter-spacing|flex-|order|grid-|text-align|columns?)/;
  loads = loads || {};
  const SKIP = /^(transition|animation|will-change|cursor|pointer-events|--)/;
  const dummy = document.createElement('div').style;
  const parse = s => {
    const o = {};
    if (!s) return o;
    dummy.cssText = s;
    for (let i = 0; i < dummy.length; i++) { const p = dummy[i]; o[p] = [dummy.getPropertyValue(p), dummy.getPropertyPriority(p)]; }
    return o;
  };
  const W = m.widths, SPEC = ':not(#\\#):not(#\\#):not(#\\#)';
  const bands = {d: [], t: [], m: []};
  const px = v => { const r = /^(-?[\d.]+)px$/.exec(v || ''); return r ? parseFloat(r[1]) : null; };
  const tags = ['t', 'm'].filter(t => m.snaps[t]);
  const attrOf = __CV_ATTR__;
  const S = (t, i) => m.snaps[t][i] + ((m.attrs && m.attrs[t] && m.attrs[t][i]) ? ';' + m.attrs[t][i] : '');
  // 分视口 class: 缩放快照里该视口 class 与桌面不同且还原后回到桌面值 → 该视口用缩放快照的 class;
  // 缩放没变而窄屏首载页同路径同标签元素 class 不同 → 只在加载时按宽度决定 class, 用首载值
  const C = m.cls || {}, swaps = [];
  const STATE = /(^|[-_])(active|current|cur|hover|focus|open|opened|show|shown|visible|selected|prev|next|in-view|inview|exposure|loaded|scrolled|playing|dismissed|skipped|expanded|collapsed)([-_]|$)/i;
  if (C.d && C.b) m.els.forEach((el, i) => {
    if (!el.isConnected || el.closest('svg')) return;
    const cd = C.d[i];
    if (C.b[i] !== cd || (el.getAttribute('class') || '') !== cd) return;
    const alt = {};
    tags.forEach(t => {
      // 两个独立来源一致才算「随视口」: 缩放快照与窄屏首载页同路径元素的 class 相同且都不同于桌面。
      // 只有一方变化的多是时间/交互状态(轮播 active/current、hover、曝光埋点、展开态), 不归视口
      const v = C[t] ? C[t][i] : cd;
      const L = m.paths && loads[t] ? loads[t][m.paths[i]] : null;
      if (v === cd || !L || L[0] !== el.tagName || L[1] !== v || /[<>"]/.test(v)) return;
      const a = new Set(cd.split(/\s+/)), b = new Set(v.split(/\s+/));
      const delta = [...a].filter(x => !b.has(x)).concat([...b].filter(x => !a.has(x)));
      if (delta.some(x => STATE.test(x))) return;
      alt[t] = v;
    });
    if (Object.keys(alt).length) swaps.push([el, alt]);
  });
  swaps.slice(0, 4000).forEach(([el, alt]) => {
    for (const t of Object.keys(alt)) el.setAttribute('data-cv-cls-' + t, alt[t]);
    el.setAttribute('data-cv-cls-d', el.getAttribute('class') || '');
    out.cls = (out.cls || 0) + 1;
  });
  // 分视口非 class 属性: 与 class 同口径要两路证据一致(缩放快照与窄屏首载同路径元素取值相同、都不同于
  // 桌面、还原后回到桌面值); data-*/aria-* 只换被样式表属性选择器引用的名(未被引用的换了也不影响渲染,
  // 多是埋点/状态), svg viewBox 恒参与。名字像交互状态的不换
  const X = m.xattr || {}, xswaps = [];
  if (X.d && X.b && tags.length) {
    const refd = new Set();
    const walk = rules => {
      for (const r of rules || []) {
        if (r.selectorText) for (const g of r.selectorText.matchAll(/\[\s*([\w:-]+)/g)) refd.add(g[1].toLowerCase());
        if (r.cssRules) walk(r.cssRules);
      }
    };
    for (const sh of document.styleSheets) { try { walk(sh.cssRules); } catch (e) {} }
    const XSTATE = /(expanded|hidden|selected|current|pressed|checked|state|active|open|index|visible|busy|disabled|live|scroll|show)/i;
    const J = s => { try { return s ? JSON.parse(s) : {}; } catch (e) { return null; } };
    m.els.forEach((el, i) => {
      const xd = X.d[i];
      if (!el.isConnected || X.b[i] !== xd || (__CV_XATTR__)(el) !== xd) return;
      const D = J(xd);
      if (!D) return;
      const alt = {};
      tags.forEach(t => {
        const v = X[t] ? X[t][i] : xd;
        const L = m.paths && loads[t] ? loads[t][m.paths[i]] : null;
        if (v === xd || !L || L[0] !== el.tagName || (L[5] || '') !== v) return;
        const V = J(v);
        if (!V) return;
        const ch = {};
        let ok = true, n = 0;
        for (const k of new Set(Object.keys(D).concat(Object.keys(V)))) {
          if (D[k] === V[k]) continue;
          if (k !== 'viewBox' && !refd.has(k)) continue;              // 样式表没引用: 不影响渲染, 不换
          if (XSTATE.test(k)) { ok = false; break; }
          ch[k] = k in V ? V[k] : null; n++;
        }
        if (ok && n) alt[t] = ch;
      });
      if (Object.keys(alt).length) xswaps.push([el, alt, D]);
    });
  }
  xswaps.slice(0, 4000).forEach(([el, alt, D]) => {
    const base = {};
    for (const t of Object.keys(alt)) {
      el.setAttribute('data-cv-xa-' + t, JSON.stringify(alt[t]));
      for (const k of Object.keys(alt[t])) base[k] = k in D ? D[k] : null;
    }
    el.setAttribute('data-cv-xa-d', JSON.stringify(base));
    out.xattr = (out.xattr || 0) + 1;
  });
  m.els.forEach((el, i) => {
    const sdRaw = m.snaps.d[i], sd = S('d', i);
    if (!el.isConnected) return;
    const full = L => L[2] + (L[4] ? ';' + L[4] : '');   // 窄屏首载: style + 尺寸属性(加载时按宽度只写一次的 canvas/svg 尺寸)
    const reloadDiff = m.paths && tags.some(t => loads[t] && loads[t][m.paths[i]] && full(loads[t][m.paths[i]]) !== sd);
    if (!reloadDiff && tags.every(t => S(t, i) === sd)) return;
    if (S('b', i) !== sd && tags.every(t => S(t, i) === S('b', i))) return;
    const D = parse(sd), B = parse(S('b', i)), V = {}, Draw = parse(sdRaw);
    tags.forEach(t => { V[t] = parse(S(t, i)); });
    tags.forEach(t => {
      const L = loads[t] && m.paths ? loads[t][m.paths[i]] : null;
      if (!L || L[0] !== el.tagName) return;
      const cls = String(el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className || '');
      if (L[1] !== cls || full(L) === sd) return;
      const LV = parse(full(L));
      const keys = new Set(Object.keys(D).concat(Object.keys(LV)));
      for (const p of keys) {
        if (!GEOM.test(p)) continue;
        if (!(p in Draw) && !(p in LV)) continue;                         // 来自尺寸属性: 重载页只给了 style, 不比
        if (JSON.stringify(V[t][p]) !== JSON.stringify(D[p])) continue;   // 缩放已采到
        if (JSON.stringify(LV[p]) === JSON.stringify(D[p])) continue;
        if (LV[p]) V[t][p] = LV[p]; else delete V[t][p];
        out.reload++;
      }
    });
    const cur = parse((el.getAttribute('style') || '') + ';' + attrOf(el));
    const props = new Set(Object.keys(D));
    tags.forEach(t => Object.keys(V[t]).forEach(p => props.add(p)));
    const rule = {d: [], t: [], m: []};
    let touched = false;
    for (const p of props) {
      if (SKIP.test(p)) continue;
      const dv = D[p], bv = B[p];
      if (JSON.stringify(dv) !== JSON.stringify(bv)) continue;          // 还原后没回到原值: 状态性变化, 不归视口
      if (tags.every(t => JSON.stringify(V[t][p]) === JSON.stringify(dv))) continue;
      if (JSON.stringify(cur[p]) !== JSON.stringify(dv)) continue;      // 引擎后续已改写(流式化等), 以它为准
      const nums = [['d', dv]].concat(tags.map(t => [t, V[t][p]])).map(([t, v]) => [t, v ? px(v[0]) : null]);
      if (nums.every(([t, n]) => n !== null && n !== 0)) {
        const r = nums.map(([t, n]) => n / W[t]);
        if (r.every(x => Math.abs(x - r[0]) <= Math.abs(r[0]) * 0.015)) {
          el.style.setProperty(p, (r[0] * 100).toFixed(4) + 'vw', dv[1]);
          out.vw++; touched = true;
          continue;
        }
      }
      if (/[{}<>]/.test(JSON.stringify([dv, V.t && V.t[p], V.m && V.m[p]]))) continue;
      el.style.removeProperty(p);
      if (dv) rule.d.push(p + ':' + dv[0] + (dv[1] ? '!important' : ''));
      tags.forEach(t => { const v = V[t][p]; if (v) rule[t].push(p + ':' + v[0] + (v[1] ? '!important' : '')); });
      out.banded++; touched = true;
    }
    if (!touched) return;
    out.els++;
    if (rule.d.length + rule.t.length + rule.m.length) {
      el.setAttribute('data-cv', String(i));
      for (const k of ['d', 't', 'm']) if (rule[k].length) bands[k].push('[data-cv="' + i + '"]' + SPEC + '{' + rule[k].join(';') + '}');
    }
  });
  if (out.cls || out.xattr) {
    // 当前视口段由分视口样式表里的 --cv-band 告诉运行时(段边界落盘时才确定, 脚本不重复计算断点)
    bands.d.push(':root{--cv-band:d}'); bands.t.push(':root{--cv-band:t}'); bands.m.push(':root{--cv-band:m}');
    const sc = document.createElement('script');
    sc.setAttribute('data-clone-preview', 'cv-cls');
    sc.textContent = CV_CLS_RUNTIME;
    (document.head || document.documentElement).appendChild(sc);
  }
  if (bands.d.length + bands.t.length + bands.m.length) {
    const st = document.createElement('style');
    st.setAttribute('data-clone-vp', '');
    // 活体页(及 MHTML 保真轨)只生效采集视口段; 平板/手机段的占位条件永不成立, 落盘时换成真实区间
    st.textContent = '@media all/*cv-d*/{\n' + bands.d.join('\n') + '\n}\n'
      + '@media (min-width: 99999px)/*cv-t*/{\n' + bands.t.join('\n') + '\n}\n'
      + '@media (min-width: 99999px)/*cv-m*/{\n' + bands.m.join('\n') + '\n}';
    (document.head || document.documentElement).appendChild(st);
  }
  window.__cvRec = null;
  return out;
}
""".replace("__CV_ATTR__", CV_ATTR_FN).replace("__CV_XATTR__", CV_XATTR_FN)

# 分视口 class 运行时(随 CV_EMIT_JS 写入页面): 读 --cv-band 决定当前段, 把 data-cv-cls-<段> 换成 class;
# 段没变不动 DOM。活体采集时段恒为 d(桌面), 不改变采集结果。
CV_CLS_RUNTIME = r"""(function(){var cur=null;function apply(){try{var b=(getComputedStyle(document.documentElement).getPropertyValue('--cv-band')||'').trim()||'d';if(b===cur)return;cur=b;var es=document.querySelectorAll('[data-cv-cls-d]');for(var i=0;i<es.length;i++){var e=es[i],v=e.getAttribute('data-cv-cls-'+b);if(v===null)v=e.getAttribute('data-cv-cls-d');if(e.getAttribute('class')!==v)e.setAttribute('class',v);}es=document.querySelectorAll('[data-cv-xa-d]');for(i=0;i<es.length;i++){e=es[i];var o=JSON.parse(e.getAttribute('data-cv-xa-d')),bv=e.getAttribute('data-cv-xa-'+b);if(bv!==null){var q=JSON.parse(bv);for(var k in q)o[k]=q[k];}for(k in o){if(o[k]===null)e.removeAttribute(k);else if(e.getAttribute(k)!==o[k])e.setAttribute(k,o[k]);}}}catch(x){}}if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',apply);else apply();window.addEventListener('resize',apply);window.addEventListener('load',apply);})();"""
CV_EMIT_JS = CV_EMIT_JS.replace("CV_CLS_RUNTIME", json.dumps(CV_CLS_RUNTIME))


# 内联尺寸流式化(序列化前): 轮播/全宽块的脚本常把"容器当前宽度"写成内联 px
# (swiper/slick 的 slide width:1920px、轨道 width:N×1920px、translate3d(-1920px)),
# 手机视口下仍是 1920px 宽(huawei 首屏轮播 2865px 高)。只改与容器宽度成整数倍关系
# 的值: 采集视口下计算结果逐像素不变, 换视口时随容器伸缩。
INLINE_FLUID_JS = r"""
() => {
  const out = {width: 0, track: 0, shift: 0};
  const near = (a, b) => Math.abs(a - b) <= 1;
  const inner = el => {
    const cs = getComputedStyle(el);
    return el.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
  };
  const pxOf = v => { const m = /^(-?[\d.]+)px$/.exec((v || '').trim()); return m ? parseFloat(m[1]) : null; };
  const cands = Array.from(document.querySelectorAll('[style*="width"],[style*="translate"]')).slice(0, 5000);
  const plan = [];
  for (const el of cands) {
    const parent = el.parentElement;
    if (!parent || el.closest('svg')) continue;
    const pw = inner(parent);
    if (pw < 150) continue;
    const w = pxOf(el.style.width);
    const kids = Array.from(el.children);
    if (w !== null && w >= 150) {
      const k = Math.round(w / pw);
      const slides = kids.filter(c => near(pxOf(c.style.width) || -1, pw));
      if (k >= 2 && k <= 60 && near(w, k * pw) && slides.length >= 2) {
        // 轨道: 宽 = k × 容器, 子项各宽 1 个容器
        plan.push([el, 'width', (k * 100) + '%']);
        for (const c of slides) plan.push([c, 'width', (100 / k).toFixed(4) + '%']);
        out.track++;
      } else if (near(w, pw)) {
        // 父容器宽若由本元素撑出(父是 inline-block / 自动宽的 flex 项 / 浮动 / 绝对定位收缩包裹):
        // 改 100% 成了循环百分比, 宽度退回由内容决定(ant.design 二维码 160px 容器被 img 撑到 186)。
        // 临时把本元素缩到 1px 看父容器跟不跟着缩, 跟着缩的不改
        const st = el.getAttribute('style');
        el.style.setProperty('width', '1px', 'important');
        const pw1 = inner(parent);
        el.setAttribute('style', st);
        if (near(pw1, pw)) {
          plan.push([el, 'width', '100%']);
          out.width++;
        }
      }
    }
    // 位移: translate(-m × 单页宽) → 按自身宽度的百分比(单页宽 = 容器宽或首个子项宽)
    const tf = el.style.transform || '';
    const tm = /translate(3d|X)?\(\s*(-?[\d.]+)px/.exec(tf);
    if (tm && parseFloat(tm[2]) !== 0) {
      const dx = parseFloat(tm[2]);
      const own = el.getBoundingClientRect().width;
      const unit = kids.length && kids[0].getBoundingClientRect().width > 100
        ? kids[0].getBoundingClientRect().width : pw;
      const m = Math.round(Math.abs(dx) / unit);
      if (own > 100 && m >= 1 && Math.abs(Math.abs(dx) - m * unit) <= m) {
        const pct = (dx / own * 100).toFixed(4) + '%';
        plan.push([el, 'transform', tf.replace(tm[0], tm[0].replace(/-?[\d.]+px$/, pct))]);
        out.shift++;
      }
    }
  }
  for (const [el, p, v] of plan) {
    const pri = el.style.getPropertyPriority(p);
    el.style.setProperty(p, v, pri);
  }
  return out;
}
"""

# 原文档 doctype 原样序列化: 渲染模式(标准 / 有限怪异 / 怪异)由它决定。XHTML 1.0 Transitional
# 等带 system id 的老 doctype 走有限怪异模式(只含行内子元素的块不计自身行高 strut), 写死成
# <!DOCTYPE html> 会让这类块变高、版面逐段下推(cgnpc 手机版栏目标题 19.8→26.4px); 没有 doctype
# 的页面是怪异模式, 也不能补。
DOCTYPE_FN = r"""(d) => {
  const t = d && d.doctype;
  if (!t) return '';
  const q = v => '"' + String(v).replace(/"/g, '') + '"';
  let s = '<!DOCTYPE ' + (t.name || 'html');
  if (t.publicId) s += ' PUBLIC ' + q(t.publicId);
  if (t.systemId) s += (t.publicId ? ' ' : ' SYSTEM ') + q(t.systemId);
  return s + '>\n';
}"""

OUTER_HTML_JS = r"""
() => ((""" + DOCTYPE_FN + r""")(document) + document.documentElement.outerHTML)
"""

# Shadow DOM 声明式序列化: outerHTML 不含影子树(Web Components/Lit/Stencil 组件内容整块丢失)。
# 有开放影子根时改用 getHTML({serializableShadowRoots, shadowRoots}) 输出
# <template shadowrootmode="open"> —— HTML 解析器原生还原, 克隆页无需组件脚本。
# 影子根的 adoptedStyleSheets 不会被序列化, 先物化为根内 <style data-clone-adopted>。
# 无影子根时返回 html=null, 调用方沿用 OUTER_HTML_JS(行为不变)。
SHADOW_SERIALIZE_JS = r"""
() => {
  const roots = [];
  const walk = (root) => {
    const els = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of els) {
      const sr = el.shadowRoot;
      if (sr) { roots.push(sr); walk(sr); }
    }
  };
  try { walk(document); } catch (e) {}
  const de = document.documentElement;
  if (!roots.length || typeof de.getHTML !== 'function') return {html: null, roots: roots.length, adopted: 0};
  let adopted = 0;
  for (const r of roots) {
    try {
      const sheets = r.adoptedStyleSheets || [];
      if (!sheets.length || r.querySelector(':scope > style[data-clone-adopted]')) continue;
      const css = sheets.map(sh => {
        try { return Array.from(sh.cssRules).map(x => x.cssText).join('\n'); } catch (e) { return ''; }
      }).join('\n');
      const st = document.createElement('style');
      st.setAttribute('data-clone-adopted', '');
      st.textContent = css;
      r.prepend(st);
      adopted++;
    } catch (e) {}
  }
  const esc = v => v.replace(/&/g, '&amp;').replace(/"/g, '&quot;');
  const attrs = Array.from(de.attributes).map(a => ' ' + a.name + '="' + esc(a.value) + '"').join('');
  const inner = de.getHTML({serializableShadowRoots: true, shadowRoots: roots});
  return {html: (__DOCTYPE__)(document) + '<html' + attrs + '>' + inner + '</html>', roots: roots.length, adopted: adopted};
}
""".replace("__DOCTYPE__", DOCTYPE_FN)

# 版面块图: 可见块级元素的结构路径(tag:nth-of-type 链) → 文档坐标矩形。
# 原站侧在纯净基准截图后采集, 克隆侧在克隆截图后采集; 两侧按路径对齐,
# 找出高度不一致的"最深"块, 生成确定性尺寸覆盖(布局校准, 消除累积错位)。
LAYOUT_MAP_JS = r"""
(maxN) => {
  const out = [];
  const HTML_NS = 'http://www.w3.org/1999/xhtml';
  const SKIP = {SCRIPT: 1, STYLE: 1, LINK: 1, META: 1, NOSCRIPT: 1, TEMPLATE: 1, BR: 1};
  const keyOf = (el) => {
    const parts = [];
    let e = el;
    while (e && e !== document.body && e.parentElement) {
      let i = 1;
      for (let s = e.previousElementSibling; s; s = s.previousElementSibling)
        if (s.tagName === e.tagName) i++;
      parts.push(e.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
      e = e.parentElement;
    }
    return e === document.body ? 'body>' + parts.reverse().join('>') : null;
  };
  if (!document.body) return out;
  const sy = window.scrollY || 0, sx = window.scrollX || 0;
  const all = document.body.getElementsByTagName('*');
  for (let n = 0; n < all.length && out.length < maxN; n++) {
    const el = all[n];
    if (el.namespaceURI !== HTML_NS || SKIP[el.tagName]) continue;
    const r = el.getBoundingClientRect();
    if (r.height < 24 || r.width < 60) continue;
    const cs = getComputedStyle(el);
    if (cs.position === 'fixed' || cs.position === 'sticky' || cs.display === 'inline') continue;
    const k = keyOf(el);
    // 第 6 项: content-box 时 min/max-height 不含内边距与边框, 生成规则时要扣掉
    const pb = cs.boxSizing === 'border-box' ? 0 :
      (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0) +
      (parseFloat(cs.borderTopWidth) || 0) + (parseFloat(cs.borderBottomWidth) || 0);
    if (k) out.push([k, Math.round(r.top + sy), Math.round(r.height),
                     Math.round(r.left + sx), Math.round(r.width), Math.round(pb)]);
  }
  return out;
}
"""
BLOB_FETCH_JS = r"""
async (urls) => {
  const out = {};
  for (const u of urls) {
    try {
      const b = await (await fetch(u)).blob();
      if (b.size > 5e6) continue;
      out[u] = await new Promise((res, rej) => {
        const fr = new FileReader();
        fr.onload = () => res(fr.result);
        fr.onerror = rej;
        fr.readAsDataURL(b);
      });
    } catch (e) {}
  }
  return out;
}
"""

# 截图全部失败时的页面形态诊断: 文档尺寸 + 面积最大的 5 个元素(含变换)
SHOT_DIAG_JS = r"""
() => {
  const de = document.documentElement;
  const big = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    const a = r.width * r.height;
    if (a > 4e6) {
      const cs = getComputedStyle(el);
      big.push([a, el.tagName.toLowerCase() + (el.id ? '#' + el.id : ''),
                Math.round(r.width), Math.round(r.height), cs.transform.slice(0, 60),
                cs.position, cs.willChange]);
    }
  }
  big.sort((x, y) => y[0] - x[0]);
  return {ready: document.readyState, href: location.href.slice(0, 80),
          docW: de.scrollWidth, docH: de.scrollHeight, vw: innerWidth, vh: innerHeight,
          dpr: devicePixelRatio, big: big.slice(0, 5).map(b => b.slice(1))};
}
"""

# 原站版面特征探针(一行日志, 远程归因用): 影子根/画布/SVG/渐变与遮罩背景/iframe 计数
PAGE_PROBE_JS = r"""
() => {
  const out = {shadow: 0, canvas: 0, svg: 0, gradientBg: 0, maskImg: 0, paint: 0, iframe: 0, fixed: 0, samples: []};
  const visit = (root) => {
    for (const el of root.querySelectorAll('*')) {
      const tag = el.tagName;
      if (tag === 'CANVAS') out.canvas++;
      else if (tag === 'svg' || tag === 'SVG') out.svg++;
      else if (tag === 'IFRAME') out.iframe++;
      try {
        const cs = getComputedStyle(el);
        if (cs.backgroundImage && cs.backgroundImage.indexOf('gradient') >= 0) out.gradientBg++;
        const m = cs.maskImage || cs.webkitMaskImage;
        if (m && m !== 'none') out.maskImg++;
        if ((cs.backgroundImage + (m || '') + cs.borderImageSource).indexOf('paint(') >= 0) out.paint++;
        if (cs.position === 'fixed' || cs.position === 'sticky') out.fixed++;
        // 大面积渐变/遮罩样本(含伪元素): 归因"背景点阵/光晕"类装饰在克隆侧消失
        if (out.samples.length < 4) {
          const r = el.getBoundingClientRect();
          if (r.width * r.height > 60000) {
            for (const ps of [null, '::before', '::after']) {
              const c = ps ? getComputedStyle(el, ps) : cs;
              if (ps && (!c.content || c.content === 'none')) continue;
              const bg = c.backgroundImage || '';
              const mk = c.maskImage || c.webkitMaskImage || 'none';
              if (bg.indexOf('radial-gradient') >= 0 || (mk !== 'none' && mk.indexOf('gradient') >= 0)) {
                out.samples.push([(el.tagName + '.' + (el.className && el.className.baseVal === undefined ? String(el.className) : '')).slice(0, 48) + (ps || ''),
                  bg.slice(0, 120), c.backgroundSize, mk.slice(0, 60), c.animationName, c.opacity]);
                break;
              }
            }
          }
        }
      } catch (e) {}
      if (el.shadowRoot) { out.shadow++; visit(el.shadowRoot); }
    }
  };
  try { visit(document); } catch (e) {}
  out.docH = Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0);
  return out;
}
"""

# 可见但仍在加载的 <img> 数(已失败的 complete=true 不计: 不会再到)
IMG_PENDING_JS = r"""() => {
  // 只计真正可见、确实在加载的图: 隐藏/透明祖先(轮播非当前页)与视口外 lazy 图不会在截图窗口内到齐
  let n = 0;
  const vh = innerHeight;
  for (const im of document.images) {
    if (im.complete) continue;
    const r = im.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) continue;
    if (im.loading === 'lazy' && (r.bottom < -vh || r.top > 2 * vh)) continue;
    try {
      if (im.checkVisibility && !im.checkVisibility({opacityProperty: true, visibilityProperty: true})) continue;
    } catch (e) {}
    n++;
  }
  return n;
}"""

# 框架运行时探针(源站活体页, 引擎改动 DOM 之前): 运行时挂在 window / 挂载节点上的
# 专有对象, 拆包混淆抹不掉。结果作为 pattern_detector 的第三证据源(只认 RUNTIME_NAMES)
RUNTIME_FP_JS = r"""() => {
  const out = [];
  const w = window;
  const has = (k) => { try { return typeof w[k] !== 'undefined' && w[k] !== null; } catch (e) { return false; } };
  try {
    if (has('__NEXT_DATA__') || has('__next_f')) out.push('Next');
    if (has('__NUXT__') || has('$nuxt') || has('useNuxtApp')) out.push('Nuxt');
    if (has('__remixContext') || has('__reactRouterContext')) out.push('Remix');
    if (has('_$HY')) out.push('Solid');
    if (has('Ember') && w.Ember && w.Ember.VERSION) out.push('Ember');
    if (has('ng') && w.ng && typeof w.ng.getComponent === 'function') out.push('Angular');
    if (document.querySelector('[ng-version]')) out.push('Angular');
    if (document.querySelector('[q\\:container]')) out.push('Qwik');
    if (document.querySelector('astro-island')) out.push('Astro');
    if (has('Shopify') && w.Shopify && (w.Shopify.shop || w.Shopify.theme)) out.push('Shopify');
    if (has('wixBiSession') || has('viewerModel')) out.push('Wix');
    if (has('wp') && w.wp && (w.wp.i18n || w.wp.hooks || w.wp.element)) out.push('WordPress');
    if (has('jQuery') && w.jQuery && w.jQuery.fn && w.jQuery.fn.jquery) out.push('jQuery');
    if (has('webpackJsonp') || Object.keys(w).some(k => k.startsWith('webpackChunk'))) out.push('Webpack');
    if (has('__svelte') || has('__SVELTEKIT_DEV__')) out.push('Svelte');
    // 挂载节点上的实例: React 18/19(__reactContainer$/__reactFiber$)、React<=17(_reactRootContainer)、
    // Vue3(__vue_app__)、Vue2(__vue__); 只看 body 下前几层, 不全树遍历
    const nodes = [document.body].concat(Array.from(document.querySelectorAll('body > *, body > * > *')).slice(0, 300));
    let react = false, vue = false;
    for (const el of nodes) {
      if (!el) continue;
      if (!react) {
        if (el._reactRootContainer) react = true;
        else for (const k in el) { if (k.startsWith('__reactContainer$') || k.startsWith('__reactFiber$')) { react = true; break; } }
      }
      if (!vue && (el.__vue_app__ || el.__vue__)) vue = true;
      if (react && vue) break;
    }
    if (react) out.push('React');
    if (vue) out.push('Vue');
  } catch (e) {}
  return Array.from(new Set(out));
}"""

# 克隆侧懒加载图一律转 eager 并等到齐: 资源全在本地沙盒, 整页截图前没被滚动窗口
# 触发的 loading=lazy 图(页尾赞助商墙/logo 栅格)在截图里是空格, 原站基准却是满的
# 只转基准时刻已到齐的图(采集侧打 data-clone-loaded): 基准里没到的图克隆也不强拉, 两侧同口径
CLONE_IMG_EAGER_JS = r"""() => {
  let n = 0;
  for (const im of document.images) {
    if (im.loading === 'lazy' && im.hasAttribute('data-clone-loaded')) { im.loading = 'eager'; n++; }
  }
  return n;
}"""
# 基准截图前: 文档序前 maxN 张懒加载图转 eager(页尾轮播/栅格没被滚动窗口触发的图);
# 不全转 —— 一次拉上百张会被图床限流成坏图(wix 页尾整片 alt 文字)
BASELINE_EAGER_JS = r"""(maxN) => {
  let n = 0;
  for (const im of document.images) {
    if (n >= maxN) break;
    if (im.loading !== 'lazy' || im.complete) continue;
    const r = im.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) continue;
    im.loading = 'eager'; n++;
  }
  return n;
}"""
# 基准截图后: 给截图时刻已解码完成的图打标, 供克隆侧决定哪些懒加载图该强拉
MARK_LOADED_IMGS_JS = r"""() => {
  let n = 0;
  for (const im of document.images) {
    if (im.complete && im.naturalWidth > 0) { im.setAttribute('data-clone-loaded', '1'); n++; }
  }
  return n;
}"""
CLONE_IMG_DONE_JS = r"""() => {
  for (const im of document.images) {
    if (im.complete) continue;
    const r = im.getBoundingClientRect();
    if (r.width > 1 && r.height > 1) return false;
  }
  return true;
}"""

# 评分渲染专用(交付物不注入): 长周期 setInterval 不触发。基准是时间冻结的快照, 克隆页加载后
# 轮播自动翻页(周期 1.5~6s 的 setInterval, wix 模板墙是 1.5s)会在截图前翻到另一页 —— 与 ANIM_ALIGN_JS
# 对 CSS 动画做的对齐同理。setTimeout 不拦: 一次性的长延时常是内容装配(qwik 页尾
# 延时挂载, 拦掉后整页矮 85~150px), 拦它得不偿失
SCORE_TIMER_GUARD_JS = r"""(() => {
  try {
    const si = window.setInterval;
    window.setInterval = function (fn, ms) {
      if ((+ms || 0) >= 1000) return 0;
      return si.apply(this, arguments);
    };
  } catch (e) {}
})();"""

# 克隆侧视频对帧: 带 data-clone-vt 的视频暂停并跳到基准时刻; 返回需等待 seek 的个数
VIDEO_SEEK_JS = r"""() => {
  let n = 0;
  // 封面就是基准帧(媒体快照取的当前帧/基准截图裁图): 视频本身可播(资源在)时评分页直接
  // 显示封面 —— 本地沙盒不支持 Range, mp4 跳帧常落不到准确位置(svelte 纪录片 98→90)
  for (const v of document.querySelectorAll('video[data-clone-poster]')) {
    try {
      if (v.error || v.readyState < 1) continue;
      v.autoplay = false; v.pause();
      v.removeAttribute('data-clone-vt');
      v.removeAttribute('src');
      v.querySelectorAll('source').forEach(s => s.remove());
      v.load();
    } catch (e) {}
  }
  for (const v of document.querySelectorAll('video[data-clone-vt]')) {
    const t = parseFloat(v.getAttribute('data-clone-vt'));
    if (!(t >= 0)) continue;
    try { v.autoplay = false; v.pause(); v.currentTime = t; n++; } catch (e) {}
  }
  return n;
}"""
VIDEO_SEEK_DONE_JS = r"""() => {
  for (const v of document.querySelectorAll('video[data-clone-vt]')) {
    if (v.error || v.networkState === 3) continue;
    if (v.seeking || v.readyState < 2) return false;
  }
  return true;
}"""

# 视口内可见的 autoplay 视频都已有当前帧(readyState>=2); 出错/无源的不等
VIDEO_FRAME_READY_JS = r"""() => {
  for (const v of document.querySelectorAll('video[autoplay]')) {
    if (v.error || v.readyState >= 2 || v.networkState === 0 || v.networkState === 3) continue;
    const r = v.getBoundingClientRect();
    if (r.width > 1 && r.height > 1 && r.top < innerHeight && r.bottom > 0) return false;
  }
  return true;
}"""

# 时间冻结(基准截图前): 暂停全部 CSS/WAAPI 动画并清空页面定时器、停掉 rAF。
# 不改变当前画面, 只让"截图那一刻"一直保持到 DOM 序列化 —— 否则轮播/跑马灯在
# 截图与序列化之间继续前进, 克隆从另一个时刻起步(nuxt 案例墙错一屏、logo 带错相位)。
TIME_FREEZE_JS = r"""
() => {
  const out = {anims: 0, timers: 0};
  try {
    for (const a of document.getAnimations()) {
      if (a.playState !== 'running') continue;
      // 过渡(轮播翻页/展开收起)是两个稳态之间的一段: 冻在半途基准就是"两页各露一半",
      // 克隆静置在稳态, 整屏错位(mi.com 轮播 99.9→74.5)。过渡直接跳到终点, 循环动画才暂停
      if (typeof CSSTransition !== 'undefined' && a instanceof CSSTransition) {
        try { a.finish(); out.anims++; continue; } catch (e) {}
      }
      // 脚本发起的有限次动画(进视口入场、叠卡展开)同理跳到终点: 克隆里它们重放后停在终态,
      // 基准冻在半途两边就对不上(wix 模板墙); 无限循环的才暂停
      if (!(typeof CSSAnimation !== 'undefined' && a instanceof CSSAnimation)) {
        try {
          const t = a.effect && a.effect.getComputedTiming ? a.effect.getComputedTiming() : null;
          if (t && isFinite(t.endTime)) { a.finish(); out.anims++; continue; }
        } catch (e) {}
      }
      a.pause(); out.anims++;
    }
  } catch (e) {}
  try {
    const maxId = setTimeout(function () {}, 0);
    for (let i = 1; i <= maxId; i++) { clearTimeout(i); clearInterval(i); }
    out.timers = maxId;
  } catch (e) {}
  // 冻结后新挂的长定时器也不触发: 轮播常在补间结束(transitionend)时重挂下一次翻页,
  // 清掉旧定时器挡不住它, 序列化时已翻到下一页(wix 模板墙比基准快一页)。短定时器
  // (引擎自身的 120ms 等待、防抖)照常
  try {
    const st = window.setTimeout;
    window.setInterval = function () { return 0; };
    window.setTimeout = function (fn, ms) { return (+ms || 0) >= 500 ? 0 : st.apply(this, arguments); };
  } catch (e) {}
  // rAF 不在这里停: 定时器清掉后不会再起新一轮翻页, 但已在飞的 JS 补间(rAF 逐帧写
  // transform 的轮播)要让它走完, 否则基准冻在两页之间(wix 模板墙)。见 RAF_FREEZE_JS
  // 视频同样定格: 截图期间不再走帧, 媒体快照记下的进度就是截图里那一帧
  try { document.querySelectorAll('video').forEach(v => { if (!v.paused) v.pause(); }); } catch (e) {}
  return out;
}
"""

# 脚本动画(element.animate 的 WAAPI)烘焙(基准截图后, 时间冻结中): 它们不在样式表里、
# 序列化带不走, 克隆只剩动画开始前的静态版面 —— wix 模板墙用 WAAPI 转 3D 叠卡, 基准
# 停在哪一张克隆都显示初始那张。把被动画的属性按截图时刻的计算值写成内联 !important
WAAPI_BAKE_JS = r"""() => {
  let n = 0;
  const plan = [];
  const skip = new Set(['offset', 'easing', 'composite', 'computedOffset']);
  const kebab = (p) => p === 'cssFloat' ? 'float' : p.replace(/[A-Z]/g, m => '-' + m.toLowerCase());
  try {
    for (const a of document.getAnimations()) {
      if ((typeof CSSAnimation !== 'undefined' && a instanceof CSSAnimation) ||
          (typeof CSSTransition !== 'undefined' && a instanceof CSSTransition)) continue;
      const eff = a.effect, el = eff && eff.target;
      if (!el || !el.style || eff.pseudoElement) continue;
      const props = new Set();
      try { for (const kf of eff.getKeyframes()) for (const k in kf) if (!skip.has(k)) props.add(kebab(k)); } catch (e) {}
      if (!props.size) continue;
      const cs = getComputedStyle(el);
      plan.push([el, Array.from(props, p => [p, cs.getPropertyValue(p)])]);
      n++;
    }
  } catch (e) {}
  try { __SCOPED_BAKE__('w', plan); } catch (e) {}
  return n;
}""".replace("__SCOPED_BAKE__", "(" + SCOPED_BAKE_FN + ")")

RAF_FREEZE_JS = r"""() => { try { window.requestAnimationFrame = function () { return 0; }; } catch (e) {} }"""

# 动画相位固化(基准截图后、DEEP_FREEZE 前): 把每个元素 CSS 动画的当前进度写成
# 负 animation-delay, 克隆页加载即从截图那一刻的相位起播; 并打 data-clone-anim
# 标记, 评分侧据此把相位对齐到同一时刻再截图(见 ANIM_ALIGN_JS)。
ANIM_PHASE_JS = r"""
() => {
  let n = 0;
  const toMs = (v) => { v = (v || '0s').trim(); const f = parseFloat(v) || 0;
                        return v.endsWith('ms') ? f : f * 1000; };
  for (const el of document.querySelectorAll('*')) {
    let anims;
    try {
      // 只处理文档时间线上的动画: 滚动驱动(scroll()/view() 时间线)的进度由滚动位置决定,
      // currentTime 是百分比, 写成 delay 或对齐都会错
      anims = el.getAnimations().filter(a => a.animationName && a.effect &&
                                             a.timeline === document.timeline &&
                                             !a.effect.pseudoElement && typeof a.currentTime === 'number');
    } catch (e) { continue; }
    if (!anims.length) continue;
    try {
      const cs = getComputedStyle(el);
      const names = cs.animationName.split(',').map(x => x.trim());
      const delays = cs.animationDelay.split(',');
      const vals = names.map((nm, i) => {
        const d0 = toMs(delays[i % delays.length]);
        const a = anims.find(x => x.animationName === nm);
        return a ? Math.round(d0 - a.currentTime) + 'ms' : d0 + 'ms';
      });
      el.style.setProperty('animation-delay', vals.join(', '), 'important');
      el.setAttribute('data-clone-anim', '1');
      n++;
    } catch (e) {}
  }
  return n;
}
"""

# 评分侧相位对齐: 带 data-clone-anim 的动画回到起播相位(= 原站截图时刻)并暂停,
# 比较的是同一时刻的两张图; 交付的克隆页本身照常播放。
ANIM_ALIGN_JS = r"""
() => {
  let n = 0;
  for (const el of document.querySelectorAll('[data-clone-anim]')) {
    try {
      for (const a of el.getAnimations()) {
        if (!a.animationName || a.timeline !== document.timeline) continue;
        a.currentTime = 0; a.pause(); n++;
      }
    } catch (e) {}
  }
  return n;
}
"""

# 可见文本采集(含同源 iframe 内容, 保证克隆/原站两侧口径一致)
# 自适应缓释(三段式, 全部为同步脚本 + 受时限的 wait_for_function):
# 不能用"evaluate 一个 async Promise"实现 —— MHTML 保真轨页面脚本被禁用, 其
# setTimeout 永不触发, Promise 永不 resolve, 而 evaluate 没有超时 → 整个任务挂死。
SETTLE_INSTALL_JS = r"""({quiet}) => {
  try {
    const w = window;
    w.__cloneSettle = {last: performance.now(), quiet: quiet, mo: null};
    const mo = new MutationObserver(() => { w.__cloneSettle.last = performance.now(); });
    mo.observe(document.documentElement, {subtree: true, childList: true,
                                          attributes: true, characterData: true});
    w.__cloneSettle.mo = mo;
    return true;
  } catch (e) { return false; }
}"""

SETTLE_READY_JS = r"""() => {
  const s = window.__cloneSettle;
  if (!s) return true;
  try { if (document.fonts && document.fonts.status === 'loading') return false; } catch (e) {}
  const imgs = document.images;
  for (let i = 0; i < imgs.length; i++) { if (!imgs[i].complete) return false; }
  return performance.now() - s.last >= s.quiet;
}"""

SETTLE_CLEANUP_JS = r"""() => {
  const s = window.__cloneSettle;
  try { if (s && s.mo) s.mo.disconnect(); } catch (e) {}
  try { delete window.__cloneSettle; } catch (e) {}
}"""

TEXT_EXTRACT_JS = r"""
() => {
  let out = '';
  try { out += document.body ? document.body.innerText : ''; } catch (e) {}
  document.querySelectorAll('iframe').forEach(f => {
    try {
      // 只计可见 iframe: 隐藏/零尺寸 iframe(预加载沙箱、以 text/plain 打开的样式表等)
      // 的文字不在画面上, 计入会让文本通道按不可见内容扣分
      const r = f.getBoundingClientRect();
      const cs = getComputedStyle(f);
      if (r.width < 2 || r.height < 2 || cs.visibility === 'hidden' || cs.display === 'none') return;
      const d = f.contentDocument;
      if (d && d.body) out += '\n' + d.body.innerText;
    } catch (e) {}
  });
  return out.slice(0, 30000);
}
"""

# 全部 iframe 的绝对 src(含跨域; 跨域 contentDocument 不可读, 需顶级渲染固化)
IFRAME_SRCS_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('iframe[src]').forEach(f => {
    const raw = f.getAttribute('src') || '';
    if (!raw || raw.startsWith('about:') || raw.startsWith('javascript:') || raw.startsWith('data:')) return;
    // 不可见 iframe(埋点/转化像素, 0x0 或隐藏)不固化: 每个都要整页导航 + 退化重试,
    // 视觉上毫无贡献(wix 两个 doubleclick 像素白耗约 11s)
    try {
      const r = f.getBoundingClientRect();
      const cs = getComputedStyle(f);
      if (r.width <= 2 || r.height <= 2 || f.clientWidth <= 2 || f.clientHeight <= 2 || cs.display === 'none' || cs.visibility === 'hidden') return;
    } catch (e) {}
    try { out.push(new URL(raw, location.href).href); } catch (e) {}
  });
  return out.slice(0, 10);
}
"""

# 基准后塌陷回撤: 基准截图时可见且尺寸稳定的块(layout_map 稳定项), 到序列化前被页面
# 脚本用内联 display:none 收起 —— 典型是广告位竞价落空后 GPT collapseEmptyDivs 把空位
# 连同外框一起收掉(techcrunch 14 个位整列上移 600px)。克隆按基准口径保留占位:
# 只去掉内联 display:none, 不碰类名/样式表; 基准里本就不可见的块不在稳定项里, 不受影响。
COLLAPSE_REVERT_JS = r"""
(keys) => {
  let n = 0;
  for (const k of keys) {
    let el = null;
    try { el = document.querySelector(k); } catch (e) { continue; }
    if (!el || !el.style || el.style.display !== 'none') continue;
    el.style.removeProperty('display');
    n++;
    // 收起常是一层层做的(位 + 外框): 祖先同样被内联收起的一并回撤
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      if (p.style && p.style.display === 'none') { p.style.removeProperty('display'); n++; }
    }
  }
  return n;
}
"""

# 克隆侧塌陷探针(布局校准用): 原站基准可见、克隆里被脚本内联 display:none 收起的块
# (离线时广告/推荐位请求失败, 站点脚本把空位收掉; 采集侧回撤挡不住回放时再收一次)。
# 返回 [路径, 去掉内联后样式表给出的 display], 由校准样式以 !important 压回。
COLLAPSED_PROBE_JS = r"""
(keys) => {
  const out = [];
  for (const k of keys) {
    let el = null;
    try { el = document.querySelector(k); } catch (e) { continue; }
    if (!el || !el.style || el.style.display !== 'none') continue;
    const prev = el.style.getPropertyValue('display'), pri = el.style.getPropertyPriority('display');
    el.style.removeProperty('display');
    const d = getComputedStyle(el).display;
    el.style.setProperty('display', prev, pri);
    if (d && d !== 'none') out.push([k, d]);
  }
  return out.slice(0, 40);
}
"""

# 不可见 iframe(与 IFRAME_SRCS_JS 同口径)打标: 它们不固化, 若保留活体 src, 克隆每次渲染
# 都会经反代/直连加载埋点沙盒(Shopify web-pixels 等), 同源进程内跑脚本拖垮评分渲染
MARK_HIDDEN_FRAMES_JS = r"""
() => {
  let n = 0;
  document.querySelectorAll('iframe[src]').forEach(f => {
    const raw = f.getAttribute('src') || '';
    if (!raw || /^(about:|javascript:|data:)/i.test(raw)) return;
    try {
      const r = f.getBoundingClientRect();
      const cs = getComputedStyle(f);
      if (r.width <= 2 || r.height <= 2 || f.clientWidth <= 2 || f.clientHeight <= 2 || cs.display === 'none' || cs.visibility === 'hidden') {
        f.setAttribute('data-clone-hidden-frame', '1'); n++;
      }
    } catch (e) {}
  });
  return n;
}
"""


COLLECT_ASSETS_JS = r"""
() => {
  const urls = [];
  const srcsets = [];
  const rawRefs = [];
  const seen = new Set();
  const seenSet = new Set();
  const seenRaw = new Set();
  const add = (u) => {
    if (!u) return;
    u = String(u).trim();
    if (!u || u.startsWith('data:') || u.startsWith('blob:') || u.startsWith('#') || u.startsWith('%23')
        || u.startsWith('javascript:') || u.startsWith('mailto:') || u.startsWith('tel:')) return;
    try {
      const abs = new URL(u, document.baseURI).href;
      if (abs.startsWith('http')) {
        if (!seen.has(abs)) { seen.add(abs); urls.push(abs); }
        const key = u + '=>' + abs;
        if (!seenRaw.has(key)) { seenRaw.add(key); rawRefs.push([u, abs]); }
      }
    } catch (e) { return; }
  };
  const addSet = (s) => {
    if (!s) return;
    s = String(s).trim();
    if (s && !seenSet.has(s)) { seenSet.add(s); srcsets.push(s); }
  };
  document.querySelectorAll('link[href]').forEach(el => {
    const rel = (el.getAttribute('rel') || '').toLowerCase();
    if (rel.includes('stylesheet') || rel.includes('icon') || rel.includes('preload') || rel.includes('prefetch')) {
      add(el.getAttribute('href'));
    }
  });
  document.querySelectorAll('script[src]').forEach(el => add(el.getAttribute('src')));
  document.querySelectorAll('img').forEach(el => {
    add(el.getAttribute('src'));
    add(el.getAttribute('data-src'));
    add(el.getAttribute('data-original'));
    addSet(el.getAttribute('srcset'));
    addSet(el.getAttribute('data-srcset'));
  });
  document.querySelectorAll('picture source, source, video, audio').forEach(el => {
    add(el.getAttribute('src'));
    add(el.getAttribute('data-src'));
    addSet(el.getAttribute('srcset'));
    addSet(el.getAttribute('data-srcset'));
  });
  // video poster 封面帧
  document.querySelectorAll('video[poster]').forEach(el => add(el.getAttribute('poster')));
  // 内联 SVG 外部雪碧图 <use href> / <image xlink:href>
  document.querySelectorAll('svg image, svg use, image').forEach(el => {
    add(el.getAttribute('href'));
    add(el.getAttribute('xlink:href'));
  });
  // 内联 style 与 <style> 块中的 url(...)(含 background/mask/@font-face)
  const scanStyle = (s) => {
    if (!s) return;
    const re = /url\((['"]?)([^'")]+)\1\)/g;
    let m;
    while ((m = re.exec(String(s))) !== null) add(m[2]);
  };
  document.querySelectorAll('[style]').forEach(el => scanStyle(el.getAttribute('style')));
  document.querySelectorAll('style').forEach(el => scanStyle(el.textContent));
  return { urls: urls, srcsets: srcsets, rawRefs: rawRefs };
}
"""

IFRAME_EXTRACT_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('iframe').forEach(f => {
    try {
      const raw = f.getAttribute('src') || '';
      if (!raw || raw.startsWith('about:') || raw.startsWith('javascript:')) return;
      const abs = new URL(raw, location.href);
      if (abs.origin !== location.origin) return;  // 仅同源 iframe 可读取
      const doc = f.contentDocument;
      if (!doc || !doc.documentElement) return;
      const urls = [], srcsets = [], rawRefs = [];
      const seen = new Set(), seenSet = new Set();
      const add = (u) => {
        if (!u) return;
        u = String(u).trim();
        if (!u || u.startsWith('data:') || u.startsWith('blob:') || u.startsWith('#') || u.startsWith('%23')) return;
        try {
          const a = new URL(u, doc.baseURI || abs.href).href;
          if (a.startsWith('http') && !seen.has(a)) { seen.add(a); urls.push(a); rawRefs.push([u, a]); }
        } catch (e) { return; }
      };
      const addSet = (s) => {
        s = String(s || '').trim();
        if (s && !seenSet.has(s)) { seenSet.add(s); srcsets.push(s); }
      };
      doc.querySelectorAll('link[href],script[src],img,source,video,audio,svg image,svg use').forEach(el => {
        add(el.getAttribute('href'));
        add(el.getAttribute('xlink:href'));
        add(el.getAttribute('src'));
        add(el.getAttribute('data-src'));
        addSet(el.getAttribute('srcset'));
        addSet(el.getAttribute('data-srcset'));
      });
      doc.querySelectorAll('video[poster]').forEach(el => add(el.getAttribute('poster')));
      doc.querySelectorAll('[style]').forEach(el => {
        const s = el.getAttribute('style') || '';
        const re = /url\((['"]?)([^'")]+)\1\)/g;
        let m;
        while ((m = re.exec(s)) !== null) add(m[2]);
      });
      out.push({ src: abs.href, html: (__DOCTYPE__)(doc) + doc.documentElement.outerHTML,
                 urls: urls, srcsets: srcsets, rawRefs: rawRefs });
    } catch (e) { return; }
  });
  return out.slice(0, 5);
}
""".replace("__DOCTYPE__", DOCTYPE_FN)

COLLECT_LINKS_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const skipExt = /\.(png|jpe?g|gif|svg|webp|avif|bmp|ico|css|js|mjs|json|xml|pdf|zip|gz|rar|7z|tar|mp4|webm|mp3|wav|ogg|woff2?|ttf|eot|otf)$/i;
  document.querySelectorAll('a[href]').forEach(a => {
    const h = a.getAttribute('href');
    if (!h || h.startsWith('#') || h.startsWith('mailto:') || h.startsWith('tel:') || h.startsWith('javascript:')) return;
    try {
      const u = new URL(h, document.baseURI);
      if (u.origin !== location.origin) return;
      const p = u.pathname.replace(/\/+$/, '');
      if (!p || skipExt.test(p)) return;
      if (!seen.has(p)) { seen.add(p); out.push(p); }
    } catch (e) { return; }
  });
  return out.slice(0, 120);
}
"""


# 第三方广告位(程序化竞价, 每次请求创意都不同): 克隆不可能也不该复现某一次的竞价结果,
# 评分时两侧同一位置统一遮成中灰。只认广告投放的专有标识(GPT 容器/iframe、主流 SSP 域名),
# 站点自有的推广位不在此列。techcrunch 每轮 3~8 分的随机抖动全来自这里
AD_RECTS_JS = r"""() => {
  const out = [], sx = window.scrollX || 0, sy = window.scrollY || 0;
  const AD_SRC = /doubleclick\.net|googlesyndication\.com|adservice\.google|amazon-adsystem\.com|adnxs\.com|taboola\.com|outbrain\.com|criteo\.(?:com|net)|pubmatic\.com|rubiconproject\.com/i;
  const seen = new Set();
  const add = (el) => {
    if (!el || seen.has(el)) return;
    seen.add(el);
    const r = el.getBoundingClientRect();
    if (r.width < 30 || r.height < 30) return;
    out.push([Math.round(r.left + sx), Math.round(r.top + sy), Math.round(r.width), Math.round(r.height)]);
  };
  document.querySelectorAll('iframe').forEach(f => {
    const id = f.id || '', src = f.getAttribute('src') || '';
    if (id.startsWith('google_ads_iframe') || AD_SRC.test(src)) add(f);
  });
  document.querySelectorAll('[id^="div-gpt-ad"], [data-google-query-id]').forEach(add);
  return out.slice(0, 80);
}"""


# 插屏广告层(进页即弹的全屏遮罩 + 跨域广告 iframe, 素材每次竞价都不同): 评分截图前两侧同口径隐藏,
# 不计分(与 AD_RECTS_JS 遮广告位同理)。克隆交付物里照样保留该层, 只影响评分截图。
# 纯结构判据, 不认域名: position:fixed、覆盖视口 ≥90% 宽且 ≥80% 高、z-index ≥ 1000, 且内含可见的
# 跨源 iframe(面积 ≥ 100×100)。163/搜狐手机版首屏每次都被百度联盟插屏盖住, 两侧素材/几何必然不同
INTERSTITIAL_HIDE_JS = r"""() => {
  const vw = innerWidth, vh = innerHeight;
  let n = 0;
  const cross = f => {
    const src = f.getAttribute('src') || '';
    if (!src || /^(about:|javascript:|data:|blob:)/i.test(src)) return false;
    try { return new URL(src, document.baseURI).origin !== location.origin; } catch (e) { return false; }
  };
  const iframes = Array.from(document.querySelectorAll('iframe')).filter(f => {
    const r = f.getBoundingClientRect();
    return r.width >= 100 && r.height >= 100 && cross(f);
  });
  const done = new Set();
  for (const f of iframes) {
    for (let e = f.parentElement; e && e !== document.body && e !== document.documentElement; e = e.parentElement) {
      if (done.has(e)) break;
      const cs = getComputedStyle(e);
      if (cs.position !== 'fixed') continue;
      const r = e.getBoundingClientRect(), z = parseInt(cs.zIndex, 10);
      if (r.width >= vw * 0.9 && r.height >= vh * 0.8 && z >= 1000) {
        e.setAttribute('data-clone-ihide', e.style.getPropertyValue('display') + '|' + e.style.getPropertyPriority('display'));
        e.style.setProperty('display', 'none', 'important');
        done.add(e); n++;
      }
      break;   // 只看最近的 fixed 祖先
    }
  }
  return n;
}"""

# 截图后还原(原站活体页随后还要冻结交付, 插屏层不能带着 display:none 落盘)
INTERSTITIAL_RESTORE_JS = r"""() => {
  document.querySelectorAll('[data-clone-ihide]').forEach(e => {
    const [v, pri] = e.getAttribute('data-clone-ihide').split('|');
    e.removeAttribute('data-clone-ihide');
    if (v) e.style.setProperty('display', v, pri || ''); else e.style.removeProperty('display');
    if (!e.getAttribute('style')) e.removeAttribute('style');
  });
}"""

# rem 基准(lib-flexible 类脚本按 视口宽/N 写在 <html>/<body> 内联 font-size): 冻结后克隆里没有脚本重算,
# 采集宽下的 px 被写死(手机版 390 采集 → 420 真机全站 rem 尺寸统一小 7%, 内容列 390 宽右侧留白)。
# 读: 返回 [innerWidth, html 内联 px, html 计算 px, body 内联 px, body 计算 px]
ROOT_FONT_READ_JS = r"""() => {
  const px = v => { const m = /^([\d.]+)px$/.exec((v || '').trim()); return m ? parseFloat(m[1]) : 0; };
  const h = document.documentElement, b = document.body;
  return [innerWidth, px(h.style.fontSize), px(getComputedStyle(h).fontSize),
          b ? px(b.style.fontSize) : 0, b ? px(getComputedStyle(b).fontSize) : 0];
}"""
# 写: {html: vw, body: vw} → 内联 font-size 改成 vw(保留优先级)
ROOT_FONT_APPLY_JS = r"""(m) => {
  let n = 0;
  for (const [k, el] of [['html', document.documentElement], ['body', document.body]]) {
    if (!el || !m[k]) continue;
    el.style.setProperty('font-size', m[k].toFixed(4) + 'vw', el.style.getPropertyPriority('font-size'));
    n++;
  }
  return n;
}"""
