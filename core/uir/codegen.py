"""代码直写轨: 文字需求 → 模型直接写可运行网页 → 浏览器体检 → 多模态验收 → 定向修复。

为什么不走 UIR: UIR 是静态界面树(闭集角色, HTML 目标禁脚本), 表达不了动效 / canvas / 交互逻辑; 「按一句话
需求创作网页」需要的是完整的 HTML+CSS+JS。本轨只服务 kind=description 且 target=html_css, 其余输入/目标
仍走 UIR 适配器。

流程(与题材无关, 代码里不出现任何具体需求的词):
  plan     需求 → 任务书 JSON(理解 / 设计方向 / 技术选型 / 验收清单)
  write    任务书 → 单文件 HTML(CSS/JS 内联, 不引用外网)
  probe    core/uir/probe.py 真实运行取证(报错 / 帧差 / 交互 / 响应式)
  review   验收清单 + 关键帧截图 + 体检事实 → 逐条 yes/partial/no + 设计分 + 问题清单
  score    需求符合度 req-fit-v1: 0.6×清单达成率 + 0.2×运行健康度 + 0.2×设计质量, 硬证据优先于模型说法
  repair   未达标: 当前代码 + 问题清单(+截图) → 修复后的完整 HTML; 只保留分数更高的版本
"""
import json
import re
import shutil
import time
from pathlib import Path

from core.uir import llm, probe, settings

SCORING = "req-fit-v1"
ITEM_TYPES = ("content", "visual", "motion", "effect", "interaction")

PLAN_SYSTEM = (
    "你是资深产品设计师兼前端架构师。用户会用一两句话描述想要的网页, 你要把它扩写成一份任务书: "
    "工程师拿到能直接实现, 验收员拿到能逐条检查。只输出一个 JSON 对象, 不要 markdown, 不要解释。\n"
    "字段:\n"
    "- title: 页面标题(简短)\n"
    "- lang: 页面文字语言代码, 与用户需求所用语言一致(如 zh-CN / en)\n"
    "- summary: 一两句话说明你对需求的理解; 需求没说清的地方你做了什么假设\n"
    "- kind: 用几个字概括页面性质(由你自由判断, 例如动效展示、小游戏、工具、信息页、表单)\n"
    "- design: {mood: 气质关键词, palette: 3~6 个十六进制色值并注明各自用途, typography: 字体与字号层级, "
    "layout: 布局与构图}。用户明确说了的照做, 没说的由你给出有主张的设计, 避免紫色渐变、霓虹发光、emoji 装饰这类套路\n"
    "- concept: 一句话说明这个页面的核心创意 / 记忆点(打开页面第一眼让人记住的东西)\n"
    "- tech: 实现要点 —— 图形用 CSS / 内联 SVG / canvas 中的哪种, 动效怎么做, 有哪些状态和事件, 数据怎么存。"
    "具象图形(形状、图标、物件、角色)用内联 SVG path 或 canvas 路径精确绘制, 不用 CSS 伪元素拼接\n"
    "- checklist: 4~10 条验收项, 每条 {id: \"c1\"..., text, type, must}\n"
    "验收项规则:\n"
    "1. 每条必须是打开页面后能直接观察或操作验证的具体事实, 不写空话(「体验良好」这类不算)。\n"
    "2. type 取值: content(页面上有什么内容/元素) / visual(外观、形状、配色、布局) / "
    "motion(无需用户操作、页面加载后自动出现并且持续或循环进行的动效) / effect(一次性的短动效: 入场动画、"
    "状态切换时的过渡) / interaction(用户操作后页面的反应, 含操作之后才开始的计时与动画)。"
    "interaction 只用于要用户动手(点击、输入、按键、拖动)才能验证的条目; 靠打开页面验证不了的要求"
    "(跟随系统偏好设置、离线可用、数据持久化这类)归 content。\n"
    "3. must=true 只给用户原话明确要求或直接蕴含的条目; 你补充的质量要求 must=false。\n"
    "4. 用户原话里的每一个要求都要有对应条目, 不要遗漏, 也不要把与主题无关的大功能(账号、后端、多页面)加进来。\n"
    "5. 页面必须是不依赖后端和外网的单个网页, 不要规划需要服务器、登录或外部数据源的东西; 需要数据就用本地示例数据或浏览器本地存储。\n"
    "6. 体量与完成度: 用户只说一两句话时, 期待的是一个完整、精致、有记忆点的成品, 不是满足字面的最低实现。"
    "围绕主题定一个核心创意, 在不偏离题意的前提下补足: 与主题呼应的辅助层次(背景氛围、装饰元素、相关读数或文字), "
    "以及至少一个与主题相关的轻量交互反馈(展示类页面也要有, 但不能妨碍默认效果)。主体视觉要醒目, 在桌面视口里不能缩成一小块。"
    "这些补充写进 concept / design / tech, 并在验收清单里各有对应条目(must=false)。"
)

WRITE_SYSTEM = (
    "你是顶尖的前端工程师兼视觉设计师。根据用户需求和任务书, 写出一个完整、打开就能运行的网页。\n"
    "硬性要求:\n"
    "1. 只输出一个完整的 HTML 文档, 从 <!DOCTYPE html> 开始, 到 </html> 结束。不要任何解释文字, 不要 markdown 代码围栏。\n"
    "2. 所有 CSS 和 JavaScript 都内联在这个文件里。禁止引用任何外部资源: 不用 CDN, 不外链脚本、样式、字体、图片、音视频。"
    "需要图形就用 CSS、内联 SVG 或 canvas 自己画, 字体用系统字体栈。\n"
    "3. 真正实现任务书验收清单里的每一条。不要占位内容、TODO、「功能开发中」; 页面上出现的每个按钮和控件都必须有真实作用。\n"
    "4. 需求中的动效在页面加载后自动开始并持续可见, 幅度要足够明显, 不依赖用户操作(需求明确是操作触发的除外)。\n"
    "5. 自适应: 写 viewport meta; 从 360px 宽的手机到 1920px 宽的桌面都正常显示, 任何宽度下都不能出现横向滚动条; "
    "canvas 按容器尺寸和 devicePixelRatio 设置分辨率, 窗口尺寸变化时重新计算。\n"
    "6. 代码不能抛出运行时异常; 不使用 alert / confirm / prompt; 用到 localStorage 时做异常保护。\n"
    "7. 游戏或可操作的内容: 同时支持键盘和鼠标/触屏, 在页面上写明操作方式, 有开始、结束、重新开始的完整流程。\n"
    "8. 主体视觉要醒目(在桌面视口里不能缩成一小块); 具象图形(形状、图标、物件、角色)用内联 SVG path 或 canvas 路径"
    "精确绘制, 不要用 CSS 伪元素拼接, 写完核对形状完整、比例协调、该对称处对称。任务书里的 concept 和辅助层次都要做出来。\n"
    "9. 设计质量: 要有明确的视觉主张 —— 讲究的配色、字号层级、留白和细节(过渡、状态反馈), 与内容气质相称。"
    "不要默认的紫色渐变背景, 不要堆砌霓虹发光, 不要用 emoji 充当图标或装饰。界面文字使用任务书 lang 指定的语言。\n"
    "10. 代码紧凑, 不写冗长注释, 整个文件控制在能一次完整输出的长度内。"
)

_REPAIR_HEAD = (
    "\n\n你现在的任务是修复一个已有页面。会给你: 用户需求、任务书、当前页面的完整代码、在真实浏览器里运行后发现的问题清单"
    "(可能附一张当前页面截图)。逐条解决问题清单里的每一项, 保留已经正确的部分, 不要顺手删掉已有功能。"
)
REPAIR_SYSTEM = WRITE_SYSTEM + _REPAIR_HEAD + (
    "\n输出方式(二选一, 不要输出任何解释):\n"
    "A. 改动集中在局部时优先用修改块 —— 只输出若干个修改块, 每块严格按下面的格式:\n"
    "<<<<<<< SEARCH\n(当前代码里要被替换的原文: 逐字符照抄, 包括缩进和换行; 取连续的几行, 足以在文件中唯一定位)\n"
    "=======\n(替换后的内容; 留空表示删除)\n>>>>>>> REPLACE\n"
    "修改块之间互不重叠; 要新增内容, 就把插入位置相邻的原文放进 SEARCH, 在替换内容里带上原文和新增部分。\n"
    "B. 需要大面积重写时, 输出修复后的完整 HTML 文档(从 <!DOCTYPE html> 到 </html>)。"
)
REPAIR_FULL_SYSTEM = WRITE_SYSTEM + _REPAIR_HEAD + "输出修复后的完整 HTML 文档(不是补丁, 不是只输出改动的片段)。"

REVIEW_SYSTEM = (
    "你是严格的产品验收员。给你: 用户的原始需求、验收清单、页面在真实浏览器里运行的截图(不同时刻 / 不同宽度)、"
    "自动体检数据。请逐条判断验收项是否满足, 并评价设计质量。只输出一个 JSON 对象, 不要 markdown。\n"
    "规则:\n"
    "1. 只依据截图里实际看到的内容和体检数据下结论, 不要因为「应该是这样」就判满足; 每条 evidence 写你看到的具体依据。\n"
    "2. verdict 取值: yes(明确满足) / partial(部分满足, 或满足但有明显瑕疵) / no(有证据表明没有实现, 或与需求不符) / "
    "unsure(现有截图和体检数据既不能证明满足, 也不能证明不满足 —— 例如要走完多步操作才能看到的结果)。"
    "不要把 unsure 的情况判成 no; 也不要因为拿不准就判 yes。\n"
    "3. 体检数据是确定性测量, 与你的观感冲突时以体检数据为准(例如相邻帧变化占比为 0 说明画面没有在动)。\n"
    "4. 先核对主体图形有没有画对: 形状完整、比例协调、该对称处对称、没有错位或残缺(有「主体特写」图时以特写为准)。"
    "画错了, 描述该图形的条目判 no, 并作为 high 级 issue 写清哪里画错。\n"
    "5. 设计质量 design.score 为 0~10 的数, 用「资深设计师会交出的成品」做参照: 5 是能用但平庸; "
    "只有主体元素、缺少层次与细节的最低限度实现, 即使干净也不超过 6.5; 7 要求有完整的层次和打磨; "
    "8 以上要求有清晰的创意主张、层次丰富、细节到位, 达到专业作品水准。布局错乱、元素重叠、文字溢出或被裁切、"
    "对比度不足、大片无意义空白、手机宽度下不可用, 都要扣分并写进 issues。\n"
    "6. 前两张截图拍在页面刚加载时, 入场动画可能还没结束(元素半透明、位置未到位属正常); 对比度、布局、内容是否齐全"
    "以「静置后的整页」为准。整页图在截取前已自动逐屏滚动过一遍, 仍然空白的区域才是真的没有内容。\n"
    "7. 涉及时间、日期、计时的条目, 用体检数据里给出的截图时刻去核对画面上的读数。\n"
    "8. issues 只列真正需要修改的问题, 按严重程度从高到低, 每条写清现象(problem)和修改方向(fix), 最多 6 条; 没有问题给空数组。\n"
    "输出格式: {\"items\":[{\"id\":\"c1\",\"verdict\":\"yes|partial|no|unsure\",\"evidence\":\"...\"}],"
    "\"design\":{\"score\":0,\"comment\":\"...\"},"
    "\"issues\":[{\"severity\":\"high|mid|low\",\"problem\":\"...\",\"fix\":\"...\"}],\"summary\":\"一句话总评\"}"
)

_NO_VISION = set()        # (base_url, model): 实测不接受图片输入的上游, 之后直接走文本验收


class CodegenError(RuntimeError):
    """模型没有产出可用的网页。"""


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------
def fallback_plan(desc: str) -> dict:
    """规划调用失败时的退化任务书: 用户原话即任务, 验收只剩「实现了原话描述的内容」一条。"""
    d = (desc or "").strip()
    return {"title": d[:24] or "页面", "lang": "", "summary": d, "kind": "", "design": {}, "tech": "",
            "checklist": [{"id": "c1", "text": f"页面完整实现了用户描述的内容: {d[:300]}", "type": "content", "must": True}],
            "fallback": True}


def normalize_plan(obj: dict, desc: str) -> dict:
    if not isinstance(obj, dict):
        raise ValueError("任务书不是对象")
    items, seen = [], set()
    for raw in obj.get("checklist") or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        typ = str(raw.get("type") or "content").strip().lower()
        items.append({"id": f"c{len(items) + 1}", "text": text[:300],
                      "type": typ if typ in ITEM_TYPES else "content", "must": bool(raw.get("must"))})
        if len(items) >= 12:
            break
    if not items:
        raise ValueError("任务书没有验收清单")
    if not any(i["must"] for i in items):
        items[0]["must"] = True          # 至少有一条代表用户原话, 否则 must 封顶规则形同虚设
    design = obj.get("design") if isinstance(obj.get("design"), dict) else {}
    tech = obj.get("tech")
    return {"title": str(obj.get("title") or (desc or "")[:24] or "页面").strip()[:80],
            "lang": str(obj.get("lang") or "").strip()[:16],
            "summary": str(obj.get("summary") or "").strip()[:600],
            "kind": str(obj.get("kind") or "").strip()[:40],
            "concept": str(obj.get("concept") or "").strip()[:300],
            "design": design,
            "tech": (tech if isinstance(tech, str) else json.dumps(tech, ensure_ascii=False))[:1500] if tech else "",
            "checklist": items}


def _json_call(ai_cfg: dict, system: str, user: str, deadline: float, normalize, usage: dict = None,
               parts=(), temperature: float = 0.2, max_tokens: int = 4096):
    """结构化调用: JSON 模式 + 关思考; 输出不是合法 JSON 或规整失败时原样重试一次(升一点温度避免复现同一处坏输出)。
    实测小模型偶发吐出少逗号的 JSON, 一次重试比整步回落便宜得多。上游/网络错误不在这里重试(llm 层已处理)。"""
    last = None
    for attempt in range(2):
        if attempt and deadline - time.monotonic() < 8:
            break
        try:
            obj = llm.chat_json(ai_cfg, system, user, parts=parts, temperature=temperature + 0.25 * attempt,
                                max_tokens=max_tokens, deadline=deadline, thinking=False, usage_out=usage,
                                json_mode=True)
            return normalize(obj)
        except llm.DeadlineExceeded:
            raise
        except (ValueError, RuntimeError) as exc:
            if getattr(exc, "response", None) is not None or "网络错误" in str(exc):
                raise
            last = exc
    raise last or llm.DeadlineExceeded("调用墙钟上限已到")


def plan(ai_cfg: dict, desc: str, deadline: float, usage: dict = None) -> dict:
    return _json_call(ai_cfg, PLAN_SYSTEM, f"用户需求:\n{desc}", deadline, lambda o: normalize_plan(o, desc),
                      usage, temperature=0.3)


# ---------------------------------------------------------------------------
# write / repair
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*\n?(.*?)(?:```|\Z)", re.S)
_DOC_START_RE = re.compile(r"<!doctype\s+html|<html[\s>]", re.I)


def extract_html(text: str):
    """从模型输出里取出 HTML 文档 → (html, complete)。complete=False 表示没有 </html>(多半是被截断)。"""
    t = (text or "").strip()
    m = _DOC_START_RE.search(t)
    if not m:
        raise CodegenError(f"模型输出不是 HTML 文档: {t[:120]!r}")
    fence = t.rfind("```", 0, m.start())
    if fence >= 0:                         # 文档在代码围栏里: 只取围栏内的内容
        fm = _FENCE_RE.match(t, fence)
        if fm:
            t = fm.group(1).strip()
            m = _DOC_START_RE.search(t)
    lo = m.start()
    hi = t.lower().rfind("</html>")
    if hi > lo:
        return t[lo:hi + 7] + "\n", True
    return t[lo:], False


def _html_complete(text: str) -> bool:
    try:
        return extract_html(text)[1]
    except CodegenError:
        return False


def _brief_text(brief: dict) -> str:
    slim = {k: brief.get(k) for k in ("title", "lang", "summary", "kind", "concept", "design", "tech")}
    slim["checklist"] = [{"id": i["id"], "text": i["text"], "type": i["type"], "must": i["must"]}
                         for i in brief["checklist"]]
    return json.dumps(slim, ensure_ascii=False, indent=1)


def _code_call(ai_cfg, system, user, deadline, temperature, parts=()):
    out = llm.chat_text(ai_cfg, system, user, parts=parts, temperature=temperature,
                        max_tokens=settings.CODE_MAX_TOKENS, deadline=deadline,
                        thinking=settings.CODE_THINKING, complete=_html_complete)
    html, complete = extract_html(out["text"])
    if not complete:
        raise CodegenError(f"模型输出的网页不完整(没有 </html>, finish_reason={out.get('finish')}, "
                           f"max_tokens={out.get('budget')})")
    if len(html) > settings.CODE_MAX_HTML_CHARS:
        raise CodegenError(f"模型输出过长({len(html)} 字符)")
    return html, {"elapsed": out["elapsed"], "usage": out.get("usage") or {}}


def write_page(ai_cfg: dict, desc: str, brief: dict, deadline: float, note: str = ""):
    user = f"用户需求:\n{desc}\n\n任务书:\n{_brief_text(brief)}\n\n" + (f"{note}\n\n" if note else "") + "现在输出完整的 HTML 文档。"
    return _code_call(ai_cfg, WRITE_SYSTEM, user, deadline, 0.6)


def problem_list(defects: list, detail: dict, limit: int = 14) -> list:
    """修复轮的问题清单: 体检硬伤 → 未满足的验收项(must 在前) → 验收员提出的问题。"""
    out = [f"[硬伤] {d}" for d in defects]
    items = sorted((detail or {}).get("items") or [], key=lambda i: (not i.get("must"), i.get("verdict") != "no"))
    for it in items:
        if it.get("verdict") in ("no", "partial") and not it.get("soft"):
            why = it.get("gate") or it.get("evidence") or ""
            out.append(f"[验收项{'未满足' if it['verdict'] == 'no' else '部分满足'}{' · 用户明确要求' if it.get('must') else ''}] "
                       f"{it['text']}" + (f" —— {why}" if why else ""))
    for iss in (detail or {}).get("issues") or []:
        out.append(f"[{iss.get('severity', 'mid')}] {iss.get('problem', '')}" + (f" → {iss['fix']}" if iss.get("fix") else ""))
    return out[:limit]


def actionable(defects: list, detail: dict) -> bool:
    """有没有值得再开一轮修复的目标: 硬伤、未满足/部分满足(且不是「无法确认」)的验收项、中高严重度的问题。
    只剩低优先级意见或证据确认不了的条目时, 再修一轮多半只是换一种随机结果, 不值那几十秒。"""
    if defects:
        return True
    if any(i.get("verdict") in ("no", "partial") and not i.get("soft") for i in (detail or {}).get("items") or []):
        return True
    return any(i.get("severity") in ("high", "mid") for i in (detail or {}).get("issues") or [])


_PATCH_RE = re.compile(r"<{5,9} ?SEARCH[^\n]*\n(.*?)\n?={5,9}[^\n]*\n(.*?)\n?>{5,9} ?REPLACE", re.S)


def apply_patches(html: str, text: str):
    """把模型输出的 SEARCH/REPLACE 修改块应用到 html → (新 html, 成功块数, 失败块数)。

    先按原文精确匹配(只替换第一处); 不中再按「逐行去掉首尾空白后相等」匹配连续行 —— 模型抄原文时
    最常见的偏差就是缩进与行尾空白。两种都不中的块记为失败, 不猜。"""
    ok = bad = 0
    for m in _PATCH_RE.finditer(text or ""):
        search, repl = m.group(1), m.group(2)
        if not search.strip():
            bad += 1
            continue
        if search in html:
            html = html.replace(search, repl, 1)
            ok += 1
            continue
        want = [ln.strip() for ln in search.strip("\n").split("\n")]
        lines = html.split("\n")
        stripped = [ln.strip() for ln in lines]
        hit = next((i for i in range(len(lines) - len(want) + 1) if stripped[i:i + len(want)] == want), -1)
        if hit < 0:
            bad += 1
            continue
        lines[hit:hit + len(want)] = repl.split("\n")
        html = "\n".join(lines)
        ok += 1
    return html, ok, bad


def _repair_complete(text: str) -> bool:
    return _html_complete(text) or (text or "").rstrip().rstrip("`").rstrip().endswith("REPLACE")


def repair_page(ai_cfg: dict, desc: str, brief: dict, html: str, problems: list, deadline: float,
                shot: Path = None, stalled: bool = False):
    """修复 → (新 html, meta)。默认允许模型只输出局部修改块(整页重写一次要几十秒到两分钟, 改一处样式不值得);
    stalled(上一轮修复失败或没有改善)时要求整页重写并换思路。meta["mode"] = patch / full。"""
    user = (f"用户需求:\n{desc}\n\n任务书:\n{_brief_text(brief)}\n\n需要解决的问题(来自真实浏览器运行与验收):\n"
            + "\n".join(f"{i + 1}. {p}" for i, p in enumerate(problems))
            + ("\n\n上一次修复没有带来改善, 请换一种实现思路, 不要重复同样的改法。" if stalled else "")
            + f"\n\n当前页面完整代码:\n{html}\n\n现在输出"
            + ("修复后的完整 HTML 文档。" if stalled else "修改块(或修复后的完整 HTML 文档)。"))
    parts: list = []
    if shot is not None and Path(shot).is_file() and _vision_ok(ai_cfg):
        parts = [{"type": "text", "text": "当前页面在桌面视口的截图:"}, llm._image_part(Path(shot), 1024)]
    if stalled:
        new, meta = _code_call(ai_cfg, REPAIR_FULL_SYSTEM, user, deadline, 0.75, parts=parts)
        return new, dict(meta, mode="full")
    out = llm.chat_text(ai_cfg, REPAIR_SYSTEM, user, parts=parts, temperature=0.4, max_tokens=settings.CODE_MAX_TOKENS,
                        deadline=deadline, thinking=settings.CODE_THINKING, complete=_repair_complete)
    meta = {"elapsed": out["elapsed"], "usage": out.get("usage") or {}}
    text = out["text"]
    if _PATCH_RE.search(text):
        new, ok, bad = apply_patches(html, text)
        if not ok or new == html:
            raise CodegenError(f"修改块无法应用到当前代码(共 {ok + bad} 块, 全部未匹配或没有产生变化)")
        if len(new) > settings.CODE_MAX_HTML_CHARS:
            raise CodegenError(f"修复后页面过长({len(new)} 字符)")
        return new, dict(meta, mode="patch", patches=ok, patches_failed=bad)
    new, complete = extract_html(text)
    if not complete:
        raise CodegenError(f"修复输出不完整(既没有修改块也没有完整文档, finish_reason={out.get('finish')})")
    return new, dict(meta, mode="full")


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------
_VISION_OK = set()        # 自检通过的上游


def _vision_ok(ai_cfg: dict) -> bool:
    return (ai_cfg.get("base_url"), ai_cfg.get("model")) not in _NO_VISION


def vision_selftest(ai_cfg: dict, deadline: float = None) -> bool:
    """看图能力自检(每个上游每进程一次): 让模型读一张随机数字图。

    有的上游对图片输入不报错而是静默丢弃, 模型照样会「描述」一张它没看到的截图; 验收的可信度建立在模型真的
    看得见上, 所以先用一道有标准答案的题确认。读不对 → 记入 _NO_VISION, 之后走文本验收。"""
    key = (ai_cfg.get("base_url"), ai_cfg.get("model"))
    if key in _VISION_OK:
        return True
    if key in _NO_VISION:
        return False
    import random
    from PIL import Image, ImageDraw, ImageFont
    digits = "".join(random.choice("23456789") for _ in range(5))
    img = Image.new("RGB", (420, 160), "#ffffff")
    try:
        font = ImageFont.load_default(size=84)
    except TypeError:                      # 旧版 Pillow 没有可缩放的默认字体
        font = ImageFont.load_default()
    ImageDraw.Draw(img).text((36, 30), digits, fill="#111111", font=font)
    try:
        obj = llm.chat_json(ai_cfg, "你是读图助手。只输出 JSON。", "图片里写着一串数字, 原样读出来。输出 {\"digits\":\"...\"}",
                            parts=[llm._b64_png(img)], temperature=0, max_tokens=512, thinking=False,
                            deadline=deadline or time.monotonic() + 30)
        ok = re.sub(r"\D", "", str(obj.get("digits") or "")) == digits
    except Exception as exc:
        # 只有上游明确拒绝图片(4xx)才下「不可用」的结论; 超时 / 网络抖动 / 坏 JSON 不下结论, 下次再测
        if getattr(getattr(exc, "response", None), "status_code", None) in (400, 404, 415, 422):
            _NO_VISION.add(key)
        return False
    (_VISION_OK if ok else _NO_VISION).add(key)
    return ok


def probe_digest(rep: dict) -> str:
    """体检数据 → 给模型看的事实摘要(中文短句)。"""
    mot, lay, mob = rep.get("motion") or {}, rep.get("layout") or {}, rep.get("mobile") or {}
    lines = ["运行时报错: " + ("; ".join(rep["errors"][:5]) if rep.get("errors") else "无"),
             "外网资源请求: " + (f"{len(rep['external'])} 个(已被拦截)" if rep.get("external") else "无")]
    if rep.get("frozen"):
        lines.append("页面主线程卡死或体检中断")
    if rep.get("blank"):
        lines.append("首帧是空白页")
    if mot:
        gaps = "/".join(str(g) for g in settings.PROBE_FRAME_GAPS_MS)
        lines.append(f"动效: 间隔 {gaps} 毫秒连拍 {len(mot.get('frame_diffs', [])) + 1} 帧, 相邻帧变化像素占比 "
                     f"{mot.get('frame_diffs')}" + (f", 变化集中在视口的归一化区域 {mot['bbox']}(x0,y0,x1,y1)" if mot.get("bbox") else "")
                     + f"; 运行中的 CSS/WAAPI 动画 {mot.get('running_animations')} 个; requestAnimationFrame 约 "
                     f"{mot.get('raf_per_s')} 次/秒; canvas {mot.get('canvas')} 个, svg {mot.get('svg')} 个")
    eff = rep.get("effects")
    if eff:
        lines.append(f"动效事件(从加载到全部操作结束): 启动过关键帧动画 {eff.get('animations', 0)} 次"
                     + (f"(其中加载阶段 {eff.get('at_load', 0)} 次; 动画名: {', '.join(eff['names'])})" if eff.get("names") else "")
                     + f", 发生过渡 {eff.get('transitions', 0)} 次。一次性的入场动画在连拍开始前可能已经结束, 以这里的事件计数为准")
    shot_at = rep.get("shot_at") or {}
    if shot_at.get("f0"):
        lines.append(f"截图时刻(运行页面的浏览器本地时间): 加载后首帧 {shot_at['f0']}"
                     + (f"; 操作后的截图 {shot_at['after']}" if shot_at.get("after") else ""))
    inter = rep.get("interactions") or []
    if inter:
        lines.append("交互探测(自动逐个操作):")
        word = {True: "有反应", False: "页面内容和画面都没有变化", None: "无法自动判定(页面本身在持续变化, 请看操作后的截图)"}
        for i in inter:
            what = f"{i.get('tag') or ''}「{i.get('label') or ''}」".strip()
            if i["kind"] == "field":
                lines.append(f"  - 填写 {what}")
            else:
                lines.append(f"  - {i['action']} {what if i['kind'] in ('click', 'canvas') else i['label']}: {word[i.get('responded')]}"
                             + (", 文字变化" if i.get("text_changed") else "")
                             + (", 引出了动画" if i.get("animated") else "")
                             + ((", 之后 1 秒多画面仍在自己变化"
                                 + (f"(新出现: {' / '.join(i['then_added'])})" if i.get("then_added") else ""))
                                if i.get("sustained") else "")
                             + (f", 报错 {i['new_errors']}" if i.get("new_errors") else ""))
        post = rep.get("post")
        if post:
            gaps = "/".join(str(g) for g in settings.PROBE_POST_GAPS_MS)
            lines.append(f"全部操作完之后: 间隔 {gaps} 毫秒再连拍, 相邻帧变化像素占比 {post.get('frame_diffs')}"
                         f"({'画面仍在变化' if post.get('moving') else '画面静止'}); 运行中的动画 {post.get('running_animations')} 个"
                         + (f"; 与操作前相比新出现的文字: {' / '.join(post['text_added'])}" if post.get("text_added") else "")
                         + (f"; 消失的文字: {' / '.join(post['text_removed'])}" if post.get("text_removed") else ""))
    else:
        lines.append("交互探测: 页面上没有找到按钮、输入框、可点击区域, 也没有注册键盘/指针监听")
    if rep.get("listeners"):
        lines.append("页面注册的事件监听: " + ", ".join(rep["listeners"]))
    lines.append(f"布局: 桌面视口 {rep.get('viewport', {}).get('width')}×{rep.get('viewport', {}).get('height')}px "
                 f"横向溢出 {'是' if lay.get('overflow_x') else '否'}; "
                 f"手机 390px 横向溢出 {'是' if mob.get('overflow_x') else '否'}; 页面总高 {lay.get('doc_h')}px")
    text = (rep.get("text") or "").strip()
    lines.append("页面可见文字(节选): " + (re.sub(r"\s+", " ", text)[:700] if text else "(没有文字)"))
    return "\n".join(lines)


def subject_crop(src: Path, out: Path, max_frac: float = 0.45):
    """从截图里裁出「画面主体」并放大 → out 路径; 主体已经占满大半画面(信息页、整屏画布)时返回 None。

    缩到 1024 宽送审时, 居中的小图形只剩一两百像素, 验收模型看不出形状画错(残缺、错位、不对称)。
    做法与题材无关: 以四边像素的中位色作背景, 与背景色差明显的像素算前景, 取前景坐标的 2%~98% 分位作外接框
    (忽略零星粒子/噪点), 外扩 15% 后裁剪, 长边放大到约 900px。"""
    import numpy as np
    from PIL import Image
    try:
        with Image.open(src) as im:
            rgb = im.convert("RGB")
        w, h = rgb.size
        small = np.asarray(rgb.resize((max(1, w // 4), max(1, h // 4))), dtype=np.int16)
        border = np.concatenate([small[0], small[-1], small[:, 0], small[:, -1]])
        bg = np.median(border, axis=0)
        mask = np.abs(small - bg).max(axis=2) > 40
        ys, xs = np.nonzero(mask)
        if len(xs) < 50:
            return None
        x0, x1 = np.percentile(xs, [2, 98]) * 4
        y0, y1 = np.percentile(ys, [2, 98]) * 4
        bw, bh = x1 - x0, y1 - y0
        if bw < 40 or bh < 40 or (bw * bh) / (w * h) > max_frac:
            return None
        px, py = bw * 0.15, bh * 0.15
        box = (int(max(0, x0 - px)), int(max(0, y0 - py)), int(min(w, x1 + px)), int(min(h, y1 + py)))
        crop = rgb.crop(box)
        k = 900 / max(crop.size)
        if k > 1:
            crop = crop.resize((round(crop.size[0] * k), round(crop.size[1] * k)), Image.LANCZOS)
        crop.save(out)
        return out
    except Exception:
        return None


def _review_parts(rep: dict) -> list:
    shots, parts = rep.get("shots") or {}, []
    gap_s = sum(settings.PROBE_FRAME_GAPS_MS) / 1000.0
    n = 0

    def add(label, part):
        nonlocal n
        n += 1
        parts.append({"type": "text", "text": f"图 {n}: {label}"})
        parts.append(part)

    def ok(key):
        return bool(shots.get(key)) and Path(shots[key]).is_file()
    if ok("f0"):
        add("桌面视口, 页面加载后约 0.5 秒(入场动画可能尚未结束)", llm._image_part(Path(shots["f0"]), 1024))
    if ok("peak"):
        add(f"同一视口, 之后 {gap_s:.1f} 秒内与上一张差异最大的一帧(对比两张可判断是否在动)",
            llm._image_part(Path(shots["peak"]), 1024))
    base = shots.get("peak") if ok("peak") else (shots.get("f0") if ok("f0") else None)
    if base:
        crop = subject_crop(Path(base), Path(base).with_name("subject.png"))
        if crop:
            add("主体特写: 从上一张截图里裁出画面主体并放大, 用来核对主体图形画得对不对", llm._image_part(crop, 900))
    if ok("full"):
        tiles = llm.image_parts(Path(shots["full"]), 1024, max_tiles=3, tile_h=1700)[0]
        for k, part in enumerate(tiles):
            add("静置后的整页(截取前已逐屏滚动到底再回到顶部)"
                + (f", 从上到下第 {k + 1}/{len(tiles)} 段" if len(tiles) > 1 else ""), part)
    if ok("after") and rep.get("interactions"):
        add("自动操作完页面上的可交互元素之后" + ("又过了约 1.6 秒" if rep.get("post") else ""),
            llm._image_part(Path(shots["after"]), 1024))
    if ok("mobile"):
        add("手机宽度 390px 下的首屏", llm._image_part(Path(shots["mobile"]), 390))
    return parts


def normalize_review(obj: dict, brief: dict) -> dict:
    ids = {i["id"] for i in brief["checklist"]}
    items = {}
    for raw in (obj.get("items") or []) if isinstance(obj, dict) else []:
        if isinstance(raw, dict) and str(raw.get("id")) in ids:
            v = str(raw.get("verdict") or "").strip().lower()
            if v in ("yes", "partial", "no", "unsure"):
                items[str(raw["id"])] = {"verdict": v, "evidence": str(raw.get("evidence") or "").strip()[:300]}
    if not items:
        raise ValueError("验收结果没有任何有效条目")
    design = obj.get("design") if isinstance(obj.get("design"), dict) else {}
    try:
        dscore = max(0.0, min(10.0, float(design.get("score"))))
    except (TypeError, ValueError):
        dscore = None
    issues = []
    for raw in obj.get("issues") or []:
        if isinstance(raw, dict) and str(raw.get("problem") or "").strip():
            sev = str(raw.get("severity") or "mid").lower()
            issues.append({"severity": sev if sev in ("high", "mid", "low") else "mid",
                           "problem": str(raw["problem"]).strip()[:300], "fix": str(raw.get("fix") or "").strip()[:300]})
    return {"items": items, "design_score": dscore, "design_comment": str(design.get("comment") or "").strip()[:400],
            "issues": issues[:6], "summary": str(obj.get("summary") or "").strip()[:400]}


def review(ai_cfg: dict, desc: str, brief: dict, rep: dict, html: str, deadline: float, usage: dict = None) -> dict:
    """多模态验收; 上游不接受图片时降级为「源码 + 体检数据」的文本验收。返回 normalize_review 结果 + mode。"""
    head = (f"用户原始需求:\n{desc}\n\n验收清单:\n"
            + "\n".join(f"- {i['id']} [{i['type']}{' · 用户明确要求' if i['must'] else ''}] {i['text']}" for i in brief["checklist"])
            + f"\n\n自动体检数据:\n{probe_digest(rep)}\n")
    norm = lambda o: normalize_review(o, brief)  # noqa: E731
    why = ""
    if not _vision_ok(ai_cfg):
        why = "模型不接受图片输入"
    elif not vision_selftest(ai_cfg, deadline):
        why = "模型未通过看图自检" if not _vision_ok(ai_cfg) else "看图自检未完成(超时或上游异常)"
    else:
        parts = _review_parts(rep)
        try:
            out = _json_call(ai_cfg, REVIEW_SYSTEM, head + "\n下面是页面截图, 请据此验收。", deadline, norm, usage,
                             parts=parts, temperature=0.1)
            return dict(out, mode="vision", fallback="")
        except llm.DeadlineExceeded:
            raise
        except Exception as exc:
            # 4xx = 上游不接受图片输入: 记住, 之后直接文本验收; 其它错误只降级这一次
            if getattr(getattr(exc, "response", None), "status_code", None) in (400, 404, 415, 422):
                _NO_VISION.add((ai_cfg.get("base_url"), ai_cfg.get("model")))
            why = f"看图验收调用失败({type(exc).__name__}: {str(exc)[:100]})"
    code_text = html if len(html) <= 60000 else html[:45000] + "\n<!-- …中间省略… -->\n" + html[-12000:]
    out = _json_call(ai_cfg, REVIEW_SYSTEM, head + "\n本次没有截图, 只能依据页面源码和体检数据判断; 外观类条目没有把握时判 partial。"
                     f"\n\n页面源码:\n{code_text}", deadline, norm, usage, temperature=0.1)
    return dict(out, mode="text", fallback=why)


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------
_VALUE = {"yes": 1.0, "partial": 0.5, "no": 0.0}


def score(brief: dict, rev: dict, rep: dict) -> dict:
    """需求符合度 req-fit-v1。rev=None 表示未经模型验收(只有体检硬证据)。

    硬证据闸门(只在体检能证伪时覆盖模型说法): motion 项在「空闲和操作后都确定没在动」时判 no, 有动画但连拍
    无可见变化时最多 partial; effect 项在全程没有启动过任何动画或过渡时判 no; interaction 项在页面没有任何可交互元素、或所有可交互元素都没反应时判 no;
    文本验收(没看图)时 visual 项最多 partial。空闲静止但操作后会动的页面体检证伪不了, 交给验收模型按截图判断。
    封顶: 运行报错/空白/卡死 ≤40; 任一用户明确要求的条目为 no ≤60; 未经模型验收 ≤70。"""
    f = probe.facts(rep)
    verdicts = (rev or {}).get("items") or {}
    text_only = (rev or {}).get("mode") == "text"
    rows, wsum, vsum, must_no = [], 0.0, 0.0, False
    for it in brief["checklist"]:
        got = verdicts.get(it["id"]) or {}
        verdict, gate = got.get("verdict"), None
        soft = False        # soft: 判不满分是因为「现有证据确认不了」, 不是页面有已知缺陷 —— 不拿去让模型修
        if verdict == "unsure":
            verdict, gate, soft = "partial", "现有截图和体检数据无法确认", True
        if it["type"] == "motion":
            if f["motion_none"]:
                verdict, gate, soft = "no", "体检: 空闲时连拍各帧完全相同、没有运行中的动画, 逐个操作之后也没有动起来", False
            elif not f["motion_seen"] and not f["motion_after"] and verdict == "yes":
                verdict, gate, soft = "partial", "体检: 有动画在运行, 但连拍画面没有可见变化", True
        elif it["type"] == "effect":
            if f["effects_none"]:
                verdict, gate, soft = "no", "体检: 从加载到全部操作结束, 没有启动过任何动画或过渡", False
        elif it["type"] == "interaction":
            if f["interactive_n"] == 0:
                verdict, gate, soft = "no", "体检: 页面上没有任何可交互元素, 也没有键盘/指针监听", False
            elif f["dead_n"] and not f["responded_n"] and not f["unknown_n"]:
                verdict, gate, soft = "no", "体检: 所有可交互元素操作后都没有反应", False
        elif it["type"] == "visual" and text_only and verdict == "yes":
            verdict, gate, soft = "partial", "本轮没有看图验收, 外观类条目无法确认", True
        value = _VALUE.get(verdict, 0.5 if rev is None else 0.0)
        w = 2.0 if it["must"] else 1.0
        wsum += w
        vsum += w * value
        must_no = must_no or (it["must"] and verdict == "no")
        rows.append(dict(it, verdict=verdict or "unknown", evidence=got.get("evidence", ""), gate=gate, soft=soft))
    coverage = vsum / wsum if wsum else 0.0
    alive = not f["frozen"]
    health = (0.25 * (alive and not f["errors"]) + 0.25 * (alive and not f["blank"]) + 0.25 * (not f["external"])
              + 0.125 * (not f["overflow_desktop"]) + 0.125 * (not f["overflow_mobile"]))
    dscore = (rev or {}).get("design_score")
    design = 0.5 if dscore is None else dscore / 10.0
    raw = 100.0 * (0.6 * coverage + 0.2 * health + 0.2 * design)
    caps = []
    if f["errors"] or f["blank"] or f["frozen"]:
        caps.append(("运行报错 / 空白 / 卡死", 40.0))
    if must_no:
        caps.append(("有用户明确要求的条目未满足", 60.0))
    if rev is None:
        caps.append(("未经模型验收", 70.0))
    final = min([raw] + [c for _, c in caps])
    return {"score": round(final, 1), "scoring": SCORING, "raw": round(raw, 1),
            "coverage": round(coverage, 3), "health": round(health, 3), "design": round(design, 3),
            "caps": [{"reason": r, "max": c} for r, c in caps if raw > c],
            "items": rows, "review_mode": (rev or {}).get("mode", "none"),
            "summary": (rev or {}).get("summary", ""), "design_comment": (rev or {}).get("design_comment", ""),
            "issues": (rev or {}).get("issues") or [], "facts": f, "warnings": []}


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def _tokens(usage: dict) -> dict:
    return {"in": int(usage.get("prompt_tokens") or 0), "out": int(usage.get("completion_tokens") or 0)}


def run(desc: str, ws: Path, pool, ai_cfg: dict, judge_cfg: dict, viewport: dict, log, stage,
        deadline: float, target_score: float, max_rounds: int) -> dict:
    """跑完一次代码直写生成。产物写入 ws(index.html / cloned_preview.png / cloned_text.txt / brief.json / gen_probe/),
    返回 {"brief","rounds","calls","best":{"score","detail","round"}}; 没有任何可用产物时抛 CodegenError。"""
    left = lambda: deadline - time.monotonic()  # noqa: E731
    wall = lambda cap: time.monotonic() + max(5.0, min(cap, left()))  # noqa: E731
    calls = []

    def note(step, t0, usage=None, **extra):
        rec = dict({"step": step, "elapsed": round(time.monotonic() - t0, 1)}, **_tokens(usage or {}), **extra)
        calls.append(rec)
        return rec

    # ---- 规划 ----
    stage("理解需求", 8)
    t0, usage = time.monotonic(), {}
    try:
        brief = plan(ai_cfg, desc, wall(settings.CODE_PLAN_WALL), usage)
        note("plan", t0, usage)
    except Exception as exc:
        brief = fallback_plan(desc)
        note("plan", t0, usage, error=f"{type(exc).__name__}: {str(exc)[:160]}")
        log("GEN", f"需求规划失败, 以用户原话为任务书继续: {type(exc).__name__}: {str(exc)[:160]}")
    (ws / "brief.json").write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    if brief.get("summary"):
        log("GEN", f"需求理解: {brief['summary']}")
    log("GEN", f"验收清单 {len(brief['checklist'])} 条: " + "; ".join(
        f"{i['id']}[{i['type']}{'*' if i['must'] else ''}] {i['text'][:40]}" for i in brief["checklist"]))

    best: dict = {}
    rounds: list = []
    html, stalled = "", False
    probe_dir = ws / "gen_probe"
    for rnd in range(1, max(1, max_rounds) + 1):
        t_r = time.monotonic()
        action = "write" if not best else "repair"
        if action == "repair" and left() < settings.CODE_MIN_REPAIR_LEFT:
            log("GEN", "剩余时间不足以再修一轮, 停止")
            break
        if left() <= 5:
            break
        stage("编写页面" if action == "write" else f"修复第 {rnd - 1} 轮", min(80, 15 + rnd * 18))
        t0 = time.monotonic()
        try:
            if action == "write":
                html, meta = write_page(ai_cfg, desc, brief, wall(settings.CODE_WRITE_WALL),
                                        note="上一次输出不完整, 请精简实现, 确保一次输出完整文档。" if rounds else "")
            else:
                problems = problem_list(best["defects"], best["detail"])
                log("GEN", f"修复目标 {len(problems)} 项: " + " | ".join(p[:60] for p in problems[:4]))
                html, meta = repair_page(ai_cfg, desc, brief, best["html"], problems, wall(settings.CODE_WRITE_WALL),
                                         shot=(best["rep"].get("shots") or {}).get("f0"), stalled=stalled)
            note(action, t0, meta["usage"], **({"mode": meta["mode"]} if meta.get("mode") else {}))
            if meta.get("mode") == "patch":
                log("GEN", f"局部修改: 应用 {meta['patches']} 块" + (f", {meta['patches_failed']} 块未匹配" if meta.get("patches_failed") else ""))
        except Exception as exc:
            note(action, t0, error=f"{type(exc).__name__}: {str(exc)[:200]}")
            log("GEN", f"第 {rnd} 轮{'写页面' if action == 'write' else '修复'}失败: {type(exc).__name__}: {str(exc)[:200]}")
            rounds.append({"round": rnd, "action": action, "score": None, "kept": False,
                           "error": f"{type(exc).__name__}: {str(exc)[:200]}", "elapsed": round(time.monotonic() - t_r, 1)})
            if isinstance(exc, llm.DeadlineExceeded) and left() <= 5:
                break
            stalled = action == "repair"
            continue
        log("GEN", f"第 {rnd} 轮{'写出页面' if action == 'write' else '修复完成'}: {len(html)} 字符, {meta['elapsed']}s, "
                   f"输出 {_tokens(meta['usage'])['out']} tokens")
        # ---- 体检 ----
        stage("浏览器体检", min(85, 22 + rnd * 18))
        (ws / "index.html").write_text(html, encoding="utf-8")
        rdir = probe_dir / f"r{rnd}"
        rep = probe.inspect_page(pool, ws / "index.html", rdir, viewport, full_png=rdir / "full.png", log=log)
        defects = probe.hard_defects(rep)
        f = probe.facts(rep)
        mot = rep.get("motion") or {}
        log("PROBE", f"体检 {rep['elapsed']}s: 报错 {len(rep['errors'])}, 外网请求 {len(rep['external'])}, "
                     f"帧差 {mot.get('max_frac')}, 动画 {mot.get('running_animations')}, "
                     f"交互 {f['responded_n']} 有反应/{f['dead_n']} 无反应/{f['unknown_n']} 待定, 硬伤 {len(defects)}")
        # ---- 验收 ----
        rev, blocking = None, f["errors"] or f["blank"] or f["frozen"]
        if blocking and rnd < max_rounds and left() > settings.CODE_MIN_REPAIR_LEFT:
            log("GEN", "存在运行硬伤, 跳过模型验收直接进入修复")
        elif left() > 8:
            stage("对照需求验收", min(88, 28 + rnd * 18))
            t0, usage = time.monotonic(), {}
            try:
                rev = review(judge_cfg or ai_cfg, desc, brief, rep, html, wall(settings.CODE_REVIEW_WALL), usage)
                note("review", t0, usage, mode=rev["mode"])
                if rev.get("fallback"):
                    log("GEN", f"本轮改用文本验收: {rev['fallback']}")
            except Exception as exc:
                note("review", t0, usage, error=f"{type(exc).__name__}: {str(exc)[:160]}")
                log("GEN", f"模型验收失败, 仅按体检硬证据计分: {type(exc).__name__}: {str(exc)[:160]}")
        detail = score(brief, rev, rep)
        yes = sum(1 for i in detail["items"] if i["verdict"] == "yes")
        log("SIMILARITY", f"第 {rnd} 轮需求符合度 {detail['score']} (清单 {yes}/{len(detail['items'])} 满足, "
                          f"达成率 {detail['coverage']}, 健康度 {detail['health']}, 设计 {detail['design']}, "
                          f"验收方式 {detail['review_mode']})"
                          + ("; 封顶: " + ", ".join(c["reason"] for c in detail["caps"]) if detail["caps"] else ""))
        improved = not best or detail["score"] > best["detail"]["score"]
        rounds.append({"round": rnd, "action": action, "score": detail["score"], "coverage": detail["coverage"],
                       "health": detail["health"], "design": detail["design"], "review_mode": detail["review_mode"],
                       "defects": len(defects), "html_chars": len(html), "kept": improved,
                       "elapsed": round(time.monotonic() - t_r, 1)})
        if improved:
            best = {"html": html, "detail": detail, "rep": rep, "defects": defects, "round": rnd, "dir": rdir}
            stalled = False
        else:
            stalled = True
            log("GEN", f"第 {rnd} 轮未提升({best['detail']['score']} → {detail['score']}), 保留第 {best['round']} 轮版本")
        if best["detail"]["score"] >= target_score and not best["defects"]:
            log("GEN", f"需求符合度达到 {target_score} 且无硬伤, 停止迭代")
            break
        if best["detail"]["review_mode"] != "none" and not actionable(best["defects"], best["detail"]):
            log("GEN", "没有可以明确修复的问题了(剩下的是证据确认不了的条目或低优先级意见), 停止迭代")
            break

    if not best:
        errs = [r.get("error") for r in rounds if r.get("error")]
        raise CodegenError("模型没有产出可用的网页" + (f": {errs[-1]}" if errs else ""))
    # ---- 落盘最优版本 ----
    (ws / "index.html").write_text(best["html"], encoding="utf-8")
    full = best["dir"] / "full.png"
    if full.is_file():
        (ws / "cloned_preview.png").write_bytes(full.read_bytes())
    (ws / "cloned_text.txt").write_text(best["rep"].get("text") or "", encoding="utf-8")
    # 证据留在工作区根目录(平铺文件): 打包器会把工作区里的子目录整棵拷进交付工程, 体检截图不该进交付物
    slim = {k: v for k, v in best["rep"].items() if k not in ("text", "shots")}
    slim["shots"] = {}
    for key, path in (best["rep"].get("shots") or {}).items():
        if key != "full" and Path(path).is_file():        # 整页图已另存为 cloned_preview.png
            (ws / f"gen_{key}.png").write_bytes(Path(path).read_bytes())
            slim["shots"][key] = f"gen_{key}.png"
    (ws / "gen_probe.json").write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(probe_dir, ignore_errors=True)
    return {"brief": brief, "rounds": rounds, "calls": calls,
            "best": {"score": best["detail"]["score"], "detail": best["detail"], "round": best["round"],
                     "defects": best["defects"]}}
