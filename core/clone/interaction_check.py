"""克隆页「交互可用」检查: 在原站和克隆页上做同样的操作, 看克隆页有没有同样的反应。

确定性, 不调模型。只查页内交互(按钮、页签、折叠、菜单开关这类靠脚本响应的控件) —— 普通导航链接在克隆站里
本来就指向原站或代理到原站, 不在这里判。

做法:
1. 原站与克隆页各开一页(同一视口)。克隆页走本地沙盒服务器, 不放外网请求。
2. 在原站首屏两屏内收集候选控件: button / [role=button|tab|menuitem|switch] / summary / a[href^="#"] /
   a[href^="javascript:"] / [aria-expanded] / [aria-controls], 可见、有可读名字(文字或 aria-label), 去重后取前 N 个。
3. 克隆页按「同标签族 + 同名字」找对应元素; 找不到记为「克隆页缺少这个控件」。
4. 两边分别点一下(页面级导航被拦截, 记为「有反应」), 用 MutationObserver 计数 + URL hash 变化 + 视口截图像素差判断有无反应。
   计数先减去同样时长的空闲基线(轮播、计时器这类自己在动的页面不误判)。
5. 只有「原站有反应」的控件才计入: 克隆页同样有反应 → ok; 没反应 → failed。原站本来就没反应的不算。

输出 {checked, ok, failed:[{selector, text, reason}], skipped, elapsed}。
"""
from __future__ import annotations

import io
import time
from contextlib import suppress
from typing import Any

_COLLECT_JS = r"""
(limit) => {
  const sel = 'button, [role=button], [role=tab], [role=menuitem], [role=switch], summary, a[href^="#"], ' +
              'a[href^="javascript:"], [aria-expanded], [aria-controls]';
  const vh = innerHeight * 2, out = [], seen = new Set();
  const nameOf = e => (e.getAttribute('aria-label') || e.innerText || e.getAttribute('title') || e.value || '')
    .replace(/\s+/g, ' ').trim().slice(0, 40);
  const fam = e => {
    const r = (e.getAttribute('role') || '').toLowerCase();
    if (e.tagName === 'SUMMARY') return 'summary';
    if (r === 'tab') return 'tab';
    if (e.tagName === 'A') return 'a';
    return 'button';
  };
  for (const e of document.querySelectorAll(sel)) {
    if (out.length >= limit) break;
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    if (r.width < 6 || r.height < 6 || r.top > vh || r.bottom < 0 || s.visibility === 'hidden' || s.display === 'none') continue;
    if (e.disabled || e.getAttribute('aria-disabled') === 'true') continue;
    const href = e.getAttribute('href') || '';
    if (e.tagName === 'A' && (href === '#' || href === '') && !e.hasAttribute('aria-expanded') && !e.hasAttribute('role')) {}
    if (e.closest('form') && (e.type === 'submit' || (e.tagName === 'BUTTON' && !e.type))) continue;  // 提交表单会把两边都带走, 不比
    const name = nameOf(e);
    if (!name) continue;
    const key = fam(e) + '|' + name;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({fam: fam(e), name, tag: e.tagName.toLowerCase(), x: r.left + r.width / 2, y: r.top + r.height / 2});
  }
  return out;
}
"""

_FIND_JS = r"""
([fam, name, x, y]) => {
  const sel = 'button, [role=button], [role=tab], [role=menuitem], [role=switch], summary, a, [aria-expanded], ' +
              '[aria-controls], [onclick], div, span, li';
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 40);
  let best = null, bestD = 1e9;
  for (const e of document.querySelectorAll(sel)) {
    const n = norm(e.getAttribute('aria-label') || e.innerText || e.getAttribute('title') || e.value);
    if (n !== name) continue;
    // 同名的外层容器(innerText 恰好相同)取最内层: 子元素里还有同名的就跳过自己
    if ([...e.children].some(c => norm(c.innerText) === name)) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const d = Math.hypot(r.left + r.width / 2 - x, r.top + r.height / 2 - y);
    if (d < bestD) { bestD = d; best = e; }
  }
  if (!best) return null;
  document.querySelectorAll('[data-ic-target]').forEach(e => e.removeAttribute('data-ic-target'));
  best.setAttribute('data-ic-target', '1');
  const r = best.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2, dist: Math.round(bestD)};
}
"""

_OBS_JS = r"""
() => {
  if (window.__icObs) window.__icObs.disconnect();
  window.__icCount = 0;
  window.__icObs = new MutationObserver(list => {
    for (const m of list) {
      if (m.type === 'attributes' && m.target && m.target.hasAttribute && m.target.hasAttribute('data-ic-target')
          && m.attributeName === 'data-ic-target') continue;
      window.__icCount++;
    }
  });
  window.__icObs.observe(document.documentElement, {subtree: true, childList: true, characterData: true,
    attributes: true, attributeFilter: ['class', 'style', 'hidden', 'open', 'aria-expanded', 'aria-selected', 'aria-hidden']});
  return location.hash;
}
"""


_TEXT_JS = "() => { const t = (document.body && document.body.innerText) || ''; let h = 0; " \
           "for (let i = 0; i < t.length; i++) h = (h * 31 + t.charCodeAt(i)) | 0; return t.length + ':' + h; }"


def _gray(png: bytes):
    import numpy as np
    from PIL import Image
    with Image.open(io.BytesIO(png)) as im:
        return np.asarray(im.convert("L").resize((320, 200)), dtype=np.int16)


def _frac(a, b) -> float:
    return float((abs(a - b) > 24).mean())


class _Side:
    """一侧页面(原站或克隆页)的观察器。"""

    def __init__(self, page):
        self.page = page
        self.navigated = False

        def on_req(req):
            if req.is_navigation_request() and req.frame == page.main_frame and self.armed:
                self.navigated = True
        self.armed = False
        page.on("request", on_req)

    def idle_rate(self, ms: int) -> float:
        t0 = self.page.evaluate(_TEXT_JS)
        self.page.evaluate(_OBS_JS)
        self.page.wait_for_timeout(ms)
        self.text_churn = self.page.evaluate(_TEXT_JS) != t0      # 空闲时可见文字自己在变(时钟/滚动新闻), 文字信号不可信
        return float(self.page.evaluate("window.__icCount || 0"))

    def act(self, target: dict, idle: float, wait_ms: int) -> tuple:
        """点击 target(已由 _FIND_JS 标记), 返回 (有无反应, 依据)。"""
        p = self.page
        before = _gray(p.screenshot())
        t0 = p.evaluate(_TEXT_JS)
        h0 = p.evaluate(_OBS_JS)
        self.navigated, self.armed = False, True
        try:
            p.locator('[data-ic-target="1"]').first.click(timeout=2500, no_wait_after=True)
        except Exception:
            with suppress(Exception):
                p.mouse.click(target["x"], target["y"])
        p.wait_for_timeout(wait_ms)
        self.armed = False
        cnt = float(p.evaluate("window.__icCount || 0"))
        h1 = p.evaluate("location.hash")
        after = _gray(p.screenshot())
        px = _frac(before, after)
        if self.navigated:
            return True, "跳转"
        if h1 != h0:
            return True, "地址锚点变化"
        if cnt > idle * 1.5 + 3:
            return True, f"页面结构变化 {int(cnt)}"
        if not getattr(self, "text_churn", False) and cnt > idle and p.evaluate(_TEXT_JS) != t0:
            return True, "可见文字变化"
        if px > 0.004 and cnt > idle:
            return True, f"画面变化 {px:.1%}"
        return False, f"无变化(结构 {int(cnt)}/空闲 {int(idle)}, 画面 {px:.1%})"


def check(ctx_factory, original_url: str, clone_url: str, viewport: dict = None, limit: int = 12,
          budget_s: float = 90.0, log=None) -> dict:
    """ctx_factory(viewport) -> 上下文管理器, 产出 BrowserContext(浏览器池的 context())。
    clone_url 是克隆页的本地地址(沙盒服务器), 原站 original_url 需要外网。"""
    t0 = time.monotonic()
    vp = viewport or {"width": 1440, "height": 900}
    res: dict[str, Any] = {"checked": 0, "ok": 0, "failed": [], "skipped": [], "elapsed": 0.0, "version": 1}
    # 同一个浏览器上下文里开两页(原站 / 克隆页不同源, 互不影响): 只占浏览器池一个名额, 满载时不会互相等待
    with ctx_factory(vp) as ctx:
        op, cp = ctx.new_page(), ctx.new_page()
        local = clone_url.split("/", 3)[:3]
        local_origin = "/".join(local)

        def guard(route):
            # 克隆页: 只放本地沙盒的请求, 和评分口径一致(克隆站离线也该能用)
            if route.request.url.startswith(local_origin):
                route.continue_()
            else:
                route.abort()
        cp.route("**/*", guard)
        for pg, url in ((op, original_url), (cp, clone_url)):
            pg.set_default_timeout(20000)
            try:
                pg.goto(url, wait_until="load", timeout=30000)
            except Exception as exc:
                if pg is op:
                    res["skipped"].append({"text": "", "reason": f"原站打不开: {type(exc).__name__}"})
                    res["elapsed"] = round(time.monotonic() - t0, 1)
                    return res
            pg.wait_for_timeout(1500)
        cands = []
        for _ in range(2):                               # 加载后又自行跳转/重定向的站点: 等落定再收一次
            try:
                cands = op.evaluate(_COLLECT_JS, limit * 2)
                break
            except Exception:
                with suppress(Exception):
                    op.wait_for_load_state("load", timeout=15000)
                    op.wait_for_timeout(1000)
        so, sc = _Side(op), _Side(cp)
        idle_o, idle_c = so.idle_rate(500), sc.idle_rate(500)
        for c in cands:
            if res["checked"] >= limit or time.monotonic() - t0 > budget_s:
                break
            try:
                to = op.evaluate(_FIND_JS, [c["fam"], c["name"], c["x"], c["y"]])
                if not to:
                    continue
                ok_o, why_o = so.act(to, idle_o, 700)
                if not ok_o:
                    res["skipped"].append({"text": c["name"], "reason": "原站点了也没有反应"})
                    continue
                # 每个控件都从干净的页面开始: 上一个控件打开的菜单/切换的页签留着, 会让下一次点击的「反应」被误算
                with suppress(Exception):
                    op.goto(original_url, wait_until="load", timeout=30000)
                    op.wait_for_timeout(800)
                res["checked"] += 1
                tc = cp.evaluate(_FIND_JS, [c["fam"], c["name"], c["x"], c["y"]])
                if not tc:
                    res["failed"].append({"selector": c["tag"], "text": c["name"], "reason": "克隆页里找不到这个控件"})
                    continue
                ok_c, why_c = sc.act(tc, idle_c, 700)
                with suppress(Exception):
                    cp.goto(clone_url, wait_until="load", timeout=30000)
                    cp.wait_for_timeout(800)
                if ok_c:
                    res["ok"] += 1
                    res.setdefault("passed", []).append({"selector": c["tag"], "text": c["name"], "reason": why_c})
                else:
                    res["failed"].append({"selector": c["tag"], "text": c["name"],
                                          "reason": f"原站有反应({why_o}), 克隆页点了没反应"})
            except Exception as exc:          # 单个控件出错(页面中途跳转等)不影响其余
                res["skipped"].append({"text": c.get("name", ""), "reason": f"检查出错: {type(exc).__name__}"})
                with suppress(Exception):
                    op.goto(original_url, wait_until="load", timeout=30000)
                    cp.goto(clone_url, wait_until="load", timeout=30000)
        op.close(); cp.close()
    res["elapsed"] = round(time.monotonic() - t0, 1)
    if log:
        log("INTERACT", f"交互可用检查: 原站有反应的控件 {res['checked']} 个, 克隆页同样可用 {res['ok']} 个"
                        + (f", 点不动 {len(res['failed'])} 个: " + "; ".join(f["text"] for f in res["failed"][:4]) if res["failed"] else "")
                        + f" ({res['elapsed']}s)")
    return res


def penalty(res: dict, max_points: float = 8.0) -> float:
    """点不动的比例 × max_points(检查到的控件少于 3 个时不扣, 证据不足)。"""
    n = (res or {}).get("checked") or 0
    if n < 3:
        return 0.0
    return round(max_points * len((res or {}).get("failed") or []) / n, 1)
