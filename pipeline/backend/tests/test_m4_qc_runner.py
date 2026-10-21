"""M4：技术检查 + M3 看片驱动的 take 状态流转、自动重试、人工采纳。"""
from __future__ import annotations

import asyncio
import io
import sys
import time
import uuid
from pathlib import Path

import pytest
from PIL import Image

from tests.test_runner import make_take, pump, set_scenario, wait_status


def _accepted_ids(env, project_id):
    return {t["shot_key"]: t for t in env.db.list_takes(project_id=project_id)
            if t["status"] == "accepted"}


def _seed_project(env, seconds=20, points=("快", "净"), m3_review="pass"):
    from app import image_ops
    env.settings.m3_take_review = True
    env.settings.m3_mock_review = m3_review
    env.m3.set_review_mode(m3_review)
    pid = env.db.create_project("杯", ratio="9:16", target_seconds=seconds,
                                style="", selling_points=list(points), banned_words=[])
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
                         width=900, height=900, mime="image/jpeg", size_bytes=len(data))
    return pid


async def _wait_project_final(env, pid, timeout=120):
    """等该项目所有镜头都有一条已采纳的成片（旧 rejected take 会仍在列表里）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        await env.runner._tick()
        await asyncio.sleep(0.12)
        shots = {t["shot_key"] for t in env.db.list_takes(project_id=pid)}
        if shots and all(any(t["shot_key"] == k and t["status"] == "accepted"
                             for t in env.db.list_takes(project_id=pid)) for k in shots):
            return
    raise AssertionError([(t["shot_key"], t["status"], t["attempt_no"])
                          for t in env.db.list_takes(project_id=pid)])


async def _wait_project(env, pid, want_status, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        await env.runner._tick()
        await asyncio.sleep(0.15)
        takes = env.db.list_takes(project_id=pid)
        if takes and all(t["status"] == want_status for t in takes):
            return takes
    raise AssertionError([(t["shot_key"], t["status"]) for t in env.db.list_takes(project_id=pid)])


async def test_pipeline_pass_tech_and_m3(env):
    set_scenario(env, "success")
    pid = _seed_project(env)
    d = env.svc.generate(pid)
    env.svc.confirm(d["storyboard_id"])
    takes = await _wait_project(env, pid, "accepted")
    for t in takes:
        qc = __import__("json").loads(t["qc_json"])
        assert qc["tech"]["passed"] is True
        assert qc["m3"]["final_verdict"] == "pass"
        assert qc["decision"]["by"] in ("m3", "tech")
    # 假 M3 确实收到了代理视频和 fps=2
    assert env.m3.review_calls == len(takes)
    assert env.m3.last_review_request["fps"] == 2.0
    assert env.m3.last_review_request["video_bytes"] > 1000


async def test_m3_fail_once_triggers_retry_then_accepted(env):
    set_scenario(env, "success")
    pid = _seed_project(env, m3_review="fail_once")
    d = env.svc.generate(pid)
    env.svc.confirm(d["storyboard_id"])
    # 每镜：第一次 M3 fail -> 自动重试 -> 第二次 pass -> accepted
    await _wait_project_final(env, pid)
    # 每镜 2 条 take（根 rejected + 重试 accepted），镜头选用跟随到重试
    takes = env.db.list_takes(project_id=pid)
    assert len(takes) == 2 * len(d["shots"])
    for shot in d["shots"]:
        sel = env.db.get_shot_take(d["storyboard_id"], shot["shot_key"])
        assert env.db.get_take(int(sel["take_id"]))["status"] == "accepted"
        assert int(env.db.get_take(int(sel["take_id"]))["attempt_no"]) == 2
    # 预算里要算上重试花掉的秒数
    assert env.db.used_output_seconds(pid) >= 40


async def test_m3_uncertain_goes_to_review(env):
    set_scenario(env, "success")
    pid = _seed_project(env, m3_review="uncertain")
    d = env.svc.generate(pid)
    env.svc.confirm(d["storyboard_id"])
    for _ in range(120):
        await env.runner._tick()
        await asyncio.sleep(0.12)
        takes = env.db.list_takes(project_id=pid)
        if takes and all(t["status"] == "review" for t in takes):
            break
    else:
        raise AssertionError([t["status"] for t in env.db.list_takes(project_id=pid)])
    # 人工采纳后变 accepted，选用指向它
    first = env.db.list_takes(project_id=pid)[0]
    assert first["local_path"]
    from app.pipeline.runner import Runner
    # 直接调 service 层不方便，走数据库模拟人工采纳逻辑：用 routes 同款 db 操作
    import json
    qc = json.loads(first["qc_json"])
    qc["decision"] = {"result": "accepted", "by": "human"}
    env.db.update_take(first["id"], status="accepted", qc_json=qc, error_json=None)
    env.db.retarget_selection(env.db.take_lineage_ids(first["id"]), first["id"],
                              keep_accepted=False)
    sel = env.db.get_shot_take(d["storyboard_id"], first["shot_key"])
    assert int(sel["take_id"]) == first["id"]


async def test_tech_black_head_rejected_and_retried(env):
    set_scenario(env, "black_once")
    pid = _seed_project(env, seconds=16, m3_review="pass")
    d = env.svc.generate(pid)
    env.svc.confirm(d["storyboard_id"])
    await _wait_project_final(env, pid, timeout=150)
    takes = env.db.list_takes(project_id=pid)
    # 第一次的黑片被技术检查判 rejected（black_once 只影响每镜的第一次任务，自动重试正常）
    bad = [t for t in takes if t["qc_json"] and
           __import__("json").loads(t["qc_json"]).get("decision", {}).get("by") == "tech"
           and t["status"] == "rejected"]
    assert bad


async def test_m3_review_disabled_tech_only_accepts(env):
    set_scenario(env, "success")
    env.settings.m3_take_review = False
    tid = make_take(env)
    await wait_status(env, tid, {"accepted", "review"}, rounds=60)
    row = env.db.get_take(tid)
    assert row["status"] == "accepted"
    qc = __import__("json").loads(row["qc_json"])
    assert qc["m3"]["skipped"] is True


async def test_auto_retry_streak_resets_after_manual_retry(env):
    from app import constants as C
    set_scenario(env, "success")
    tid = make_take(env)
    await wait_status(env, tid, {"accepted"}, rounds=60)
    # 把它标记为不通过，模拟一次自动重试（auto_used=0 -> 可以）
    new1 = await env.runner._spawn_retry_take(tid, "test")
    env.db.update_take(new1, status="pending", next_run_at=None)
    second = await env.runner._spawn_retry_take(new1, "test again")
    assert second is None  # 已到自动重试上限
    # 人工重试重置连续自动重试计数
    manual = env.db.create_take(model=env.db.get_take(tid)["model"],
                                request_json=__import__("json").loads(
                                    env.db.get_take(tid)["request_json"]),
                                shot_key=env.db.get_take(tid)["shot_key"],
                                attempt_no=3, parent_take_id=new1, source="manual_retry")
    third = await env.runner._spawn_retry_take(manual, "after manual")
    assert third is not None


async def test_budget_blocks_auto_retry_only(env):
    # 目标 16s -> 预算 48s。首版 2 镜共 16s 采纳后，再追加一笔 40s 的单镜消耗（56>48），
    # 之后的自动重试（+8s）应在提交前被预算拦下转人工。
    pid = _seed_project(env, seconds=16)
    d = env.svc.generate(pid)
    env.svc.confirm(d["storyboard_id"])
    await _wait_project(env, pid, "accepted")
    extra = env.db.create_take(model="x", request_json={"duration": 40, "content": []},
                               project_id=pid, shot_key="budget", source="api")
    env.db.update_take(extra, status="accepted", platform_duration=40)
    target = env.db.list_takes(project_id=pid)[0]
    env.db.update_take(target["id"], status="rejected")
    env.settings.auto_retry_limit = 5
    new_id = await env.runner._spawn_retry_take(target["id"], "budget test")
    assert new_id is not None
    for _ in range(40):
        await env.runner._tick()
        await asyncio.sleep(0.05)
        if env.db.get_take(new_id)["status"] == "review":
            break
    row = env.db.get_take(new_id)
    assert row["status"] == "review" and "预算" in row["error_json"]

    # 人工重试不受预算限制：直接建 pending，队列不会再把它改成 review
    import json
    manual = env.db.create_take(model=row["model"], request_json=json.loads(row["request_json"]),
                                project_id=pid, shot_key=row["shot_key"], attempt_no=3,
                                parent_take_id=new_id, source="manual_retry")
    for _ in range(10):
        await env.runner._tick()
        await asyncio.sleep(0.05)
        assert env.db.get_take(manual)["status"] in ("submitting", "queued", "running",
                                                      "succeeded", "downloading", "downloaded",
                                                      "checking", "accepted", "backoff")
