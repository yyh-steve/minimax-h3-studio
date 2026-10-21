"""Local provider/config regression tests. All HTTP uses in-process transports."""
from __future__ import annotations

import asyncio
import copy
import importlib
import json
from pathlib import Path

import httpx
import pytest

LOCAL_KEY = "local-test-key-not-a-production-secret"
BACKEND = Path(__file__).resolve().parents[1]
H3_API = BACKEND.parents[1] / "h3-api"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    for name in ("MINIMAX_API_KEY", "H3_API_KEY", "M3_API_KEY", "H3_BASE_URL", "M3_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("H3_PROVIDER", "mock")
    monkeypatch.setenv("M3_PROVIDER", "mock")


def local_body():
    return {
        "model": "MiniMax-H3", "duration": 4, "resolution": "768P", "ratio": "1:1",
        "content": [{"type": "text", "text": "A quiet product close-up."},
                    {"type": "image_url", "role": "reference_image",
                     "image_url": {"url": "data:image/png;base64,AAAA"}}],
    }


def test_independent_keys_and_official_legacy_fallback(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("MINIMAX_API_KEY", "legacy-cloud-key")
    monkeypatch.setenv("H3_API_KEY", LOCAL_KEY)
    monkeypatch.setenv("M3_API_KEY", "m3-cloud-key")
    settings = Settings(provider="local", m3_provider="official")
    assert settings.h3_key == LOCAL_KEY
    assert settings.m3_key == "m3-cloud-key"
    assert settings.validate_provider() is None
    fallback = Settings(provider="official", m3_provider="official",
                        h3_api_key="", m3_api_key="")
    assert fallback.h3_key == fallback.m3_key == "legacy-cloud-key"
    fallback.provider = "local"
    assert fallback.h3_key == ""
    assert "H3_API_KEY" in fallback.validate_provider()
    fallback.m3_provider = "mock"
    assert fallback.m3_key == ""


def test_public_settings_never_return_any_raw_key():
    from app.config import Settings
    settings = Settings(api_key="legacy", h3_api_key=LOCAL_KEY, m3_api_key="m3-key")
    public = settings.public_dict()
    assert not {"api_key", "h3_api_key", "m3_api_key"} & public.keys()
    assert public["api_key_set"] and public["h3_api_key_set"] and public["m3_api_key_set"]
    assert all(value not in json.dumps(public) for value in ("legacy", LOCAL_KEY, "m3-key"))


@pytest.mark.parametrize("key", ["", "too-short", "a" * 23, "汉" * 24, "a" * 24 + " "])
def test_local_requires_explicit_valid_key_even_with_legacy(key):
    from app.config import Settings
    settings = Settings(provider="local", api_key="cloud-key", h3_api_key=key)
    assert "H3_API_KEY" in settings.validate_provider()


def test_provider_specific_default_url(monkeypatch):
    from app.config import Settings
    assert Settings(provider="local", h3_api_key=LOCAL_KEY).h3_base_url == "http://127.0.0.1:30010"
    assert Settings(provider="official").h3_base_url == "https://api.minimax.cn"
    monkeypatch.setenv("H3_PROVIDER", "local")
    assert Settings().h3_base_url == "http://127.0.0.1:30010"


@pytest.mark.parametrize("url,origin", [
    ("HTTPS://H3.EXAMPLE:443/", "https://h3.example"),
    ("http://LOCALHOST:80/", "http://localhost"),
    ("http://127.0.0.1:30010/", "http://127.0.0.1:30010"),
    ("http://[::1]:30010/", "http://[::1]:30010"),
])
def test_h3_origin_is_canonical(url, origin):
    from app.config import Settings
    assert Settings(h3_base_url=url).h3_origin == origin


@pytest.mark.parametrize("url", ["ftp://host", "https://user:pass@host", "https://host/path",
                                  "https://host?q=1", "https://host#fragment", "http://host:99999",
                                  "http://...", "http://host\\evil", "http://[::1]junk", "http://host.."])
def test_invalid_h3_origin_is_rejected(url):
    from app.config import Settings
    assert Settings(h3_base_url=url).validate_provider()


def test_dotenv_data_directory_loaded_before_path_constants(tmp_path, monkeypatch):
    import app.config as config
    env_file = BACKEND / ".env"
    actual_exists, actual_read = Path.exists, Path.read_text
    monkeypatch.delenv("PIPELINE_DATA_DIR", raising=False)
    monkeypatch.setattr(Path, "exists", lambda path: True if path == env_file else actual_exists(path))
    monkeypatch.setattr(Path, "read_text", lambda path, *a, **kw:
                        f"PIPELINE_DATA_DIR={tmp_path}\n" if path == env_file else actual_read(path, *a, **kw))
    try:
        importlib.reload(config)
        assert config.DATA_DIR == tmp_path
        assert config.DB_PATH == tmp_path / "app.db"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_factories_select_independent_providers_and_reject_unknown():
    from app.config import Settings
    from app.providers import get_m3, get_provider
    settings = Settings(provider="local", m3_provider="official", h3_api_key=LOCAL_KEY,
                        m3_api_key="m3-only", api_key="legacy")
    h3, m3 = get_provider(settings), get_m3(settings)
    assert type(h3).__name__ == "LocalH3" and h3.api_key == LOCAL_KEY
    assert m3.api_key == "m3-only"
    settings.provider = "offical"
    assert settings.validate_provider()
    with pytest.raises(ValueError):
        get_provider(settings)
    settings.provider = "mock"
    settings.m3_provider = "local"
    assert settings.validate_provider()
    with pytest.raises(ValueError):
        get_m3(settings)


@pytest.mark.parametrize("outcome", ["timeout", "connect", 500, 502, 503, 504, {}, [], {"ok": True},
                                    {"task_id": True}, {"task_id": 17}, {"task_id": {"id": "wrong"}}, {"task_id": "   "}])
def test_official_create_ambiguous_result_is_never_blindly_retryable(outcome):
    from app.providers.base import ProviderError
    from app.providers.h3_official import OfficialH3
    def respond(request):
        if outcome == "timeout":
            raise httpx.ReadTimeout("reply lost", request=request)
        if outcome == "connect":
            raise httpx.ConnectError("connection lost", request=request)
        if isinstance(outcome, int):
            return httpx.Response(outcome, json={"error": {"type": "server_error"}})
        return httpx.Response(200, json=outcome)
    provider = OfficialH3("https://h3.example", "cloud", transport=httpx.MockTransport(respond))
    with pytest.raises(ProviderError) as caught:
        provider.create(local_body())
    assert caught.value.retryable is False


def test_official_malformed_server_error_still_has_safe_create_semantics():
    from app.providers.base import ProviderError
    from app.providers.h3_official import OfficialH3
    provider = OfficialH3("https://h3.example", "cloud", transport=httpx.MockTransport(
        lambda request: httpx.Response(500, json={"error": {"message": 123, "type": []}})))
    with pytest.raises(ProviderError) as caught:
        provider.create(local_body())
    assert caught.value.http_status == 500
    assert caught.value.retryable is False


def test_explicit_rate_limit_can_retry_and_official_has_no_idempotency_header():
    from app.providers.base import ProviderError
    from app.providers.h3_official import OfficialH3
    def respond(request):
        assert "Idempotency-Key" not in request.headers
        return httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "queue full"}})
    provider = OfficialH3("https://h3.example", "cloud", transport=httpx.MockTransport(respond))
    with pytest.raises(ProviderError) as caught:
        provider.create(local_body())
    assert caught.value.retryable is True


@pytest.mark.parametrize("code,retryable", [(1004, False), (1008, False), (1026, False),
                                           (1002, True), (1000, True), (None, False),
                                           ("worker_failed", False), ("interrupted", False),
                                           ("task_expired", False), ("1004", False)])
def test_platform_failure_preserves_codes_and_fails_closed(code, retryable):
    from app.providers.base import classify_platform_failure
    error = classify_platform_failure(code, "generation failed")
    assert error.code == code
    assert error.retryable is retryable


def test_http_errors_preserve_explicit_string_codes():
    from app.providers.base import classify_http_error
    error = classify_http_error(400, {"error": {"type": "bad_request_error", "code": "invalid_input", "message": "bad"}})
    assert error.code == "invalid_input"


@pytest.mark.parametrize("code", ["worker_failed", "interrupted", "task_expired", "1000", 1000])
def test_query_and_list_preserve_error_code_type(code):
    from app.providers.h3_official import OfficialH3
    task = {"id": "task1", "status": "failed", "error": {"code": code, "message": "failed"}}
    provider = OfficialH3("https://h3.example", "cloud", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"task": task, "items": [task]})))
    assert provider.get_task("task1").error_code == code
    assert provider.list_tasks(["task1"])["task1"].error_code == code


def test_local_sends_required_idempotency_key_and_local_bearer():
    from app.providers.h3_local import LocalH3
    requests = []
    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + LOCAL_KEY
        if request.method == 'GET':
            assert request.url.path == '/capabilities'
            return httpx.Response(200, json={'model':'MiniMax-H3','workflow':'single_image_ref2va',
                                             'local_extensions':['Idempotency-Key']})
        assert request.headers["Idempotency-Key"] == "take-17-stable"
        assert json.loads(request.content) == local_body()
        return httpx.Response(200, json={"task_id": "local-task"})
    provider = LocalH3("http://local.example", LOCAL_KEY, transport=httpx.MockTransport(respond))
    for key in ("", " ", "a" * 129, "非ASCII"):
        with pytest.raises(ValueError):
            provider.create(local_body(), idempotency_key=key)
    assert requests == []
    assert provider.create(local_body(), idempotency_key="take-17-stable").task_id == "local-task"


INVALID_CHANGES = [
    {"model": "MiniMax-H3-Max"}, {"resolution": "2K"}, {"resolution": "480P"},
    {"duration": True}, {"duration": 4.0}, {"duration": "4"}, {"duration": 3}, {"duration": 16},
    {"ratio": "17:8"}, {"aigc_watermark": True}, {"seed": 1}, {"callback_url": "https://example.com"},
]


@pytest.mark.parametrize("change", INVALID_CHANGES)
def test_local_request_rejects_unsupported_options(change):
    from app.providers.capabilities import validate_local_request
    body = local_body()
    body.update(change)
    with pytest.raises(ValueError):
        validate_local_request(body)


@pytest.mark.parametrize("mutation", ["no_image", "many_images", "first_frame", "last_frame", "no_role",
                                      "empty_text", "long_text", "gif", "http", "credentials", "bad_base64", "unknown_content_field"])
def test_local_request_rejects_unsupported_content(mutation):
    from app.providers.capabilities import validate_local_request
    body = local_body()
    image = body["content"][1]
    if mutation == "no_image":
        body["content"].pop()
    elif mutation == "many_images":
        body["content"].append(copy.deepcopy(image))
    elif mutation in ("first_frame", "last_frame"):
        image["role"] = mutation
    elif mutation == "no_role":
        del image["role"]
    elif mutation == "empty_text":
        body["content"][0]["text"] = "  "
    elif mutation == "long_text":
        body["content"][0]["text"] = "a" * 7001
    elif mutation == "unknown_content_field":
        image["unknown"] = True
    else:
        image["image_url"]["url"] = {"gif": "data:image/gif;base64,AAAA", "http": "http://example.com/a.png",
            "credentials": "https://user:pass@example.com/a.png", "bad_base64": "data:image/png;base64,not-valid"}[mutation]
    with pytest.raises(ValueError):
        validate_local_request(body)


@pytest.mark.parametrize("url", ["https://example.com/a.png", "https://example.com/a.webp?signature=abc",
                                 "data:image/jpeg;base64,AAAA", "data:image/png;base64,AAAA", "data:image/webp;base64,AAAA"])
def test_local_request_valid_subset_agrees_with_actual_contract(url, monkeypatch):
    from app.providers.capabilities import validate_local_request
    monkeypatch.syspath_prepend(str(H3_API))
    from h3_api.contracts import normalize_request
    body = local_body()
    body["content"][1]["image_url"]["url"] = url
    assert validate_local_request(body) is None
    assert normalize_request(body)["image_source"] == url
    for change in INVALID_CHANGES:
        invalid = {**body, **change}
        from h3_api.errors import ApiError
        with pytest.raises(ApiError):
            normalize_request(invalid)


def test_mock_m3_only_new_local_plans_use_single_reference():
    from app.config import Settings
    from app.providers import get_m3
    text = json.dumps({"target_seconds": 20, "ratio": "1:1", "selling_points": ["simple"]})
    settings = Settings(provider="local", h3_api_key=LOCAL_KEY, m3_provider="mock")
    m3 = get_m3(settings)
    assert m3.reference_only is True
    result = m3.generate_storyboard(user_text=text, image_data_uris=["img1", "img2"])
    assert all(shot.mode == "reference" and len(shot.images.references) == 1
               and shot.images.first_frame is None and shot.images.last_frame is None
               for shot in result.storyboard.shots)
    assert m3.generate_storyboard(user_text=text, image_data_uris=[]).storyboard.shots[0].mode == "t2v"
    settings.provider = "official"
    official_plan = get_m3(settings).generate_storyboard(user_text=text, image_data_uris=["img1"])
    assert official_plan.storyboard.shots[0].mode == "first_frame"
    settings.provider = "local"
    m3 = get_m3(settings)
    for fix in (None, "revise camera"):
        preserved = m3.generate_storyboard(user_text=text, image_data_uris=["img1"],
                                           previous=official_plan.model_dump(), fix_message=fix)
        assert preserved.storyboard.shots[0].mode == "first_frame"
    rewritten = m3.rewrite_shot(shot=official_plan.storyboard.shots[0], instruction="镜头推近",
                                product_card=official_plan.product_card)
    assert rewritten.mode == "first_frame"


def test_local_planning_hint_states_exact_subset():
    from app.providers.capabilities import local_planning_hint
    hint = local_planning_hint()
    assert all(word in hint for word in ("MiniMax-H3", "768P", "reference", "4", "15", "7000"))


def test_mock_brief_allows_appended_planning_hint():
    from app.providers.m3_mock import MockM3
    text = json.dumps({"target_seconds": 20, "ratio": "1:1"}) + "\nLocal planning constraints."
    result = MockM3().generate_storyboard(user_text=text, image_data_uris=["image"])
    assert result.storyboard.total_duration == 20
    assert result.storyboard.ratio == "1:1"


def test_local_provider_against_actual_asgi_create_query_list_cancel(tmp_path, monkeypatch):
    from app.providers.h3_local import LocalH3
    from app.providers.base import ProviderError
    monkeypatch.syspath_prepend(str(H3_API))
    from h3_api.app import create_app
    from h3_api.config import Settings
    app = create_app(Settings(data_dir=tmp_path, api_key=LOCAL_KEY, public_base_url="http://local.example"))
    async def dispatch(request):
        if request.url.path.startswith("/media/"):
            assert "Authorization" not in request.headers
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.example") as client:
            response = await client.request(request.method, str(request.url), headers=request.headers, content=request.content)
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)
    provider = LocalH3("http://local.example", LOCAL_KEY,
                       transport=httpx.MockTransport(lambda request: asyncio.run(dispatch(request))))
    task_id = provider.create(local_body(), idempotency_key="stable-first").task_id
    assert provider.create(local_body(), idempotency_key="stable-first").task_id == task_id
    assert provider.get_task(task_id).status == "queued"
    assert provider.list_tasks([task_id])[task_id].duration == 4
    assert provider.cancel(task_id) == {"task_id": task_id, "action": "cancelled", "status": "cancelled"}
    assert provider.get_task(task_id).status == "cancelled"
    second_id = provider.create(local_body(), idempotency_key="stable-second").task_id
    assert app.state.store.claim()["id"] == second_id
    with pytest.raises(ProviderError) as caught:
        provider.cancel(second_id)
    assert caught.value.http_status == 400
    app.state.store.fail(second_id, "worker_failed", "The generation worker failed.")
    assert provider.get_task(second_id).error_code == "worker_failed"
    assert provider.cancel(second_id)["action"] == "deleted"
    assert not provider.get_task(second_id).found
    third_id = provider.create(local_body(), idempotency_key="stable-third").task_id
    assert app.state.store.claim()["id"] == third_id
    # Synthetic bytes exercise signed file delivery without video generation.
    media = b"offline-synthetic-output"
    output = tmp_path / "jobs" / third_id / "output.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(media)
    app.state.store.finish(third_id, {"ratio": "1:1", "duration": 4, "bytes": len(media)})
    succeeded = provider.get_task(third_id)
    assert succeeded.status == "succeeded" and succeeded.usage["output_seconds"] == 4
    assert "sig=" in succeeded.video_url and "expires=" in succeeded.video_url
    assert provider.download(succeeded.video_url) == media
    assert provider.cancel(third_id)["action"] == "deleted"
    assert not provider.get_task(third_id).found
