import sys,io,base64,subprocess,tempfile,unittest,threading,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from h3_api.config import Settings
from h3_api.store import Store
from h3_api.worker import Worker,WorkerLock
from h3_api.backend import validate_video

class SyntheticBackend:
    def run(self,job,job_dir,settings,lock_fd,stop):
        out=job_dir/'candidate.mp4';p=job['payload']
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',f'color=blue:s={job["canvas"]["width"]}x{job["canvas"]["height"]}:r=24',
            '-f','lavfi','-i','anullsrc=r=32000:cl=stereo','-t',str(p['duration']),'-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-c:a','aac',str(out)],check=True,timeout=30)
        return out

class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Settings(data_dir=Path(self.tmp.name),api_key='b'*32)
        buf=io.BytesIO();Image.new('RGB',(256,256),'blue').save(buf,'PNG')
        self.payload={'model':'MiniMax-H3','text':'A blue scene.','image_source':'data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode(),'duration':4,'resolution':'768P','ratio':'1:1'}
        self.store=Store(self.s)
    def tearDown(self):self.tmp.cleanup()
    def test_real_media_publication(self):
        row,_=self.store.submit(self.payload)
        with Worker(self.s,SyntheticBackend()) as worker:self.assertTrue(worker.run_once())
        result=self.store.get(row['id']);self.assertEqual(result['status'],'succeeded')
        path=self.s.data_dir/'jobs'/row['id']/'output.mp4'
        m=validate_video(path,4,768,768)
        self.assertEqual(m['frames'],96);self.assertEqual(m['duration'],4)
    def test_backend_failure_is_durable_and_no_retry(self):
        class Fails:
            def run(self,*args):raise RuntimeError('/private/server/secret details')
        row,_=self.store.submit(self.payload)
        with Worker(self.s,Fails()) as worker:worker.run_once();self.assertFalse(worker.run_once())
        row=Store(self.s).get(row['id']);self.assertEqual(row['status'],'failed')
        self.assertNotIn('/private',row['error_message'])
    def test_invalid_media_never_runs_model(self):
        class NoRun:
            def run(self,*args):raise AssertionError('must not run')
        p=dict(self.payload);p['image_source']='data:image/png;base64,eA=='
        row,_=self.store.submit(p)
        with Worker(self.s,NoRun()) as worker:worker.run_once()
        self.assertEqual(self.store.get(row['id'])['error_code'],'invalid_media')
    def test_single_worker_lock_is_inherited_by_child(self):
        with WorkerLock(self.s.data_dir/'worker.lock') as lock:
            child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(1)'],pass_fds=(lock.fd,))
            with self.assertRaises(RuntimeError):
                with WorkerLock(self.s.data_dir/'worker.lock'):pass
        # Closing the parent descriptor must not unlock the child's inherited lock.
        with self.assertRaises(RuntimeError):
            with WorkerLock(self.s.data_dir/'worker.lock'):pass
        child.wait(timeout=5)
        with WorkerLock(self.s.data_dir/'worker.lock'):pass

if __name__=='__main__':unittest.main()

def test_shutdown_after_media_does_not_launch_model(tmp_path,monkeypatch):
    import h3_api.worker as module
    s=Settings(data_dir=tmp_path,api_key='x'*32)
    buf=io.BytesIO();Image.new('RGB',(256,256)).save(buf,'PNG')
    payload={'model':'MiniMax-H3','text':'test','duration':4,'resolution':'768P','ratio':'1:1',
        'image_source':'data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()}
    store=Store(s);row,_=store.submit(payload)
    calls=[]
    class Backend:
        def run(self,*args):calls.append(1);raise RuntimeError('should not be called')
    original=module.prepare_image
    with Worker(s,Backend()) as worker:
        def stop_after_prepare(*args,**kwargs):
            info=original(*args,**kwargs);worker.stop.set();return info
        monkeypatch.setattr(module,'prepare_image',stop_after_prepare)
        worker.run_once()
    assert not calls
    assert store.get(row['id'])['status']=='failed'
