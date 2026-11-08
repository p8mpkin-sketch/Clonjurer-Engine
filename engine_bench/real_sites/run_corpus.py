"""真实站点语料跑分: 向运行中的平台实例提交语料批次, 轮询到完成, 产出 JSON + Markdown 报告。

用法(在平台实例可达的机器上执行):
  python3 engine_bench/real_sites/run_corpus.py --base http://127.0.0.1:8897
  python3 engine_bench/real_sites/run_corpus.py --base http://127.0.0.1:8897 --tag cn      # 只跑国内直连站
  python3 engine_bench/real_sites/run_corpus.py --base ... --sites https://a.com,https://b.com
  python3 engine_bench/real_sites/run_corpus.py --base ... --user admin --password '...'   # 实例已开鉴权时

产物(engine_bench/real_sites/runs/):
  run_<时间戳>.json   逐站明细: 检出模式/期望模式/双轨分数/阶段耗时/错误
  REPORT_<时间戳>.md  汇总表(按分数升序, 低分站在最上面), 与上一轮同站对比的增减
退出码: 0 全部完成; 2 有站点失败或超时(报告照常产出)。

度量口径:
  - 还原度 = 任务 similarity_score(双轨取优); 同时记录 interactive/fidelity 原值;
  - 识别命中 = 检出模式 == corpus.json 的 expect(expect 是先验假设, 核对后可修正语料);
  - 耗时 = 引擎 stats.stage_times 总和(新引擎), 否则 created_at→updated_at 差;
  - 轮询期每次 stage 变化也记时间线(老引擎没有 stage_times 时的兜底)。
不做 per-site 特判: 低分站只能归因到模式类, 修复必须对整类生效。
"""
import argparse
import base64
import io
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib import request as urlreq, error as urlerr
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
CORPUS = HERE / "corpus.json"


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.cookie = None

    def call(self, method: str, path: str, body=None, query=None):
        url = self.base + path + (("?" + urlencode(query)) if query else "")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urlreq.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urlreq.urlopen(req, timeout=30) as resp:
                set_cookie = resp.headers.get("Set-Cookie")
                if set_cookie:
                    self.cookie = set_cookie.split(";", 1)[0]
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urlerr.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            return exc.code, payload

    def login_if_needed(self, user, password):
        code, state = self.call("GET", "/api/auth/state")
        if code != 200:
            sys.exit(f"实例不可达或 /api/auth/state 异常: HTTP {code} {state}")
        if not state.get("required"):
            return
        if not user:
            sys.exit("实例已开启鉴权, 请带 --user/--password")
        code, payload = self.call("POST", "/api/auth/login",
                                  {"username": user, "password": password})
        if code != 200:
            sys.exit(f"登录失败: HTTP {code} {payload.get('error')}")


def _load_corpus(path: Path, tag: str, sites_csv: str, limit: int):
    doc = json.loads(path.read_text(encoding="utf-8"))
    sites = doc.get("sites", [])
    if sites_csv:
        wanted = [s.strip() for s in sites_csv.split(",") if s.strip()]
        known = {s["url"]: s for s in sites}
        sites = [known.get(u, {"url": u, "expect": "", "tags": []}) for u in wanted]
    elif tag:
        sites = [s for s in sites if tag in (s.get("tags") or [])]
    if limit:
        sites = sites[:limit]
    return sites


def _norm(url: str) -> str:
    return url.rstrip("/").lower()


def _elapsed_from_ts(task: dict):
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        a = datetime.strptime(task["created_at"][:19], fmt)
        b = datetime.strptime(task["updated_at"][:19], fmt)
        return round((b - a).total_seconds(), 1)
    except (KeyError, ValueError, TypeError):
        return None


def _previous_run():
    runs = sorted(RUNS.glob("run_*.json"))
    if not runs:
        return None, {}
    try:
        doc = json.loads(runs[-1].read_text(encoding="utf-8"))
    except ValueError:
        return None, {}
    return runs[-1].name, {_norm(r["url"]): r for r in doc.get("results", [])}


def _fmt(v, nd=1):
    return "-" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def run(args):
    api = Api(args.base)
    api.login_if_needed(args.user, args.password)
    sites = _load_corpus(Path(args.corpus), args.tag, args.sites, args.limit)
    if not sites:
        sys.exit("语料为空(检查 --tag/--sites)")
    urls = [s["url"] for s in sites]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    cfg = {"viewport": args.viewport, "mode": "single", "max_pages": 1,
           "similarity": True, "render_timeout": args.render_timeout}
    code, payload = api.call("POST", "/api/batches",
                             {"urls": urls, "name": f"corpus {stamp} ({len(urls)} 站)", "config": cfg})
    if code != 201:
        sys.exit(f"提交批次失败: HTTP {code} {payload}")
    batch_id = payload["batch_id"]
    rejected = payload.get("rejected") or []
    print(f"批次 #{batch_id} 已提交: {payload['accepted']} 站, 拒收 {len(rejected)}", flush=True)

    timelines = {}           # task_id -> [(stage, t)]
    started = time.time()
    tasks = []
    while True:
        code, payload = api.call("GET", "/api/tasks", query={"batch_id": batch_id})
        tasks = payload.get("tasks", []) if code == 200 else []
        now = time.time()
        for t in tasks:
            tl = timelines.setdefault(t["id"], [])
            if tl and tl[-1][0] == "_end":
                continue
            if t.get("status") in ("completed", "failed"):
                if tl:
                    tl.append(("_end", now))   # 封口: 最后一个阶段不再累计到出报告那一刻
                continue
            if t.get("status") != "running":
                continue                       # 排队时间不算进任何阶段
            if not tl or tl[-1][0] != t.get("stage"):
                tl.append((t.get("stage"), now))
        done = [t for t in tasks if t.get("status") in ("completed", "failed")]
        running = [t for t in tasks if t.get("status") == "running"]
        pending = len(tasks) - len(done) - len(running)
        print(f"\r  完成 {len(done)}/{len(tasks)}  运行中 {len(running)}  等待 {pending}"
              f"  {int(now - started)}s  "
              + " | ".join(f"#{t['id']} {t.get('stage')}" for t in running[:3]) + " " * 10,
              end="", flush=True)
        if tasks and len(done) == len(tasks):
            break
        if now - started > args.timeout:
            print("\n超时, 停止等待(未完成的站点记为 timeout)")
            break
        time.sleep(args.poll)
    print()

    expect_by_url = {_norm(s["url"]): s for s in sites}
    results = []
    for t in sorted(tasks, key=lambda x: x["id"]):
        code, detail = api.call("GET", f"/api/tasks/{t['id']}")
        task = detail.get("task", t) if code == 200 else t
        stats = task.get("stats") or {}
        site = task.get("site") or {}
        sim = stats.get("similarity_detail") or site.get("similarity_detail") or {}
        meta = expect_by_url.get(_norm(task.get("target_url", "")), {})
        detected = stats.get("pattern") or site.get("pattern") or ""
        stage_times = stats.get("stage_times") or {}
        tl = timelines.get(t["id"], [])
        if not stage_times and tl:
            for i, (stage, t0) in enumerate(tl):
                t1 = tl[i + 1][1] if i + 1 < len(tl) else time.time()
                if stage and stage != "_end":
                    stage_times[stage] = round(stage_times.get(stage, 0) + (t1 - t0), 1)
            stage_times["_source"] = "poll"
        status = task.get("status")
        if status not in ("completed", "failed"):
            status = "timeout"
        failure = stats.get("failure") if status == "failed" else None
        if status == "failed" and not failure:   # 老引擎没有 failure 字段: 按错误文本粗分
            failure = "unreachable" if any(m in (task.get("error_msg") or "") for m in (
                "目标不可达", "ERR_NAME_NOT_RESOLVED", "ERR_TIMED_OUT", "ERR_CONNECTION",
                "ERR_ADDRESS_UNREACHABLE", "Page.goto: Timeout")) else "engine"
        results.append({
            "task_id": t["id"], "url": task.get("target_url"), "status": status,
            "expect": meta.get("expect", ""), "detected": detected,
            "pattern_hit": bool(meta.get("expect")) and detected == meta.get("expect"),
            "holdout": bool(meta.get("holdout")),
            "confidence": stats.get("pattern_confidence"),
            "features": stats.get("pattern_features") or [],
            "score": task.get("similarity_score"),
            "blocked": stats.get("baseline_blocked") or None,
            "interactive": stats.get("interactive_score"),
            "fidelity": stats.get("fidelity_score"),
            # 所见即所得: 预览/导出默认交付物的实测分(静态版接管时取静态版, 否则交互轨)
            "preview": stats.get("static_score") if stats.get("preview_track") == "static"
            else stats.get("interactive_score"),
            "preview_track": stats.get("preview_track") or "interactive",
            "track": stats.get("dominant_track"),
            "structure": sim.get("structure"), "edge": sim.get("edge"), "text": sim.get("text"),
            "warnings": sim.get("warnings") or [],
            "calibrations": stats.get("calibrations"),
            "assets": stats.get("assets_total"), "bytes": stats.get("total_bytes"),
            # 交付侧部署隐患审计(评分口径之外): 仍会阻塞首屏的未本地化脚本/样式
            "deploy_audit": stats.get("deploy_audit") or {},
            "api": stats.get("api"),
            "elapsed_s": stats.get("elapsed_s") or _elapsed_from_ts(task),
            "stage_times": stage_times,
            "phase_times": stats.get("phase_times") or {},
            "has_ai_report": bool(task.get("has_ai_report")),
            "failure": failure,
            "error": (task.get("error_msg") or "")[:300],
        })

    vps = [v for v in (args.viewports or "").split(",") if v.strip() and v.strip() != "none"]
    if vps:
        _probe_viewports(args, results, [v.strip() for v in vps])

    RUNS.mkdir(parents=True, exist_ok=True)
    prev_name, prev = _previous_run()
    out = {"stamp": stamp, "base": args.base, "batch_id": batch_id, "viewport": args.viewport,
           "tag": args.tag, "rejected": rejected, "results": results,
           "summary": _summary(results)}
    json_path = RUNS / f"run_{stamp}.json"
    json_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = RUNS / f"REPORT_{stamp}.md"
    md_path.write_text(_report(out, prev_name, prev), encoding="utf-8")
    print(f"报告: {md_path}\n明细: {json_path}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        _ci_thumbs(results)
    s = out["summary"]
    print(f"均分 {_fmt(s['mean'])}  交互轨均分 {_fmt(s['interactive_mean'])}"
          f"  预览所见均分 {_fmt(s.get('preview_mean'))}(静态版接管 {s.get('static_delivered', 0)} 站)"
          f"  中位 {_fmt(s['median'])}"
          f"  完成 {s['completed']}/{s['total']}(不可达 {s['unreachable']})"
          f"  识别命中 {s['pattern_hits']}/{s['pattern_checked']}"
          f"  耗时 p50 {_fmt(s['p50_elapsed'])}s / p90 {_fmt(s['p90_elapsed'])}s"
          + (f"  环境受阻 {s['blocked']}" if s.get("blocked") else ""))
    for name, key in (("语料内", "tuned"), ("留出集", "holdout")):
        g = s.get(key)
        if g:
            print(f"  {name} {g['total']} 站: 均分 {_fmt(g['mean'])}  交互轨 {_fmt(g['interactive_mean'])}"
                  f"  最低 {_fmt(g['min'])}  p50 {_fmt(g['p50_elapsed'])}s" + _vp_line(g))
    if s.get("vp_mean"):
        print("  视口分项(探针)" + _vp_line(s))
    return 0 if s["completed"] == s["total"] else 2


def _probe_viewports(args, results, vps):
    """多视口分项: 桌面采集的克隆在平板/手机宽下与原站同宽对比(见 viewport_probe.py)。
    desktop 探针分是本方法(活体原站 vs 预览, 无冻结)的噪声基线, 与引擎分不同口径。"""
    sys.path.insert(0, str(HERE))
    try:
        from viewport_probe import probe_tasks
    except ImportError as exc:
        print(f"(视口探针不可用, 跳过: {exc})")
        return
    pairs = [(r["task_id"], r["url"]) for r in results
             if r["status"] == "completed" and not r.get("blocked")]
    if not pairs:
        return
    print(f"视口分项探针: {len(pairs)} 站 × {','.join(vps)}", flush=True)
    out_dir = RUNS / f"viewports_{time.strftime('%Y%m%d_%H%M%S')}"
    try:
        res, _ = probe_tasks(args.base, pairs, vps, out_dir, workers=args.probe_workers,
                             noise=args.probe_noise)
    except Exception as exc:   # noqa: BLE001 探针失败不影响主跑分
        print(f"(视口探针失败: {exc})")
        return
    for r in results:
        p = res.get(r["task_id"])
        if p:
            r["vp"] = {v: (p.get(v) or {}).get("score") for v in vps}
            if args.probe_noise:
                r["vp_ceiling"] = {v: (p.get(v) or {}).get("ceiling") for v in vps}
            r["vp_overflow"] = [v for v in vps if (p.get(v) or {}).get("clone_overflow_x")
                                and not (p.get(v) or {}).get("orig_overflow_x")]


def _summary(results):
    out = _summary_one(results)
    # 语料内(已反复调优) 与 留出集(只看不调) 分开报: 两者差距就是过拟合程度
    tuned = [r for r in results if not r.get("holdout")]
    held = [r for r in results if r.get("holdout")]
    if held:
        out["tuned"] = _summary_one(tuned) if tuned else None
        out["holdout"] = _summary_one(held)
    return out


def _summary_one(results):
    scores = [r["score"] for r in results
              if isinstance(r["score"], (int, float)) and not r.get("blocked")]
    elapsed = [r["elapsed_s"] for r in results if isinstance(r["elapsed_s"], (int, float))]
    checked = [r for r in results if r["expect"]]
    inter = [r["interactive"] for r in results
             if isinstance(r.get("interactive"), (int, float)) and not r.get("blocked")]

    def pct(xs, q):
        xs = sorted(xs)
        return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None
    return {
        "total": len(results),
        "completed": sum(1 for r in results if r["status"] == "completed"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "timeout": sum(1 for r in results if r["status"] == "timeout"),
        "mean": round(statistics.mean(scores), 1) if scores else None,
        "median": round(statistics.median(scores), 1) if scores else None,
        "min": min(scores) if scores else None,
        "pattern_checked": len(checked),
        "pattern_hits": sum(1 for r in checked if r["pattern_hit"]),
        "mean_elapsed": round(statistics.mean(elapsed), 1) if elapsed else None,
        "fidelity_dominant": sum(1 for r in results if r["track"] == "fidelity"),
        # 诚实口径: 头条分取双轨最优, 交互轨(可交互交付物)单独看均分;
        # 网络不可达与引擎失败分开计数, 前者不是引擎缺陷
        "interactive_mean": round(statistics.mean(inter), 1) if inter else None,
        "preview_mean": _mean_of(results, "preview"),
        "static_delivered": sum(1 for r in results if r.get("preview_track") == "static"),
        "ge90": sum(1 for x in scores if x >= 90),
        "unreachable": sum(1 for r in results if r.get("failure") == "unreachable"),
        "engine_failed": sum(1 for r in results if r.get("failure") == "engine"),
        # 基准页是登录墙/风控/验证码: 环境受阻, 分数不反映引擎能力, 不计入均分
        "blocked": sum(1 for r in results if r.get("blocked")),
        "p50_elapsed": pct(elapsed, 0.5),
        "p90_elapsed": pct(elapsed, 0.9),
        # 视口分项(探针口径): {desktop/tablet/mobile: 均分}
        "vp_mean": _vp_means(results),
        # 交付侧: 首页仍残留阻塞首屏的未本地化脚本/样式的站数(应为 0)
        "deploy_hazard_sites": sum(
            1 for r in results
            if ((r.get("deploy_audit") or {}).get("blocking_scripts")
                or (r.get("deploy_audit") or {}).get("blocking_styles"))),
    }


def _mean_of(results, key):
    xs = [r.get(key) for r in results
          if isinstance(r.get(key), (int, float)) and not r.get("blocked")]
    return round(statistics.mean(xs), 1) if xs else None


def _vp_means(results):
    out = {}
    for r in results:
        for v, sc in (r.get("vp") or {}).items():
            if isinstance(sc, (int, float)) and not r.get("blocked"):
                out.setdefault(v, []).append(sc)
    return {v: round(statistics.mean(xs), 1) for v, xs in out.items()}


_VP_NAMES = {"desktop": "桌面", "tablet": "平板768", "mobile": "手机390",
             "iphone-air": "iPhone Air 真机口径", "ipad-air": "iPad Air 真机口径"}


def _vp_line(g):
    vm = g.get("vp_mean") or {}
    return "".join(f"  {_VP_NAMES.get(v, v)} {_fmt(x)}" for v, x in vm.items())


def _ci_thumbs(results, below=99.0):
    """CI 上把未满分站点的两侧缩略图(base64 JPEG)打进日志: 产物下载在部分环境不可达,
    job 日志总能取到; 远程归因不再只能看 <90 分站点(工作流里那段只管 <90)。"""
    try:
        from PIL import Image
    except ImportError:
        return
    root = Path.cwd() / "workspaces"
    for r in results:
        if not isinstance(r.get("score"), (int, float)) or r["score"] >= below or r["score"] < 90:
            continue
        for name in ("original_preview.png", "cloned_preview.png"):
            f = root / str(r["task_id"]) / name
            if not f.is_file():
                continue
            try:
                im = Image.open(f).convert("RGB")
                im = im.resize((480, max(1, round(im.height * 480 / im.width))))
                im = im.crop((0, 0, 480, min(im.height, 4000)))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=60)
            except Exception as exc:   # noqa: BLE001 缩略图只为归因, 失败不影响跑分
                print(f"(缩略图失败 {r['task_id']} {name}: {exc})")
                continue
            b64 = base64.b64encode(buf.getvalue()).decode()
            print(f"::group::shot {r['task_id']} {name} {r['url']}")
            for k in range(0, len(b64), 4000):
                print("SHOT64:" + b64[k:k + 4000])
            print("::endgroup::")


def _report(out, prev_name, prev):
    s = out["summary"]
    lines = [f"# 真实语料跑分 {out['stamp']}", "",
             f"实例 {out['base']} · 批次 #{out['batch_id']} · 视口 {out['viewport']}"
             + (f" · 标签 {out['tag']}" if out['tag'] else ""), "",
             f"- 站点 {s['total']}: 完成 {s['completed']} / 失败 {s['failed']}"
             f"(网络不可达 {s.get('unreachable', 0)}, 引擎 {s.get('engine_failed', 0)}) / 超时 {s['timeout']}",
             f"- 还原度: 均分 **{_fmt(s['mean'])}**, 中位 {_fmt(s['median'])}, 最低 {_fmt(s['min'])};"
             f" ≥90 分 {s.get('ge90')} 站; 交互轨均分 {_fmt(s.get('interactive_mean'))};"
             f" 预览所见均分 {_fmt(s.get('preview_mean'))}(静态版接管 {s.get('static_delivered', 0)} 站)",
             f"- 识别命中: {s['pattern_hits']}/{s['pattern_checked']}",
             f"- 耗时: 平均 {_fmt(s['mean_elapsed'])}s, p50 {_fmt(s.get('p50_elapsed'))}s,"
             f" p90 {_fmt(s.get('p90_elapsed'))}s; 保真轨胜出 {s['fidelity_dominant']} 站"
             + (f"; 环境受阻 {s['blocked']} 站(不计入均分)" if s.get("blocked") else ""),
             f"- 交付侧部署审计: 首页残留阻塞首屏的未本地化脚本/样式 **{s.get('deploy_hazard_sites', 0)}** 站"
             f"(应为 0; 评分口径对非本地请求瞬时 abort, 此项补量交付产物部署后的首屏隐患)", ""]
    for name, key in (("语料内(已调优)", "tuned"), ("留出集(只看不调)", "holdout")):
        g = s.get(key)
        if g:
            lines.append(f"- **{name}** {g['total']} 站: 均分 {_fmt(g['mean'])}, 交互轨 {_fmt(g['interactive_mean'])},"
                         f" 最低 {_fmt(g['min'])}, 识别 {g['pattern_hits']}/{g['pattern_checked']},"
                         f" p50 {_fmt(g['p50_elapsed'])}s / p90 {_fmt(g['p90_elapsed'])}s"
                         + (";" + _vp_line(g) if g.get("vp_mean") else ""))
    if s.get("vp_mean"):
        lines.append("- **视口分项**(桌面视口采集的克隆, 在各宽度下与原站同宽对比; 探针口径: 活体原站无冻结,"
                     " 桌面列是本口径的噪声基线):" + _vp_line(s))
    if s.get("holdout"):
        lines.append("")
    if prev_name:
        lines.append(f"对比上一轮 `{prev_name}`: Δ 列为分数增减。")
        lines.append("")
    vps = list(s.get("vp_mean") or {})
    lines.append("| # | 站点 | 期望模式 | 检出模式 | 命中 | 分 | Δ | 交互 | 保真 | 轨 | "
                 + "".join(f"{_VP_NAMES.get(v, v)} | " for v in vps) + "耗时 s | 最慢阶段 | 错误 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|" + "---|" * len(vps) + "---|---|---|")

    def key(r):
        return (r["status"] != "completed", r["score"] if isinstance(r["score"], (int, float)) else -1)
    for r in sorted(out["results"], key=key):
        p = prev.get(_norm(r["url"] or ""))
        delta = "-"
        if p and isinstance(p.get("score"), (int, float)) and isinstance(r["score"], (int, float)):
            d = r["score"] - p["score"]
            delta = f"{d:+.1f}"
        st = {k: v for k, v in (r["stage_times"] or {}).items() if isinstance(v, (int, float))}
        slowest = max(st, key=st.get) + f" {st[max(st, key=st.get)]}s" if st else "-"
        hit = "✓" if r["pattern_hit"] else ("✗" if r["expect"] else "·")
        err = r["error"].replace("|", "/").replace("\n", " ")[:80] if r["status"] != "completed" else ""
        if r.get("blocked"):
            err = f"环境受阻: {r['blocked']}"
        if r.get("holdout"):
            hit += "ᴴ"
        lines.append(f"| {r['task_id']} | {r['url']} | {r['expect'] or '-'} | {r['detected'] or '-'} | {hit}"
                     f" | {_fmt(r['score'])} | {delta} | {_fmt(r['interactive'])} | {_fmt(r['fidelity'])}"
                     f" | {r['track'] or '-'} | "
                     + "".join(f"{_fmt((r.get('vp') or {}).get(v))}{'↔' if v in (r.get('vp_overflow') or []) else ''} | "
                               for v in vps)
                     + f"{_fmt(r['elapsed_s'])} | {slowest} | {err} |")
    lines += ["", "## 阶段耗时(秒)", ""]
    stages = []
    for r in out["results"]:
        for k in (r["stage_times"] or {}):
            if k not in stages and not k.startswith("_"):
                stages.append(k)
    if stages:
        lines.append("| 站点 | " + " | ".join(stages) + " |")
        lines.append("|---|" + "---|" * len(stages))
        for r in out["results"]:
            st = r["stage_times"] or {}
            lines.append(f"| {r['url']} | " + " | ".join(_fmt(st.get(k)) for k in stages) + " |")
    phases = []
    for r in out["results"]:
        for k in (r.get("phase_times") or {}):
            if k not in phases:
                phases.append(k)
    if phases:
        lines += ["", "## 子阶段耗时(秒)", "",
                  "| 站点 | " + " | ".join(phases) + " |", "|---|" + "---|" * len(phases)]
        for r in out["results"]:
            pt = r.get("phase_times") or {}
            lines.append(f"| {r['url']} | " + " | ".join(_fmt(pt.get(k)) for k in phases) + " |")
        sums = {k: sum((r.get("phase_times") or {}).get(k) or 0 for r in out["results"]) for k in phases}
        lines.append("| **合计** | " + " | ".join(f"{sums[k]:.0f}" for k in phases) + " |")
    lines += ["", "## 识别明细", ""]
    for r in out["results"]:
        lines.append(f"- {r['url']}: {r['detected'] or '-'} (置信 {_fmt(r['confidence'], 2)}; "
                     f"特征 {', '.join(r['features']) or '无'})"
                     + (f"; 评分告警: {'; '.join(map(str, r['warnings']))}" if r["warnings"] else ""))
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8897", help="平台实例地址")
    ap.add_argument("--corpus", default=str(CORPUS))
    ap.add_argument("--tag", default="", help="只跑带此标签的站(cn/global/...)")
    ap.add_argument("--sites", default="", help="逗号分隔 URL, 覆盖语料")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--viewport", default="desktop", choices=["desktop", "laptop", "mobile"])
    ap.add_argument("--viewports", default="desktop,tablet,mobile",
                    help="视口分项探针(逗号分隔 desktop/tablet/mobile; none 关闭)")
    ap.add_argument("--probe-workers", type=int, default=3)
    ap.add_argument("--probe-noise", action="store_true", help="视口探针另测原站自比上限(多一倍原站加载)")
    ap.add_argument("--render-timeout", type=int, default=0, help="页面加载超时秒(0=平台默认)")
    ap.add_argument("--timeout", type=int, default=3600, help="整批等待上限秒")
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--user", default="")
    ap.add_argument("--password", default="")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
