"""SQLite 存储层。

演进：
- M1：takes（每次 H3 提交）/ events / settings
- M2：projects（一个商品一条片）/ project_assets（原图与裁切变体）
      / storyboards（分镜版本）/ shots（镜头）
      takes 增加 project_id，确认后为每个 shot 建一条 take
- M3：shot_takes（每个分镜版本里每镜当前选用的 take）
- M4：takes.qc_json（技术检查 + M3 看片结果）/ renders（合成成片）
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any

from .config import DB_PATH, ensure_dirs
from . import constants as C

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_transaction_depth = 0


SCHEMA = """
CREATE TABLE IF NOT EXISTS takes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id          INTEGER,
    storyboard_id       INTEGER,
    shot_key            TEXT NOT NULL DEFAULT 's1',
    attempt_no          INTEGER NOT NULL DEFAULT 1,
    parent_take_id      INTEGER,
    title               TEXT NOT NULL DEFAULT '',
    model               TEXT NOT NULL,
    request_json        TEXT NOT NULL,
    fingerprint         TEXT NOT NULL DEFAULT '',
    idempotency_key     TEXT NOT NULL DEFAULT '',
    provider_name       TEXT NOT NULL DEFAULT '',
    provider_origin     TEXT NOT NULL DEFAULT '',
    task_id             TEXT,
    status              TEXT NOT NULL,
    error_json          TEXT,
    http_status         INTEGER,
    next_run_at         REAL,
    submit_started_at   REAL,
    finished_at         REAL,
    local_path          TEXT,
    probe_json          TEXT,
    usage_json          TEXT,
    platform_status     TEXT,
    platform_ratio      TEXT,
    platform_resolution TEXT,
    platform_duration   INTEGER,
    raw_response_json   TEXT,
    qc_json             TEXT,
    source              TEXT NOT NULL DEFAULT 'api',   -- api|confirm|auto_retry|manual_retry
    created_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_takes_status ON takes(status);
CREATE INDEX IF NOT EXISTS idx_takes_task_id ON takes(task_id);
CREATE INDEX IF NOT EXISTS idx_takes_project_shot ON takes(project_id, shot_key);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    take_id INTEGER,
    project_id INTEGER,
    level TEXT NOT NULL DEFAULT 'info',
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',     -- draft|editing|confirmed|generating|reviewing|composing|done
    ratio TEXT NOT NULL DEFAULT '9:16',
    target_seconds INTEGER NOT NULL DEFAULT 30,
    style TEXT NOT NULL DEFAULT '',
    selling_points_json TEXT NOT NULL DEFAULT '[]',
    banned_words_json TEXT NOT NULL DEFAULT '[]',
    bgm_path TEXT,
    h3_model TEXT NOT NULL DEFAULT 'MiniMax-H3',
    draft_resolution TEXT NOT NULL DEFAULT '768P',
    active_storyboard_id INTEGER,
    confirmed_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS project_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    asset_id TEXT NOT NULL,                 -- 对外稳定 ID（文件名）
    kind TEXT NOT NULL DEFAULT 'original',  -- original|variant
    variant TEXT NOT NULL DEFAULT '',       -- ''|crop_<ratio>|pad_<ratio>
    parent_asset_id TEXT,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL DEFAULT '',
    width INTEGER,
    height INTEGER,
    mime TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_project ON project_assets(project_id);

CREATE TABLE IF NOT EXISTS storyboards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'draft',   -- draft|confirmed|superseded
    product_card_json TEXT NOT NULL DEFAULT '{}',
    qc_report_json TEXT,
    review_json TEXT,
    created_by TEXT NOT NULL DEFAULT 'm3',  -- m3|user
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_storyboard_project ON storyboards(project_id);

CREATE TABLE IF NOT EXISTS shots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    storyboard_id INTEGER NOT NULL,
    shot_key TEXT NOT NULL,                 -- s1..，跨版本稳定
    order_no INTEGER NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'feature',
    duration INTEGER NOT NULL DEFAULT 5,
    mode TEXT NOT NULL DEFAULT 'first_frame',   -- first_frame|first_last|reference|t2v
    images_json TEXT NOT NULL DEFAULT '{}',
    visual TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    camera TEXT NOT NULL DEFAULT '',
    audio_json TEXT NOT NULL DEFAULT '{}',
    subtitle TEXT NOT NULL DEFAULT '',
    selling_point_ids_json TEXT NOT NULL DEFAULT '[]',
    prompt_override TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shots_storyboard ON shots(storyboard_id, order_no);

-- 某个分镜版本里每个镜头当前选用的 take（确认复用 / 自动重试跟随都写这里）
CREATE TABLE IF NOT EXISTS shot_takes (
    storyboard_id INTEGER NOT NULL,
    shot_key TEXT NOT NULL,
    take_id INTEGER NOT NULL,
    origin TEXT NOT NULL DEFAULT 'new',     -- new（新生成）| reused（沿用上一版本）
    fingerprint TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (storyboard_id, shot_key)
);
CREATE INDEX IF NOT EXISTS idx_shot_takes_take ON shot_takes(take_id);

-- 合成成片：一个项目可以合成多次（换转场/字幕/BGM），每次一条
CREATE TABLE IF NOT EXISTS renders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    storyboard_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',      -- queued|rendering|done|failed
    options_json TEXT NOT NULL DEFAULT '{}',
    plan_json TEXT,                              -- 每镜用的 take、时长、字幕
    expected_duration REAL,
    output_path TEXT,
    probe_json TEXT,
    warnings_json TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_renders_project ON renders(project_id);
"""

# 给老库补列，幂等（列已存在时 ALTER 会报错，忽略即可）。
_MIGRATIONS = (
    "ALTER TABLE takes ADD COLUMN project_id INTEGER",
    "ALTER TABLE takes ADD COLUMN storyboard_id INTEGER",
    "ALTER TABLE events ADD COLUMN project_id INTEGER",
    "ALTER TABLE takes ADD COLUMN qc_json TEXT",
    "ALTER TABLE takes ADD COLUMN source TEXT NOT NULL DEFAULT 'api'",
    "ALTER TABLE takes ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE takes ADD COLUMN provider_name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE takes ADD COLUMN provider_origin TEXT NOT NULL DEFAULT ''",
)


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        ensure_dirs()
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # 列已存在
        incomplete = conn.execute(
            "SELECT * FROM takes WHERE idempotency_key='' OR idempotency_key IS NULL "
            "OR provider_name='' OR provider_origin=''").fetchall()
        for row in incomplete:
            never_submitted = (row['status'] == C.PENDING and not row['task_id']
                               and row['submit_started_at'] is None)
            if never_submitted and not row['idempotency_key']:
                conn.execute("UPDATE takes SET idempotency_key=? WHERE id=?", (uuid.uuid4().hex, row['id']))
            elif not never_submitted and row['status'] in C.ACTIVE_STATES:
                conn.execute("UPDATE takes SET status=?,error_json=? WHERE id=?", (
                    C.REVIEW, json.dumps({'kind': 'legacy_identity',
                                          'message': '旧任务缺少原始提交身份或服务来源，停止自动恢复；请人工核对'},
                                         ensure_ascii=False), row['id']))
        old_index = conn.execute("SELECT sql FROM sqlite_master WHERE name='idx_takes_idempotency'").fetchone()
        if old_index and ' WHERE ' not in old_index['sql'].upper():
            conn.execute("DROP INDEX idx_takes_idempotency")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_takes_idempotency ON takes(idempotency_key) WHERE idempotency_key!=''")
        conn.commit()
        _conn = conn
    return _conn


def now() -> float:
    return time.time()


def _commit() -> None:
    if _transaction_depth == 0:
        connect().commit()


@contextmanager
def transaction():
    """Serialize one atomic workflow, including writes made through DB helpers.

    BEGIN IMMEDIATE protects against a second connection/process; the reentrant
    lock also prevents this shared connection being used by another request.
    No network await belongs inside this synchronous transaction.
    """
    global _transaction_depth
    with _lock:
        conn = connect()
        depth = _transaction_depth
        savepoint = f"workflow_{depth}"
        conn.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT {savepoint}")
        _transaction_depth += 1
        try:
            yield conn
        except BaseException:
            if depth == 0:
                conn.rollback()
            else:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            try:
                if depth == 0:
                    conn.commit()
                else:
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                if depth == 0:
                    conn.rollback()
                else:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
        finally:
            _transaction_depth -= 1


def _one(sql: str, args: tuple = ()) -> sqlite3.Row | None:
    """读一行。读也要加锁：同一个连接被多个线程共用（请求线程、执行器、质检/合成工作线程），
    不串行化时并发读写会互相干扰，偶发读不到刚写入的数据。"""
    with _lock:
        return connect().execute(sql, args).fetchone()


def _all(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return list(connect().execute(sql, args).fetchall())


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False) if value is not None else None


# ---------- takes ----------

def create_take(*, model: str, request_json: dict, title: str = "",
                shot_key: str = "s1", attempt_no: int = 1,
                parent_take_id: int | None = None,
                fingerprint: str = "",
                project_id: int | None = None,
                storyboard_id: int | None = None,
                source: str = "api", provider_name: str = "", provider_origin: str = "") -> int:
    with _lock:
        cur = connect().execute(
            """INSERT INTO takes(project_id, storyboard_id, shot_key, attempt_no, parent_take_id,
                                title, model, request_json, fingerprint, status, source, created_at,
                                idempotency_key, provider_name, provider_origin)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (project_id, storyboard_id, shot_key, attempt_no, parent_take_id, title, model,
             _dumps(request_json), fingerprint, C.PENDING, source, now(), uuid.uuid4().hex,
             provider_name, provider_origin),
        )
        _commit()
        return int(cur.lastrowid)


def bind_take_source(take_id: int, provider_name: str, provider_origin: str):
    """Bind only a provably unsubmitted pending record; never guess prior origin."""
    with _lock:
        connect().execute("UPDATE takes SET provider_name=?,provider_origin=? WHERE id=? "
                          "AND provider_name='' AND provider_origin='' AND status=? "
                          "AND (task_id IS NULL OR task_id='') AND submit_started_at IS NULL AND idempotency_key!=''",
                          (provider_name, provider_origin, take_id, C.PENDING))
        _commit()
        return _one("SELECT * FROM takes WHERE id=?", (take_id,))


def has_active_takes() -> bool:
    states = sorted(C.ACTIVE_STATES | {C.SUBMITTING})
    return _one("SELECT id FROM takes WHERE status IN (" + ','.join('?' for _ in states) + ") LIMIT 1", tuple(states)) is not None


def get_take(take_id: int) -> sqlite3.Row | None:
    return _one("SELECT * FROM takes WHERE id=?", (take_id,))


def list_takes(limit: int = 100, project_id: int | None = None) -> list[sqlite3.Row]:
    if project_id is None:
        return _all(
            "SELECT * FROM takes ORDER BY id DESC LIMIT ?", (limit,))
    return _all(
        "SELECT * FROM takes WHERE project_id=? ORDER BY id DESC LIMIT ?",
        (project_id, limit))


def list_active_takes() -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in C.ACTIVE_STATES)
    return _all(
        f"SELECT * FROM takes WHERE status IN ({placeholders}) ORDER BY id",
        tuple(C.ACTIVE_STATES))


def list_pending_or_due() -> list[sqlite3.Row]:
    return _all(
        """SELECT * FROM takes
           WHERE status=? OR (status=? AND (next_run_at IS NULL OR next_run_at<=?))
           ORDER BY id""",
        (C.PENDING, C.BACKOFF, now()))


def bound_task_ids() -> set[str]:
    rows = _all(
        "SELECT task_id FROM takes WHERE task_id IS NOT NULL AND task_id!=''")
    return {r["task_id"] for r in rows}


def update_take(take_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols, vals = [], []
    for key, value in fields.items():
        cols.append(f"{key}=?")
        if key.endswith("_json") and value is not None and not isinstance(value, str):
            value = _dumps(value)
        vals.append(value)
    vals.append(take_id)
    with _lock:
        connect().execute(f"UPDATE takes SET {', '.join(cols)} WHERE id=?", vals)
        _commit()


def update_take_if(take_id: int, *, expected_status: str,
                   expected_task_id: str | None = None, **fields: Any) -> bool:
    """Compare expected state and mutate while holding the same SQLite lock."""
    with transaction():
        row = get_take(take_id)
        if (row is None or row['status'] != expected_status
                or expected_task_id is not None and row['task_id'] != expected_task_id):
            return False
        update_take(take_id, **fields)
        return True


def claim_submitting(take_id: int) -> bool:
    with _lock:
        cur = connect().execute(
            "UPDATE takes SET status=?, submit_started_at=? "
            "WHERE id=? AND status IN (?,?)",
            (C.SUBMITTING, now(), take_id, C.PENDING, C.BACKOFF))
        _commit()
        return cur.rowcount == 1


def claim_checking(take_id: int) -> bool:
    """downloaded -> checking 原子占位，避免同一条 take 被并发质检两次。"""
    with _lock:
        cur = connect().execute(
            "UPDATE takes SET status=?, next_run_at=NULL WHERE id=? AND status=?",
            (C.CHECKING, take_id, C.DOWNLOADED))
        _commit()
        return cur.rowcount == 1


def used_output_seconds(project_id: int | None = None) -> int:
    """已产生的平台输出秒数：平台返回成功（有 platform_duration）就算花了钱，
    不管后来是被采纳、质检不通过还是转人工。"""
    sql = ("SELECT COALESCE(SUM(platform_duration),0) AS s FROM takes "
           "WHERE platform_duration IS NOT NULL")
    args: list[Any] = []
    if project_id is not None:
        sql += " AND project_id=?"
        args.append(project_id)
    row = _one(sql, tuple(args))
    return int(row["s"] or 0)


def list_takes_for_check(limit: int = 10) -> list[sqlite3.Row]:
    """已落盘、等待质检且到点的 take（M3 看片失败后会设 next_run_at 退避）。"""
    return _all(
        """SELECT * FROM takes WHERE status=? AND (next_run_at IS NULL OR next_run_at<=?)
           ORDER BY id LIMIT ?""", (C.DOWNLOADED, now(), limit))


def child_takes(take_id: int) -> list[sqlite3.Row]:
    return _all(
        "SELECT * FROM takes WHERE parent_take_id=? ORDER BY id", (take_id,))


def take_lineage_ids(take_id: int) -> set[int]:
    """同一镜头同一请求的重试家族：先沿 parent 找到根，再收集根的所有后代。"""
    row = get_take(take_id)
    if row is None:
        return set()
    seen: set[int] = set()
    root = row
    while root["parent_take_id"] is not None and root["id"] not in seen:
        seen.add(root["id"])
        parent = get_take(int(root["parent_take_id"]))
        if parent is None:
            break
        root = parent
    family: set[int] = set()
    stack = [int(root["id"])]
    while stack:
        cur = stack.pop()
        if cur in family:
            continue
        family.add(cur)
        stack.extend(int(c["id"]) for c in child_takes(cur))
    return family


# ---------- events ----------

def add_event(take_id: int | None, type_: str, message: str,
              level: str = "info", payload: Any = None,
              project_id: int | None = None) -> int:
    with _lock:
        cur = connect().execute(
            """INSERT INTO events(take_id, project_id, level, type, message, payload_json, ts)
               VALUES (?,?,?,?,?,?,?)""",
            (take_id, project_id, level, type_, message,
             _dumps(payload), now()))
        _commit()
        return int(cur.lastrowid)


def list_events(take_id: int | None = None, limit: int = 200,
                project_id: int | None = None) -> list[sqlite3.Row]:
    if take_id is not None:
        return _all(
            "SELECT * FROM events WHERE take_id=? ORDER BY id DESC LIMIT ?",
            (take_id, limit))
    if project_id is not None:
        return _all(
            "SELECT * FROM events WHERE project_id=? ORDER BY id DESC LIMIT ?",
            (project_id, limit))
    return _all(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))


# ---------- settings ----------

def get_setting(key: str, default: Any = None) -> Any:
    row = _one("SELECT value_json FROM settings WHERE key=?", (key,))
    return default if row is None else json.loads(row["value_json"])


def set_setting(key: str, value: Any) -> None:
    with _lock:
        connect().execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
            "updated_at=excluded.updated_at",
            (key, _dumps(value), now()))
        _commit()


def all_settings() -> dict[str, Any]:
    return {r["key"]: json.loads(r["value_json"])
            for r in _all("SELECT key, value_json FROM settings")}


# ---------- projects ----------

def create_project(name: str, ratio: str, target_seconds: int, style: str,
                   selling_points: list[str], banned_words: list[str],
                   h3_model: str = "MiniMax-H3",
                   draft_resolution: str = "768P") -> int:
    with _lock:
        cur = connect().execute(
            """INSERT INTO projects(name, status, ratio, target_seconds, style,
                                    selling_points_json, banned_words_json,
                                    h3_model, draft_resolution, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (name, "draft", ratio, target_seconds, style,
             _dumps(selling_points), _dumps(banned_words),
             h3_model, draft_resolution, now(), now()))
        _commit()
        return int(cur.lastrowid)


def get_project(project_id: int) -> sqlite3.Row | None:
    return _one("SELECT * FROM projects WHERE id=?", (project_id,))


def list_projects(limit: int = 100) -> list[sqlite3.Row]:
    return _all(
        "SELECT * FROM projects ORDER BY id DESC LIMIT ?", (limit,))


def update_project(project_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols, vals = [], []
    for key, value in fields.items():
        cols.append(f"{key}=?")
        if key.endswith("_json") and value is not None and not isinstance(value, str):
            value = _dumps(value)
        vals.append(value)
    vals.extend([now(), project_id])
    with _lock:
        connect().execute(
            f"UPDATE projects SET {', '.join(cols)}, updated_at=? WHERE id=?", vals)
        _commit()


# ---------- assets ----------

def add_asset(*, project_id: int, asset_id: str, path: str, kind: str = "original",
              variant: str = "", parent_asset_id: str | None = None,
              sha256: str = "", width: int | None = None, height: int | None = None,
              mime: str = "", size_bytes: int = 0) -> None:
    with _lock:
        connect().execute(
            """INSERT INTO project_assets(project_id, asset_id, kind, variant, parent_asset_id,
                                          path, sha256, width, height, mime, bytes, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (project_id, asset_id, kind, variant, parent_asset_id, path, sha256,
             width, height, mime, size_bytes, now()))
        _commit()


def list_assets(project_id: int) -> list[sqlite3.Row]:
    return _all(
        "SELECT * FROM project_assets WHERE project_id=? ORDER BY id", (project_id,))


def get_asset_by_id(project_id: int, asset_id: str) -> sqlite3.Row | None:
    return _one(
        "SELECT * FROM project_assets WHERE project_id=? AND asset_id=?",
        (project_id, asset_id))


# ---------- storyboards / shots ----------

def create_storyboard(project_id: int, product_card: dict, version: int,
                      status: str = "draft", created_by: str = "m3",
                      qc_report: dict | None = None,
                      review: dict | None = None) -> int:
    with _lock:
        cur = connect().execute(
            """INSERT INTO storyboards(project_id, version, status, product_card_json,
                                      qc_report_json, review_json, created_by, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (project_id, version, status, _dumps(product_card),
             _dumps(qc_report), _dumps(review), created_by, now()))
        _commit()
        return int(cur.lastrowid)


def get_storyboard(storyboard_id: int) -> sqlite3.Row | None:
    return _one(
        "SELECT * FROM storyboards WHERE id=?", (storyboard_id,))


def list_storyboards(project_id: int) -> list[sqlite3.Row]:
    return _all(
        "SELECT * FROM storyboards WHERE project_id=? ORDER BY version DESC",
        (project_id,))


def update_storyboard(storyboard_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols, vals = [], []
    for key, value in fields.items():
        cols.append(f"{key}=?")
        if key.endswith("_json") and value is not None and not isinstance(value, str):
            value = _dumps(value)
        vals.append(value)
    vals.append(storyboard_id)
    with _lock:
        connect().execute(f"UPDATE storyboards SET {', '.join(cols)} WHERE id=?", vals)
        _commit()


def add_shot(storyboard_id: int, *, shot_key: str, order_no: int, shot: dict) -> int:
    with _lock:
        cur = connect().execute(
            """INSERT INTO shots(storyboard_id, shot_key, order_no, purpose, duration, mode,
                                 images_json, visual, action, camera, audio_json, subtitle,
                                 selling_point_ids_json, prompt_override, title, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (storyboard_id, shot_key, order_no,
             shot.get("purpose", "feature"), int(shot.get("duration", 5)),
             shot.get("mode", "first_frame"),
             _dumps(shot.get("images", {})), shot.get("visual", ""),
             shot.get("action", ""), shot.get("camera", ""),
             _dumps(shot.get("audio", {})), shot.get("subtitle", ""),
             _dumps(shot.get("selling_point_ids", [])),
             shot.get("prompt_override", ""), shot.get("title", ""), now()))
        _commit()
        return int(cur.lastrowid)


def list_shots(storyboard_id: int) -> list[sqlite3.Row]:
    return _all(
        "SELECT * FROM shots WHERE storyboard_id=? ORDER BY order_no",
        (storyboard_id,))


def update_shot(shot_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols, vals = [], []
    for key, value in fields.items():
        cols.append(f"{key}=?")
        if key.endswith("_json") and value is not None and not isinstance(value, str):
            value = _dumps(value)
        vals.append(value)
    vals.append(shot_id)
    with _lock:
        connect().execute(f"UPDATE shots SET {', '.join(cols)} WHERE id=?", vals)
        _commit()


def replace_shots(storyboard_id: int, shots: list[dict]) -> None:
    """整份重写：删旧镜头再写入（同一 storyboard 草稿内）。"""
    with _lock:
        connect().execute("DELETE FROM shots WHERE storyboard_id=?", (storyboard_id,))
        for i, shot in enumerate(shots, start=1):
            connect().execute(
                """INSERT INTO shots(storyboard_id, shot_key, order_no, purpose, duration, mode,
                                     images_json, visual, action, camera, audio_json, subtitle,
                                     selling_point_ids_json, prompt_override, title, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (storyboard_id, shot.get("shot_key") or f"s{i}", i,
                 shot.get("purpose", "feature"), int(shot.get("duration", 5)),
                 shot.get("mode", "first_frame"),
                 _dumps(shot.get("images", {})), shot.get("visual", ""),
                 shot.get("action", ""), shot.get("camera", ""),
                 _dumps(shot.get("audio", {})), shot.get("subtitle", ""),
                 _dumps(shot.get("selling_point_ids", [])),
                 shot.get("prompt_override", ""), shot.get("title", ""), now()))
        _commit()


# ---------- shot_takes（镜头当前选用的 take）----------

def set_shot_take(storyboard_id: int, shot_key: str, take_id: int, *,
                  origin: str = "new", fingerprint: str = "") -> None:
    with _lock:
        connect().execute(
            """INSERT INTO shot_takes(storyboard_id, shot_key, take_id, origin, fingerprint, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(storyboard_id, shot_key) DO UPDATE SET
                 take_id=excluded.take_id, origin=excluded.origin,
                 fingerprint=excluded.fingerprint, updated_at=excluded.updated_at""",
            (storyboard_id, shot_key, take_id, origin, fingerprint, now()))
        _commit()


def get_shot_take(storyboard_id: int, shot_key: str) -> sqlite3.Row | None:
    return _one(
        "SELECT * FROM shot_takes WHERE storyboard_id=? AND shot_key=?",
        (storyboard_id, shot_key))


def list_shot_takes(storyboard_id: int) -> list[sqlite3.Row]:
    return _all(
        "SELECT * FROM shot_takes WHERE storyboard_id=?", (storyboard_id,))


def delete_shot_takes(storyboard_id: int) -> None:
    with _lock:
        connect().execute("DELETE FROM shot_takes WHERE storyboard_id=?", (storyboard_id,))
        _commit()


def find_accepted_take(*, project_id: int, shot_key: str, fingerprint: str) -> sqlite3.Row | None:
    """在同一项目、同一镜头里，找指纹一致且已采纳的 take（用于改稿复用）。"""
    return _one(
        """SELECT * FROM takes
           WHERE project_id=? AND shot_key=? AND fingerprint=? AND status='accepted'
           ORDER BY id DESC LIMIT 1""",
        (project_id, shot_key, fingerprint))


def retarget_selection(from_take_ids: set[int] | list[int], to_take_id: int, *,
                       keep_accepted: bool = True) -> list[tuple[int, str]]:
    """把指向 from_take_ids 的镜头选用改指到 to_take_id（跨分镜版本）。

    keep_accepted=True 时，当前已采纳的选用不动（自动重试不抢人工/已通过的结果）；
    人工「采纳/选用」时传 False 强制改。返回被改动的 (storyboard_id, shot_key)。
    """
    ids = [int(i) for i in from_take_ids if int(i) != int(to_take_id)]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = _all(
        f"SELECT * FROM shot_takes WHERE take_id IN ({placeholders})", tuple(ids))
    changed: list[tuple[int, str]] = []
    for r in rows:
        if keep_accepted:
            cur = get_take(int(r["take_id"]))
            if cur is not None and cur["status"] == C.ACCEPTED:
                continue
        set_shot_take(int(r["storyboard_id"]), r["shot_key"], int(to_take_id),
                      origin=r["origin"], fingerprint=r["fingerprint"])
        changed.append((int(r["storyboard_id"]), r["shot_key"]))
    return changed


# ---------- renders（合成成片）----------

def create_render(*, project_id: int, storyboard_id: int, options: dict,
                  plan: dict, expected_duration: float) -> int:
    with _lock:
        cur = connect().execute(
            """INSERT INTO renders(project_id, storyboard_id, status, options_json, plan_json,
                                   expected_duration, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (project_id, storyboard_id, "queued", _dumps(options), _dumps(plan),
             expected_duration, now()))
        _commit()
        return int(cur.lastrowid)


def get_render(render_id: int) -> sqlite3.Row | None:
    return _one("SELECT * FROM renders WHERE id=?", (render_id,))


def list_renders(project_id: int | None = None, status: str | None = None,
                 limit: int = 50) -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM renders WHERE 1=1", []
    if project_id is not None:
        sql += " AND project_id=?"
        args.append(project_id)
    if status is not None:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return _all(sql, tuple(args))


def update_render(render_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols, vals = [], []
    for key, value in fields.items():
        cols.append(f"{key}=?")
        if key.endswith("_json") and value is not None and not isinstance(value, str):
            value = _dumps(value)
        vals.append(value)
    vals.append(render_id)
    with _lock:
        connect().execute(f"UPDATE renders SET {', '.join(cols)} WHERE id=?", vals)
        _commit()


def claim_render(render_id: int) -> bool:
    with _lock:
        cur = connect().execute(
            "UPDATE renders SET status='rendering', started_at=?, error=NULL "
            "WHERE id=? AND status='queued'", (now(), render_id))
        _commit()
        return cur.rowcount == 1
