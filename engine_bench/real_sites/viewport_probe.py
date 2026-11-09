"""多视口还原度探针: 桌面视口采集的克隆, 在平板/手机视口下与原站同宽对比。

克隆按 1920 桌面视口采集, 但交付物会在各种宽度下被打开(预览面板的平板/手机切换、
真机访问)。本探针对每个已完成任务, 在同一 UA 下以若干视口宽度分别渲染原站与克隆
预览(/preview/<id>/), 用引擎同款 compute_visual_similarity 打分。

为把"响应式还原缺陷"与"活体原站随机性"分开, 默认同时测 desktop 宽(1920):
desktop 探针分是本方法的噪声基线, tablet/mobile 与它的差才是视口适配差距。

用法:
  python3 engine_bench/real_sites/viewport_probe.py --base http://127.0.0.1:8897 --tasks 536,529
  python3 engine_bench/real_sites/viewport_probe.py --run runs/run_xxx.json   # 跑分产物里的全部完成任务
被 run_corpus.py --viewports 复用(probe_tasks)。
"""
import argparse
import json
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

PROBE_VIEWPORTS = {
    "desktop": {"width": 1920, "height": 1080},
    "tablet": {"width": 768, "height": 1024},
    "mobile": {"width": 390, "height": 844},
    # 真机口径: 移动 UA + 触屏 + DPR + 视口 meta 生效(is_mobile)。服务端按 UA 分发移动版、
    # (hover:none)/(pointer:coarse) 媒体查询、JS 嗅探 UA 的站, 只有这样才与真机看到的一致
    "iphone-air": {"width": 420, "height": 912},
    "ipad-air": {"width": 820, "height": 1180},
}
IOS_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1")
# iPadOS 13 起 Safari 默认「桌面级浏览」: 发 Mac Safari UA(不含 iPad/Mobile), 靠触屏区分
IPAD_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
           "(KHTML, like Gecko) Version/18.6 Safari/605.1.15")
# 主流安卓机型 UA(Chrome 移动版 / 三星浏览器 / 鸿蒙; 型号段按真机)
ANDROID_UA = ("Mozilla/5.0 (Linux; Android 15; {model}) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36")
SAMSUNG_UA = ("Mozilla/5.0 (Linux; Android 15; SAMSUNG SM-S931B) AppleWebKit/537.36 "
              "(KHTML, like Gecko) SamsungBrowser/27.0 Chrome/125.0.0.0 Mobile Safari/537.36")
HARMONY_UA = ("Mozilla/5.0 (Phone; OpenHarmony 5.0) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/114.0.0.0 Safari/537.36 ArkWeb/4.1.6.1 Mobile HuaweiBrowser/5.0.1.300")
DEVICE_OPTS = {
    "iphone-air": {"user_agent": IOS_UA, "device_scale_factor": 3, "is_mobile": True, "has_touch": True},
    "ipad-air": {"user_agent": IPAD_UA, "device_scale_factor": 2, "is_mobile": True, "has_touch": True},
}
# 主流手机机型矩阵(CSS 像素视口 × DPR × UA): 名字可直接用于 --viewports, 也可写 phones 展开全部
PHONE_MATRIX = {
    # 苹果
    "iphone-se": (375, 667, 2, IOS_UA),
    "iphone-15": (393, 852, 3, IOS_UA),
    "iphone-16-pro": (402, 874, 3, IOS_UA),
    "iphone-16-pro-max": (440, 956, 3, IOS_UA),
    # 安卓
    "pixel-8": (412, 915, 2.625, ANDROID_UA.format(model="Pixel 8")),
    "galaxy-s24": (360, 780, 3, SAMSUNG_UA),
    "xiaomi-14": (393, 873, 2.75, ANDROID_UA.format(model="24031PN0DC")),
    "huawei-mate60": (360, 780, 3, HARMONY_UA),
    "oppo-find-x7": (412, 915, 2.625, ANDROID_UA.format(model="PHZ110")),
}
for _n, (_w, _h, _dpr, _ua) in PHONE_MATRIX.items():
    PROBE_VIEWPORTS[_n] = {"width": _w, "height": _h}
    DEVICE_OPTS[_n] = {"user_agent": _ua, "device_scale_factor": _dpr, "is_mobile": True, "has_touch": True}
VIEWPORT_GROUPS = {
    "phones": ["iphone-air"] + list(PHONE_MATRIX),                     # 全机型表(发版前)
    "phones4": ["galaxy-s24", "iphone-15", "pixel-8", "iphone-16-pro-max"],   # 4 档代表宽度 360/393/412/440(日常回归)
}


def expand_viewports(names):
    """--viewports 里的组名(phones)展开为机型列表, 去重保序。"""
    out = []
    for n in names:
        for v in VIEWPORT_GROUPS.get(n, [n]):
            if v not in out:
                out.append(v)
    return out


def context_opts(name):
    """视口名 -> new_context 参数(桌面口径: 桌面 UA、DPR 1; 真机口径见 DEVICE_OPTS)。"""
    opts = {"viewport": PROBE_VIEWPORTS[name], "user_agent": UA, "device_scale_factor": 1}
    opts.update(DEVICE_OPTS.get(name, {}))
    return opts
MAX_SCREENS = 8          # 每侧最多截前 N 屏: 长页尾部(推荐流/页脚)对适配判断信息量低
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 两侧同口径: 停动画/过渡, 滚动一遍触发懒加载, 回顶
SETTLE_CSS = ("*,*::before,*::after{animation-play-state:paused!important;"
              "transition:none!important;caret-color:transparent!important}")
# 每步等 320ms(与引擎采集滚动同节奏): 无限推荐流在快滚下来不及续页, 原站只量到首屏长度,
# 而用户往下滑时流会续上(克隆里冻结的就是续上后的样子), 快滚会把这当成克隆"多出来"的内容
SCROLL_JS = r"""
async (maxH) => {
  const h = () => Math.min(document.documentElement.scrollHeight, maxH);
  // html{scroll-behavior:smooth} 下 scrollTo 是动画, 400ms 回不到顶(solidjs 克隆截在 -1650px); 滚动期间强制 auto, 回顶后等到 scrollY==0
  const go = y => scrollTo({top: y, left: 0, behavior: 'instant'});
  for (let y = 0; y < h(); y += innerHeight * 0.8) { go(y); await new Promise(r => setTimeout(r, 320)); }
  go(0); await new Promise(r => setTimeout(r, 400));
  for (let i = 0; i < 12 && scrollY; i++) { go(0); await new Promise(r => setTimeout(r, 100)); }
  return document.documentElement.scrollWidth;
}
"""
# 截图区内的图全部转 eager 并等到齐(封顶), 视频停帧, 字体就绪: 两侧同口径, 减少"一侧图没到"的噪声
SETTLE_MEDIA_JS = r"""
async ([maxH, capMs]) => {
  const t0 = performance.now();
  try { document.querySelectorAll('video').forEach(v => { try { v.pause(); } catch (e) {} }); } catch (e) {}
  const inArea = im => { const r = im.getBoundingClientRect(); return r.width > 1 && r.height > 1 && r.top + scrollY < maxH; };
  for (const im of document.images) if (im.loading === 'lazy' && inArea(im)) im.loading = 'eager';
  try { await Promise.race([document.fonts.ready, new Promise(r => setTimeout(r, capMs))]); } catch (e) {}
  while (performance.now() - t0 < capMs) {
    const pending = Array.from(document.images).filter(im => !im.complete && inArea(im)).length;
    if (!pending) break;
    await new Promise(r => setTimeout(r, 150));
  }
  return Math.round(performance.now() - t0);
}
"""


# 诊断: 该视口下加载失败/4xx+ 的请求、样式表装载情况、脚本错误 —— 远程只能读日志时, 这是
# 定位"手机宽度下图裂/样式丢"的直接证据(产物存储域名在部分环境不可达)
# 对侧点名: 原站里带 0px 行的 grid 容器, 在克隆里同路径元素(按 data-pi 下标)也转储命中规则, 两侧对照看少了/多了哪条
# 带 id 的元素里 class 含状态词的(及源码里可能有而运行时已去掉的: 全部带 id 且 class 非空的前 3000 个都给, Python 侧比源码)
STATE_CLS_JS = r"""() => { const o = []; for (const e of document.querySelectorAll('[id][class]')) { if (o.length >= 3000) break; const c = typeof e.className === 'string' ? e.className : (e.getAttribute('class') || ''); if (e.id && c) o.push([e.id, c]); } return o; }"""
FOCUS_RULES_JS = r"""(idxs) => { const o = {}; for (const i of idxs) { try { const el = document.querySelector('[data-pi="' + i + '"]'); o[i] = el && window.__probeDump ? window.__probeDump(el) : '(无)'; } catch (e) { o[i] = 'ERR ' + e; } } return o; }"""

DIAG_JS = r"""() => {
  const sheets = [];
  for (const sh of document.styleSheets) {
    let n = -1; try { n = sh.cssRules.length; } catch (e) {}
    sheets.push([(sh.href || ('inline:' + ((sh.ownerNode && sh.ownerNode.getAttribute('data-clone-freeze')) || (sh.ownerNode && sh.ownerNode.getAttribute('data-cv-band')) || ''))).slice(-90), n, sh.media.mediaText.slice(0, 60), !!sh.disabled]);
  }
  const broken = [];
  for (const im of document.images) {
    if (im.complete && im.naturalWidth === 0 && im.currentSrc) {
      const r = im.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) broken.push([im.currentSrc.slice(-120), Math.round(r.width), Math.round(r.height)]);
    }
  }
  const styles = Array.from(document.querySelectorAll('style')).map(st => [
    Array.from(st.attributes, a => a.name + '=' + a.value.slice(0, 30)).join(' ').slice(0, 120),
    st.textContent.length, st.textContent.replace(/\s+/g, ' ').slice(0, 160)]);
  const attrs = e => e ? Array.from(e.attributes, a => a.name + '=' + a.value.slice(0, 80)).join(' ').slice(0, 400) : '';
  const h1 = document.querySelector('h1'), a = document.querySelector('main a, a[href]');
  const cs = e => e ? getComputedStyle(e) : null;
  const probe = {
    html: attrs(document.documentElement), body: attrs(document.body),
    // body 直接子元素(到 main 之后 3 个为止): 顶边/高/display/position/外边距, 再加 body 内边距 —— 位移源头在 main 本身、
    // 前面又对不上路径时, 占位者可能是零宽/极窄块、body 内边距或外边距, tops 行收不到
    kids: (() => { try { const b = document.body, bc = cs(b); const out = ['body pt=' + bc.paddingTop + ' mt=' + bc.marginTop]; let after = -1;
      for (const k of b.children) { if (/^(SCRIPT|STYLE|LINK|META|NOSCRIPT|TEMPLATE)$/.test(k.tagName)) continue; const c = cs(k), r = k.getBoundingClientRect();
        out.push(k.tagName.toLowerCase() + (k.id ? '#' + k.id.slice(-30) : '') + '@' + Math.round(r.top + scrollY) + ' h' + Math.round(r.height) + ' w' + Math.round(r.width) + ' ' + c.display + '/' + c.position + (c.marginTop !== '0px' ? ' mt=' + c.marginTop : '') + (c.paddingTop !== '0px' ? ' pt=' + c.paddingTop : '') + (c.visibility !== 'visible' ? ' ' + c.visibility : ''));
        if (k.tagName === 'MAIN' || k.querySelector('main')) after = 0; else if (after >= 0 && ++after >= 3) break; if (out.length > 40) break; }
      return out.join(' | ').slice(0, 1500); } catch (e) { return 'ERR ' + e; } })(),
    bodyFont: cs(document.body) ? cs(document.body).fontFamily.slice(0, 80) + ' / ' + cs(document.body).fontSize + ' / ' + cs(document.body).color : '',
    h1: h1 ? cs(h1).fontSize + ' / ' + cs(h1).lineHeight + ' / ' + cs(h1).fontWeight + ' / ' + cs(h1).fontFamily.slice(0, 60) : '',
    a: a ? cs(a).textDecorationLine + ' / ' + cs(a).color : '',
    band: getComputedStyle(document.documentElement).getPropertyValue('--cv-band'),
    // 网页字体是否真的到了(fallback 字体行高不同, 整页逐行位移): 各 FontFace 的 family/weight/status
    fonts: (() => { try { const o = {}; for (const f of document.fonts) { const k = f.family.replace(/["']/g, '') + ' ' + f.weight; o[k] = (o[k] ? o[k] + ',' : '') + ({loaded: 'L', loading: 'g', error: 'E', unloaded: 'u'}[f.status] || f.status[0]); } return Object.entries(o).slice(0, 24).map(([k, v]) => k + ':' + v).join(' | '); } catch (e) { return ''; } })(),
    // 字体是否真的在画: 同一段字用"首选族, monospace"与纯 monospace 量宽, 不同才是网页字体在渲染(status=loaded 但文件不对/
    // unicode-range 不覆盖拉丁时照样回落到后备字体, qwik 克隆两张 Poppins 都 loaded 却按后备字体行高排)
    fontDraws: (() => { try { const fam = cs(document.body).fontFamily.split(',')[0].trim(); const mk = f => { const s = document.createElement('span'); s.textContent = 'mmmmmmmmmmlliiWWW'; s.style.cssText = 'position:absolute;left:-9999px;top:0;font-size:40px;font-family:' + f; document.body.appendChild(s); const w = s.getBoundingClientRect().width; s.remove(); return w; };
      return fam + ' ' + (Math.abs(mk(fam + ', monospace') - mk('monospace')) > 1 ? 'draws' : 'FALLBACK'); } catch (e) { return ''; } })(),
    // 该族的 @font-face 规则: src 与 unicode-range(两侧对照看克隆本地化后的文件/子集)
    fontFaces: (() => { try { const fam = cs(document.body).fontFamily.split(',')[0].trim().replace(/["']/g, ''); const out = [];
      for (const sh of document.styleSheets) { let rules; try { rules = sh.cssRules; } catch (e) { continue; } if (!rules) continue;
        for (const r of rules) { if (r instanceof CSSFontFaceRule && r.style.fontFamily.replace(/["']/g, '') === fam) out.push(r.style.fontWeight + ' ' + (r.style.fontDisplay ? 'display=' + r.style.fontDisplay + ' ' : '') + (r.style.src || '').slice(-70) + ' ' + (r.style.unicodeRange || '').slice(0, 40)); if (out.length >= 12) break; } }
      return out.join(' | '); } catch (e) { return ''; } })(),
    // 字体文件的网络事实(资源计时): 状态码/耗时/字节 —— status=loading 卡 6s 不结束时, 看请求是慢、404 还是根本没发出
    fontReq: (() => { try { return performance.getEntriesByType('resource').filter(e => /\.(woff2?|ttf|otf|eot)(\?|$)/i.test(e.name) || e.initiatorType === 'font' || e.initiatorType === 'css' && /font/i.test(e.name)).slice(0, 10)
      .map(e => e.name.split('/').slice(-2).join('/').slice(-48) + ' ' + (e.responseStatus || '?') + ' ' + Math.round(e.duration) + 'ms ' + (e.transferSize || 0) + 'B').join(' | '); } catch (e) { return ''; } })(),
    referrer: (() => { try { const m = document.querySelector('meta[name=referrer]'); return (m ? m.content : '-') + ' / ' + document.referrer.slice(0, 40); } catch (e) { return ''; } })(),
    keep: (() => { try { return (window.__cloneKept || 0) + ' ' + (window.__cloneKeepDbg ? JSON.stringify(window.__cloneKeepDbg()).slice(0, 500) : ''); } catch (e) { return ''; } })(),
    vw: innerWidth, dpr: devicePixelRatio,
  };
  // 可见文本行(评分覆盖的前 8 屏): 两侧做差直接看出克隆少了/多了哪些文案(表单/计数/弹层)
  const lines = [];
  try {
    const lim = innerHeight * 8;
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let t;
    while ((t = w.nextNode()) && lines.length < 900) {
      const s = (t.nodeValue || '').replace(/\s+/g, ' ').trim();
      if (s.length < 2) continue;
      const el = t.parentElement; if (!el) continue;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height || r.top > lim) continue;
      const c = cs(el); if (c.visibility === 'hidden' || c.display === 'none' || +c.opacity === 0) continue;
      lines.push(s.slice(0, 80) + ' @' + Math.round(r.top) + ',' + Math.round(r.left) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
    }
  } catch (e) {}
  // 表单控件也进文本行(占位符/值/aria-label): 表单整块丢失时文本做差才看得见; 另列 DOM 里有但不可见的控件及原因
  const hiddenInputs = [];
  try {
    for (const el of document.querySelectorAll('input:not([type=hidden]),textarea,select,button')) {
      const label = (el.placeholder || el.value || el.getAttribute('aria-label') || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 60);
      if (!label) continue;
      const r = el.getBoundingClientRect(); const c = cs(el);
      const vis = r.width > 0 && r.height > 0 && c.visibility !== 'hidden' && c.display !== 'none' && +c.opacity !== 0 && r.top < innerHeight * 8;
      if (vis) { if (lines.length < 900) lines.push('[' + el.tagName.toLowerCase() + '] ' + label + ' @' + Math.round(r.top) + ',' + Math.round(r.left) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height)); continue; }
      if (hiddenInputs.length >= 12 || r.top > innerHeight * 8) continue;
      let why = c.display === 'none' ? 'display:none' : c.visibility === 'hidden' ? 'visibility' : +c.opacity === 0 ? 'opacity0' : (!r.width || !r.height ? 'zero-size' : 'other');
      let anc = el, hop = 0;
      while (anc && anc !== document.body && hop++ < 12) { const a = cs(anc); if (a.display === 'none' || a.visibility === 'hidden') { why += ' <- ' + anc.tagName.toLowerCase() + (anc.id ? '#' + anc.id : '') + (anc.hasAttribute('data-cv-vp') ? '[cv-vp=' + anc.getAttribute('data-cv-vp') + ']' : '') + (anc.hasAttribute('data-cv') ? '[cv]' : '') + ':' + (a.display === 'none' ? 'display:none' : 'visibility'); break; } anc = anc.parentElement; }
      hiddenInputs.push('[' + el.tagName.toLowerCase() + '] ' + label + ' ' + why);
    }
  } catch (e) {}
  // 盖住视口 ≥50% 的 fixed/sticky 层(弹窗/遮罩/抽屉): 标签#id.class z 尺寸 是否含输入框/关闭钮, 供评分口径的弹层识别对账
  const overlays = [];
  try {
    for (const el of document.querySelectorAll('body *')) {
      if (overlays.length >= 12) break;
      const c = cs(el); if (c.position !== 'fixed' && c.position !== 'sticky') continue;
      const r = el.getBoundingClientRect(); if (r.width < innerWidth * 0.5 || r.height < innerHeight * 0.5) continue;
      if (c.visibility === 'hidden' || c.display === 'none' || +c.opacity === 0) continue;
      const inp = el.querySelector('input:not([type=hidden]),textarea'), btn = el.querySelector('button,[role=button],[aria-label*="close" i],[class*="close" i]');
      overlays.push((el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + '.' + String(el.className).slice(0, 40)) + ' z=' + c.zIndex
        + ' ' + Math.round(r.width) + 'x' + Math.round(r.height) + '@' + Math.round(r.top) + ' bg=' + c.backgroundColor.slice(0, 24) + ' role=' + (el.getAttribute('role') || '') + (el.getAttribute('aria-modal') ? ' aria-modal' : '')
        + (inp ? ' input' : '') + (btn ? ' btn' : '') + (el.hasAttribute('data-clone-ihide') ? ' ihide' : ''));
    }
  } catch (e) {}
  // 可见图片/视频盒(前 8 屏): 两侧按源名配对比较渲染高度与原始尺寸 —— 克隆用了不同裁切/比例的图源时
  // 整页从该图起整体位移几个像素, 文本做差(阈 40px)看不出, 分段 NCC 却从此段起折半
  const boxes = [];
  try {
    const lim = innerHeight * 8;
    for (const el of document.querySelectorAll('img,video,picture>source,[style*="background-image"]')) {
      if (boxes.length >= 300) break;
      const r = (el.tagName === 'SOURCE' ? el.parentElement : el).getBoundingClientRect();
      if (r.width < 24 || r.height < 24 || r.top > lim || r.bottom < 0) continue;
      const c = cs(el); if (c.visibility === 'hidden' || c.display === 'none') continue;
      let src = el.currentSrc || el.src || el.getAttribute('srcset') || '';
      if (!src && c.backgroundImage && c.backgroundImage !== 'none') src = c.backgroundImage;
      src = (src.match(/[^\/"'()?#]+(?=[?#"')]|$)/) || [src])[0].slice(-60);
      const par = el.parentElement, pc = par ? cs(par) : null;
      boxes.push([el.tagName.toLowerCase(), src, Math.round(r.top), Math.round(r.left), Math.round(r.width), Math.round(r.height),
                  el.naturalWidth || el.videoWidth || 0, el.naturalHeight || el.videoHeight || 0, (c.objectFit || '') + '/' + (c.aspectRatio || ''),
                  // [9] 自身内联 style [10] 父元素 标签.class + 内联 style + 计算高: 盒高与原站不同时看高度是写死在谁身上
                  (el.getAttribute('style') || '').slice(0, 80) + (el.hasAttribute('data-cv') ? ' cv=' + el.getAttribute('data-cv') : ''),
                  par ? par.tagName.toLowerCase() + '.' + String(par.className || '').slice(0, 24) + ' {' + (par.getAttribute('style') || '').slice(0, 60) + '} h=' + Math.round(par.getBoundingClientRect().height) + (par.hasAttribute('data-cv') ? ' cv=' + par.getAttribute('data-cv') : '') : '']);
    }
  } catch (e) {}
  // 大块显隐(前 8 屏): body 下 7 层内 高≥150px 且宽≥半屏的可见块, 以及含图/链接却 display:none/visibility:hidden 的块
  // (记内联 display 与是否带分视口标记): 两侧按路径做差直接看出"原站手机端藏起的桌面横幅克隆没藏"是样式表没生效
  // 还是脚本首载时写的内联样式没采到
  const blocks = [];
  try {
    const key = e => {
      const p = []; let x = e;
      while (x && x !== document.body && p.length < 7) {
        let s = x.tagName.toLowerCase() + (x.id ? '#' + x.id.slice(0, 24) : '');
        const cl = String(x.className && x.className.baseVal !== undefined ? x.className.baseVal : x.className || '').trim().split(/\s+/)
          .filter(c => c && !/^(active|on|cur|current|show|open|hover|slick-|swiper-slide-|bx-|owl-|cv-|clone)/.test(c))[0];
        if (cl) s += '.' + cl.slice(0, 24);
        let i = 1, sib = x; while ((sib = sib.previousElementSibling)) if (sib.tagName === x.tagName && !sib.hasAttribute('data-cv-vp')) i++;
        p.unshift(s + ':' + i); x = x.parentElement;
      }
      return p.join('>');
    };
    const stAttr = e => { let s = ''; for (const a of e.attributes) if (/^(aria-(expanded|hidden|selected|current|controls)|open|hidden|data-(state|open|active|expanded|collapsed|visible|toggle|accordion))$/.test(a.name)) s += ' ' + a.name + '=' + a.value.slice(0, 14); return s; };
    const walk = (el, depth) => {
      if (blocks.length >= 120 || depth > 7) return;
      for (const ch of el.children) {
        if (blocks.length >= 120) break;
        const c = cs(ch); const r = ch.getBoundingClientRect();
        // 第 6 项: class / 内联 style / 定位 / transform / 分视口标记 —— 两侧同路径块一显一隐时看出是类状态(轮播 next/active)、
        // 脚本写的内联样式还是引擎分视口规则(data-cv)造成
        const how = (ch.getAttribute('class') || '').slice(0, 60) + ' | ' + (ch.getAttribute('style') || '').slice(0, 90) + ' | ' + c.position
          + (c.transform !== 'none' ? ' tf=' + c.transform.slice(0, 40) : '') + (ch.hasAttribute('data-cv') ? ' cv=' + ch.getAttribute('data-cv') : '')
          + (ch.hasAttribute('data-cv-vp') ? ' cv-vp=' + ch.getAttribute('data-cv-vp') : '') + (ch.hasAttribute('data-cv-cls-d') ? ' cv-cls' : '')
          // 状态属性(自身 + 前一个兄弟, 手风琴/折叠的开关钮通常在内容前): 两侧同路径块一显一隐时看出是脚本按烘进 HTML 的桌面态
          // (aria-expanded=true)重跑把内容展开, 还是样式表没生效
          + (stAttr(ch) ? ' attrs[' + stAttr(ch).trim() + ']' : '')
          + (ch.previousElementSibling ? ' prev[' + ch.previousElementSibling.tagName.toLowerCase() + stAttr(ch.previousElementSibling) + ']' : '');
        if (c.display === 'none' || c.visibility === 'hidden') {
          if (ch.querySelector('img,video,a') && (ch.textContent.trim().length + ch.querySelectorAll('img').length * 100) > 60)
            blocks.push([key(ch), 'hidden:' + (c.display === 'none' ? 'display' : 'visibility'), 0, 0, ch.querySelectorAll('img').length, how]);
          continue;
        }
        if (r.top + scrollY > innerHeight * 8) continue;
        if (r.height >= 150 && r.width >= innerWidth * 0.5) blocks.push([key(ch), 'vis', Math.round(r.top + scrollY), Math.round(r.height), ch.querySelectorAll('img').length, how]);
        walk(ch, depth + 1);
      }
    };
    walk(document.body, 0);
  } catch (e) {}
  // 逐元素顶边(前 20 屏, 12 层内, 高≥6 宽≥30 的可见元素, DOM 序): 两侧按路径对齐后, 相邻元素间"位移量跳变"的地方
  // 就是位移源头(某块两侧高度不同), 比只看图盒/文字快得多——位移源头常是无图无字的容器(视频壳/动画占位)
  const tops = [];
  try {
    const key12 = e => {
      const p = []; let x = e;
      while (x && x !== document.body && p.length < 12) {
        let s = x.tagName.toLowerCase() + (x.id ? '#' + x.id.slice(0, 24) : '');
        const cl = String(x.className && x.className.baseVal !== undefined ? x.className.baseVal : x.className || '').trim().split(/\s+/)
          .filter(c => c && !/^(active|on|cur|current|show|open|hover|slick-|swiper-slide-|bx-|owl-|cv-|clone)/.test(c))[0];
        if (cl) s += '.' + cl.slice(0, 24);
        let i = 1, sib = x; while ((sib = sib.previousElementSibling)) if (sib.tagName === x.tagName && !sib.hasAttribute('data-cv-vp')) i++;
        p.unshift(s + ':' + i); x = x.parentElement;
      }
      return p.join('>');
    };
    // 第 5 项: 父元素在 tops 里的下标(-1 为 body 直属), 路径只留 12 层, 深处的祖先/子孙关系靠它判断
    // 作者写的 grid-template-rows(计算值只给 used px, 看不出是 0px 写死还是 auto 塌掉): 预筛含该声明的规则(含媒体条件), 逐 grid 元素匹配
    const gtrRules = (() => { const out = []; try { const walk = (rules, media) => { for (const r of rules) { if (out.length > 600) return;
      if (r.type === 4) { walk(r.cssRules, (media ? media + ' & ' : '') + r.conditionText); continue; } if (r.type === 12 || r.type === 3) { try { walk(r.cssRules, media); } catch (e) {} continue; }
      if (r.style && r.style.gridTemplateRows) out.push([r.selectorText, r.style.gridTemplateRows.slice(0, 60), media, (media ? matchMedia(media.split(' & ').pop()).matches : true)]); } };
      for (const sh of document.styleSheets) { try { walk(sh.cssRules, ''); } catch (e) {} } } catch (e) {} return out; })();
    const gtrSrc = (el) => { const hits = []; for (const [sel, v, media, on] of gtrRules) { try { if (el.matches(sel)) hits.push(v + (media ? '@' + media.slice(0, 30) : '') + (on ? '' : '(off)')); } catch (e) {} if (hits.length >= 3) break; } return hits.join(';'); };
    // 带 0px 行的 grid 容器(内容在、行却是 0: 子项没参与行尺寸, 原因在作者样式里): 列出命中该容器及其子项的全部规则声明
    let dumped = 0; const dumpedSet = new Map();
    const LAYP = /^(display|position|height|min-height|max-height|grid-row|grid-row-start|grid-row-end|grid-area|grid-template-rows|grid-auto-rows|margin-top|margin-bottom|contain|content-visibility|visibility|overflow|overflow-y|align-self|place-self|top|bottom|transform|--l_display|--comp-display|--container-display|--display)$/;
    const rulesOf = (el, cap) => { const hits = []; try { for (const sh of document.styleSheets) { let rules; try { rules = sh.cssRules; } catch (e) { continue; }
      const walk = (rs, on) => { for (const r of rs) { if (hits.length >= cap) return; if (r.type === 4) { walk(r.cssRules, on && matchMedia(r.conditionText).matches); continue; } if (r.type === 12 || r.type === 3) { try { walk(r.cssRules, on); } catch (e) {} continue; }
        if (r.type === 1 && on) { try { if (el.matches(r.selectorText)) { const ds = []; for (const pn of r.style) if (LAYP.test(pn)) ds.push(pn + ':' + r.style.getPropertyValue(pn).slice(0, 60) + (r.style.getPropertyPriority(pn) ? '!' : '')); const h = r.selectorText.slice(0, 90) + '{' + ds.join(';') + '}'; if (ds.length && !hits.includes(h)) hits.push(h); } } catch (e) {} } } };
      walk(rules, true); if (hits.length >= cap) break; } } catch (e) {} return hits.join(' '); };
    const dumpOf = (ch) => { let out = ' RULES[' + rulesOf(ch, 10) + ']'; for (const k of ch.children) { const kc = cs(k); out += ' CHILD ' + (k.id ? '#' + k.id.slice(0, 24) : k.tagName) + ' gr=' + kc.gridRowStart + '/' + kc.gridRowEnd + ' h=' + Math.round(k.getBoundingClientRect().height) + ' style=' + (k.getAttribute('style') || '').slice(0, 80) + ' [' + rulesOf(k, 8) + ']'; } return out; };
    window.__probeDump = dumpOf;
    const zeroRowDump = (ch, c) => { if (dumped >= 30 || !/(^| )0px( |$)/.test(c.gridTemplateRows) || ch.children.length > 8) return ''; dumped++;
      const out = dumpOf(ch); if (dumpedSet.has(out)) return ' RULES=same#' + dumpedSet.get(out); dumpedSet.set(out, dumped); return out; };
    const walkT = (el, depth, pidx) => {
      if (tops.length >= 12000 || depth > 40) return;
      for (const ch of el.children) {
        if (tops.length >= 12000) break;
        if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|LINK|META)$/.test(ch.tagName)) continue;
        const c = cs(ch); if (c.display === 'none') continue;
        const r = ch.getBoundingClientRect();
        if (r.top + scrollY > innerHeight * 20) continue;
        // visibility:hidden 的块看不见却占位(水合过渡中/入场前的整段): 记一行带 VH 标记、不再下钻, 否则位移源头处找不到占位者
        if (c.visibility === 'hidden') {
          if (r.height >= 50 && r.width >= 30 && c.position !== 'fixed' && c.position !== 'absolute')
            tops.push([key12(ch), Math.round(r.top + scrollY), Math.round(r.height), (ch.getAttribute('class') || '').slice(0, 50) + ' | ' + (ch.getAttribute('style') || '').slice(0, 100) + ' VH', pidx]);
          continue;
        }
        let idx = pidx;
        if (r.height >= 10 && r.width >= 30 && c.position !== 'fixed') {
          idx = tops.length;
          try { ch.setAttribute('data-pi', String(idx)); } catch (e) {}   // 截图已拍, 打下标供随后按路径点名转储规则
          tops.push([key12(ch), Math.round(r.top + scrollY), Math.round(r.height), (ch.getAttribute('class') || '').slice(0, 50) + ' | ' + (ch.getAttribute('style') || '').slice(0, 100)
            + (ch.hasAttribute('data-cv-vp') ? ' cv-vp=' + ch.getAttribute('data-cv-vp') : '') + (ch.hasAttribute('data-cv') ? ' cv=' + ch.getAttribute('data-cv') : '')
            + (/^(IMG|CANVAS|VIDEO|SVG)$/i.test(ch.tagName) ? ' a=' + (ch.getAttribute('width') || '-') + 'x' + (ch.getAttribute('height') || '-') + (ch.naturalWidth ? ' nat=' + ch.naturalWidth + 'x' + ch.naturalHeight : '') + ' box=' + Math.round(r.width) + 'x' + Math.round(r.height) + ' h=' + c.height + ' ar=' + c.aspectRatio : '')
            + (c.maxHeight !== 'none' ? ' mh=' + c.maxHeight : '') + (c.overflow !== 'visible' ? ' ov=' + c.overflow : '')
            + (c.display.indexOf('grid') >= 0 ? ' grid gtr=' + c.gridTemplateRows.slice(0, 80) + ' gtrSrc=' + gtrSrc(ch) + zeroRowDump(ch, c) : '') + (c.minHeight !== '0px' && c.minHeight !== 'auto' ? ' minh=' + c.minHeight : '')
            + ((ch.firstChild && ch.firstChild.nodeType === 3 && ch.firstChild.nodeValue.trim().length >= 3) ? ' f=' + c.fontSize + '/' + c.lineHeight + '/' + c.fontFamily.split(',')[0].slice(0, 16) : '')
            + (c.position !== 'static' ? ' pos=' + c.position + (c.position === 'absolute' ? '@' + c.top : '') : '') + (c.marginTop !== '0px' ? ' mt=' + c.marginTop : '')
            + (cs(el).display.indexOf('grid') >= 0 ? ' gr=' + c.gridRowStart + '/' + c.gridRowEnd + (c.alignSelf !== 'auto' && c.alignSelf !== 'normal' ? ' as=' + c.alignSelf : '') : '')
            + (ch.hasAttribute('aria-expanded') ? ' exp=' + ch.getAttribute('aria-expanded') : '') + (ch.hasAttribute('aria-hidden') ? ' ahid=' + ch.getAttribute('aria-hidden') : ''), pidx]);
        }
        walkT(ch, depth + 1, idx);
      }
    };
    walkT(document.body, 0, -1);
  } catch (e) {}
  return {tops: tops, sheets: sheets.slice(0, 40), broken: broken.slice(0, 30), nimg: document.images.length,
          styles: styles.slice(0, 60), probe: probe, lines: lines, overlays: overlays, hiddenInputs: hiddenInputs, boxes: boxes, blocks: blocks};
}"""


def _shoot(ctx, url, vp, out: Path, timeout_ms=45000, extra_wait_ms=0, focus_keys=None):
    page = ctx.new_page()
    info = {"url": url}
    failed, errors = [], []
    page.on("requestfailed", lambda rq: len(failed) < 40 and failed.append(
        [rq.url[-120:], (rq.failure or "")[:60], rq.resource_type]))
    page.on("response", lambda rs: rs.status >= 400 and len(failed) < 40 and failed.append(
        [rs.url[-120:], str(rs.status), rs.request.resource_type]))
    page.on("pageerror", lambda e: len(errors) < 10 and errors.append(
        (str(e)[:160] + " @ " + str(getattr(e, "stack", "") or "").split("\n")[1:2].__str__()[:200])))
    # 语法错误没有栈: 走 CDP 拿到出错脚本的 URL 与行列, 再从页面源码截出那一行(定位是哪段注入/改写的脚本)
    exc_locs = []
    try:
        cdp = ctx.new_cdp_session(page)
        cdp.send("Runtime.enable")
        cdp.on("Runtime.exceptionThrown", lambda ev: len(exc_locs) < 6 and exc_locs.append(
            [((ev.get("exceptionDetails") or {}).get("text") or "")[:120],
             (ev.get("exceptionDetails") or {}).get("url") or "",
             (ev.get("exceptionDetails") or {}).get("lineNumber"),
             (ev.get("exceptionDetails") or {}).get("columnNumber")]))
    except Exception:   # noqa: BLE001
        pass
    page.on("console", lambda m: m.type == "error" and len(errors) < 10 and errors.append(
        "console: " + m.text[:160] + " @ " + str((m.location or {}).get("url", ""))[-100:]
        + ":" + str((m.location or {}).get("line_number", ""))))
    try:
        for attempt in range(4):   # 网络层抖动(RESET/CLOSED/断网)退避重试, 不当成克隆缺陷
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                break
            except Exception as exc:   # noqa: BLE001
                if attempt == 3 or "net::ERR_" not in str(exc):
                    raise
                time.sleep(2 * (attempt + 1))
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=8000)
        with suppress(Exception):
            page.add_style_tag(content=SETTLE_CSS)
        max_h = vp["height"] * MAX_SCREENS
        sw = page.evaluate(SCROLL_JS, max_h)
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=4000)
        with suppress(Exception):
            page.evaluate(SETTLE_MEDIA_JS, [max_h, 6000])
        if extra_wait_ms:
            page.wait_for_timeout(extra_wait_ms)
        with suppress(Exception):
            from core.cloner_engine import AD_RECTS_JS, INTERSTITIAL_HIDE_JS, SCROLL_LOCK_STRIP_JS
            info["interstitial"] = page.evaluate(INTERSTITIAL_HIDE_JS) or 0
            # 弹层藏掉后把它写在 html/body 上的滚动锁也剥掉(两侧同做): 否则活体原站整页只剩一屏(allbirds 桌面
            # 原高 1080 / 克隆 4335), 比的是"弹层锁住的一屏"而不是页面
            info["unlocked"] = page.evaluate(SCROLL_LOCK_STRIP_JS) or 0
            info["ads"] = page.evaluate(AD_RECTS_JS) or []
        info["scroll_width"] = sw
        info["overflow_x"] = sw > vp["width"] + 2
        full_h = page.evaluate("document.documentElement.scrollHeight")
        info["height"] = full_h
        clip_h = min(full_h, max_h)
        page.screenshot(path=str(out), full_page=True, animations="disabled", scale="css",
                        clip={"x": 0, "y": 0, "width": vp["width"], "height": clip_h})
        info["text"] = page.evaluate("document.body ? document.body.innerText.slice(0, 200000) : ''")
        with suppress(Exception):
            d = page.evaluate(DIAG_JS) or {}
            d["failed"] = failed
            d["errors"] = errors
            # 源码里的 <html>/<body> 开标签(脚本跑之前的内联 style/class): 与上面"探 html/body"(运行后)对照,
            # 分得清滚动锁/状态类是序列化时就烘进交付 HTML 的, 还是克隆里脚本重跑又写上去的
            with suppress(Exception):
                raw = page.request.get(url, timeout=15000).text()
                d["src_tags"] = [m.group(0)[:300] for m in re.finditer(r"<(?:html|body)\b[^>]*>", raw[:400000], re.I)][:2]
                # 状态类(open/expanded/active/show)运行时与源码不一致的元素: 区分"状态烘进 HTML"与"脚本在本页重跑时加/去"
                with suppress(Exception):
                    st_re = re.compile(r"(^|[\s_-])(is-)?(open|opened|expanded|active|show|shown|collapsed)($|[\s_-])", re.I)
                    live = page.evaluate(STATE_CLS_JS) or []
                    diff = []
                    for el_id, cls in live:
                        m = re.search(r"<[a-zA-Z][^>]*\bid=[\"']" + re.escape(el_id) + r"[\"'][^>]*>", raw)
                        if not m:
                            continue
                        cm = re.search(r"\bclass=[\"']([^\"']*)", m.group(0))
                        scls = cm.group(1) if cm else ""
                        a = {t for t in cls.split() if st_re.search(t)}
                        b = {t for t in scls.split() if st_re.search(t)}
                        if a != b:
                            diff.append([el_id[:60], sorted(a - b)[:4], sorted(b - a)[:4]])
                    d["state_cls"] = diff[:20]
            if focus_keys and d.get("tops"):
                with suppress(Exception):
                    idx = {t[0]: i for i, t in enumerate(d["tops"])}
                    hcl = {t[0]: t[2] for t in d["tops"]}
                    # 原站带 0px 行的 grid 容器可能有几十个(wix 布局全是 grid): 先转储两侧高度不同的(真出问题的那些), 再补其他, 共 12 个
                    oh = focus_keys if isinstance(focus_keys, dict) else {k: None for k in focus_keys}
                    keys = [k for k in oh if k in idx]
                    diff = [k for k in keys if oh[k] is not None and abs((hcl.get(k) or 0) - oh[k]) > 4]
                    keys = (diff + [k for k in keys if k not in diff])[:12]
                    want = [idx[k] for k in keys]
                    got = page.evaluate(FOCUS_RULES_JS, want) or {}
                    d["focus"] = {k: got.get(str(idx[k]), got.get(idx[k], "")) for k in keys}
            if exc_locs:
                src_lines = None
                with suppress(Exception):
                    src_lines = page.request.get(url, timeout=15000).text().splitlines()
                for ex in exc_locs:
                    if src_lines and ex[1] in (url, page.url) and isinstance(ex[2], int) and 0 <= ex[2] < len(src_lines):
                        col = ex[3] or 0
                        ex.append(src_lines[ex[2]][max(0, col - 160):col + 120])
                d["exceptions"] = exc_locs
            info["diag"] = d
        info["ok"] = True
    except Exception as exc:   # noqa: BLE001 单侧失败记入结果
        info["ok"] = False
        info["error"] = str(exc)[:200]
    finally:
        with suppress(Exception):
            page.close()
    return info


# ---- 抗活体随机性的稳健口径(--noise 时一并给出) ----
# 原站两次加载之间就不同的区域(轮播相位、实时新闻、计数器、A/B 实验位): 克隆不可能也不该
# 复现某一次的状态。按 24px 网格比两次原站截图, 差异块(膨胀一格)三张图同处涂灰后再比;
# 克隆分取与两次原站中较接近的一次(活体在两种状态间切换时, 克隆对上其中任一种都算对)。
# 差异块超过公共区域一半时不遮(整页错位, 遮了就没有可比内容), 只报 dyn_frac 供参考。
NOISE_PHASE_SHIFT_MS = 3500
DYN_BLOCK = 24
DYN_THRESH = 12.0
DYN_MAX_FRAC = 0.5


def dynamic_rects(a_path, a2_path, block=DYN_BLOCK, thresh=DYN_THRESH):
    """两次原站截图的差异块 -> ([x, y, w, h], 差异块占公共区域比例)。"""
    import numpy as np
    from PIL import Image
    with Image.open(a_path) as ia, Image.open(a2_path) as ib:
        ga, gb = ia.convert("L"), ib.convert("L")
    w, h = min(ga.width, gb.width), min(ga.height, gb.height)
    if w < block or h < block:
        return [], 0.0
    A = np.asarray(ga, dtype=np.float32)[:h, :w]
    B = np.asarray(gb, dtype=np.float32)[:h, :w]
    nx, ny = w // block, h // block
    d = np.abs(A[:ny * block, :nx * block] - B[:ny * block, :nx * block])
    cells = d.reshape(ny, block, nx, block).mean(axis=(1, 3)) > thresh
    if not cells.any():
        return [], 0.0
    grown = cells.copy()
    grown[1:, :] |= cells[:-1, :]
    grown[:-1, :] |= cells[1:, :]
    grown[:, 1:] |= cells[:, :-1]
    grown[:, :-1] |= cells[:, 1:]
    frac = float(grown.mean())
    rects = []
    for y in range(ny):            # 每行合并连续块, 矩形数可控
        x = 0
        while x < nx:
            if grown[y, x]:
                x0 = x
                while x < nx and grown[y, x]:
                    x += 1
                rects.append([x0 * block, y * block, (x - x0) * block, block])
            else:
                x += 1
    return rects, frac


def robust_scores(a, a2, b, outdir, tag, ia, i2, ib):
    """返回 {robust, robust_ceiling, dyn_frac}; 原图不动, 遮罩写到 *_rb.png 副本。"""
    import shutil
    from core.cloner_engine import _mask_rects, compute_visual_similarity
    rects, frac = dynamic_rects(a, a2)
    out = {"dyn_frac": round(frac, 3)}
    if frac > DYN_MAX_FRAC:
        rects = []
    am, a2m, bm = (outdir / f"{tag}_{k}_rb.png" for k in ("orig", "orig2", "clone"))
    for src, dst in ((a, am), (a2, a2m), (b, bm)):
        shutil.copyfile(src, dst)
        if rects:
            _mask_rects(dst, rects)
    s1 = compute_visual_similarity(am, bm, None, orig_text=ia.get("text", ""), clone_text=ib.get("text", "")).get("score")
    s2 = compute_visual_similarity(a2m, bm, None, orig_text=i2.get("text", ""), clone_text=ib.get("text", "")).get("score")
    out["robust"] = max(x for x in (s1, s2) if x is not None) if (s1 is not None or s2 is not None) else None
    out["robust_ceiling"] = compute_visual_similarity(
        am, a2m, None, orig_text=ia.get("text", ""), clone_text=i2.get("text", "")).get("score")
    return out


def probe_one(browser, base, task_id, url, viewports, outdir: Path, clone_tpl=None, noise=False):
    from core.cloner_engine import compute_visual_similarity
    res = {"task_id": task_id, "url": url}
    for name in viewports:
        vp = PROBE_VIEWPORTS[name]
        ctx = browser.new_context(**context_opts(name))
        try:
            a = outdir / f"{task_id}_{name}_orig.png"
            b = outdir / f"{task_id}_{name}_clone.png"
            ia = _shoot(ctx, url, vp, a)
            clone_url = (clone_tpl or "{base}/preview/{id}/").format(base=base.rstrip("/"), id=task_id)
            focus = {}
            with suppress(Exception):
                for t in ((ia.get("diag") or {}).get("tops") or []):
                    g = re.search(r"grid gtr=([^ ]+(?: [0-9.]+(?:px|fr))*)", t[3])
                    if g and re.search(r"(^| )0px( |$)", g.group(1)) and len(focus) < 60:
                        focus[t[0]] = t[2]
            ib = _shoot(ctx, clone_url, vp, b, focus_keys=focus or None)
        finally:
            ctx.close()
        row = {"orig_overflow_x": ia.get("overflow_x"), "clone_overflow_x": ib.get("overflow_x"),
               "orig_h": ia.get("height"), "clone_h": ib.get("height"),
               "orig_interstitial": ia.get("interstitial", 0), "clone_interstitial": ib.get("interstitial", 0),
               "clone_diag": ib.get("diag"), "orig_diag": ia.get("diag")}
        if ia.get("ok") and ib.get("ok"):
            rects = (ia.get("ads") or []) + (ib.get("ads") or [])
            if rects:   # 第三方竞价广告位两侧统一遮灰(与引擎评分同口径)
                from core.cloner_engine import _mask_rects
                _mask_rects(a, rects)
                _mask_rects(b, rects)
            sim = compute_visual_similarity(a, b, outdir / f"{task_id}_{name}_diff.png",
                                            orig_text=ia.get("text", ""), clone_text=ib.get("text", ""),
                                            seg_detail=True)
            row.update({k: sim.get(k) for k in ("score", "structure", "edge", "text", "height_ratio", "seg_detail")})
            if noise:
                # 原站自比: 隔一次重新加载原站再比, 得到本口径下的可达上限(活体随机性)
                ctx2 = browser.new_context(**context_opts(name))
                try:
                    a2 = outdir / f"{task_id}_{name}_orig2.png"
                    # 第二次原站多等几秒再截: 按定时轮换的区域(标签页/轮播由 setInterval 翻页)两次同相位
                    # 加载看不出来, 错开相位才会落进动态区被遮
                    i2 = _shoot(ctx2, url, vp, a2, extra_wait_ms=NOISE_PHASE_SHIFT_MS)
                finally:
                    ctx2.close()
                if i2.get("ok"):
                    if rects or i2.get("ads"):
                        from core.cloner_engine import _mask_rects
                        _mask_rects(a2, rects + (i2.get("ads") or []))
                    row["ceiling"] = compute_visual_similarity(
                        a, a2, None, orig_text=ia.get("text", ""), clone_text=i2.get("text", "")).get("score")
                    with suppress(Exception):
                        row.update(robust_scores(a, a2, b, outdir, f"{task_id}_{name}", ia, i2, ib))
        else:
            row["error"] = ia.get("error") or ib.get("error")
        res[name] = row
    return res


def probe_tasks(base, tasks, viewports=("desktop", "tablet", "mobile"), outdir=None, workers=3,
                clone_tpl=None, noise=False):
    """tasks: [(task_id, url)]。返回 {task_id: {viewport: {score,...}}}。"""
    from playwright.sync_api import sync_playwright
    outdir = Path(outdir or tempfile.mkdtemp(prefix="vprobe_"))
    outdir.mkdir(parents=True, exist_ok=True)
    results = {}

    def work(chunk):
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                for tid, url in chunk:
                    t0 = time.time()
                    r = probe_one(browser, base, tid, url, viewports, outdir, clone_tpl, noise)
                    r["probe_s"] = round(time.time() - t0, 1)
                    results[tid] = r
                    print(f"  探针 #{tid} {url}: " + "  ".join(
                        f"{v} {r.get(v, {}).get('score', '-')}" for v in viewports), flush=True)
            finally:
                browser.close()
    chunks = [tasks[i::workers] for i in range(workers)]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, [c for c in chunks if c]))
    return results, outdir


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8897")
    ap.add_argument("--tasks", default="", help="逗号分隔任务号")
    ap.add_argument("--run", default="", help="run_*.json, 取其中全部完成任务")
    ap.add_argument("--db", default=str(ROOT / "cloner.db"))
    ap.add_argument("--viewports", default="desktop,tablet,mobile")
    ap.add_argument("--out", default="")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--clone-url", default="", help="克隆地址模板, 默认 {base}/preview/{id}/")
    ap.add_argument("--noise", action="store_true",
                    help="另测原站自比上限(每视口多一次原站加载), 并给出抗活体随机的稳健分 robust")
    args = ap.parse_args()
    pairs = []
    if args.run:
        doc = json.loads(Path(args.run).read_text(encoding="utf-8"))
        pairs = [(r["task_id"], r["url"]) for r in doc["results"] if r["status"] == "completed"]
    if args.tasks:
        import sqlite3
        con = sqlite3.connect(args.db)
        for tid in [int(x) for x in args.tasks.split(",") if x.strip()]:
            row = con.execute("select target_url from clone_tasks where id=?", (tid,)).fetchone()
            if row:
                pairs.append((tid, row[0]))
    vps = expand_viewports([v.strip() for v in args.viewports.split(",") if v.strip()])
    res, outdir = probe_tasks(args.base, pairs, vps, args.out or None, args.workers,
                              args.clone_url or None, args.noise)
    print(f"\n截图/热力图: {outdir}")
    print("| # | 站点 | " + " | ".join(vps) + " |")
    print("|---|---|" + "---|" * len(vps))
    for tid, url in pairs:
        r = res.get(tid, {})
        cells = []
        for v in vps:
            c = r.get(v, {})
            s = c.get("score", "-")
            if c.get("clone_overflow_x") and not c.get("orig_overflow_x"):
                s = f"{s} ↔"
            if c.get("ceiling") is not None:
                s = f"{s} /{c['ceiling']}"
            cells.append(str(s))
        print(f"| {tid} | {url} | " + " | ".join(cells) + " |")
    (outdir / "probe.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
