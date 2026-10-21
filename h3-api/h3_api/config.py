from dataclasses import dataclass, field
from pathlib import Path
import os
from urllib.parse import urlsplit

@dataclass(frozen=True)
class Settings:
    data_dir: Path
    api_key: str
    public_base_url: str = 'http://127.0.0.1:30010'
    queue_limit: int = 8
    max_body_bytes: int = 64_000_000
    download_ttl: int = 3600
    retention_seconds: int = 7 * 86400
    runner_dir: Path = Path('/data/h3-cpu')
    model_dir: Path = Path('/data/h3-cpu/model')
    model_python: Path = Path('/data/h3-cpu/.venv/bin/python')
    threads: int = 80
    job_timeout: int = 64800
    poll_seconds: float = 1.0

    def __post_init__(self):
        if not isinstance(self.api_key,str) or len(self.api_key)<24 or not self.api_key.isascii() or any(c.isspace() for c in self.api_key):
            raise ValueError('Set a local API key of at least 24 characters')
        u=urlsplit(self.public_base_url)
        _ = u.port
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ('','/'):
            raise ValueError('public_base_url must be an HTTP(S) origin without credentials or a path')
        for name in ('queue_limit','max_body_bytes','download_ttl','retention_seconds','threads','job_timeout'):
            value=getattr(self,name)
            if type(value) is not int or value<=0:raise ValueError(name+' must be a positive integer')
        if self.download_ttl>86400 or self.retention_seconds>7*86400 or self.poll_seconds<=0:
            raise ValueError('Invalid TTL, retention, or polling interval')
        object.__setattr__(self,'data_dir',Path(self.data_dir).resolve())
        for name in ('runner_dir','model_dir'):
            object.__setattr__(self,name,Path(getattr(self,name)).resolve())
        # A venv Python is commonly a symlink. Preserve its invocation path so
        # Python finds that venv's pyvenv.cfg instead of using the base environment.
        object.__setattr__(self,'model_python',Path(self.model_python).absolute())
        object.__setattr__(self,'public_base_url',self.public_base_url.rstrip('/'))

    @classmethod
    def from_env(cls):
        return cls(data_dir=Path(os.environ.get('H3_DATA_DIR','./h3-data')),
            api_key=os.environ.get('H3_API_KEY',''),public_base_url=os.environ.get('H3_PUBLIC_BASE_URL','http://127.0.0.1:30010'),
            queue_limit=int(os.environ.get('H3_QUEUE_LIMIT','8')),
            runner_dir=Path(os.environ.get('H3_RUNNER_DIR','/data/h3-cpu')),
            model_dir=Path(os.environ.get('H3_MODEL_DIR','/data/h3-cpu/model')),
            model_python=Path(os.environ.get('H3_MODEL_PYTHON','/data/h3-cpu/.venv/bin/python')),
            threads=int(os.environ.get('H3_THREADS','80')),job_timeout=int(os.environ.get('H3_JOB_TIMEOUT','64800')))
