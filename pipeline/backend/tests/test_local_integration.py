"""Copied pipeline lifecycle tests using fake providers only; no paid generation."""
from __future__ import annotations

import base64
import io
import json
import uuid

import pytest
from PIL import Image


def request_body():
    image = io.BytesIO()
    Image.new('RGB', (256, 256), 'blue').save(image, 'PNG')
    return {'model': 'MiniMax-H3', 'resolution': '768P', 'duration': 5, 'ratio': '1:1', 'content': [
        {'type': 'text', 'text': 'A synthetic contract fixture.'},
        {'type': 'image_url', 'role': 'reference_image',
         'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode()}},
    ]}


def local(env):
    env.settings.provider = 'local'
    env.settings.h3_base_url = 'http://local-h3.example'
    env.settings.h3_api_key = 'synthetic-local-h3-key-at-least-24'
    env.settings.m3_provider = 'mock'
    env.settings.auto_retry_limit = 0
    return env.settings


def take(env, *, source='api'):
    return env.db.create_take(model='MiniMax-H3', request_json=request_body(), source=source,
                              provider_name=env.settings.provider, provider_origin=env.settings.h3_origin)


def api_client(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routes import router
    from app import services
    monkeypatch.setattr(services, '_settings', env.settings)
    monkeypatch.setattr(services, '_runner', env.runner)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_take_uuid_is_persistent_unique_and_contains_no_credential(env):
    one = env.db.create_take(model='MiniMax-H3', request_json=request_body())
    two = env.db.create_take(model='MiniMax-H3', request_json=request_body())
    first = env.db.get_take(one)
    assert 'idempotency_key' in first.keys(), 'Every Take must persist its submission identity before any network call'
    key = first['idempotency_key']
    assert uuid.UUID(key).version == 4
    assert key != env.db.get_take(two)['idempotency_key']
    env.db._conn.close(); env.db._conn = None
    assert env.db.get_take(one)['idempotency_key'] == key
    assert not {'api_key', 'h3_api_key', 'm3_api_key'} & set(first.keys())


async def test_local_unknown_submission_recovers_with_the_same_key(env, monkeypatch):
    local(env)
    from app.pipeline import runner
    from app.providers.base import ProviderError, CreateResult
    calls = []
    class Fake:
        def create(self, body, *, idempotency_key):
            calls.append((idempotency_key, body))
            if len(calls) == 1:
                raise ProviderError('network', 'Synthetic response loss after accept', retryable=False)
            return CreateResult('a' * 32)
        def list_tasks(self, *args):
            raise AssertionError('Local unknown submissions must not be matched by time/duration')
    monkeypatch.setattr(runner, 'get_provider', lambda settings: Fake())
    tid = take(env)
    await env.runner._submit_one(tid)
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0][0] == env.db.get_take(tid)['idempotency_key']
    assert env.db.get_take(tid)['task_id'] == 'a' * 32
    assert len(env.db.list_takes()) == 1


async def test_official_unknown_never_reposts_or_guesses_an_id(env, monkeypatch):
    env.settings.provider = 'official'; env.settings.api_key = 'synthetic-official-key'
    from app.pipeline import runner
    from app.providers.base import ProviderError
    calls = []
    class Fake:
        def create(self, body):
            calls.append(body)
            raise ProviderError('server', 'Synthetic ambiguous HTTP 500', http_status=500, retryable=True)
        def list_tasks(self, *args):
            raise AssertionError('Official unknown submissions must require manual task identification')
    monkeypatch.setattr(runner, 'get_provider', lambda settings: Fake())
    tid = take(env)
    await env.runner._submit_one(tid)
    await env.runner._tick()
    assert len(calls) == 1
    assert env.db.get_take(tid)['status'] == 'unknown'


def test_active_takes_block_provider_and_origin_changes_atomically(env, monkeypatch):
    from app import services
    monkeypatch.setattr(services, '_settings', env.settings)
    tid = env.db.create_take(model='MiniMax-H3', request_json=request_body())
    old = env.settings.h3_base_url
    with pytest.raises(ValueError):
        services.update_settings({'provider': 'local', 'h3_base_url': 'http://other.example',
                                  'h3_api_key': 'synthetic-local-h3-key-at-least-24'})
    assert env.settings.provider == 'mock' and env.settings.h3_base_url == old
    with pytest.raises(ValueError):
        services.update_settings({'h3_base_url': 'http://other.example'})
    env.db.update_take(tid, status='review')
    assert services.update_settings({'provider': 'local', 'h3_base_url': 'http://other.example',
                                      'h3_api_key': 'synthetic-local-h3-key-at-least-24'}).provider == 'local'


def test_fingerprint_includes_backend_origin(env):
    from app.storyboard_schema import content_fingerprint
    body = dict(provider='local', model='MiniMax-H3', resolution='768P', duration=5,
                ratio='1:1', mode='reference', prompt='test', image_hashes=['image-hash'])
    assert content_fingerprint(**body, backend_origin='http://one.example') != \
           content_fingerprint(**body, backend_origin='http://two.example')


async def test_local_platform_failure_never_creates_a_paid_retry(env):
    local(env); env.settings.auto_retry_limit = 9
    from app.providers.base import TaskInfo
    tid = take(env)
    env.db.update_take(tid, status='running', task_id='b' * 32)
    await env.runner._apply_task_info(tid, TaskInfo('b' * 32, 'failed', error_code='invalid_media', error_message='bad fixture'))
    assert len(env.db.list_takes()) == 1
    assert env.db.get_take(tid)['status'] == 'needs_edit'
    assert json.loads(env.db.get_take(tid)['error_json'])['code'] == 'invalid_media'
    assert await env.runner._spawn_retry_take(tid, 'synthetic QC failure') is None
    assert len(env.db.list_takes()) == 1


async def test_download_always_refreshes_signed_url_without_regeneration(env, monkeypatch):
    local(env)
    from app.pipeline import runner
    from app.providers.base import TaskInfo
    calls = []
    class Fake:
        def get_task(self, tid):
            calls.append('query'); return TaskInfo(tid, 'succeeded', video_url='http://download.example/fresh')
        def download(self, url):
            calls.append(url); assert url.endswith('/fresh'); return b'synthetic media fixture'
        def create(self, *args, **kwargs):
            raise AssertionError('Downloading never creates a new task')
    monkeypatch.setattr(runner, 'get_provider', lambda settings: Fake())
    monkeypatch.setattr(runner, 'probe_video', lambda path: {'width': 768, 'height': 768, 'duration': 5})
    tid = take(env)
    env.db.update_take(tid, status='succeeded', task_id='c' * 32,
                       raw_response_json=json.dumps({'content': {'url': 'http://download.example/expired'}}))
    await env.runner._download_one(tid)
    assert calls == ['query', 'http://download.example/fresh']
    assert env.db.get_take(tid)['status'] == 'downloaded'


def test_local_admission_rejects_unsupported_requests_without_creating_takes(env, monkeypatch):
    local(env)
    body = request_body()
    valid = dict(prompt=body['content'][0]['text'], duration=5, resolution='768P', model='MiniMax-H3',
                 ratio='1:1', images=[{'url': body['content'][1]['image_url']['url'], 'role': 'reference_image'}])
    bad = [dict(valid, model='MiniMax-H3-Max'), dict(valid, resolution='2K'),
           dict(valid, images=[]), dict(valid, images=valid['images'] * 2),
           dict(valid, images=[dict(valid['images'][0], role='first_frame')]), dict(valid, prompt='x' * 7001)]
    with api_client(env, monkeypatch) as client:
        for request in bad:
            assert client.post('/api/takes', json=request).status_code == 400
        assert not env.db.list_takes()
        assert client.post('/api/takes', json=valid).status_code == 200
        assert len(env.db.list_takes()) == 1


def test_cancel_race_deleted_result_is_never_reported_as_cancelled(env, monkeypatch):
    local(env)
    from app.api import routes
    class Fake:
        def cancel(self, tid):return {'task_id': tid, 'action': 'deleted', 'status': 'deleted'}
    monkeypatch.setattr(routes, 'get_provider', lambda settings: Fake())
    tid = take(env); env.db.update_take(tid, status='queued', task_id='d' * 32)
    with api_client(env, monkeypatch) as client:
        response = client.post(f'/api/takes/{tid}/cancel')
    assert response.status_code == 200
    assert response.json()['status'] == 'review'
    assert env.db.get_take(tid)['platform_status'] == 'deleted'
    assert len(env.db.list_takes()) == 1


def test_settings_http_accepts_independent_keys_without_returning_them(env, monkeypatch):
    with api_client(env, monkeypatch) as client:
        values = {'h3_api_key': 'synthetic-h3-key-000000000000', 'm3_api_key': 'synthetic-m3-key-000000000000'}
        response = client.put('/api/settings', json=values)
        assert response.status_code == 200
        public = response.json()
        assert public['h3_api_key_set'] and public['m3_api_key_set']
        assert not any(value in response.text for value in values.values())


def test_unknown_task_can_be_bound_only_to_an_explicit_manual_id(env, monkeypatch):
    env.settings.provider = 'official'; env.settings.api_key = 'synthetic-official-key'
    from app.api import routes
    from app.providers.base import TaskInfo
    class Fake:
        def get_task(self, task_id):return TaskInfo(task_id, 'queued')
        def create(self, *args, **kwargs):raise AssertionError('Manual binding must not create work')
        def list_tasks(self, *args, **kwargs):raise AssertionError('Manual binding must not guess an ID')
    monkeypatch.setattr(routes, 'get_provider', lambda settings: Fake())
    one, two = take(env), take(env)
    env.db.update_take(one, status='unknown');env.db.update_take(two, status='unknown')
    with api_client(env, monkeypatch) as client:
        assert client.post(f'/api/takes/{one}/bind-task', json={'task_id': 'manual-task-id'}).status_code == 200
        assert client.post(f'/api/takes/{two}/bind-task', json={'task_id': 'manual-task-id'}).status_code == 409
    assert env.db.get_take(one)['task_id'] == 'manual-task-id'
    assert env.db.get_take(one)['status'] == 'queued'


def test_local_confirm_blocks_an_existing_keyframe_without_converting_it(env):
    from test_m3_revise_service import _seed_project
    pid = _seed_project(env, seconds=20)
    draft = env.svc.generate(pid)
    sb = draft['storyboard_id']
    before = env.svc._load_result(sb)
    assert any(shot.mode == 'first_frame' for shot in before.storyboard.shots)
    local(env)
    with pytest.raises(PermissionError):
        env.svc.confirm(sb)
    assert not env.db.list_takes(project_id=pid)
    assert [shot.mode for shot in env.svc._load_result(sb).storyboard.shots] == [shot.mode for shot in before.storyboard.shots]


def test_local_fresh_mock_planning_and_confirmation_use_single_references(env):
    local(env)
    from test_m3_revise_service import _seed_project
    pid = _seed_project(env, seconds=20)
    draft = env.svc.generate(pid)
    result = env.svc._load_result(draft['storyboard_id'])
    assert all(shot.mode == 'reference' and len(shot.images.references) == 1 for shot in result.storyboard.shots)
    confirmation = env.svc.confirm(draft['storyboard_id'])
    assert confirmation['new_shots'] == len(result.storyboard.shots)
    for row in env.db.list_takes(project_id=pid):
        assert row['provider_name'] == 'local'
        assert row['provider_origin'] == env.settings.h3_origin
        payload = json.loads(row['request_json'])
        assert len(payload['content']) == 2 and payload['content'][1]['role'] == 'reference_image'


def test_restart_finds_submitting_take_and_retains_its_identity(env):
    local(env)
    from app.pipeline.runner import Runner
    tid = take(env)
    key = env.db.get_take(tid)['idempotency_key']
    assert env.db.claim_submitting(tid)
    restarted = Runner(env.settings)
    restarted.reconcile_on_startup()
    row = env.db.get_take(tid)
    assert row['status'] == 'unknown'
    assert row['idempotency_key'] == key


async def test_local_backoff_and_restart_keep_the_original_key(env, monkeypatch):
    local(env)
    from app.pipeline import runner
    from app.providers.base import ProviderError, CreateResult
    keys = []
    class Fake:
        def create(self, body, *, idempotency_key):
            keys.append(idempotency_key)
            if len(keys) <= 2:
                raise ProviderError('network', 'Synthetic response loss')
            return CreateResult('e' * 32)
    monkeypatch.setattr(runner, 'get_provider', lambda settings: Fake())
    tid = take(env)
    await env.runner._submit_one(tid)
    assert env.db.get_take(tid)['status'] == 'backoff'
    env.db._conn.close(); env.db._conn = None
    restarted = runner.Runner(env.settings)
    restarted.reconcile_on_startup()
    env.db.update_take(tid, next_run_at=0)
    await restarted._submit_one(tid)
    assert len(keys) == 3 and len(set(keys)) == 1
    assert env.db.get_take(tid)['task_id'] == 'e' * 32
    assert len(env.db.list_takes()) == 1


async def test_saved_origin_mismatch_blocks_all_h3_transport(env, monkeypatch):
    local(env)
    from app.pipeline import runner
    tid = take(env)
    env.db.update_take(tid, task_id='f' * 32, status='queued')
    env.settings.h3_base_url = 'http://different-origin.example'
    def forbidden(settings):
        raise AssertionError('Source mismatch must be rejected before selecting a provider')
    monkeypatch.setattr(runner, 'get_provider', forbidden)
    await env.runner._poll_all()
    assert env.db.get_take(tid)['status'] == 'review'
    env.db.update_take(tid, status='succeeded')
    await env.runner._download_one(tid)
    assert env.db.get_take(tid)['status'] == 'review'


def test_settings_http_blocks_active_origin_switch(env, monkeypatch):
    local(env)
    take(env)
    with api_client(env, monkeypatch) as client:
        response = client.put('/api/settings', json={'h3_base_url': 'http://different.example'})
    assert response.status_code == 409
    assert env.settings.h3_base_url == 'http://local-h3.example'


@pytest.mark.parametrize('code', ['backend_oom', 'policy_error', 'unrecognized_failure'])
async def test_local_string_failure_codes_stay_manual_without_recomputation(env, code):
    local(env); env.settings.auto_retry_limit = 9
    from app.providers.base import TaskInfo
    tid = take(env)
    env.db.update_take(tid, status='running', task_id='b' * 32)
    await env.runner._apply_task_info(tid, TaskInfo('b' * 32, 'failed', error_code=code))
    assert env.db.get_take(tid)['status'] == 'review'
    assert json.loads(env.db.get_take(tid)['error_json'])['code'] == code
    assert len(env.db.list_takes()) == 1


async def test_official_5xx_rate_limit_body_does_not_authorize_reposting(env):
    env.settings.provider = 'official'; env.settings.api_key = 'synthetic-official-key'
    from app.providers.base import ProviderError
    tid = take(env)
    await env.runner._handle_create_error(tid, ProviderError(
        'rate_limit', 'Synthetic misleading 503 rate_limit body', http_status=503, retryable=False))
    assert env.db.get_take(tid)['status'] == 'unknown'


async def test_local_unresolved_backoff_blocks_a_second_submission(env, monkeypatch):
    local(env)
    from app.pipeline import runner
    one, two = take(env), take(env)
    env.db.update_take(one, status='backoff', next_run_at=10 ** 12)
    async def forbidden(tid):
        raise AssertionError('Unresolved local recovery must finish before submitting another Take')
    monkeypatch.setattr(env.runner, '_submit_one', forbidden)
    await env.runner._submit_due()
    assert not env.runner._inflight
    assert env.db.get_take(two)['status'] == 'pending'
