"""pytest 夹具：每个测试用独立数据目录和高速 Mock，不依赖外部服务。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("H3_PROVIDER", "mock")
    monkeypatch.setenv("MAX_CONCURRENCY", "4")
    monkeypatch.setenv("AUTO_RETRY_LIMIT", "1")
    monkeypatch.setenv("BUDGET_OUTPUT_SECONDS", "300")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "1")

    # 强制全部 app 子模块按新环境变量重新加载
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    import app.config as config
    from app import constants as C
    from app import db
    import app.providers as providers
    from app.providers.h3_mock import MockH3
    from app.providers.m3_mock import MockM3
    from app.pipeline.runner import Runner
    from app.pipeline.storyboard_service import StoryboardService

    # 测试里的退避只等几十毫秒
    C.BACKOFF_SCHEDULE_SECONDS = (0.05, 0.1, 0.2)

    config.ensure_dirs()
    db.connect()

    fast = MockH3(queue_seconds=0.3, run_seconds=0.5)
    providers.set_h3_mock(fast)
    providers._m3_mock = MockM3()

    settings = config.Settings(provider="mock", m3_provider="mock",
                               max_concurrency=4, auto_retry_limit=1,
                               budget_output_seconds=300)
    runner = Runner(settings)          # 不 start()，测试手动 _tick，时序可控
    runner.reconcile_on_startup()
    svc = StoryboardService(settings)

    yield SimpleNamespace(settings=settings, runner=runner, svc=svc, db=db,
                          mock=fast, h3=fast, m3=providers._m3_mock,
                          constants=C, tmp=tmp_path)
