"""M4 HTTP：质检结论上看板、人工采纳、重新质检、合成队列与成片播放。"""
from __future__ import annotations

import io
import sys
import subprocess
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
    monkeypatch.setenv("M3_MOCK_REVIEW", "pass")
    monkeypatch.setenv("MAX_CONCURRENCY", "4")
    monkeypatch.setenv("AUTO_RETRY_LIMIT", "1")
    monkeypatch.setenv("BUDGET_OUTPUT_SECONDS", "600")
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    from app import constants as C
    import app.providers as providers
    from app.providers.h3_mock import MockH3
    from app.providers.m3_mock import MockM3
    C.BACKOFF_SCHEDULE_SECONDS = (0.05, 0.1, 0.2)
    providers.set_h3_mock(MockH3(queue_seconds=0.3, run_seconds=0.5))
    providers._m3_mock = MockM3()

    from app.main import app
    with TestClient(app) as c:
        yield c


def _project(c, seconds=16):
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


def _wait_board_done(c, pid, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        b = c.get(f"/api/projects/{pid}/board").json()
        if b["all_accepted"]:
            return b
        time.sleep(0.3)
    raise AssertionError(c.get(f"/api/projects/{pid}/board").text)


def test_board_carries_qc_results_and_render_ready(client):
    c = client
    pid = _project(c)
    _image(c, pid)
    sb = c.post(f"/api/projects/{pid}/storyboards/generate").json()
    c.post(f"/api/storyboards/{sb['storyboard_id']}/confirm")
    b = _wait_board_done(c, pid)
    for s in b["shots"]:
        assert s["qc"]["tech_passed"] is True
        assert s["qc"]["m3_verdict"] == "pass"
        assert s["status"] == "accepted"


def test_render_not_ready_before_all_accepted(client):
    c = client
    pid = _project(c)
    _image(c, pid)
    r = c.post(f"/api/projects/{pid}/renders", json={})
    # 没确认没素材就绪，409
    assert r.status_code == 409


def test_render_cut_and_fade_and_media_download(client, tmp_path):
    c = client
    pid = _project(c)
    _image(c, pid)
    sb = c.post(f"/api/projects/{pid}/storyboards/generate").json()
    c.post(f"/api/storyboards/{sb['storyboard_id']}/confirm")
    _wait_board_done(c, pid)

    # 硬切：16s 两镜
    r = c.post(f"/api/projects/{pid}/renders",
               json={"transition": "cut", "subtitles": True, "keep_audio": True,
                     "use_bgm": False})
    assert r.status_code == 200
    rid_cut = r.json()["id"]

    # 上传一段 BGM 再合成淡入淡出
    bgm = tmp_path / "b.m4a"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=f=220:duration=3", "-c:a", "aac", str(bgm)],
                   check=True)
    up = c.post(f"/api/projects/{pid}/bgm",
                files={"file": ("bgm.m4a", bgm.read_bytes(), "audio/mp4")})
    assert up.status_code == 200

    r2 = c.post(f"/api/projects/{pid}/renders",
                json={"transition": "fade", "subtitles": True, "keep_audio": True,
                      "use_bgm": True, "bgm_volume": 0.3})
    assert r2.status_code == 200
    rid_fade = r2.json()["id"]

    final = {}
    deadline = time.time() + 180
    while time.time() < deadline:
        for rid, want in ((rid_cut, 16), (rid_fade, 15.7)):
            d = c.get(f"/api/renders/{rid}").json()
            final[rid] = d
        if all(d["status"] in ("done", "failed") for d in final.values()):
            if all(d["status"] == "done" for d in final.values()):
                break
        time.sleep(0.5)
    assert final[rid_cut]["status"] == "done", final[rid_cut]
    assert final[rid_fade]["status"] == "done", final[rid_fade]
    assert abs(final[rid_cut]["probe_json"]["duration"] - 16) < 0.3
    assert abs(final[rid_fade]["probe_json"]["duration"] - 15.7) < 0.3

    # 播放 / 下载
    media = c.get(f"/api/media/renders/{rid_cut}")
    assert media.status_code == 200 and media.headers["content-type"].startswith("video/mp4")
    dl = c.get(f"/api/media/renders/{rid_cut}?download=1", follow_redirects=False)
    assert dl.status_code == 200 and "attachment" in dl.headers.get("content-disposition", "")

    # 渲染列表
    lst = c.get(f"/api/projects/{pid}/renders").json()
    assert {r["id"] for r in lst} == {rid_cut, rid_fade}


def test_manual_accept_via_api_and_requalify(client):
    c = client
    # 让 M3 看片拿不准 -> review
    c.put("/api/settings", json={"m3_mock_review": "uncertain"})
    pid = _project(c, seconds=16)
    _image(c, pid)
    sb = c.post(f"/api/projects/{pid}/storyboards/generate").json()
    c.post(f"/api/storyboards/{sb['storyboard_id']}/confirm")
    # 等所有镜头到 review
    deadline = time.time() + 90
    while time.time() < deadline:
        b = c.get(f"/api/projects/{pid}/board").json()
        if b["shots"] and all(s["status"] == "review" for s in b["shots"]):
            break
        time.sleep(0.3)
    b = c.get(f"/api/projects/{pid}/board").json()
    shot = b["shots"][0]
    assert shot["can_accept"] and shot["take_id"]

    r = c.post(f"/api/takes/{shot['take_id']}/accept", json={"note": "看着没问题"})
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    # 重新质检：状态回到 downloaded，下一轮会再跑一遍（切回 pass）
    c.put("/api/settings", json={"m3_mock_review": "pass"})
    r2 = c.post(f"/api/takes/{shot['take_id']}/requalify")
    assert r2.status_code == 200 and r2.json()["status"] == "downloaded"
    deadline = time.time() + 60
    while time.time() < deadline:
        t = c.get(f"/api/takes/{shot['take_id']}").json()
        if t["status"] == "accepted":
            break
        time.sleep(0.3)
    assert c.get(f"/api/takes/{shot['take_id']}").json()["status"] == "accepted"


def test_bgm_validation_rejects_non_audio(client):
    c = client
    pid = _project(c)
    r = c.post(f"/api/projects/{pid}/bgm",
               files={"file": ("x.txt", b"not audio", "text/plain")})
    assert r.status_code == 400
