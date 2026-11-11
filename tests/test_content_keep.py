"""内容守卫: 克隆页里脚本不等用户手势就整块拆掉已渲染内容(水合失配后客户端重建拿不到数据)时放回原块;
拆了再换一份、异步数据稍后补上、用户手势之后的移除都不管。"""
import unittest

from core.clone.support import CONTENT_KEEP_GUARD_JS
from tests._env import chromium_available

PAGE = """<!doctype html><html><head>GUARD</head><body style="margin:0">
<div id="top" style="height:200px">top</div>
<div id="sec"><div id="title" style="height:22px">Best Sellers</div><div id="car" style="height:394px">carousel</div></div>
<div id="foot"><footer id="f1" style="height:800px">footer</footer></div>
<div id="lazy"><div id="ph" style="height:300px">placeholder</div></div>
<div id="sec2"><div id="car2" style="height:394px">carousel2</div></div>
<div id="sec3"><div class="mb-6" style="height:22px">t</div><div id="ssr" class="relative z-0 select-none" style="height:394px">ssr</div></div>
<div id="apple"><div id="old" class="hero" style="height:300px">old</div></div>
<div id="wrap"><div class="slide" style="height:200px"></div><div class="slide" style="height:200px"></div></div>
<div id="foot2"><footer id="f2" style="height:800px">Shop Men Women Kids Sale Help Returns Stores About Careers Sustainability Privacy</footer></div>
<button id="b" onclick="document.getElementById('top').remove()">x</button>
<script>
  // 水合重建: 轮播整块被拆掉, 没有东西补上
  document.getElementById('car').remove();
  // 页脚拆了再换一份(不同高度): 不是塌掉
  var f = document.getElementById('f1'); var g = document.createElement('div'); g.style.height = '520px'; g.textContent = 'grid';
  f.parentNode.replaceChild(g, f);
  // 占位块先拆, 1.4s 后异步内容才到
  document.getElementById('ph').remove();
  setTimeout(function(){ var d = document.createElement('div'); d.id = 'real'; d.style.height = '300px'; d.textContent = 'real'; document.getElementById('lazy').appendChild(d); }, 1400);
  // Vue 式: 拆掉旧块后立刻插入空壳, 1.5s 后壳内才长出内容(父元素 childList 不再变, 只有尺寸变)
  document.getElementById('car2').remove();
  var shell = document.createElement('div'); shell.id = 'shell'; document.getElementById('sec2').appendChild(shell);
  setTimeout(function(){ var d = document.createElement('div'); d.style.height = '394px'; shell.appendChild(d); }, 1500);
  window.addEventListener('DOMContentLoaded', function(){
    // 水合追加而非接管: SSR 块旁再插一份同签名同高的块 → 1.5s 后旧块应被移除
    setTimeout(function(){ var d=document.createElement('div'); d.id='live'; d.className='relative z-0 select-none swiper-initialized'; d.style.height='394px'; document.getElementById('sec3').appendChild(d); }, 200);
    // 苹果类"先插新再删旧": 站点自己删旧, 守卫既不放回旧块也不动新块
    setTimeout(function(){ var d=document.createElement('div'); d.id='new'; d.className='hero'; d.style.height='300px'; document.getElementById('apple').appendChild(d); setTimeout(function(){ document.getElementById('old').remove(); }, 400); }, 200);
    // 换了标签/class 的重渲染(页脚 footer → div.grid), 文案相同: 也算孪生, 移除旧块
    setTimeout(function(){ var d=document.createElement('div'); d.id='grid'; d.className='grid grid-cols-1'; d.style.height='520px'; d.textContent='Shop Men Women Kids Sale Help Returns Stores About Careers Sustainability Privacy Terms'; document.getElementById('foot2').appendChild(d); }, 200);
    // 轮播复制幻灯片: 父下同签名已有多份, 不管
    setTimeout(function(){ var d=document.createElement('div'); d.className='slide'; d.style.height='200px'; document.getElementById('wrap').appendChild(d); }, 200);
  });
</script></body></html>"""


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class ContentKeep(unittest.TestCase):
    def test_guard(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 390, "height": 844})
                page.set_content(PAGE.replace("GUARD", CONTENT_KEEP_GUARD_JS))
                page.wait_for_timeout(900)
                self.assertEqual(page.evaluate("!!document.getElementById('car')"), True, "拆掉的轮播应放回")
                self.assertEqual(page.evaluate("document.getElementById('sec').children[1].id"), "car", "放回原位(标题之后)")
                self.assertEqual(page.evaluate("!!document.getElementById('f1')"), False, "换了一份的页脚不放回")
                self.assertEqual(page.evaluate("!!document.getElementById('ph')"), True, "占位块先放回")
                self.assertEqual(page.evaluate("!!document.getElementById('car2')"), True, "空壳未长高前旧块先放回")
                page.wait_for_timeout(1400)
                self.assertEqual(page.evaluate("!!document.getElementById('real')"), True)
                self.assertEqual(page.evaluate("!!document.getElementById('ph')"), False, "异步内容到了, 放回的占位块撤掉")
                self.assertEqual(page.evaluate("!!document.getElementById('car2')"), False, "壳内长出内容(父元素只变尺寸)后旧块撤掉")
                self.assertEqual(page.evaluate("window.__cloneKept"), 1)
                self.assertEqual(page.evaluate("!!document.getElementById('ssr')"), False, "水合追加的孪生块: 序列化旧块移除")
                self.assertEqual(page.evaluate("!!document.getElementById('live')"), True)
                self.assertEqual(page.evaluate("!!document.getElementById('old')"), False, "站点自己删的旧块不放回")
                self.assertEqual(page.evaluate("!!document.getElementById('new')"), True)
                self.assertEqual(page.evaluate("document.querySelectorAll('#wrap .slide').length"), 3)
                self.assertEqual(page.evaluate("!!document.getElementById('f2')"), False, "换标签重渲染的页脚: 文案相同, 旧块移除")
                self.assertEqual(page.evaluate("!!document.getElementById('grid')"), True)
                self.assertEqual(page.evaluate("window.__cloneHydrDedup"), 2)
                page.click("#b")   # 用户手势之后的移除不管
                page.wait_for_timeout(900)
                self.assertEqual(page.evaluate("!!document.getElementById('top')"), False)
            finally:
                b.close()


LIST_PAGE = """<!doctype html><html><head>GUARD</head><body style="margin:0">
<div id="w" style="display:flex;overflow:hidden">CARDS</div>
<div id="w2" style="display:flex;overflow:hidden">CARDS</div>
<div id="w3" style="display:flex;overflow:hidden">CARDS</div>
<div id="w4" style="display:flex;overflow:hidden">CARDS</div>
<script>
  function mk(t, i) { var d = document.createElement('div'); d.className = 'slide card'; d.style.cssText = 'width:300px;height:400px;flex:none'; d.textContent = t + ' ' + i; return d; }
  function kids(id) { return [].slice.call(document.getElementById(id).children); }
  // 按 1 条数据水合: 其余 11 张被当多余节点删掉(横向列表, 父元素不变矮)
  kids('w').slice(1).forEach(function (c) { c.parentNode.removeChild(c); });
  // 整批换成新的一批: 不是丢失
  kids('w2').forEach(function (c) { c.remove(); }); for (var i = 0; i < 12; i++) document.getElementById('w2').appendChild(mk('New', i));
  // 删到只剩 1 张, 1.5s 后数据到了自己渲染出 8 张: 放回的要撤
  kids('w3').slice(1).forEach(function (c) { c.remove(); });
  setTimeout(function () { for (var i = 0; i < 8; i++) document.getElementById('w3').appendChild(mk('Fresh', i)); }, 1500);
  // 只删两张: 不算成批丢失
  kids('w4')[3].remove(); kids('w4')[5].remove();
</script></body></html>"""


@unittest.skipUnless(chromium_available(), "需要 Playwright Chromium")
class ContentKeepGroup(unittest.TestCase):
    def test_sibling_group(self):
        from playwright.sync_api import sync_playwright
        cards = "".join(f'<div class="slide card" style="width:300px;height:400px;flex:none">Product {i}</div>' for i in range(12))
        texts = "[].map.call(document.getElementById('%s').children, function (c) { return c.textContent; }).join('|')"
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                page = b.new_page(viewport={"width": 390, "height": 844})
                page.set_content(LIST_PAGE.replace("GUARD", CONTENT_KEEP_GUARD_JS).replace("CARDS", cards))
                page.wait_for_timeout(1000)
                want = "|".join(f"Product {i}" for i in range(12))
                self.assertEqual(page.evaluate(texts % "w"), want, "成批被删的同类兄弟按原顺序放回")
                self.assertEqual(page.evaluate(texts % "w2"), "|".join(f"New {i}" for i in range(12)), "整批替换不放回")
                self.assertEqual(page.evaluate(texts % "w3"), want, "数据未到前先放回")
                self.assertEqual(page.evaluate("document.getElementById('w4').children.length"), 10, "零星删除不管")
                page.wait_for_timeout(2600)
                self.assertNotIn("Product 5", page.evaluate(texts % "w3"), "脚本自己渲染出列表后撤掉放回的")
                self.assertIn("Fresh 7", page.evaluate(texts % "w3"))
                self.assertEqual(page.evaluate(texts % "w"), want)
                off = CONTENT_KEEP_GUARD_JS.replace(",GRP=1;", ",GRP=0;")
                self.assertNotEqual(off, CONTENT_KEEP_GUARD_JS)
                page = b.new_page(viewport={"width": 390, "height": 844})
                page.set_content(LIST_PAGE.replace("GUARD", off).replace("CARDS", cards))
                page.wait_for_timeout(1000)
                self.assertEqual(page.evaluate(texts % "w"), "Product 0", "开关关闭时不放回")
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main()
