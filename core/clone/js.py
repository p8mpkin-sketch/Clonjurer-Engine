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
  // 只给 width 属性 + 内联 aspect-ratio, 不给 height 属性也不写 height:auto: img 的 height 属性是展示提示(等于 CSS height:Hpx,
  // 别的视口不随宽缩), 内联 height:auto 又会压过站点 CSS 给这个盒子的高(flex 拉伸/显式高): vite 平板的图表 canvas 盒高 347 由
  // 布局给出、位图按盒子算, 固化后 height:auto 按桌面位图比例只剩 312, 下文整段上移 35px。aspect-ratio 与 canvas 固有比例同义:
  // CSS 没定高时按比例随宽缩, CSS 定了高/拉伸时让位
  // 比例取 canvas 显示盒(位图常按 DPR/内部分辨率另算, 与盒子不成比例: vite 平板位图 751x359、盒 653x347), 没渲染才退回位图比例;
  // 位图高另存 data-cv-h, 供分视口对照按 canvas 的 height 属性口径比较(CV_ATTR_FN)
  if (cv.width && cv.height) {
    img.setAttribute('width', String(cv.width)); img.setAttribute('data-cv-h', String(cv.height));
    let bw = 0, bh = 0;
    try { const rb = cv.getBoundingClientRect(); bw = rb.width; bh = rb.height; } catch (e) {}
    img.style.aspectRatio = (bw > 0 && bh > 0) ? (Math.round(bw * 100) / 100) + ' / ' + (Math.round(bh * 100) / 100) : cv.width + ' / ' + cv.height;
  }
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

# 外链样式表的内联 CSSOM 副本去重(异步, 固化后单独执行): 对每个带 data-cssom-href 的序列化块,
# 从浏览器缓存取回样式表原文(force-cache, 刚加载过, 毫秒级), 用浏览器自己的解析器解析并做同样的
# url() 绝对化, 与块文本逐条 cssText 比对; 完全一致说明该表运行时未被改动, 文件本身已随包本地化,
# 内联副本是纯重复 → 移除。跨域无 CORS(fetch 失败)/解析不一致的保留。
# 全语料 120 页均值: data-cssom 内联 215KB/页, 占交付 HTML 37%, 绝大多数就是这类副本。
CSSOM_LINK_DEDUPE_JS = r"""
async () => {
  const out = { checked: 0, removed: 0, bytes: 0, kept: 0 };
  const abs = (text, base) => text.replace(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^'")\s]+))\s*\)/g,
    function (m, d, s1, b) {
      const q = d !== undefined ? '"' : (s1 !== undefined ? "'" : '');
      const u = (d !== undefined ? d : (s1 !== undefined ? s1 : b)).trim();
      if (/^(data:|blob:|about:|#|%23|https?:)/i.test(u)) return m;
      try { return 'url(' + q + new URL(u, base).href + q + ')'; } catch (e) { return m; }
    });
  const canon = (text) => {
    const s = new CSSStyleSheet(); s.replaceSync(text);
    const parts = []; for (let i = 0; i < s.cssRules.length; i++) parts.push(s.cssRules[i].cssText);
    return parts;
  };
  const blocks = Array.from(document.querySelectorAll('style[data-cssom="true"][data-cssom-href]'));
  await Promise.all(blocks.map(async (st) => {
    const href = st.getAttribute('data-cssom-href');
    out.checked++;
    try {
      const r = await fetch(href, { cache: 'force-cache', credentials: 'include' });
      if (!r.ok) { out.kept++; return; }
      let src = await r.text();
      const body = st.textContent || '';
      const mm = body.match(/^\s*@media\s+([^{]+)\{/);
      if (mm) src = '@media ' + mm[1].trim() + ' {\n' + src + '\n}';
      const a = canon(abs(src, href).replace(/\n/g, '\n'));
      const b = canon(body);
      let same = a.length === b.length && a.length > 0;
      for (let i = 0; same && i < a.length; i++) same = a[i] === b[i];
      if (same) { out.bytes += body.length; st.remove(); out.removed++; } else { out.kept++; }
    } catch (e) { out.kept++; }
  }));
  return out;
}
"""

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
      //    内联 <style> 的表: 若 CSSOM 与元素文本解析结果逐条一致(未被 insertRule 改过),
      //    DOM 里已有这份样式, 再序列化一份就是纯重复(gitee 首页 14 块 Next.js 内联样式全是) → 跳过
      const owner = sheet.ownerNode;
      if (owner && owner.tagName === 'STYLE' && !owner.hasAttribute('data-cssom') && kind === 'true') {
        try {
          const tmp = new CSSStyleSheet();
          tmp.replaceSync(owner.textContent || '');
          let same = tmp.cssRules.length === rules.length;
          for (let i = 0; same && i < rules.length; i++) same = tmp.cssRules[i].cssText === rules[i].cssText;
          if (same) { result.cssSkipped = (result.cssSkipped || 0) + 1; return; }
        } catch (e) { /* 构造表不可用或解析差异: 照常序列化 */ }
      }
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
      //    记录来源表地址与规则数: 落盘时若该表已成功本地化且规则集一致, 内联副本即纯重复
      //    (全语料均值 215KB/页, 占交付 HTML 37%), 由引擎侧去重; 运行时改过的表规则集不一致则保留。
      if (sheet.href) style.setAttribute('data-cssom-href', sheet.href);
      style.setAttribute('data-cssom-rules', String(rules.length));
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
        const img = (__CANVAS_IMG__)(cv, data);
        cv.replaceWith(img);
        // 多视口记录(CV_RECORD_JS)在固化之前抓的元素表里还是这个 canvas: 换成 img, 否则 CV_EMIT 见它已脱离文档就跳过,
        // canvas 的分视口盒高(图表库按宽算、不成比例的平板/手机高)一条都写不出(vite 平板 312 vs 347)
        try { const R = window.__cvRec, k = R && R.els ? R.els.indexOf(cv) : -1; if (k >= 0) R.els[k] = img; } catch (e) {}
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
    const SIZEP = new Set(['font-size', 'line-height', 'letter-spacing']);
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
      // 字号随视口宽同比的元素(见 FLUID_READ_JS 探测): 字号/行高/字距交给样式表, 不钉成采集宽的 px
      const fluid = el.hasAttribute('data-cv-fluidsz');
      for (let i = 0; i < PROPS.length; i++) {
        if (i === 0 && mine[0] === defFam) continue;
        if (fluid && SIZEP.has(PROPS[i])) continue;
        if (!pv || pv[i] !== mine[i]) diff.push([PROPS[i], mine[i]]);
      }
      if (diff.length) plan.push([el, diff]);
    }
    document.querySelectorAll('[data-cv-fluidsz]').forEach(e => e.removeAttribute('data-cv-fluidsz'));
    return __SCOPED_BAKE__('f', plan);
  } catch (e) { return 0; }
}
""".replace("__SCOPED_BAKE__", "(" + SCOPED_BAKE_FN + ")")
# rem 基准随视口宽的窄屏采集(见 ROOT_FONT_READ_JS): 字号/行高/字距由 rem 规则随宽变化, 烘焙成采集宽的 px
# 会把它们冻住(163 手机版 420 宽下标题/来源统一小 7% = 390/420)。只烘焙族名/字重/字形/颜色
FONT_BAKE_NOSIZE_JS = FONT_BAKE_JS.replace(
    "const PROPS = ['font-family', 'font-size', 'font-weight', 'font-style',\n                   'line-height', 'letter-spacing', 'color'];",
    "const PROPS = ['font-family', 'font-weight', 'font-style', 'color'];")
assert FONT_BAKE_NOSIZE_JS != FONT_BAKE_JS


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
# body 直接子元素有稳定 id 的按 id 认, 其余按"同标签且无 id 的前兄弟"计序: 第三方脚本(Shopify 应用/像素沙箱/埋点)往 body
# 插的容器在主页与窄屏首载页里数量、位置不一样, 纯序号路径整体错位, 分视口样式/变体会对到别的区块上(allbirds 手机
# 底部 526px 区块的窄屏版被并列插到 main 之前, 93→43 双峰)。只在 body 这一层这样做: 深层 id 多是按渲染计数生成的
CV_PATH_FN = r"""(e) => {
  const p = [];
  const M = window.__cvSid || (window.__cvSid = new WeakMap());
  const sid = x => { if (M.has(x)) return M.get(x); const v = x.id;
    const r = v && /^[A-Za-z][\w-]{1,100}$/.test(v) && document.querySelectorAll('[id="' + v + '"]').length === 1 ? v : ''; M.set(x, r); return r; };
  while (e && e.parentElement && e !== document.body) {
    const top = e.parentElement === document.body;
    const id = top ? sid(e) : '';
    if (id) p.unshift(e.tagName.toLowerCase() + '#' + id);
    else {
      let i = 1, s = e;
      while ((s = s.previousElementSibling)) if (s.tagName === e.tagName && !(top && sid(s))) i++;
      p.unshift(e.tagName.toLowerCase() + ':' + i);
    }
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
  // 固化 canvas 的 img 只有 width 属性 + 内联 aspect-ratio(见 CANVAS_IMG_FN): 等价高度补出来, 才能与窄屏首载页里
  // canvas 的 height 属性对照(图表库按宽算高不成比例时, 平板/手机段各自写 px)
  if (e.tagName === 'IMG' && e.getAttribute('data-canvas-frozen') && !e.getAttribute('height')) {
    const hv = e.getAttribute('data-cv-h');
    if (hv && /^\d+$/.test(hv)) s += 'height:' + hv + 'px;';
    else {
      const w = +e.getAttribute('width'), ar = /([\d.]+)\s*\/\s*([\d.]+)/.exec(e.style.aspectRatio || '');
      if (w && ar && +ar[1]) s += 'height:' + (w * ar[2] / ar[1]).toFixed(2).replace(/\.?0+$/, '') + 'px;';
    }
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
  // [6] 媒体源(src/srcset/poster, \x01 连接) [7] 叶子文本: 桌面上隐藏的手机版元素, 脚本只在可见时才写 src /
  // 才跑计数动画, 桌面采集永远拿不到 → 用窄屏首载页同路径元素的值补(见 CV_EMIT_JS 末尾)
  const MEDIA = /^(IMG|SOURCE|VIDEO|AUDIO)$/;
  // 主页的 canvas 在 DEEP_FREEZE 后已固化成 <img data-canvas-frozen>, 窄屏首载页里还是 <canvas>: 子元素签名里按 CANVAS 记,
  // 否则每个 canvas 的父元素都被判成「随视口换结构」, 并列插入的窄屏版带着首载那一个宽度的 px 盒(768 采的到 820 宽窄一截)
  const CANON = 1;
  for (const e of els) out[pathOf(e)] = [e.tagName, String(e.className && e.className.baseVal !== undefined ? e.className.baseVal : e.className || ''), e.getAttribute('style') || '',
    Array.from(e.children, c => (CANON && c.tagName === 'IMG' && c.getAttribute('data-canvas-frozen')) ? 'CANVAS' : c.tagName).filter(t => !NONVIS.has(t)).join(',').slice(0, 400), (""" + CV_ATTR_FN + r""")(e),
    (""" + CV_XATTR_FN + r""")(e),
    MEDIA.test(e.tagName) ? [e.getAttribute('src') || '', e.getAttribute('srcset') || '', e.getAttribute('poster') || ''].join('\x01').replace(/\x01+$/, '') : '',
    (!e.children.length && !NONVIS.has(e.tagName) && e.tagName !== 'TEXTAREA') ? (e.textContent || '').trim().slice(0, 300) : '',
    // [8] 子元素 id 序列(与 [3] 同序; 非稳定形态的 id 记空): 标签序列相同而某个带 id 的子项只在一侧存在时,
    // 按序号对齐会把不同组件配成一对(见 _inject_dom_variants 的 id 错位判定)
    Array.from(e.children).filter(c => !NONVIS.has(c.tagName)).map(c => /^[A-Za-z][\w-]{1,100}$/.test(c.id || '') ? c.id : '').join(',').slice(0, 2400)];
  // 根元素只记 class(加载时按宽度给 <html>/<body> 打的 _mobile / is-mobile 类标记)
  for (const [k, e] of [['@html', document.documentElement], ['@body', document.body]])
    if (e) out[k] = [e.tagName, e.getAttribute('class') || '', '', '', '', '', '', '', ''];
  return out;
}"""

# ---- 窄屏 DOM 变体 ----
# React/Vue 站按断点渲染不同的 DOM(ant.design 手机宽新闻卡片换成单卡轮播): 样式怎么
# 改都救不回结构。窄屏首载页与主页按结构路径对齐, 子元素结构不同的最上层子树把窄屏版
# 也序列化进克隆, 两版各打 data-cv-vp 视口令牌, 由分视口样式表只显示当前视口那一版。
ALT_EXTRACT_JS = r"""(paths) => {
  const VFILL = 1;
  // 与 CV_PATH_FN 同口径: body 这一层有唯一 id 的段写成 tag#id, 序号段只数同标签且无唯一 id 的兄弟
  const resolve = (path) => {
    let e = document.body;
    const sid = x => { const v = x.id; return v && /^[A-Za-z][\w-]{1,100}$/.test(v) && document.querySelectorAll('[id="' + v + '"]').length === 1 ? v : ''; };
    for (const seg of path ? path.split('>') : []) {
      const top = e === document.body;
      let hit = null;
      const h = seg.indexOf('#');
      if (h > 0) {
        const tag = seg.slice(0, h), id = seg.slice(h + 1);
        const c = document.getElementById(id);
        if (c && c.parentElement === e && c.tagName.toLowerCase() === tag) hit = c;
      } else {
        const [tag, n] = seg.split(':');
        let k = 0;
        for (const c of e.children) if (c.tagName.toLowerCase() === tag && !(top && sid(c)) && ++k === +n) { hit = c; break; }
      }
      if (!hit) return null;
      e = hit;
    }
    return e;
  };
  // 互斥兄弟组(轮播项/标签页: ≥2 个同标签兄弟, 恰一个渲染、其余 display:none, 只差状态类)当前显示的不是第一个时,
  // 把第一个与当前项的 class/内联 style 对调 —— 窄屏页已跑了几秒, 轮播翻到了第 k 项; 站点脚本在克隆里重跑时常
  // 无条件给第一项加 active(原站首载本就是第一项, 无副作用), 克隆就两项同时可见(cgnpc 手机横幅下多出一整项)。
  // 对调后克隆与原站同从第一项起播, 脚本加 active 成为空操作
  const STATE = /^(active|current|cur|on|show|shown|selected|open|opened|in|visible|is-active|is-current|is-selected|slick-active|slick-current|swiper-slide-active|next|prev|left|right)$/i;
  const normalize = (live, copy) => {
    let n = 0;
    const walk = (a, b) => {
      const ac = a.children, bc = b.children;
      if (ac.length !== bc.length) return;
      if (ac.length >= 2 && Array.from(ac).every(x => x.tagName === ac[0].tagName)) {
        const shown = Array.from(ac).filter(x => x.getClientRects().length);
        const hidden = Array.from(ac).filter(x => !x.getClientRects().length && getComputedStyle(x).display === 'none');
        if (shown.length === 1 && hidden.length === ac.length - 1 && shown[0] !== ac[0]) {
          const k = Array.prototype.indexOf.call(ac, shown[0]);
          const toks = x => new Set((x.getAttribute('class') || '').split(/\s+/).filter(Boolean));
          const ts = toks(shown[0]), tf = toks(ac[0]);
          const diff = [...ts].filter(x => !tf.has(x)).concat([...tf].filter(x => !ts.has(x)));
          if (diff.length && diff.every(x => STATE.test(x))) {
            const f = bc[0], s = bc[k];
            for (const at of ['class', 'style']) {
              const fv = f.getAttribute(at), sv = s.getAttribute(at);
              if (sv === null) f.removeAttribute(at); else f.setAttribute(at, sv);
              if (fv === null) s.removeAttribute(at); else s.setAttribute(at, fv);
            }
            n++;
          }
        }
      }
      for (let i = 0; i < ac.length; i++) walk(ac[i], bc[i]);
    };
    walk(live, copy);
    return n;
  };
  const out = {};
  for (const p of paths) {
    const el = resolve(p);
    if (!el) continue;
    const c = el.cloneNode(true);
    const normed = normalize(el, c);
    // cloneNode 不带 canvas 位图: 按原节点逐个固化成 <img>(与 DEEP_FREEZE 第 5 步同口径), 否则窄屏版
    // 的图表/插画整块空白
    const cvs = el.querySelectorAll('canvas'), cvc = c.querySelectorAll('canvas');
    cvs.forEach((cv, k) => {
      try {
        const data = cv.toDataURL('image/png'), dst = cvc[k];
        if (!dst || !data || data.length <= 128) return;
        const im = (__CANVAS_IMG__)(cv, data);
        // 脚本按容器宽写了内联 px 宽高、且显示盒正好撑满父元素内容宽的 canvas: 窄屏版只在首载那一个宽度(768/390)采到,
        // 钉着 px 到同段别的机型(820/360/420)就窄一截或溢出 → 写 width:100% + height:auto, 由内联 aspect-ratio 定高
        if (VFILL) try {
          const pw = /^([\d.]+)px$/.exec(cv.style.width || ''), ph = /^([\d.]+)px$/.exec(cv.style.height || ''), pe = cv.parentElement;
          if (pw && ph && pe) {
            const pc = getComputedStyle(pe), rb = cv.getBoundingClientRect();
            const inner = pe.clientWidth - (parseFloat(pc.paddingLeft) || 0) - (parseFloat(pc.paddingRight) || 0);
            if (rb.width > 0 && Math.abs(inner - rb.width) <= 1.5 && Math.abs(rb.width - +pw[1]) <= 1.5 && Math.abs(rb.height - +ph[1]) <= 1.5) {
              im.style.setProperty('width', '100%'); im.style.setProperty('height', 'auto');
            }
          }
        } catch (e) {}
        dst.replaceWith(im);
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
    out[p] = [c.outerHTML, (el.innerText || '').trim().length, el.getElementsByTagName('*').length, el.querySelectorAll('input:not([type=hidden]),textarea,select').length, normed];
  }
  return out;
}""".replace("__CANVAS_IMG__", CANVAS_IMG_FN)

# 主页同路径子树的体量(文本长度/元素数), 判断窄屏版是"换了结构"还是"还没加载完"
SUBTREE_SIZE_JS = r"""(paths) => {
  // 与 CV_PATH_FN 同口径: body 这一层有唯一 id 的段写成 tag#id, 序号段只数同标签且无唯一 id 的兄弟
  const resolve = (path) => {
    let e = document.body;
    const sid = x => { const v = x.id; return v && /^[A-Za-z][\w-]{1,100}$/.test(v) && document.querySelectorAll('[id="' + v + '"]').length === 1 ? v : ''; };
    for (const seg of path ? path.split('>') : []) {
      const top = e === document.body;
      let hit = null;
      const h = seg.indexOf('#');
      if (h > 0) {
        const tag = seg.slice(0, h), id = seg.slice(h + 1);
        const c = document.getElementById(id);
        if (c && c.parentElement === e && c.tagName.toLowerCase() === tag) hit = c;
      } else {
        const [tag, n] = seg.split(':');
        let k = 0;
        for (const c of e.children) if (c.tagName.toLowerCase() === tag && !(top && sid(c)) && ++k === +n) { hit = c; break; }
      }
      if (!hit) return null;
      e = hit;
    }
    return e;
  };
  const out = {};
  for (const p of paths) { const el = resolve(p); if (el) out[p] = [(el.innerText || '').trim().length, el.getElementsByTagName('*').length, el.querySelectorAll('input:not([type=hidden]),textarea,select').length]; }
  return out;
}"""

# 运行时样式文本(<style> 与文档级构造样式表): 窄屏版组件的 CSS-in-JS 样式只在窄屏页里有
# 页面全部样式规则的逐条文本(含 <link> 表、<style>、adoptedStyleSheets; 分组规则 @media/@supports/@layer 递归到
# 叶子, 叶子带上分组前缀)。窄屏首载页与主页按规则做差, 只把主页没有的规则注入窄屏段 —— 此前按整个 <style>
# 文本做差, 同一份样式表在两页里序列化略有不同就被当成"窄屏才有", 整表再注入一遍到手机段, 层叠顺序被打乱
# (solidjs 手机端链接下划线/字体回落, 基础层被后注入的副本盖掉)
STYLE_TEXTS_JS = r"""() => {
  const out = [];
  const walk = (rules, prefix) => {
    for (const r of rules) {
      let p = null;
      if (r.cssRules && (r.conditionText !== undefined || r instanceof CSSLayerBlockRule)) {
        const head = r instanceof CSSMediaRule ? '@media ' + r.conditionText
          : r instanceof CSSSupportsRule ? '@supports ' + r.conditionText
          : (r instanceof CSSLayerBlockRule ? '@layer ' + r.name : '');
        if (head) { walk(r.cssRules, prefix.concat(head)); continue; }
      }
      out.push([prefix.join('\x00'), r.cssText, src]);
    }
  };
  // 第三项: 规则所属样式表(href / 节点标签+属性 / adopted), 只用于归因日志, 做差不比较它
  let src = '';
  const sheet = (sh, id) => { src = id; try { walk(sh.cssRules, []); } catch (e) {} };
  for (const sh of document.styleSheets) {
    const n = sh.ownerNode;
    // 引擎的 CSSOM 副本(data-cssom)要算进主页规则集: 固化后原 <link> 表可能已停用/摘除, 不算副本就把整份主表
    // 当成"窄屏才有"(solidjs 79KB 重注入)。烘焙表/分视口表仍不算
    if (n && n.getAttribute && (n.hasAttribute('data-clone-bake') || n.hasAttribute('data-clone-vp'))) continue;
    let id = sh.href || '';
    if (!id && n) { id = n.tagName.toLowerCase(); for (const a of (n.attributes || [])) id += ' ' + a.name + (a.value ? '=' + a.value.slice(0, 40) : '');
      // 无属性 <style> 用首条规则文本区分(脚本注入的表没有 href/属性可认)
      if (!n.attributes.length) { let r0 = ''; try { r0 = sh.cssRules.length ? sh.cssRules[0].cssText.slice(0, 50) : ''; } catch (e) {} id += ' #' + (n.parentNode && n.parentNode.tagName || '') + ':' + r0; } }
    sheet(sh, (sh.disabled ? '[disabled] ' : '') + (id || 'inline') + (sh.ownerRule ? ' <-@import' : ''));
  }
  try { for (const sh of document.adoptedStyleSheets || []) sheet(sh, 'adopted'); } catch (e) {}
  return out;
}"""

# 窄屏首载页"到位"等待: 文字量 + 已有 src 的图片数 连续两次(250ms)不变即返回, 上限 ms
NARROW_STABLE_JS = r"""(ms) => new Promise(res => {
  let last = '', hits = 0;
  const sig = () => { const im = document.images; let s = 0; for (const i of im) if (i.currentSrc) s++;
    return (document.body ? document.body.innerText.length : 0) + '|' + im.length + '|' + s + '|' + document.querySelectorAll('input,textarea,select,button').length; };
  const t = setInterval(() => { const v = sig(); if (v === last) { if (++hits >= 2) { clearInterval(t); res(1); } } else { hits = 0; last = v; } }, 250);
  setTimeout(() => { clearInterval(t); res(0); }, ms);
})"""

# 桌面采集视口下整棵不渲染(display:none)的子树根: 父元素可见、自身 display:none、子树 ≥3 个元素。
# 这类子树(手机版横幅/visible-xs 块)的类与内联样式在桌面无从验证 —— 后台脚本照样驱动它们(Bootstrap 轮播对
# display:none 的项翻页, 过渡事件永不触发、时间冻结又截掉 600ms 兜底定时器, 序列化时两项都带 active),
# 窄屏首载页里它可见且已静置, 其版本才是该视口的真实状态(见 _inject_dom_variants)
HIDDEN_ROOTS_JS = r"""() => {
  const out = [], pathOf = __CV_PATH__;
  for (const e of document.body ? document.body.querySelectorAll('*') : []) {
    if (out.length >= 200) break;
    if (e.getClientRects().length || /^(SCRIPT|STYLE|TEMPLATE|NOSCRIPT|LINK|META)$/.test(e.tagName)) continue;
    const p = e.parentElement;
    if (!p || !p.getClientRects().length) continue;
    if (getComputedStyle(e).display !== 'none') continue;
    if (e.getElementsByTagName('*').length < 3) continue;
    out.push(pathOf(e));
  }
  return out;
}""".replace("__CV_PATH__", CV_PATH_FN)

# 给定路径里在本页实际渲染(有盒)、有内容(可见文字 ≥20 字或图/视频/画布)、且不是程序化广告位(GPT 容器/广告 iframe,
# 与 AD_RECTS_JS 同口径)的那些: 广告位在窄屏首载页里是 250px 占位态, 原站评分时要么空着要么被遮, 换成窄屏版
# 反把占位高度带进克隆(techcrunch 手机 96→62)
VISIBLE_PATHS_JS = r"""(paths) => {
  const AD_SRC = /doubleclick\.net|googlesyndication\.com|adservice\.google|amazon-adsystem\.com|adnxs\.com|taboola\.com|outbrain\.com|criteo\.(?:com|net)|pubmatic\.com|rubiconproject\.com|carbonads\.net|buysellads\.(?:net|com)/i;
  const AD_SEL = '[id^="div-gpt-ad"],[data-google-query-id],iframe[id^="google_ads_iframe"],#carbonads,[id^="carbonads"]';
  const isAd = el => {
    if (el.matches(AD_SEL) || el.closest(AD_SEL) || el.querySelector(AD_SEL)) return true;
    for (const f of el.querySelectorAll('iframe,img')) if (AD_SRC.test(f.getAttribute('src') || '')) return true;
    return false;
  };
  const hasContent = el => {
    if ((el.innerText || '').trim().length >= 20) return true;
    for (const m of el.querySelectorAll('img,video,canvas,svg,picture')) { const r = m.getBoundingClientRect(); if (r.width >= 24 && r.height >= 24) return true; }
    return false;
  };
  // 与 CV_PATH_FN 同口径: body 这一层有唯一 id 的段写成 tag#id, 序号段只数同标签且无唯一 id 的兄弟
  const resolve = (path) => {
    let e = document.body;
    const sid = x => { const v = x.id; return v && /^[A-Za-z][\w-]{1,100}$/.test(v) && document.querySelectorAll('[id="' + v + '"]').length === 1 ? v : ''; };
    for (const seg of path ? path.split('>') : []) {
      const top = e === document.body;
      let hit = null;
      const h = seg.indexOf('#');
      if (h > 0) {
        const tag = seg.slice(0, h), id = seg.slice(h + 1);
        const c = document.getElementById(id);
        if (c && c.parentElement === e && c.tagName.toLowerCase() === tag) hit = c;
      } else {
        const [tag, n] = seg.split(':');
        let k = 0;
        for (const c of e.children) if (c.tagName.toLowerCase() === tag && !(top && sid(c)) && ++k === +n) { hit = c; break; }
      }
      if (!hit) return null;
      e = hit;
    }
    return e;
  };
  return paths.filter(p => { const el = resolve(p); return !!(el && el.getClientRects().length && hasContent(el) && !isAd(el)); });
}"""

# 窄屏首载页抽取变体前: 进行中的 CSS 过渡跳到终点(与主页 TIME_FREEZE_JS 同理)。轮播翻页是两个稳态之间的一段,
# 抽在半途就是「上一项 active left + 下一项 next left」两项同时有盒(cgnpc 手机横幅下多出一整项, 整页下移 210px);
# finish() 会派发 transitionend, 轮播脚本据此收尾 class。收尾可能再起一段过渡, 最多三轮
# 滚动锁剥离(序列化前): html/body 的内联 style 带 overflow:hidden|clip(常伴 position:fixed|absolute; inset:0;
# top:-Npx; height:100%/unset; padding-right:滚动条宽)是弹层/抽屉菜单打开时脚本写的"锁滚动"状态
# (body-scroll-lock / Headless UI / Shopify 主题 等)。采集窗口里交互轨开过菜单或自动弹层, 锁就烘进交付 HTML:
# 整页只剩一屏(allbirds 手机端 原 4628px 克隆 844px, 38 分)。原站源码的 html/body 从不带这些内联声明,
# 剥掉只影响这类过渡态。只动 html/body, 其余元素不碰。
SCROLL_LOCK_STRIP_JS = r"""() => {
  const LOCK = /^(overflow(?:-[xy])?|position|inset|top|right|bottom|left|height|width|padding-right|touch-action|overscroll-behavior(?:-[xy])?|scrollbar-gutter)$/i;
  let n = 0;
  for (const el of [document.documentElement, document.body]) {
    if (!el) continue;
    const st = el.getAttribute('style') || '';
    if (!/(?:^|;)\s*overflow(?:-y)?\s*:\s*(?:hidden|clip)\b/i.test(st)) continue;
    const keep = [];
    for (const d of st.split(';')) {
      const m = d.match(/^\s*([a-z-]+)\s*:\s*([^]*)$/i);
      if (!m) { if (d.trim()) keep.push(d.trim()); continue; }
      const k = m[1].toLowerCase(), v = m[2].trim();
      const lock = LOCK.test(k) && !(k === 'position' && !/^(fixed|absolute)/i.test(v))
        && !(k === 'height' && !/^(100%|100vh|100dvh|unset|auto|initial)$/i.test(v))
        && !(k === 'width' && !/^(100%|100vw|unset|auto)$/i.test(v));
      if (lock) n++; else keep.push(k + ': ' + v);
    }
    if (keep.length) el.setAttribute('style', keep.join('; ')); else el.removeAttribute('style');
  }
  return n;
}"""


TRANSITION_FINISH_JS = r"""async () => {
  let n = 0;
  for (let round = 0; round < 3; round++) {
    let ts = [];
    try {
      ts = document.getAnimations().filter(a => typeof CSSTransition !== 'undefined' && a instanceof CSSTransition && a.playState === 'running');
    } catch (e) { break; }
    if (!ts.length) break;
    for (const a of ts) { try { a.finish(); n++; } catch (e) {} }
    await new Promise(r => setTimeout(r, 120));
  }
  return n;
}"""

DOM_COUNTS_JS = r"""() => { const im = document.images; let s = 0; for (const i of im) if (i.currentSrc) s++;
  return { text: document.body ? document.body.innerText.length : 0, imgs: im.length, imgsrc: s,
    ctrl: document.querySelectorAll('input:not([type=hidden]),textarea,select').length,
    btn: document.querySelectorAll('button,[role=button]').length, els: document.getElementsByTagName('*').length }; }"""

ALT_INJECT_JS = r"""({items, css}) => {
  // 与 CV_PATH_FN 同口径: body 这一层有唯一 id 的段写成 tag#id, 序号段只数同标签且无唯一 id 的兄弟
  const resolve = (path) => {
    let e = document.body;
    const sid = x => { const v = x.id; return v && /^[A-Za-z][\w-]{1,100}$/.test(v) && document.querySelectorAll('[id="' + v + '"]').length === 1 ? v : ''; };
    for (const seg of path ? path.split('>') : []) {
      const top = e === document.body;
      let hit = null;
      const h = seg.indexOf('#');
      if (h > 0) {
        const tag = seg.slice(0, h), id = seg.slice(h + 1);
        const c = document.getElementById(id);
        if (c && c.parentElement === e && c.tagName.toLowerCase() === tag) hit = c;
      } else {
        const [tag, n] = seg.split(':');
        let k = 0;
        for (const c of e.children) if (c.tagName.toLowerCase() === tag && !(top && sid(c)) && ++k === +n) { hit = c; break; }
      }
      if (!hit) return null;
      e = hit;
    }
    return e;
  };
  // 先全部解析出主页元素再插入(插入会改变后续兄弟的同类序号)
  const plan = items.map(it => [resolve(it.path), it]).filter(x => x[0]);
  let n = 0, fixed = 0;
  for (const [el, it] of plan) {
    el.setAttribute('data-cv-vp', it.own);
    const srcImgs = Array.from(el.querySelectorAll('img')).filter(s => s.getAttribute('src') || s.getAttribute('srcset'));
    let anchor = el;
    for (const alt of it.alts) {
      const t = document.createElement('template');
      t.innerHTML = alt.html;
      const node = t.content.firstElementChild;
      if (!node) continue;
      node.setAttribute('data-cv-vp', alt.bands);
      // 窄屏首载页抽取时脚本还没给 <img> 写 src(懒加载/按容器宽算地址的图片组件), 变体里只剩 alt 文字:
      // 用主页同子树里的对应图(alt 相同, 或数量相同按序)补上, 至少显示桌面那张而不是裂图
      const dstImgs = Array.from(node.querySelectorAll('img'));
      const allImgs = Array.from(node.querySelectorAll('img'));
      dstImgs.forEach((d, k) => {
        if (d.getAttribute('src') || d.getAttribute('srcset')) return;
        const m = (d.alt && srcImgs.find(s => s.alt === d.alt)) || (srcImgs.length === allImgs.length ? srcImgs[k] : null);
        if (!m) return;
        for (const a of ['src', 'srcset', 'sizes']) { const v = m.getAttribute(a); if (v) d.setAttribute(a, v); }
        fixed++;
      });
      anchor.after(node);
      anchor = node;
      n++;
    }
  }
  window.__cvAltImgFixed = fixed;
  if (n) {
    const st = document.createElement('style');
    st.setAttribute('data-clone-vp', 'alt');
    const hide = (b) => '[data-cv-vp]:not([data-cv-vp~="' + b + '"]){display:none!important}';
    st.textContent = '@media all/*cv-d*/{' + hide('d') + '}\n'
      + '@media (min-width: 99999px)/*cv-t*/{' + hide('t') + '}\n'
      // 手机段: 非触屏只显示带 m 的; 触屏另显示 mt(只在手机 UA 下换结构的变体)并藏起 mx(其主页版)
      + '@media (min-width: 99999px)/*cv-m*/{html:not([data-cv-touch]) [data-cv-vp]:not([data-cv-vp~="m"]),'
      + 'html[data-cv-touch] [data-cv-vp]:not([data-cv-vp~="m"]):not([data-cv-vp~="mt"]),html[data-cv-touch] [data-cv-vp~="mx"]{display:none!important}}\n'
      + (css ? '@media (min-width: 99999px)/*cv-x*/{\n' + css + '\n}' : '');
    (document.head || document.documentElement).appendChild(st);
    if (items.some(it => / mx$/.test(it.own))) {
      // 触屏标记在解析期最早设上(先于分视口样式生效): 粗指针 = 手机/平板真机
      const sc = document.createElement('script');
      sc.setAttribute('data-clone-preview', 'cv-touch');
      sc.textContent = "try{if(matchMedia('(pointer: coarse)').matches)document.documentElement.setAttribute('data-cv-touch','')}catch(e){}";
      const h = document.head || document.documentElement;
      h.insertBefore(sc, h.firstChild);
    }
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

# canvas 显示盒钉住: 缩放来回前 save 记下每个 canvas 的显示盒, 回到原宽后 restore 把高度变了(宽不变)的 canvas 按原盒比例钉住。
# 有的图表/动画库只在变宽时重设位图、变窄回来不收(vite 首页 638x339 → 548; 窄屏页 1.128 倍宽对照后 653x347 → 751x359),
# 回到原宽的页面就停在错态: 基准截图、布局校准(按错态补 min-height)、固化比例全跟着错。位图按原盒拉伸显示, 版面回到首载态
CANVAS_PIN_JS = r"""(mode) => {
  let n = 0;
  document.querySelectorAll('canvas').forEach(cv => {
    try {
      const r = cv.getBoundingClientRect();
      if (mode === 'save') { cv.__cvBox = r.width > 0 && r.height > 0 ? [r.width, r.height] : null; return; }
      const b = cv.__cvBox;
      if (!b || !(r.width > 0) || Math.abs(r.width - b[0]) > 1 || Math.abs(r.height - b[1]) <= 1) return;
      cv.style.setProperty('aspect-ratio', (Math.round(b[0] * 100) / 100) + ' / ' + (Math.round(b[1] * 100) / 100), 'important');
      cv.style.setProperty('height', 'auto', 'important');
      n++;
    } catch (e) {}
  });
  return n;
}"""

# 图片地址钉住: 缩放来回前 save 记下每张 img/source 的 src/srcset, 回到原宽后 restore 把被站点脚本清空的地址还原。
# 有的站点脚本在视口切换时把当前视口不显示的图片 src 置空(wix 首屏大图 1920→768→390→1920 走一遍后 src=""),
# 引擎的多视口采集/窄屏对照都是"来回"操作, 不还原就把空地址烘进交付物。只还原"之前有、现在空"的, 不覆盖脚本换的新地址
IMG_SRC_PIN_JS = r"""(mode) => {
  let n = 0;
  document.querySelectorAll('img, picture source').forEach(el => {
    try {
      const keys = el.tagName === 'IMG' ? ['src', 'srcset'] : ['srcset'];
      if (mode === 'save') {
        const o = {};
        keys.forEach(k => { const v = el.getAttribute(k); if (v && v.trim()) o[k] = v; });
        el.__cvSrc = o;
        return;
      }
      const o = el.__cvSrc;
      // 原宽下不渲染的(display:none 的手机版实例)不还原: 缩放前记到的多是懒加载占位图(wix blur_2 小图), 还原了反而
      // 挡住 CV_EMIT 用窄屏首载页真图地址补 src(那一步只补空 src 的)
      if (!o || !el.isConnected || !(el.tagName === 'SOURCE' ? el.parentElement && el.parentElement.getClientRects().length : el.getClientRects().length)) return;
      let hit = 0;
      keys.forEach(k => {
        const v = el.getAttribute(k);
        if (o[k] && !(v && v.trim())) { el.setAttribute(k, o[k]); hit = 1; }
      });
      n += hit;
    } catch (e) {}
  });
  return n;
}"""

# 内联态钉住(窄屏首载页 0.92/1.128 倍宽对照来回用): save 记下每个元素的全部属性, restore 把来回后变了的复原。
# 轮播/虚拟列表在改宽时按新宽重排(react-slick 换 track 宽与当前项、translate), 回到原宽不一定回到首载态, 随后抽取的
# 窄屏变体就带着来回后的错态(ant.design 手机 91→54)。只复原 save 时就在的元素; 返回复原个数
ROUNDTRIP_PIN_JS = r"""(mode) => {
  // 全部属性(不只 style/class): 轮播改宽后还会改 aria-hidden/tabindex/data-index 等, 只复原样式时当前项标记仍错位
  let n = 0;
  const els = document.body ? document.body.getElementsByTagName('*') : [];
  for (let i = 0; i < els.length && i < 30000; i++) {
    const el = els[i];
    try {
      if (mode === 'save') { const o = {}; for (const a of el.attributes) o[a.name] = a.value; el.__cvRt = o; continue; }
      const o = el.__cvRt;
      if (!o) continue;
      let hit = 0;
      // canvas 的 width/height 属性一写就清空位图(图表变白), 交给 CANVAS_PIN_JS 按显示盒钉
      const keep = el.tagName === 'CANVAS' ? /^(width|height)$/ : null;
      for (const a of Array.from(el.attributes)) if (!(a.name in o) && !(keep && keep.test(a.name))) { el.removeAttribute(a.name); hit = 1; }
      for (const k in o) if (!(keep && keep.test(k)) && el.getAttribute(k) !== o[k]) { el.setAttribute(k, o[k]); hit = 1; }
      n += hit;
    } catch (e) {}
  }
  return n;
}"""

# 缩放来回之后 width/height 属性与缩放前(CV_RECORD "d" 快照)不同的 canvas 个数
CV_CANVAS_DRIFT_JS = r"""() => {
  const m = window.__cvRec;
  if (!m || !m.attrs || !m.attrs.d) return 0;
  let n = 0;
  m.els.forEach((el, i) => {
    if (el.tagName !== 'CANVAS' || !el.isConnected) return;
    const w = el.getAttribute('width'), h = el.getAttribute('height');
    const d = m.attrs.d[i] || '';
    if (w && h && d && d !== 'width:' + (+w) + 'px;height:' + (+h) + 'px;') n++;
  });
  return n;
}"""

# 状态类相位复位: 同一组件的多个实例(同标签、去掉状态词后 class 相同)里, 状态词(open/active/expanded/current…)在采集时
# 从源码 HTML 里的那个实例"移"到了别的实例上 = 自动轮换(手风琴/卡片轮播)转到了第 k 项。克隆里站点脚本从烘进 HTML 的
# 第 k 项接着转, 原站从首载项起转, 两边永远差一个相位(wix 手机展开卡差 350px, iPhone Air 49↔70)。把这组实例的状态词
# 复位成源码里的分配, 克隆与原站从同一相位起步。只动组内状态词; 参数 src: {id: 源码 class}
STATE_PHASE_RESET_JS = r"""(arg) => {
  const src = Array.isArray(arg) ? (arg[0] || {}) : (arg || {});
  const sib = Array.isArray(arg) ? (arg[1] || []) : [];
  const ST = /(^|[-_])(is-)?(open|opened|expanded|active|show|shown|current|selected|collapsed)$/i;
  const toks = c => (c || '').split(/\s+/).filter(Boolean);
  const groups = new Map();
  for (const id of Object.keys(src || {})) {
    const el = document.getElementById(id);
    if (!el || typeof el.className !== 'string') continue;
    const live = toks(el.className), orig = toks(src[id]);
    // 含自身 id 的 class(组件框架常给每个实例加 comp-<id> 之类)是实例私有的, 不参与"同类"判定
    const own = t => t.indexOf(id) >= 0;
    const base = live.filter(t => !ST.test(t) && !own(t)).sort().join(' ');
    if (!base || base !== orig.filter(t => !ST.test(t) && !own(t)).sort().join(' ')) continue;   // 非状态 class 也变了: 不是同一种相位差
    const k = el.tagName + '|' + base;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push([el, live.filter(t => ST.test(t)), orig.filter(t => ST.test(t))]);
  }
  // 没有 id 的同类兄弟组: 源码里父元素(最近带 id 的祖先 + 子元素序号链)在活体里找到、子元素个数与标签序列都对上才用;
  // 有 id 且源码登记过的子项上面已处理
  let moved = 0, movedG = 0; const mvDbg = [];
  sib.forEach((gp, gi) => {
    let par = gp.a ? document.getElementById(gp.a) : document.body;
    for (const i of gp.i) { par = par && par.children[i]; }
    if (!par || par.children.length !== gp.k.length) return;
    for (let i = 0; i < gp.k.length; i++) if (par.children[i].tagName.toLowerCase() !== gp.k[i][0]) return;
    const usable = i => { const el = par.children[i]; return typeof el.className === 'string' && !(el.id && src[el.id] != null) && (gp.k[i][2] || '') === (el.id || ''); };
    // 不靠状态词表: 同一个 class 词从一个兄弟身上去掉、加到另一个除此之外 class 完全相同的同标签兄弟身上(组内仅此一去一加)
    // = 当前项轮换(.on/.cur/.z-crt 等各家自造的状态名), 移回源码所在的那个
    const mv = new Map();
    for (let i = 0; i < gp.k.length; i++) {
      if (!usable(i)) continue;
      const el = par.children[i], live = toks(el.className), orig = toks(gp.k[i][1]);
      const note = (t, rest, sign) => { const k = t + '|' + el.tagName + '|' + rest.filter(x => x !== t).sort().join(' '); if (!mv.has(k)) mv.set(k, {t, add: [], del: []}); mv.get(k)[sign].push(el); };
      live.forEach(t => { if (!orig.includes(t)) note(t, live, 'add'); });
      orig.forEach(t => { if (!live.includes(t)) note(t, orig, 'del'); });
    }
    for (const r of mv.values()) {
      if (r.add.length !== 1 || r.del.length !== 1 || r.add[0] === r.del[0]) continue;
      r.add[0].classList.remove(r.t); r.del[0].classList.add(r.t);
      moved += 2; movedG++;
      if (mvDbg.length < 4) mvDbg.push((gp.a ? '#' + gp.a.slice(-20) : 'body') + '>' + gp.i.join('>') + ' ' + r.add[0].tagName.toLowerCase() + '.' + r.t);
    }
    for (let i = 0; i < gp.k.length; i++) {
      const el = par.children[i], oc = gp.k[i][1];
      if (oc == null || !usable(i)) continue;
      const live = toks(el.className), orig = toks(oc);
      const base = live.filter(t => !ST.test(t)).sort().join(' ');
      if (!base || base !== orig.filter(t => !ST.test(t)).sort().join(' ')) continue;
      const k = 'P' + gi + '|' + el.tagName + '|' + base;
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push([el, live.filter(t => ST.test(t)), orig.filter(t => ST.test(t))]);
    }
  });
  let n = 0, g = 0, cand = 0; const dbg = [];
  for (const mem of groups.values()) {
    if (mem.length < 2) continue;
    if (mem.some(([, a, b]) => a.join(' ') !== b.join(' '))) { cand++; if (dbg.length < 4) dbg.push(mem.slice(0, 8).map(([el, a, b], i) => (el.id ? el.id.slice(-24) : el.tagName.toLowerCase() + '[' + i + ']') + ':' + a.join('+') + '/' + b.join('+')).join(' ')); }
    // 某状态词只在组内部分实例上加/去(轮换到第 k 项, 或源码全收起而采集时展开了其中一个)才复位;
    // 全体实例一起加/去的(整组菜单打开、主题切换)不是相位差, 不动
    const cnt = new Map();
    for (const [, a, b] of mem) {
      a.forEach(t => { if (!b.includes(t)) cnt.set('+' + t, (cnt.get('+' + t) || 0) + 1); });
      b.forEach(t => { if (!a.includes(t)) cnt.set('-' + t, (cnt.get('-' + t) || 0) + 1); });
    }
    // 只认两种确定的相位差: 状态词从一个实例移到另一个实例(轮换, +1/-1), 或源码里全组都没有、采集时恰有一个实例加上
    // (≥3 个实例的折叠卡展开了一张)。多个实例同时变化的是水合/脚本初始化给出的真实首屏态(aliyun 手机复位后 97→29), 不动
    const partial = new Set();
    const has0 = t => mem.some(([, , b]) => b.includes(t));
    for (const [k, c] of cnt) {
      const t = k.slice(1);
      const p = cnt.get('+' + t) || 0, q = cnt.get('-' + t) || 0;
      if (c >= mem.length) continue;
      if ((p === 1 && q === 1) || (k[0] === '+' && p === 1 && q === 0 && !has0(t) && mem.length >= 3)) partial.add(k);
    }
    if (!partial.size) continue;
    g++;
    for (const [el, a, b] of mem) {
      let hit = false;
      a.forEach(t => { if (!b.includes(t) && partial.has('+' + t)) { el.classList.remove(t); hit = true; } });
      b.forEach(t => { if (!a.includes(t) && partial.has('-' + t)) { el.classList.add(t); hit = true; } });
      if (hit) n++;
    }
  }
  return {groups: g + movedG, els: n + moved, cand: cand + movedG, dbg: dbg.concat(mvDbg)};
}"""

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
  // DEEP_FREEZE 已把 canvas 固化成 <img data-canvas-frozen>: 与窄屏首载页同路径的 canvas 对照时仍按 CANVAS 认
  // (vite 平板: canvas 只在挂载时按宽定高, 缩放采不到, 首载页能采到, 却因标签名不同被跳过, 克隆 img 留着 height:auto 按位图比例 312 vs 原 347)
  const tagOf = e => e.getAttribute && e.getAttribute('data-canvas-frozen') ? 'CANVAS' : e.tagName;
  const bands = {d: [], t: [], m: []};
  const px = v => { const r = /^(-?[\d.]+)px$/.exec(v || ''); return r ? parseFloat(r[1]) : null; };
  const tags = ['t', 'm'].filter(t => m.snaps[t]);
  const attrOf = __CV_ATTR__;
  const S0 = (t, i) => m.snaps[t][i] + ((m.attrs && m.attrs[t] && m.attrs[t][i]) ? ';' + m.attrs[t][i] : '');
  // 固化 canvas: 图表/动画库在缩放来回后按当时(布局未稳)量到的盒子重设了位图尺寸(vite 首页 638x339 → 638x548),
  // 固化时拿到的是来回之后的位图, img 按它的比例占 548 高, 桌面就比原站高 209px、其下整页错位; 且"还原后没回到原值"
  // 让这个 canvas 的平板/手机段也一条写不出。以来回之前(d)的尺寸为准: img 盒按 d 的比例, 还原快照视同 d
  const bFix = new Set();
  m.els.forEach((el, i) => {
    if (!el.isConnected || !(el.getAttribute && el.getAttribute('data-canvas-frozen')) || !m.attrs || !m.attrs.d || !m.attrs.b) return;
    const dim = s => { const w = /width:([\d.]+)px/.exec(s || ''), h = /height:([\d.]+)px/.exec(s || ''); return w && h ? [+w[1], +h[1]] : null; };
    const dd = dim(m.attrs.d[i]), bb = dim(m.attrs.b[i]);
    if (!dd || !bb || (dd[0] === bb[0] && dd[1] === bb[1]) || m.snaps.d[i] !== m.snaps.b[i] || !dd[0] || !dd[1]) return;
    el.setAttribute('width', String(dd[0])); el.setAttribute('data-cv-h', String(dd[1]));
    el.style.aspectRatio = dd[0] + ' / ' + dd[1];
    bFix.add(i); out.cvFix = (out.cvFix || 0) + 1;
  });
  const S = (t, i) => S0(t === 'b' && bFix.has(i) ? 'd' : t, i);
  // 分视口 class: 缩放快照里该视口 class 与桌面不同且还原后回到桌面值 → 该视口用缩放快照的 class;
  // 缩放没变而窄屏首载页同路径同标签元素 class 不同 → 只在加载时按宽度决定 class, 用首载值
  const C = m.cls || {}, swaps = [];
  const ROOTUA = 1;
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
      if (v === cd || !L || L[0] !== tagOf(el) || L[1] !== v || /[<>"]/.test(v)) return;
      const a = new Set(cd.split(/\s+/)), b = new Set(v.split(/\s+/));
      const delta = [...a].filter(x => !b.has(x)).concat([...b].filter(x => !a.has(x)));
      if (delta.some(x => STATE.test(x))) return;
      alt[t] = v;
    });
    if (Object.keys(alt).length) swaps.push([el, alt]);
  });
  // <html>/<body>: 缩放快照不含根元素; 站点常在加载时按宽度给根元素打设备类(DevDocs 的 _mobile), resize 不再改,
  // 只能看窄屏首载页。根元素上的轮播/曝光类状态极少, 仍排除状态类 token
  [['@html', document.documentElement], ['@body', document.body]].forEach(([k, el]) => {
    if (!el) return;
    const cd = el.getAttribute('class') || '', alt = {};
    tags.forEach(t => {
      const L = loads[t] && loads[t][k];
      if (!L || L[0] !== el.tagName || L[1] === cd || /[<>"]/.test(L[1])) return;
      const a = new Set(cd.split(/\s+/)), b = new Set(L[1].split(/\s+/));
      const delta = [...a].filter(x => !b.has(x)).concat([...b].filter(x => !a.has(x)));
      if (delta.some(x => STATE.test(x))) return;
      // 手机 UA 首载页变了、桌面 UA 同宽首载页(__mdesk)与主页相同: 是按 UA/触屏写的设备类(touch / is-mobile),
      // 不是按宽度 —— 只给触屏手机段(mt), 桌面浏览器窄窗口不带(合成用例 device_touch 手机 390 38.7)
      const MDk = ROOTUA && t === 'm' && loads.__mdesk && loads.__mdesk[k];
      if (MDk && MDk[0] === L[0] && MDk[1] === cd) { alt.mt = L[1]; return; }
      alt[t] = L[1];
    });
    if (Object.keys(alt).length) swaps.push([el, alt]);
  });
  // 只随 UA 变的 class(触屏): 手机 UA 首载页与桌面 UA 同宽首载页(loads.__mdesk)同路径元素 class 不同、桌面 UA 同宽页
  // 与主页相同 → 宽度没改它, 是站点按设备类型(触屏/手机 UA)写的(wix 手机上卡片默认收起, 桌面展开第一张)。
  // 只给触屏手机段(mt)。含状态词的变化要排除轮换: 同一组件(同标签、去状态词后同 class)里有别的实例反向变了同一个词 → 是时间相位不是 UA
  const LM = loads.m, MD = loads.__mdesk;
  if (C.d && LM && MD && m.paths) {
    const has = new Set(swaps.map(([e]) => e));
    const keyOf = (tg, c) => tg + '|' + c.split(/\s+/).filter(x => x && !STATE.test(x)).sort().join(' ');
    const flips = {};
    for (const p in LM) {
      const a = LM[p], b = MD[p];
      if (!a || !b || a[0] !== b[0] || a[1] === b[1]) continue;
      const A = new Set(a[1].split(/\s+/)), B = new Set(b[1].split(/\s+/));
      const k = keyOf(a[0], b[1]);
      for (const x of A) if (x && !B.has(x) && STATE.test(x)) (flips[k + '+' + x] = (flips[k + '+' + x] || 0) + 1);
      for (const x of B) if (x && !A.has(x) && STATE.test(x)) (flips[k + '-' + x] = (flips[k + '-' + x] || 0) + 1);
    }
    let nt = 0;
    m.els.forEach((el, i) => {
      if (nt >= 2000 || has.has(el) || !el.isConnected || el.closest('svg')) return;
      const cd = C.d[i], p = m.paths[i], L = LM[p], D = MD[p];
      if (!L || !D || C.b[i] !== cd || (el.getAttribute('class') || '') !== cd) return;
      if (L[0] !== tagOf(el) || D[0] !== L[0] || D[1] !== cd || L[1] === cd || /[<>"]/.test(L[1])) return;
      const a = new Set(cd.split(/\s+/)), b = new Set(L[1].split(/\s+/));
      const k = keyOf(L[0], cd);
      const lost = [...a].filter(x => x && !b.has(x)), got = [...b].filter(x => x && !a.has(x));
      if (lost.some(x => STATE.test(x) && flips[k + '+' + x]) || got.some(x => STATE.test(x) && flips[k + '-' + x])) return;
      swaps.push([el, {mt: L[1]}]); nt++;
    });
    if (nt) out.clsTouch = nt;
    // 只随 UA 的状态词: 桌面 UA 同宽首载页有、手机 UA 首载页没有(wix 卡片桌面加载后自动展开第一张, 手机不展开)。
    // 克隆跑的是桌面版脚本, 加载后(或滚到时)会照桌面逻辑再加上 → 触屏手机段上用户点按/按键前把这些词拿掉
    // (data-cv-touch-off, 运行时守住; 滑动滚动不算操作: 手机上滚动不会展开, 只有点按会)
    let no = 0;
    m.els.forEach((el, i) => {
      if (no >= 200 || !el.isConnected || el.closest('svg')) return;
      const p = m.paths[i], L = LM[p], D = MD[p];
      if (!L || !D || L[0] !== D[0] || L[0] !== tagOf(el) || L[1] === D[1]) return;
      const a = new Set(L[1].split(/\s+/)), k = keyOf(D[0], D[1]);
      const off = D[1].split(/\s+/).filter(x => x && !a.has(x) && STATE.test(x) && !flips[k + '+' + x]);
      if (!off.length || /[<>"]/.test(off.join(' '))) return;
      el.setAttribute('data-cv-touch-off', off.join(' ')); no++;
    });
    if (no) out.touchOff = no;
  }
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
        if (v === xd || !L || L[0] !== tagOf(el) || (L[5] || '') !== v) return;
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
  // 固化 canvas 的分视口对照诊断: 图表库按宽算高不成比例时平板/手机盒高靠这条链, 哪一环没接上在 engine.log 里能看出
  const cvdbg = [];
  m.els.forEach((el, i) => {
    if (!el.isConnected || !(el.getAttribute && el.getAttribute('data-canvas-frozen')) || cvdbg.length >= 6) return;
    const L = {};
    for (const t of tags) { const x = loads[t] && m.paths ? loads[t][m.paths[i]] : null; L[t] = x ? [x[0], x[1].slice(0, 30), x[2], x[4]] : null; }
    cvdbg.push({p: (m.paths ? m.paths[i] : '').slice(-60), d: S('d', i), b: S('b', i), t: m.snaps.t ? S('t', i) : null, m: m.snaps.m ? S('m', i) : null,
      cur: (el.getAttribute('style') || '').slice(0, 80) + ';' + attrOf(el), cls: String(el.className || '').slice(0, 30), L});
  });
  if (cvdbg.length) out.cvdbg = cvdbg;
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
      if (!L || L[0] !== tagOf(el)) return;
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
      // 分段同比: 脚本按容器/视口宽算的字号或尺寸被上限(桌面)或下限(手机)夹住时, 三点不同比, 但相邻两段
      // 仍与宽度同比(值不同、值/宽相同) → 这两段写 vw, 否则手机段会被钉在 390 采集宽的 px
      // (420 宽真机全站字号统一小 7%: 390/420)。夹住的那段照旧写 px
      const nmap = Object.fromEntries(nums);
      const same = (a, b) => nmap[a] !== null && nmap[b] !== null && nmap[a] !== nmap[b] && nmap[a] !== 0 && nmap[b] !== 0
        && Math.abs(nmap[a] / W[a] - nmap[b] / W[b]) <= Math.abs(nmap[a] / W[a]) * 0.015;
      const fluid = new Set();
      if (tags.length === 2) {
        if (same('t', 'm')) { fluid.add('t'); fluid.add('m'); }
        else if (same('d', 't')) { fluid.add('d'); fluid.add('t'); }
      }
      // 手机段与 0.92 倍宽首载对照页(loads.__pair.m)同比: 平板/手机两段不同比(换了列数)时三点看不出, 但 390 与 359
      // 两张首载页在同一布局里, 值比≈宽比就是"按宽算的尺寸"(wix 卡片图 height 289px → 420 宽原站 311), 手机段写 vw
      const pairM = loads.__pair && loads.__pair.m;
      const pairVal = (k) => {
        if (k !== 'm' || !pairM || !m.paths || nmap.m === null || nmap.m === undefined || nmap.m === 0) return null;
        const L2 = pairM.paths && pairM.paths[m.paths[i]];
        if (!L2 || L2[0] !== tagOf(el) || L2[0] === 'CANVAS') return null;   // canvas 位图尺寸改宽即重设, 不是首载写一次
        const PV = parse(L2[2] + (L2[4] ? ';' + L2[4] : ''));
        const pv = PV[p] ? px(PV[p][0]) : null;
        if (pv === null || pv === 0 || pv === nmap.m) return null;
        return Math.abs(pv / pairM.w - nmap.m / W.m) <= Math.abs(nmap.m / W.m) * 0.015 ? (nmap.m / W.m * 100).toFixed(4) + 'vw' : null;
      };
      // 固化 canvas「撑满容器、宽高比恒定」: 桌面采集时显示宽 = 父元素内容宽, 且该段首载页量到的宽高比与桌面相同(±1.5%)
      // → 平板/手机段不钉首载那一个宽度的 px(768 采的 672px 到 820 宽的 iPad 上窄一截、390 采的到 360 宽溢出),
      // 写 width:100% + height:auto, 由内联 aspect-ratio 定高。宽高比随段变化的(图表库按宽另算高)照旧钉 px
      const CVFILL = 1;
      const cvFill = (k) => {
        if (!CVFILL || k === 'd' || (p !== 'width' && p !== 'height') || !el.getAttribute('data-canvas-frozen')) return false;
        const g = (o, q) => (o && o[q]) ? px(o[q][0]) : null;
        const dw = g(D, 'width'), dh = g(D, 'height'), w = g(V[k], 'width'), h = g(V[k], 'height');
        if (!dw || !dh || !w || !h || w === dw || Math.abs((h / w) / (dh / dw) - 1) > 0.015) return false;
        const pe = el.parentElement; if (!pe) return false;
        const pc = getComputedStyle(pe);
        return Math.abs(pe.clientWidth - (parseFloat(pc.paddingLeft) || 0) - (parseFloat(pc.paddingRight) || 0) - dw) <= 1.5;
      };
      const emit = (k, v) => {
        if (!v) return;
        if (cvFill(k)) { rule[k].push(p === 'width' ? 'width:100%' : 'height:auto'); out.cvFill = (out.cvFill || 0) + 1; return; }
        // 固化 canvas 的桌面段不钉 px: 桌面段靠 aspect-ratio 随宽缩(采集宽之外的桌面宽度也对), 只给平板/手机段写首载页量到的高
        if (k === 'd' && p === 'height' && el.getAttribute('data-canvas-frozen')) return;
        let val = fluid.has(k) ? (nmap[k] / W[k] * 100).toFixed(4) + 'vw' : v[0];
        if (!fluid.has(k)) { const pv = pairVal(k); if (pv) { val = pv; out.pairvw = (out.pairvw || 0) + 1; if ((out.pairdbg = out.pairdbg || []).length < 12) out.pairdbg.push([(m.paths && m.paths[i] || '').slice(-80), el.tagName, p, v[0], pv]); } }
        rule[k].push(p + ':' + val + (v[1] ? '!important' : ''));
      };
      emit('d', dv);
      tags.forEach(t => emit(t, V[t][p]));
      if (fluid.size) out.vw++;
      out.banded++; touched = true;
    }
    if (!touched) return;
    out.els++;
    if (rule.d.length + rule.t.length + rule.m.length) {
      el.setAttribute('data-cv', String(i));
      for (const k of ['d', 't', 'm']) if (rule[k].length) bands[k].push('[data-cv="' + i + '"]' + SPEC + '{' + rule[k].join(';') + '}');
    }
  });
  // 桌面上没渲染(display:none 的手机版实例)的元素: 没 src 的媒体用窄屏首载页同路径元素的 src 补, 空叶子用其文本补。
  // 脚本只给可见元素写图片地址/跑计数动画(wix 手机首屏图只剩 alt 文字、"300M+" 为空), 桌面采集永远拿不到;
  // 这些元素桌面本就不显示, 补上不影响桌面
  let srcFill = 0, textFill = 0;
  if (m.paths && tags.length) m.els.forEach((el, i) => {
    if (!el.isConnected || el.getClientRects().length || el.closest('svg')) return;
    for (const t of ['m', 't']) {
      const L = loads[t] && loads[t][m.paths[i]];
      if (!L || L[0] !== tagOf(el)) continue;
      if (L[6] && !el.getAttribute('src') && !el.getAttribute('srcset')) {
        const [s, ss, po] = L[6].split('\x01');
        if (s && !/^(data:|blob:|javascript:)/i.test(s)) el.setAttribute('src', s);
        if (ss) el.setAttribute('srcset', ss);
        if (po) el.setAttribute('poster', po);
        srcFill++; break;
      }
      if (L[7] && !el.children.length && !(el.textContent || '').trim() && !/^(SCRIPT|STYLE|TEXTAREA|INPUT|IFRAME)$/.test(el.tagName)
          && !/[<>]/.test(L[7])) {
        el.textContent = L[7]; textFill++; break;
      }
    }
  });
  if (srcFill) out.srcFill = srcFill;
  if (textFill) out.textFill = textFill;
  if (out.cls || out.xattr || out.touchOff) {
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

# 分视口 class/属性运行时(随 CV_EMIT_JS 写入页面): 读 --cv-band 决定当前段, 把 data-cv-cls-<段> 换成 class;
# 解析期由 MutationObserver 逐元素即时应用(不等 DOMContentLoaded): 站点脚本(defer/module 打包, 跑在 DOMContentLoaded 前)初始化手风琴/标签页/轮播时读到的
# class/aria 态已是本视口段的, 否则会按烘进 HTML 的桌面态(aria-expanded=true 等)把窄屏上该折叠的内容展开;
# 段没变不动 DOM。活体采集时段恒为 d(桌面), 不改变采集结果。
CV_CLS_RUNTIME = r"""(function(){var cur=null;
function band(){try{return (getComputedStyle(document.documentElement).getPropertyValue('--cv-band')||'').trim();}catch(x){return '';}}
function one(e,b){try{
if(e.hasAttribute('data-cv-cls-d')){var v=e.getAttribute('data-cv-cls-'+b);if(v===null&&b==='mt')v=e.getAttribute('data-cv-cls-m');if(v===null)v=e.getAttribute('data-cv-cls-d');v=keepTouch(e,b,v);if(e.getAttribute('class')!==v)e.setAttribute('class',v);}
if(e.hasAttribute('data-cv-xa-d')){var o=JSON.parse(e.getAttribute('data-cv-xa-d')),bv=e.getAttribute('data-cv-xa-'+(b==='mt'?'m':b));if(bv!==null){var q=JSON.parse(bv);for(var k in q)o[k]=q[k];}for(k in o){if(o[k]===null)e.removeAttribute(k);else if(e.getAttribute(k)!==o[k])e.setAttribute(k,o[k]);}}
}catch(x){}}
function all(b){var es=document.querySelectorAll('[data-cv-cls-d],[data-cv-xa-d]');for(var i=0;i<es.length;i++)one(es[i],b);}
function touch(){try{return matchMedia('(pointer: coarse)').matches;}catch(x){return false;}}
var mtSeen=typeof WeakSet==='function'?new WeakSet():null;
function keepTouch(e,b,v){if(b==='mt'){if(mtSeen)mtSeen.add(e);return v;}if(!mtSeen||mtSeen.has(e)||!e.hasAttribute('data-cv-cls-mt')||!touch())return v;var mt=(e.getAttribute('data-cv-cls-mt')||'').split(/\s+/),d=' '+(e.getAttribute('data-cv-cls-d')||'')+' ',cur=' '+(e.getAttribute('class')||'')+' ',vv=' '+v+' ';
for(var i=0;i<mt.length;i++){var x=mt[i];if(x&&d.indexOf(' '+x+' ')<0&&cur.indexOf(' '+x+' ')>=0&&vv.indexOf(' '+x+' ')<0)v=(v?v+' ':'')+x;}return v;}
function bt(){var b=band();if(b==='m'&&touch())b='mt';return b;}
var gest=false;try{['click','keydown'].forEach(function(n){addEventListener(n,function(ev){if(ev.isTrusted)gest=true;},true);});}catch(x){}
function off(e){if(gest||(cur||bt())!=='mt')return;var w=(e.getAttribute('data-cv-touch-off')||'').split(' ');for(var i=0;i<w.length;i++)if(w[i]&&e.classList.contains(w[i]))e.classList.remove(w[i]);}
function offAll(){var es=document.querySelectorAll('[data-cv-touch-off]');for(var i=0;i<es.length;i++)off(es[i]);}
try{new MutationObserver(function(ms){if(gest||(cur||bt())!=='mt')return;for(var i=0;i<ms.length;i++){var t=ms[i].target;if(t.nodeType===1&&t.hasAttribute('data-cv-touch-off'))off(t);}}).observe(document.documentElement,{attributes:true,attributeFilter:['class'],subtree:true});}catch(x){}
function apply(){var b=bt()||'d';if(b!==cur){cur=b;all(b);}offAll();}
try{new MutationObserver(function(ms){var b=cur||bt();if(!b)return;for(var i=0;i<ms.length;i++){var ad=ms[i].addedNodes;for(var j=0;j<ad.length;j++){var n=ad[j];if(n.nodeType!==1)continue;one(n,b);if(n.hasAttribute('data-cv-touch-off'))off(n);if(n.children&&n.children.length){var es=n.querySelectorAll('[data-cv-cls-d],[data-cv-xa-d],[data-cv-touch-off]');for(var k=0;k<es.length;k++){one(es[k],b);if(es[k].hasAttribute('data-cv-touch-off'))off(es[k]);}}}}}).observe(document.documentElement,{childList:true,subtree:true});}catch(x){}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',apply);else apply();window.addEventListener('resize',apply);window.addEventListener('load',apply);})();"""
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
  const AD_SRC = /doubleclick\.net|googlesyndication\.com|adservice\.google|amazon-adsystem\.com|adnxs\.com|taboola\.com|outbrain\.com|criteo\.(?:com|net)|pubmatic\.com|rubiconproject\.com|carbonads\.net|buysellads\.(?:net|com)/i;
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
  // Carbon/BuySellAds(开发者站常见, 非 iframe 投放): 容器 id 固定为 carbonads, 素材来自 srv.carbonads.net / cdn4.buysellads.net
  document.querySelectorAll('#carbonads, [id^="carbonads"]').forEach(add);
  document.querySelectorAll('img[src]').forEach(im => { if (AD_SRC.test(im.getAttribute('src') || '')) add(im.closest('#carbonads, [id^="carbonads"], a') || im); });
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
  // 同源营销弹层(邮件订阅/优惠券/下载 App 的模态框 + 遮罩): 活体按定时/滚动弹出, 采集期多半没弹或已被
  // 关掉, 克隆里没有; 评分时两侧同隐藏。只认"模态"证据: fixed、z-index ≥ 100、盖住 ≥85% 宽 × ≥80% 高,
  // 且自身或后代带 role=dialog / aria-modal, 或含输入框 + 关闭钮。Cookie 条(贴底, 不盖高)与导航抽屉(默认关)不中
  const hideEl = e => {
    if (done.has(e)) return;
    e.setAttribute('data-clone-ihide', e.style.getPropertyValue('display') + '|' + e.style.getPropertyPriority('display'));
    e.style.setProperty('display', 'none', 'important');
    done.add(e); n++;
  };
  const modalish = e => (e.matches('[role=dialog],[role=alertdialog],[aria-modal=true]') ||
    e.querySelector('[role=dialog],[role=alertdialog],[aria-modal=true]') ||
    (e.querySelector('input:not([type=hidden]):not([type=checkbox]):not([type=radio]),select,textarea') &&
     e.querySelector('button[aria-label*=close i],[class*=close i],button[aria-label*=关闭],[aria-label*=dismiss i]')));
  const fixeds = [];
  for (const e of document.body ? document.body.querySelectorAll('div,section,aside,dialog,form') : []) {
    if (done.has(e)) continue;
    const cs = getComputedStyle(e);
    if (cs.position !== 'fixed' || cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) continue;
    const z = parseInt(cs.zIndex, 10);
    if (!(z >= 100)) continue;
    const r = e.getBoundingClientRect();
    if (r.width < vw * 0.85 || r.height < vh * 0.8) continue;
    if (fixeds.some(f => f.contains(e))) continue;   // 外层已记
    fixeds.push(e);
  }
  // 第三方营销弹窗 iframe(attentive/klaviyo 等): iframe 自身 fixed 铺满视口(z 往往是 auto), 里面是跨域文档看不到
  // role/输入框; 采集时按埋点沙盒置空或根本没弹, 克隆里没有 → 评分时两侧都藏 iframe 本身
  for (const f of document.querySelectorAll('iframe')) {
    if (done.has(f)) continue;
    const cs = getComputedStyle(f);
    if (cs.position !== 'fixed' || cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) continue;
    const r = f.getBoundingClientRect();
    if (r.width >= vw * 0.85 && r.height >= vh * 0.8) hideEl(f);
  }
  for (const e of fixeds) {
    if (!modalish(e)) {
      // 纯遮罩: 要有同层级的模态兄弟(对话框与遮罩分开挂)才算营销弹层
      const sib = Array.from((e.parentElement || document.body).children).find(x => {
        if (x === e || getComputedStyle(x).position !== 'fixed' || !modalish(x)) return false;
        const q = x.getBoundingClientRect();
        return q.width >= vw * 0.6 && q.height >= vh * 0.3;   // 模态框本体尺寸, 贴底的 Cookie 条不算
      });
      if (!sib) continue;
      hideEl(sib);
    }
    hideEl(e);
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

# 逐元素"随宽同比"探测(窄屏采集): 0.92 倍宽另开页面对照, 计算字号与宽度同比的元素字体烘焙不烘字号/
# 行高/字距; 内联 px 几何值与宽度同比的改写成 vw。站点按容器宽算字号(wix 手机版 h1 = 宽 × 0.1056)
# 时, 390 采集 → 420/440 真机全站字号钉在 390 的值(小 7~11%, 整页逐段上移, 像素分 ~40)。
# 读: {路径: [标签, 计算字号 px, 内联 style]}
FLUID_READ_JS = r"""() => {
  const out = {}, pathOf = """ + CV_PATH_FN + r""";
  const els = Array.from(document.body ? document.body.querySelectorAll('*') : []).slice(0, 20000);
  for (const e of els) {
    if (e.closest('svg')) continue;
    let st = e.getAttribute('style') || '';
    // 替换元素的 width/height 属性也是按宽算出来写死的几何(wix 手机图 325x215 在 360~440 宽都不变):
    // 以伪声明并入, 同比探测一并认, 应用时写成 style 的 vw(属性不接受 vw)
    if (/^(IMG|VIDEO|CANVAS|IFRAME)$/.test(e.tagName)) {
      const aw = e.getAttribute('width'), ah = e.getAttribute('height');
      if (aw && /^\d+(\.\d+)?$/.test(aw)) st += ';attr-width:' + aw + 'px';
      if (ah && /^\d+(\.\d+)?$/.test(ah)) st += ';attr-height:' + ah + 'px';
    }
    let fs = 0;
    if (e.firstChild || st) fs = parseFloat(getComputedStyle(e).fontSize) || 0;
    out[pathOf(e)] = [e.tagName, fs, st];
  }
  return out;
}"""
# 写: {paths: [字号随宽的路径], inline: {路径: {属性: 'Xvw'}}} → 标 data-cv-fluidsz(烘焙跳过字号类), 内联改 vw
FLUID_APPLY_JS = r"""({paths, inline}) => {
  // 与 CV_PATH_FN 同口径: body 这一层有唯一 id 的段写成 tag#id, 序号段只数同标签且无唯一 id 的兄弟
  const resolve = (path) => {
    let e = document.body;
    const sid = x => { const v = x.id; return v && /^[A-Za-z][\w-]{1,100}$/.test(v) && document.querySelectorAll('[id="' + v + '"]').length === 1 ? v : ''; };
    for (const seg of path ? path.split('>') : []) {
      const top = e === document.body;
      let hit = null;
      const h = seg.indexOf('#');
      if (h > 0) {
        const tag = seg.slice(0, h), id = seg.slice(h + 1);
        const c = document.getElementById(id);
        if (c && c.parentElement === e && c.tagName.toLowerCase() === tag) hit = c;
      } else {
        const [tag, n] = seg.split(':');
        let k = 0;
        for (const c of e.children) if (c.tagName.toLowerCase() === tag && !(top && sid(c)) && ++k === +n) { hit = c; break; }
      }
      if (!hit) return null;
      e = hit;
    }
    return e;
  };
  let marked = 0, vw = 0;
  for (const p of paths || []) { const e = resolve(p); if (e) { e.setAttribute('data-cv-fluidsz', ''); marked++; } }
  for (const [p, props] of Object.entries(inline || {})) {
    const e = resolve(p);
    if (!e) continue;
    for (const [k, v] of Object.entries(props)) {
      const prop = k.startsWith('attr-') ? k.slice(5) : k;
      e.style.setProperty(prop, v, e.style.getPropertyPriority(prop)); vw++;
    }
  }
  return {marked, vw};
}"""


# 回顶(采集/评分前): html{scroll-behavior:smooth} 下 scrollTo(0,0) 是动画, 等 400ms 未必到顶, 截图/快照就带着滚动偏移。
# behavior:'instant' 显式覆盖 CSS 的 smooth(不改 DOM 的 style 属性, 序列化不受影响)
SCROLL_TOP_JS = r"""() => { window.scrollTo({top: 0, left: 0, behavior: 'instant'}); }"""


# 开关落在定义模块里: 各 mixin 都直接从本模块导入, 在别处重绑定只改到那个模块的名字(合并拆模块时实测踩过)
import config as _config  # noqa: E402

if not getattr(_config, "VARIANT_CANVAS_CANON", False):
    CV_PATHS_JS = CV_PATHS_JS.replace("const CANON = 1;", "const CANON = 0;")
if not getattr(_config, "VARIANT_CANVAS_FILL", True):
    ALT_EXTRACT_JS = ALT_EXTRACT_JS.replace("const VFILL = 1;", "const VFILL = 0;")
if not getattr(_config, "ROOT_CLS_UA_SPLIT", True):
    CV_EMIT_JS = CV_EMIT_JS.replace("const ROOTUA = 1;", "const ROOTUA = 0;")
