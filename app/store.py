import os,sqlite3,json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from app.errors import DomainError
def _default(value):
    if isinstance(value,datetime): return value.isoformat()
    if isinstance(value,Decimal): return str(value)
    raise TypeError(f"不可序列化类型 {type(value)!r}")
def connection():
    path=Path(os.environ.get("DATA_DIR","data"));path.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path/"runs.sqlite",timeout=15)
    db.row_factory=sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,review TEXT NOT NULL DEFAULT '待复核')")
    # 补传修订所需的批次原料与留痕，原批次 payload 始终不被覆盖
    db.execute("CREATE TABLE IF NOT EXISTS sources (run_id TEXT PRIMARY KEY,raw TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS revision_jobs (id TEXT PRIMARY KEY,base_run_id TEXT NOT NULL,digest TEXT NOT NULL,payload TEXT NOT NULL,updated TEXT NOT NULL)")
    # 替代批次的补传留痕独立存储，避免 INSERT OR IGNORE 丢失 revision 块；随批次内容固定
    db.execute("CREATE TABLE IF NOT EXISTS revisions (run_id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS active_ledger (singleton INTEGER PRIMARY KEY CHECK(singleton=1),run_id TEXT NOT NULL,updated TEXT NOT NULL)")
    return db
def save(result):
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO runs(id,payload) VALUES(?,?)",(result["manifest"]["run_id"],json.dumps(result,ensure_ascii=False,default=_default)))
    return fetch(result["manifest"]["run_id"])
def save_source(run_id,raw):
    if isinstance(raw,bytes): raw=raw.decode("utf-8-sig")
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO sources(run_id,raw) VALUES(?,?)",(run_id,raw))
def source(run_id):
    with connection() as db: row=db.execute("SELECT raw FROM sources WHERE run_id=?",(run_id,)).fetchone()
    if row is None: raise DomainError("NOT_FOUND","批次原始凭证未留存，无法办理补传",404)
    return row["raw"]
def save_revision(run_id,payload):
    with connection() as db:
        db.execute("INSERT INTO revisions(run_id,payload) VALUES(?,?) "
                   "ON CONFLICT(run_id) DO UPDATE SET payload=excluded.payload",
                   (run_id,json.dumps(payload,ensure_ascii=False,default=_default)))
def get_revision(run_id):
    with connection() as db: row=db.execute("SELECT payload FROM revisions WHERE run_id=?",(run_id,)).fetchone()
    return json.loads(row["payload"]) if row else None
def fetch(run_id):
    with connection() as db:
        row=db.execute("SELECT * FROM runs WHERE id=?",(run_id,)).fetchone()
        rev=db.execute("SELECT payload FROM revisions WHERE run_id=?",(run_id,)).fetchone()
    if row is None: raise DomainError("NOT_FOUND","批次不存在",404)
    result=json.loads(row["payload"]);result["version"]=row["version"];result["review"]=row["review"]
    if rev is not None: result["revision"]=json.loads(rev["payload"])
    return result
def history():
    with connection() as db: rows=db.execute("SELECT id,version,review FROM runs ORDER BY rowid DESC LIMIT 100").fetchall()
    return [dict(row) for row in rows]
def review(run_id,version,decision):
    if decision not in ("通过","退回"): raise DomainError("REVIEW","请选择通过或退回")
    if not isinstance(version,int): raise DomainError("STALE","版本必须为整数")
    with connection() as db:
        cur=db.execute("UPDATE runs SET review=?,version=version+1 WHERE id=? AND version=?",(decision,run_id,version))
        if cur.rowcount!=1: raise DomainError("STALE","复核版本已更新",409)
    return fetch(run_id)
# ---- 补传修订任务：检查点随每一步确认进度落盘，中断后据此续跑 ----
def upsert_job(job):
    from datetime import datetime,timezone
    job=dict(job);job["updated"]=datetime.now(timezone.utc).isoformat()
    with connection() as db:
        db.execute("INSERT INTO revision_jobs(id,base_run_id,digest,payload,updated) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated=excluded.updated",
                   (job["id"],job["base_run_id"],job["digest"],json.dumps(job,ensure_ascii=False,default=_default),job["updated"]))
    return job
def get_job(job_id):
    with connection() as db: row=db.execute("SELECT payload FROM revision_jobs WHERE id=?",(job_id,)).fetchone()
    if row is None: raise DomainError("NOT_FOUND","补传任务不存在",404)
    return json.loads(row["payload"])
def find_job(base_run_id,digest):
    with connection() as db:
        row=db.execute("SELECT payload FROM revision_jobs WHERE base_run_id=? AND digest=? ORDER BY rowid LIMIT 1",(base_run_id,digest)).fetchone()
    return json.loads(row["payload"]) if row else None
def list_jobs():
    with connection() as db: rows=db.execute("SELECT payload FROM revision_jobs ORDER BY rowid DESC LIMIT 50").fetchall()
    return [json.loads(r["payload"]) for r in rows]
# ---- 现行日账指针：整表仅一行，切换在单事务内完成，失败保留旧版 ----
def active():
    with connection() as db: row=db.execute("SELECT run_id FROM active_ledger WHERE singleton=1").fetchone()
    return row["run_id"] if row else None
def switch_active(run_id,expected=None):
    from datetime import datetime,timezone
    with connection() as db:
        row=db.execute("SELECT run_id FROM active_ledger WHERE singleton=1").fetchone()
        current=row["run_id"] if row else None
        if expected is not None and expected!=current:
            raise DomainError("STALE","现行日账已被其他复核切换",409)
        db.execute("INSERT INTO active_ledger(singleton,run_id,updated) VALUES(1,?,?) "
                   "ON CONFLICT(singleton) DO UPDATE SET run_id=excluded.run_id,updated=excluded.updated",
                   (run_id,datetime.now(timezone.utc).isoformat()))
        return current
