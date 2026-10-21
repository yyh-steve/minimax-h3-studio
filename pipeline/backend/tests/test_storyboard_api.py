"""M2 HTTP 接口测试（TestClient）：项目/素材/分镜/确认闸门全链路。"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture
def client(tmp_path, monkeypatch):
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
    import app.providers as providers
    from app.providers.h3_mock import MockH3
    C.BACKOFF_SCHEDULE_SECONDS = (0.05, 0.1, 0.2)
    providers.set_h3_mock(MockH3(queue_seconds=0.3, run_seconds=0.5))

    from app.main import app
    with TestClient(app) as c:
        yield c


def _make_project(c, seconds=20):
    r = c.post("/api/projects", json={
        "name": "榨汁杯", "ratio": "9:16", "target_seconds": seconds,
        "style": "明亮", "selling_points": ["30秒出汁", "可拆洗"],
        "banned_words": ["最好"]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _upload_image(c, pid, color=(180, 220, 255)):
    buf = io.BytesIO()
    Image.new("RGB", (900, 900), color).save(buf, format="JPEG", quality=90)
    r = c.post(f"/api/projects/{pid}/assets",
               files={"file": ("p.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 200, r.text
    return r.json()


def test_project_crud_and_validation(client):
    c = client
    r = c.post("/api/projects", json={"name": "x", "ratio": "bad", "target_seconds": 30})
    assert r.status_code == 400
    r = c.post("/api/projects", json={"name": "x", "ratio": "9:16", "target_seconds": 5})
    assert r.status_code == 400
    pid = _make_project(c)
    assert c.get(f"/api/projects/{pid}").status_code == 200
    assert any(p["id"] == pid for p in c.get("/api/projects").json())


def test_upload_creates_ratio_variants(client):
    c = client
    pid = _make_project(c)
    res = _upload_image(c, pid)
    assert res["width"] == 900
    variants = {v["variant"]: v for v in res["variants"]}
    assert "crop_9x16" in variants and "pad_9x16" in variants
    assert variants["crop_9x16"]["width"] == 768
    assert variants["crop_9x16"]["height"] == 1344
    # 原图和两个变体都能取到
    assert c.get(f"/api/projects/{pid}/assets/{res['asset_id']}").status_code == 200


def test_generate_validate_confirm_flow(client):
    c = client
    pid = _make_project(c)
    _upload_image(c, pid)
    _upload_image(c, pid, color=(255, 210, 180))

    # 生成
    r = c.post(f"/api/projects/{pid}/storyboards/generate")
    assert r.status_code == 200, r.text
    sb = r.json()
    sb_id = sb["storyboard_id"]
    assert sb["shots"]

    # 校验应无 blocker
    rep = c.post(f"/api/storyboards/{sb_id}/validate").json()
    assert not [i for i in rep["issues"] if i["severity"] == "blocker"]

    # 确认前没有 take
    assert c.get("/api/takes").json() == [] or all(
        t["project_id"] != pid for t in c.get("/api/takes").json())

    # 确认
    conf = c.post(f"/api/storyboards/{sb_id}/confirm")
    assert conf.status_code == 200, conf.text
    data = conf.json()
    assert data["shots"] == len(sb["shots"])
    assert data["estimated_output_seconds"] == rep["total_seconds"]

    # 每镜一个 take，且全部能跑到 accepted
    deadline = time.time() + 40
    takes = []
    while time.time() < deadline:
        takes = [t for t in c.get("/api/takes").json() if t["project_id"] == pid]
        if takes and all(t["status"] == "accepted" for t in takes):
            break
        time.sleep(0.5)
    assert len(takes) == len(sb["shots"])
    assert all(t["status"] == "accepted" for t in takes), [(t["id"], t["status"]) for t in takes]


def test_confirm_rejects_when_blocked(client):
    c = client
    pid = _make_project(c)
    _upload_image(c, pid)
    sb = c.post(f"/api/projects/{pid}/storyboards/generate").json()
    sb_id, key = sb["storyboard_id"], sb["shots"][0]["shot_key"]
    dur = sb["shots"][0]["duration"]
    r = c.put(f"/api/storyboards/{sb_id}/shots/{key}",
              json={"patch": {"audio": {"type": "on_screen",
                                        "text": "字" * (dur * 4 + 20)}}})
    assert r.status_code == 200
    assert c.post(f"/api/storyboards/{sb_id}/confirm").status_code == 409


def test_generate_without_assets_is_400(client):
    c = client
    pid = _make_project(c)
    r = c.post(f"/api/projects/{pid}/storyboards/generate")
    assert r.status_code == 400 and "商品图" in r.json()["detail"]


def test_review_endpoint_returns_issues(client):
    c = client
    pid = _make_project(c)
    _upload_image(c, pid)
    sb = c.post(f"/api/projects/{pid}/storyboards/generate").json()
    r = c.post(f"/api/storyboards/{sb['storyboard_id']}/review")
    assert r.status_code == 200
    assert "issues" in r.json()


def test_save_shot_and_card_endpoints(client):
    c = client
    pid = _make_project(c)
    _upload_image(c, pid)
    sb = c.post(f"/api/projects/{pid}/storyboards/generate").json()
    sb_id, key = sb["storyboard_id"], sb["shots"][0]["shot_key"]
    r = c.put(f"/api/storyboards/{sb_id}/shots/{key}",
              json={"patch": {"camera": "环绕"}})
    assert r.status_code == 200 and r.json()["shots"][0]["camera"] == "环绕"
    card = sb["product_card"]
    card["appearance"] = "人工核对后的外观"
    r = c.put(f"/api/storyboards/{sb_id}/product-card", json={"card": card})
    assert r.status_code == 200 and "人工核对" in r.json()["product_card"]["appearance"]
