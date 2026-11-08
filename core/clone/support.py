"""克隆引擎的模块级工具: 安全边界、URL/CSS/HTML 改写、截图评分、本地沙盒预览等
(自 core/cloner_engine.py 纯搬迁, 语句逐字不变)。"""
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

from core.clone.js import (  # noqa: F401  纯搬迁后的再导出, 外部 import 路径不变
    AD_RECTS_JS,
    ALT_EXTRACT_JS,
    ALT_INJECT_JS,
    ANIM_ALIGN_JS,
    ANIM_PHASE_JS,
    BASELINE_EAGER_JS,
    BLOB_FETCH_JS,
    CLONE_IMG_DONE_JS,
    CLONE_IMG_EAGER_JS,
    COLLAPSED_PROBE_JS,
    COLLAPSE_REVERT_JS,
    COLLECT_ASSETS_JS,
    COLLECT_LINKS_JS,
    CV_ATTR_FN,
    CV_EMIT_JS,
    CV_PATHS_JS,
    CV_PATH_FN,
    CV_QUIET_JS,
    CV_RECORD_JS,
    DEEP_FREEZE_JS,
    FONT_BAKE_JS,
    FORM_ACTIVATE_JS,
    IFRAME_EXTRACT_JS,
    IFRAME_SRCS_JS,
    IMG_PENDING_JS,
    INLINE_FLUID_JS,
    LAYOUT_MAP_JS,
    LAZY_JS,
    MARK_HIDDEN_FRAMES_JS,
    MARK_LOADED_IMGS_JS,
    MEDIA_SHOT_APPLY_JS,
    MEDIA_SHOT_JS,
    OUTER_HTML_JS,
    PAGE_PROBE_JS,
    RAF_FREEZE_JS,
    REMOVE_SCRIPTS_JS,
    RESPONSIVE_CSS,
    RUNTIME_FP_JS,
    SCOPED_BAKE_FN,
    SCORE_TIMER_GUARD_JS,
    SETTLE_CLEANUP_JS,
    SETTLE_INSTALL_JS,
    SETTLE_READY_JS,
    SHADOW_SERIALIZE_JS,
    SHOT_DIAG_JS,
    STORAGE_SNAPSHOT_JS,
    STYLE_TEXTS_JS,
    SUBTREE_SIZE_JS,
    TEXT_EXTRACT_JS,
    TIME_FREEZE_JS,
    TRACKER_OBSERVE_JS,
    UPDATE_PROMPT_CLICK_JS,
    UPDATE_PROMPT_PROBE_JS,
    VIDEO_FRAME_READY_JS,
    VIDEO_SEEK_DONE_JS,
    VIDEO_SEEK_JS,
    WAAPI_BAKE_JS,
)


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

# 响应式适配注入(仅限原站没有 viewport meta 的老式定宽站): 补 viewport meta + 手机防溢出。
# 原站自带 viewport meta 说明它自己做了响应式, 任何兜底 CSS 都只会改坏它: 旧版对所有页
# 注入 `img{height:auto!important}`(压掉 h-full/object-cover 的定高图, huawei 手机视口
# 首图拉成 2865px 高) 与 `html,body{overflow-x:auto!important}`(body 成为滚动容器,
# 整页高度塌成一屏, gitee 手机视口只剩 844px)。
RESPONSIVE_META = ('<meta name="viewport" '
                   'content="width=device-width, initial-scale=1.0">')

_VIEWPORT_META_RE = re.compile(r"<meta\b[^>]*\bname\s*=\s*[\"']?viewport\b", re.I)


def _inject_responsive(html: str, enhance: bool = False) -> str:
    """原站已有 viewport meta → 原样返回(它自己的媒体查询就是权威)。
    没有 meta 的老式定宽站: 默认也原样返回 —— 触屏浏览器对它按 980 布局视口缩小显示, 补上
    device-width meta 会让克隆在真机上换成另一种版式(douban/baidu iPad 真机口径 66/45, 原站自比 ~100)。
    enhance=True(任务配置 responsive_enhance, 主动要"响应式增强")时才补 meta + 兜底 CSS。"""
    if not enhance or _VIEWPORT_META_RE.search(html):
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

_CV_BAND_RE = re.compile(r"@media [^{]*?/\*cv-([dtmx])\*/\{")


def scope_viewport_css(html: str, floor_d: int, floor_t: int) -> str:
    """多视口内联样式表的三段占位 → 真实区间。断点缺失时按常见栅格取分界。"""
    if "/*cv-" not in html:
        return html
    sd = floor_d if floor_d > 768 else 1024
    st = floor_t if 390 < floor_t <= 768 else 600
    conds = {"d": f"@media (min-width: {sd}px)/*cv-d*/{{",
             "t": f"@media (min-width: {st}px) and (max-width: {sd - 0.02:.2f}px)/*cv-t*/{{",
             "m": f"@media (max-width: {st - 0.02:.2f}px)/*cv-m*/{{",
             "x": f"@media (max-width: {sd - 0.02:.2f}px)/*cv-x*/{{"}
    return _CV_BAND_RE.sub(lambda m: conds[m.group(1)], html)

# 运行时 blob: URL(会话内有效) → data: URL; 单个上限 5MB
BLOB_URL_RE = re.compile(r"blob:(?:https?://[^/\s\"'<>()]+|null)/[0-9a-fA-F-]{36}")
BLOB_INLINE_MAX = 60

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


# ES 模块相对说明符: import x from './chunk.js' / export * from '../y.js' / import('./z.js')。
# 模块被本地化到 /static/js/<名>.js 后, 相对说明符按本地目录解析, 原 CDN 目录里的兄弟模块全部 404
# (allbirds: Vite/Vue 主题 40 个 chunk 404, 应用不起, 手机端整页黄块)。字面量替换表只有绝对 URL, 改不到它们。
_MOD_SPEC_RE = re.compile(
    r"""((?:\bimport|\bexport)\s*(?:[\w$*{}\s,]{0,400}?\s*from\s*)?|\bimport\s*\(\s*)(['"])(\.{1,2}/[^'"\n]{1,300})\2""")


def rewrite_module_imports(text: str, module_url: str, lookup) -> str:
    """把模块里的相对 import/export 说明符按模块原地址绝对化, 命中资源池(lookup 返回本地路径)的改成本地路径。

    未命中(未采到的 chunk)原样保留; 非模块文件(没有 import/export 字样或没有 ./ ../)直接返回。"""
    if not module_url or not text or ("./" not in text) or ("import" not in text and "export" not in text):
        return text

    def rep(m):
        try:
            absolute = urljoin(module_url, m.group(3))
        except ValueError:
            return m.group(0)
        local = lookup(absolute)
        if not local or not isinstance(local, str):
            return m.group(0)
        return m.group(1) + m.group(2) + local + m.group(2)
    return _MOD_SPEC_RE.sub(rep, text)


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
    r"Access Denied|Too Many Requests|Request blocked|Pardon Our Interruption|"
    # Cloudflare/Turnstile 新版挑战页: "Verify you are human" 在挑战 iframe 里, 主文档只有这几句
    r"Performing security verification|Verifying you are human|Enable JavaScript and cookies to continue|"
    # 网关/CDN 错误页(上游超时、源站宕机): 克隆它没有意义, 分数不反映引擎能力
    r"Gateway time-?out|Bad gateway|Error code 5\d\d|Origin is unreachable|Web server is down|"
    r"Service (?:Temporarily )?Unavailable", re.I)
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
    return _f_from_counts(*_token_counts(orig_text, clone_text), beta)  # type: ignore[call-arg]


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


STATIC_LINK_REL_FILTER = os.environ.get("CLONER_STATIC_LINK_REL_FILTER", "1") != "0"
_LINK_REL_RE = re.compile(r"""\srel\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)


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

    page_self = (base or "").split("#", 1)[0]
    for m in _STATIC_ATTR_RE.finditer(html or ""):
        attr, val = m.group(1).lower(), m.group(2) if m.group(2) is not None else m.group(3)
        if STATIC_LINK_REL_FILTER and m.group(0)[1:5].lower() == "link":
            # <link> 只取样式/图标/预载(与 COLLECT_ASSETS_JS 同口径): rel=alternate/canonical/hreflang
            # 指向的是其它语言/地区的整页 HTML(apple 一页 45 份 × ~450KB 当资产下载)
            end = (html or "").find(">", m.end())
            tag = (html or "")[m.start(): end if end > 0 else m.end()]
            rel = _LINK_REL_RE.search(tag)
            rel_v = (rel.group(1) or rel.group(2) or rel.group(3) or "").lower() if rel else ""
            if not any(k in rel_v for k in ("stylesheet", "icon", "preload", "prefetch")):
                continue
        if attr != "srcset" and page_self and \
                _urljoin(base, _html.unescape((val or "").strip())).split("#", 1)[0] == page_self:
            continue   # 页面自身地址(canonical/自引用)不是资产
        if attr == "srcset":
            srcsets.append(_html.unescape(val))
        else:
            _add(val)
    for raw, _absolute in parse_css_refs(html or "", base):
        _add(raw)
    return {"urls": urls, "srcsets": srcsets, "rawRefs": raw_refs}


def _mask_rects(path, rects):
    """按文档坐标把矩形区域涂成中灰(截图宽度即文档宽度, 越界自动裁)。"""
    with Image.open(path) as im:
        im.load()
        out = im.convert("RGB")
    draw = ImageDraw.Draw(out)
    w, h = out.size
    for x, y, rw, rh in rects:
        x0, y0, x1, y1 = max(0, x), max(0, y), min(w, x + rw) - 1, min(h, y + rh) - 1
        if x1 < x0 or y1 < y0:      # 越界/零面积(负尺寸、整块在截图上方或左侧)
            continue
        draw.rectangle([x0, y0, x1, y1], fill=(128, 128, 128))
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
            with suppress((BrokenPipeError, ConnectionResetError)):  # type: ignore[arg-type]
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


# 埋点/广告信标: 渲染驱动回补不回源(真实语料里修复回补 20~65s 多数耗在这类请求上, 每轮带随机参数
# 都"不同", 负缓存挡不住; 它们是 1x1 像素或计数接口, 回补进来也不改变画面)。只按通用形态识别,
# 不认具体站点: 路径段是常见上报动词, 或主机名首段是常见上报子域, 或「图片扩展名 + 超长查询串」。
_BEACON_SEG = re.compile(r"(?:^|/)(?:log|logs|logger|track|tracker|tracking|beacon|pixel|collect|"
                         r"stat|stats|statistics|report|monitor|ping|pv|uv|click|impression|analytics|"
                         r"metrics|telemetry|rum|gtag|ga|adx?|ads|adserver|eclick|cm)(?:$|[/.])", re.I)
_BEACON_HOST = re.compile(r"^(?:track|tracker|tracking|log|logs|stat|stats|analytics|metrics|telemetry|"
                          r"beacon|pixel|collect|eclick|click|pos|ad|ads|adx|cm|data|report|mon|monitor)"
                          r"[0-9-]*\.", re.I)


def beacon_like(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    if _BEACON_HOST.match(host):
        return True
    if _BEACON_SEG.search(path):
        return True
    ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    return ext in ("gif", "jpg", "jpeg", "png", "bin", "") and len(parts.query) >= 120


# 交付产物部署性能守卫: 落盘前把"未本地化的阻塞 <script src>"去阻塞化。 -----------
# 根因(cgnpc 562 复盘): 采集时就 404/被跳过的外链计数脚本(cmsHitsCount.js)与跨域埋点图标
# (govicon.js)仍留在交付 HTML 里作经典阻塞脚本。本地部署时它们本地无文件 → 走 Flask 反代兜底
# 回源(timeout 20s)或直连一个远端慢主机, 经典脚本在解析期阻塞, 首屏被拖到 20s+。
# 评分口径一直量不到: 离线渲染的 route guard 对非本地请求一律瞬时 abort, 而交付产物是等 20s。
# 通用处置(不认具体站点, 只按形态): 已本地化(/static/、相对影子路径、data:)的保持不动;
# 其余绝对/根相对未捕获的阻塞脚本 —— 埋点/计数/信标形态整条删除, 其余补 async, 使其永不阻塞首屏。
_SCRIPT_SRC_TAG_RE = re.compile(
    r"<script\b(?P<attrs>[^>]*?)\bsrc\s*=\s*(?P<q>[\"'])(?P<src>.*?)(?P=q)(?P<rest>[^>]*)>",
    re.I | re.S)

# 计数器/统计脚本的通用命名形态(不认具体站点): hitcount / viscount / pvcount / cmsHits 等。
_HITCOUNT_HINT = re.compile(
    r"(?:hit|visit|view|page)s?[-_]?count|count[-_]?(?:hit|visit|view|pv|uv)|cmshits", re.I)


def _script_src_is_localized(src: str) -> bool:
    """落盘后由静态服务器直接命中、不触发反代回源的引用形态。"""
    s = (src or "").strip()
    if not s:
        return True   # 空 src 不发请求
    low = s.lower()
    if low.startswith(("data:", "blob:", "about:", "javascript:", "#")):
        return True
    if low.startswith(("http://", "https://", "//")):
        return False   # 绝对/协议相对: 未被本地化(本地化会改成 /static 根相对)
    if s.startswith("/"):
        # 根相对: 仅 /static/ 是内容寻址的已捕获资源; 其余根相对路径(/cgnhit/... 等)
        # 落盘无文件, 必走反代兜底
        return low.startswith("/static/")
    return True        # 页面目录相对路径: 影子文件随包落盘, 静态直通


def neutralize_deploy_hazard_scripts(html: str, page_host: str = "") -> str:
    """交付 HTML 去阻塞: 未本地化的阻塞 <script src> 不得挡首屏(删埋点 / 其余转 async)。"""
    if "<script" not in html.lower():
        return html

    def _repl(m):
        src = m.group("src")
        if _script_src_is_localized(src):
            return m.group(0)
        attrs = m.group("attrs") or ""
        rest = m.group("rest") or ""
        flags = (attrs + " " + rest).lower()
        if "async" in flags or "defer" in flags:
            return m.group(0)   # 本就非阻塞
        abs_url = src if src.lower().startswith(("http://", "https://")) else (
            "https:" + src if src.startswith("//") else "https://_rel_/" + src.lstrip("/"))
        # 埋点/计数/信标形态: 部署后只会回源打点, 不改画面 → 整条移除
        if beacon_like(abs_url) or _HITCOUNT_HINT.search(src):
            return ""
        # 其余未捕获阻塞脚本(可能是跨域库/未采到的应用脚本): 转 async, 保留 best-effort
        # 执行但不再阻塞解析与首屏; 配合反代短超时+多线程, 后台 404 代价可控。
        return f"<script{attrs} src={m.group('q')}{src}{m.group('q')}{rest} async>"

    return _SCRIPT_SRC_TAG_RE.sub(_repl, html)


# 渲染阻塞样式表(举一反三, 全语料 554 页复盘: 未本地化的阻塞 <link rel=stylesheet> 59 处/45 站,
# 是脚本那一类的 4 倍; 44 处是根相对但原站也 404 的路径, 15 处跨域)。样式表阻塞的是**首次绘制**:
# 部署后浏览器白屏等它加载/失败, 走反代兜底就是等满超时。未被捕获的样式表, 评分渲染时也没有它,
# 克隆画面不依赖它 —— 改为异步加载(media=print + onload 还原), 到了就生效, 到不了不挡首屏。
_LINK_TAG_RE = re.compile(r"<link\b(?P<attrs>[^>]*)>", re.I | re.S)
_ATTR_RE_CACHE = {}


def _attr(attrs: str, name: str):
    rx = _ATTR_RE_CACHE.get(name)
    if rx is None:
        rx = _ATTR_RE_CACHE[name] = re.compile(
            r"\b" + name + r"\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.I)
    m = rx.search(attrs)
    if not m:
        return None
    return next((g for g in m.groups() if g is not None), "")


def neutralize_deploy_hazard_styles(html: str) -> str:
    """交付 HTML: 未本地化的渲染阻塞样式表改为异步加载(不挡首次绘制)。"""
    if "stylesheet" not in html.lower():
        return html

    def _repl(m):
        attrs = m.group("attrs") or ""
        rel = (_attr(attrs, "rel") or "").lower()
        if "stylesheet" not in rel.split() and rel != "stylesheet":
            return m.group(0)
        href = _attr(attrs, "href")
        if href is None or _script_src_is_localized(href):
            return m.group(0)
        if "data-clone-async-css" in attrs.lower():
            return m.group(0)
        media = _attr(attrs, "media")
        restore = (media or "all").replace("'", "\\'")
        # 去掉原 media(若有), 换成 print + onload 还原
        new_attrs = re.sub(r"\s*\bmedia\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", "", attrs, flags=re.I)
        return (f"<link{new_attrs} media=\"print\" "
                f"onload=\"this.media='{restore}'\" data-clone-async-css>")

    return _LINK_TAG_RE.sub(_repl, html)


# 资源提示与外部 iframe(全语料: 未本地化 preload/prefetch 275 处/121 站, preconnect/dns-prefetch
# 1010 处/239 站, 跨域 iframe 168 处/92 站)。提示类标签只是"预热", 指向未本地化地址时部署后只会
# 多发一轮回源/外联请求(抢带宽、泄露访问), 删掉不影响任何渲染; 跨域 iframe(视频/地图/广告)无法
# 本地化, 补 loading=lazy 让它不与首屏资源抢加载(评分渲染本就拦截外联, 画面不变)。
_IFRAME_TAG_RE = re.compile(r"<iframe\b(?P<attrs>[^>]*)>", re.I | re.S)


def neutralize_deploy_hints(html: str) -> str:
    """删未本地化的 preload/prefetch/modulepreload 与所有 preconnect/dns-prefetch; 外部 iframe 懒加载。"""
    low = html.lower()
    if "<link" in low:
        def _link(m):
            attrs = m.group("attrs") or ""
            rel = (_attr(attrs, "rel") or "").lower().split()
            if not rel:
                return m.group(0)
            if "preconnect" in rel or "dns-prefetch" in rel:
                return ""
            if any(r in rel for r in ("preload", "prefetch", "modulepreload", "prerender")):
                href = _attr(attrs, "href")
                if href is not None and not _script_src_is_localized(href):
                    return ""
            return m.group(0)
        html = _LINK_TAG_RE.sub(_link, html)
    if "<iframe" in low:
        def _iframe(m):
            attrs = m.group("attrs") or ""
            src = _attr(attrs, "src")
            if src is None or _script_src_is_localized(src):
                return m.group(0)
            if re.search(r"\bloading\s*=", attrs, re.I):
                return m.group(0)
            return f"<iframe{attrs} loading=\"lazy\">"
        html = _IFRAME_TAG_RE.sub(_iframe, html)
    return html


# 远程字体(全语料: 本地化后的 CSS 里仍有 10 万条 url(https://...) 字体切片引用, 99% 集中在采集时
# ConnectTimeout 的字体 CDN)。浏览器按 unicode-range 懒请求切片, 每片默认 font-display:auto =
# 文字先隐形(FOIT)最多 3s 等字体 —— 部署在连不上该 CDN 的环境里, 整页文字"闪白"数秒。
# 通用处置: @font-face 的 src 含未本地化 url() 且未声明 font-display 时补 font-display:swap,
# 文字立即以回退字体显示, 字体到了再换; 字体到不了画面与评分渲染(拦截外联)一致。
_FONTFACE_RE = re.compile(r"@font-face\s*\{(?P<body>[^{}]*)\}", re.I | re.S)
_CSS_REMOTE_URL_RE = re.compile(r"url\(\s*[\"']?(?P<u>[^\"')\s]+)", re.I)


def fontface_swap_for_remote(css: str) -> str:
    """CSS 文本: 含未本地化 src 的 @font-face 补 font-display:swap(已声明的不动)。返回新 CSS。"""
    if "@font-face" not in css.lower():
        return css

    def _repl(m):
        body = m.group("body")
        if re.search(r"font-display\s*:", body, re.I):
            return m.group(0)
        urls = [u.strip() for u in _CSS_REMOTE_URL_RE.findall(body)]
        if not any(not _script_src_is_localized(u) for u in urls):
            return m.group(0)
        # 浏览器实际取用的是最后一条 src 里首个受支持格式的 url(): 它已本地化时, 后面留着远程地址的
        # 兜底格式(字体按需下载不取)永不请求, 不必改 font-display
        srcs = list(_FF_SRC_RE.finditer(body))
        if srcs:
            for entry in _split_top_commas(srcs[-1].group(1)):
                um = _FF_URL_RE.search(entry)
                if not um:
                    continue
                u = (um.group(1) or um.group(2) or um.group(3) or "").strip()
                if _font_entry_supported(entry, u):
                    if _script_src_is_localized(u):
                        return m.group(0)
                    break
        sep = "" if body.rstrip().endswith(";") or not body.strip() else ";"
        return "@font-face{" + body.rstrip() + sep + "font-display:swap;}"

    return _FONTFACE_RE.sub(_repl, css)


_STYLE_BLOCK_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)", re.I | re.S)


def neutralize_deploy_fonts(html: str) -> str:
    """交付 HTML 内联 <style> 里的远程 @font-face 补 font-display:swap。"""
    if "@font-face" not in html.lower():
        return html
    return _STYLE_BLOCK_RE.sub(lambda m: m.group(1) + fontface_swap_for_remote(m.group(2)) + m.group(3), html)


def neutralize_deploy_hazards(html: str, page_host: str = "") -> str:
    """交付 HTML 去阻塞总入口: 阻塞脚本 + 渲染阻塞样式表 + 资源提示/外部 iframe + 远程字体 swap。"""
    return neutralize_deploy_fonts(neutralize_deploy_hints(neutralize_deploy_hazard_styles(
        neutralize_deploy_hazard_scripts(html, page_host))))


def deploy_hazard_audit(html: str, page_host: str = "") -> dict:
    """部署隐患审计(评分口径之外的交付侧度量): 仍会阻塞首屏的未本地化脚本/样式数、
    未本地化引用总数、涉及的外部主机。写入任务 stats, 语料跑分汇总, 让交付侧也被量到。"""
    blocking_scripts, blocking_styles, uncaptured = 0, 0, 0
    hosts = set()
    for m in _SCRIPT_SRC_TAG_RE.finditer(html):
        src = m.group("src")
        if _script_src_is_localized(src):
            continue
        uncaptured += 1
        flags = ((m.group("attrs") or "") + " " + (m.group("rest") or "")).lower()
        if "async" not in flags and "defer" not in flags:
            blocking_scripts += 1
        h = (urlsplit(src if "://" in src else "https:" + src).hostname or "") if src.lower().startswith(
            ("http://", "https://", "//")) else ""
        if h and h != (page_host or "").lower():
            hosts.add(h)
    for m in _LINK_TAG_RE.finditer(html):
        attrs = m.group("attrs") or ""
        rel = (_attr(attrs, "rel") or "").lower()
        if "stylesheet" not in rel:
            continue
        href = _attr(attrs, "href")
        if href is None or _script_src_is_localized(href):
            continue
        uncaptured += 1
        if "data-clone-async-css" not in attrs.lower() and (_attr(attrs, "media") or "all") != "print":
            blocking_styles += 1
        if href.lower().startswith(("http://", "https://", "//")):
            h = urlsplit(href if "://" in href else "https:" + href).hostname or ""
            if h and h != (page_host or "").lower():
                hosts.add(h)
    return {"blocking_scripts": blocking_scripts, "blocking_styles": blocking_styles,
            "uncaptured_refs": uncaptured, "external_hosts": sorted(hosts)}


# 重放去重守卫(交互轨通用): 冻结 DOM 已含页面脚本运行后的结果, 交互版再执行一遍同步脚本时,
# 「插入型」脚本(insertAdjacentHTML / appendChild 追加组件、轮播复制幻灯片等)会把同样的内容再插一次,
# 页面出现成段重复(example.com 交互轨 70.4 / 保真轨 100 即此类)。守卫在所有页面脚本之前挂钩 DOM
# 插入 API, 只标记**解析期间(readyState=loading, 即同步经典脚本重放)由脚本新建并插入**的元素(移动已在
# 文档中的节点不算; DOMContentLoaded 之后的异步/模块脚本插入一律不管——苹果类页面会先插新节点、稍后再
# 移除旧节点, 提前去重会两边都丢, 实测 airpods-pro 交互轨 70.5→48); 若被标记元素在同一父节点
# 下有相同(outerHTML, 忽略引擎自加的 data-cb/data-cv/data-cssom/data-clone-* 属性)且未被标记的兄弟——即冻结 DOM 里已有的同一份——则移除这份重放副本。
# 框架水合/重渲染会先移除旧节点, 不会留下"旧的仍在"的孪生, 因而不受影响。
REPLAY_DEDUPE_JS = r"""<script data-clone-preview="replay-dedupe">(function(){try{if(window.__cloneDedupe)return;window.__cloneDedupe=1;var M=[],mark=new WeakSet(),mo=new MutationObserver(function(){});mo.observe(document.documentElement,{childList:true,subtree:true});
function take(was){var rs=mo.takeRecords();if(document.readyState!=='loading')return;for(var i=0;i<rs.length;i++){var a=rs[i].addedNodes;for(var j=0;j<a.length;j++){var n=a[j];if(n.nodeType===1&&!(was&&was.indexOf(n)>=0)&&!mark.has(n)){mark.add(n);if(M.length<4000)M.push(n);}}}}
function wrap(P,k){var d=Object.getOwnPropertyDescriptor(P,k);if(!d||typeof d.value!=='function')return;var f=d.value;P[k]=function(){var was=[];for(var i=0;i<arguments.length;i++){var x=arguments[i];if(x&&x.nodeType===1&&x.isConnected)was.push(x);}mo.takeRecords();var r=f.apply(this,arguments);take(was);return r;};}
function wrapSet(P,k){var d=Object.getOwnPropertyDescriptor(P,k);if(!d||!d.set)return;Object.defineProperty(P,k,{configurable:true,enumerable:d.enumerable,get:d.get,set:function(v){mo.takeRecords();d.set.call(this,v);take(null);}});}
['appendChild','insertBefore','replaceChild'].forEach(function(k){wrap(Node.prototype,k);});
['insertAdjacentHTML','insertAdjacentElement','append','prepend','after','before','replaceWith','replaceChildren'].forEach(function(k){wrap(Element.prototype,k);});
wrapSet(Element.prototype,'innerHTML');wrapSet(Element.prototype,'outerHTML');
var R=/ data-(?:cb|cv[\w-]*|cssom|clone[\w-]*)="[^"]*"/g;function sweep(){try{take(null);var n=0;for(var i=0;i<M.length;i++){var m=M[i];if(!m||!m.isConnected||!m.parentNode)continue;var t=m.tagName;if(t==='SCRIPT'||t==='HTML'||t==='HEAD'||t==='BODY')continue;var h=m.outerHTML;if(h.length<24||h.length>400000)continue;h=h.replace(R,'');var tc=m.textContent,s=m.parentNode.children;for(var j=0;j<s.length;j++){var e=s[j];if(e===m||mark.has(e)||e.tagName!==t||e.textContent!==tc)continue;if(e.outerHTML.replace(R,'')===h){m.remove();M[i]=null;n++;break;}}}if(n)window.__cloneDedupeRemoved=(window.__cloneDedupeRemoved||0)+n;}catch(e){}}
window.__cloneDedupeSweep=sweep;document.addEventListener('DOMContentLoaded',function(){sweep();try{mo.disconnect();}catch(e){}});window.addEventListener('load',function(){sweep();setTimeout(sweep,800);setTimeout(sweep,2500);});}catch(e){}})();</script>"""


# ---------------------------------------------------------------------------
# 手机版(按 UA 分发的站): 探测判定 + 克隆入口路由
# ---------------------------------------------------------------------------
# 很多站对手机 UA 返回另一套页面(跳 m./3g. 子域, 或同址下发移动模板), 真机打开原站看到的
# 是手机版, 而桌面 UA 采集的克隆在手机上只能把桌面版挤窄。判定只看两个通用信号:
# 落地地址不同, 或页面骨架(标签 + class 词集合)几乎不重合。响应式站(同一份 HTML 靠媒体
# 查询切换)两侧骨架高度重合, 不会误判。
_SIG_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9-]*)\b([^>]*)>")
_SIG_CLASS_RE = re.compile(r"""\bclass\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_SIG_SKIP_TAGS = {"html", "head", "body", "meta", "link", "script", "style", "noscript", "br", "template"}


def dom_signature(html: str, limit: int = 6000) -> set:
    """页面骨架词集合: 标签名与 tag.class 词(前 limit 个元素), 文本与属性值不计。"""
    out = set()
    body = html or ""
    m = re.search(r"<body\b", body, re.I)
    if m:
        body = body[m.start():]
    for i, t in enumerate(_SIG_TAG_RE.finditer(body)):
        if i >= limit:
            break
        tag = t.group(1).lower()
        if tag in _SIG_SKIP_TAGS:
            continue
        out.add(tag)
        c = _SIG_CLASS_RE.search(t.group(2) or "")
        if c:
            for w in (c.group(1) or c.group(2) or "").split():
                if not re.search(r"\d{3,}|^(active|current|cur|on|show|hide|hover|selected)$", w):
                    out.add(f"{tag}.{w}")
    return out


def _edition_url_key(url: str) -> tuple:
    p = urlsplit(url or "")
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host, (p.path or "/").rstrip("/") or "/"


def separate_phone_edition(desktop_url: str, desktop_html: str, phone_url: str, phone_html: str,
                           threshold: float = 0.45):
    """手机 UA 下是否是另一套页面。返回 (是否, 原因说明)。"""
    if not phone_html or len(phone_html) < 200:
        return False, "手机 UA 页为空"
    if _edition_url_key(desktop_url) != _edition_url_key(phone_url):
        return True, f"手机 UA 落地地址不同: {phone_url}"
    a, b = dom_signature(desktop_html), dom_signature(phone_html)
    if len(a) < 8 or len(b) < 8:
        return False, "骨架过小, 不判定"
    sim = len(a & b) / max(1, len(a | b))
    if sim < threshold:
        return True, f"同址下发不同模板(骨架重合 {sim:.2f} < {threshold})"
    return False, f"同一套页面(骨架重合 {sim:.2f})"


PHONE_EDITION_FILE = "index.m.html"
# 克隆入口路由: 手机 UA 打开克隆时换到手机版(与原站按 UA 分发同口径); ?clone_edition=desktop 可强制看桌面版。
# 平板(iPadOS 默认发桌面 UA、安卓平板不带 Mobile)与原站一样留在桌面版。
EDITION_ROUTER_SCRIPT = (
    '<script data-clone-preview="edition-router">(function(){try{var u=navigator.userAgent||"";'
    'if(!/iPhone|iPod|Android.+Mobile|Windows Phone|Mobile.+Firefox|HarmonyOS.+Mobile|OpenHarmony.+Mobile/i.test(u))return;'
    'if(/[?&]clone_edition=desktop/.test(location.search))return;'
    'var p=location.pathname,i=p.lastIndexOf("/");'
    'location.replace(p.slice(0,i+1)+"' + PHONE_EDITION_FILE + '"+location.search+location.hash);'
    '}catch(e){}})();</script>'
)


def inject_edition_router(html: str) -> str:
    """把入口路由插到 <head> 最前(早于时间冻结/SPA 伪装等注入, 先跳转再说); 已有则不重复。"""
    if not html or 'data-clone-preview="edition-router"' in html:
        return html
    m = re.search(r"<head\b[^>]*>", html, re.I)
    at = m.end() if m else 0
    return html[:at] + EDITION_ROUTER_SCRIPT + html[at:]


def merge_static_tree(src: Path, dst: Path) -> int:
    """手机版资源并入主工作区(CAS 文件名, 同名即同内容): 只拷主工作区没有的文件。"""
    import shutil
    n = 0
    if not src.is_dir():
        return 0
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        target = dst / f.relative_to(src)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        n += 1
    return n


# ---------------------------------------------------------------------------
# @font-face 下载计划: 只下浏览器确实会用到的那份字体文件
# ---------------------------------------------------------------------------
# 实测(cz 本机 250 任务): apple.com.cn 每次下 164 个字体文件 42.6MB(同一字重 woff2/woff/ttf/eot
# 四份兜底, 占全部下载字节 83%), wordpress.org 156 个 17.9MB(unicode-range 切片: 西里尔/希腊/越南
# 等页面根本没有的字符)。浏览器的取用规则是确定的:
#   ① src 列表按序取第一个"格式受支持"的 url(), 之后的兜底格式永不请求(该份加载失败才会往后走);
#   ② 带 unicode-range 的切片只在页面里出现落在范围内的字符时才请求。
# 据此把"证明不会被请求"的字体 URL 从下载队列剔除: 交付产物里它们保持原远程地址(部署环境在线时
# 浏览器照常可取, 离线时与评分渲染一致), 画面与浏览器实际行为逐字节相同。
# 保守边界: 首选份下载失败时兜底份照常补下; 含私用区(图标字体)的切片不按字符剔除; 活体加载过的一律保留。
_FONT_OK_FMT = ("woff2", "woff", "truetype", "opentype", "collection")
_FONT_BAD_EXT = (".eot", ".svg", ".svgz")
_FF_BLOCK_RE = re.compile(r"@font-face\s*\{([^{}]*)\}", re.I | re.S)
_FF_SRC_RE = re.compile(r"(?:^|[;{\s])src\s*:\s*((?:[^;\"'()]|\"[^\"]*\"|'[^']*'|\([^()]*\))+)", re.I)
_FF_URANGE_RE = re.compile(r"unicode-range\s*:\s*([^;}]+)", re.I)
_FF_URL_RE = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)'"\s]+))\s*\)""", re.I)
_FF_FMT_RE = re.compile(r"""format\(\s*["']?([^"')]+)""", re.I)
_CSS_ESC_RE = re.compile(r"\\([0-9a-fA-F]{1,6})")


def _split_top_commas(text: str):
    parts, depth, quote, cur = [], 0, "", []
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _font_entry_supported(entry: str, url: str) -> bool:
    m = _FF_FMT_RE.search(entry)
    if m:
        fmt = m.group(1).strip().lower()
        return any(fmt.startswith(f) for f in _FONT_OK_FMT)
    path = urlsplit(url).path.lower()
    return not path.endswith(_FONT_BAD_EXT)


def _parse_unicode_range(text: str):
    out = []
    for tok in text.split(","):
        tok = tok.strip().upper()
        if not tok.startswith("U+"):
            continue
        tok = tok[2:]
        try:
            if "?" in tok:
                lo, hi = int(tok.replace("?", "0"), 16), int(tok.replace("?", "F"), 16)
            elif "-" in tok:
                a, b = tok.split("-", 1)
                lo, hi = int(a, 16), int(b, 16)
            else:
                lo = hi = int(tok, 16)
        except ValueError:
            return None   # 解析不了: 当作全范围(保守)
        out.append((lo, hi))
    return out or None


def _is_private_use(lo: int, hi: int) -> bool:
    return (lo <= 0xF8FF and hi >= 0xE000) or hi >= 0xF0000


def page_codepoints(texts) -> list:
    """页面可能出现的字符全集(已排序码位): HTML 原文(含反转义) + CSS content 的 \\XXXX 转义。
    原文含脚本/JSON/属性值, 是渲染文字的超集 —— 只会多留切片, 不会少留。"""
    import html as _h
    cps = set()
    for t in texts:
        if not t:
            continue
        cps.update(map(ord, t))
        if "&" in t:
            cps.update(map(ord, _h.unescape(t)))
        if "\\" in t:
            for m in _CSS_ESC_RE.finditer(t):
                with suppress(ValueError):
                    cps.add(int(m.group(1), 16))
    return sorted(cps)


def _range_hits(ranges, cps) -> bool:
    import bisect
    for lo, hi in ranges:
        i = bisect.bisect_left(cps, lo)
        if i < len(cps) and cps[i] <= hi:
            return True
    return False


def fontface_plan(css_text: str, base_url: str, cps=None):
    """返回 (needed, skippable, fallback_of): 浏览器会请求的字体 URL / 证明不会请求的 URL /
    {兜底 URL: 它前面的首选 URL}(首选下载失败时据此补下兜底)。cps=None 时不按字符剔除。"""
    needed, skippable, fallback_of = set(), set(), {}
    if not css_text or "@font-face" not in css_text.lower():
        return needed, skippable, fallback_of
    for block in _FF_BLOCK_RE.finditer(css_text):
        body = block.group(1)
        srcs = list(_FF_SRC_RE.finditer(body))
        if not srcs:
            continue
        entries = []   # (abs_url, supported)
        for sm in srcs:
            for entry in _split_top_commas(sm.group(1)):
                um = _FF_URL_RE.search(entry)
                if not um:
                    continue
                raw = (um.group(1) or um.group(2) or um.group(3) or "").strip()
                if not raw or raw.startswith(("blob:", "#")):
                    continue
                if raw.startswith("data:"):
                    entries.append(("data:", _font_entry_supported(entry, ""), sm is srcs[-1]))
                    continue
                absu = _urljoin(base_url, raw)
                if not absu.startswith(("http://", "https://")):
                    continue
                entries.append((absu, _font_entry_supported(entry, absu), sm is srcs[-1]))
        if not entries:
            continue
        # 同一规则里多条 src 声明: 只有最后一条生效
        live_entries = [(u, ok) for u, ok, last in entries if last]
        dead = {u for u, ok, last in entries if not last}
        out_of_range = False
        if cps is not None:
            um = _FF_URANGE_RE.search(body)
            ranges = _parse_unicode_range(um.group(1)) if um else None
            if ranges and not any(_is_private_use(lo, hi) for lo, hi in ranges) \
                    and not _range_hits(ranges, cps):
                out_of_range = True
        primary = None
        for u, ok in live_entries:
            if primary is None and ok and not out_of_range:
                primary = u
                if u != "data:":
                    needed.add(u)
            else:
                dead.add(u)
                if primary not in (None, "data:") and ok:
                    fallback_of.setdefault(u, primary)
        dead.discard("data:")
        skippable |= dead
    skippable -= needed
    return needed, skippable, fallback_of


# ---- 窄屏/流式字号辅助(来自移动端适配线) ----
_FLUID_GEOM_RE = re.compile(r"(?:^|;)\s*(width|height|min-width|max-width|min-height|max-height|font-size|line-height|"
                            r"letter-spacing|top|left|right|bottom|margin-[a-z]+|padding-[a-z]+|gap|column-gap|row-gap)\s*:\s*"
                            r"(-?\d+(?:\.\d+)?)px\s*(!important)?(?=;|$)", re.I)


def _fluid_elements(fa: dict, fb: dict, w1: float, w2: float, tol: float = 0.015):
    """两个宽度下的 FLUID_READ_JS 快照 → 字号随宽同比的元素路径 + 内联 px 同比的属性(改 vw)。
    同比: 值比 ≈ 宽比(±tol), 且值不同(定值不算)。"""
    paths, inline = [], {}
    if not fa or not fb or not w1 or not w2 or w1 == w2:
        return {"paths": paths, "inline": inline}
    ratio = w2 / w1
    for path, va in fa.items():
        vb = fb.get(path)
        if not vb or vb[0] != va[0]:
            continue
        f1, f2 = va[1] or 0, vb[1] or 0
        if f1 >= 6 and f2 > 0 and f1 != f2 and abs((f2 / f1) / ratio - 1) <= tol:
            paths.append(path)
        if va[2] and vb[2] and va[2] != vb[2]:
            pb = {m.group(1).lower(): float(m.group(2)) for m in _FLUID_GEOM_RE.finditer(vb[2])}
            for m in _FLUID_GEOM_RE.finditer(va[2]):
                k, v1 = m.group(1).lower(), float(m.group(2))
                v2 = pb.get(k)
                if v2 is None or v1 == 0 or v1 == v2 or abs((v2 / v1) / ratio - 1) > tol:
                    continue
                inline.setdefault(path, {})[k] = f"{v1 / w1 * 100:.4f}vw"
    return {"paths": paths, "inline": inline}


_HIDE_DECL_RE = re.compile(
    r"^(?:visibility\s*:\s*hidden|opacity\s*:\s*0(?:\.0+)?|display\s*:\s*none|pointer-events\s*:\s*none"
    r"|transform\s*:[^;]*|animation(?:-[\w-]+)?\s*:[^;]*|transition(?:-[\w-]+)?\s*:[^;]*|will-change\s*:[^;]*)"
    r"\s*(?:!important)?$", re.I)


_TYPE_SEL_RE = re.compile(r"^[a-z][a-z0-9-]*(?:::?[a-z-]+(?:\([^)]*\))?)*$", re.I)


def _is_global_reset_rule(rule_text: str) -> bool:
    """窄屏页多出的"全局重置"规则: 选择器全是 * / html / body / :root / 纯标签名(含伪元素), 或 @font-face。
    媒体查询里的规则在任何视口都在 CSSOM 里, 所以"窄屏页才有"的只能是脚本注入/条件加载的整张表;
    其中的标签级重置(`*{margin:0}` `a{text-decoration:underline}` `body{font-family}` 同名 @font-face 指向
    404 字体)注入手机段会改写整页层叠(solidjs 手机端链接全下划线、字体回落), 不属于响应式差异, 跳过。"""
    m = re.match(r"^\s*([^{}]+)\{", rule_text)
    if not m:
        return False
    sel = m.group(1).strip()
    if sel.lower().startswith("@font-face"):
        return True
    if sel.startswith("@"):
        return False
    parts = [p.strip() for p in sel.split(",") if p.strip()]
    return bool(parts) and all(p in ("*", ":root") or _TYPE_SEL_RE.match(p) for p in parts)


def _is_transient_hide_rule(rule_text: str) -> bool:
    """窄屏页采到的"只把元素藏起来"的规则: 声明块全是 visibility:hidden / opacity:0 / display:none /
    transform / animation / transition。这是入场动画的初始态(wix 组件滚入视口前 `#comp-x{visibility:hidden!important}`),
    窄屏首载页截取时动画还没跑, 若当成"窄屏专属样式"注入手机段, 克隆手机端整块表单/计数永远不显形。"""
    m = re.match(r"^\s*([^{}]+)\{(.*)\}\s*$", rule_text, re.S)
    if not m or "{" in m.group(2):
        return False
    decls = [d.strip() for d in m.group(2).split(";") if d.strip()]
    return bool(decls) and all(_HIDE_DECL_RE.match(d) for d in decls)


def _regroup_css_rules(pairs) -> str:
    """[(分组前缀 'head\x00head', 规则文本)] → 按前缀重新包回 @media/@supports/@layer 块的 CSS 文本。"""
    groups = {}
    for g, t in pairs:
        groups.setdefault(g, []).append(t)
    out = []
    for g, rules in groups.items():
        heads = [h for h in g.split("\x00") if h]
        body = "\n".join(rules)
        for h in reversed(heads):
            body = h + "{\n" + body + "\n}"
        out.append(body)
    return "\n".join(out)


# ---- 有时限的 Playwright 同步调用 ----
def bounded_call(sync_obj, coro, timeout_s: float):
    """在 Playwright 同步对象的事件循环上带时限执行一个底层协程(如 cdp._impl_obj.send(...))。
    同步 API 的 CDP send / page.close 没有超时参数, 页面渲染进程卡住时会让整个任务永久挂起
    (163/wix 偶发); 超时抛 TimeoutError, 迟到的应答由连接层丢弃(回调 future 已取消)。
    取不到内部接口(Playwright 内部结构变化)时退回无时限执行, 行为同旧实现。"""
    import asyncio
    runner = getattr(sync_obj, "_sync", None)
    if runner is None:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    try:
        return runner(asyncio.wait_for(coro, timeout_s))
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"超过 {timeout_s:.0f}s 未返回") from exc


# ---- 带边界的 URL 替换(本地化落盘用) ----
# apply_replacements 是纯子串替换(与冻结基线逐值等价, 不改); 本地化时无协议键(根相对 /x/、原始相对写法)
# 也会命中更长路径的中段: 页面自身路径 "/airpods-pro/" 被当资产落地后, 视频
# "/105/media/us/airpods-pro/2025/…/large.mp4" 中段被换成 "/static/misc/x.bin", 交互版 13 个视频全 404。
# 无协议键只在左侧不是路径字符时替换(引号/括号/=/空白/逗号等之后); http(s):// 与 // 键照旧。
URL_REPL_BOUNDARY = os.environ.get("CLONER_URL_REPL_BOUNDARY", "1") != "0"
_URL_MID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.~%/-")
# 归因: CLONER_URL_REPL_TRACE=1 时记录被左边界挡下的替换(上下文 => 本应换成), 引擎每轮本地化后写进日志
URL_REPL_TRACE = os.environ.get("CLONER_URL_REPL_TRACE", "0") == "1"
URL_REPL_BLOCKED: list = []


def _rel_prefix_ok(text: str, s: int, k: str) -> bool:
    """左侧是独立的相对前缀 ./ 或 ../(前面是引号/括号等非路径字符)时照旧替换, 与旧行为一致。

    模块说明符 from"./chunk-x.js" 里根相对键 "/chunk-x.js" 紧跟在 "." 之后; 挡下它会让同一 chunk
    经说明符改写与运行时原路径镜像两条路各加载一份(Angular 整页渲染两遍, 交互轨 100→42.6)。"""
    j = s
    if not k.startswith("/"):
        if j < 1 or text[j - 1] != "/":
            return False
        j -= 1
    dots = 0
    while j > 0 and text[j - 1] == "." and dots < 3:
        j -= 1
        dots += 1
    if dots not in (1, 2):
        return False
    return j == 0 or text[j - 1] not in _URL_MID_CHARS


def apply_url_replacements(text: str, pairs) -> str:
    """apply_replacements + 无协议键的左边界约束(见上)。"""
    if not pairs or not URL_REPL_BOUNDARY:
        return apply_replacements(text, pairs)
    pattern, mapping = _replacement_matcher(pairs)
    mid = _URL_MID_CHARS

    def _sub(m):
        k = m.group(0)
        s = m.start()
        if s and text[s - 1] in mid and not k.startswith(("http:", "https:", "//")) \
                and not _rel_prefix_ok(text, s, k):
            if URL_REPL_TRACE and len(URL_REPL_BLOCKED) < 60:
                URL_REPL_BLOCKED.append(f"{text[max(0, s - 40):s]!r}+{k!r} => {mapping.get(k, k)!r}")
            return k
        return mapping.get(k, k)
    return pattern.sub(_sub, text)


# ---- 跨域接口回放垫片 ----
# Mock 只按路径在本地(沙盒/交付 Flask)回放, 但 SPA 的数据接口常在另一个主机(api.x.com, 协议相对 //api.x.com
# 在本地页里还会解析成 http:)。评分与交付里这些请求要么被外链守卫中止、要么跨域失败, 整页楼层停在骨架
# (bilibili 交互轨 38 分: 录了 81 条接口, 本地命中 0)。录到过 JSON 响应的跨域主机, 其 fetch/XHR 改发同源
# 同路径, 由本地 Mock 接住; 没录到的路径本地 404, 与外链被拦等价。
XHOST_MOCK_SHIM = os.environ.get("CLONER_XHOST_MOCK", "1") != "0"
XHOST_MOCK_TEMPLATE = r"""<script data-clone-preview="xhost-mock">(function(){try{var H=__HOSTS__;var map=function(u){try{var x=new URL(String(u),location.href);if(x.host===location.host||H.indexOf(x.host)<0)return null;var b=window.__CLONE_PREVIEW_BASE__||'/';return b+x.pathname.replace(/^\/+/,'')+x.search;}catch(e){return null;}};var of=window.fetch;if(of){window.fetch=function(i,o){try{var s=(typeof i==='string'||(typeof URL!=='undefined'&&i instanceof URL))?String(i):(i&&i.url);var m=s&&map(s);if(m){i=(typeof i==='string'||(typeof URL!=='undefined'&&i instanceof URL))?m:new Request(m,i);}}catch(e){}return of.call(this,i,o);};}var X=window.XMLHttpRequest;if(X&&X.prototype&&X.prototype.open){var oo=X.prototype.open;X.prototype.open=function(me,u){try{var m=map(u);if(m){arguments[1]=m;}}catch(e){}return oo.apply(this,arguments);};}}catch(e){}})();</script>"""


def xhost_mock_script(mock_entries, page_netloc: str) -> str:
    """录到 JSON 响应的跨域主机清单 → 回放垫片(无跨域主机时返回空串)。"""
    if not XHOST_MOCK_SHIM:
        return ""
    hosts = sorted({str(e.get("host") or "") for e in mock_entries or ()
                    if e.get("host") and e.get("host") != page_netloc})
    if not hosts:
        return ""
    return XHOST_MOCK_TEMPLATE.replace("__HOSTS__", json.dumps(hosts[:200]).replace("</", "<\\/"))


# ---------------------------------------------------------------------------
# 图片分片主机 / 变体就近解析: 运行时拼出来的图片地址(接口 JSON 里的底图 + 客户端追加的
# "@672w_378h_1c.avif" 之类变换后缀, 主机在 i0/i1/i2 分片间轮换)整串对不上替换表, 克隆页里
# 照旧外链, 离线评分被中止、离线交付破图; 而同一张底图的某个变体其实已经在资源池里。
# 垫片在 <img>/<source> 的 src/srcset 赋值与解析器插入时, 把"同注册域主机 + 同路径"(精确, 或
# 去掉 @ 后缀的底图路径)的外链指向资源池里的本地文件。跨主机命中要求文件名带内容哈希,
# 免得 a.x.com/logo.png 与 b.x.com/logo.png 这类同名不同图串号。开关 CLONER_IMG_SHARD_RESOLVE=0。
IMG_SHARD_RESOLVE = os.environ.get("CLONER_IMG_SHARD_RESOLVE", "1") != "0"
_SHARD_IMG_EXT_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|avif|svg|bmp|ico)$", re.I)
_SECOND_LEVEL = {"com", "net", "org", "gov", "edu", "co", "ac", "ne", "or"}


def host_suffix(host: str) -> str:
    """近似注册域(与垫片里的 JS 同口径): 末两段, 二级公共后缀(com.cn/co.uk 等)取末三段; IP 原样。"""
    host = (host or "").lower()
    labels = host.split(".")
    if re.fullmatch(r"[\d.]+", host) or len(labels) <= 2:
        return host
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


IMG_SHARD_TEMPLATE = r"""<script data-clone-preview="img-shard">(function(){try{var G=__MAP__;var SL=['com','net','org','gov','edu','co','ac','ne','or'];var sfx=function(h){h=String(h).toLowerCase();var l=h.split('.');if(/^[\d.]+$/.test(h)||l.length<=2)return h;if(l.length>=3&&l[l.length-1].length===2&&SL.indexOf(l[l.length-2])>=0)return l.slice(-3).join('.');return l.slice(-2).join('.');};var ok=function(eh,h,p){if(eh===h)return true;var st=p.split('/').pop().replace(/\.[a-z0-9]+$/i,'');return /[0-9a-f]{16,}/i.test(st)||st.length>=24;};var loc=function(v){return (window.__CLONE_PREVIEW_BASE__||'/')+String(v).replace(/^\/+/,'');};var res=function(u){try{if(u==null)return null;var s=String(u);if(!s||/^(data|blob|about|javascript):/i.test(s))return null;var x=new URL(s,location.href);if(x.host===location.host||(x.protocol!=='http:'&&x.protocol!=='https:'))return null;var g=G[sfx(x.hostname)];if(!g)return null;var p=x.pathname;var e=g.e[p+x.search];if(e&&ok(e[0],x.host,p))return loc(e[1]);var bs=p.split('@')[0];var v=g.b[bs];if(v&&ok(v[0],x.host,bs))return loc(v[1]);}catch(err){}return null;};var rss=function(v){try{if(v==null)return null;var ch=false;var out=String(v).split(',').map(function(c){var t=c.trim();var i=t.search(/\s/);var u=i<0?t:t.slice(0,i);var r=u&&res(u);if(r){ch=true;return r+(i<0?'':t.slice(i));}return t;});return ch?out.join(', '):null;}catch(err){return null;}};var hook=function(P,n,f){try{var d=Object.getOwnPropertyDescriptor(P,n);if(!d||!d.set)return;Object.defineProperty(P,n,{configurable:true,enumerable:d.enumerable,get:d.get,set:function(v){var r=f(v);return d.set.call(this,r||v);}});}catch(err){}};var I=window.HTMLImageElement&&HTMLImageElement.prototype,S=window.HTMLSourceElement&&HTMLSourceElement.prototype;if(I){hook(I,'src',res);hook(I,'srcset',rss);}if(S){hook(S,'src',res);hook(S,'srcset',rss);}var sa=Element.prototype.setAttribute;Element.prototype.setAttribute=function(n,v){try{var t=this.tagName;if(t==='IMG'||t==='SOURCE'){var ln=String(n).toLowerCase();var r=ln==='src'?res(v):(ln==='srcset'?rss(v):null);if(r)return sa.call(this,n,r);}}catch(err){}return sa.apply(this,arguments);};var fix=function(el){try{var t=el.tagName;if(t!=='IMG'&&t!=='SOURCE')return;var s=el.getAttribute('src');var r=s&&res(s);if(r)sa.call(el,'src',r);var ss=el.getAttribute('srcset');var rs=ss&&rss(ss);if(rs)sa.call(el,'srcset',rs);}catch(err){}};var scan=function(n){if(!n||n.nodeType!==1)return;fix(n);if(n.firstElementChild&&n.querySelectorAll){var l=n.querySelectorAll('img,source');for(var i=0;i<l.length;i++)fix(l[i]);}};new MutationObserver(function(ms){for(var i=0;i<ms.length;i++){var m=ms[i];if(m.type==='attributes')fix(m.target);else for(var j=0;j<m.addedNodes.length;j++)scan(m.addedNodes[j]);}}).observe(document.documentElement,{childList:true,subtree:true,attributes:true,attributeFilter:['src','srcset']});}catch(e){}})();</script>"""


def img_shard_map(items, page_netloc: str, limit: int = 2000) -> dict:
    """资源池 [(原地址, 本地路径, 字节数)] → {注册域: {"e": {路径+查询: [主机, 本地]}, "b": {底图路径: [主机, 本地]}}}。

    同一底图多个变体取字节数最大的(大图缩小显示失真最小)。关闭开关时返回空表。"""
    if not IMG_SHARD_RESOLVE:
        return {}
    groups: dict = {}
    n = 0
    for url, local, size in items or ():
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.netloc == page_netloc:
            continue
        if not str(local or "").startswith("/static/"):
            continue
        base = parts.path.split("@", 1)[0]
        if not _SHARD_IMG_EXT_RE.search(base):
            continue
        g = groups.setdefault(host_suffix(parts.hostname or ""), {"e": {}, "b": {}})
        key = parts.path + (("?" + parts.query) if parts.query else "")
        g["e"].setdefault(key, [parts.netloc, local])
        cur = g["b"].get(base)
        if cur is None or int(size or 0) > cur[2]:
            g["b"][base] = [parts.netloc, local, int(size or 0)]
        n += 1
        if n >= limit:
            break
    for g in groups.values():
        g["b"] = {k: v[:2] for k, v in g["b"].items()}
    return groups


_HASHY_STEM_RE = re.compile(r"[0-9a-f]{16,}", re.I)


def _shard_ok(entry_host: str, host: str, path: str) -> bool:
    if entry_host == host:
        return True
    stem = re.sub(r"\.[a-z0-9]+$", "", path.rsplit("/", 1)[-1], flags=re.I)
    return bool(_HASHY_STEM_RE.search(stem)) or len(stem) >= 24


def shard_resolve(groups: dict, url: str):
    """与垫片里 res() 同口径的 Python 版: 绝对/协议相对的外域图片地址 → 本地路径, 未命中 None。"""
    if not groups or not url:
        return None
    u = url.strip()
    if u.startswith("//"):
        u = "http:" + u
    if not u.startswith(("http://", "https://")):
        return None
    try:
        parts = urlsplit(u)
    except ValueError:
        return None
    g = groups.get(host_suffix(parts.hostname or ""))
    if not g:
        return None
    path = parts.path
    e = g["e"].get(path + (("?" + parts.query) if parts.query else ""))
    if e and _shard_ok(e[0], parts.netloc, path):
        return e[1]
    base = path.split("@", 1)[0]
    v = g["b"].get(base)
    if v and _shard_ok(v[0], parts.netloc, base):
        return v[1]
    return None


_IMG_TAG_RE = re.compile(r"<(?:img|source)\b[^>]*>", re.I)
_IMG_SRC_ATTR_RE = re.compile(r"""(\s(src|srcset)\s*=\s*)(["'])([^"'<>]*)\3""", re.I)


def rewrite_img_shards(html: str, groups: dict):
    """固化 HTML 里 <img>/<source> 的 src/srcset 外链按资源池就近改成本地(预加载扫描器在垫片之前就发请求,
    且保真轨/静态版不跑脚本)。返回 (新 HTML, 改写个数)。"""
    if not groups or not html:
        return html, 0
    count = 0

    def attr(m):
        nonlocal count
        name, val = m.group(2).lower(), m.group(4)
        if name == "src":
            r = shard_resolve(groups, val)
            if not r:
                return m.group(0)
            count += 1
            return m.group(1) + m.group(3) + r + m.group(3)
        out, changed = [], False
        for c in val.split(","):
            sp = c.strip().split(None, 1)
            r = shard_resolve(groups, sp[0]) if sp else None
            if r:
                changed = True
                out.append(r + ((" " + sp[1]) if len(sp) > 1 else ""))
            else:
                out.append(c.strip())
        if not changed:
            return m.group(0)
        count += 1
        return m.group(1) + m.group(3) + ", ".join(out) + m.group(3)

    def tag(m):
        t = m.group(0)
        if "//" not in t:
            return t
        return _IMG_SRC_ATTR_RE.sub(attr, t)

    return _IMG_TAG_RE.sub(tag, html), count


def img_shard_script(groups: dict) -> str:
    """分片/变体解析表 → 运行时垫片; 空表返回空串。"""
    if not groups:
        return ""
    return IMG_SHARD_TEMPLATE.replace(
        "__MAP__", json.dumps(groups, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
