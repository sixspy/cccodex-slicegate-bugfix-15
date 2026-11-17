
import hashlib, secrets, sqlite3
from datetime import datetime
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

from day_boundary import (
    BOUNDARY_RULES,
    PLAN,
    PLAN_DESC,
    BoundaryError,
    annotate,
    audit,
    boundary_tx,
    connect,
    current_biz_date,
    day_cut,
    ensure_tables,
    pin_lock,
    release_gate,
    request_carryover,
    void_lock,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "日切放行"
SKELETON = "业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS slices(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_no TEXT UNIQUE NOT NULL,
          biz_date TEXT NOT NULL,
          partition_key TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          expected_count INTEGER NOT NULL,
          completeness TEXT NOT NULL,
          release_status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS releases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER NOT NULL,
          consumer TEXT NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watermarks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          consumer TEXT NOT NULL,
          biz_date TEXT NOT NULL,
          watermark TEXT NOT NULL,
          source_slice TEXT NOT NULL
        );
        """)
        if conn.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"] == 0:
            rows = [
                ("SL-2026-11-01-east", "2026-11-01", "east", 980, 1000, "部分完整", "未放行"),
                ("SL-2026-11-01-west", "2026-11-01", "west", 1000, 1000, "完整", "已放行"),
                ("SL-2026-11-02-east", "2026-11-02", "east", 0, 1000, "待检", "未放行"),
            ]
            for r in rows:
                conn.execute("INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)", r)
            conn.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (2,'ads_order','成功','完整切片放行','2026-11-01T18:00:00')")
            conn.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES ('ads_order','2026-11-01','2026-11-01T18:00:00','SL-2026-11-01-west')")
        conn.commit()
    # 日界表与口径配置独立事务建立，避免与上面的种子事务混在一起
    with connect() as conn:
        ensure_tables(conn)
        conn.commit()

@app.on_event("startup")
def _start():
    init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "slices": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username, role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/slices")
def list_slices(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM slices ORDER BY id").fetchall()
        today = current_biz_date(c)
        items = []
        for r in rows:
            d = dict(r)
            note, lock = annotate(c, r, today)
            d["boundary_note"] = note                      # 目录旁注
            d["boundary_locked"] = lock is not None
            d["current_biz_date"] = today
            items.append(d)
    return {"items": items, "current_biz_date": today}

class SliceIn(BaseModel):
    slice_no: str
    biz_date: str
    partition_key: str
    row_count: int
    expected_count: int

@app.post("/api/slices")
def add_slice(body: SliceIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    comp = "完整" if body.row_count >= body.expected_count and body.expected_count > 0 else ("部分完整" if body.row_count > 0 else "待检")
    with connect() as c:
        try:
            cur = c.execute(
                "INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)",
                (body.slice_no, body.biz_date, body.partition_key, body.row_count, body.expected_count, comp, "未放行"),
            )
            c.commit()
            return {"id": cur.lastrowid, "completeness": comp}
        except sqlite3.IntegrityError:
            raise HTTPException(409, "切片号已存在")

@app.post("/api/slices/{sid}/evaluate")
def evaluate(sid: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        comp = "完整" if row["row_count"] >= row["expected_count"] and row["expected_count"] > 0 else ("部分完整" if row["row_count"] > 0 else "待检")
        c.execute("UPDATE slices SET completeness=? WHERE id=?", (comp, sid))
        c.commit()
    return {"id": sid, "completeness": comp}

# ---------------- 日界锁定：策略台 / 关日台 / 审计 ----------------

class LockIn(BaseModel):
    reason: str = "策略锁定"

class VoidIn(BaseModel):
    reason: str = "人工解锁"

class CarryIn(BaseModel):
    reason: str = ""

class DayCutIn(BaseModel):
    to_biz_date: str | None = None

@app.get("/api/boundary/spec")
def boundary_spec(authorization: str | None = Header(default=None)):
    require_user(authorization)
    return {"plan": PLAN, "plan_desc": PLAN_DESC, "rules": BOUNDARY_RULES,
            "spec_doc": "docs/日界锁定说明.md"}

@app.get("/api/boundary/state")
def boundary_state(authorization: str | None = Header(default=None)):
    """策略台与关日台共用的同源读数。"""
    require_user(authorization)
    with connect() as c:
        today = current_biz_date(c)
        locks = [dict(r) for r in c.execute(
            "SELECT * FROM policy_locks ORDER BY id DESC").fetchall()]
        active = [l for l in locks if l["status"] == "生效"]
        residue = [l for l in active if l["biz_date"] != today]
        cuts = [dict(r) for r in c.execute(
            "SELECT * FROM day_cuts ORDER BY id DESC").fetchall()]
        deviation = c.execute(
            "SELECT COUNT(*) c FROM boundary_audit WHERE event_type='日界口径偏差'").fetchone()["c"]
        contention = c.execute(
            "SELECT COUNT(*) c FROM boundary_audit WHERE event_type='日界争用'").fetchone()["c"]
    return {"current_biz_date": today, "plan": PLAN, "plan_desc": PLAN_DESC,
            "locks": locks, "active_count": len(active), "residue_count": len(residue),
            "day_cuts": cuts, "deviation_count": deviation, "contention_count": contention}

@app.post("/api/slices/{sid}/lock")
def pin_slice_lock(sid: int, body: LockIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    try:
        with boundary_tx(user, "策略钉死") as c:
            lock_id = pin_lock(c, sid, body.reason, user)
            today = current_biz_date(c)
        return {"ok": True, "lock_id": lock_id, "biz_date": today, "plan": PLAN}
    except BoundaryError as e:
        raise HTTPException(e.status, e.message)

@app.post("/api/boundary/locks/{lock_id}/void")
def void_slice_lock(lock_id: int, body: VoidIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    try:
        with boundary_tx(user, "锁定作废") as c:
            void_lock(c, lock_id, body.reason, user)
        return {"ok": True}
    except BoundaryError as e:
        raise HTTPException(e.status, e.message)

@app.post("/api/boundary/carryover/{lock_id}")
def carryover(lock_id: int, body: CarryIn, authorization: str | None = Header(default=None)):
    """乙方案入口：甲口径钉死后永远拒绝并登记偏差。"""
    user = require_user(authorization)
    try:
        request_carryover(lock_id, user)
    except BoundaryError as e:
        raise HTTPException(e.status, e.message)

@app.post("/api/boundary/day-cut")
def cut_day(body: DayCutIn, authorization: str | None = Header(default=None)):
    """关日台日切：与策略钉死互斥；同事务作废昨日锁定。"""
    user = require_user(authorization)
    try:
        with boundary_tx(user, "日切") as c:
            result = day_cut(c, body.to_biz_date, user)
        return {"ok": True, **result, "plan": PLAN}
    except BoundaryError as e:
        raise HTTPException(e.status, e.message)

@app.get("/api/boundary/audit")
def boundary_audit_list(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM boundary_audit ORDER BY id DESC LIMIT 200").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/api/slices/{sid}/boundary")
def slice_boundary(sid: int, authorization: str | None = Header(default=None)):
    """详情旁注：与目录旁注同一口径、同一当前业务日。"""
    require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        today = current_biz_date(c)
        note, lock = annotate(c, row, today)
        recent = [dict(r) for r in c.execute(
            "SELECT * FROM boundary_audit WHERE biz_date=? ORDER BY id DESC LIMIT 10",
            (today,)).fetchall()]
    return {"slice_id": sid, "current_biz_date": today, "boundary_note": note,
            "active_lock": lock, "recent_audit": recent, "plan": PLAN}

# ---------------- 放行（日界逻辑落点） ----------------

class ReleaseIn(BaseModel):
    consumer: str

@app.post("/api/slices/{sid}/release")
def release(sid: int, body: ReleaseIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    ts = datetime.now().isoformat(timespec="seconds")
    # 放行全程持日界互斥（阻塞等待，不参与争用超时），保证与日切/钉策略串行
    with boundary_tx(user, "放行日界校验", timeout=None) as c:
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        if row["completeness"] != "完整":
            c.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                      (sid, body.consumer, "拒绝", "完整度未达标", ts))
            audit(c, "放行拒绝", current_biz_date(c),
                  {"slice_id": sid, "slice_no": row["slice_no"], "reason": "完整度未达标"}, user)
            c.commit()
            raise HTTPException(409, "未完整禁止放行")
        active, cleaned = release_gate(c, row)
        if active:
            note = (f"当前业务日策略锁定拦截（锁#{active['id']}：{active['reason']}），"
                    f"非跨日残留锁，拒绝放行")
            c.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                      (sid, body.consumer, "拒绝", note, ts))
            audit(c, "放行拒绝", current_biz_date(c),
                  {"slice_id": sid, "slice_no": row["slice_no"], "reason": "同日策略锁定",
                   "lock_id": active["id"]}, user)
            c.commit()
            raise HTTPException(409, note)
        c.execute("UPDATE slices SET release_status='已放行' WHERE id=?", (sid,))
        rel_note = "完整切片放行"
        if cleaned:
            rel_note = f"完整切片放行；已清理跨日锁定残留{len(cleaned)}笔（{','.join(map(str, cleaned))}）"
        c.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                  (sid, body.consumer, "成功", rel_note, ts))
        c.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES (?,?,?,?)",
                  (body.consumer, row["biz_date"], ts, row["slice_no"]))
        audit(c, "放行成功", current_biz_date(c),
              {"slice_id": sid, "slice_no": row["slice_no"], "consumer": body.consumer,
               "cleaned_residue_locks": cleaned}, user)
        return {"ok": True, "cleaned_residue_locks": cleaned, "note": rel_note}

@app.get("/api/releases")
def list_releases(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT r.*, s.slice_no FROM releases r JOIN slices s ON s.id=r.slice_id ORDER BY r.id DESC""").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/api/watermarks")
def list_wm(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM watermarks ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")

app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
