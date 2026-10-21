"""One POSIX worker per state directory. No automatic retry of interrupted work."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import threading
import time
import traceback
from .backend import RunnerBackend,BackendError,validate_video
from .media import prepare_image,choose_canvas,MediaError
from .store import Store

class WorkerLock:
    def __init__(self,path):self.path=Path(path);self.fd=None
    def __enter__(self):
        self.fd=os.open(self.path,os.O_CREAT|os.O_RDWR,0o600)
        try:fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd);self.fd=None
            raise RuntimeError('Another worker or inherited generation process still holds the worker lock') from None
        return self
    def __exit__(self,*args):
        # No LOCK_UN: an orphaned child may still own the same open description.
        if self.fd is not None:os.close(self.fd);self.fd=None

class Worker:
    def __init__(self,settings,backend=None):
        self.settings=settings;self.store=Store(settings);self.backend=backend or RunnerBackend()
        self.stop=threading.Event();self.lock=None
    def __enter__(self):
        self.lock=WorkerLock(self.settings.data_dir/'worker.lock');self.lock.__enter__()
        self.store.recover_interrupted();return self
    def __exit__(self,*args):
        self.stop.set()
        if self.lock:self.lock.__exit__(*args);self.lock=None
    def run_once(self):
        if self.lock is None:raise RuntimeError('Enter the Worker context before processing jobs')
        if self.stop.is_set():return False
        job=self.store.claim()
        if job is None:return False
        directory=self.settings.data_dir/'jobs'/job['id']
        try:
            if shutil.disk_usage(self.settings.data_dir).free<1024**3:
                raise BackendError('Insufficient free job storage')
            directory.mkdir(parents=True,mode=0o700,exist_ok=False)
            os.chmod(directory,0o700)
            image=prepare_image(job['payload']['image_source'],directory/'reference.png')
            if self.stop.is_set():
                raise BackendError('Worker shutdown requested before model launch')
            job['canvas']=choose_canvas(job['payload']['ratio'],image.width,image.height)
            job['reference_sha256']=image.sha256
            (directory/'references.json').write_text(json.dumps([{'type':'image','path':'reference.png'}])+'\n')
            (directory/'prompt.txt').write_text(job['payload']['text'],encoding='utf8')
            for p in (directory/'prompt.txt',directory/'references.json'):os.chmod(p,0o600)
            output=Path(self.backend.run(job,directory,self.settings,self.lock.fd,self.stop)).resolve()
            if output!=directory.resolve()/'candidate.mp4':raise BackendError('Backend output was outside its isolated task path')
            m=validate_video(output,job['payload']['duration'],job['canvas']['width'],job['canvas']['height'])
            m['ratio']=job['canvas']['ratio'];m['reference_sha256']=image.sha256
            output.replace(directory/'output.mp4');os.chmod(directory/'output.mp4',0o600)
            self.store.finish(job['id'],m)
        except MediaError:
            self.store.fail(job['id'],'invalid_media','The reference image is invalid, unsupported, too large, or could not be fetched safely.')
        except Exception:
            if directory.exists():
                with (directory/'worker-error.log').open('a') as f:traceback.print_exc(file=f)
            self.store.fail(job['id'],'worker_failed','The local generation worker failed. The operator can inspect the private task log; this task will not be rerun automatically.')
        return True
    def run_forever(self):
        while not self.stop.is_set():
            if not self.run_once():self.stop.wait(self.settings.poll_seconds)
