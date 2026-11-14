"""生成产物的浏览器体检: 真实加载运行, 采集与题材无关的硬证据。

代码直写轨(core/uir/codegen.py)的验收不能只听模型的说法 —— 模型自评偏高, 且「在动 / 点了有反应 / 没报错」
这些事实浏览器可以直接量出来。本模块只做确定性测量, 不调用模型, 不识别页面内容:

  运行报错   pageerror + console.error
  外网依赖   拦截并记录所有非本地请求(产物要求离线自洽)
  空白/卡死  首帧是否几乎单色且无文字; 主线程卡死导致截图/求值超时
  动效证据   间隔连拍多帧算帧差(占比 + 位置), 外加运行中的 CSS/WAAPI 动画数与 rAF 调用数
  交互证据   填表单 → 逐个点击可交互元素 → 键盘/指针(页面注册了对应监听时), 对比操作前后的 DOM 变更与画面
  响应式     手机宽度再加载一次, 检查横向溢出

返回的 dict 同时用于: 评分的硬证据闸门、验收模型的参考事实、修复轮的问题清单。
"""
import io
import time
from contextlib import suppress
from pathlib import Path

import numpy as np
from PIL import Image

import config
from core import cloner_engine as _ce
from core.uir import settings

_INIT_JS = r"""
(() => {
  if (window.__probe) return;
  const P = window.__probe = {raf: 0, mut: 0, anim: 0, trans: 0, names: [], listeners: {}, dialogs: 0};
  const raf = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = cb => { P.raf++; return raf(cb); };
  const add = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function (type, fn, opt) {
    try {
      P.listeners[type] = (P.listeners[type] || 0) + 1;
      if (this instanceof Element) { (this.__probeEv = this.__probeEv || {})[type] = 1; }
    } catch (e) {}
    return add.call(this, type, fn, opt);
  };
  // 关键帧动画的「开始」事件: 无限循环的动画只在启动时触发一次, 所以操作之后新增的次数可归因于这次操作。
  // 一次性的短动效(入场动画、操作后的过渡)几百毫秒就结束, 连拍多半拍不到, 只能靠事件留下「发生过」的证据。
  // 过渡单独计数: 鼠标移上按钮的悬停过渡几乎每个页面都有, 不能当成「操作引出了动画」
  add.call(document, 'animationstart', e => {
    P.anim++;
    if (e.animationName && P.names.length < 8 && !P.names.includes(e.animationName)) P.names.push(e.animationName);
  }, true);
  add.call(document, 'transitionrun', () => { P.trans++; }, true);
  const watch = () => new MutationObserver(l => { P.mut += l.length; }).observe(
    document.documentElement, {subtree: true, childList: true, attributes: true, characterData: true});
  if (document.documentElement) watch(); else document.addEventListener('readystatechange', watch, {once: true});
})();
"""

_CANDIDATES_JS = r"""
(limit) => {
  const vis = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none'
      && s.pointerEvents !== 'none' && r.bottom > 0 && r.top < innerHeight * 3;
  };
  const out = [], seen = new Set();
  const push = (el, kind) => {
    if (seen.has(el) || !vis(el) || out.filter(o => o.kind === kind).length >= limit) return;
    seen.add(el);
    el.setAttribute('data-probe-id', String(out.length));
    const label = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title')
      || el.placeholder || el.id || '').trim().replace(/\s+/g, ' ').slice(0, 40);
    out.push({id: out.length, kind, tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || ''), label});
  };
  document.querySelectorAll('input:not([type=hidden]):not([type=button]):not([type=submit]):not([type=reset]),textarea,select')
    .forEach(e => push(e, 'field'));
  document.querySelectorAll('button,[role=button],input[type=button],input[type=submit],summary,[onclick],a[href^="#"],a:not([href]),label[for]')
    .forEach(e => push(e, 'click'));
  const EV = ['click', 'pointerdown', 'mousedown', 'touchstart', 'pointerup', 'mouseup'];
  document.querySelectorAll('body *').forEach(e => {
    if (e.__probeEv && EV.some(t => e.__probeEv[t])) push(e, e.tagName === 'CANVAS' ? 'canvas' : 'click');
  });
  document.querySelectorAll('canvas').forEach(e => push(e, 'canvas'));
  return out;
}
"""

_STATE_JS = r"""
() => {
  const de = document.documentElement, P = window.__probe || {};
  let anims = 0;
  try { anims = document.getAnimations().filter(a => a.playState === 'running').length; } catch (e) {}
  const d = new Date(), z = n => String(n).padStart(2, '0');
  return {
    raf: P.raf || 0, mut: P.mut || 0, anim_ev: P.anim || 0, trans_ev: P.trans || 0, anim_names: P.names || [],
    listeners: P.listeners || {},
    now: d.getFullYear() + '-' + z(d.getMonth() + 1) + '-' + z(d.getDate()) + ' ' + z(d.getHours()) + ':'
      + z(d.getMinutes()) + ':' + z(d.getSeconds()),
    scroll_y: Math.round(window.scrollY || 0), vh: innerHeight,
    anims, canvas: document.querySelectorAll('canvas').length, svg: document.querySelectorAll('svg').length,
    text: (document.body ? document.body.innerText : '').slice(0, 20000),
    nodes: document.querySelectorAll('body *').length,
    sw: de.scrollWidth, cw: de.clientWidth || innerWidth,
    sh: Math.max(de.scrollHeight, document.body ? document.body.scrollHeight : 0, innerHeight),
    title: document.title || ''
  };
}
"""

_FILL_JS = r"""
(id) => {
  const el = document.querySelector('[data-probe-id="' + id + '"]');
  if (!el) return 'gone';
  const fire = () => { for (const t of ['input', 'change']) el.dispatchEvent(new Event(t, {bubbles: true})); };
  const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || 'text').toLowerCase();
  if (tag === 'select') { if (el.options.length > 1) el.selectedIndex = 1; fire(); return 'select'; }
  if (type === 'checkbox' || type === 'radio') { el.click(); return 'toggle'; }
  if (type === 'range') {
    const lo = Number(el.min || 0), hi = Number(el.max || 100);
    el.value = String(lo + (hi - lo) * 0.7); fire(); return 'range';
  }
  const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const set = Object.getOwnPropertyDescriptor(proto, 'value').set;
  const v = {number: '12', date: '2026-01-15', time: '09:30', month: '2026-01', email: 'test@example.com',
             tel: '13800000000', url: 'https://example.com', color: '#3366cc',
             'datetime-local': '2026-01-15T09:30'}[type];
  if (type === 'file') return 'skip';
  if (v !== undefined) { set.call(el, v); fire(); return 'fill'; }
  // type=text 的输入框按它自己声明的线索推断该填什么: inputmode / pattern / 占位符的样子
  const mode = (el.getAttribute('inputmode') || '').toLowerCase(), ph = (el.placeholder || '').trim();
  const pat = el.getAttribute('pattern') || '';
  let val = '测试 Test 12';
  if (mode === 'decimal' || mode === 'numeric' || /^[\d\s.,:%+\-¥$€£]+$/.test(ph) || /^[\\d\[\]0-9.*+?{},()\-]+$/.test(pat)) val = '12';
  else if (mode === 'email' || /@/.test(ph)) val = 'test@example.com';
  else if (mode === 'tel') val = '13800000000';
  else if (mode === 'url' || /^https?:/i.test(ph)) val = 'https://example.com';
  if (el.maxLength > 0) val = val.slice(0, el.maxLength);
  set.call(el, val); fire(); return 'fill';
}
"""


def _gray(png: bytes, width: int = 360) -> np.ndarray:
    with Image.open(io.BytesIO(png)) as im:
        im = im.convert("L")
        h = max(1, round(im.size[1] * width / im.size[0]))
        return np.asarray(im.resize((width, h), Image.BILINEAR), dtype=np.int16)


def frame_diff(a: np.ndarray, b: np.ndarray, thr: int = None):
    """两帧灰度图的变化: (变化像素占比, 变化区域外接框[x0,y0,x1,y1] 归一化 或 None)。"""
    thr = settings.PROBE_DIFF_THRESHOLD if thr is None else thr
    if a.shape != b.shape:
        return 1.0, [0.0, 0.0, 1.0, 1.0]
    mask = np.abs(a - b) > thr
    frac = float(mask.mean())
    if not mask.any():
        return 0.0, None
    ys, xs = np.where(mask)
    h, w = mask.shape
    return frac, [round(float(xs.min()) / w, 3), round(float(ys.min()) / h, 3),
                  round(float(xs.max() + 1) / w, 3), round(float(ys.max() + 1) / h, 3)]


def _union(b1, b2):
    if b1 is None:
        return b2
    if b2 is None:
        return b1
    return [min(b1[0], b2[0]), min(b1[1], b2[1]), max(b1[2], b2[2]), max(b1[3], b2[3])]


def _is_blank(gray: np.ndarray, text: str) -> bool:
    """首帧几乎单色(偏离众数色的像素极少)且页面没有可见文字。"""
    if (text or "").strip():
        return False
    vals, counts = np.unique(gray, return_counts=True)
    mode = int(vals[counts.argmax()])
    return float((np.abs(gray - mode) > 6).mean()) < 0.0008


def _remote(url: str) -> bool:
    return url.split(":", 1)[0].lower() in ("http", "https", "ws", "wss", "ftp")


# 注意: 本模块所有截图都带 caret="initial"。Playwright 默认会在截图时给每个输入框临时写内联样式来隐藏光标,
# 这些写入会被页面里的变更计数当成「页面自己在改 DOM」, 带表单的页面就全成了「无法判定操作有没有反应」。


def _reveal(page, doc_h: int, vh: int):
    """逐屏滚到底再回到顶部。滚动渐显(IntersectionObserver / scroll 监听)的区块只有进过视口才显示, 整页截图
    不会触发它们 —— 不先滚一遍, 首屏以下在截图里就是一片空白, 验收看不到真实内容。"""
    if doc_h <= vh + 40:
        return
    step = max(200, int(vh * 0.8))
    n = min(settings.PROBE_REVEAL_STEPS, -(-(doc_h - vh) // step))
    jump = "y => window.scrollTo({top: y, left: 0, behavior: 'instant'})"
    for k in range(1, n + 1):
        page.evaluate(jump, min(k * step, doc_h))
        page.wait_for_timeout(130)
    page.evaluate(jump, 0)
    page.wait_for_timeout(420)


def _text_delta(before: str, after: str, limit: int = 10):
    """操作前后可见文字的差异(按空白切词, 各取前几个) → (新增, 消失)。给验收模型看「操作到底改变了什么」。"""
    a, b = (before or "").split(), (after or "").split()
    sa, sb = set(a), set(b)
    added = [w[:24] for w in dict.fromkeys(b) if w not in sa][:limit]
    gone = [w[:24] for w in dict.fromkeys(a) if w not in sb][:limit]
    return added, gone


def inspect_page(pool, entry: Path, out_dir: Path, viewport: dict, full_png: Path = None, log=None) -> dict:
    """体检入口。out_dir 落关键帧(f0/peak/after/mobile); full_png 给出时另存整页截图(任务预览图)。"""
    t0 = time.monotonic()
    out_dir.mkdir(parents=True, exist_ok=True)
    vp = {"width": int(viewport.get("width") or 1440), "height": int(viewport.get("height") or 900)}
    uri = entry.resolve().as_uri()
    rep: dict = {"errors": [], "external": [], "dialogs": 0, "frozen": False, "blank": False,
           "motion": {}, "layout": {}, "interactions": [], "mobile": {}, "shots": {}, "viewport": vp}

    def _wire(ctx, page, errors, external):
        def on_route(route):
            url = route.request.url
            if _remote(url):
                if url not in external and len(external) < 30:
                    external.append(url[:300])
                with suppress(Exception):
                    route.abort()
                return
            with suppress(Exception):
                route.continue_()
        ctx.route("**/*", on_route)
        ctx.add_init_script(_INIT_JS)
        page.on("pageerror", lambda e: errors.append(str(e)[:400]) if len(errors) < 20 else None)
        page.on("console", lambda m: errors.append(("console.error: " + m.text)[:400])
                if m.type == "error" and len(errors) < 20 and "net::ERR_" not in m.text else None)

        def on_dialog(d):
            rep["dialogs"] += 1
            with suppress(Exception):
                d.accept()
        page.on("dialog", on_dialog)
        page.set_default_timeout(8000)

    try:
        with pool.context(viewport=vp) as ctx:
            page = ctx.new_page()
            _wire(ctx, page, rep["errors"], rep["external"])
            page.goto(uri, wait_until="load", timeout=config.SHOT_TIMEOUT)
            with suppress(Exception):
                page.evaluate("() => document.fonts && document.fonts.ready")
            page.wait_for_timeout(450)
            # ---- 动效: 连拍 ----
            s0 = page.evaluate(_STATE_JS)
            t_cap = time.monotonic()
            pngs = [page.screenshot(type="png", caret="initial")]
            for gap in settings.PROBE_FRAME_GAPS_MS:
                page.wait_for_timeout(gap)
                pngs.append(page.screenshot(type="png", caret="initial"))
            cap_s = max(0.05, time.monotonic() - t_cap)
            s1 = page.evaluate(_STATE_JS)
            rep["shot_at"] = {"f0": s0.get("now"), "burst_end": s1.get("now")}
            grays = [_gray(p) for p in pngs]
            fracs, box = [], None
            for a, b in zip(grays, grays[1:]):
                f, bx = frame_diff(a, b)
                fracs.append(round(f, 5))
                box = _union(box, bx)
            vs_first = [frame_diff(grays[0], g)[0] for g in grays[1:]]
            peak = 1 + int(np.argmax(vs_first))
            (out_dir / "f0.png").write_bytes(pngs[0])
            (out_dir / "peak.png").write_bytes(pngs[peak])
            rep["shots"].update({"f0": str(out_dir / "f0.png"), "peak": str(out_dir / "peak.png")})
            base_mut = (s1["mut"] - s0["mut"]) / cap_s          # 每秒 DOM 变更基线(自发的)
            rep["motion"] = {"frame_diffs": fracs, "max_frac": max(fracs), "bbox": box,
                             "moving": max(fracs) >= settings.PROBE_MOTION_EPS,
                             "running_animations": s1["anims"], "raf_per_s": round((s1["raf"] - s0["raf"]) / cap_s, 1),
                             "dom_mutations_per_s": round(base_mut, 1), "canvas": s1["canvas"], "svg": s1["svg"]}
            rep["effects"] = {"animations": s1.get("anim_ev", 0), "transitions": s1.get("trans_ev", 0),
                              "names": s1.get("anim_names") or [], "at_load": s1.get("anim_ev", 0)}
            rep["blank"] = _is_blank(grays[0], s1["text"])
            rep["layout"] = {"overflow_x": s1["sw"] > s1["cw"] + 2, "scroll_w": s1["sw"], "client_w": s1["cw"],
                             "doc_h": s1["sh"], "nodes": s1["nodes"], "text_len": len(s1["text"]), "title": s1["title"]}
            rep["text"] = s1["text"]
            rep["listeners"] = sorted(k for k in s1["listeners"] if k in (
                "keydown", "keyup", "keypress", "mousemove", "pointermove", "click", "pointerdown", "mousedown",
                "touchstart", "wheel", "scroll", "resize"))
            with suppress(Exception):
                _reveal(page, int(s1["sh"]), int(s1.get("vh") or vp["height"]))
                rep["layout"]["revealed"] = s1["sh"] > (s1.get("vh") or vp["height"]) + 40
            if full_png is not None:
                w = max(1, s1["cw"])
                cap = min(int(_ce.SCORE_MAX_HEIGHT * w / _ce.SCORE_WIDTH), _ce.SHOT_MAX_TEXTURE)
                if s1["sh"] > cap:
                    page.screenshot(path=str(full_png), full_page=True,
                                    clip={"x": 0, "y": 0, "width": w, "height": cap}, timeout=config.SHOT_TIMEOUT, caret="initial")
                else:
                    page.screenshot(path=str(full_png), full_page=True, timeout=config.SHOT_TIMEOUT, caret="initial")
                rep["shots"]["full"] = str(full_png)
            # ---- 交互 ----
            rep["interactions"] = _interact(page, rep, base_mut, rep["motion"]["moving"])
            # ---- 操作之后: 再连拍一小段。很多动效/计时要先点「开始」才有, 只看空闲状态会把它们判成「不会动」 ----
            if any(i["kind"] != "field" for i in rep["interactions"]):
                with suppress(Exception):
                    post = [page.screenshot(type="png", caret="initial")]
                    for gap in settings.PROBE_POST_GAPS_MS:
                        page.wait_for_timeout(gap)
                        post.append(page.screenshot(type="png", caret="initial"))
                    s2 = page.evaluate(_STATE_JS)
                    pg = [_gray(x) for x in post]
                    pf = [round(frame_diff(a, b)[0], 5) for a, b in zip(pg, pg[1:])]
                    added, gone = _text_delta(s1["text"], s2["text"])
                    rep["effects"].update(animations=s2.get("anim_ev", 0), transitions=s2.get("trans_ev", 0),
                                          names=s2.get("anim_names") or [])
                    rep["post"] = {"frame_diffs": pf, "moving": max(pf) >= settings.PROBE_MOTION_EPS,
                                   "running_animations": s2["anims"], "text_added": added, "text_removed": gone}
                    rep["shot_at"]["after"] = s2.get("now")
                    (out_dir / "after.png").write_bytes(post[-1])
                    rep["shots"]["after"] = str(out_dir / "after.png")
            elif rep["interactions"]:
                with suppress(Exception):
                    (out_dir / "after.png").write_bytes(page.screenshot(type="png", caret="initial"))
                    rep["shots"]["after"] = str(out_dir / "after.png")
            page.close()
    except Exception as exc:
        # 主线程死循环 / 同步阻塞会让截图与求值超时: 记为卡死(硬伤), 不让体检异常拖垮任务
        rep["frozen"] = True
        rep["errors"].append(f"体检中断({type(exc).__name__}): {str(exc)[:200]}")
        if log:
            log("PROBE", f"体检中断: {type(exc).__name__}: {str(exc)[:160]}")

    # ---- 手机宽度 ----
    mvp = dict(settings.PROBE_MOBILE_VIEWPORT)
    merr, mext = [], []
    try:
        with pool.context(viewport=mvp) as ctx:
            page = ctx.new_page()
            _wire(ctx, page, merr, mext)
            page.goto(uri, wait_until="load", timeout=config.SHOT_TIMEOUT)
            page.wait_for_timeout(1100)          # 等入场动画走完再截: 半透明的中间帧会被当成对比度不足
            ms = page.evaluate(_STATE_JS)
            page.screenshot(path=str(out_dir / "mobile.png"), type="png", caret="initial")
            rep["shots"]["mobile"] = str(out_dir / "mobile.png")
            rep["mobile"] = {"overflow_x": ms["sw"] > ms["cw"] + 2, "scroll_w": ms["sw"], "client_w": ms["cw"],
                             "errors": [e for e in merr if e not in rep["errors"]][:5]}
            page.close()
    except Exception as exc:
        rep["mobile"] = {"overflow_x": None, "errors": [f"手机宽度加载失败({type(exc).__name__})"]}
    rep["elapsed"] = round(time.monotonic() - t0, 2)
    return rep


def _interact(page, rep: dict, base_mut: float, moving: bool) -> list:
    """填表单 → 逐个点击 → 键盘/指针。每步记录: DOM 变更增量、文字是否变化、画面是否变化、是否新增报错。

    responded: True/False/None。页面本身在自发变化(动效或定时刷新)时, 没有超出基线的变化无法判定, 记 None,
    不当作「没反应」—— 这类情况交给验收模型看操作后的截图。"""
    out = []
    try:
        cands = page.evaluate(_CANDIDATES_JS, settings.PROBE_MAX_INTERACT)
    except Exception:
        return out
    static = (not moving) and base_mut < 0.5
    win = 0.38                                   # 每次操作后的观察窗口(秒)
    base_raf = float((rep.get("motion") or {}).get("raf_per_s") or 0)

    looks = [0]                                  # 已做过几次「再看一眼」

    def observe(do, linger=False):
        e0 = len(rep["errors"])
        try:
            st0 = page.evaluate(_STATE_JS)
            shot0 = _gray(page.screenshot(type="png", caret="initial")) if static else None
            do()
            page.wait_for_timeout(int(win * 1000))
            st1 = page.evaluate(_STATE_JS)
            d_mut = st1["mut"] - st0["mut"]
            text_changed = st1["text"] != st0["text"]
            # 这次操作是否引出了动画: 新启动的关键帧动画, 或 rAF 调用明显超出空闲基线
            animated = bool(st1.get("anim_ev", 0) > st0.get("anim_ev", 0)
                            or (st1["raf"] - st0["raf"]) > base_raf * win * 1.5 + 4)
            pix = None
            if static:
                pix = frame_diff(shot0, _gray(page.screenshot(type="png", caret="initial")))[0]
                responded = bool(d_mut > 0 or text_changed or pix >= settings.PROBE_MOTION_EPS)
            else:
                if base_mut < 0.5:        # 只有画面在动、DOM 本身是静的: DOM/文字一变即可归因于这次操作
                    responded = True if (d_mut > 0 or text_changed) else None
                else:                     # DOM 也在自发变化: 明显超出基线才算
                    responded = True if d_mut > base_mut * win * 3 + 3 else None
            res = {"responded": responded, "dom_mutations": d_mut, "text_changed": text_changed, "animated": animated,
                   "pixel_change": None if pix is None else round(pix, 5)}
            # 再看一眼: 原本静止的页面, 点完之后隔 1 秒多画面还在自己变(计时开始走、游戏开始跑) → sustained。
            # 只在整串操作结束后才看就晚了 —— 后面的按钮可能已经把它暂停或重置
            if linger and static and looks[0] < settings.PROBE_LINGER_CLICKS:
                looks[0] += 1
                g1, t1 = _gray(page.screenshot(type="png", caret="initial")), st1["text"]
                page.wait_for_timeout(settings.PROBE_LINGER_MS)
                st2 = page.evaluate(_STATE_JS)
                if frame_diff(g1, _gray(page.screenshot(type="png", caret="initial")))[0] >= settings.PROBE_MOTION_EPS or st2["text"] != t1:
                    res["sustained"] = True
                    res["then_added"], res["then_removed"] = _text_delta(t1, st2["text"], 4)
            res["new_errors"] = rep["errors"][e0:][:3]
            return res
        except Exception as exc:
            return {"responded": False, "failed": f"{type(exc).__name__}: {str(exc)[:120]}",
                    "new_errors": rep["errors"][e0:][:3]}

    for c in (x for x in cands if x["kind"] == "field"):
        with suppress(Exception):
            how = page.evaluate(_FILL_JS, c["id"])
            out.append({"kind": "field", "tag": c["tag"], "type": c["type"], "label": c["label"], "action": how,
                        "responded": None})
    for c in (x for x in cands if x["kind"] in ("click", "canvas")):
        loc = page.locator(f'[data-probe-id="{c["id"]}"]').first

        def do(loc=loc, c=c):
            try:
                loc.click(timeout=1200)
            except Exception:
                loc.evaluate("el => el.click()", timeout=1200)
        res = observe(do, linger=True)
        out.append(dict({"kind": c["kind"], "tag": c["tag"], "label": c["label"], "action": "click"}, **res))
    lst = set(rep.get("listeners") or ())
    if lst & {"keydown", "keyup", "keypress"}:
        def keys():
            for k in ("Space", "Enter", "ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"):
                page.keyboard.press(k)
                page.wait_for_timeout(60)
        out.append(dict({"kind": "keyboard", "tag": "", "label": "空格 / 回车 / 方向键", "action": "press"}, **observe(keys)))
    if lst & {"mousemove", "pointermove"}:
        vp = page.viewport_size or {"width": 1280, "height": 800}

        def move():
            for i in range(1, 9):
                page.mouse.move(vp["width"] * i / 9, vp["height"] * (0.3 + 0.05 * i), steps=3)
        out.append(dict({"kind": "pointer", "tag": "", "label": "鼠标划过页面", "action": "move"}, **observe(move)))
    return out


def facts(rep: dict) -> dict:
    """体检结论的硬事实摘要(评分闸门与提示词共用)。"""
    inter = [i for i in rep.get("interactions", []) if i["kind"] != "field"]
    mot = rep.get("motion") or {}
    idle_none = (bool(mot) and not mot.get("moving") and not mot.get("running_animations")
                 and (mot.get("raf_per_s") or 0) < 2)
    # 操作之后出现的动效: 操作后的连拍在动, 或某次操作引出了动画 / 让画面持续变化
    after = bool((rep.get("post") or {}).get("moving")) or any(i.get("animated") or i.get("sustained") for i in inter)
    eff = rep.get("effects") or {}
    return {
        "errors": bool(rep.get("errors")),
        "blank": bool(rep.get("blank")),
        "frozen": bool(rep.get("frozen")),
        "external": bool(rep.get("external")),
        "overflow_desktop": bool((rep.get("layout") or {}).get("overflow_x")),
        "overflow_mobile": bool((rep.get("mobile") or {}).get("overflow_x")),
        # 「确定没在动」: 空闲时帧差为零、没有运行中的动画、没有持续的 rAF 循环, 并且把能操作的都操作一遍之后也没有动起来
        "motion_none": idle_none and not after,
        "motion_seen": bool(mot.get("moving")),
        "motion_after": after,
        # 「从头到尾没有任何动效」: 没启动过关键帧动画, 也没发生过过渡, 画面空闲和操作后都不动
        "effects_none": bool(mot) and idle_none and not after and not eff.get("animations") and not eff.get("transitions"),
        "interactive_n": len(inter),
        "responded_n": sum(1 for i in inter if i.get("responded") is True),
        "dead_n": sum(1 for i in inter if i.get("responded") is False),
        "unknown_n": sum(1 for i in inter if i.get("responded") is None),
    }


def dead_controls(rep: dict) -> list:
    out = []
    for i in rep.get("interactions", []):
        if i.get("responded") is False and i["kind"] != "field":
            out.append((f"{i['tag']}「{i['label']}」" if i.get("label") else (i["tag"] or i["kind"]))
                       + (f"({i['failed']})" if i.get("failed") else ""))
    return out


def hard_defects(rep: dict) -> list:
    """必须先修的硬伤(文本, 直接喂给修复轮)。"""
    f, out = facts(rep), []
    if f["frozen"]:
        out.append("页面加载后主线程卡死或体检中断(可能有死循环 / 同步阻塞): " + "; ".join(rep["errors"][-1:]))
    for e in (rep.get("errors") or [])[:6]:
        if not e.startswith("体检中断"):
            out.append(f"运行时报错: {e}")
    if f["blank"]:
        out.append("页面渲染结果是空白(单色背景且没有任何可见内容)")
    for u in (rep.get("external") or [])[:5]:
        out.append(f"引用了外网资源(产物必须离线可用, 需改为内联实现): {u}")
    if f["overflow_desktop"]:
        lay = rep["layout"]
        out.append(f"桌面宽度下出现横向滚动条(内容宽 {lay.get('scroll_w')}px > 视口 {lay.get('client_w')}px)")
    if f["overflow_mobile"]:
        m = rep["mobile"]
        out.append(f"手机宽度(390px)下出现横向滚动条(内容宽 {m.get('scroll_w')}px)")
    for e in ((rep.get("mobile") or {}).get("errors") or [])[:2]:
        out.append(f"手机宽度下报错: {e}")
    # 个别按钮点了没变化未必是缺陷(初始状态下的「重置」「暂停」本就无事可做), 只有全部可交互元素都没反应才算硬伤;
    # 其余情况作为事实交给验收模型判断(见 dead_controls)
    if f["dead_n"] and not f["responded_n"] and not f["unknown_n"]:
        out.append("页面上所有可交互元素操作后都没有任何反应: " + "、".join(dead_controls(rep)))
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[:14]
