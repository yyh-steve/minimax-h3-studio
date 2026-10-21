import base64
import io
import time
from pathlib import Path

import pytest
from PIL import Image
from asgi_client import TestClient

from h3_api.app import create_app
from h3_api.config import Settings
from h3_api.store import Store

KEY = 'local-test-key-not-a-production-secret'
AUTH = {'Authorization': 'Bearer ' + KEY}

def request_body(duration=4):
    buf = io.BytesIO()
    Image.new('RGB', (256, 256), 'blue').save(buf, format='PNG')
    return {'model':'MiniMax-H3','content':[{'type':'text','text':'A quiet close-up.'},
        {'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()},'role':'reference_image'}],
        'resolution':'768P','duration':duration,'ratio':'1:1'}

@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=tmp_path, api_key=KEY, public_base_url='http://testserver')
    return settings, TestClient(create_app(settings))

def test_auth_and_error_envelope(setup):
    _, c = setup
    r = c.post('/v2/video_generation', json=request_body())
    assert r.status_code == 401
    assert r.json()['type']=='error' and r.json()['error']['http_code']=='401'
    assert r.json()['request_id']

def test_create_query_list_cancel_persist(setup):
    s,c=setup
    r=c.post('/v2/video_generation', headers=AUTH, json=request_body())
    assert r.status_code==200
    tid=r.json()['task_id']
    task=c.get('/v2/query/video_generation/'+tid,headers=AUTH).json()['task']
    assert task['status']=='queued' and task['duration']==4
    assert 'url' not in task.get('content',{})
    listing=c.get('/v2/query/video_generation',headers=AUTH,params=[('filter.task_ids',tid)]).json()
    assert listing['total']==1 and listing['items'][0]['id']==tid
    r=c.delete('/v2/video_generation/'+tid,headers=AUTH)
    assert r.json()=={'task_id':tid,'action':'cancelled','status':'cancelled'}
    other=TestClient(create_app(s))
    assert other.get('/v2/query/video_generation/'+tid,headers=AUTH).json()['task']['status']=='cancelled'
    assert other.delete('/v2/video_generation/'+tid,headers=AUTH).status_code==400

@pytest.mark.parametrize('change',[
    {'duration':True},{'duration':3},{'duration':16},{'duration':'5'}, {'resolution':'2K'},
    {'model':'MiniMax-H3-Max'},{'callback_url':'https://example.com/cb'},
    {'aigc_watermark':True},{'seed':1},{'extra':{}},{'ratio':'17:8'},
])
def test_unsupported_or_invalid_request_rejected(setup,change):
    _,c=setup;b=request_body();b.update(change)
    r=c.post('/v2/video_generation',headers=AUTH,json=b)
    assert r.status_code==400 and r.json()['type']=='error'

@pytest.mark.parametrize('role',['first_frame','last_frame','reference_video'])
def test_only_single_ref_image_is_supported(setup,role):
    _,c=setup;b=request_body();b['content'][1]['role']=role
    assert c.post('/v2/video_generation',headers=AUTH,json=b).status_code==400

def test_idempotency_and_queue_limit(setup):
    s,c=setup
    h={**AUTH,'Idempotency-Key':'same-request'}
    first=c.post('/v2/video_generation',headers=h,json=request_body()).json()
    assert c.post('/v2/video_generation',headers=h,json=request_body()).json()==first
    b=request_body();b['duration']=5
    assert c.post('/v2/video_generation',headers=h,json=b).status_code==409
    for _ in range(s.queue_limit-1):
        assert c.post('/v2/video_generation',headers=AUTH,json=b).status_code==200
    assert c.post('/v2/video_generation',headers=AUTH,json=b).status_code==429

def test_running_delete_rejected_and_failed_delete_hides_record(setup):
    s,c=setup
    tid=c.post('/v2/video_generation',headers=AUTH,json=request_body()).json()['task_id']
    store=Store(s); job=store.claim()
    assert job['id']==tid
    assert c.delete('/v2/video_generation/'+tid,headers=AUTH).status_code==400
    store.fail(tid,'worker_failed','The generation worker failed.')
    q=c.get('/v2/query/video_generation/'+tid,headers=AUTH)
    assert q.status_code==200 and q.json()['task']['error']['code']=='worker_failed'
    assert c.delete('/v2/video_generation/'+tid,headers=AUTH).json()['action']=='deleted'
    assert c.get('/v2/query/video_generation/'+tid,headers=AUTH).status_code==404

def test_invalid_json_and_content_type_and_body_limit(tmp_path):
    s=Settings(data_dir=tmp_path,api_key=KEY,public_base_url='http://testserver',max_body_bytes=100)
    c=TestClient(create_app(s))
    r=c.post('/v2/video_generation',headers={**AUTH,'Content-Type':'application/json'},content='{' )
    assert r.status_code==400
    assert c.post('/v2/video_generation',headers=AUTH,content='test').status_code==400
    assert c.post('/v2/video_generation',headers=AUTH,json=request_body()).status_code==413

def test_settings_fail_closed(tmp_path):
    with pytest.raises(ValueError):Settings(data_dir=tmp_path,api_key='')
    with pytest.raises(ValueError):Settings(data_dir=tmp_path,api_key=KEY,public_base_url='http://user:pass@host')

def test_signed_download_expiry_tamper_and_terminal_deletion(setup):
    from h3_api.worker import Worker
    from test_worker import SyntheticBackend
    from h3_api.app import _signature
    s,c=setup
    tid=c.post('/v2/video_generation',headers=AUTH,json=request_body()).json()['task_id']
    with Worker(s,SyntheticBackend()) as w:w.run_once()
    task=c.get('/v2/query/video_generation/'+tid,headers=AUTH).json()['task']
    assert task['status']=='succeeded' and task['usage']['output_seconds']==4
    url=task['content']['url'];data=c.get(url)
    assert data.status_code==200 and data.headers['content-type']=='video/mp4'
    assert c.get(url+'&unused=1').status_code==200
    assert c.get(url.replace('sig=','sig=0')).status_code==403
    expires=int(time.time())-1
    assert c.get(f'/media/{tid}.mp4?expires={expires}&sig={_signature(s,tid,expires)}').status_code==403
    assert c.get(url,headers={'Range':'bytes=0-15'}).status_code==206
    c.delete('/v2/video_generation/'+tid,headers=AUTH)
    assert c.get(url).status_code==404

def test_retention_and_pagination(setup):
    s,c=setup;store=Store(s)
    ids=[c.post('/v2/video_generation',headers=AUTH,json=request_body()).json()['task_id'] for _ in range(3)]
    page=c.get('/v2/query/video_generation?page_num=2&page_size=1',headers=AUTH).json()
    assert page['total']==3 and page['items'][0]['id']==ids[1]
    with store.connect() as db:db.execute('UPDATE tasks SET created_at=? WHERE id=?',(time.time()-8*86400,ids[0]))
    assert c.get('/v2/query/video_generation/'+ids[0],headers=AUTH).status_code==404
    assert c.get('/v2/query/video_generation?page_size=0',headers=AUTH).status_code==400
    assert c.get('/v2/query/video_generation?filter.status=deleted',headers=AUTH).status_code==400
