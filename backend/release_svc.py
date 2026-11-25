"""准排放裁定放行。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from adjudication_svc import ledger_release_blocker, limits_for_sample
from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, require_lock
from models import ReleaseIn
from validators import adjudicate_exceed, validate_sample_releasable

router = APIRouter(tags=["release"])


@router.get("/api/releases")
def list_releases(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.*, s.sample_code, o.outfall_code FROM discharge_releases r
               JOIN sample_batches s ON s.id=r.sample_id
               JOIN outfalls o ON o.id=r.outfall_id
               ORDER BY r.id DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/releases")
def create_release(body: ReleaseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"sample:{body.sample_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            sample = conn.execute(
                "SELECT * FROM sample_batches WHERE id=?", (body.sample_id,)
            ).fetchone()
            if not sample:
                raise HTTPException(404, "采样批次不存在")
            validate_sample_releasable(sample["status"])
            open_hold = conn.execute(
                "SELECT id FROM exceed_holds WHERE sample_id=? AND status='开放'",
                (body.sample_id,),
            ).fetchone()
            if open_hold:
                raise HTTPException(400, "仍有开放超标挂起，禁止准排")
            # 闸一：正式指标列必须对照排放口限值重算合格，拒绝“闭了挂起但指标仍旧超标”的假绿。
            metrics = json.loads(sample["metrics"] or "{}")
            exceeded, metric, measured, limit = adjudicate_exceed(
                metrics, limits_for_sample(conn, sample)
            )
            if exceeded:
                raise HTTPException(
                    400,
                    f"准排页拒绝旧超标指标：{metric}={measured:g} 仍超限值 {limit:g}，"
                    "须先闭环同重裁合格（甲）或把已升正影子裁定写入正式列（乙）",
                )
            # 闸二：有裁定台账时，最新一条必须是合格生效正式裁定；未升正影子不得充数。
            ledger_block = ledger_release_blocker(conn, body.sample_id)
            if ledger_block:
                raise HTTPException(400, ledger_block)
            existing = conn.execute(
                "SELECT id FROM discharge_releases WHERE sample_id=? AND status='已准排'",
                (body.sample_id,),
            ).fetchone()
            if existing:
                raise HTTPException(400, "已准排，勿重复")
            ref = f"RL-{sample['sample_code']}"
            conn.execute(
                """INSERT INTO discharge_releases(sample_id,outfall_id,release_ref,status,officer,released_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    body.sample_id,
                    sample["outfall_id"],
                    ref,
                    "已准排",
                    body.officer,
                    now_iso(),
                ),
            )
            conn.execute(
                "UPDATE sample_batches SET status=? WHERE id=?",
                ("已准排", body.sample_id),
            )
            write_audit(
                conn,
                user,
                "release",
                sample["sample_code"],
                {"release_ref": ref, "officer": body.officer},
            )
            conn.commit()
            return {"release_ref": ref, "status": "已准排"}
        finally:
            release(conn, lock_key, user)
            conn.commit()
