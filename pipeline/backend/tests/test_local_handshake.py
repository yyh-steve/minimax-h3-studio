"""Local idempotent submission requires the actual local server contract."""
import httpx
import pytest

KEY = 'local-handshake-test-key-not-a-real-secret'
CAPS = {'model': 'MiniMax-H3', 'workflow': 'single_image_ref2va',
        'local_extensions': ['Idempotency-Key', 'signed media URLs']}


def body():
    return {'model': 'MiniMax-H3', 'resolution': '768P', 'duration': 5,
            'ratio': '1:1', 'content': [
                {'type': 'text', 'text': 'Synthetic protocol test.'},
                {'type': 'image_url', 'role': 'reference_image',
                 'image_url': {'url': 'https://example.org/reference.png'}}]}


def test_handshake_precedes_post_and_rechecks_each_attempt():
    from app.providers.h3_local import LocalH3
    seen = []
    def respond(request):
        seen.append((request.method, request.url.path))
        assert request.headers['Authorization'] == 'Bearer ' + KEY
        if request.method == 'GET':
            assert 'Idempotency-Key' not in request.headers
            return httpx.Response(200, json=CAPS)
        assert request.headers['Idempotency-Key'] == 'persisted-test-key'
        return httpx.Response(200, json={'task_id': 'same-existing-task'})
    provider = LocalH3('http://local.example', KEY, transport=httpx.MockTransport(respond))
    for _ in range(2):
        assert provider.create(body(), idempotency_key='persisted-test-key').task_id == 'same-existing-task'
    assert seen == [('GET', '/capabilities'), ('POST', '/v2/video_generation')] * 2


@pytest.mark.parametrize('caps', [{}, [], {'model': 'MiniMax-H3'},
    {**CAPS, 'workflow': 'other'}, {**CAPS, 'local_extensions': []},
    {**CAPS, 'local_extensions': 'Idempotency-Key'}])
def test_unverified_server_never_receives_post(caps):
    from app.providers.h3_local import LocalH3
    from app.providers.base import ProviderError
    methods = []
    def respond(request):
        methods.append(request.method)
        return httpx.Response(200, json=caps)
    provider = LocalH3('http://local.example', KEY, transport=httpx.MockTransport(respond))
    with pytest.raises(ProviderError):
        provider.create(body(), idempotency_key='persisted-test-key')
    assert methods == ['GET']


@pytest.mark.parametrize('origin', ['https://api.minimax.cn', 'https://api.minimaxi.com',
                                   'https://API.MINIMAX.IO:443'])
def test_local_rejects_official_origin_before_transmitting_key(origin):
    from app.config import Settings
    from app.providers.h3_local import LocalH3
    from app.providers.base import ProviderError
    assert Settings(provider='local', h3_base_url=origin, h3_api_key=KEY).validate_provider()
    def reject_network(request):
        raise AssertionError('No request or credential may be sent to an official origin in local mode')
    provider = LocalH3(origin, KEY, transport=httpx.MockTransport(reject_network))
    with pytest.raises(ProviderError):
        provider.create(body(), idempotency_key='persisted-test-key')
