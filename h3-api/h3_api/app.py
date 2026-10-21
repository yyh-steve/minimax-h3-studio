"""MiniMax V2-shaped HTTP subset; never executes model work in request handlers."""
import hashlib
import hmac
import json
import re
import time
import uuid
from urllib.parse import urlencode
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse,FileResponse
from starlette.exceptions import HTTPException
from .contracts import normalize_request,STATES
from .errors import ApiError
from .store import Store

ID_RE=re.compile(r'^[0-9a-f]{32}$')

def _signature(settings,tid,expires):
    return hmac.new(settings.api_key.encode(),f'h3-download-v1:{tid}:{expires}'.encode(),hashlib.sha256).hexdigest()

def download_url(settings,tid):
    expires=int(time.time())+settings.download_ttl
    return settings.public_base_url+f'/media/{tid}.mp4?'+urlencode({'expires':expires,'sig':_signature(settings,tid,expires)})

def public_task(settings,job):
    p=job['payload'];result=job['result']
    task={'id':job['id'],'model':'MiniMax-H3','status':job['status'],
        'created_at':int(job['created_at']),'updated_at':int(job['updated_at']),
        'resolution':p['resolution'],'duration':p['duration'],'ratio':result['ratio'] if result else p['ratio'],
        'task_type':'generation','modality':'video'}
    if job['status']=='succeeded':
        task['content']={'url':download_url(settings,job['id'])}
        task['usage']={'total_seconds':result['duration'],'input_seconds':0,'output_seconds':result['duration'],'input_image_count':1}
    elif job['status']=='failed':task['error']={'code':job['error_code'],'message':job['error_message']}
    return task

def create_app(settings):
    store=Store(settings)
    app=FastAPI(title='Local H3 V2 subset',version='0.1.1',docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store=store;app.state.settings=settings

    def authenticate(request):
        parts=request.headers.get('authorization','').split(' ',1)
        if len(parts)!=2 or parts[0].lower()!='bearer' or not hmac.compare_digest(parts[1].encode(),settings.api_key.encode()):
            raise ApiError(401,'Use the local service Bearer API key.','authorized_error')
    def valid_id(tid):
        if not ID_RE.fullmatch(tid):raise ApiError(404,'Task not found.','not_found_error')
    def error_response(status,message,kind):
        rid=uuid.uuid4().hex
        return JSONResponse({'type':'error','error':{'type':kind,'message':message,'http_code':str(status)},'request_id':rid},status_code=status,headers={'X-Request-ID':rid,'Cache-Control':'no-store'})
    @app.exception_handler(ApiError)
    async def api_error(request,error):return error_response(error.status,error.message,error.kind)
    @app.exception_handler(HTTPException)
    async def http_error(request,error):return error_response(error.status_code,'Resource or method not available.','not_found_error' if error.status_code==404 else 'bad_request_error')
    @app.exception_handler(Exception)
    async def server_error(request,error):return error_response(500,'Internal service failure.','server_error')

    @app.get('/healthz')
    def health():return {'status':'ok','service':'h3-local-api','model_readiness':'not_checked'}

    @app.get('/capabilities')
    def capabilities(request:Request):
        authenticate(request)
        return {'model':'MiniMax-H3','workflow':'single_image_ref2va','resolution':['768P'],
            'durations':list(range(4,16)),'ratios':['adaptive','21:9','16:9','4:3','1:1','3:4','9:16'],
            'image_formats':['JPEG','PNG','WEBP'],'input_types':['https','data_uri'],
            'worker_concurrency':1,'validation':'Model validation is specific to the deployment, duration and canvas; see the deployment verification record.',
            'local_extensions':['Idempotency-Key','signed media URLs']}

    @app.post('/v2/video_generation')
    async def create(request:Request):
        authenticate(request)
        if request.headers.get('content-type','').split(';',1)[0].strip().lower()!='application/json':
            raise ApiError(400,'Content-Type must be application/json.')
        length=request.headers.get('content-length')
        if length:
            try:declared=int(length)
            except ValueError:raise ApiError(400,'Invalid Content-Length.')
            if declared<0:raise ApiError(400,'Invalid Content-Length.')
            if declared>settings.max_body_bytes:raise ApiError(413,'Request body is too large.','request_too_large_error')
        data=bytearray()
        async for chunk in request.stream():
            if len(data)+len(chunk)>settings.max_body_bytes:raise ApiError(413,'Request body is too large.','request_too_large_error')
            data.extend(chunk)
        try:raw=json.loads(data)
        except (ValueError,UnicodeError,RecursionError):raise ApiError(400,'Malformed JSON request.')
        normalized=normalize_request(raw)
        idem=request.headers.get('idempotency-key')
        if idem is not None and (not 1<=len(idem)<=128 or any(ord(c)<33 or ord(c)>126 for c in idem)):
            raise ApiError(400,'Idempotency-Key must contain 1 to 128 printable non-space ASCII characters.')
        # SQLite writes are short; model and URL work is exclusively in the worker.
        from starlette.concurrency import run_in_threadpool
        job,_=await run_in_threadpool(store.submit,normalized,idem)
        return {'task_id':job['id']}

    @app.get('/v2/query/video_generation/{task_id}')
    def query(task_id:str,request:Request):
        authenticate(request);valid_id(task_id)
        job=store.get(task_id)
        if job is None:raise ApiError(404,'Task not found.','not_found_error')
        return {'task':public_task(settings,job)}

    @app.get('/v2/query/video_generation')
    def listing(request:Request):
        authenticate(request);q=request.query_params
        if set(q)-{'page_num','page_size','filter.status','filter.task_ids','filter.model','filter.task_type'}:
            raise ApiError(400,'Unsupported list query parameter.')
        try:page_num=int(q.get('page_num','1'));page_size=int(q.get('page_size','20'))
        except ValueError:raise ApiError(400,'Invalid page number or page size.')
        if page_num<1 or page_num>1_000_000 or not 1<=page_size<=100:raise ApiError(400,'page_num must be positive and page_size must be between 1 and 100.')
        status=q.get('filter.status')
        if status is not None and status not in STATES:raise ApiError(400,'Invalid task status filter.')
        ids=q.getlist('filter.task_ids')
        if len(ids)>100 or any(not ID_RE.fullmatch(t) for t in ids):raise ApiError(400,'Invalid or excessive task ID filter.')
        jobs,total=store.list_jobs(ids=ids,status=status,model=q.get('filter.model'),task_type=q.get('filter.task_type'),page_num=page_num,page_size=page_size)
        return {'items':[public_task(settings,j) for j in jobs],'total':total}

    @app.delete('/v2/video_generation/{task_id}')
    def delete(task_id:str,request:Request):
        authenticate(request);valid_id(task_id)
        return store.delete_or_cancel(task_id)

    @app.get('/media/{task_id}.mp4')
    def media(task_id:str,request:Request):
        valid_id(task_id);q=request.query_params
        try:expires=int(q.get('expires','0'))
        except ValueError:raise ApiError(403,'Invalid or expired download URL.','authorized_error')
        sig=q.get('sig','')
        if not re.fullmatch('[0-9a-f]{64}',sig) or expires<=int(time.time()) or not hmac.compare_digest(sig,_signature(settings,task_id,expires)):
            raise ApiError(403,'Invalid or expired download URL.','authorized_error')
        job=store.get(task_id)
        if job is None or job['status']!='succeeded':raise ApiError(404,'Result not found.','not_found_error')
        path=settings.data_dir/'jobs'/task_id/'output.mp4'
        if path.is_symlink() or not path.is_file() or path.stat().st_size!=job['result']['bytes']:
            raise ApiError(404,'Result file is unavailable.','not_found_error')
        return FileResponse(path,media_type='video/mp4',filename=task_id+'.mp4',headers={'Cache-Control':'private, no-store'})
    return app
