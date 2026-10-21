"""HTTP 接口测试：参数校验、手动重试、provider 配置。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("H3_PROVIDER", "mock")
    monkeypatch.setenv("MAX_CONCURRENCY", "4")
    monkeypatch.setenv("AUTO_RETRY_LIMIT", "1")
    monkeypatch.setenv("BUDGET_OUTPUT_SECONDS", "300")
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    monkeypatch.setenv("MOCK_SCENARIO", "success")
    from app import constants as C
    from app.providers.h3_mock import MockH3
    import app.providers as providers
    C.BACKOFF_SCHEDULE_SECONDS = (0.05, 0.1, 0.2)
    fast = MockH3(queue_seconds=0.3, run_seconds=0.5)
    providers.set_h3_mock(fast)

    from app.main import app
    with TestClient(app) as c:
        yield c, fast


def base_payload(**overrides):
    p = {"prompt": "水果落入杯中，镜头缓慢推近，画面无文字", "duration": 5,
         "resolution": "768P", "ratio": "9:16", "model": "MiniMax-H3", "images": []}
    p.update(overrides)
    return p


def poll(client, take_id, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/takes/{take_id}")
        data = r.json()
        if data["status"] in ("accepted", "review", "needs_edit", "paused",
                              "cancelled", "expired", "rejected"):
            return data
        time.sleep(0.2)
    raise AssertionError("timeout waiting terminal status: " + data["status"])


def test_health_mock_ok(client):
    c, _ = client
    data = c.get("/api/health").json()
    assert data["ok"] is True and data["provider"] == "mock"


def test_text_to_video_requires_concrete_ratio(client):
    c, _ = client
    r = c.post("/api/takes", json=base_payload(ratio="adaptive"))
    assert r.status_code == 400 and "ratio" in r.json()["detail"]


def test_mixed_keyframe_and_reference_rejected(client):
    c, _ = client
    body = base_payload(images=[
        {"url": "data:image/png;base64,AAAA", "role": "first_frame"},
        {"url": "data:image/png;base64,AAAA", "role": "reference_image"},
    ])
    r = c.post("/api/takes", json=body)
    assert r.status_code == 400 and "混用" in r.json()["detail"]


def test_last_frame_requires_first_frame(client):
    c, _ = client
    body = base_payload(images=[{"url": "data:image/png;base64,AAAA", "role": "last_frame"}])
    r = c.post("/api/takes", json=body)
    assert r.status_code == 400 and "成对" in r.json()["detail"]


def test_duration_out_of_range(client):
    c, _ = client
    r = c.post("/api/takes", json=base_payload(duration=3))
    assert r.status_code == 400
    r = c.post("/api/takes", json=base_payload(duration=16))
    assert r.status_code == 400


def test_empty_prompt_rejected(client):
    c, _ = client
    r = c.post("/api/takes", json=base_payload(prompt="   "))
    assert r.status_code == 400


def test_manual_retry_creates_new_take(client):
    c, fast = client
    c.put("/api/settings", json={"mock_scenario": "sensitive"})
    tid = c.post("/api/takes", json=base_payload()).json()["id"]
    first = poll(c, tid)
    assert first["status"] == "needs_edit"

    # 手动重试不受自动重试上限约束，切回成功场景后重试
    c.put("/api/settings", json={"mock_scenario": "success"})
    new_id = c.post(f"/api/takes/{tid}/retry", json={"message": "改了提示词"}).json()["id"]
    assert new_id != tid
    second = poll(c, new_id)
    assert second["status"] == "accepted"
    assert c.get(f"/api/takes/{new_id}").json()["parent_take_id"] == tid

    # 成片可播放
    media = c.get(f"/api/media/takes/{new_id}")
    assert media.status_code == 200
    assert media.headers["content-type"].startswith("video/mp4")


def test_settings_switch_scenario(client):
    c, _ = client
    r = c.put("/api/settings", json={"mock_scenario": "balance"})
    assert r.status_code == 200 and r.json()["mock_scenario"] == "balance"
    tid = c.post("/api/takes", json=base_payload()).json()["id"]
    data = poll(c, tid)
    assert data["status"] == "paused"


def test_official_without_key_is_rejected(client, monkeypatch):
    c, _ = client
    c.put("/api/settings", json={"provider": "official", "api_key": ""})
    r = c.post("/api/takes", json=base_payload())
    assert r.status_code == 400 and "MINIMAX_API_KEY" in r.json()["detail"]
