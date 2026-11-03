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
import hashlib
import html as _html
import json
import os
import posixpath
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from PIL import Image, ImageChops, ImageFilter, ImageStat

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
  Array.from(document.styleSheets).forEach(sheet => {
    try {
      const rules = sheet.cssRules;
      if (!rules || !rules.length) return;
      let text = '';
      for (let i = 0; i < rules.length; i++) text += rules[i].cssText + '\n';
      if (text.trim()) {
        const base = sheet.href || document.baseURI;
        text = text.replace(/url\((\s*['"]?)([^'")]+?)(['"]?\s*)\)/g,
          function (m, q1, u, q2) {
            u = u.trim();
            if (/^(data:|blob:|about:|#|https?:)/i.test(u)) return m;
            try { return 'url(' + q1 + new URL(u, base).href + q2 + ')'; }
            catch (e) { return m; }
          });
        const style = document.createElement('style');
        style.setAttribute('data-cssom', 'true');
        style.textContent = text;
        head.appendChild(style);
        result.css++;
      }
    } catch (err) {
      result.crossOriginSheets++;
    }
  });

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
FORM_ACTIVATE_JS = r"""
() => {
  let n = 0;
  document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]), textarea, select').forEach(el => {
    if (n >= 60) return;
    try { el.focus(); el.blur(); n++; } catch (e) {}
  });
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
        if (!pv || pv[i] !== mine[i]) diff.push([PROPS[i], mine[i]]);
      }
      if (diff.length) plan.push([el, diff]);
    }
    for (const [el, diff] of plan) {
      for (const [p, v] of diff) {
        if (v) el.style.setProperty(p, v, 'important');
      }
    }
    return plan.length;
  } catch (e) { return 0; }
}
"""

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

# 响应式适配注入: 补 viewport meta + 手机防溢出 + 表单可用性。
# 克隆页普遍缺失 viewport meta(手机按 980px 桌面布局渲染再整页缩小, 文字不可读),
# 且桌面站常见固定宽度容器/超宽绝对定位元素造成横向滚动。
RESPONSIVE_META = ('<meta name="viewport" '
                   'content="width=device-width, initial-scale=1.0">')

RESPONSIVE_CSS = """<style data-clone-preview="responsive">
/* 移动端适配兜底(桌面不受影响)。
   策略: 不强改老站桌面布局(会裁掉内容), 而是
   1) 视口按设备宽渲染(viewport meta 已注入);
   2) 响应式站点自然收缩;
   3) 固定宽度老站可横向平移查看而不破版。 */
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
@media screen and (max-width: 768px) {
  html, body { overflow-x: auto !important; }
  img, video { max-width: 100% !important; height: auto !important; }
  iframe { max-width: 100% !important; }  /* 不动 height: 登录 iframe 依赖定高 */
  input, select, textarea { font-size: 16px !important; }
  table { max-width: 100%; }
}
@media screen and (max-width: 359px) {
  body { font-size: 14px !important; }
}
</style>"""


def _inject_responsive(html: str) -> str:
    """视口 meta + 移动端兜底 CSS。已有 viewport meta 则不覆盖(尊重原站)。"""
    if "viewport" not in html[:4000]:
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

OUTER_HTML_JS = r"""
() => ('<!DOCTYPE html>\n' + document.documentElement.outerHTML)
"""

# 可见文本采集(含同源 iframe 内容, 保证克隆/原站两侧口径一致)
TEXT_EXTRACT_JS = r"""
() => {
  let out = '';
  try { out += document.body ? document.body.innerText : ''; } catch (e) {}
  document.querySelectorAll('iframe').forEach(f => {
    try {
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
    try { out.push(new URL(raw, location.href).href); } catch (e) {}
  });
  return out.slice(0, 10);
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
    if (!u || u.startsWith('data:') || u.startsWith('blob:') || u.startsWith('#')
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
        if (!u || u.startsWith('data:') || u.startsWith('blob:') || u.startsWith('#')) return;
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


def rewrite_js_navigation(text: str) -> str:
    """JS/内联脚本导航逃逸改写(见上方注释; 幂等, 可重复应用)。"""
    text = JS_NAV_OPEN_RE.sub(r"window.__cloneNav(\1,'_\3')", text)
    text = JS_TOP_ASSIGN_RE.sub(r"window.__cloneLoc('top').href = \1", text)
    text = JS_TOP_HREF_RE.sub("window.__cloneLoc('top').href =", text)
    text = JS_PARENT_LOC_RE.sub("window.__cloneLoc('parent').href =", text)
    return text

CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.I)
# 同时覆盖 @import url(...) 与 @import "..." 两种语法
CSS_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\(\s*['"]?(?P<u1>[^'")]+?)['"]?\s*\)|['"](?P<u2>[^'"]+)['"])""",
    re.I,
)
# image-set("a.png" 1x, url("b.png") 2x) 候选提取
CSS_IMAGE_SET_RE = re.compile(r"image-set\(\s*([^)]*?)\s*\)", re.I)

ASSET_SKIP_PREFIXES = ("data:", "blob:", "#", "javascript:", "mailto:", "tel:")

# CAS 防污染: 这些类别的资源若返回 HTML 拦截页则拒绝入库
PROTECTED_CATEGORIES = {"css", "js", "img", "fonts", "media"}
HTML_INTERCEPT_TYPES = ("text/html", "application/xhtml+xml")

# 视为静态资产的 Playwright resource_type 集合(活体采集/离线评分共用)
ASSET_RESOURCE_TYPES = {"stylesheet", "script", "image", "font", "media", "other"}

# 瞬时 Playwright 会话错误特征: 与稳定克隆失败不同源, 值得换会话重跑一次
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


def text_overlap_score(orig_text: str, clone_text: str) -> float:
    """克隆页对原站可见文本的召回率(0~1)。"""
    tokens_a = _text_tokens(orig_text)
    tokens_b = _text_tokens(clone_text)
    if not tokens_a:
        return 1.0 if not tokens_b else 0.7  # 原站本就无文本(纯图页面)时弱化
    return len(tokens_a & tokens_b) / len(tokens_a)


def text_f_score(orig_text: str, clone_text: str, beta: float = 2.0) -> tuple:
    """文本 F-beta(默认 F2, 召回权重 4 倍于精确): 返回 (f, recall, precision)。

    旧口径只算召回 —— 克隆页多出的错误/残留文案(兜底壳、报错提示、未隐藏
    的模板)不扣分; F2 让多余内容受罚, 同时缺失内容仍是主要失分项。"""
    tokens_a = _text_tokens(orig_text)
    tokens_b = _text_tokens(clone_text)
    if not tokens_a:
        v = 1.0 if not tokens_b else 0.7
        return v, v, v
    if not tokens_b:
        return 0.0, 0.0, 0.0
    inter = len(tokens_a & tokens_b)
    recall = inter / len(tokens_a)
    precision = inter / len(tokens_b)
    if recall == 0 or precision == 0:
        return 0.0, recall, precision
    b2 = beta * beta
    return (1 + b2) * precision * recall / (b2 * precision + recall), recall, precision


SCORE_WIDTH = 512            # 评分画布宽度(等比缩放)
SCORE_SEGMENT = 512          # 纵向分段高度(每段一个 512×512 方块)
SCORE_MAX_HEIGHT = 512 * 12  # 评分覆盖的最大缩放后高度(超长页尾部不计)
FIRST_SEGMENT_WEIGHT = 2.0   # 首段(首屏区域)权重


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


def _block_ncc(gray_a, gray_b, size=512, block=32) -> float:
    """块级归一化互相关: 双平坦且相近记 1, 一方有结构一方平坦记 0。

    注意不能用 ImageChops.multiply 计算协方差(那是除以 255 的混合运算),
    必须用原始像素值精确累加。numpy 可用时走向量化(实测 6-7 倍), 语义逐块一致。
    """
    if _np is not None:
        return _block_ncc_np(gray_a, gray_b, size, block)
    return _block_ncc_py(gray_a, gray_b, size, block)


def _block_ncc_np(gray_a, gray_b, size=512, block=32) -> float:
    grid = size // block
    span = grid * block

    def blocks(gray):
        arr = _np.asarray(gray, dtype=_np.float64).reshape(size, size)[:span, :span]
        return arr.reshape(grid, block, grid, block).transpose(0, 2, 1, 3) \
                  .reshape(grid * grid, block * block)

    a, b = blocks(gray_a), blocks(gray_b)
    ma, mb = a.mean(axis=1), b.mean(axis=1)
    da, db = a - ma[:, None], b - mb[:, None]
    cov = (da * db).sum(axis=1)
    va = (da * da).sum(axis=1)
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


def compute_visual_similarity(image_a: Path, image_b: Path, diff_path: Path = None,
                              orig_text: str = "", clone_text: str = "") -> dict:
    """诚实相似度 v3。返回 {score, structure, edge, text, ..., warnings[]}。

    v3 相对 v2 的口径修正:
    - 等比缩放到 512 宽 + 纵向 512 分段比较(v2 把整页长图压成 512×512,
      长页纵向压缩数十倍, 细节差异不可见, 高度差异被拉伸掩盖);
    - 高度不一致时短图以底色补齐, 缺失/多余区域在结构与边缘通道真实失分;
    - 文本通道由召回改为 F2(多余错误文案也扣分)。
    score = 0.5*分段块级NCC + 0.3*分段边缘IoU + 0.2*文本F2(原站无文本时重分配)。
    """
    with Image.open(image_a) as ia, Image.open(image_b) as ib:
        # 口径冻结(2026-11-04): 同宽两图先在原始分辨率补到同高再缩放(见引擎 _src_pad_heights)
        (wa, ha), (wb, hb) = ia.size, ib.size
        pa = pb = max(ha, hb) if wa == wb else None
        ca, cb = ia.convert("RGB"), ib.convert("RGB")
        if pa and pa > ha:
            ca = _pad_to(ca, pa)
        if pb and pb > hb:
            cb = _pad_to(cb, pb)
        ra = _fit_width(ca)
        rb = _fit_width(cb)
    h_a, h_b = ra.size[1], rb.size[1]
    height = max(h_a, h_b)
    seg_count = max(1, -(-height // SCORE_SEGMENT))
    canvas_h = seg_count * SCORE_SEGMENT
    ra, rb = _pad_to(ra, canvas_h), _pad_to(rb, canvas_h)

    if diff_path is not None:
        view_h = min(height, canvas_h)
        da, db = ra.crop((0, 0, SCORE_WIDTH, view_h)), rb.crop((0, 0, SCORE_WIDTH, view_h))
        difference = ImageChops.difference(da, db).convert("L")
        mask = difference.point(lambda p: 255 if p > 28 else 0)
        backdrop = db.point(lambda p: int(p * 0.45)).convert("RGB")
        red_layer = Image.new("RGB", da.size, (255, 0, 0))
        Image.composite(red_layer, backdrop, mask).save(diff_path)

    ga, gb = ra.convert("L"), rb.convert("L")
    s_sum = e_sum = w_sum = 0.0
    for k in range(seg_count):
        box = (0, k * SCORE_SEGMENT, SCORE_WIDTH, (k + 1) * SCORE_SEGMENT)
        sa, sb = ga.crop(box), gb.crop(box)
        w = FIRST_SEGMENT_WEIGHT if k == 0 else 1.0
        s_sum += w * _block_ncc(sa, sb, size=SCORE_SEGMENT)
        e_sum += w * _edge_iou(sa, sb)
        w_sum += w
    structure = s_sum / w_sum
    edge = e_sum / w_sum
    has_text_source = bool(_text_tokens(orig_text))
    if has_text_source:
        text, recall, precision = text_f_score(orig_text, clone_text)
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
        absolute = urljoin(base_url, url)
        if not absolute.startswith(("http://", "https://")):
            continue
        candidates.append(absolute)
        if width > best_width:
            best_width, best = width, absolute
    return best, candidates


# ---------------------------------------------------------------------------
# CSS 递归解析(url() / @import 双语法 / mask-image / image-set)
# ---------------------------------------------------------------------------
def parse_css_refs(css_text: str, base_url: str):
    """提取 CSS 内所有引用, 严格基于该 CSS 自身的远程 URL 拼接相对路径。"""
    refs, seen = [], set()
    candidates = []

    for match in CSS_URL_RE.finditer(css_text):
        candidates.append(match.group(2))  # 覆盖 background/mask/-webkit-mask 等
    for match in CSS_IMPORT_RE.finditer(css_text):
        candidates.append(match.group("u1") or match.group("u2"))
    for match in CSS_IMAGE_SET_RE.finditer(css_text):
        inner = match.group(1)
        for part in re.split(r"\s*,\s*", inner):
            token = part.strip()
            token_match = re.match(
                r"""(?:url\(\s*)?['"]?([^'")\s]+)""", token)
            if token_match:
                candidates.append(token_match.group(1))

    for raw in candidates:
        raw = raw.strip().strip("'\"").strip()
        if not raw or raw.startswith(ASSET_SKIP_PREFIXES):
            continue
        absolute = urljoin(base_url, raw)
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


def apply_replacements(text: str, pairs) -> str:
    """单趟正则替换全部 URL 变体(pairs 需按长度降序)。"""
    if not pairs:
        return text
    mapping = dict(pairs)
    pattern = re.compile("|".join(re.escape(old) for old, _ in pairs))
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
        self._raw_refs = {}              # 原始引用字符串 -> 绝对 URL(本地化替换用)
        self._page_dir = "/"             # 目标页所在目录(目录型 URL, 相对路径基准)
        self._page_frag = ""             # 目标 URL 的 hash 路由(SPA #/login)
        self._pattern_info = {"pattern": "DEFAULT", "features": [], "confidence": 0.0}
        self._strategy_pack = strategy_packs.for_pattern("DEFAULT")
        self._fidelity_score = None           # N1 保真轨评分(双轨取优用)
        self._fidelity_rendered = False      # 保真轨 MHTML 已渲染(每任务一次)
        self._calibrations = 0               # A1 像素校准执行次数
        self._calib_css = ""                 # A1 校准覆盖(跨修复轮持久, _localize 注入)
        self._captured_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        self._net_tape = []              # VCR 网络磁带(W2): XHR/fetch 全量文本响应
        self._tape_seen = set()          # (method, path, query) 去重
        self._tape_bytes = 0
        self._stat_accum = {"css": 0, "js": 0, "img": 0, "fonts": 0,
                            "media": 0, "misc": 0, "bytes": 0}

    # ------------------------------------------------------------------
    def log(self, stage: str, message: str):
        self.bus.publish(self.task_id, stage, message)

    def _set(self, progress=None, stage=None, **extra):
        fields = dict(extra)
        if progress is not None:
            fields["progress"] = int(progress)
        if stage is not None:
            fields["stage"] = stage
        self.db.update_task(self.task_id, **fields)

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
        self._target = self.task["target_url"]
        parts = urlsplit(self._target)
        origin = f"{parts.scheme}://{parts.netloc}"
        domain = self.task["domain"] or parts.netloc
        # 目录型 URL(/apps_resource_supplier/#/login)页面真实位置在目录下,
        # 相对路径(webpack publicPath='' 懒加载 chunk)以此为基准; 记录之,
        # 本地化时按原路径落位, 避免页面挪到根路径后相对引用全体漂移。
        target_path = parts.path or "/"
        if target_path.endswith("/") or "." not in target_path.rsplit("/", 1)[-1]:
            # 归一化: 目标路径是不可信输入, `..` 段会让影子页落盘逃出工作区
            self._page_dir = _normalize_page_dir(target_path)
        self._page_frag = ("#" + parts.fragment) if parts.fragment else ""

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
            if not getattr(self, "_transient_retried", False) and any(
                    m in f"{type(exc).__name__}: {exc}" for m in _TRANSIENT_ERROR_MARKS):
                self.log("WARN", f"瞬时会话崩溃, 自动重试: {type(exc).__name__}: {exc}")
                # 整链重跑前重置运行态(磁带/引用表/校准/保真轨/统计累加器),
                # 防上一次半途采集的残留混入新会话的产物与评分
                self.__init__(self.task_id, self.db, self.bus, self.pool)
                self._transient_retried = True
                self._set(status="running", stage="重试渲染", progress=4, error_msg="")
                return self.run()
            self.log("ERROR", f"任务失败: {type(exc).__name__}: {exc}")
            self._set(status="failed", error_msg=f"{type(exc).__name__}: {exc}"[:1500])
            return
        elapsed = time.time() - started
        self.log("INFO", f"任务 #{self.task_id} 完成, 耗时 {elapsed:.1f}s")

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
                    self.ws, html=pages[0][1] if pages else None)
                self._strategy_pack = strategy_packs.for_pattern(
                    self._pattern_info["pattern"])
                self.log("PATTERN",
                         f"识别模式 {self._pattern_info['pattern']} "
                         f"(置信 {self._pattern_info['confidence']:.2f}, 特征: "
                         f"{','.join(self._pattern_info['features']) or '无'})")
            except Exception as exc:
                self._strategy_pack = strategy_packs.for_pattern("DEFAULT")
                self.log("WARN", f"模式识别失败, 使用默认策略包: {exc}")
            render_and_score = self._make_render_and_score(ctx)

            repair_started = time.time()
            for round_i in range(max(1, config.RENDER_REPAIR_ROUNDS)
                                 if want_score else 1):
                # P0-A 时间预算: 快站快修, 慢站限时收敛 —— 速度是引擎一等公民
                if time.time() - repair_started > config.REPAIR_TIME_BUDGET:
                    self.log("RENDER", f"修复循环时间预算耗尽"
                             f"({config.REPAIR_TIME_BUDGET}s), 提前收敛")
                    break
                self._set(progress=62 + round_i * 6,
                          stage="资源重写" if round_i == 0 else f"修复循环第{round_i + 1}轮")
                pairs = self._compose_pairs(results, routes, origin, srcset_map,
                                            iframe_pages, frame_records)
                self._localize(pages, iframe_pages, frame_records, pairs, pages_map)
                self._rewrite_js_files(results, pairs)
                self._rewrite_mock_bodies(mock_entries, pairs)
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
                if not failed_local and not remote_missing:
                    break
                if round_i >= config.RENDER_REPAIR_ROUNDS - 1:
                    self.log("SMART-DL",
                             f"仍有 {len(failed_local) + len(remote_missing)} 个本地缺失资源"
                             f"(回补轮数已用尽): {sorted(failed_local | {urlsplit(u).path for u in remote_missing})[:6]}")
                    break
                new, shadows = self._failed_backfill(ctx, failed_local,
                                                    remote_missing, results, origin)
                if not new and not shadows:
                    break
                results.update(new)

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
                            self.ws, ai_cfg, detail, self.log, render_and_score)
                        if ai_report.get("final_score") is not None:
                            score = ai_report["final_score"]
                            detail = dict(detail or {})
                            detail["score"] = score
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
            tracker_hook=bool(bake_cfg),
            tracker_cfg=bake_cfg,
        )
        self.log("BUILD", f"Flask 工程已生成: {zip_path.name}")

        stats = self._collect_stats()
        stats["api"] = len(mock_entries)
        if detail is not None:
            stats["similarity_detail"] = detail
        # 双轨取优: 引擎能力 = max(交互轨重演, 保真轨 MHTML 确定性渲染)。
        # 两轨是同一采集的两个交付物, 取更能保真者作为任务还原度。
        fidelity = getattr(self, "_fidelity_score", None)
        # 双轨明细恒记账: 头条分数取优的同时, 两轨原值都可追溯(诚实口径)
        stats["interactive_score"] = score if isinstance(score, (int, float)) else None
        stats["fidelity_score"] = fidelity if isinstance(fidelity, (int, float)) else None
        stats["dominant_track"] = "interactive"
        stats["score_baseline"] = "pristine"   # 基准截图取自 DOM 改动前(v4.5 起)
        if isinstance(fidelity, (int, float)) and \
                (not isinstance(score, (int, float)) or fidelity > score):
            score = fidelity
            stats["dominant_track"] = "fidelity"
        self._set(status="completed", progress=100, stage="完成",
                  similarity_score=score, stats_json=json.dumps(stats, ensure_ascii=False),
                  zip_path=str(zip_path))
        self.db.increment_completed(self.task["batch_id"])

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
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=config.PAGELOAD_TIMEOUT)
        except Exception as exc:
            # 首导航瞬时失败(超时/连接抖动): 重试一次, 防慢站一票判死
            if any(m in str(exc) for m in ("Timeout", "ERR_", "net::")):
                self.log("RENDER", f"首导航失败({type(exc).__name__}), 重试一次")
                page.goto(url, wait_until="domcontentloaded", timeout=config.PAGELOAD_TIMEOUT)
            else:
                raise
        try:
            page.wait_for_load_state("networkidle", timeout=config.NETWORK_IDLE_TIMEOUT)
        except Exception:
            self.log("RENDER", "networkidle 等待超时, 继续后续固化流程")
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
        # 评分基准(纯净原站): 必须在任何引擎 DOM 改动(表单激活/DEEP_FREEZE/
        # 水合剥离/字体烘焙)之前截取 —— 否则评分比较的是"克隆 vs 引擎改过的
        # 原站", 引擎自身引入的失真被基准同步吸收, 分数系统性偏乐观。
        baseline_text = None
        if screenshot:
            with suppress(OSError):   # 重建/重试: 清掉上一轮基准, 防口径混用
                (self.ws / "original_preview.png").unlink()
            try:
                page.screenshot(path=str(self.ws / "original_preview.png"), full_page=True)
                baseline_text = _safe_eval(TEXT_EXTRACT_JS)
                self.log("RENDER", "原站纯净基准快照已保存(DOM 改动前)")
            except Exception as exc:
                self.log("WARN", f"原站截图失败: {exc}")
        activated = _safe_eval(FORM_ACTIVATE_JS)
        if activated:
            self.log("RENDER", f"表单激活: focus/blur {activated} 个输入控件")
        counts = _safe_eval(DEEP_FREEZE_JS)
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
        html = _safe_eval(OUTER_HTML_JS)
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
        text = _safe_eval(TEXT_EXTRACT_JS)
        if screenshot and baseline_text is not None:
            # 原站文本指纹与基准截图同口径(改动前), 采集期显形内容不计入
            text = baseline_text
        if screenshot and not (self.ws / "original_preview.png").is_file():
            # 基准截图失败时的兜底(改动后截图, 记日志以便账本区分口径)
            try:
                page.screenshot(path=str(self.ws / "original_preview.png"), full_page=True)
                self.log("WARN", "原站基准快照回退为改动后截图(口径偏乐观)")
            except Exception as exc:
                self.log("WARN", f"原站截图失败: {exc}")
        collected = _safe_eval(COLLECT_ASSETS_JS)
        iframes = _safe_eval(IFRAME_EXTRACT_JS)
        iframe_srcs = _safe_eval(IFRAME_SRCS_JS)
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
            page.close()
        return {"html": html, "text": text, "urls": collected["urls"],
                "srcsets": collected["srcsets"], "rawRefs": collected["rawRefs"],
                "links": links, "iframes": iframes, "iframe_srcs": iframe_srcs,
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
        """分步滚动到底再回顶, 触发 IntersectionObserver / 无限加载。"""
        try:
            step = 900
            position = 0
            for _ in range(60):
                position += step
                page.evaluate(f"window.scrollTo(0, {position})")
                page.wait_for_timeout(320)
                height = page.evaluate("document.documentElement.scrollHeight")
                if position >= height:
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
            if request.resource_type not in ASSET_RESOURCE_TYPES:
                return
            self._live_seen[response.url] = request.resource_type
        except Exception:
            return

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
        page_netloc = urlsplit(self._target).netloc
        seeded = set()
        for url in results:
            seeded.update(self._variants(url, page_netloc))
        for url in sorted(assets):
            if url.startswith(("http://", "https://")) and url not in seeded:
                jobs.append({"url": url, "referer": origin + "/"})
        seen = set(seeded)
        css_sources, css_refs = {}, {}
        failed_for_browser = []
        total, downloaded = len(jobs), 0
        executor = ThreadPoolExecutor(max_workers=config.DOWNLOAD_WORKERS)
        try:
            while jobs and len(results) < config.MAX_ASSETS_PER_TASK:
                chunk = []
                while jobs and len(chunk) < 32:
                    job = jobs.popleft()
                    if job["url"] in seen:
                        continue
                    seen.add(job["url"])
                    chunk.append(job)
                if not chunk:
                    break
                for job, out in zip(chunk, executor.map(self._fetch_asset, chunk)):
                    out = self._normalize_out(out)
                    if not out["ok"]:
                        self.log("WARN", f"跳过 {job['url']}: {out['error']}")
                        if out.get("status") and ctx is not None:
                            failed_for_browser.append(job)
                        continue
                    info = self._store(out)
                    results[out["url"]] = info
                    downloaded += 1
                    if downloaded % 8 == 0 or not jobs:
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
                                jobs.append({"url": absolute, "referer": out["url"]})
                                total += 1

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
            executor.shutdown(wait=True)

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
            out = self.downloader.fetch_via_browser(url, ctx.request,
                                                    referer=self._target)
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
        for url in missing[:config.LIVE_HARVEST_MAX]:
            if len(results) >= config.MAX_ASSETS_PER_TASK:
                break
            out = self.downloader.fetch_via_browser(url, ctx.request,
                                                    referer=self._target)
            out = self._normalize_out(out)
            if not out["ok"]:
                continue
            info = self._store(out)
            results[out["url"]] = info
            added += 1
            if out["is_css"]:
                text = out["content"].decode("utf-8", "replace")
                for raw, absolute in parse_css_refs(text, out["url"]):
                    if absolute in results or absolute in self._live_seen:
                        continue
                    child = self.downloader.fetch_via_browser(
                        absolute, ctx.request, referer=out["url"])
                    child = self._normalize_out(child)
                    if child["ok"]:
                        results[child["url"]] = self._store(child)
        return added

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
            if len(results) + len(new) >= config.MAX_ASSETS_PER_TASK:
                break
            out = self.downloader.fetch_via_browser(url, ctx.request,
                                                    referer=self._target)
            out = self._normalize_out(out)
            if out["ok"]:
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
            known_sha = self.db.find_asset_name(base)
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
                if len(results) + len(new) >= config.MAX_ASSETS_PER_TASK:
                    break
                out = self.downloader.fetch_via_browser(url, ctx.request,
                                                        referer=self._target)
                out = self._normalize_out(out)
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
        return new, shadows

    # ------------------------------------------------------------------
    def _compose_pairs(self, results: dict, routes: set, origin: str,
                       srcset_map: dict, iframe_pages: list, frame_records: list):
        """构造 (原始引用 -> 本地路径) 替换表, 长字符串优先。"""
        pairs = {}
        page_netloc = urlsplit(self._target).netloc
        for url, info in results.items():
            for variant in self._variants(url, page_netloc):
                pairs[variant] = info["local"]
        for candidate, best in srcset_map.items():
            if candidate in results:   # 口径冻结 2026-11-04: 已落地的候选用自身文件
                continue
            info = results.get(best)
            if info is None:
                continue
            for variant in self._variants(candidate, page_netloc):
                pairs[variant] = info["local"]
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
        return sorted(pairs.items(), key=lambda kv: -len(kv[0]))

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

    @staticmethod
    def _inject_calib(html: str, css: str) -> str:
        """校准覆盖置于 </head> 前(晚于站点样式, 层叠优先)。"""
        style = f'<style data-clone-calib>{css}</style>'
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

        def finalize(html: str, route: str) -> str:
            # <base href> 会让克隆页所有未改写的相对引用按原站指定的基准解析
            # (指向 CDN/原站即整类漂移); 剥离后恢复"按克隆页自身路径解析"
            # 的统一不变量 —— 与 page_dir 影子落盘机制配套
            localized = _BASE_TAG_RE.sub("", rewrite_js_navigation(html))
            if route == "/" and self._calib_css:
                localized = self._inject_calib(localized, self._calib_css)
            localized = self._inject_spa_disguise(localized, route)
            localized = inject_state_restore(localized)
            if bg_css:
                localized = self._inject_after_head(localized, bg_css)
            localized = self._inject_after_head(localized, sim_script)
            return _inject_responsive(localized)

        for route, html in pages:
            fname = "index.html" if route == "/" else f"p__{slugify(route)}.html"
            localized = finalize(apply_replacements(html, pairs), route)
            (self.ws / fname).write_text(localized, encoding="utf-8")
            pages_map[route] = fname
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
                "", rewrite_js_navigation(apply_replacements(html, pairs)))
            localized = self._inject_after_head(localized, sim_script)
            localized = _inject_responsive(localized)
            (self.ws / f"iframe_{i + 1}.html").write_text(
                localized, encoding="utf-8")
            pages_map[f"/iframe_{i + 1}.html"] = f"iframe_{i + 1}.html"
        for i, (_src, html, _text) in enumerate(frame_records):
            localized = _BASE_TAG_RE.sub(
                "", rewrite_js_navigation(apply_replacements(html, pairs)))
            localized = self._inject_after_head(localized, sim_script)
            localized = _inject_responsive(localized)
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

    @staticmethod
    def _variants(url: str, page_netloc: str):
        _p = urlsplit(url)
        scheme, netloc, path = _p.scheme, _p.netloc, _p.path
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
    def _make_render_and_score(self, ctx):
        """构造"本地离线渲染 + 三通道评分"闭包(AI 修复循环复用)。"""
        def render_and_score():
            server = _LocalPreviewServer(
                self.ws, use_tape=self._strategy_pack.net_tape)
            server.start()
            base = f"http://127.0.0.1:{server.port}"
            failed_local, remote_missing = set(), set()
            page = ctx.new_page()
            page.set_default_timeout(30000)
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
                    if resp.request.resource_type not in ASSET_RESOURCE_TYPES:
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
                    if req.resource_type not in ASSET_RESOURCE_TYPES:
                        return
                    url = req.url
                    if url.startswith(base):
                        failed_local.add(urlsplit(url).path or "/")
                except Exception:
                    return

            try:
                page.route("**/*", _route_guard)
                page.on("response", _on_resp)
                page.on("requestfailed", _on_fail)
                # 混合等待: dcl 锚定(重型站 load 可能超时) → 短 load 等待
                # (重水合 SPA 需要资源齐) → 网络静置封顶 → 滚动 → 缓释
                # (W1 起参数由模式策略包供给, 值=现行全局行为)
                page.goto(base + self._page_dir + self._page_frag,
                          wait_until="domcontentloaded", timeout=30000)
                with suppress(Exception):
                    page.wait_for_load_state(
                        "load", timeout=self._strategy_pack.wait_load_timeout)
                with suppress(Exception):
                    page.wait_for_load_state(
                        "networkidle", timeout=self._strategy_pack.wait_networkidle_timeout)
                if self._strategy_pack.scroll_pass:
                    # 懒加载触发: 分段滚动到底再回顶, 派发 IntersectionObserver
                    # (现代站首屏外内容/图片全部懒挂载, 不滚动则永远空白)
                    with suppress(Exception):
                        page.evaluate("""async () => {
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
                        }""")
                page.wait_for_timeout(self._strategy_pack.settle_ms)
                page.screenshot(path=str(self.ws / "cloned_preview.png"), full_page=True)
                clone_text = page.evaluate(TEXT_EXTRACT_JS)
                (self.ws / "cloned_text.txt").write_text(clone_text or "",
                                                         encoding="utf-8")
                # N1 保真轨: MHTML 确定性渲染(Chromium 对 MHTML 默认禁脚本),
                # 与交互轨分开评分 —— 回放保真由构造保证。
                # MHTML 不随修复循环变化 → 每任务只渲染/评分一次(速度)
                if (self.ws / 'capture.mhtml').is_file() and not self._fidelity_rendered:
                    self._fidelity_rendered = True
                    with suppress(OSError):   # 防上一轮/重建残留截图被误评
                        (self.ws / 'fidelity_preview.png').unlink()
                    fpage = ctx.new_page()
                    try:
                        # 离线守卫: 归档内资源由 Chromium 直接供给(不经网络层),
                        # 归档外的引用一律中止 —— 保真轨必须确定且不回源
                        fpage.route("**/*", _fidelity_guard)
                        fpage.goto('file://' + str(self.ws / 'capture.mhtml'),
                                   wait_until='load', timeout=30000)
                        with suppress(Exception):
                            fpage.wait_for_timeout(2000)
                        fpage.screenshot(path=str(self.ws / 'fidelity_preview.png'),
                                         full_page=True)
                        f_text = fpage.evaluate(TEXT_EXTRACT_JS)
                        (self.ws / 'fidelity_text.txt').write_text(
                            f_text or '', encoding='utf-8')
                    except Exception as exc:
                        self.log('WARN', f'保真轨渲染失败: {exc}')
                    finally:
                        with suppress(Exception):
                            fpage.close()
            except Exception as exc:
                self.log("WARN", f"克隆页渲染失败: {exc}")
                with suppress(Exception):
                    page.screenshot(path=str(self.ws / "cloned_preview.png"),
                                    full_page=True)
                clone_text = ""
            remote_missing.discard("")
            # 页面/沙盒在评分与 A1 校准全程保持存活(校准需在活页上 elementFromPoint
            # 并重导航重截图; 旧实现在此前已 close/stop, 校准恒抛异常从未生效)
            try:
                original = self.ws / "original_preview.png"
                cloned = self.ws / "cloned_preview.png"
                if not (original.is_file() and cloned.is_file()):
                    return None, failed_local, remote_missing
                try:
                    orig_text = (self.ws / "original_text.txt").read_text(
                        encoding="utf-8", errors="replace") if (self.ws / "original_text.txt").is_file() else ""
                    detail = compute_visual_similarity(
                        original, cloned, diff_path=self.ws / "diff_preview.png",
                        orig_text=orig_text, clone_text=clone_text or "")
                    # A1 完整版: 像素差分 → 元素映射 → CSS 覆盖合成(确定性校准闭环)。
                    # 网格差分定位色差格(原站侧纯色格才取), elementFromPoint 映射到
                    # 克隆 DOM 元素, 以原站格色生成覆盖 —— 注入后重渲染重评分一轮。
                    if detail is not None and (detail.get('score') or 0) < 99 \
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
                                cal_css = pixel_calibrator.element_css(page, cal_cells)
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
                                        cal_style = (f'<style data-clone-calib>'
                                                     f'{cal_css}</style>')
                                        if 'data-clone-calib' in doc:
                                            doc = re.sub(
                                                r'<style data-clone-calib>.*?</style>',
                                                lambda _m: cal_style, doc, count=1, flags=re.S)
                                        else:
                                            doc = self._inject_calib(doc, cal_css)
                                        idx_html.write_text(doc, encoding='utf-8')
                                        self._calibrations += 1
                                        self.log("RENDER", f"像素校准: {len(cal_cells)} "
                                                 f"格差 → {len(cal_css.splitlines())} 条覆盖, 重评分")
                                        page.goto(base + self._page_dir + self._page_frag,
                                                  wait_until='domcontentloaded', timeout=30000)
                                        with suppress(Exception):
                                            page.wait_for_load_state(
                                                'load', timeout=self._strategy_pack.wait_load_timeout)
                                        page.wait_for_timeout(self._strategy_pack.settle_ms)
                                        page.screenshot(path=str(cloned), full_page=True)
                                        clone_text = page.evaluate(TEXT_EXTRACT_JS)
                                        (self.ws / 'cloned_text.txt').write_text(
                                            clone_text or '', encoding='utf-8')
                                        detail = compute_visual_similarity(
                                            original, cloned,
                                            diff_path=self.ws / 'diff_preview.png',
                                            orig_text=orig_text, clone_text=clone_text or '')
                                        # 回滚守卫: 校准必须净提升, 否则撤销覆盖并恢复上一版
                                        # 截图/文本/明细(色块覆盖误伤内容时不允许拉低分数)
                                        if (detail.get('score') or 0) < (prev_detail.get('score') or 0):
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
                    # N1 保真轨评分: MHTML 确定性渲染 vs 原站(独立于交互轨)
                    fid_png = self.ws / 'fidelity_preview.png'
                    fid_txt = self.ws / 'fidelity_text.txt'
                    if fid_png.is_file() and self._fidelity_score is None:
                        try:
                            f_detail = compute_visual_similarity(
                                original, fid_png,
                                diff_path=self.ws / 'fidelity_diff.png',
                                orig_text=orig_text,
                                clone_text=fid_txt.read_text(errors='replace'))
                            self._fidelity_score = f_detail.get('score')
                            self.log("RENDER",
                                     f"保真轨评分: {self._fidelity_score} "
                                     f"(交互轨: {detail.get('score')})")
                        except Exception as exc:
                            self.log("WARN", f"保真轨评分失败: {exc}")
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
