"""M3 HTTP 测试：改稿复用、镜头看板、自动重试后选用跟随。"""
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


def _project(c, seconds=20):
    r = c.post("/api/projects", json={"name": "杯", "ratio": "9:16",
                                      "target_seconds": seconds,
                                      "selling_points": ["快", "净"]})
    assert r.status_code == 200
    return r.json()["id"]


def _image(c, pid):
    buf = io.BytesIO()
    Image.new("RGB", (900, 900), (180, 220, 255)).save(buf, format="JPEG")
    r = c.post(f"/api/projects/{pid}/assets",
               files={"file": ("p.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 200


def _generate_confirm(c, pid):
    sb = c.post(f"/api/projects/{pid}/storyboards/generate").json()
    r = c.post(f"/api/storyboards/{sb['storyboard_id']}/confirm")
    assert r.status_code == 200, r.text
    return sb, r.json()


def _wait_all_accepted(c, pid, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        b = c.get(f"/api/projects/{pid}/board").json()
        if b["all_accepted"]:
            return b
        time.sleep(0.4)
    raise AssertionError("shots not all accepted: " +
                         str([(s["shot_key"], s["status"]) for s in
                              c.get(f'/api/projects/{pid}/board').json()["shots"]]))


def test_board_first_confirmation(client):
    c = client
    pid = _project(c)
    _image(c, pid)
    sb, conf = _generate_confirm(c, pid)
    assert conf["new_shots"] == len(sb["shots"]) and conf["reused_shots"] == 0
    b = _wait_all_accepted(c, pid)
    assert b["total"] == len(sb["shots"])
    assert all(s["video_url"] for s in b["shots"])
    assert all(not s["reused"] for s in b["shots"])


def test_revise_reuses_unchanged_shots(client):
    c = client
    pid = _project(c)
    _image(c, pid)
    sb, _ = _generate_confirm(c, pid)
    _wait_all_accepted(c, pid)
    first_takes = {s["shot_key"]: s["take_id"] for s in
                   c.get(f"/api/projects/{pid}/board").json()["shots"]}

    # 开新版本，只改一个镜头的运镜
    v2 = c.post(f"/api/projects/{pid}/storyboards/revise").json()
    changed = v2["shots"][0]["shot_key"]
    r = c.put(f"/api/storyboards/{v2['storyboard_id']}/shots/{changed}",
              json={"patch": {"camera": "环绕一圈"}})
    assert r.status_code == 200
    conf = c.post(f"/api/storyboards/{v2['storyboard_id']}/confirm").json()
    assert conf["new_shots"] == 1
    assert conf["reused_shots"] == len(sb["shots"]) - 1
    assert changed not in conf["reused_shot_keys"]

    # 看板：复用镜头指向旧 take，变化镜头指向新 take
    b = c.get(f"/api/projects/{pid}/board").json()
    by = {s["shot_key"]: s for s in b["shots"]}
    for key, shot in by.items():
        if key == changed:
            assert shot["reused"] is False
            assert shot["take_id"] != first_takes[key]
        else:
            assert shot["reused"] is True
            assert shot["take_id"] == first_takes[key]

    # 变化镜头也跑完后，整版就绪；总 take 数只比首版多 1
    b = _wait_all_accepted(c, pid)
    takes = [t for t in c.get("/api/takes").json() if t["project_id"] == pid]
    assert len(takes) == len(sb["shots"]) + 1


def test_revise_no_change_creates_nothing(client):
    c = client
    pid = _project(c)
    _image(c, pid)
    sb, _ = _generate_confirm(c, pid)
    _wait_all_accepted(c, pid)
    n_before = len([t for t in c.get("/api/takes").json() if t["project_id"] == pid])
    v2 = c.post(f"/api/projects/{pid}/storyboards/revise").json()
    conf = c.post(f"/api/storyboards/{v2['storyboard_id']}/confirm").json()
    assert conf["new_shots"] == 0 and conf["reused_shots"] == len(sb["shots"])
    n_after = len([t for t in c.get("/api/takes").json() if t["project_id"] == pid])
    assert n_after == n_before


def test_revise_requires_confirmed(client):
    c = client
    pid = _project(c)
    _image(c, pid)
    c.post(f"/api/projects/{pid}/storyboards/generate")
    # 还没确认，不能 revise
    r = c.post(f"/api/projects/{pid}/storyboards/revise")
    assert r.status_code == 400
