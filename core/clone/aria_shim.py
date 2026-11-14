"""克隆页通用交互兜底层(ARIA 语义驱动)。

站点自己的应用脚本被水合剥离 / 静态版交付去掉之后, 菜单、页签、折叠面板就点不动了; 这些控件大多按 WAI-ARIA
写明了自己是什么、控制谁(aria-expanded / aria-controls / role=tab / aria-selected)。兜底层只按这些标准语义行事:

- 点击后先等一小会儿: 如果页面自己的脚本已经响应(状态或结构变了), 兜底层什么都不做 —— 不会和仍然可用的站点脚本打架;
- 没有响应时:
  * aria-expanded 控件: 翻转 aria-expanded, 显示/隐藏它 aria-controls 指向的元素; 没写 aria-controls 时取紧跟着的隐藏兄弟元素;
  * role=tab: 选中当前页签, 显示它控制的面板, 隐藏同一组其它页签的面板;
- 兜底打开的弹层: 点外面或按 Esc 关闭。

不针对任何具体网站; 不改写站点自己的类名; 只动 aria 属性、hidden 属性和必要的内联显示样式。
"""

ARIA_SHIM_JS = r"""<script data-clone-preview="aria-shim">(function(){try{
if(window.__cloneAriaShim)return;window.__cloneAriaShim=1;
var WAIT=80,opened=[];
function hiddenEl(e){if(!e)return false;var s=getComputedStyle(e);return e.hidden||s.display==='none'||s.visibility==='hidden'||parseFloat(s.opacity)===0||e.getAttribute('aria-hidden')==='true';}
function show(e){if(!e)return;e.__shimPrev={h:e.hidden,d:e.style.display,v:e.style.visibility,o:e.style.opacity,a:e.getAttribute('aria-hidden')};
 e.hidden=false;if(e.getAttribute('aria-hidden')==='true')e.setAttribute('aria-hidden','false');
 if(getComputedStyle(e).display==='none')e.style.display='block';
 if(getComputedStyle(e).visibility==='hidden')e.style.visibility='visible';
 if(parseFloat(getComputedStyle(e).opacity)===0)e.style.opacity='1';}
function hide(e){if(!e)return;var p=e.__shimPrev;if(p){e.hidden=p.h;e.style.display=p.d;e.style.visibility=p.v;e.style.opacity=p.o;
 if(p.a===null)e.removeAttribute('aria-hidden');else e.setAttribute('aria-hidden',p.a);e.__shimPrev=null;}else{e.hidden=true;}}
function targetOf(c){var id=c.getAttribute('aria-controls');if(id){var t=document.getElementById(id.split(/\s+/)[0]);if(t)return t;}
 var n=c.nextElementSibling;if(n&&hiddenEl(n))return n;
 if(c.parentElement){n=c.parentElement.nextElementSibling;if(n&&hiddenEl(n))return n;
  var k=c.parentElement.querySelector(':scope > [hidden], :scope > [aria-hidden="true"]');if(k&&k!==c)return k;}
 return null;}
function snap(c){var t=targetOf(c);return {ex:c.getAttribute('aria-expanded'),sel:c.getAttribute('aria-selected'),
 vis:t?!hiddenEl(t):null,cls:c.className&&c.className.baseVal===undefined?c.className:'',muts:window.__shimMuts||0};}
var mo=new MutationObserver(function(l){window.__shimMuts=(window.__shimMuts||0)+l.length;});
function arm(){try{mo.observe(document.documentElement,{subtree:true,childList:true,attributes:true});}catch(e){}}
if(document.documentElement)arm();
function closeAll(except){opened=opened.filter(function(o){if(o.c===except)return true;
 o.c.setAttribute('aria-expanded','false');hide(o.t);return false;});}
function tab(c){var list=c.closest('[role=tablist]')||c.parentElement;if(!list)return;
 var tabs=list.querySelectorAll('[role=tab]');for(var i=0;i<tabs.length;i++){var on=tabs[i]===c;tabs[i].setAttribute('aria-selected',on?'true':'false');
  var id=tabs[i].getAttribute('aria-controls'),p=id&&document.getElementById(id);if(p){if(on)show(p);else{p.hidden=true;}}}}
function toggle(c){var t=targetOf(c),open=c.getAttribute('aria-expanded')!=='true';
 c.setAttribute('aria-expanded',open?'true':'false');
 if(t){if(open){show(t);closeAll(c);opened.push({c:c,t:t});}else{hide(t);opened=opened.filter(function(o){return o.c!==c;});}}}
document.addEventListener('click',function(ev){
 var c=ev.target&&ev.target.closest&&ev.target.closest('[aria-expanded],[role=tab]');
 if(!c){if(opened.length&&!opened.some(function(o){return o.t.contains(ev.target);}))closeAll(null);return;}
 if(c.closest('a[href]')&&!/^#|^javascript:/i.test(c.closest('a[href]').getAttribute('href')||''))return;
 var before=snap(c);
 setTimeout(function(){var after=snap(c);
  var reacted=after.ex!==before.ex||after.sel!==before.sel||after.vis!==before.vis||after.cls!==before.cls||(after.muts-before.muts)>3;
  if(reacted)return;
  if(c.getAttribute('role')==='tab')tab(c);else toggle(c);},WAIT);
},false);
document.addEventListener('keydown',function(ev){if(ev.key==='Escape'&&opened.length)closeAll(null);});
}catch(e){}})();</script>"""
