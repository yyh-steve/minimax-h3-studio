import base64,io,json,sys,time
from pathlib import Path
import pytest
from PIL import Image
from h3_api.config import Settings
from h3_api.backend import RunnerBackend
from h3_api.store import Store
from h3_api.worker import Worker

SCRIPT = r'''
import argparse,json,os,subprocess,sys,hashlib,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('stage');p.add_argument('--execute-inference',action='store_true')
for key in ['model-dir','references-json','prompt-file','output','width','height','duration-seconds','steps','dtype','threads','seed','max-runtime-seconds']:p.add_argument('--'+key)
a=p.parse_args();out=Path(a.output);text=Path(a.prompt_file).read_text()
(out.parent/'bridge-audit.json').write_text(json.dumps({'api_key_in_environment':'H3_API_KEY' in os.environ,'prompt_in_argv':text in ' '.join(sys.argv),'prompt_read':text}))
SLEEP_LINE
subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',f'color=blue:s={a.width}x{a.height}:r=24','-f','lavfi','-i','anullsrc=r=32000:cl=stereo','-t',a.duration_seconds,'-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-c:a','aac',str(out)],check=True)
refs=json.loads(Path(a.references_json).read_text());ref=Path(a.references_json).parent/refs[0]['path']
args=vars(a)
for k in ['width','height','duration_seconds','steps','threads','seed','max_runtime_seconds']:args[k]=int(args[k])
report={'status':'completed_media_validated','workflow':'ref2va','observed_denoiser_forwards':49,'arguments':args,'references':[{'sha256':hashlib.sha256(ref.read_bytes()).hexdigest()}],'media_validation':{'duration_seconds_validated':int(a.duration_seconds)}}
BAD_LINE
out.with_suffix('.report.json').write_text(json.dumps(report))
'''

def setup_job(tmp_path,script):
    runner=tmp_path/'runtime';runner.mkdir();(runner/'h3_cpu_trial.py').write_text(script)
    s=Settings(data_dir=tmp_path/'state',api_key='test-only-local-service-key-000000',runner_dir=runner,model_python=Path(sys.executable),job_timeout=1 if 'time.sleep(5)' in script else 30)
    buf=io.BytesIO();Image.new('RGB',(256,256)).save(buf,'PNG')
    body={'model':'MiniMax-H3','text':'secret fixture prompt','duration':4,'resolution':'768P','ratio':'1:1','image_source':'data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()}
    store=Store(s);row,_=store.submit(body);return s,store,row

def test_real_subprocess_bridge_without_model_weights(tmp_path,monkeypatch):
    script=SCRIPT.replace('SLEEP_LINE','').replace('BAD_LINE','')
    s,db,row=setup_job(tmp_path,script);monkeypatch.setenv('H3_API_KEY',s.api_key)
    with Worker(s) as w:w.run_once()
    assert db.get(row['id'])['status']=='succeeded'
    audit=json.loads((s.data_dir/'jobs'/row['id']/'bridge-audit.json').read_text())
    assert audit=={'api_key_in_environment':False,'prompt_in_argv':False,'prompt_read':'secret fixture prompt'}

def test_invalid_model_report_never_publishes(tmp_path):
    script=SCRIPT.replace('SLEEP_LINE','').replace('BAD_LINE',"report['references'][0]['sha256']='wrong-reference'")
    s,db,row=setup_job(tmp_path,script)
    with Worker(s) as w:w.run_once()
    assert db.get(row['id'])['status']=='failed'
    assert not (s.data_dir/'jobs'/row['id']/'output.mp4').exists()

def test_subprocess_deadline_terminates_task(tmp_path):
    script=SCRIPT.replace('SLEEP_LINE','time.sleep(5)').replace('BAD_LINE','')
    s,db,row=setup_job(tmp_path,script);started=time.monotonic()
    with Worker(s) as w:w.run_once()
    assert time.monotonic()-started<5
    assert db.get(row['id'])['status']=='failed'
