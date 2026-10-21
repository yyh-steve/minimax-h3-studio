"""M4 合成单元：字幕渲染、硬切/转场合片、时长与画幅校验。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.compose import compose, expected_total, find_cjk_font, render_subtitle_png
from app.media import probe_video
from media_fixtures import make_clip


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    directory = tmp_path_factory.mktemp("compose-clips")
    sound = "sine=frequency=440:sample_rate=48000"
    return {
        # Darken the lower fifth so the subtitle assertion cannot pass on the
        # bright test pattern alone. All frames are synthesized for this run.
        "c1": make_clip(directory, "c1.mp4", duration=4, audio=sound,
                        video="testsrc2=size=144x256:rate=24:duration=4,"
                              "drawbox=x=0:y=ih*0.8:w=iw:h=ih*0.2:color=black:t=fill"),
        "c2": make_clip(directory, "c2.mp4", duration=5,
                        video="testsrc2=size=256x144:rate=24:duration=5"),
        "c3": make_clip(directory, "c3.mp4", duration=6, audio=sound,
                        video="testsrc2=size=192x192:rate=24:duration=6"),
    }


def _items(clips):
    return [
        {"shot_key": "s1", "path": str(clips["c1"]), "duration": 4, "subtitle": "30 秒出汁",
         "has_audio": True},
        {"shot_key": "s2", "path": str(clips["c2"]), "duration": 5,
         "subtitle": "可拆洗，冲一下就干净，还可以写一段更长的字幕验证两行换行逻辑",
         "has_audio": False},
        {"shot_key": "s3", "path": str(clips["c3"]), "duration": 6, "subtitle": "一杯带走",
         "has_audio": True},
    ]


def _bgm(tmp_path):
    bgm = tmp_path / "bgm.m4a"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=f=220:sample_rate=44100:duration=3",
                    "-ac", "2", "-c:a", "aac", str(bgm)], check=True)
    return bgm


def test_expected_total():
    assert expected_total([4, 5, 6], "cut") == 15
    assert abs(expected_total([4, 5, 6], "fade", 0.3) - 14.4) < 1e-9


def test_render_subtitle_png(tmp_path):
    font = find_cjk_font()
    if font is None:
        pytest.skip("没有中文字体")
    png = tmp_path / "sub.png"
    render_subtitle_png("一二三四五六七八九十一二三四五六七八九十"
                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ 再来一些中文句子验证",
                        768, 1344, png, font)
    with Image.open(png) as im:
        assert im.size == (768, 1344)
        # 底部字幕区有非透明像素，顶部没有字幕
        alpha = im.split()[-1]
        bbox = alpha.getbbox()
        assert bbox is not None
        assert bbox[1] > 1344 * 0.5 and bbox[3] < 1344 * 0.98


def test_compose_cut(clips, tmp_path):
    out = tmp_path / "cut.mp4"
    r = compose(items=_items(clips), width=768, height=1344, transition="cut",
                subtitles=False, keep_audio=False, original_volume=1.0, bgm_path=None,
                bgm_volume=0.3, out_path=out, work_dir=tmp_path / "w")
    probe = probe_video(out)
    assert probe["width"] == 768 and probe["height"] == 1344
    assert abs(probe["duration"] - 15) < 0.3 and probe["has_audio"]
    assert r["warnings"] == []


def test_compose_fade_with_bgm_and_subtitles(clips, tmp_path):
    out = tmp_path / "fade.mp4"
    r = compose(items=_items(clips), width=768, height=1344, transition="fade",
                subtitles=True, keep_audio=True, original_volume=1.0,
                bgm_path=str(_bgm(tmp_path)), bgm_volume=0.3,
                out_path=out, work_dir=tmp_path / "w2")
    probe = probe_video(out)
    assert probe["width"] == 768 and probe["height"] == 1344
    assert abs(probe["duration"] - 14.4) < 0.3 and probe["has_audio"]
    # s2 无音轨会给一条提示
    assert any("没有音轨" in w for w in r["warnings"])
    # 字幕确实叠上去了：底部区域不是纯黑
    frame = tmp_path / "f.png"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "2",
                    "-i", str(out), "-frames:v", "1", str(frame)], check=True)
    with Image.open(frame) as im:
        bottom = im.crop((0, int(im.height * 0.82), im.width, im.height)).convert("L")
        assert bottom.getextrema()[1] > 160
