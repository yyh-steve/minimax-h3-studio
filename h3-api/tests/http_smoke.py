"""Real loopback HTTP lifecycle using generated test media, never H3 weights."""
import base64,hashlib,io,json,os,secrets,socket,subprocess,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from PIL import Image
from h3_api.config import Settings
from h3_api.worker import Worker
from test_worker import SyntheticBackend

ROOT=Path(__file__).resolve().parents[1]

def main():
    with tempfile.TemporaryDirectory(prefix='h3-api-smoke-') as tmp:
        key=secrets.token_urlsafe(32)
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','LANG','TMPDIR')}
        env.update(PYTHONPATH=str(ROOT),H3_API_KEY=key,H3_DATA_DIR=tmp,H3_PUBLIC_BASE_URL=base)
        with (Path(tmp)/'api.log').open('wb') as log:
            proc=subprocess.Popen([sys.executable,'-m','h3_api','api','--port',str(port)],env=env,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
            try:
                deadline=time.monotonic()+15
                while True:
                    try:
                        if httpx.get(base+'/healthz',timeout=1,trust_env=False).status_code==200:break
                    except httpx.HTTPError:pass
                    if proc.poll() is not None or time.monotonic()>deadline:raise RuntimeError('Local test API did not start')
                    time.sleep(.1)
                with httpx.Client(base_url=base,timeout=30,trust_env=False) as c:
                    headers={'Authorization':'Bearer '+key}
                    buf=io.BytesIO();Image.new('RGB',(256,256),'blue').save(buf,'PNG')
                    body={'model':'MiniMax-H3','duration':4,'resolution':'768P','ratio':'1:1','content':[
                        {'type':'text','text':'Synthetic protocol test, not model generation.'},
                        {'type':'image_url','role':'reference_image','image_url':{'url':'data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()}}]}
                    assert c.post('/v2/video_generation',json=body).status_code==401
                    posted=c.post('/v2/video_generation',headers=headers,json=body);posted.raise_for_status();tid=posted.json()['task_id']
                    assert c.get('/v2/query/video_generation/'+tid,headers=headers).json()['task']['status']=='queued'
                    settings=Settings(data_dir=Path(tmp),api_key=key,public_base_url=base)
                    with Worker(settings,SyntheticBackend()) as w:assert w.run_once()
                    task=c.get('/v2/query/video_generation/'+tid,headers=headers).json()['task'];assert task['status']=='succeeded'
                    video=c.get(task['content']['url']);video.raise_for_status();assert len(video.content)>0
                    assert c.get('/v2/query/video_generation',headers=headers).json()['total']==1
                    cancelled=c.post('/v2/video_generation',headers=headers,json=body).json()['task_id']
                    assert c.delete('/v2/video_generation/'+cancelled,headers=headers).json()['status']=='cancelled'
                    bad=dict(body,resolution='2K');assert c.post('/v2/video_generation',headers=headers,json=bad).status_code==400
                    image=Path(tmp)/'client-reference.png';image.write_bytes(buf.getvalue())
                    prompt=Path(tmp)/'client-prompt.txt';prompt.write_text('Synthetic client test, no H3 weights.')
                    target=Path(tmp)/'client-output.mp4'
                    client=subprocess.Popen([sys.executable,str(ROOT/'examples/client.py'),'--image',str(image),
                        '--prompt-file',str(prompt),'--duration','4','--ratio','1:1','--base-url',base,
                        '--output',str(target),'--poll-interval','0.1'],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                    try:
                        client_deadline=time.monotonic()+20
                        with Worker(settings,SyntheticBackend()) as w:
                            while client.poll() is None and time.monotonic()<client_deadline:
                                w.run_once();time.sleep(.05)
                        stdout,stderr=client.communicate(timeout=5)
                        assert client.returncode==0,stderr.decode()
                    finally:
                        if client.poll() is None:client.kill();client.wait(timeout=5)
                    receipt_path=target.with_name(target.name+'.task.json')
                    receipt=json.loads(receipt_path.read_text());assert key not in receipt_path.read_text()
                    total_before=c.get('/v2/query/video_generation',headers=headers).json()['total']
                    resumed=Path(tmp)/'resumed.mp4'
                    resume=subprocess.run([sys.executable,str(ROOT/'examples/client.py'),'--task-id',receipt['task_id'],
                        '--base-url',base,'--output',str(resumed),'--poll-interval','0.1'],env=env,capture_output=True,timeout=10)
                    assert resume.returncode==0,resume.stderr.decode()
                    assert resumed.read_bytes()==target.read_bytes()
                    assert c.get('/v2/query/video_generation',headers=headers).json()['total']==total_before
                    result={'passed':True,'scope':'real loopback HTTP + SQLite + worker + ffmpeg synthetic media; NO H3 inference',
                        'checks':['Bearer authentication','POST task_id','queued query','worker execution','succeeded query','signed MP4 download without Bearer','list','queued cancel','unsupported 2K rejection','example client creates and saves receipt','example client resumes without another POST'],
                        'synthetic_video_bytes':len(video.content),'synthetic_video_sha256':hashlib.sha256(video.content).hexdigest(),
                        'source_video_is_h3_output':False}
                    out=ROOT/'test-results';out.mkdir(exist_ok=True);(out/'http-smoke.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
                    print(json.dumps(result,ensure_ascii=False))
            finally:
                proc.terminate()
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
if __name__=='__main__':main()
