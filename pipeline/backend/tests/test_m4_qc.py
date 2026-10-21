"""M4 技术质检：ffmpeg 检测解析、规则判定。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.qc import evaluate, parse_detect_log, run_tech_check
from app.media import probe_video
from media_fixtures import make_clip


def _probe(path):
    return probe_video(path)


@pytest.fixture(scope="module")
def qc_clips(tmp_path_factory):
    directory = tmp_path_factory.mktemp("qc-clips")
    return {
        "black2.mp4": make_clip(
            directory, "black2.mp4",
            video=("testsrc2=size=144x256:rate=24:duration=3,"
                   "tpad=start_duration=2:start_mode=add:color=black"),
            duration=5,
        ),
        "frozen_silent.mp4": make_clip(
            directory, "frozen_silent.mp4",
            video="color=c=gray:size=144x256:rate=24",
            duration=5,
            audio="anullsrc=channel_layout=stereo:sample_rate=48000",
        ),
        "normal.mp4": make_clip(
            directory, "normal.mp4",
            video="testsrc2=size=144x256:rate=24",
            duration=5,
        ),
    }


def test_parse_log_segments_to_eof():
    log = ("[blackdetect @ x] black_start:0 black_end:2 black_duration:2\n"
           "[freezedetect @ x] lavfi.freezedetect.freeze_start: 3\n"
           "[silencedetect @ x] silence_start: 0\n"
           "silence_end: 5.024 | silence_duration: 5.024\n")
    seg = parse_detect_log(log)
    assert seg["black"] == [[0.0, 2.0]]
    assert seg["freeze"] == [[3.0, None]]
    assert seg["silence"] == [[0.0, 5.024]]


def test_evaluate_normal_passes():
    probe = {"duration": 10.0, "width": 768, "height": 1344, "has_audio": True}
    r = evaluate(probe=probe, segments={"black": [], "freeze": [], "silence": []},
                 expected_duration=10, expected_ratio="9:16", expect_voice=False)
    assert r.passed and r.issues == []


def test_evaluate_black_and_freeze_block():
    probe = {"duration": 5.0, "width": 768, "height": 1344, "has_audio": True}
    seg = {"black": [[0.0, 2.0]], "freeze": [[0.0, 4.0]], "silence": []}
    r = evaluate(probe=probe, segments=seg, expected_duration=5,
                 expected_ratio="9:16", expect_voice=False)
    codes = {i.code for i in r.issues if i.severity == "blocker"}
    assert "black_frames" in codes and "frozen" in codes
    assert not r.passed


def test_evaluate_too_short_and_voice_missing():
    probe = {"duration": 8.2, "width": 768, "height": 1344, "has_audio": False}
    seg = {"black": [], "freeze": [], "silence": []}
    r = evaluate(probe=probe, segments=seg, expected_duration=10,
                 expected_ratio="9:16", expect_voice=True)
    codes = {i.code for i in r.issues if i.severity == "blocker"}
    assert {"too_short", "voice_missing"} <= codes


def test_evaluate_short_black_is_warning_only():
    probe = {"duration": 5.0, "width": 768, "height": 1344, "has_audio": True}
    r = evaluate(probe=probe,
                 segments={"black": [[1.0, 1.6]], "freeze": [], "silence": []},
                 expected_duration=5, expected_ratio="9:16", expect_voice=False)
    assert r.passed
    assert [i.code for i in r.issues] == ["short_black"]


@pytest.mark.parametrize("file_name,duration,expect_voice,want_passed", [
    ("black2.mp4", 5, False, False),
    ("frozen_silent.mp4", 5, True, False),
    ("normal.mp4", 5, False, True),
])
def test_run_tech_check_on_real_clips(qc_clips, file_name, duration, expect_voice, want_passed):
    clip = qc_clips[file_name]
    r = run_tech_check(clip, _probe(clip), expected_duration=duration,
                       expected_ratio="9:16", expect_voice=expect_voice)
    assert r.passed is want_passed
