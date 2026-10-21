"""Deterministic release regression cases: no real network or model calls."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import threading

import pytest

from test_local_integration import local, take, api_client
from test_m3_revise_service import _seed_project


def draft(env):
    local(env)
    pid = _seed_project(env, seconds=20)
    board = env.svc.generate(pid)
    return pid, board['storyboard_id'], len(board['shots'])


def test_confirm_is_once_per_storyboard(env, monkeypatch):
    pid, sid, count = draft(env)
    with api_client(env, monkeypatch) as client:
        assert client.post(f'/api/storyboards/{sid}/confirm').status_code == 200
        assert client.post(f'/api/storyboards/{sid}/confirm').status_code == 409
    assert len(env.db.list_takes(project_id=pid)) == count


def test_concurrent_confirm_only_creates_one_batch(env):
    pid, sid, count = draft(env)
    ready = threading.Barrier(2)
    def confirm():
        ready.wait(timeout=5)
        try:
            env.svc.confirm(sid)
        except PermissionError:
            return 'rejected'
        return 'confirmed'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(), range(2)))
    assert sorted(results) == ['confirmed', 'rejected']
    assert len(env.db.list_takes(project_id=pid)) == count


def test_interrupted_confirmation_rolls_back_before_retry(env, monkeypatch):
    pid, sid, count = draft(env)
    original = env.db.create_take
    calls = 0
    def interrupted(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt('Synthetic interruption midway through confirmation')
        return original(**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(env.db, 'create_take', interrupted)
        with pytest.raises(KeyboardInterrupt):
            env.svc.confirm(sid)
    assert env.db.get_storyboard(sid)['status'] == 'draft'
    assert env.db.list_takes(project_id=pid) == []
    assert env.db.list_shot_takes(sid) == []
    env.svc.confirm(sid)
    assert len(env.db.list_takes(project_id=pid)) == count


@pytest.mark.parametrize('provider', ['local', 'official'])
@pytest.mark.parametrize('late_status', ['queued', 'running', 'failed', 'succeeded'])
async def test_late_poll_cannot_override_deleted_review(env, monkeypatch, provider, late_status):
    local(env)
    env.settings.provider = provider
    env.settings.api_key = 'synthetic-official-key'
    env.settings.auto_retry_limit = 9
    from app.api import routes
    from app.providers.base import TaskInfo
    class Fake:
        def cancel(self, task_id):
            return {'task_id': task_id, 'action': 'deleted', 'status': 'deleted'}
    monkeypatch.setattr(routes, 'get_provider', lambda settings: Fake())
    tid = take(env)
    env.db.update_take(tid, task_id='a' * 32, status='queued')
    with api_client(env, monkeypatch) as client:
        assert client.post(f'/api/takes/{tid}/cancel').status_code == 200
    await env.runner._apply_task_info(tid, TaskInfo(
        'a' * 32, late_status, error_code='backend_error',
        video_url='http://synthetic.example/stale'))
    row = env.db.get_take(tid)
    assert (row['status'], row['platform_status']) == ('review', 'deleted')
    assert len(env.db.list_takes()) == 1


def test_cancel_claim_blocks_poll_while_delete_is_in_flight(env, monkeypatch):
    from app.api import routes
    from app.providers.base import TaskInfo
    env.settings.provider = 'official'
    env.settings.api_key = 'synthetic-official-key'
    env.settings.auto_retry_limit = 9
    tid = take(env)
    env.db.update_take(tid, task_id='a' * 32, status='queued')
    class Fake:
        def cancel(self, task_id):
            asyncio.run(env.runner._apply_task_info(tid, TaskInfo(
                task_id, 'failed', error_code=1000, error_message='Synthetic stale poll')))
            return {'task_id': task_id, 'action': 'deleted', 'status': 'deleted'}
    monkeypatch.setattr(routes, 'get_provider', lambda settings: Fake())
    with api_client(env, monkeypatch) as client:
        assert client.post(f'/api/takes/{tid}/cancel').status_code == 200
    assert len(env.db.list_takes()) == 1
    assert env.db.get_take(tid)['status'] == 'review'


def test_switching_to_local_requires_explicit_local_key(env, monkeypatch):
    env.settings.h3_api_key = 'synthetic-previous-official-key-0000'
    with api_client(env, monkeypatch) as client:
        response = client.put('/api/settings', json={
            'provider': 'local', 'h3_base_url': 'http://127.0.0.1:30010'})
    assert response.status_code == 400
    assert env.settings.provider == 'mock'


@pytest.mark.parametrize('status,task_id', [('unknown', None), ('submitting', None),
                                          ('backoff', None), ('queued', 'a' * 32)])
async def test_legacy_inflight_without_identity_is_never_reposted(env, monkeypatch, status, task_id):
    tid = take(env)
    env.db.update_take(tid, status=status, task_id=task_id, idempotency_key='',
                       provider_name='', provider_origin='')
    env.db._conn.close(); env.db._conn = None
    local(env)
    row = env.db.get_take(tid)
    assert row['status'] == 'review'
    assert row['idempotency_key'] == ''
    from app.pipeline import runner
    def forbidden(settings):
        raise AssertionError('Migration must never invent the identity of an earlier submission')
    monkeypatch.setattr(runner, 'get_provider', forbidden)
    await env.runner._reconcile_unknown(tid)
    await env.runner._submit_due()
    assert not env.runner._inflight


def test_only_never_submitted_pending_legacy_take_receives_new_identity(env):
    tid = take(env)
    env.db.update_take(tid, idempotency_key='', provider_name='', provider_origin='')
    env.db._conn.close(); env.db._conn = None
    row = env.db.get_take(tid)
    assert row['status'] == 'pending' and row['idempotency_key']


def test_interrupted_cancel_restarts_into_manual_review(env):
    local(env)
    tid = take(env)
    env.db.update_take(tid, status='cancelling', task_id='a' * 32)
    env.runner.reconcile_on_startup()
    assert env.db.get_take(tid)['status'] == 'review'


async def test_manual_review_during_download_is_not_overwritten(env, monkeypatch):
    local(env)
    from app.pipeline import runner
    from app.providers.base import TaskInfo
    tid = take(env)
    env.db.update_take(tid, task_id='b' * 32, status='succeeded')
    class Fake:
        def get_task(self, task_id):
            return TaskInfo(task_id, 'succeeded', video_url='http://synthetic.example/media')
        def download(self, url):
            env.db.update_take(tid, status='review', platform_status='deleted')
            return b'synthetic video'
    monkeypatch.setattr(runner, 'get_provider', lambda settings: Fake())
    monkeypatch.setattr(runner, 'probe_video', lambda path: {'width': 768, 'height': 768, 'duration': 5})
    await env.runner._download_one(tid)
    row = env.db.get_take(tid)
    assert (row['status'], row['platform_status'], row['local_path']) == ('review', 'deleted', None)
