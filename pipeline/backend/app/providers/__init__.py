"""Provider 选择与单例。"""
from __future__ import annotations

from ..config import Settings
from .base import ProviderError  # re-export
from .h3_official import OfficialH3, MODEL_H3, MODEL_H3_MAX
from .h3_local import LocalH3
from .h3_mock import MockH3
from .m3_official import M3Client, M3Error
from .m3_mock import MockM3

_h3_mock: MockH3 | None = None
_m3_mock: MockM3 | None = None


def get_provider(settings: Settings):
    """H3 视频生成 provider。单例：测试会注入自己的高速实例，这里不能在每次调用时新建。"""
    global _h3_mock
    if settings.provider == "mock":
        if _h3_mock is None:
            _h3_mock = MockH3()
        _h3_mock.set_scenario(settings.mock_scenario)
        return _h3_mock
    if settings.provider == "local":
        error = settings.validate_provider()
        if error:
            raise ValueError(error)
        return LocalH3(base_url=settings.h3_origin, api_key=settings.h3_key)
    if settings.provider == "official":
        return OfficialH3(base_url=settings.h3_base_url, api_key=settings.h3_key)
    raise ValueError(f"未知 H3 provider: {settings.provider}")


def set_h3_mock(mock: MockH3) -> None:
    """测试夹具注入自己的 mock（自定义排队/运行时长）。"""
    global _h3_mock
    _h3_mock = mock


def h3_mock_instance() -> MockH3:
    global _h3_mock
    if _h3_mock is None:
        _h3_mock = MockH3()
    return _h3_mock


def get_m3(settings: Settings):
    """M3 分镜/评审/看片 provider。m3_provider=mock 时用本地假 M3。"""
    global _m3_mock
    if getattr(settings, "m3_provider", "mock") == "mock":
        if _m3_mock is None:
            _m3_mock = MockM3()
        _m3_mock.set_review_mode(getattr(settings, "m3_mock_review", "pass"))
        _m3_mock.reference_only = settings.provider == "local"
        return _m3_mock
    if settings.m3_provider == "official":
        return M3Client(base_url=settings.m3_base_url, api_key=settings.m3_key)
    raise ValueError(f"未知 M3 provider: {settings.m3_provider}")


def m3_mock_instance() -> MockM3:
    global _m3_mock
    if _m3_mock is None:
        _m3_mock = MockM3()
    return _m3_mock


__all__ = ["get_provider", "h3_mock_instance", "get_m3", "m3_mock_instance",
           "ProviderError", "OfficialH3", "LocalH3", "MockH3", "M3Client", "M3Error",
           "MockM3", "MODEL_H3", "MODEL_H3_MAX"]
