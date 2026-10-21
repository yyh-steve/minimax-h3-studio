"""Transactional FIFO state. HTTP restart does not modify running tasks."""
import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from .errors import ApiError

class Store:
    def __init__(self, settings):
        self.settings=settings
        settings.data_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
        os.chmod(settings.data_dir,0o700)
        self.path=settings.data_dir/'tasks.sqlite3'
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''CREATE TABLE IF NOT EXISTS tasks (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL, seed INTEGER NOT NULL,
                idem TEXT UNIQUE, request_hash TEXT NOT NULL,
                error_code TEXT, error_message TEXT, result TEXT,
                deleted INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status,seq);''')
        os.chmod(self.path,0o600)

    @contextlib.contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=10)
        c.row_factory=sqlite3.Row
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback();raise
        finally:c.close()

    def _row(self,row):
        if row is None:return None
        d=dict(row);d['payload']=json.loads(d['payload']);d['result']=json.loads(d['result']) if d['result'] else None
        return d

    def _expire_queued(self,c,now):
        c.execute("UPDATE tasks SET status='failed',error_code='task_expired',error_message='The queued task expired.',updated_at=? WHERE status='queued' AND created_at<?",(now,now-self.settings.retention_seconds))

    def submit(self,payload,idem=None):
        encoded=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        digest=hashlib.sha256(encoded.encode()).hexdigest();now=time.time()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');self._expire_queued(c,now)
            if idem:
                old=c.execute('SELECT * FROM tasks WHERE idem=?',(idem,)).fetchone()
                if old:
                    if old['request_hash']!=digest or old['deleted'] or old['created_at']<now-self.settings.retention_seconds:
                        raise ApiError(409,'Idempotency-Key was already used for a different, deleted, or expired task.','conflict_error')
                    return self._row(old),False
            n=c.execute("SELECT COUNT(*) FROM tasks WHERE deleted=0 AND status IN ('queued','running')").fetchone()[0]
            if n>=self.settings.queue_limit:raise ApiError(429,'The local generation queue is full.','rate_limit_error')
            tid=uuid.uuid4().hex
            c.execute('INSERT INTO tasks(id,created_at,updated_at,status,payload,seed,idem,request_hash) VALUES(?,?,?,?,?,?,?,?)',
                (tid,now,now,'queued',encoded,secrets.randbelow(2**31),idem,digest))
            return self._row(c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()),True

    def get(self,tid):
        with self.connect() as c:
            return self._row(c.execute('SELECT * FROM tasks WHERE id=? AND deleted=0 AND created_at>=?',(tid,time.time()-self.settings.retention_seconds)).fetchone())

    def list_jobs(self,*,ids=None,status=None,model=None,task_type=None,page_num=1,page_size=20):
        if model not in (None,'MiniMax-H3') or task_type not in (None,'generation'):return [],0
        where=['deleted=0','created_at>=?'];params=[time.time()-self.settings.retention_seconds]
        if status:where.append('status=?');params.append(status)
        if ids:
            where.append('id IN ('+','.join('?' for _ in ids)+')');params.extend(ids)
        expr=' AND '.join(where)
        with self.connect() as c:
            total=c.execute('SELECT COUNT(*) FROM tasks WHERE '+expr,params).fetchone()[0]
            rows=c.execute('SELECT * FROM tasks WHERE '+expr+' ORDER BY seq DESC LIMIT ? OFFSET ?',params+[page_size,(page_num-1)*page_size]).fetchall()
            return [self._row(r) for r in rows],total

    def claim(self):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');self._expire_queued(c,time.time())
            if c.execute("SELECT 1 FROM tasks WHERE status='running' AND deleted=0 LIMIT 1").fetchone():return None
            row=c.execute("SELECT * FROM tasks WHERE status='queued' AND deleted=0 ORDER BY seq LIMIT 1").fetchone()
            if row is None:return None
            c.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?",(time.time(),row['id']))
            return self._row(c.execute('SELECT * FROM tasks WHERE id=?',(row['id'],)).fetchone())

    def finish(self,tid,result):
        with self.connect() as c:
            count=c.execute("UPDATE tasks SET status='succeeded',result=?,updated_at=? WHERE id=? AND status='running' AND deleted=0",
                (json.dumps(result,separators=(',',':')),time.time(),tid)).rowcount
            if count!=1:raise RuntimeError('Task is not running')

    def fail(self,tid,code,message):
        with self.connect() as c:
            c.execute("UPDATE tasks SET status='failed',error_code=?,error_message=?,updated_at=? WHERE id=? AND status='running' AND deleted=0",
                (code,message[:500],time.time(),tid))

    def recover_interrupted(self):
        # Called only while holding the exclusive inherited worker lock.
        with self.connect() as c:
            c.execute("UPDATE tasks SET status='failed',error_code='worker_interrupted',error_message='The previous worker stopped before committing a result; no automatic rerun was started.',updated_at=? WHERE status='running'",(time.time(),))

    def delete_or_cancel(self,tid):
        now=time.time()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM tasks WHERE id=? AND deleted=0 AND created_at>=?',(tid,now-self.settings.retention_seconds)).fetchone()
            if row is None:raise ApiError(404,'Task not found.','not_found_error')
            if row['status']=='queued':
                c.execute("UPDATE tasks SET status='cancelled',updated_at=? WHERE id=?",(now,tid))
                return {'task_id':tid,'action':'cancelled','status':'cancelled'}
            if row['status'] in ('succeeded','failed'):
                c.execute('UPDATE tasks SET deleted=1,updated_at=? WHERE id=?',(now,tid))
                return {'task_id':tid,'action':'deleted','status':'deleted'}
            raise ApiError(400,'Only queued tasks can be cancelled; running and cancelled tasks cannot be deleted.')
