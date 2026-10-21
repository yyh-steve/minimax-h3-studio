"""应用级服务：Runner 单例与运行时配置。"""
from __future__ import annotations

from dataclasses import replace

from . import db
from .config import Settings, ensure_dirs
from .pipeline.runner import Runner

_settings: Settings | None = None
_runner: Runner | None = None


class BackendChangeBlocked(ValueError):
    pass


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def update_settings(patch: dict) -> Settings:
    """更新允许在设置页修改的字段。改 provider/mock 场景会影响下一次调用。"""
    global _settings, _runner
    s = get_settings()
    allowed = ("h3_base_url", "m3_base_url", "api_key", "h3_api_key", "m3_api_key", "provider", "m3_provider",
                "max_concurrency", "poll_interval_seconds", "auto_retry_limit",
                "budget_output_seconds", "mock_scenario", "m3_take_review",
                "m3_review_fps", "m3_review_detail", "m3_mock_review")
    changes = {key: patch[key] for key in allowed if key in patch and patch[key] is not None}
    for key in ('provider', 'm3_provider'):
        if key in changes:
            changes[key] = (changes[key] or 'mock').lower()
    candidate = replace(s, **changes)
    if s.provider != 'local' and candidate.provider == 'local' and not changes.get('h3_api_key'):
        raise ValueError('切换到 local 必须显式填写该本地服务的 H3_API_KEY，不能沿用官网凭证')
    if candidate.provider not in ('mock', 'local', 'official') or candidate.m3_provider not in ('mock', 'official'):
        raise ValueError('未知 provider')
    if (candidate.provider, candidate.h3_origin) != (s.provider, s.h3_origin) and db.has_active_takes():
        raise BackendChangeBlocked('存在活动任务，不能切换 H3 provider 或服务地址；请先完成或明确处理这些任务')
    _settings = candidate
    if _runner is not None:
        _runner.settings = candidate
    return candidate


def validate_take_origin(row) -> None:
    settings = get_settings()
    values = dict(row)
    if not values.get('provider_name') or not values.get('provider_origin'):
        raise BackendChangeBlocked('任务缺少原始 H3 服务来源，请人工核对，不能猜测查询或取消地址')
    if values.get('provider_name') and (values['provider_name'], values['provider_origin']) != (settings.provider, settings.h3_origin):
        raise BackendChangeBlocked('任务属于另一 H3 服务，不能向当前后端发送查询或取消请求')


def get_runner() -> Runner:
    global _runner
    if _runner is None:
        ensure_dirs()
        _runner = Runner(get_settings())
        _runner.start()
    return _runner


async def shutdown_runner() -> None:
    global _runner
    if _runner is not None:
        await _runner.stop()
        _runner = None
