"""L1 生成编排: 输入 → InputAdapter → UIR → OutputTarget → 渲染截图 → 评分 v3 → 迭代。

与 ClonerEngine 同一任务契约(clone_tasks 行 + 工作空间 + site.json + flask_generator 打包),
由 scheduler 按 target_url 前缀分流到这里, 不碰 cloner_engine.py。

质量循环(DESIGN.md §9.5/§9.6):
  - 预算: GENERATE_TIME_BUDGET 内, 最多 GENERATE_MAX_ROUNDS 轮, ≥ GENERATE_TARGET_SCORE 即停;
  - 每轮: 适配器出 UIR(带上一轮差异线索) → 渲染 → 截图 → compute_visual_similarity;
  - 只保留净提升的轮次(回滚守卫, 与 AI 视觉修复同语义);
  - 截图输入: 参考图 = 输入图(像素评分); 描述输入: 退化自评(desc-self), 报分单列。
"""
import json
import shutil
import time
from contextlib import suppress
from pathlib import Path

import config
from core import flask_generator
from core.uir import llm, render, settings
from core.uir.model import UIR
from core.uir.registry import GenRequest, BuildContext, get_input, get_output

GEN_FILES = ("index.html", "static", "uir.json", "cloned_preview.png", "cloned_text.txt",
             "diff_preview.png", "original_preview.png", "original_text.txt", "site.json",
             "similarity_detail.json", "generate_report.json", "flask_app")


def is_generate_task(task: dict) -> bool:
    return str((task or {}).get("target_url") or "").startswith(settings.TARGET_URL_PREFIX)


def _snapshot(ws: Path) -> dict:
    snap = {}
    # 含 Electron 壳内页面: 回滚守卫对所有可截图目标的渲染产物一视同仁
    for name in ("index.html", "uir.json", "cloned_preview.png", "cloned_text.txt",
                 "electron_app/app/index.html", "electron_app/app/uir.json", "electron_app/app/static/css/app.css"):
        p = ws / name
        snap[name] = p.read_bytes() if p.is_file() else None
    css = ws / "static" / "css" / "app.css"
    snap["static/css/app.css"] = css.read_bytes() if css.is_file() else None
    return snap


def _restore(ws: Path, snap: dict):
    for name, data in snap.items():
        p = ws / name
        if data is None:
            with suppress(Exception):
                p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)


class GenerateEngine:
    """一个生成任务的全流程(同步, 在调度器工作线程内运行)。"""

    def __init__(self, task_id, db, bus, pool):
        self.task_id, self.db, self.bus, self.pool = task_id, db, bus, pool
        self.ws: Path = config.WORKSPACE_ROOT / str(task_id)
        self.cfg, self.spec = {}, {}
        self._timeline = []

    def log(self, stage, msg):
        self.bus.publish(self.task_id, stage, msg)

    def _stage(self, name, progress):
        self._timeline.append((name, time.monotonic()))
        self.db.update_task(self.task_id, stage=name, progress=progress)

    # ------------------------------------------------------------------
    def run(self):
        t0 = time.monotonic()
        task = self.db.get_task(self.task_id)
        batch = self.db.get_batch(task["batch_id"]) if hasattr(self.db, "get_batch") else None
        try:
            self.cfg = json.loads((batch or {}).get("config_json") or "{}") if batch else {}
        except ValueError:
            self.cfg = {}
        self.spec = dict(self.cfg.get("gen") or {})
        self.ws.mkdir(parents=True, exist_ok=True)
        self.db.update_task(self.task_id, status="running", workspace_path=str(self.ws), error_msg="")
        self.log("GEN", f"生成任务 #{self.task_id} 启动: kind={self.spec.get('kind')} "
                        f"target={self.spec.get('target', 'html_css')}")
        try:
            report = self._execute()
            elapsed = round(time.monotonic() - t0, 1)
            self.log("INFO", f"生成任务 #{self.task_id} 完成, 耗时 {elapsed}s, "
                             f"最终评分 {report.get('final_score')}")
        except Exception as exc:
            self.log("ERROR", f"生成失败: {type(exc).__name__}: {exc}")
            self.db.update_task(self.task_id, status="failed",
                                error_msg=f"{type(exc).__name__}: {exc}"[:1500])
            raise

    # ------------------------------------------------------------------
    def _request(self, hints: dict) -> GenRequest:
        kind = self.spec.get("kind") or "description"
        vp = dict(self.spec.get("viewport") or settings.GEN_DEFAULT_VIEWPORT)
        image = None
        if kind == "screenshot":
            image = self.ws / "original_preview.png"
            if not image.is_file():
                src = Path(self.spec.get("image_path") or "")
                if not src.is_file():
                    raise FileNotFoundError("截图输入缺失")
                shutil.copyfile(src, image)
            from PIL import Image
            with Image.open(image) as im:
                vp = {"width": im.size[0], "height": min(im.size[1], 1200)}
        source = Path(self.spec["source_path"]) if self.spec.get("source_path") else None
        return GenRequest(kind=kind, image_path=image, description=self.spec.get("description") or "",
                          viewport=vp, hints=hints, source_path=source,
                          meta={"task_id": self.task_id, "layout": self.spec.get("layout", ""),
                                "llm_mode": self.spec.get("llm_mode") or "skeleton_fill"})

    def _execute(self) -> dict:
        budget = int(self.spec.get("time_budget") or settings.GENERATE_TIME_BUDGET)
        target_score = float(self.spec.get("target_score") or settings.GENERATE_TARGET_SCORE)
        max_rounds = int(self.spec.get("max_rounds") or settings.GENERATE_MAX_ROUNDS)
        deadline = time.monotonic() + budget
        ai_cfg = llm.resolve_ai_cfg(self.cfg)
        ctx = BuildContext(ws=self.ws, log=self.log, deadline=deadline, ai_cfg=ai_cfg,
                           score=render.score_against_reference, pool=self.pool)
        kind = self.spec.get("kind") or "description"
        target_name = self.spec.get("target") or "html_css"
        adapter, target = get_input(kind), get_output(target_name)
        self.log("GEN", f"适配器 {kind} → 目标 {target_name}; 预算 {budget}s / {max_rounds} 轮 / 目标分 {target_score}"
                        + ("" if ai_cfg else "; 未配置大模型 Key, 走确定性 Tier 0"))

        ref_png = self.ws / "original_preview.png"
        ref_text = ""
        read_fut = None
        if kind == "screenshot" and self.spec.get("reference_text"):
            ref_text = str(self.spec["reference_text"])[:20000]
        elif kind == "screenshot" and ai_cfg and target.shootable:
            # 截图没有 DOM 文本 → 评分 v3 的文本通道原本被关掉(权重重分配), 结果"文字纹理代理"与真实文字
            # 在像素口径下不可区分(实测同几何只换文字反而 -10 分)。先让模型读一遍图作为参考文本, 地板轮与
            # 填充轮在同一参考下打三通道分, 真实文字才会被奖励。参考文本来源在报告里标明为 model-read。
            # 读图与骨架填充都是纯模型等待, 二者并行发出(此前串行, 长页墙钟 = 读图 100s+ 与填充相加),
            # 各自有墙钟上限; 地板轮评分时才取读图结果。
            from concurrent.futures import ThreadPoolExecutor
            from core.uir.adapters.vision import read_screenshot_text, prefetch_fill
            self._request({})  # 确保 original_preview.png 已就位
            ex = ThreadPoolExecutor(max_workers=1)
            read_fut = ex.submit(read_screenshot_text, ai_cfg, ref_png, 400, 6, settings.GENERATE_READ_BUDGET)
            ex.shutdown(wait=False)
            if (self.spec.get("llm_mode") or "skeleton_fill") == "skeleton_fill" and get_input(kind).__class__.__name__ == "ScreenshotAdapter":
                try:
                    ctx.extra["prefetch_fill"] = prefetch_fill(ai_cfg, ref_png, settings.GENERATE_FILL_BUDGET)
                except Exception as exc:
                    self.log("GEN", f"骨架填充预取失败, 第 2 轮按需填充: {type(exc).__name__}: {str(exc)[:120]}")

        def _resolve_ref_text():
            nonlocal ref_text, read_fut
            if read_fut is None:
                return ref_text
            fut, read_fut = read_fut, None
            try:
                ref_text = fut.result(timeout=settings.GENERATE_READ_BUDGET + 10)
                self.log("GEN", f"参考文本: 模型读图 {len(ref_text.splitlines())} 行 / {len(ref_text)} 字, 文本通道启用")
            except Exception as exc:
                self.log("GEN", f"模型读图失败, 文本通道不启用: {type(exc).__name__}: {str(exc)[:120]}")
                ref_text = ""
            if ref_text:
                (self.ws / "original_text.txt").write_text(ref_text, encoding="utf-8")
            report["reference_text"] = "provided" if self.spec.get("reference_text") else ("model-read" if ref_text else "none")
            return ref_text

        if ref_text:
            (self.ws / "original_text.txt").write_text(ref_text, encoding="utf-8")
        ctx.deadline = time.monotonic() + budget
        deadline = ctx.deadline

        report = {"kind": kind, "target": target_name, "tier": None, "rounds": [], "final_score": None,
                  "reference_text": ("provided" if self.spec.get("reference_text") else ("model-read" if ref_text else "none")),
                  "scoring": "v3-segmented" if kind == "screenshot" else "desc-self-v1",
                  "target_score": target_score, "budget": budget, "ai_model": (ai_cfg or {}).get("model")}
        best, hints = None, {}
        # 确定性优先、LLM 兜底(字面执行): 有 Key 时第 1 轮强制走 Tier 0 作为地板(2s 级), 后续 LLM 轮
        # 只有净提升才会被保留 —— 否则 LLM 轮每轮 2~4 分钟, 预算内从未评估过确定性候选, 实测留出集
        # 像素均分 39.8(Tier 0) → 23.2(全 LLM) 还被当成"最优"保留。
        floor_round = 1 if ai_cfg and target.shootable else 0
        if floor_round:
            max_rounds += 1
        for rnd in range(1, max_rounds + 1):
            if ctx.expired():
                self.log("GEN", "时间预算耗尽, 停止迭代")
                break
            self._stage(f"第 {rnd} 轮生成", 10 + rnd * 15)
            t_r = time.monotonic()
            if rnd == floor_round:
                hints = dict(hints, force_tier0=True)
            elif hints.get("force_tier0"):
                hints = {k: v for k, v in hints.items() if k != "force_tier0"}
            req = self._request(hints)
            snap = _snapshot(self.ws)
            uir = adapter.to_uir(req, ctx)
            errs = uir.validate()
            if errs:
                self.log("GEN", f"UIR 校验告警 {len(errs)} 条: {errs[0]}")
            rr = target.render(uir, self.ws, ctx)
            if not rr.shootable:
                # 非可截图目标(Flutter 等): 像素评分不覆盖, 单轮产出代码即止, 分数记 None
                self.log("GEN", f"目标 {target_name} 不可截图, 跳过像素评分; 产物 {rr.entry_file.name} ({rr.meta.get('bytes')} 字节)")
                best = {"score": None, "detail": {"score": None, "scoring": "none", "warnings": ["目标不可截图, 像素评分不覆盖"]},
                        "round": rnd, "uir_stats": uir.stats()}
                report["rounds"].append({"round": rnd, "tier": uir.meta.get("tier"), "nodes": uir.root.count(),
                                         "score": None, "elapsed": round(time.monotonic() - t_r, 1), "kept": True,
                                         "entry": str(rr.entry_file)})
                report["tier"] = uir.meta.get("tier") or report["tier"]
                report["scoring"] = "none"
                break
            self._stage(f"第 {rnd} 轮渲染评分", 20 + rnd * 15)
            shot = render.screenshot_dir(self.pool, self.ws, rr.entry_file, self.ws / "cloned_preview.png",
                                         req.viewport, log=self.log)
            out_text = render.visible_text_of(rr.entry_file.read_text(encoding="utf-8", errors="replace"))
            (self.ws / "cloned_text.txt").write_text(out_text, encoding="utf-8")
            if kind == "screenshot":
                detail = render.score_against_reference(ref_png, self.ws / "cloned_preview.png",
                                                        self.ws / "diff_preview.png", _resolve_ref_text(), out_text)
            else:
                detail = render.description_self_score(req.description, out_text, uir.stats())
            score = detail["score"]
            rec = {"round": rnd, "tier": uir.meta.get("tier"), "nodes": uir.root.count(), "score": score,
                   "structure": detail.get("structure"), "edge": detail.get("edge"), "text": detail.get("text"),
                   "elapsed": round(time.monotonic() - t_r, 1), "shot": shot, "kept": False}
            report["tier"] = report["tier"] or uir.meta.get("tier")
            improved = best is None or score > best["score"]
            if improved:
                best = {"score": score, "detail": detail, "round": rnd, "uir_stats": uir.stats()}
                rec["kept"] = True
            else:
                _restore(self.ws, snap)
                self.log("GEN", f"第 {rnd} 轮未提升({best['score']} → {score}), 回滚到最优轮")
            report["rounds"].append(rec)
            self.log("SIMILARITY", f"第 {rnd} 轮评分 {score} (结构 {detail.get('structure')} / "
                                   f"边缘 {detail.get('edge')} / 文本 {detail.get('text')}), "
                                   f"{rec['nodes']} 节点, {rec['elapsed']}s")
            if score >= target_score:
                self.log("GEN", f"已达目标分 {target_score}, 停止迭代")
                break
            if uir.meta.get("llm_mode") == "skeleton_fill":
                # 骨架是确定性的, 填充是唯一的随机项。关思考后一轮填充 5~15s, 预算内多取几个候选(升温增加
                # 多样性), 只保留评分净提升者; 填充轮数用尽即停(带思考时一轮 60s+, 当时只跑一轮)
                fills = sum(1 for r in report["rounds"] if r.get("tier") == "llm")
                if fills >= settings.GENERATE_FILL_ROUNDS or ctx.remaining() < 30:
                    self.log("GEN", f"骨架填充已取 {fills} 个候选, 停止")
                    break
                hints = {"fill_temp": min(1.0, 0.2 + 0.35 * fills)}
                continue
            # 下一轮线索: 像素差分区域(截图) / 自评反馈(描述); 确定性 Tier 0 加深切分粒度
            hints = {"score": score, "structure": detail.get("structure"), "edge": detail.get("edge"),
                     "last_render": str(self.ws / "cloned_preview.png") if kind == "screenshot" else None}
            if kind == "screenshot":
                hints["regions"] = render.region_hints(ref_png, self.ws / "cloned_preview.png", detail)
                if uir.meta.get("tier") == "deterministic" and uir.meta.get("seg") == "v2":
                    # v2 之后再给 v1(缩略图 XY 切分)一轮作为对照候选, 只保留更高分者
                    hints["seg_algo"] = "v1"
                elif uir.meta.get("tier") == "deterministic":
                    hints["seg_algo"] = "v1"
                    # 确定性细化: 深度 +1, 最小区域面积减半, 沟槽最短长度 -1(≥2) —— 都朝"切得更细"单调
                    hints["seg_depth"] = min(int(req.hints.get("seg_depth") or settings.SEG_MAX_DEPTH) + 1, 8)
                    hints["seg_min_area"] = max(0.0002, float(req.hints.get("seg_min_area") or settings.SEG_MIN_REGION_AREA) * 0.5)
                    hints["seg_min_run"] = max(3, int(req.hints.get("seg_min_run") or 6) - 1)
            else:
                hints["feedback"] = (f"自评 {score}: 文本覆盖 {detail.get('text')}, 结构 {detail.get('structure')}; "
                                     "请补全描述中出现的具体文案与控件")
                if uir.meta.get("tier") == "deterministic" and not (ai_cfg and rnd == floor_round):
                    break  # 确定性原型无随机性, 再跑一轮无意义(地板轮除外: 之后还要让 LLM 轮竞争)

        if best is None:
            raise RuntimeError("没有任何一轮产出有效结果")
        self._stage("打包产物", 90)
        detail = best["detail"]
        (self.ws / "similarity_detail.json").write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
        pages_map = {"/": "index.html"} if (self.ws / "index.html").is_file() else {}
        site_meta = {"task_id": self.task_id, "domain": "generated", "upstream": "", "routes": pages_map,
                     "proxy_enabled": False, "similarity": best["score"], "similarity_detail": detail,
                     "fidelity_score": None, "frames": [], "pattern": "UIR-GENERATE",
                     "generate": {"kind": kind, "target": target_name, "tier": report["tier"]}}
        (self.ws / "site.json").write_text(json.dumps(site_meta, ensure_ascii=False, indent=2), encoding="utf-8")
        if pages_map:
            zip_path = flask_generator.generate_project(self.task_id, "", self.ws, pages_map, [], False,
                                                        tracker_hook="", tracker_cfg=None)
        else:   # 工程型产物(Flutter / Electron): 直接打包工程目录(源码, 不含构建产物)
            zip_path = config.EXPORTS_DIR / f"{self.task_id}.zip"
            proj = next((self.ws / d for d in ("flutter_app", "electron_app") if (self.ws / d).is_dir()), self.ws)
            flask_generator.build_zip(proj, zip_path, f"generated_{self.task_id}")
        build_kind = str(self.spec.get("build") or "")
        if build_kind:
            # 构建安装包在生成预算之外(冷启动 Gradle 可达数分钟); 失败只记缺口, 不影响生成结果
            self._stage("构建安装包", 92)
            report["build"] = self._build_binaries(build_kind)
        report["final_score"] = best["score"]
        report["best_round"] = best["round"]
        report["uir_stats"] = best["uir_stats"]
        (self.ws / "generate_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        stage_times = {}
        for i, (name, t) in enumerate(self._timeline):
            nxt = self._timeline[i + 1][1] if i + 1 < len(self._timeline) else time.monotonic()
            stage_times[name] = round(nxt - t, 2)
        stats = {"generate": report, "similarity_detail": detail, "stage_times": stage_times,
                 "pattern": "UIR-GENERATE", "scoring": report["scoring"],
                 "interactive_score": best["score"] if kind == "screenshot" else None,
                 "fidelity_score": None, "css": 1, "js": 0, "img": 0, "fonts": 0, "api": 0}
        self.db.update_task(self.task_id, status="completed", progress=100, stage="完成",
                            similarity_score=best["score"], stats_json=json.dumps(stats, ensure_ascii=False),
                            workspace_path=str(self.ws), zip_path=str(zip_path))
        with suppress(Exception):
            self.db.increment_completed(self.db.get_task(self.task_id)["batch_id"])
        return report

    def _build_binaries(self, build_kind: str) -> dict:
        """apk: flutter_app → release APK; desktop: electron_app → 宿主平台包 + Windows x64 包。
        产物复制/压缩到 EXPORTS_DIR, 报告里只记文件名与大小, 下载走 /api/v4/generate/<id>/artifact/<i>。"""
        from core.uir import toolchain
        blog = lambda m: self.log("BUILD", m)  # noqa: E731
        out = config.EXPORTS_DIR
        out.mkdir(parents=True, exist_ok=True)
        try:
            if build_kind == "apk":
                app = self.ws / "flutter_app"
                if not app.is_dir():
                    return {"ok": False, "gap": "构建 APK 需要输出目标 flutter"}
                res = toolchain.build_apk(app, release=True, log=blog)
                if not res.get("ok"):
                    return {"ok": False, "gap": res.get("gap"), "log": res.get("log", "")[-2000:]}
                dest = out / f"{self.task_id}_app-release.apk"
                shutil.copyfile(res["artifact"], dest)
                arts = [{"platform": "android", "file": dest.name, "bytes": dest.stat().st_size, "label": "APK"}]
                result = {"ok": True, "artifacts": arts, "signing": res.get("signing"),
                          "analyze_ok": res.get("analyze_ok"), "test_ok": res.get("test_ok")}
            elif build_kind == "desktop":
                app = self.ws / "electron_app"
                if not app.is_dir():
                    return {"ok": False, "gap": "构建桌面包需要输出目标 electron"}
                host = toolchain.host_platform()
                plats = [host] + (["win32"] if host != "win32" else [])
                res = toolchain.build_desktop(app, plats, log=blog)
                arts = []
                for a in res.get("artifacts", []):
                    src = Path(a["dir"])
                    dest = out / f"{self.task_id}_{src.name}.zip"
                    toolchain.zip_dir(src, dest)
                    label = {"darwin": "macOS .app", "win32": "Windows .exe", "linux": "Linux"}.get(a["platform"], a["platform"])
                    arts.append({"platform": a["platform"], "arch": a["arch"], "file": dest.name,
                                 "bytes": dest.stat().st_size, "label": f"{label} ({a['arch']})"})
                result = {"ok": bool(arts), "artifacts": arts, "signing": res.get("signing"),
                          "gap": res.get("gap") or ("; ".join(f"{k} 打包失败" for k in res.get("failures", {})) or None)}
            else:
                return {"ok": False, "gap": f"未知构建类型 {build_kind!r}"}
        except Exception as exc:  # 工具链异常不拖垮生成任务
            self.log("BUILD", f"构建异常: {type(exc).__name__}: {exc}")
            return {"ok": False, "gap": f"构建异常: {type(exc).__name__}"}
        for a in result.get("artifacts", []):
            self.log("BUILD", f"产物 {a['label']}: {a['file']} ({a['bytes'] / 1048576:.1f} MB)")
        if result.get("gap"):
            self.log("BUILD", f"缺口: {result['gap']}")
        return result


def run_generation_task(task: dict, db, bus, pool):
    GenerateEngine(task["id"], db, bus, pool).run()
