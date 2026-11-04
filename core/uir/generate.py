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
    for name in ("index.html", "uir.json", "cloned_preview.png", "cloned_text.txt"):
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
                          meta={"task_id": self.task_id, "layout": self.spec.get("layout", "")})

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
        if kind == "screenshot" and self.spec.get("reference_text"):
            ref_text = str(self.spec["reference_text"])[:20000]
            (self.ws / "original_text.txt").write_text(ref_text, encoding="utf-8")

        report = {"kind": kind, "target": target_name, "tier": None, "rounds": [], "final_score": None,
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
                                                        self.ws / "diff_preview.png", ref_text, out_text)
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
            # 下一轮线索: 像素差分区域(截图) / 自评反馈(描述); 确定性 Tier 0 加深切分粒度
            hints = {"score": score, "structure": detail.get("structure"), "edge": detail.get("edge"),
                     "last_render": str(self.ws / "cloned_preview.png") if kind == "screenshot" else None}
            if kind == "screenshot":
                hints["regions"] = render.region_hints(ref_png, self.ws / "cloned_preview.png", detail)
                if uir.meta.get("tier") == "deterministic":
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
        else:   # 代码型产物(Flutter): 直接打包产物目录
            zip_path = config.EXPORTS_DIR / f"{self.task_id}.zip"
            flask_generator.build_zip(self.ws / "flutter_app" if (self.ws / "flutter_app").is_dir() else self.ws,
                                      zip_path, f"generated_{self.task_id}")
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


def run_generation_task(task: dict, db, bus, pool):
    GenerateEngine(task["id"], db, bus, pool).run()
