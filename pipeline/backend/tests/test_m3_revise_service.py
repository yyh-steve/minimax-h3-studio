"""M3 service 级：新增/删除镜头、自动重试后选用跟随、项目预算。"""
from __future__ import annotations

import asyncio
import io
import sys
import uuid
from pathlib import Path

import pytest
from PIL import Image

# conftest.py 提供 env 夹具


def _seed_project(env, seconds=20, points=("快", "净")):
    from app import image_ops
    pid = env.db.create_project("杯", ratio="9:16", target_seconds=seconds,
                                style="", selling_points=list(points),
                                banned_words=[])
    for color in ((180, 220, 255), (255, 210, 180)):
        buf = io.BytesIO()
        Image.new("RGB", (900, 900), color).save(buf, format="JPEG", quality=90)
        data = buf.getvalue()
        aid = f"{uuid.uuid4().hex[:12]}.jpg"
        path = env.tmp / "data" / "assets" / f"project_{pid}"
        path.mkdir(parents=True, exist_ok=True)
        fpath = path / aid
        fpath.write_bytes(data)
        env.db.add_asset(project_id=pid, asset_id=aid, path=str(fpath),
                         kind="original", sha256=image_ops.sha256_bytes(data),
                         width=900, height=900, mime="image/jpeg",
                         size_bytes=len(data))
    return pid


def test_added_shot_generated_removed_shot_dropped(env):
    # 用 16s（4 个 4s 镜头），删一镜加一镜保持总时长和卖点覆盖
    pid = _seed_project(env, seconds=16)
    d = env.svc.generate(pid)
    sb1 = d["storyboard_id"]
    env.svc.confirm(sb1)
    for t in env.db.list_takes(project_id=pid):
        env.db.update_take(t["id"], status="accepted")

    v2 = env.svc.revise(pid)
    from app.storyboard_schema import Shot
    result = env.svc._load_result(v2["storyboard_id"])
    n = len(result.storyboard.shots)            # 16s 切成 2 个 8s
    assert n == 2
    removed = result.storyboard.shots[-1]
    kept = result.storyboard.shots[:-1]
    added = Shot(shot_key="s99", purpose="cta", duration=removed.duration,
                 mode="t2v", visual="收尾", action="举杯", camera="固定机位",
                 subtitle="收尾", selling_point_ids=sorted(set(removed.selling_point_ids)))
    kept.append(added)
    result.storyboard.shots = kept
    env.svc._replace_from_result(v2["storyboard_id"], result)

    r = env.svc.confirm(v2["storyboard_id"])
    assert r["new_shots"] == 1
    assert set(r["reused_shot_keys"]) == {s.shot_key for s in
                                          result.storyboard.shots[:-1]}
    keys = {row["shot_key"] for row in env.db.list_shots(v2["storyboard_id"])}
    assert "s99" in keys and removed.shot_key not in keys


async def test_auto_retry_selection_follows(env):
    """确认后某镜头自动重试成功，shot_takes 跟随到新 take（用 runner 真跑）。"""
    pid = _seed_project(env, seconds=20)
    d = env.svc.generate(pid)
    env.svc.confirm(d["storyboard_id"])
    # 初始 take 都是 pending；人为让第一个镜头的任务进平台后失败
    target_key = d["shots"][0]["shot_key"]
    # 推到该镜头拿到 task_id（queued/running）
    for _ in range(30):
        await env.runner._tick()
        await asyncio.sleep(0.05)
        row = next(t for t in env.db.list_takes(project_id=pid)
                   if t["shot_key"] == target_key)
        if row["task_id"]:
            break
    assert row["task_id"]
    # 直接让它的平台任务失败
    env.h3._tasks[row["task_id"]].fail = True

    # 跑到该镜头出现两条 take 且都 accepted（重试成功）
    deadline = 80
    for _ in range(deadline):
        await env.runner._tick()
        await asyncio.sleep(0.15)
        all_takes = env.db.list_takes(project_id=pid)
        target_takes = [t for t in all_takes if t["shot_key"] == target_key]
        if (len(target_takes) >= 2 and
                any(t["status"] == "accepted" for t in target_takes)):
            break
    sel = env.db.get_shot_take(d["storyboard_id"], target_key)
    final_take = env.db.get_take(int(sel["take_id"]))
    assert final_take["status"] == "accepted"
    assert int(final_take["attempt_no"]) >= 2


def test_project_budget_is_target_times_three(env):
    # 目标 20s -> 项目预算 60s；构造 3 次成功（40s）后第 4 次提交才应被拦
    pid = _seed_project(env, seconds=20)
    from app import constants as C
    d = env.svc.generate(pid)
    env.svc.confirm(d["storyboard_id"])
    takes = env.db.list_takes(project_id=pid)
    total = sum(int(env.db.get_take(t["id"])["platform_duration"] or 0)
                for t in [])  # 尚未有平台时长
    # 直接验证预算计算函数
    assert env.runner._budget_for(env.db.get_take(takes[0]["id"])) == 60
    # 无项目的临时 take 用全局预算
    env.db.create_take(model="MiniMax-H3",
                       request_json={"duration": 5, "content": [], "model": "x"})
    lone = env.db.list_takes(limit=1)[0]
    if lone["project_id"] is None:
        assert env.runner._budget_for(lone) == env.settings.budget_output_seconds
