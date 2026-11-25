"""排放批争用锁。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])


def record_contention(
    conn, lock_key: str, holder: str, detail: str | None = None
) -> None:
    """在给定连接里写一条“争用失败”痕（是否持久化由调用方事务决定）。"""
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用失败", detail, now_iso()),
    )


def durable_contention(lock_key: str, holder: str, detail: str | None = None) -> None:
    """独立连接落争用痕并立即提交，保证败方被拒收/回退后争用痕仍在。"""
    with connect() as trace_conn:
        record_contention(trace_conn, lock_key, holder, detail)
        trace_conn.commit()


def check_existing_lock(conn, lock_key: str, holder: str) -> None:
    """只读检查既有锁是否被他者持有；命中则持久争用痕并 409。本调用不写锁行。"""
    row = conn.execute(
        "SELECT holder FROM effluent_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row and row["holder"] != holder:
        record_contention(
            conn, lock_key, holder, f"持有人={row['holder']}（只读闸检查）"
        )
        conn.commit()
        raise HTTPException(409, f"锁被占用：{lock_key} 持有人 {row['holder']}")


def require_lock(conn, lock_key: str, holder: str) -> None:
    # INSERT OR IGNORE：并发同抢时靠主键仲裁，避免 SELECT-then-INSERT 撞
    # UNIQUE 抛 500；仲裁后再读持有人判定重入/争用。
    conn.execute(
        "INSERT OR IGNORE INTO effluent_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
        (lock_key, holder, now_iso()),
    )
    row = conn.execute(
        "SELECT holder FROM effluent_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row and row["holder"] != holder:
        detail = f"持有人={row['holder']}"
        record_contention(conn, lock_key, holder, detail)
        # 抢锁失败要对外抛 409：先把争用痕独立持久化，避免随请求事务回滚丢失。
        conn.commit()
        raise HTTPException(409, f"锁被占用：{lock_key} 持有人 {row['holder']}")


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM effluent_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        locks = [dict(r) for r in conn.execute("SELECT * FROM effluent_locks").fetchall()]
        logs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM contention_log ORDER BY id DESC LIMIT 50"
            ).fetchall()
        ]
        return {"locks": locks, "contention": logs}


@router.post("/api/locks/acquire")
def acquire(lock_key: str, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        require_lock(conn, lock_key, user)
        write_audit(conn, user, "lock_acquire", lock_key, {})
        conn.commit()
        return {"ok": True, "lock_key": lock_key, "holder": user}


@router.post("/api/locks/release")
def release_api(lock_key: str, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        release(conn, lock_key, user)
        write_audit(conn, user, "lock_release", lock_key, {})
        conn.commit()
        return {"ok": True}
