"""日界锁定口径与放行服务日界逻辑。

唯一口径来源：docs/日界锁定说明.md
钉死口径：甲方案——业务日切换在同一事务内作废昨日全部生效锁定，次日必须重新钉策略。
乙方案（显式结转）未启用：任何结转/回拨等偏离说明的动作一律拒绝，并登记「日界口径偏差」。

本模块只使用本机文件库 data/app.db，不引入任何外置存储。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "app.db"

# 钉死口径：甲。代码中不存在切换口径的写路径。
PLAN = "甲"
PLAN_DESC = "业务日切换同一事务内作废昨日锁定，次日必须重新钉策略"
BOUNDARY_LOCK_TIMEOUT = 3.0
_UNSET = object()

BOUNDARY_RULES = [
    "策略锁定仅在 biz_date 等于当前业务日且状态为生效时参与放行拦截",
    "日切单事务：推进业务日 → 作废全部生效锁定 → 写日切流水 → 写审计行",
    "次日如需锁定必须重新钉策略，禁止跨日静默沿用昨日锁定拒放",
    "乙方案显式结转未启用，结转请求拒绝并登记日界口径偏差",
    "日切与策略钉死互斥，后手动作登记日界争用",
    "放行服务发现跨日残留锁不拦截，当场作废清理并写审计",
    "业务日只能向前切换，回拨/混子日请求拒绝并登记日界口径偏差",
    "业务日状态只允许日切事务更新，禁止无审计改日界",
]

DDL = """
CREATE TABLE IF NOT EXISTS biz_day_state(
  id INTEGER PRIMARY KEY CHECK (id=1),
  current_biz_date TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS boundary_config(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  note TEXT NOT NULL,
  decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_locks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slice_id INTEGER NOT NULL,
  biz_date TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL,
  pinned_by TEXT NOT NULL,
  pinned_at TEXT NOT NULL,
  voided_at TEXT,
  void_reason TEXT
);
CREATE TABLE IF NOT EXISTS day_cuts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_biz_date TEXT NOT NULL,
  to_biz_date TEXT NOT NULL,
  voided_locks INTEGER NOT NULL,
  operator TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS boundary_audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  payload TEXT NOT NULL,
  operator TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


class BoundaryError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status
        self.message = message


class ContentionError(BoundaryError):
    """日切与策略钉死互斥，后手拿不到锁时抛出。"""


# 日切 / 策略钉死 / 放行日界校验共用的进程内互斥量。
_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def audit(conn: sqlite3.Connection, event_type: str, biz_date: str,
          payload: dict[str, Any], operator: str) -> None:
    conn.execute(
        "INSERT INTO boundary_audit(event_type,biz_date,payload,operator,created_at)"
        " VALUES(?,?,?,?,?)",
        (event_type, biz_date, json.dumps(payload, ensure_ascii=False), operator, now_iso()),
    )


def current_biz_date(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT current_biz_date FROM biz_day_state WHERE id=1").fetchone()
    return row["current_biz_date"]


def register_deviation(detail: dict[str, Any], operator: str,
                       conn: sqlite3.Connection | None = None) -> None:
    """偏差登记须立即落库。

    已有事务连接时复用并当场 commit（随后抛出的拒绝异常只回滚空白事务）；
    无连接时开独立事务，避免与外层 BEGIN IMMEDIATE 争用 SQLite 写锁。
    """
    if conn is not None:
        today = current_biz_date(conn)
        audit(conn, "日界口径偏差", today, detail, operator)
        conn.commit()
        return
    with connect() as own:
        today = current_biz_date(own)
        audit(own, "日界口径偏差", today, detail, operator)
        own.commit()


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    if not conn.execute("SELECT 1 FROM biz_day_state WHERE id=1").fetchone():
        row = conn.execute("SELECT MAX(biz_date) AS m FROM slices").fetchone()
        seed = row["m"] if row and row["m"] else date.today().isoformat()
        conn.execute(
            "INSERT INTO biz_day_state(id,current_biz_date,updated_at) VALUES(1,?,?)",
            (seed, now_iso()),
        )
    if not conn.execute("SELECT 1 FROM boundary_config WHERE key='plan'").fetchone():
        conn.execute(
            "INSERT INTO boundary_config(key,value,note,decided_at) VALUES(?,?,?,?)",
            ("plan", PLAN, PLAN_DESC, now_iso()),
        )


@contextmanager
def boundary_tx(operator: str, op_name: str, timeout: Any = _UNSET):
    """日界动作互斥区：进程锁 + BEGIN IMMEDIATE 单事务。

    后手超时即登记「日界争用」并抛 ContentionError；timeout=None 表示一直等待（放行用）。
    """
    wait = BOUNDARY_LOCK_TIMEOUT if timeout is _UNSET else timeout
    acquired = _lock.acquire(timeout=wait) if wait is not None else _lock.acquire()
    if not acquired:
        # 排队拿到锁后只写争用审计，不做任何业务变更
        _lock.acquire()
        try:
            with connect() as conn:
                today = current_biz_date(conn)
                audit(conn, "日界争用", today,
                      {"loser": op_name, "operator": operator,
                       "reason": "日切与策略钉死互斥，后手未获锁"}, operator)
                conn.commit()
        finally:
            _lock.release()
        raise ContentionError(f"日界争用：{op_name}与另一日界动作互斥，后手已登记日界争用")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
        _lock.release()


def pin_lock(conn: sqlite3.Connection, slice_id: int, reason: str, operator: str) -> int:
    today = current_biz_date(conn)
    sl = conn.execute("SELECT * FROM slices WHERE id=?", (slice_id,)).fetchone()
    if not sl:
        raise BoundaryError("切片不存在", 404)
    dup = conn.execute(
        "SELECT id FROM policy_locks WHERE slice_id=? AND biz_date=? AND status='生效'",
        (slice_id, today),
    ).fetchone()
    if dup:
        raise BoundaryError("该切片当前业务日已有生效锁定，请勿重复钉策略")
    cur = conn.execute(
        "INSERT INTO policy_locks(slice_id,biz_date,reason,status,pinned_by,pinned_at)"
        " VALUES(?,?,?,'生效',?,?)",
        (slice_id, today, reason, operator, now_iso()),
    )
    lock_id = cur.lastrowid
    audit(conn, "策略钉死", today,
          {"lock_id": lock_id, "slice_id": slice_id, "slice_no": sl["slice_no"],
           "reason": reason, "plan": PLAN}, operator)
    return lock_id


def void_lock(conn: sqlite3.Connection, lock_id: int, reason: str, operator: str) -> None:
    lk = conn.execute("SELECT * FROM policy_locks WHERE id=?", (lock_id,)).fetchone()
    if not lk:
        raise BoundaryError("锁定不存在", 404)
    if lk["status"] != "生效":
        raise BoundaryError("锁定已作废，不能重复作废")
    conn.execute(
        "UPDATE policy_locks SET status='作废',voided_at=?,void_reason=? WHERE id=?",
        (now_iso(), f"手工作废：{reason or '未填原因'}", lock_id),
    )
    audit(conn, "锁定作废", lk["biz_date"],
          {"lock_id": lock_id, "reason": reason, "mode": "手工作废"}, operator)


def day_cut(conn: sqlite3.Connection, to_biz_date: str | None, operator: str) -> dict[str, str]:
    cur = current_biz_date(conn)
    if to_biz_date:
        try:
            target = date.fromisoformat(to_biz_date).isoformat()
        except ValueError:
            register_deviation({"attempt": "日切目标日格式非法", "from": cur, "to": to_biz_date},
                               operator, conn=conn)
            raise BoundaryError("业务日格式应为 YYYY-MM-DD，异常请求已登记日界口径偏差")
    else:
        target = (date.fromisoformat(cur) + timedelta(days=1)).isoformat()
    if target <= cur:
        register_deviation({"attempt": "日切回拨/混子日", "from": cur, "to": target},
                           operator, conn=conn)
        raise BoundaryError("日界只能向前切换，回拨请求已拒绝并登记日界口径偏差")

    conn.execute("UPDATE biz_day_state SET current_biz_date=?,updated_at=? WHERE id=1",
                 (target, now_iso()))
    res = conn.execute(
        "UPDATE policy_locks SET status='作废',voided_at=?,void_reason=?"
        " WHERE status='生效'",
        (now_iso(), "业务日切换作废（甲方案）"),
    )
    voided = res.rowcount
    conn.execute(
        "INSERT INTO day_cuts(from_biz_date,to_biz_date,voided_locks,operator,created_at,note)"
        " VALUES(?,?,?,?,?,?)",
        (cur, target, voided, operator, now_iso(), "甲方案：同事务作废昨日锁定，次日重新钉策略"),
    )
    audit(conn, "日切", target,
          {"from": cur, "to": target, "voided_locks": voided, "plan": PLAN}, operator)
    audit(conn, "锁定作废", target,
          {"count": voided, "reason": "业务日切换作废（甲方案）"}, operator)
    return {"from": cur, "to": target, "voided_locks": voided}


def request_carryover(lock_id: int, operator: str) -> None:
    """乙方案动作：甲口径下永远拒绝并登记偏差（单连接立即提交）。"""
    with connect() as conn:
        lk = conn.execute("SELECT * FROM policy_locks WHERE id=?", (lock_id,)).fetchone()
        today = current_biz_date(conn)
        audit(conn, "日界口径偏差", today,
              {"attempt": "显式结转（乙方案未启用）", "lock_id": lock_id,
               "lock_biz_date": lk["biz_date"] if lk else None, "active_plan": PLAN},
              operator)
        conn.commit()
    raise BoundaryError("当前钉死口径为甲方案，显式结转未启用；请求已拒绝并登记日界口径偏差")


def release_gate(conn: sqlite3.Connection, slice_row: sqlite3.Row) -> tuple[sqlite3.Row | None, list[int]]:
    """放行日界校验。

    跨日残留锁（biz_date 不等于当前业务日却仍生效）当场作废清理并审计，绝不参与拦截；
    仅同日生效锁可拒绝放行。
    """
    today = current_biz_date(conn)
    residue = conn.execute(
        "SELECT * FROM policy_locks WHERE slice_id=? AND status='生效' AND biz_date<>?",
        (slice_row["id"], today),
    ).fetchall()
    cleaned: list[int] = []
    for lk in residue:
        conn.execute(
            "UPDATE policy_locks SET status='作废',voided_at=?,void_reason=? WHERE id=?",
            (now_iso(), "跨日锁定残留清理（放行服务日界逻辑）", lk["id"]),
        )
        audit(conn, "跨日残留清理", today,
              {"lock_id": lk["id"], "lock_biz_date": lk["biz_date"],
               "slice_id": slice_row["id"], "slice_no": slice_row["slice_no"]},
              "release-service")
        cleaned.append(lk["id"])
    active = conn.execute(
        "SELECT * FROM policy_locks WHERE slice_id=? AND status='生效' AND biz_date=?"
        " ORDER BY id DESC LIMIT 1",
        (slice_row["id"], today),
    ).fetchone()
    return active, cleaned


def annotate(conn: sqlite3.Connection, slice_row: sqlite3.Row, today: str) -> tuple[str, dict[str, Any] | None]:
    """目录旁注与详情旁注的唯一同源构造函数。"""
    active = conn.execute(
        "SELECT * FROM policy_locks WHERE slice_id=? AND status='生效' AND biz_date=?"
        " ORDER BY id DESC LIMIT 1",
        (slice_row["id"], today),
    ).fetchone()
    if active:
        note = (f"业务日{today}｜策略锁定中（锁#{active['id']}：{active['reason']}，"
                f"{active['pinned_at']}钉）")
        return note, dict(active)
    return f"业务日{today}｜无同日生效锁定，按完整度规则放行", None
