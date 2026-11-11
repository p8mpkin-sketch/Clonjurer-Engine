"""手机端适配分类测试集的合成站点: 每个用例一页, 只含一种"随手机端变化"的机制。

真实站点语料测的是"这 20 个站的平均效果", 分不清是哪类机制没处理好; 活体站点还带噪声(广告/轮播相位/AB 实验)。
这里每类机制各做几个确定性的本地页, 克隆后在各移动视口与原页逐屏对比, 应当接近 100 —— 低于门槛就是
引擎对这一类机制的处理有缺口, 而且可以本地几十秒复现。

用例按机制分类(CASES 的 category):
  responsive  纯媒体查询的响应式(对照组)
  ua          按 UA 分发: 服务端同址换模板 / 跳手机子路径 / 脚本读 UA 换结构
  structure   手机与桌面组件不一一对应(脚本按屏宽插入/替换带 id 的组件)
  rerun       站点脚本在克隆里重跑: 数据接口失效后删内容 / 重复追加 / 改状态
  lazy        懒渲染: 滚动进场(可回退)、IO 懒加载图、滚到才渲染的区块
  timed       定时状态: 自动轮播、自动展开的手风琴
  overlay     弹层与滚动锁
  pinned      吸顶/固定底栏/100vh
  measured    脚本量宽写内联尺寸、rem 基准随屏宽、canvas
  legacy      无 viewport meta 的定宽老页
  media       picture/srcset 按宽换图
  state       采集期被脚本改过的状态冻进了 DOM(滚动监听移动的当前项、已转过去的轮播帧), 新开的原页是初始态
  api         接口回放: 同一接口按请求体里的键 / 查询参数返回不同内容(带每次变化的请求号), 回放要挑对那一份
  drift       交互漂移: 脚本在克隆里重跑时按 Math.random / 时钟 / 轮换相位选了另一版内容(门槛看引擎交互轨分)
  scrollfx    滚动驱动动画: 钉住舞台按滚动进度变形、视差、CSS animation-timeline

接口 /api/items 带时效签名(页面内嵌的时间戳 12s 后失效 → 403): 模拟真实站点"克隆里数据接口拿不到"。
"""
import io
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PHONE_UA_RE = re.compile(r"iPhone|iPod|Android.+Mobile|Windows Phone|HarmonyOS.+Mobile", re.I)
API_TTL_S = 12

BASE_CSS = """
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;font-family:Arial,Helvetica,sans-serif;color:#1c2330;background:#f6f7f9;line-height:1.5}
h1{font-size:34px;margin:0 0 12px}h2{font-size:24px;margin:0 0 10px}p{margin:0 0 12px}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
.bar{background:#14324d;color:#fff;padding:14px 24px;display:flex;justify-content:space-between;align-items:center}
.bar a{color:#cfe3f5;margin-left:18px;text-decoration:none}
.hero{background:#1f5f8b;color:#fff;padding:56px 24px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.card{background:#fff;border:1px solid #d9dee5;border-radius:10px;padding:16px}
.card img{display:block;width:100%;height:auto;border-radius:6px;margin-bottom:10px}
.band{padding:40px 24px}.band.alt{background:#e9eef4}
.foot{background:#10202f;color:#b8c4d0;padding:32px 24px}
.btn{display:inline-block;background:#f0a500;color:#1c2330;padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:bold}
@media (max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}h1{font-size:28px}}
@media (max-width:600px){.grid{grid-template-columns:1fr}.bar a{display:none}.hero{padding:32px 16px}.wrap{padding:16px}h1{font-size:24px}}
"""

LOREM = ("Portable layouts adapt to the width of the screen and keep the reading order intact. "
         "Each block below carries enough text to wrap differently at every width so that a wrong "
         "container size shows up as a shift further down the page.")


def _cards(n, start=0, img=True):
    out = []
    for i in range(start, start + n):
        pic = f'<img src="/img/400x240/{(i * 37 + 40) % 256:02x}{(i * 71 + 90) % 256:02x}{(i * 13 + 150) % 256:02x}.png" width="400" height="240" alt="pic {i}">' if img else ""
        out.append(f'<div class="card">{pic}<h2>Item {i + 1}</h2><p>{LOREM}</p></div>')
    return "".join(out)


def _page(title, body, head="", viewport=True, css=BASE_CSS):
    vp = '<meta name="viewport" content="width=device-width,initial-scale=1">' if viewport else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">{vp}<title>{title}</title>'
            f'<style>{css}</style>{head}</head><body>{body}</body></html>')


def _shell(title, middle, head="", **kw):
    body = (f'<div class="bar"><strong>{title}</strong><nav><a href="#a">Docs</a><a href="#b">Pricing</a><a href="#c">Blog</a></nav></div>'
            f'<div class="hero"><h1>{title}</h1><p>{LOREM}</p><a class="btn" href="#go">Get started</a></div>'
            f'{middle}'
            f'<div class="band alt"><div class="wrap"><h2>More to read</h2><div class="grid">{_cards(3, 20, img=False)}</div></div></div>'
            f'<div class="foot"><p>Footer line one for {title}</p><p>Contact · Terms · Privacy</p></div>')
    return _page(title, body, head, **kw)


# --------------------------------------------------------------------------- 用例
def case_responsive_grid(ctx):
    return _shell("Responsive grid", f'<div class="band"><div class="wrap"><h2>Catalog</h2><div class="grid">{_cards(6)}</div></div></div>')


def case_responsive_nav(ctx):
    css = """.menu{display:flex;gap:20px;padding:12px 24px;background:#fff;border-bottom:1px solid #d9dee5}
.burger{display:none;padding:12px 24px;background:#fff;border-bottom:1px solid #d9dee5;font-weight:bold}
.side{float:right;width:300px;margin:0 0 16px 24px;background:#fff;border:1px solid #d9dee5;padding:16px;border-radius:10px}
@media (max-width:800px){.menu{display:none}.burger{display:block}.side{float:none;width:auto;margin:0 0 16px}}"""
    mid = (f'<div class="menu"><span>Overview</span><span>Features</span><span>Customers</span><span>Company</span></div>'
           f'<div class="burger">☰ Menu</div>'
           f'<div class="band"><div class="wrap"><div class="side"><h2>Sidebar</h2><p>{LOREM}</p></div>'
           f'<h2>Article</h2><p>{LOREM}</p><p>{LOREM}</p><p>{LOREM}</p><p>{LOREM}</p><p>{LOREM}</p></div></div>')
    return _shell("Responsive nav", mid, head=f"<style>{css}</style>")


def case_ua_server_template(ctx):
    """同一地址, 服务端按 UA 下发两套模板(骨架完全不同)。"""
    if ctx["phone"]:
        css = "body{margin:0;font-family:Arial;background:#fff}.mtop{background:#b3261e;color:#fff;padding:12px 14px;font-size:18px}" \
              ".mrow{display:flex;gap:10px;padding:12px 14px;border-bottom:1px solid #eee}.mrow img{width:96px;height:72px;border-radius:4px}" \
              ".mtab{display:flex;justify-content:space-around;padding:10px;background:#fafafa;border-bottom:1px solid #ddd}.mfoot{padding:20px 14px;color:#888}"
        rows = "".join(f'<div class="mrow"><img src="/img/192x144/{(i * 53 + 30) % 256:02x}6a{(i * 29 + 80) % 256:02x}.png" width="96" height="72" alt="">'
                       f'<div><b>Mobile story {i + 1}</b><br><span>Short summary of mobile story number {i + 1} for phones.</span></div></div>' for i in range(14))
        body = f'<div class="mtop">m.Portal</div><div class="mtab"><span>Top</span><span>Local</span><span>Video</span><span>Me</span></div>{rows}<div class="mfoot">Mobile edition footer</div>'
        return _page("Portal (mobile edition)", body, css=css)
    return _shell("Portal desktop edition", f'<div class="band"><div class="wrap"><h2>Desktop stories</h2><div class="grid">{_cards(9)}</div></div></div>')


def case_ua_redirect(ctx):
    """手机 UA 302 跳到 /m/ 子路径(另一套页面)。"""
    if ctx["phone"] and not ctx["path"].startswith("/m/"):
        return ("redirect", "/m/ua_redirect")
    if ctx["path"].startswith("/m/"):
        css = "body{margin:0;font-family:Arial}.h{background:#00695c;color:#fff;padding:14px}.it{padding:14px;border-bottom:1px solid #e0e0e0}.f{padding:24px 14px;color:#777}"
        items = "".join(f'<div class="it"><b>Touch item {i + 1}</b><p>Compact description for touch item {i + 1}.</p></div>' for i in range(16))
        return _page("Shop (m)", f'<div class="h">Shop · mobile</div>{items}<div class="f">m-site footer</div>', css=css)
    return _shell("Shop desktop", f'<div class="band"><div class="wrap"><h2>Departments</h2><div class="grid">{_cards(6)}</div></div></div>')


def case_ua_client_branch(ctx):
    """同一份 HTML, 脚本读 navigator.userAgent: 手机 UA 下把导航换成底部标签栏、把三列卡片换成紧凑列表。"""
    css = """.tabbar{display:flex;justify-content:space-around;background:#fff;border-top:1px solid #d0d5dc;padding:10px 0;margin-top:16px}
.compact .row{display:flex;gap:12px;padding:12px 0;border-bottom:1px solid #d9dee5}.compact .row i{flex:none;width:64px;height:64px;background:#7aa5c9;border-radius:8px}"""
    js = """<script>(function(){var phone=/iPhone|Android.+Mobile/i.test(navigator.userAgent);
var host=document.getElementById('list');
if(phone){document.documentElement.className+=' is-phone';var h='';for(var i=0;i<8;i++)h+='<div class="row"><i></i><div><b>Phone row '+(i+1)+'</b><p>Compact phone layout row number '+(i+1)+' rendered by script.</p></div></div>';
host.className='compact';host.innerHTML=h;var t=document.createElement('div');t.className='tabbar';t.id='tabbar';t.innerHTML='<span>Home</span><span>Search</span><span>Cart</span><span>Me</span>';host.parentNode.appendChild(t);}
})();</script>"""
    mid = f'<div class="band"><div class="wrap"><h2>Products</h2><div id="list" class="grid">{_cards(6, img=False)}</div></div></div>{js}'
    return _shell("Client UA branch", mid, head=f"<style>{css}</style>")


def case_structure_swap(ctx):
    """脚本按屏宽决定容器里放哪些组件(带 id), 手机多一个 0 高占位兄弟并占一条行距; 之后不随 resize 变。"""
    css = """.form{display:grid;row-gap:16px;background:#fff;border:1px solid #d9dee5;border-radius:10px;padding:20px;max-width:520px}
.form input{padding:10px;border:1px solid #b9c2cc;border-radius:6px;font-size:16px;width:100%}.ghost{height:0;visibility:hidden}
.promo{background:#fff3cd;border:1px solid #f0d58a;padding:14px;border-radius:8px}"""
    js = """<script>(function(){var f=document.getElementById('signup'),narrow=innerWidth<700;
function el(id,html,cls){var d=document.createElement('div');d.id=id;if(cls)d.className=cls;d.innerHTML=html;return d;}
if(narrow){f.appendChild(el('f-ghost','','ghost'));f.appendChild(el('f-mail','<input placeholder="Email address">'));f.appendChild(el('f-go','<a class="btn" href="#">Start on mobile</a>'));}
else{f.appendChild(el('f-name','<input placeholder="Full name">'));f.appendChild(el('f-mail','<input placeholder="Work email">'));f.appendChild(el('f-go','<a class="btn" href="#">Start free trial</a>'));}
var s=document.getElementById('slots');
if(narrow){s.appendChild(el('s-promo','<b>Mobile only promo</b><p>Install the app for a faster checkout.</p>','promo'));s.appendChild(el('s-b','<h2>Section B</h2><p>Body of section B.</p>'));}
else{s.appendChild(el('s-a','<h2>Section A</h2><p>Body of section A for wide screens only.</p>'));s.appendChild(el('s-b','<h2>Section B</h2><p>Body of section B.</p>'));}
})();</script>"""
    mid = (f'<div class="band"><div class="wrap"><h2>Sign up</h2><div class="form" id="signup"></div></div></div>'
           f'<div class="band alt"><div class="wrap" id="slots"></div></div>{js}'
           f'<div class="band"><div class="wrap"><div class="grid">{_cards(3)}</div></div></div>')
    return _shell("Structure swap", mid, head=f"<style>{css}</style>")


def _api_boot(ctx):
    return f"<script>window.__API_T={ctx['now']};</script>"


def case_rerun_list_trim(ctx):
    """横向商品列表服务端已渲染 10 张; 脚本拿数据"水合": 接口失效时按 1 条兜底数据对齐, 删掉多余节点。"""
    css = """.track{display:flex;gap:12px;overflow:hidden}.track .card{flex:none;width:240px}"""
    js = """<script>(function(){var t=document.getElementById('track');
function sync(items){var kids=[].slice.call(t.children);for(var i=kids.length-1;i>=items.length;i--)t.removeChild(kids[i]);
for(var j=0;j<items.length&&j<kids.length;j++){kids[j].querySelector('h2').textContent=items[j];}}
fetch('/api/items?n=10&t='+window.__API_T).then(function(r){if(!r.ok)throw 0;return r.json();}).then(function(d){sync(d.items);}).catch(function(){sync(['Item 1']);});
})();</script>"""
    mid = (f'{_api_boot(ctx)}<div class="band"><div class="wrap"><h2>Best sellers</h2><div class="track" id="track">{_cards(10)}</div></div></div>'
           f'<div class="band alt"><div class="wrap"><div class="grid">{_cards(3, 30)}</div></div></div>{js}')
    return _shell("Rerun list trim", mid, head=f"<style>{css}</style>")


def case_rerun_block_drop(ctx):
    """纵向整块: 接口失效时脚本把已渲染的推荐区整块移除并把页脚换成精简版。"""
    js = """<script>(function(){fetch('/api/items?n=3&t='+window.__API_T).then(function(r){if(!r.ok)throw 0;return r.json();}).then(function(){}).catch(function(){
var r=document.getElementById('reco');if(r)r.parentNode.removeChild(r);});})();</script>"""
    mid = (f'{_api_boot(ctx)}<div class="band"><div class="wrap"><h2>Intro</h2><p>{LOREM}</p></div></div>'
           f'<div class="band alt" id="reco"><div class="wrap"><h2>Recommended</h2><div class="grid">{_cards(6)}</div></div></div>{js}')
    return _shell("Rerun block drop", mid)


def case_rerun_append(ctx):
    """非幂等初始化: 脚本每次运行都往容器里追加一批节点、在 body 前插公告条(克隆里重跑会翻倍)。"""
    js = """<script>(function(){var f=document.getElementById('feed');for(var i=0;i<5;i++){var d=document.createElement('div');d.className='card';d.style.marginBottom='12px';d.innerHTML='<h2>Feed entry '+(i+1)+'</h2><p>Generated on the client at load time, entry '+(i+1)+'.</p>';f.appendChild(d);}
var n=document.createElement('div');n.style.cssText='background:#b3261e;color:#fff;padding:10px 24px';n.textContent='Notice: maintenance window on Sunday';document.body.insertBefore(n,document.body.firstChild);
var c=document.getElementById('count');c.textContent=String(parseInt(c.textContent,10)+5);})();</script>"""
    mid = f'<div class="band"><div class="wrap"><h2>Feed (<span id="count">0</span> entries)</h2><div id="feed"></div></div></div>{js}'
    return _shell("Rerun append", mid)


def case_rerun_state(ctx):
    """脚本重跑改状态: 加载时读 localStorage 计数决定显示哪个标签页并自增(克隆里再跑一次就换了一页)。"""
    css = ".tabs span{display:inline-block;padding:8px 14px;border:1px solid #b9c2cc;margin-right:6px;border-radius:6px}.tabs span.on{background:#14324d;color:#fff}.pane{display:none}.pane.on{display:block}"
    js = """<script>(function(){var k=parseInt(localStorage.getItem('visits')||'0',10);localStorage.setItem('visits',String(k+1));
var i=k%3;document.querySelectorAll('.tabs span')[i].className='on';document.querySelectorAll('.pane')[i].className='pane on';})();</script>"""
    panes = "".join(f'<div class="pane"><h2>Pane {c}</h2>' + f"<p>{LOREM}</p>" * (i + 1) + '</div>' for i, c in enumerate("ABC"))
    mid = f'<div class="band"><div class="wrap"><div class="tabs"><span>Tab A</span><span>Tab B</span><span>Tab C</span></div>{panes}</div></div>{js}'
    return _shell("Rerun state", mid, head=f"<style>{css}</style>")


def case_lazy_reveal(ctx):
    """滚动进场且可回退: 进入视口加 .in(透明度/位移过渡), 离开视口去掉。回顶后下半页是"未显示"状态。"""
    css = ".rv{opacity:0;transform:translateY(40px);transition:opacity .5s,transform .5s}.rv.in{opacity:1;transform:none}"
    js = """<script>(function(){var io=new IntersectionObserver(function(es){es.forEach(function(e){e.target.classList.toggle('in',e.isIntersecting);});},{threshold:0.05});
document.querySelectorAll('.rv').forEach(function(e){io.observe(e);});})();</script>"""
    secs = "".join(f'<div class="band{" alt" if i % 2 else ""} rv"><div class="wrap"><h2>Reveal section {i + 1}</h2><div class="grid">{_cards(3, i * 3)}</div></div></div>' for i in range(4))
    return _shell("Lazy reveal", secs + js, head=f"<style>{css}</style>")


def case_lazy_images(ctx):
    """IO 懒加载图(data-src → src), 占位高度由宽高属性给出。"""
    js = """<script>(function(){var io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){e.target.src=e.target.getAttribute('data-src');io.unobserve(e.target);}});},{rootMargin:'100px'});
document.querySelectorAll('img[data-src]').forEach(function(e){io.observe(e);});})();</script>"""
    cards = "".join(f'<div class="card"><img data-src="/img/400x240/{(i * 41 + 60) % 256:02x}{(i * 17 + 120) % 256:02x}88.png" width="400" height="240" alt="lazy {i}"><h2>Lazy {i + 1}</h2><p>{LOREM}</p></div>' for i in range(9))
    return _shell("Lazy images", f'<div class="band"><div class="wrap"><div class="grid">{cards}</div></div></div>{js}')


def case_lazy_sections(ctx):
    """滚到才渲染: 占位哨兵进入视口时才把区块内容插进来(之后保留)。"""
    js = """<script>(function(){var io=new IntersectionObserver(function(es){es.forEach(function(e){if(!e.isIntersecting)return;io.unobserve(e.target);var n=e.target.getAttribute('data-n');
var h='<div class="wrap"><h2>Deferred section '+n+'</h2><div class="grid">';for(var i=0;i<3;i++)h+='<div class="card"><h2>Deferred '+n+'.'+(i+1)+'</h2><p>Rendered when scrolled into view. Rendered when scrolled into view.</p></div>';
e.target.innerHTML=h+'</div></div>';e.target.style.minHeight='';});},{rootMargin:'50px'});
document.querySelectorAll('.defer').forEach(function(e){io.observe(e);});})();</script>"""
    secs = "".join(f'<div class="band{" alt" if i % 2 else ""} defer" data-n="{i + 1}" style="min-height:120px"></div>' for i in range(4))
    return _shell("Lazy sections", f'<div class="band"><div class="wrap"><h2>Above the fold</h2><p>{LOREM}</p></div></div>' + secs + js)


def case_timed_carousel(ctx):
    """定时轮播(每 2.5s 换一张, 高度不同) + 指示点。"""
    css = ".slide{display:none;padding:28px;border-radius:10px;color:#fff}.slide.on{display:block}.dots b{display:inline-block;width:10px;height:10px;border-radius:50%;background:#b9c2cc;margin:10px 4px}.dots b.on{background:#14324d}"
    js = """<script>(function(){var s=document.querySelectorAll('.slide'),d=document.querySelectorAll('.dots b'),k=0;
setInterval(function(){s[k].classList.remove('on');d[k].classList.remove('on');k=(k+1)%s.length;s[k].classList.add('on');d[k].classList.add('on');},2500);})();</script>"""
    slides = "".join(f'<div class="slide{" on" if i == 0 else ""}" style="background:#{c}"><h2>Slide {i + 1}</h2>' + f"<p>{LOREM}</p>" * (i + 1) + '</div>'
                     for i, c in enumerate(("1f5f8b", "7b3f8c", "2e7d32")))
    dots = '<div class="dots"><b class="on"></b><b></b><b></b></div>'
    mid = f'<div class="band"><div class="wrap">{slides}{dots}</div></div><div class="band alt"><div class="wrap"><div class="grid">{_cards(3)}</div></div></div>{js}'
    return _shell("Timed carousel", mid, head=f"<style>{css}</style>")


def case_timed_accordion(ctx):
    """源码里全收起; 滚到视口后脚本每 2s 轮流展开一张(只在宽屏自动轮播, 触屏不自动)。"""
    css = ".acc{border:1px solid #d9dee5;border-radius:8px;margin-bottom:10px;background:#fff}.acc h2{padding:14px;margin:0;font-size:18px}.acc .body{display:none;padding:0 14px 14px}.acc.is-open .body{display:block}"
    js = """<script>(function(){var a=document.querySelectorAll('.acc'),k=-1,tm=0;if(matchMedia('(pointer: coarse)').matches)return;
new IntersectionObserver(function(es){if(es[0].isIntersecting&&!tm){tm=setInterval(function(){if(k>=0)a[k].classList.remove('is-open');k=(k+1)%a.length;a[k].classList.add('is-open');},2000);}}).observe(document.getElementById('accs'));})();</script>"""
    accs = "".join(f'<div class="acc" id="acc-{i}"><h2>Question {i + 1}</h2><div class="body"><p>{LOREM}</p><p>{LOREM}</p></div></div>' for i in range(4))
    mid = f'<div class="band"><div class="wrap"><h2>FAQ</h2><div id="accs">{accs}</div></div></div>{js}'
    return _shell("Timed accordion", mid, head=f"<style>{css}</style>")


def case_overlay_modal(ctx):
    """加载 0.8s 后弹订阅弹窗: 全屏遮罩 + body 滚动锁(overflow:hidden; position:fixed)。"""
    css = ".mask{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:1000;display:flex;align-items:center;justify-content:center}.dlg{background:#fff;padding:28px;border-radius:12px;max-width:420px;width:86%}"
    js = """<script>setTimeout(function(){var m=document.createElement('div');m.className='mask';m.id='newsletter-modal';m.setAttribute('role','dialog');m.setAttribute('aria-modal','true');
m.innerHTML='<div class="dlg"><h2>Join the newsletter</h2><p>Get 10% off your first order.</p><input placeholder="Email" style="width:100%;padding:10px;margin-bottom:12px"><a class="btn" href="#">Subscribe</a> <button id="mclose">Close</button></div>';
document.body.appendChild(m);document.documentElement.style.overflow='hidden';document.body.style.overflow='hidden';document.body.style.position='fixed';document.body.style.width='100%';
document.getElementById('mclose').onclick=function(){m.remove();document.documentElement.style.overflow='';document.body.style.overflow='';document.body.style.position='';};},800);</script>"""
    mid = f'<div class="band"><div class="wrap"><div class="grid">{_cards(6)}</div></div></div>{js}'
    return _shell("Overlay modal", mid, head=f"<style>{css}</style>")


def case_pinned_layout(ctx):
    """吸顶导航 + 固定底栏(只手机宽显示) + 100vh 首屏。"""
    css = """.top{position:sticky;top:0;z-index:10;background:#fff;border-bottom:1px solid #d9dee5;padding:12px 24px;font-weight:bold}
.full{min-height:100vh;display:flex;flex-direction:column;justify-content:center;background:#263b50;color:#fff;padding:24px}
.dock{display:none;position:fixed;left:0;right:0;bottom:0;background:#14324d;color:#fff;padding:12px;text-align:center;z-index:20}
@media (max-width:700px){.dock{display:block}body{padding-bottom:48px}}"""
    mid = (f'<div class="top">Sticky section nav · Overview · Specs · Reviews</div>'
           f'<div class="full"><h1>Full viewport panel</h1><p>{LOREM}</p></div>'
           f'<div class="band"><div class="wrap"><div class="grid">{_cards(6)}</div></div></div><div class="dock">Fixed buy bar · Add to cart</div>')
    return _shell("Pinned layout", mid, head=f"<style>{css}</style>")


def case_measured_inline(ctx):
    """脚本量屏宽写内联 px(加载时一次, 不监听 resize): 轨道宽、等高卡片、按列数算的卡片宽。"""
    css = ".mrow{display:flex;flex-wrap:wrap;gap:12px}.mrow .card{flex:none}"
    js = """<script>(function(){var w=document.getElementById('mrow'),cw=w.clientWidth,cols=cw>=900?4:cw>=560?2:1,gap=12,each=Math.floor((cw-gap*(cols-1))/cols);
var kids=w.children,mx=0;for(var i=0;i<kids.length;i++){kids[i].style.width=each+'px';}
for(var j=0;j<kids.length;j++){mx=Math.max(mx,kids[j].offsetHeight);}for(var k=0;k<kids.length;k++){kids[k].style.height=mx+'px';}
var b=document.getElementById('banner');b.style.height=Math.round(b.clientWidth*0.4)+'px';})();</script>"""
    mid = (f'<div class="band"><div class="wrap"><div id="banner" style="background:#7b3f8c;border-radius:10px;margin-bottom:16px"></div>'
           f'<div class="mrow" id="mrow">{_cards(8, img=False)}</div></div></div>{js}')
    return _shell("Measured inline", mid, head=f"<style>{css}</style>")


def case_measured_rem(ctx):
    """rem 基准随屏宽(html font-size = clientWidth/7.5, 上限 100px), 全页尺寸用 rem —— 国内移动站常见写法。"""
    css = """body{margin:0;font-family:Arial;font-size:.28rem;color:#222;background:#f4f4f4}.rh{background:#c62828;color:#fff;padding:.3rem;font-size:.36rem}
.rgrid{display:flex;flex-wrap:wrap;padding:.15rem}.rc{width:3.45rem;margin:.15rem;background:#fff;border-radius:.12rem;padding:.2rem}.rc i{display:block;height:2rem;background:#90a4ae;border-radius:.08rem;margin-bottom:.15rem}
.rf{padding:.4rem .3rem;color:#777}"""
    js = "<script>(function(){function s(){var w=Math.min(document.documentElement.clientWidth,750);document.documentElement.style.fontSize=(w/7.5)+'px';}s();addEventListener('resize',s);})();</script>"
    cells = "".join(f'<div class="rc"><i></i><b>Rem card {i + 1}</b><p>Sized entirely in rem units against the root font size.</p></div>' for i in range(10))
    return _page("Measured rem", f'<div class="rh">Rem based mobile page</div><div class="rgrid">{cells}</div><div class="rf">rem footer</div>', head=js, css=css)


def case_measured_canvas(ctx):
    """canvas 图表: 位图尺寸按容器宽 × DPR, 只在变宽时重设。"""
    js = """<script>(function(){var c=document.getElementById('chart'),last=0;function draw(){var w=c.parentNode.clientWidth;if(w<=last)return;last=w;var d=window.devicePixelRatio||1,h=Math.round(w*0.45);
c.width=w*d;c.height=h*d;c.style.width=w+'px';c.style.height=h+'px';var g=c.getContext('2d');g.scale(d,d);g.fillStyle='#fff';g.fillRect(0,0,w,h);g.fillStyle='#1f5f8b';
for(var i=0;i<8;i++){var bh=(h-30)*(0.25+0.09*i);g.fillRect(12+i*(w-24)/8,h-10-bh,(w-24)/8-8,bh);}}draw();addEventListener('resize',draw);})();</script>"""
    mid = f'<div class="band"><div class="wrap"><h2>Chart</h2><div><canvas id="chart"></canvas></div><p>{LOREM}</p><div class="grid">{_cards(3)}</div></div></div>{js}'
    return _shell("Measured canvas", mid)


def case_legacy_fixed(ctx):
    """无 viewport meta 的 980 定宽老页(XHTML 过渡型 doctype, 表格布局)。"""
    rows = "".join(f'<tr><td width="200" bgcolor="#dfe6ee" valign="top"><b>Channel {i + 1}</b></td><td valign="top"><font size="2">{LOREM}</font></td>'
                   f'<td width="180" valign="top"><img src="/img/180x120/{(i * 61 + 50) % 256:02x}7799.png" width="180" height="120" alt=""></td></tr>' for i in range(8))
    return ('<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">'
            '<html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"><title>Legacy fixed</title>'
            '<style>body{margin:0;font-family:SimSun,Arial;font-size:12px}#w{width:980px;margin:0 auto}#hd{height:80px;background:#003366;color:#fff;line-height:80px;font-size:24px;padding-left:20px}</style></head>'
            f'<body><div id="w"><div id="hd">Legacy portal 980</div><table width="980" cellpadding="8" cellspacing="0" border="0">{rows}</table>'
            '<div style="padding:20px;background:#eee">Copyright legacy portal</div></div></body></html>')


def case_media_picture(ctx):
    """<picture> 艺术指导 + srcset/sizes: 窄屏换竖图, 高度随图变。"""
    pics = "".join(
        f'<div class="card"><picture><source media="(max-width:600px)" srcset="/img/600x700/{c}.png"><source media="(max-width:900px)" srcset="/img/800x500/{c}.png">'
        f'<img src="/img/1200x400/{c}.png" alt="art {i}" style="width:100%;height:auto"></picture><h2>Art directed {i + 1}</h2><p>{LOREM}</p></div>'
        for i, c in enumerate(("3a6ea5", "a55a3a", "3aa56e")))
    src = '<img src="/img/800x300/556677.png" srcset="/img/400x150/556677.png 400w, /img/800x300/556677.png 800w, /img/1600x600/556677.png 1600w" sizes="100vw" style="width:100%;height:auto;display:block" alt="wide">'
    return _shell("Media picture", f'{src}<div class="band"><div class="wrap">{pics}</div></div>')



# ---- 第二批(2026-11-11): 脚本排版/设备探测类机制, 按通用前端写法构造, 不复刻任何站 ----
def case_jslayout_masonry(ctx):
    """瀑布流: 脚本按容器宽算列数, 卡片 position:absolute + top/left px, 容器高 px; 监听 resize 重排。"""
    css = ".mas{position:relative}.mas .card{position:absolute;margin:0}"
    hs = [120, 200, 160, 260, 140, 220, 180, 240, 130, 210, 170, 250]
    items = "".join(f'<div class="card"><div style="height:{h}px;background:#{(i * 41 + 60) % 256:02x}8fa{i % 10}"></div><b>Tile {i + 1}</b></div>' for i, h in enumerate(hs))
    js = """<script>(function(){var m=document.getElementById('mas');function lay(){var cw=m.clientWidth,cols=cw>=900?4:cw>=560?3:2,g=12,w=Math.floor((cw-g*(cols-1))/cols),hh=[];
for(var c=0;c<cols;c++)hh.push(0);for(var i=0;i<m.children.length;i++){var k=m.children[i];k.style.width=w+'px';var c2=hh.indexOf(Math.min.apply(null,hh));
k.style.left=(c2*(w+g))+'px';k.style.top=hh[c2]+'px';hh[c2]+=k.offsetHeight+g;}m.style.height=Math.max.apply(null,hh)+'px';}lay();addEventListener('resize',lay);})();</script>"""
    mid = f'<div class="band"><div class="wrap"><h2>Feed</h2><div class="mas" id="mas">{items}</div></div></div>{js}'
    return _shell("JS masonry", mid, head=f"<style>{css}</style>")


def case_jslayout_masonry_once(ctx):
    """瀑布流(加载时排一次, 不监听 resize): 列数/位置只在首载宽度下算。"""
    html = case_jslayout_masonry(ctx)
    return html.replace("lay();addEventListener('resize',lay);", "lay();").replace("JS masonry", "JS masonry once")


def case_jslayout_slider(ctx):
    """量宽轮播(常见轮播库写法): 每张 slide 宽 = 容器宽 px, 轨道 translate3d(-n*宽px), resize 重算; 4s 自动翻页。"""
    css = ".sv{overflow:hidden;border-radius:10px}.st{display:flex;transition:transform .3s}.ss{flex:none;height:220px;color:#fff;font-size:28px;padding:24px}"
    sl = "".join(f'<div class="ss" style="background:#{c}">Slide {i + 1}</div>' for i, c in enumerate(("2e7d32", "1565c0", "6a1b9a", "ef6c00")))
    js = """<script>(function(){var v=document.getElementById('sv'),t=v.firstElementChild,n=1;function lay(){var w=v.clientWidth;for(var i=0;i<t.children.length;i++)t.children[i].style.width=w+'px';
t.style.width=(w*t.children.length)+'px';t.style.transform='translate3d('+(-n*w)+'px,0,0)';}lay();addEventListener('resize',lay);setInterval(function(){n=(n+1)%t.children.length;lay();},4000);})();</script>"""
    mid = f'<div class="band"><div class="wrap"><div class="sv" id="sv"><div class="st">{sl}</div></div><p>{LOREM}</p><div class="grid">{_cards(3)}</div></div></div>{js}'
    return _shell("JS slider", mid, head=f"<style>{css}</style>")


def case_jslayout_clamp(ctx):
    """脚本按宽截断文字: 逐字缩短到两行高度以内并补省略号(加载时 + resize 时按原文重算)。"""
    css = ".cl{font-size:16px;line-height:24px;margin:0 0 14px;background:#fff;padding:10px;border-radius:8px}"
    paras = "".join(f'<p class="cl" data-full="{LOREM} {LOREM}">{LOREM} {LOREM}</p>' for _ in range(6))
    js = """<script>(function(){function fit(){document.querySelectorAll('.cl').forEach(function(p){var t=p.getAttribute('data-full');p.textContent=t;
if(p.clientHeight<=68)return;var lo=0,hi=t.length;while(lo<hi){var mid=(lo+hi+1)>>1;p.textContent=t.slice(0,mid)+'…';if(p.clientHeight<=68)lo=mid;else hi=mid-1;}p.textContent=t.slice(0,lo)+'…';});}
fit();addEventListener('resize',fit);})();</script>"""
    mid = f'<div class="band"><div class="wrap"><h2>Summaries</h2>{paras}</div></div>{js}'
    return _shell("JS clamp", mid, head=f"<style>{css}</style>")


def case_device_flexible(ctx):
    """flexible 方案: 脚本按 DPR 改写 viewport(initial-scale=1/dpr) + html 字号 = 布局宽/10 + data-dpr 属性, 样式用 rem 与 [data-dpr] 选择器。"""
    css = """body{margin:0;font-family:Arial;background:#f4f4f4;font-size:.4rem}.fh{background:#283593;color:#fff;padding:.4rem;font-size:.5rem}
.fl{display:flex;flex-wrap:wrap;padding:.2rem}.fc{width:4.4rem;margin:.15rem;background:#fff;border-radius:.15rem;padding:.25rem}.fc i{display:block;height:2.6rem;background:#7986cb;margin-bottom:.2rem}
[data-dpr="2"] .fc b{font-size:28px}[data-dpr="3"] .fc b{font-size:42px}[data-dpr="1"] .fc b{font-size:14px}.ff{padding:.5rem .3rem;color:#666}"""
    js = """<script>(function(){var d=document.documentElement,dpr=Math.min(3,Math.floor(window.devicePixelRatio||1)),isM=/iPhone|Android/.test(navigator.userAgent);if(!isM)dpr=1;
var sc=1/dpr,m=document.querySelector('meta[name=viewport]');if(!m){m=document.createElement('meta');m.name='viewport';document.head.appendChild(m);}
m.content='width=device-width,initial-scale='+sc+',maximum-scale='+sc+',minimum-scale='+sc+',user-scalable=no';d.setAttribute('data-dpr',dpr);
function rf(){var w=d.getBoundingClientRect().width;if(w/dpr>540)w=540*dpr;d.style.fontSize=(w/10)+'px';}rf();addEventListener('resize',rf);})();</script>"""
    cells = "".join(f'<div class="fc"><i></i><b>Flex card {i + 1}</b><p>Rem sized card under a device pixel ratio scaled viewport.</p></div>' for i in range(8))
    return _page("Device flexible", f'<div class="fh">Flexible layout</div><div class="fl">{cells}</div><div class="ff">footer</div>', head=js, css=css, viewport=False)


def case_device_touch(ctx):
    """触屏探测: 脚本按 ontouchstart / pointer:coarse 给 <html> 加 touch 类, 样式据此放大点按区、换成横滑列表。"""
    css = """.tl{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.touch .tl{display:flex;overflow-x:auto;gap:10px}.touch .tl .card{flex:0 0 72%}
.touch .btn{display:block;text-align:center;padding:16px}.tip{background:#fff3cd;padding:10px;border-radius:6px;margin-bottom:12px}.touch .tip{display:none}"""
    js = "<script>(function(){var t=('ontouchstart' in window)||(window.matchMedia&&matchMedia('(pointer:coarse)').matches);if(t)document.documentElement.className+=' touch';})();</script>"
    mid = f'<div class="band"><div class="wrap"><div class="tip">Hover cards for details</div><div class="tl">{_cards(4)}</div><p><a class="btn" href="#x">Tap target</a></p></div></div>'
    return _shell("Device touch", mid, head=f"<style>{css}</style>{js}")


def case_device_matchmedia(ctx):
    """matchMedia 分支: 脚本按 (max-width:767px) 渲染不同导航(抽屉按钮 + 底部栏 / 顶部横排), 并监听 change 切换。"""
    css = ".tn{display:flex;gap:16px;padding:12px 24px;background:#fff}.dr{padding:12px 16px;background:#fff;font-weight:bold}.bt{position:sticky;bottom:0;display:flex;justify-content:space-around;background:#263238;color:#fff;padding:12px}"
    js = """<script>(function(){var q=matchMedia('(max-width:767px)'),h=document.getElementById('navhost'),f=document.getElementById('bothost');
function r(){if(q.matches){h.innerHTML='<div class="dr">&#9776; Sections</div>';f.innerHTML='<div class="bt"><span>Home</span><span>Find</span><span>Cart</span><span>Me</span></div>';}
else{h.innerHTML='<div class="tn"><span>Home</span><span>Products</span><span>Solutions</span><span>Pricing</span><span>Support</span></div>';f.innerHTML='';}}
r();q.addEventListener?q.addEventListener('change',r):q.addListener(r);})();</script>"""
    mid = (f'<div id="navhost"></div><div class="band"><div class="wrap"><h2>Main</h2><div class="grid">{_cards(6)}</div></div></div><div id="bothost"></div>{js}')
    return _shell("Device matchMedia", mid, head=f"<style>{css}</style>")


def case_device_vhfix(ctx):
    """首屏高度修正: 脚本把首屏设为 window.innerHeight px(规避移动端 100vh 地址栏问题), resize 时重设。"""
    css = ".vf{display:flex;align-items:center;justify-content:center;background:#004d40;color:#fff;font-size:30px}"
    js = "<script>(function(){var e=document.getElementById('vf');function s(){e.style.height=window.innerHeight+'px';}s();addEventListener('resize',s);})();</script>"
    mid = f'<div class="vf" id="vf">Full height intro</div><div class="band"><div class="wrap"><div class="grid">{_cards(3)}</div></div></div>{js}'
    return _shell("Device vh fix", mid, head=f"<style>{css}</style>")


def case_device_container(ctx):
    """容器查询(纯 CSS 对照): @container 按卡片容器宽换排版。"""
    css = """.cq{container-type:inline-size}.cqi{display:grid;grid-template-columns:1fr;gap:10px;background:#fff;border-radius:10px;padding:14px}
@container (min-width:420px){.cqi{grid-template-columns:160px 1fr}}.cqi i{display:block;height:110px;background:#8d6e63;border-radius:6px}
.cqg{display:grid;grid-template-columns:2fr 1fr;gap:16px}@media (max-width:700px){.cqg{grid-template-columns:1fr}}"""
    box = "".join(f'<div class="cq"><div class="cqi"><i></i><div><b>Container card {i + 1}</b><p>{LOREM}</p></div></div></div>' for i in range(4))
    return _shell("Device container", f'<div class="band"><div class="wrap"><div class="cqg">{box}</div></div></div>', head=f"<style>{css}</style>")


def case_device_hscroll(ctx):
    """横滑标签条: 加载时脚本把当前项滚到可视区(scrollLeft 只在窄屏下非 0)。"""
    css = ".hs{display:flex;overflow-x:auto;gap:8px;padding:10px 0;scrollbar-width:none}.hs span{flex:none;padding:8px 16px;background:#fff;border-radius:16px;border:1px solid #ccd}.hs .on{background:#1f5f8b;color:#fff}"
    tabs = "".join(f'<span class="{"on" if i == 9 else ""}">Topic {i + 1}</span>' for i in range(14))
    js = "<script>(function(){var h=document.getElementById('hs'),o=h.querySelector('.on');h.scrollLeft=Math.max(0,o.offsetLeft-h.clientWidth/2+o.offsetWidth/2);})();</script>"
    mid = f'<div class="band"><div class="wrap"><div class="hs" id="hs">{tabs}</div><div class="grid">{_cards(3)}</div></div></div>{js}'
    return _shell("Device hscroll", mid, head=f"<style>{css}</style>")


def case_structure_nth(ctx):
    """按 UA 换组件 + 奇偶样式: 区块用 :nth-child(2n) 交替底色; 手机 UA 下脚本把第一个区块的内容换成另一种结构的引导条(区块本身还在, 变体根落在它上)。
    克隆里备选子树若作常驻兄弟插入, 隐藏着也参与计数, 桌面/平板全页底色对调。"""
    css = ".sec{padding:28px 24px;min-height:160px}.sec:nth-child(2n){background:#dfe7f0}.sec:nth-child(2n+1){background:#fff}.app{padding:18px;background:#263238;color:#fff}"
    secs = "".join(f'<section class="sec"><h2>Section {i + 1}</h2><p>{LOREM}</p></section>' for i in range(6))
    js = """<script>(function(){if(!/iPhone|Android/.test(navigator.userAgent))return;var s=document.querySelector('#secs .sec');
s.innerHTML='<div class="app"><b>Get the app</b><ul><li>Faster</li><li>Offline</li></ul></div>';})();</script>"""
    return _shell("Structure nth", f'<div id="secs">{secs}</div>{js}', head=f"<style>{css}</style>")


# --------------------------------------------------------------------------- 交互漂移 / 滚动驱动
# 这两类看"克隆里脚本再跑一遍后还是不是采集时那一版"。原页每次加载本来就不同, 探针(活体原页对克隆)
# 没有意义, 所以门槛改看引擎交互轨分(克隆对采集时冻结的基准), 见 ENGINE_GATED。
_PICKS = [("Mountain trail guide", 1), ("Coastal weekend plan with ferry timetable and tide notes", 3), ("City food walk", 2),
          ("Desert night sky", 1), ("River kayak beginner notes and safety checklist", 4), ("Forest cabin list", 2),
          ("Island hopping budget", 3), ("Winter rail journeys across the northern line", 1)]


def _picks_js():
    return "var POOL=" + json.dumps([{"t": t, "n": n} for t, n in _PICKS]) + ";var LOREM=" + json.dumps(LOREM) + ";"


def case_drift_random(ctx):
    """随机推荐位: 加载时用 Math.random 洗牌, 取前 4 条渲染(每条段落数不同, 换一批整页高度就变)。"""
    js = "<script>(function(){" + _picks_js() + """var a=POOL.slice();for(var i=a.length-1;i>0;i--){var j=Math.floor(Math.random()*(i+1)),t=a[i];a[i]=a[j];a[j]=t;}
var h='';for(var k=0;k<4;k++){h+='<div class="card" style="margin-bottom:12px"><h2>'+a[k].t+'</h2>';for(var m=0;m<a[k].n;m++)h+='<p>'+LOREM+'</p>';h+='</div>';}
document.getElementById('rec').innerHTML=h;})();</script>"""
    mid = f'<div class="band"><div class="wrap"><h2>Recommended for you</h2><div id="rec"></div></div></div>{js}'
    return _shell("Drift random", mid)


def case_drift_random_late(ctx):
    """随机广告/推荐位延后渲染: load 后 600ms 才用 Math.random 选一条横幅(高度各不同), 之前有别的脚本也消耗随机数。"""
    js = "<script>var __ids=[];for(var q=0;q<5;q++)__ids.push(Math.random().toString(36).slice(2));</script><script>(function(){" + _picks_js() + """
addEventListener('load',function(){setTimeout(function(){var p=POOL[Math.floor(Math.random()*POOL.length)],b=document.getElementById('promo');
var h='<h2>'+p.t+'</h2>';for(var m=0;m<p.n;m++)h+='<p>'+LOREM+'</p>';b.innerHTML=h;b.style.background=['#fde8c8','#d8ecf8','#e3f3d9','#f5dbe6'][Math.floor(Math.random()*4)];},600);});})();</script>"""
    mid = (f'<div class="band"><div class="wrap"><div id="promo" class="card" style="min-height:40px"></div></div></div>'
           f'<div class="band alt"><div class="wrap"><div class="grid">{_cards(3)}</div></div></div>{js}')
    return _shell("Drift random late", mid)


def case_drift_clock(ctx):
    """按时钟选内容: 用当前"分钟"选今日专题(段落数不同), 并显示"更新于 hh:mm" —— 克隆回放时钟不同就换了一条。"""
    js = "<script>(function(){" + _picks_js() + """var d=new Date(),p=POOL[(d.getHours()*60+d.getMinutes())%POOL.length],h='<h2>Feature of the minute: '+p.t+'</h2>';
for(var m=0;m<p.n;m++)h+='<p>'+LOREM+'</p>';h+='<p><small>Updated at '+('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2)+'</small></p>';
document.getElementById('feat').innerHTML=h;})();</script>"""
    mid = f'<div class="band"><div class="wrap"><div id="feat" class="card"></div></div></div><div class="band alt"><div class="wrap"><div class="grid">{_cards(3)}</div></div></div>{js}'
    return _shell("Drift clock", mid)


def case_drift_rotator(ctx):
    """自轮播横幅起始相位随机 + 每 3s 换一张(各张高度不同): 评分时相位与采集时不同。"""
    css = ".rot>div{display:none;padding:24px;border-radius:10px;color:#fff}.rot>div.on{display:block}"
    js = """<script>(function(){var s=document.querySelectorAll('.rot>div'),k=Math.floor(Math.random()*s.length);s[k].classList.add('on');
setInterval(function(){s[k].classList.remove('on');k=(k+1)%s.length;s[k].classList.add('on');},3000);})();</script>"""
    slides = "".join(f'<div style="background:#{c}"><h2>Banner {i + 1}</h2>' + f"<p>{LOREM}</p>" * (i + 1) + "</div>"
                     for i, c in enumerate(("1f5f8b", "7b3f8c", "2e7d32", "8c5a1f")))
    mid = f'<div class="band"><div class="wrap"><div class="rot">{slides}</div></div></div><div class="band alt"><div class="wrap"><div class="grid">{_cards(3)}</div></div></div>{js}'
    return _shell("Drift rotator", mid, head=f"<style>{css}</style>")


def case_scrollfx_progress(ctx):
    """滚动驱动的钉住舞台(产品页常见): 300vh 轨道里 sticky 舞台, 脚本按滚动进度改标题透明度/位移与色块缩放。"""
    css = """.track{height:300vh;position:relative;background:#0f1722}.stage{position:sticky;top:0;height:100vh;overflow:hidden;display:flex;flex-direction:column;align-items:center;justify-content:center;color:#fff}
.stage .blob{width:40vw;height:40vw;max-width:420px;max-height:420px;border-radius:24px;background:#f0a500;will-change:transform}.stage h2{font-size:40px;margin-top:24px}"""
    js = """<script>(function(){var tr=document.getElementById('track'),bl=document.getElementById('blob'),tt=document.getElementById('stt'),cap=document.getElementById('cap');
function up(){var r=tr.getBoundingClientRect(),p=Math.min(1,Math.max(0,-r.top/(r.height-innerHeight)));bl.style.transform='scale('+(0.5+p)+') rotate('+(p*90)+'deg)';bl.style.background=p>0.5?'#2e7d32':'#f0a500';
tt.style.opacity=String(1-p);tt.style.transform='translateY('+(-80*p)+'px)';cap.textContent=p<0.33?'Stage one':p<0.66?'Stage two':'Stage three';}
addEventListener('scroll',up,{passive:true});addEventListener('resize',up);up();})();</script>"""
    mid = (f'<div class="track" id="track"><div class="stage"><div class="blob" id="blob"></div><h2 id="stt">Pinned story</h2><p id="cap">Stage one</p></div></div>'
           f'<div class="band"><div class="wrap"><div class="grid">{_cards(3)}</div></div></div>{js}')
    return _shell("Scrollfx progress", mid, head=f"<style>{css}</style>")


def case_scrollfx_parallax(ctx):
    """视差与随滚动淡入(不可回退地写内联样式): 背景层按 scrollY*0.4 位移, 卡片按进入比例设 opacity。"""
    css = ".px{position:relative;height:520px;overflow:hidden;background:#263b50;color:#fff}.px .bg{position:absolute;left:0;right:0;top:-120px;height:760px;background:linear-gradient(#7b3f8c,#1f5f8b 50%,#2e7d32)}.px .fg{position:relative;padding:160px 24px}"
    js = """<script>(function(){var bg=document.getElementById('pbg'),cs=document.querySelectorAll('.fade');
function up(){bg.style.transform='translateY('+(scrollY*0.4)+'px)';for(var i=0;i<cs.length;i++){var r=cs[i].getBoundingClientRect(),v=Math.min(1,Math.max(0,(innerHeight-r.top)/260));cs[i].style.opacity=String(0.15+0.85*v);cs[i].style.transform='translateY('+(30*(1-v))+'px)';}}
addEventListener('scroll',up,{passive:true});up();})();</script>"""
    cards = "".join(f'<div class="card fade"><h2>Fade {i + 1}</h2><p>{LOREM}</p></div>' for i in range(6))
    mid = (f'<div class="px"><div class="bg" id="pbg"></div><div class="fg"><h2>Parallax band</h2><p>{LOREM}</p></div></div>'
           f'<div class="band"><div class="wrap"><div class="grid">{cards}</div></div></div>{js}')
    return _shell("Scrollfx parallax", mid, head=f"<style>{css}</style>")


def case_scrollfx_timeline(ctx):
    """纯 CSS 滚动驱动动画(animation-timeline: view()/scroll()): 进度条与区块进场。"""
    css = """@keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}@keyframes rise{from{opacity:.1;transform:translateY(60px) scale(.9)}to{opacity:1;transform:none}}
.prog{position:fixed;left:0;top:0;height:6px;width:100%;background:#f0a500;transform-origin:0 50%;animation:grow linear both;animation-timeline:scroll(root);z-index:30}
.vt{animation:rise linear both;animation-timeline:view();animation-range:entry 0% cover 35%}"""
    secs = "".join(f'<div class="band{" alt" if i % 2 else ""}"><div class="wrap vt"><h2>Timeline section {i + 1}</h2><div class="grid">{_cards(3, i * 3)}</div></div></div>' for i in range(3))
    return _shell("Scrollfx timeline", f'<div class="prog"></div>{secs}', head=f"<style>{css}</style>")


# --------------------------------------------------------------------------- 接口回放
_BLOCKS = {"news": 9, "sports": 4, "local": 12, "finance": 6}


def case_api_post_keyed(ctx):
    """一个 POST 接口靠请求体区分区块(门户站常见: 全页区块都打同一个 blockdata 接口, body 里带区块键和每次随机的请求号)。
    克隆回放若只按 方法+路径 匹配, 所有区块拿到同一份响应, 按键取不到数据就成了空块。"""
    js = """<script>(function(){document.querySelectorAll('[data-block]').forEach(function(el){var k=el.getAttribute('data-block');
fetch('/api/block',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({requestId:Math.random().toString(36).slice(2),pv:Date.now(),resourceList:[{tplCompKey:k,size:20}]})})
.then(function(r){return r.json();}).then(function(d){var b=(d.data||{})[k];if(!b){el.innerHTML='';return;}
var h='<h2>'+b.title+'</h2><ul>';b.list.forEach(function(t){h+='<li style="padding:6px 0;border-bottom:1px solid #d9dee5">'+t+'</li>';});el.innerHTML=h+'</ul>';}).catch(function(){el.innerHTML='';});});})();</script>"""
    blocks = "".join(f'<div class="card" data-block="{k}" style="margin-bottom:16px;min-height:20px"></div>' for k in _BLOCKS)
    return _shell("Api post keyed", f'<div class="band"><div class="wrap">{blocks}</div></div>{js}')


def case_api_get_variants(ctx):
    """同一 GET 接口按查询参数分页/分栏(?tab=&page=), 另带每次变化的时间戳参数: 回放要按参数挑对那一份。"""
    js = """<script>(function(){var tabs=['hot','new','top'];tabs.forEach(function(t,i){fetch('/api/list?tab='+t+'&page=1&_='+Date.now()+i).then(function(r){return r.json();}).then(function(d){
var el=document.getElementById('tab-'+t),h='<h2>'+d.title+'</h2>';d.list.forEach(function(x){h+='<p style="margin:0;padding:6px 0;border-bottom:1px solid #d9dee5">'+x+'</p>';});el.innerHTML=h;}).catch(function(){});});})();</script>"""
    cols = "".join(f'<div class="card" id="tab-{t}" style="min-height:20px"></div>' for t in ("hot", "new", "top"))
    return _shell("Api get variants", f'<div class="band"><div class="wrap"><div class="grid">{cols}</div></div></div>{js}')


def case_api_post_rerender(ctx):
    """同上, 但页面是"应用壳"写法: 启动时清空根节点重建骨架, 每个区块等自己的数据到了才挂上去(拿不到就不挂)。"""
    js = """<script>(function(){var root=document.getElementById('app'),keys=""" + json.dumps(list(_BLOCKS)) + """;root.textContent='';
var col=document.createElement('div');col.className='wrap';root.appendChild(col);var slots={};keys.forEach(function(k){var s=document.createElement('section');s.setAttribute('data-slot',k);col.appendChild(s);slots[k]=s;});
keys.forEach(function(k){fetch('/api/block',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({requestId:Math.random().toString(36).slice(2),pv:Date.now(),resourceList:[{tplCompKey:k,size:20}]})})
.then(function(r){return r.json();}).then(function(d){var b=(d.data||{})[k];if(!b)return;var c=document.createElement('div');c.className='card';c.style.marginBottom='16px';
var h='<h2>'+b.title+'</h2><ul>';b.list.forEach(function(t){h+='<li style="padding:6px 0;border-bottom:1px solid #d9dee5">'+t+'</li>';});c.innerHTML=h+'</ul>';slots[k].appendChild(c);});});})();</script>"""
    return _shell("Api post rerender", f'<div class="band" id="app"></div>{js}')


def case_lazy_infinite_cols(ctx):
    """无限加载, 列数随屏宽: 底部哨兵进视口就按 列数/页码 去取下一页追加(共 4 页), 每页条数 = 列数 × 2。"""
    css = ".inf{display:grid;gap:12px}.inf .card{min-height:150px}#sentinel{height:1px}"
    js = """<script>(function(){var g=document.getElementById('inf'),s=document.getElementById('sentinel'),page=0,busy=false,cols=Math.max(1,Math.floor(g.clientWidth/260));
g.style.gridTemplateColumns='repeat('+cols+',1fr)';function more(){if(busy||page>=4)return;busy=true;page++;
fetch('/api/feed?cols='+cols+'&page='+page).then(function(r){return r.json();}).then(function(d){d.items.forEach(function(t){var c=document.createElement('div');c.className='card';c.innerHTML='<h2>'+t+'</h2><p>Loaded on demand.</p>';g.appendChild(c);});busy=false;
if(s.getBoundingClientRect().top<innerHeight+200)more();}).catch(function(){busy=false;});}
new IntersectionObserver(function(es){if(es[0].isIntersecting)more();},{rootMargin:'200px'}).observe(s);})();</script>"""
    mid = f'<div class="band"><div class="wrap"><h2>Endless feed</h2><div class="inf" id="inf"></div><div id="sentinel"></div></div></div>{js}'
    return _shell("Lazy infinite cols", mid, head=f"<style>{css}</style>")


def case_scrollfx_sticky_measured(ctx):
    """脚本按视口高度定钉住段的高度(产品故事页常见): 轨道高 = 2.5 × innerHeight(load/resize 时写内联),
    里面 sticky 舞台高 100vh, 起始帧/结束帧两张图按滚动进度交叉淡入淡出。"""
    css = """.strack{position:relative;background:#0f1722}.sstage{position:sticky;top:0;height:100vh;overflow:hidden;color:#fff;display:flex;align-items:center;justify-content:center}
.sstage img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.sstage h2{position:relative;font-size:40px;text-shadow:0 2px 8px #000}"""
    js = """<script>(function(){var tr=document.getElementById('strack'),a=document.getElementById('fa'),b=document.getElementById('fb'),tt=document.getElementById('stitle');
function size(){tr.style.height=Math.round(innerHeight*2.5)+'px';up();}
function up(){var r=tr.getBoundingClientRect(),p=Math.min(1,Math.max(0,-r.top/Math.max(1,r.height-innerHeight)));a.style.opacity=String(1-p);b.style.opacity=String(p);tt.textContent=p<0.5?'Start frame':'End frame';}
addEventListener('scroll',up,{passive:true});addEventListener('resize',size);size();})();</script>"""
    mid = (f'<div class="band"><div class="wrap"><div class="grid">{_cards(3)}</div></div></div>'
           f'<div class="strack" id="strack"><div class="sstage"><img id="fa" src="/img/1200x800/1f5f8b.png" alt="start"><img id="fb" src="/img/1200x800/7b3f8c.png" alt="end" style="opacity:0"><h2 id="stitle">Start frame</h2></div></div>'
           f'<div class="band"><div class="wrap"><div class="grid">{_cards(6, 3)}</div></div></div>{js}')
    return _shell("Scrollfx sticky measured", mid, head=f"<style>{css}</style>")


def _index_groups_js(trigger):
    return ("<script>(function(){function render(){var n=document.getElementById('idx');for(var g=0;g<3;g++){var h='<div class=\"idx-group\"><h3>Group '+(g+1)+'</h3><ul>';"
            "for(var i=0;i<4+g;i++)h+='<li>Index link '+(g+1)+'.'+(i+1)+'</li>';n.insertAdjacentHTML('beforeend',h+'</ul></div>');}}" + trigger + "})();</script>")


_IDX_CSS = "#idx{display:flex;flex-wrap:wrap;gap:24px;padding:24px;background:#e9eef4}.idx-group{flex:1 1 220px}.idx-group h3{margin:0 0 8px}.idx-group ul{margin:0;padding-left:18px}"


def case_rerun_append_dcl(ctx):
    """非幂等的客户端渲染: 源码里页底索引导航是空容器, DOMContentLoaded 时脚本往里追加 3 组(不先清空)。
    克隆里冻结了 3 组, 脚本重跑又追加 3 组 → 页底翻倍。"""
    mid = f'<div class="band"><div class="wrap"><div class="grid">{_cards(3)}</div></div></div><nav id="idx"></nav>'
    return _shell("Rerun append dcl", mid + _index_groups_js("document.addEventListener('DOMContentLoaded',render);"), head=f"<style>{_IDX_CSS}</style>")


def case_rerun_append_async(ctx):
    """同上, 但等数据接口回来才追加(接口在克隆里回放得到, 所以一定会再追加一遍)。"""
    mid = f'<div class="band"><div class="wrap"><div class="grid">{_cards(3)}</div></div></div><nav id="idx"></nav>'
    return _shell("Rerun append async", mid + _index_groups_js("fetch('/api/list?tab=hot&page=1').then(function(r){return r.json();}).then(render);"),
                  head=f"<style>{_IDX_CSS}</style>")


def case_state_tab_spy(ctx):
    """采集期被脚本改过的"当前项": 桌面宽全部面板都显示, 滚动监听把 .active 移到视口里的那一项;
    ≤768 宽变成选项卡只显示 .active 面板(各面板高度不同)。采集时滚过全页, active 停在后面的项;
    新开的原页在窄屏默认显示第 1 项。"""
    css = """.tabs{display:flex;gap:8px;margin-bottom:12px}.tabs b{padding:8px 12px;border:1px solid #b9c2cc;border-radius:6px}.tabs b.active{background:#14324d;color:#fff}
.pn{background:#fff;border:1px solid #d9dee5;border-radius:10px;padding:16px;margin-bottom:16px}
@media (max-width:768px){.pn{display:none}.pn.active{display:block}}"""
    js = """<script>(function(){var ps=document.querySelectorAll('.pn'),ts=document.querySelectorAll('.tabs b');function set(i){for(var k=0;k<ps.length;k++){ps[k].classList.toggle('active',k===i);ts[k].classList.toggle('active',k===i);}}
function spy(){if(innerWidth<=768)return;var best=0;for(var k=0;k<ps.length;k++){if(ps[k].getBoundingClientRect().top<innerHeight*0.5)best=k;}set(best);}
ts.forEach(function(t,i){t.addEventListener('click',function(){set(i);});});addEventListener('scroll',spy,{passive:true});set(0);})();</script>"""
    panes = "".join(f'<div class="pn" id="pn-{i}"><h2>Module {c}</h2>' + f"<p>{LOREM}</p>" * (i + 2) + "</div>" for i, c in enumerate("ABCD"))
    tabs = '<div class="tabs">' + "".join(f"<b>Tab {c}</b>" for c in "ABCD") + "</div>"
    mid = f'<div class="band"><div class="wrap">{tabs}{panes}</div></div><div class="band alt"><div class="wrap"><div class="grid">{_cards(6)}</div></div></div>{js}'
    return _shell("State tab spy", mid, head=f"<style>{css}</style>")


def case_state_carousel_frame(ctx):
    """采集期已经转过去的轮播帧: 3 个卡片轮播各 4 帧(颜色/文字不同, 高度相同), 每 3s 换帧并更新进度条。
    新开的原页都在第 1 帧; 克隆应从第 1 帧起播, 而不是冻结在采集时转到的那一帧。"""
    css = ".sw{position:relative;height:220px;border-radius:10px;overflow:hidden;margin-bottom:16px}.sw>div{position:absolute;inset:0;display:none;padding:24px;color:#fff}.sw>div.cur{display:block}.sw i{position:absolute;left:0;bottom:0;height:6px;background:#f0a500}"
    js = """<script>(function(){document.querySelectorAll('.sw').forEach(function(sw){var fs=sw.querySelectorAll('div'),bar=sw.querySelector('i'),k=0;function show(){for(var j=0;j<fs.length;j++)fs[j].classList.toggle('cur',j===k);bar.style.width=((k+1)*25)+'%';}
show();setInterval(function(){k=(k+1)%fs.length;show();},3000);});})();</script>"""
    cols = ("1f5f8b", "7b3f8c", "2e7d32", "8c5a1f")
    sws = "".join('<div class="sw">' + "".join(f'<div style="background:#{cols[(j + n) % 4]}"><h2>Product {n + 1} frame {j + 1}</h2><p>Frame text {j + 1} of carousel {n + 1}.</p></div>' for j in range(4)) + "<i></i></div>" for n in range(3))
    mid = f'<div class="band"><div class="wrap">{sws}</div></div>{js}'
    return _shell("State carousel frame", mid, head=f"<style>{css}</style>")


CASES = {
    "responsive_grid": ("responsive", case_responsive_grid),
    "responsive_nav": ("responsive", case_responsive_nav),
    "ua_server_template": ("ua", case_ua_server_template),
    "ua_redirect": ("ua", case_ua_redirect),
    "ua_client_branch": ("ua", case_ua_client_branch),
    "structure_swap": ("structure", case_structure_swap),
    "structure_nth": ("structure", case_structure_nth),
    "rerun_list_trim": ("rerun", case_rerun_list_trim),
    "rerun_block_drop": ("rerun", case_rerun_block_drop),
    "rerun_append": ("rerun", case_rerun_append),
    "rerun_state": ("rerun", case_rerun_state),
    "lazy_reveal": ("lazy", case_lazy_reveal),
    "lazy_images": ("lazy", case_lazy_images),
    "lazy_sections": ("lazy", case_lazy_sections),
    "timed_carousel": ("timed", case_timed_carousel),
    "timed_accordion": ("timed", case_timed_accordion),
    "overlay_modal": ("overlay", case_overlay_modal),
    "pinned_layout": ("pinned", case_pinned_layout),
    "measured_inline": ("measured", case_measured_inline),
    "measured_rem": ("measured", case_measured_rem),
    "measured_canvas": ("measured", case_measured_canvas),
    "legacy_fixed": ("legacy", case_legacy_fixed),
    "media_picture": ("media", case_media_picture),
    "jslayout_masonry": ("jslayout", case_jslayout_masonry),
    "jslayout_masonry_once": ("jslayout", case_jslayout_masonry_once),
    "jslayout_slider": ("jslayout", case_jslayout_slider),
    "jslayout_clamp": ("jslayout", case_jslayout_clamp),
    "device_flexible": ("device", case_device_flexible),
    "device_touch": ("device", case_device_touch),
    "device_matchmedia": ("device", case_device_matchmedia),
    "device_vhfix": ("device", case_device_vhfix),
    "device_container": ("device", case_device_container),
    "device_hscroll": ("device", case_device_hscroll),
    "rerun_append_dcl": ("rerun", case_rerun_append_dcl),
    "rerun_append_async": ("rerun", case_rerun_append_async),
    "state_tab_spy": ("state", case_state_tab_spy),
    "state_carousel_frame": ("state", case_state_carousel_frame),
    "api_post_keyed": ("api", case_api_post_keyed),
    "api_get_variants": ("api", case_api_get_variants),
    "api_post_rerender": ("api", case_api_post_rerender),
    "lazy_infinite_cols": ("lazy", case_lazy_infinite_cols),
    "scrollfx_sticky_measured": ("scrollfx", case_scrollfx_sticky_measured),
    "drift_random": ("drift", case_drift_random),
    "drift_random_late": ("drift", case_drift_random_late),
    "drift_clock": ("drift", case_drift_clock),
    "drift_rotator": ("drift", case_drift_rotator),
    "scrollfx_progress": ("scrollfx", case_scrollfx_progress),
    "scrollfx_parallax": ("scrollfx", case_scrollfx_parallax),
    "scrollfx_timeline": ("scrollfx", case_scrollfx_timeline),
}
# 这些类别原页每次加载都不同(随机/时钟), 活体探针没有意义: 门槛看引擎交互轨分(克隆对采集时冻结的基准)
ENGINE_GATED = {"drift"}
# 这些类别探针照常算, 另加一格引擎交互轨分: 交互轨塌了时引擎会改交付静态版, 探针看不出来(sohu 类: 头条 99 / 交互 66)
INTERACTIVE_ALSO = {"api", "state"}
# 已知缺口: 用例保留、照常出分, 但不计入 --gate 门槛(修好后从这里删掉)
KNOWN_GAPS = {
    "measured_canvas": "iPad 820 宽 ~94: canvas 固化成位图后按宽缩放, 图里按固定 px 画的边距/间隙不会像原站那样重画",
    "measured_inline": "420 宽: 脚本按文字折行算的等高卡片高度, 手机段只有 390/359 两个采样, 高度随宽不单调, 仍钉 390 的 px",
}
CATEGORY_LABEL = {
    "responsive": "响应式(对照)", "ua": "按 UA 分发", "structure": "手机专有组件", "rerun": "脚本重跑",
    "lazy": "懒渲染/滚动进场", "timed": "定时状态", "overlay": "弹层/滚动锁", "pinned": "吸顶/固定",
    "measured": "脚本量宽/rem/canvas", "legacy": "无 viewport 老页", "media": "按宽换图",
    "state": "采集期被改的状态(当前项/轮播帧)", "api": "接口回放(按请求体/参数区分)", "drift": "交互漂移(随机/时钟/轮换相位)", "scrollfx": "滚动驱动动画",
    "jslayout": "脚本排版(瀑布流/量宽轮播/截断)", "device": "设备探测(DPR/触屏/matchMedia/vh/容器查询)",
}


def _png(w, h, hexrgb):
    from PIL import Image, ImageDraw
    rgb = tuple(int(hexrgb[i:i + 2], 16) for i in (0, 2, 4))
    im = Image.new("RGB", (w, h), rgb)
    d = ImageDraw.Draw(im)
    lite = tuple(min(255, c + 60) for c in rgb)
    d.rectangle([w * 0.1, h * 0.2, w * 0.55, h * 0.8], fill=lite)     # 非纯色: 裁切/拉伸/换图在截图里看得出
    d.ellipse([w * 0.6, h * 0.25, w * 0.9, h * 0.75], fill=(255, 255, 255))
    d.text((8, 6), f"{w}x{h}", fill=(0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):   # noqa: D401
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if ctype.startswith(("text/html", "application/json")) else "max-age=3600")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):   # noqa: N802
        u = urlsplit(self.path)
        path, q = u.path, parse_qs(u.query)
        m = re.fullmatch(r"/img/(\d{1,4})x(\d{1,4})/([0-9a-fA-F]{6})\.png", path)
        if m:
            return self._send(200, _png(int(m.group(1)), int(m.group(2)), m.group(3)), "image/png")
        if path == "/api/items":
            try:
                fresh = time.time() - float(q.get("t", ["0"])[0]) <= API_TTL_S
            except ValueError:
                fresh = False
            if not fresh:
                return self._send(403, json.dumps({"error": "expired"}), "application/json")
            n = int(q.get("n", ["3"])[0])
            return self._send(200, json.dumps({"items": [f"Item {i + 1}" for i in range(n)]}), "application/json")
        if path == "/api/feed":
            cols, page = int(q.get("cols", ["3"])[0]), int(q.get("page", ["1"])[0])
            items = [f"Feed p{page} #{i + 1} ({cols} cols)" for i in range(cols * 2)] if 1 <= page <= 4 else []
            return self._send(200, json.dumps({"items": items}), "application/json")
        if path == "/api/list":
            tab = q.get("tab", ["hot"])[0]
            n = {"hot": 5, "new": 9, "top": 3}.get(tab, 2)
            return self._send(200, json.dumps({"title": f"Tab {tab}", "list": [f"{tab} entry {i + 1} of {n}" for i in range(n)]}), "application/json")
        name = path.strip("/")
        if name.startswith("m/"):
            name = name[2:]
        if name in ("", "index"):
            links = "".join(f'<li><a href="/{k}">{k}</a> — {CATEGORY_LABEL[c]}</li>' for k, (c, _) in CASES.items())
            return self._send(200, _page("mobile cases", f'<div class="wrap"><h1>Mobile cases</h1><ul>{links}</ul></div>'))
        case = CASES.get(name)
        if not case:
            return self._send(404, "not found", "text/plain")
        ctx = {"phone": bool(PHONE_UA_RE.search(self.headers.get("User-Agent", ""))), "path": path, "now": time.time()}
        out = case[1](ctx)
        if isinstance(out, tuple) and out[0] == "redirect":
            return self._send(302, "", headers={"Location": out[1]})
        return self._send(200, out, headers={"Vary": "User-Agent"})


    def do_POST(self):   # noqa: N802
        u = urlsplit(self.path)
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            req = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            req = {}
        if u.path == "/api/block":
            data = {}
            for item in req.get("resourceList") or []:
                k = str(item.get("tplCompKey"))
                if k in _BLOCKS:
                    data[k] = {"title": f"Block {k}", "list": [f"{k} headline {i + 1} of {_BLOCKS[k]}" for i in range(_BLOCKS[k])]}
            return self._send(200, json.dumps({"code": 0, "data": data}), "application/json")
        return self._send(404, "not found", "text/plain")


def serve(port=0, host="127.0.0.1"):
    """后台线程起站点; 返回 (httpd, base_url)。主机名用 127.0.0.1: 平台 API 只收带点的域名。"""
    httpd = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


if __name__ == "__main__":
    import sys
    httpd, base = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8899)
    print(base, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        httpd.shutdown()
