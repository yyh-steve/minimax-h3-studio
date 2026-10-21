"""Subprocess bridge; HTTP never imports torch or invokes a fake model backend."""
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

class BackendError(Exception):
    pass

def validate_video(path,duration,width,height):
    path=Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size==0:
        raise BackendError('Missing output video')
    try:
        r=subprocess.run(['ffprobe','-v','error','-count_frames','-show_entries',
            'format=duration:stream=codec_type,width,height,avg_frame_rate,nb_read_frames,sample_rate,channels,duration_ts,time_base,duration',
            '-of','json',str(path)],capture_output=True,text=True,check=True,timeout=60)
        data=json.loads(r.stdout)
        videos=[x for x in data['streams'] if x.get('codec_type')=='video']
        audios=[x for x in data['streams'] if x.get('codec_type')=='audio']
        if len(videos)!=1 or len(audios)!=1:raise ValueError('Wrong stream count')
        v,a=videos[0],audios[0]
        if (v.get('width'),v.get('height'))!=(width,height) or Fraction(v['avg_frame_rate'])!=24 or int(v['nb_read_frames'])!=24*duration:
            raise ValueError('Wrong video geometry or frame count')
        if int(a['sample_rate'])!=32000 or a['channels']!=2:raise ValueError('Wrong audio format')
        for stream in (v,a):
            t=Fraction(stream['duration_ts'])*Fraction(stream['time_base']) if 'duration_ts' in stream else Fraction(stream['duration'])
            if t!=duration:raise ValueError('Wrong presentation duration')
        if Fraction(data['format']['duration'])!=duration:raise ValueError('Wrong container duration')
        subprocess.run(['ffmpeg','-nostdin','-v','error','-xerror','-i',str(path),'-map','0:v:0','-map','0:a:0','-f','null','-'],capture_output=True,check=True,timeout=120)
    except (KeyError,ValueError,subprocess.SubprocessError,OSError) as exc:
        raise BackendError('Output media validation failed') from exc
    return {'width':width,'height':height,'frames':24*duration,'duration':duration,
            'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

class RunnerBackend:
    @staticmethod
    def command(job,job_dir,settings):
        p=job['payload'];canvas=job['canvas']
        return [str(settings.model_python),'-u',str(settings.runner_dir/'h3_cpu_trial.py'),'infer',
            '--execute-inference','--model-dir',str(settings.model_dir),
            '--references-json',str(job_dir/'references.json'),'--prompt-file',str(job_dir/'prompt.txt'),
            '--output',str(job_dir/'candidate.mp4'),'--width',str(canvas['width']),'--height',str(canvas['height']),
            '--duration-seconds',str(p['duration']),'--steps','49','--dtype','bf16','--threads',str(settings.threads),
            '--seed',str(job['seed']),'--max-runtime-seconds',str(settings.job_timeout)]

    @staticmethod
    def _stop(proc):
        try:os.killpg(proc.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            proc.wait(timeout=10)
        # Reap any remaining members of the isolated generation process group.
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass

    def run(self,job,job_dir,settings,lock_fd,stop):
        if not settings.model_python.is_file() or not (settings.runner_dir/'h3_cpu_trial.py').is_file():
            raise BackendError('Configure the installed CPU runner and Python interpreter')
        allowed=('PATH','HOME','LANG','LC_ALL','LD_LIBRARY_PATH','DYLD_LIBRARY_PATH','TMPDIR','TEMP','TMP','HF_HOME','HUGGINGFACE_HUB_CACHE')
        env={k:v for k,v in os.environ.items() if k in allowed}
        env.update(OMP_PLACES='cores',OMP_PROC_BIND='spread',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
        proc=None
        with (job_dir/'runner.log').open('wb') as log:
            try:
                proc=subprocess.Popen(self.command(job,job_dir,settings),cwd=settings.runner_dir,
                    stdout=log,stderr=subprocess.STDOUT,env=env,start_new_session=True,pass_fds=(lock_fd,))
                deadline=time.monotonic()+settings.job_timeout
                while proc.poll() is None:
                    if stop.wait(.25) or time.monotonic()>=deadline:
                        self._stop(proc);raise BackendError('Generation stopped or exceeded its job time limit')
                if proc.returncode!=0:raise BackendError('CPU runner exited unsuccessfully')
            except BaseException:
                if proc is not None and proc.poll() is None:self._stop(proc)
                raise
        report_path=job_dir/'candidate.report.json'
        try:
            report=json.loads(report_path.read_text())
            if report['status']!='completed_media_validated' or report['workflow']!='ref2va' or report['observed_denoiser_forwards']!=49:
                raise ValueError('Incomplete model report')
            if report['media_validation']['duration_seconds_validated']!=job['payload']['duration']:
                raise ValueError('Unverified requested duration')
            expected={'duration_seconds':job['payload']['duration'],'width':job['canvas']['width'],
                      'height':job['canvas']['height'],'steps':49,'seed':job['seed']}
            if any(report['arguments'].get(key)!=value for key,value in expected.items()):
                raise ValueError('Runner arguments differ from the accepted task')
            if len(report['references'])!=1 or report['references'][0].get('sha256')!=job['reference_sha256']:
                raise ValueError('Runner reference differs from the accepted image')
        except (OSError,KeyError,ValueError) as exc:raise BackendError('CPU runner completion report was not valid') from exc
        return job_dir/'candidate.mp4'
