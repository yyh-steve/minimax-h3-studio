"""M2 分镜链路测试：项目 -> 素材 -> M3 生成 -> 规则 -> 确认闸门 -> 每镜一个 take。

全部用 mock（M3/H3 都是假的），不花钱。
"""
from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("H3_PROVIDER", "mock")
    monkeypatch.setenv("M3_PROVIDER", "mock")
    monkeypatch.setenv("MOCK_SCENARIO", "success")
    monkeypatch.setenv("MAX_CONCURRENCY", "4")
    monkeypatch.setenv("AUTO_RETRY_LIMIT", "1")
    monkeypatch.setenv("BUDGET_OUTPUT_SECONDS", "600")
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    from app import constants as C
    from app import db
    import app.providers as providers
    from app.providers.h3_mock import MockH3
    from app.providers.m3_mock import MockM3
    from app.pipeline.storyboard_service import StoryboardService
    C.BACKOFF_SCHEDULE_SECONDS = (0.05, 0.1, 0.2)

    db.ensure_dirs(); db.connect()
    h3 = MockH3(queue_seconds=0.3, run_seconds=0.5)
    m3 = MockM3()
    providers.set_h3_mock(h3)
    providers._m3_mock = m3

    from app.config import Settings
    settings = Settings(provider="mock", m3_provider="mock",
                        max_concurrency=4, auto_retry_limit=1,
                        budget_output_seconds=600)
    svc = StoryboardService(settings)

    import base64, uuid
    from app import image_ops
    pid = db.create_project("测试榨汁杯", ratio="9:16", target_seconds=20,
                            style="明亮", selling_points=["30秒出汁", "可拆洗"],
                            banned_words=["最好"], h3_model="MiniMax-H3",
                            draft_resolution="768P")
    # 造两张 1000x1000 的商品图并落库（含画幅变体）
    asset_ids = []
    for color in ((180, 220, 255), (255, 210, 180)):
        buf = io.BytesIO()
        Image.new("RGB", (1000, 1000), color).save(buf, format="JPEG", quality=90)
        data = buf.getvalue()
        aid = f"{uuid.uuid4().hex[:12]}.jpg"
        path = Path(db.ASSETS_DIR) / f"project_{pid}" if False else tmp_path / "data" / "assets" / f"project_{pid}"
        path.mkdir(parents=True, exist_ok=True)
        fpath = path / aid
        fpath.write_bytes(data)
        db.add_asset(project_id=pid, asset_id=aid, path=str(fpath), kind="original",
                     sha256=image_ops.sha256_bytes(data), width=1000, height=1000,
                     mime="image/jpeg", size_bytes=len(data))
        for v in image_ops.make_variants(fpath, "9:16", path / "variants"):
            vid = Path(v["path"]).name
            db.add_asset(project_id=pid, asset_id=vid, path=v["path"], kind="variant",
                         variant=v["variant"], parent_asset_id=aid, sha256=v["sha256"],
                         width=v["width"], height=v["height"], mime=v["mime"],
                         size_bytes=v["bytes"])
        asset_ids.append(aid)

    yield _Bundle(
        db=db, settings=settings, svc=svc, h3=h3, m3=m3, pid=pid,
        assets=asset_ids, tmp=tmp_path)


class _Bundle:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _gen(env):
    detail = env.svc.generate(env.pid)
    return detail


def test_generate_covers_points_and_duration(env):
    d = _gen(env)
    shots = d["shots"]
    assert shots, "应当生成镜头"
    total = sum(s["duration"] for s in shots)
    assert abs(total - 20) <= 1
    assert all(4 <= s["duration"] <= 15 for s in shots)
    covered = {p for s in shots for p in s["selling_point_ids"]}
    assert {"sp1", "sp2"}.issubset(covered)
    # 图片引用应已解析成真实 asset_id（img1 -> 第一张原图）
    used = {a for s in shots for a in
            [s["images"].get("first_frame"), s["images"].get("last_frame"),
             *s["images"].get("references", [])] if a}
    assert used, "至少有镜头引用商品图"


def test_no_h3_before_confirm(env):
    _gen(env)
    # 生成、校验阶段：项目下没有任何 take
    assert env.db.list_takes(project_id=env.pid) == []
    active = env.db.get_project(env.pid)["active_storyboard_id"]
    report = env.svc.validate(active)
    assert not [i for i in report["issues"] if i["severity"] == "blocker"]
    # 再评审一次也不产生 take
    env.svc.review(active)
    assert env.db.list_takes(project_id=env.pid) == []


def test_confirm_blocked_when_rule_issue(env):
    d = _gen(env)
    sb_id = d["storyboard_id"]
    # 人为把某镜头口播改成超长 -> blocker，确认应被拒
    shot = d["shots"][0]
    env.svc.save_shot(sb_id, shot["shot_key"],
                      {"audio": {"type": "on_screen",
                                 "text": "字" * (shot["duration"] * 4 + 20)}})
    report = env.svc.validate(sb_id)
    assert any(i["severity"] == "blocker" and i["code"] == "voice_too_long"
               for i in report["issues"])
    with pytest.raises(PermissionError):
        env.svc.confirm(sb_id)
    assert env.db.list_takes(project_id=env.pid) == []


def test_confirm_creates_one_take_per_shot(env):
    d = _gen(env)
    sb_id = d["storyboard_id"]
    report = env.svc.validate(sb_id)
    assert not [i for i in report["issues"] if i["severity"] == "blocker"]
    result = env.svc.confirm(sb_id)
    takes = env.db.list_takes(project_id=env.pid)
    assert len(takes) == len(d["shots"]) == result["shots"]
    assert result["estimated_output_seconds"] == sum(s["duration"] for s in d["shots"])
    # 每个 take 都有指纹和完整请求快照，shot_key 对齐
    keys = {t["shot_key"] for t in takes}
    assert keys == {s["shot_key"] for s in d["shots"]}
    for t in takes:
        req = json.loads(t["request_json"])
        assert req["model"] == "MiniMax-H3"
        assert req["duration"] in range(4, 16)
        assert t["fingerprint"]
    # 分镜确认；项目进入生成中（M3 改稿复用从此打开）
    assert env.db.get_storyboard(sb_id)["status"] == "confirmed"
    assert env.db.get_project(env.pid)["status"] == "generating"


def test_revise_only_reruns_changed_shots(env):
    """M3：确认并出片后改一镜，二次确认只新建变化镜头的 take，其余按指纹复用。"""
    d = _gen(env)
    sb1 = d["storyboard_id"]
    r1 = env.svc.confirm(sb1)
    assert r1["new_shots"] == len(d["shots"])
    # 模拟 runner：首版 take 全部下载校验通过
    for t in env.db.list_takes(project_id=env.pid):
        env.db.update_take(t["id"], status="accepted")
    for s in d["shots"]:
        sel = env.db.get_shot_take(sb1, s["shot_key"])
        assert sel is not None and sel["origin"] == "new"

    d2 = env.svc.revise(env.pid)
    sb2 = d2["storyboard_id"]
    changed = d2["shots"][1]["shot_key"]
    env.svc.save_shot(sb2, changed, {"camera": "环绕一圈"})

    r2 = env.svc.confirm(sb2)
    assert r2["new_shots"] == 1
    assert r2["reused_shots"] == len(d["shots"]) - 1
    assert set(r2["reused_shot_keys"]) == {s["shot_key"] for s in d2["shots"]
                                           if s["shot_key"] != changed}
    sel = env.db.get_shot_take(sb2, r2["reused_shot_keys"][0])
    assert sel["origin"] == "reused"
    assert len(env.db.list_takes(project_id=env.pid)) == len(d["shots"]) + 1
    assert env.db.get_storyboard(sb1)["status"] == "superseded"


def test_revise_without_change_creates_zero_takes(env):
    d = _gen(env)
    sb1 = d["storyboard_id"]
    env.svc.confirm(sb1)
    for t in env.db.list_takes(project_id=env.pid):
        env.db.update_take(t["id"], status="accepted")
    d2 = env.svc.revise(env.pid)
    r2 = env.svc.confirm(d2["storyboard_id"])
    assert r2["new_shots"] == 0
    assert r2["reused_shots"] == len(d["shots"])
    assert len(env.db.list_takes(project_id=env.pid)) == len(d["shots"])


def test_failed_take_not_reused(env):
    """没成功（accepted）的镜头不能复用，改稿确认时会重新生成。"""
    d = _gen(env)
    sb1 = d["storyboard_id"]
    env.svc.confirm(sb1)
    # 只让 s1 成功，s2 失败
    takes = {t["shot_key"]: t for t in env.db.list_takes(project_id=env.pid)}
    env.db.update_take(takes["s1"]["id"], status="accepted")
    env.db.update_take(takes["s2"]["id"], status="review")
    d2 = env.svc.revise(env.pid)
    r2 = env.svc.confirm(d2["storyboard_id"])
    assert set(r2["reused_shot_keys"]) == {"s1"}
    assert r2["new_shots"] == 1


def test_save_shot_and_product_card_persist(env):
    d = _gen(env)
    sb_id = d["storyboard_id"]
    key = d["shots"][0]["shot_key"]
    env.svc.save_shot(sb_id, key, {"camera": "环绕", "action": "只做一个动作"})
    again = env.svc.get_detail(sb_id)
    shot = next(s for s in again["shots"] if s["shot_key"] == key)
    assert shot["camera"] == "环绕" and shot["action"] == "只做一个动作"

    env.svc.save_product_card(sb_id, {**again["product_card"],
                                      "appearance": "奶白磨砂杯身（人工核对）"})
    card = env.svc.get_detail(sb_id)["product_card"]
    assert "人工核对" in card["appearance"]


def test_invalid_shot_rejected(env):
    d = _gen(env)
    sb_id, key = d["storyboard_id"], d["shots"][0]["shot_key"]
    with pytest.raises(Exception):
        env.svc.save_shot(sb_id, key, {"duration": 30})  # 超 15s 上限
    with pytest.raises(Exception):
        env.svc.save_shot(sb_id, key, {"mode": "bad_mode"})


def test_review_flags_voice_overflow(env):
    d = _gen(env)
    sb_id = d["storyboard_id"]
    shot = d["shots"][0]
    env.svc.save_shot(sb_id, shot["shot_key"],
                      {"audio": {"type": "on_screen",
                                 "text": "字" * (shot["duration"] * 4 + 10)}})
    detail = env.svc.get_detail(sb_id)
    review = env.svc.review(sb_id)
    assert any(i["code"] == "voice_too_long" and i["severity"] == "blocker"
               for i in review["issues"])


def test_h3_request_mode_rules(env):
    """首帧方式 ratio=adaptive；文生方式 ratio=具体比例。"""
    from app.storyboard_schema import (StoryboardResult, build_h3_request)
    d = _gen(env)
    card = type(env.svc)._load_result(env.svc, d["storyboard_id"]).product_card
    detail = env.svc.get_detail(d["storyboard_id"])
    assets = {a["asset_id"]: "data:image/jpeg;base64,AAAA"
              for a in env.db.list_assets(env.pid)}
    for shot in detail["shots"]:
        from app.storyboard_schema import Shot
        req = build_h3_request(Shot.model_validate(shot), card, ratio="9:16",
                               style="", model="MiniMax-H3", resolution="768P",
                               asset_data_uri=assets)
        if shot["mode"] in ("first_frame", "first_last"):
            assert req["ratio"] == "adaptive"
            assert any(c.get("role") == "first_frame" for c in req["content"])
        elif shot["mode"] == "t2v":
            assert req["ratio"] == "9:16"
            assert all(c["type"] == "text" for c in req["content"])
