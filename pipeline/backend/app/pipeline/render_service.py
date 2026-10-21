"""合成编排：按当前分镜的镜头选用组装合成计划，后台执行并校验成片。

合成只用「已采纳」的镜头；有镜头没就绪时拒绝合成并说明是哪几个。
合成本身不花 H3/M3 的钱，可以反复换转场、字幕、BGM 重新合成。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .. import constants as C
from .. import db
from ..compose import ComposeError, compose
from ..config import ASSETS_DIR, RENDERS_DIR
from ..image_ops import ratio_size
from ..media import probe_video

DEFAULT_OPTIONS = {
    "transition": "cut",       # cut | fade
    "subtitles": True,
    "keep_audio": True,        # 保留 H3 生成的环境音/动作音
    "original_volume": 1.0,
    "use_bgm": True,           # 项目上传过 BGM 才生效
    "bgm_volume": 0.25,
}

BGM_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"}
BGM_MAX_BYTES = 30 * 1024 * 1024


class RenderNotReady(ValueError):
    pass


def normalize_options(raw: dict | None) -> dict:
    opts = dict(DEFAULT_OPTIONS)
    for key, value in (raw or {}).items():
        if key in opts and value is not None:
            opts[key] = value
    if opts["transition"] not in ("cut", "fade"):
        raise ValueError("转场只能是 cut（硬切）或 fade（淡入淡出）")
    opts["subtitles"] = bool(opts["subtitles"])
    opts["keep_audio"] = bool(opts["keep_audio"])
    opts["use_bgm"] = bool(opts["use_bgm"])
    opts["original_volume"] = min(max(float(opts["original_volume"]), 0.0), 2.0)
    opts["bgm_volume"] = min(max(float(opts["bgm_volume"]), 0.0), 1.5)
    return opts


def build_plan(project_id: int) -> dict:
    """当前 active 分镜的合成计划：每镜用哪条 take、合成时长、字幕。"""
    project = db.get_project(project_id)
    if project is None:
        raise ValueError("项目不存在")
    sb_id = project["active_storyboard_id"]
    if not sb_id:
        raise RenderNotReady("项目还没有分镜")
    sb = db.get_storyboard(int(sb_id))
    if sb["status"] != "confirmed":
        raise RenderNotReady("当前分镜还没确认（改稿草稿需要先确认），确认后才能合成")
    selections = {r["shot_key"]: r for r in db.list_shot_takes(int(sb_id))}
    items, missing = [], []
    for shot in db.list_shots(int(sb_id)):
        sel = selections.get(shot["shot_key"])
        take = db.get_take(int(sel["take_id"])) if sel else None
        if take is None or take["status"] != C.ACCEPTED or not take["local_path"] \
                or not Path(take["local_path"]).exists():
            missing.append({"shot_key": shot["shot_key"],
                            "status": take["status"] if take else "no_take"})
            continue
        probe = json.loads(take["probe_json"] or "{}")
        items.append({"shot_key": shot["shot_key"], "take_id": int(take["id"]),
                      "path": take["local_path"], "duration": int(shot["duration"]),
                      "subtitle": shot["subtitle"] or "",
                      "has_audio": bool(probe.get("has_audio")),
                      "source_duration": probe.get("duration")})
    if missing:
        desc = "、".join(f"{m['shot_key']}（{m['status']}）" for m in missing)
        raise RenderNotReady(f"还有镜头没有采纳的成片：{desc}")
    if not items:
        raise RenderNotReady("当前分镜没有镜头")
    width, height = ratio_size(project["ratio"])
    return {"storyboard_id": int(sb_id), "version": sb["version"], "ratio": project["ratio"],
            "width": width, "height": height, "items": items}


def create_render(project_id: int, options: dict | None) -> int:
    from ..compose import expected_total
    plan = build_plan(project_id)
    opts = normalize_options(options)
    project = db.get_project(project_id)
    if opts["use_bgm"] and project["bgm_path"] and Path(project["bgm_path"]).exists():
        opts["bgm_path"] = project["bgm_path"]
    else:
        opts["bgm_path"] = None
    expected = expected_total([i["duration"] for i in plan["items"]], opts["transition"])
    rid = db.create_render(project_id=project_id, storyboard_id=plan["storyboard_id"],
                           options=opts, plan=plan, expected_duration=expected)
    db.update_project(project_id, status="composing")
    db.add_event(None, "render",
                 f"合成 #{rid} 已排队：{len(plan['items'])} 镜，预计 {expected:.1f}s，"
                 f"{'淡入淡出' if opts['transition'] == 'fade' else '硬切'}"
                 f"{'，含 BGM' if opts['bgm_path'] else ''}",
                 project_id=project_id)
    return rid


def execute_render(render_id: int) -> None:
    """后台线程执行一次合成（Runner 先用 claim_render 占位）。"""
    r = db.get_render(render_id)
    plan = json.loads(r["plan_json"] or "{}")
    opts = json.loads(r["options_json"] or "{}")
    pid = int(r["project_id"])
    out = RENDERS_DIR / f"project_{pid}" / f"render_{render_id}.mp4"
    work = RENDERS_DIR / f"project_{pid}" / f"work_{render_id}"
    try:
        for item in plan["items"]:
            if not Path(item["path"]).exists():
                raise ComposeError(f"{item['shot_key']} 的成片文件缺失：{item['path']}")
        bgm = opts.get("bgm_path")
        if bgm and not Path(bgm).exists():
            bgm = None
        result = compose(items=plan["items"], width=int(plan["width"]),
                         height=int(plan["height"]), transition=opts["transition"],
                         subtitles=bool(opts["subtitles"]), keep_audio=bool(opts["keep_audio"]),
                         original_volume=float(opts["original_volume"]),
                         bgm_path=bgm, bgm_volume=float(opts["bgm_volume"]),
                         out_path=out, work_dir=work)
        probe = probe_video(out)
        warnings = list(result["warnings"])
        expected = float(result["expected_duration"])
        if abs(probe["duration"] - expected) > C.RENDER_DURATION_TOLERANCE:
            warnings.append(f"成片时长 {probe['duration']:.2f}s 与预期 {expected:.2f}s 偏差较大")
        if (probe["width"], probe["height"]) != (int(plan["width"]), int(plan["height"])):
            raise ComposeError(f"成片分辨率 {probe['width']}x{probe['height']} 与预期不符")
        if not probe["has_audio"]:
            raise ComposeError("成片缺少音轨")
        db.update_render(render_id, status="done", output_path=str(out),
                         probe_json=probe, warnings_json=warnings,
                         expected_duration=expected, finished_at=time.time())
        db.update_project(pid, status="done")
        db.add_event(None, "render",
                     f"合成 #{render_id} 完成：{probe['width']}x{probe['height']}，"
                     f"{probe['duration']:.2f}s" + (f"（{len(warnings)} 条提示）" if warnings else ""),
                     project_id=pid)
    except Exception as e:
        db.update_render(render_id, status="failed", error=str(e)[:800], finished_at=time.time())
        db.update_project(pid, status="generating")
        db.add_event(None, "render", f"合成 #{render_id} 失败：{e}", level="error", project_id=pid)


def save_bgm(project_id: int, filename: str, data: bytes) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in BGM_EXTS:
        raise ValueError(f"不支持的音频格式 {suffix}，可用：{'、'.join(sorted(BGM_EXTS))}")
    if len(data) > BGM_MAX_BYTES:
        raise ValueError("BGM 文件超过 30MB")
    folder = ASSETS_DIR / f"project_{project_id}" / "audio"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"bgm{suffix}"
    for old in folder.glob("bgm.*"):
        old.unlink(missing_ok=True)
    path.write_bytes(data)
    # 用 ffprobe 确认是能解码的音频
    import subprocess
    proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                           "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
                          capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        path.unlink(missing_ok=True)
        raise ValueError("文件里没有可解码的音频流")
    db.update_project(project_id, bgm_path=str(path))
    db.add_event(None, "asset", f"上传 BGM {filename}", project_id=project_id)
    return str(path)


def render_dict(row) -> dict:
    d = dict(row)
    for key in ("options_json", "plan_json", "probe_json", "warnings_json"):
        if d.get(key):
            try:
                d[key] = json.loads(d[key])
            except Exception:
                pass
    d["video_url"] = f"/api/media/renders/{row['id']}" if row["status"] == "done" else None
    d["download_url"] = (f"/api/media/renders/{row['id']}?download=1"
                         if row["status"] == "done" else None)
    return d
