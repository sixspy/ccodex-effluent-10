"""超标旁路挂起开闭 + 闭环同重裁（effluent-10）。"""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException

from adjudication_svc import (
    LANE_FORMAL,
    STATUS_FORMAL_ACTIVE,
    STATUS_SHADOW_PENDING,
    LaneDeviation,
    adjudicate_metrics,
    extract_recalc_metrics,
    record_adjudication,
    reject_deviation,
    resolve_lane,
)
from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import check_existing_lock, record_contention, release, require_lock
from models import ExceedCloseIn, ExceedOpenIn
from validators import validate_metrics_nonneg

router = APIRouter(tags=["exceed"])


@router.get("/api/exceeds")
def list_exceeds(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        q = """SELECT h.*, s.sample_code, s.status AS sample_status,
                      s.metrics AS sample_metrics_raw,
                      s.adjudicated_at AS sample_adjudicated_at,
                      o.outfall_code,
                      (SELECT ma.adjudicated_at FROM metric_adjudications ma
                        WHERE ma.hold_id=h.id ORDER BY ma.id DESC LIMIT 1) AS recalc_at,
                      (SELECT ma.adjudication_no FROM metric_adjudications ma
                        WHERE ma.hold_id=h.id ORDER BY ma.id DESC LIMIT 1) AS adjudication_no,
                      (SELECT ma.lane FROM metric_adjudications ma
                        WHERE ma.hold_id=h.id ORDER BY ma.id DESC LIMIT 1) AS recalc_lane
               FROM exceed_holds h
               JOIN sample_batches s ON s.id=h.sample_id
               JOIN outfalls o ON o.id=s.outfall_id"""
        if status:
            rows = conn.execute(
                q + " WHERE h.status=? ORDER BY h.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(q + " ORDER BY h.id DESC").fetchall()
        items = []
        for r in rows:
            d = dict(r)
            try:
                d["current_metrics"] = json.loads(d.pop("sample_metrics_raw") or "{}")
            except (ValueError, TypeError):
                d["current_metrics"] = {}
                d.pop("sample_metrics_raw", None)
            items.append(d)
        return {"items": items}


@router.get("/api/exceeds/{hold_id}")
def get_exceed(hold_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT h.*, s.sample_code, s.status AS sample_status,
                      s.adjudicated_at AS sample_adjudicated_at, o.outfall_code
               FROM exceed_holds h
               JOIN sample_batches s ON s.id=h.sample_id
               JOIN outfalls o ON o.id=s.outfall_id
               WHERE h.id=?""",
            (hold_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "挂起不存在")
        item = dict(row)
        adjs = conn.execute(
            "SELECT * FROM metric_adjudications WHERE hold_id=? ORDER BY id ASC",
            (hold_id,),
        ).fetchall()
        item["adjudications"] = [dict(a) for a in adjs]
        return item


@router.post("/api/exceeds/open")
def open_exceed(body: ExceedOpenIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"sample:{body.sample_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            sample = conn.execute(
                """SELECT s.*, o.cod_limit, o.nh3n_limit, o.tp_limit
                   FROM sample_batches s JOIN outfalls o ON o.id=s.outfall_id
                   WHERE s.id=?""",
                (body.sample_id,),
            ).fetchone()
            if not sample:
                raise HTTPException(404, "采样批次不存在")
            if sample["status"] == "作废":
                raise HTTPException(400, "已作废不可挂起")
            open_row = conn.execute(
                "SELECT id FROM exceed_holds WHERE sample_id=? AND status='开放'",
                (body.sample_id,),
            ).fetchone()
            if open_row:
                raise HTTPException(400, "已有开放挂起")

            metrics = json.loads(sample["metrics"] or "{}")
            from validators import adjudicate_exceed

            exceeded, metric, measured, limit = adjudicate_exceed(
                metrics,
                {
                    "cod": sample["cod_limit"],
                    "nh3n": sample["nh3n_limit"],
                    "tp": sample["tp_limit"],
                },
            )
            if not exceeded:
                raise HTTPException(400, "当前指标未超标，无需挂起")
            hold_key = f"EX-{sample['sample_code']}-{metric}-M"
            conn.execute(
                """INSERT INTO exceed_holds(sample_id,hold_key,exceed_metric,measured,
                   limit_value,status,opened_at,note) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    body.sample_id,
                    hold_key,
                    metric,
                    measured,
                    limit,
                    "开放",
                    now_iso(),
                    body.note,
                ),
            )
            conn.execute(
                "UPDATE sample_batches SET status=? WHERE id=?",
                ("旁路挂起", body.sample_id),
            )
            write_audit(
                conn,
                user,
                "exceed_open",
                sample["sample_code"],
                {"hold_key": hold_key, "metric": metric},
            )
            conn.commit()
            return {"hold_key": hold_key, "metric": metric}
        finally:
            release(conn, lock_key, user)
            conn.commit()


def _contention_and_clear(conn, lock_key: str, holder: str, detail: str) -> None:
    """落争用痕并提交，随后清掉本请求 holder 的锁行再提交，避免泄漏一次性锁。"""
    record_contention(conn, lock_key, holder, detail)
    conn.commit()
    release(conn, lock_key, holder)
    conn.commit()


@router.post("/api/exceeds/close")
def close_exceed(body: ExceedCloseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        hold = conn.execute(
            "SELECT * FROM exceed_holds WHERE id=?", (body.hold_id,)
        ).fetchone()
        if not hold:
            raise HTTPException(404, "挂起不存在")

        hold_lock = f"exceed_hold:{body.hold_id}"
        # 锁外先拦重复闭环：后到者也要留下持久争用痕（独立提交，不被回滚带走）。
        if hold["status"] != "开放":
            record_contention(
                conn,
                hold_lock,
                user,
                f"hold_key={hold['hold_key']} 后到闭环，挂起状态={hold['status']}",
            )
            conn.commit()
            raise HTTPException(
                409, f"挂起已关闭，闭环争用失败：{hold['hold_key']}"
            )

        sample_id = hold["sample_id"]
        sample_lock = f"sample:{sample_id}"

        # 口径解析与偏航审计在抢锁写事务之前完成（reject_deviation 自行提交审计）。
        try:
            lane, kind, path_code = resolve_lane(body)
        except LaneDeviation as exc:
            raise reject_deviation(conn, user, hold["hold_key"], exc) from exc

        inline_metrics = extract_recalc_metrics(body)
        explicit_metrics = inline_metrics is not None
        if explicit_metrics:
            validate_metrics_nonneg(inline_metrics)

        # 同一会话用户也用一次性 holder，保证“两路会话/双击”在锁表层即区分。
        holder = f"{user}#{uuid.uuid4().hex[:10]}"
        # 兼容旧 sample 锁：若被准排/封批等流程显式持有，闭环只读闸直接 409。
        check_existing_lock(conn, sample_lock, holder)
        # 挂起锁是 EX-J60 并发闭环的权威互斥（配合下方原子认领）。
        require_lock(conn, hold_lock, holder)

        # 锁内复检挂起状态（另一路可能已提交闭环）。
        fresh_hold = conn.execute(
            "SELECT status FROM exceed_holds WHERE id=?", (body.hold_id,)
        ).fetchone()
        if not fresh_hold or fresh_hold["status"] != "开放":
            _contention_and_clear(
                conn, hold_lock, holder,
                f"hold_key={hold['hold_key']} 锁内复检：挂起已被他路关闭",
            )
            raise HTTPException(409, "闭环争用失败：该挂起已被另一会话关闭")

        sample = conn.execute(
            """SELECT s.*, o.cod_limit, o.nh3n_limit, o.tp_limit
               FROM sample_batches s JOIN outfalls o ON o.id=s.outfall_id
               WHERE s.id=?""",
            (sample_id,),
        ).fetchone()
        if not sample:
            raise HTTPException(404, "采样批次不存在")

        # 重裁值优先用闭环体内嵌复测值；否则在锁内读最新正式指标列。
        metrics = inline_metrics
        if metrics is None:
            metrics = json.loads(sample["metrics"] or "{}")
        exceeded, metric, measured, limit = adjudicate_metrics(conn, sample, metrics)

        # 甲路径：若显式提交了复测指标且重裁仍超标，说明“声称纠正却仍超标”，
        # 整笔回退拒收（尚未认领挂起/写裁定，抛错由 with-connect 回滚一次性锁）。
        # 未带复测值时按在库正式指标如实重裁：允许闭环并落“超标”正式裁定+新时戳，
        # 假绿由准排闸/对账一致性拦截（验收口径：准排页拒绝旧超标指标）。
        if lane == LANE_FORMAL and exceeded and explicit_metrics:
            raise HTTPException(
                400,
                f"闭环重裁拒收：{metric} 复测 {measured} 仍超限值 {limit}，"
                "不得据此写合格新裁定，整笔回退，挂起保持开放",
            )

        ts = now_iso()
        # 原子认领是最终“挂起锁”：WHERE status='开放' 保证并发两路至多一路命中。
        claim_note = body.note or hold["note"] or ""
        cur = conn.execute(
            """UPDATE exceed_holds SET status='已关闭', closed_at=?, note=?
               WHERE id=? AND status='开放'""",
            (ts, claim_note, body.hold_id),
        )
        if cur.rowcount != 1:
            _contention_and_clear(
                conn, hold_lock, holder,
                f"hold_key={hold['hold_key']} 原子认领失败，他路已闭环",
            )
            raise HTTPException(409, "闭环争用失败：该挂起已被另一会话关闭")

        verdict = "超标" if exceeded else "合格"
        if lane == LANE_FORMAL:
            adj_id, adj_ts, adj_no = record_adjudication(
                conn,
                sample_id=sample_id,
                hold_id=body.hold_id,
                lane=lane,
                kind=kind,
                path_code=path_code,
                verdict=verdict,
                metrics=metrics,
                status=STATUS_FORMAL_ACTIVE,
                source="闭环同裁(甲)",
                actor=user,
                note=body.note or "闭环同事务重裁",
                official=1,
                at=ts,
            )
            # 新裁定与闭环同事务落库；显式复测值才改写正式指标列，
            # 未带复测值时正式指标原样保留（裁定如实记录超标/合格）。
            if explicit_metrics:
                conn.execute(
                    """UPDATE sample_batches
                       SET metrics=?, status='已裁定', adjudicated_at=?, recalc_at=?
                       WHERE id=?""",
                    (json.dumps(metrics, ensure_ascii=False), ts, ts, sample_id),
                )
            else:
                conn.execute(
                    """UPDATE sample_batches
                       SET status='已裁定', adjudicated_at=?, recalc_at=?
                       WHERE id=?""",
                    (ts, ts, sample_id),
                )
            sample_adjudicated = True
        else:
            adj_id, adj_ts, adj_no = record_adjudication(
                conn,
                sample_id=sample_id,
                hold_id=body.hold_id,
                lane=lane,
                kind=kind,
                path_code=path_code,
                verdict=verdict,
                metrics=metrics,
                status=STATUS_SHADOW_PENDING,
                source="闭环影子裁(乙)",
                actor=user,
                note=body.note or "仅落影子裁定，正式指标零改",
                official=0,
                at=ts,
            )
            # 乙路径：批态可显示“已裁定”，但正式 metrics/adjudicated_at 零改，
            # 准排闸只认正式生效裁定，影子未升正一律拒绝。
            conn.execute(
                "UPDATE sample_batches SET status='已裁定' WHERE id=?",
                (sample_id,),
            )
            sample_adjudicated = False

        shift_tag_note = f"【闭环重裁·{path_code}】{verdict} {adj_no} {ts} 当班锁死"
        final_note = f"{shift_tag_note}；{body.note or hold['note'] or ''}".strip("；")
        conn.execute(
            """UPDATE exceed_holds
               SET closed_at=?, note=?, recalc_at=?, adjudication_id=?, recalc_lane=?
               WHERE id=?""",
            (adj_ts, final_note, adj_ts, adj_id, lane, body.hold_id),
        )

        write_audit(
            conn,
            user,
            "exceed_close",
            sample["sample_code"],
            {
                "hold_id": body.hold_id,
                "hold_key": hold["hold_key"],
                "lane": lane,
                "path": path_code,
                "verdict": verdict,
                "adjudication_id": adj_id,
                "adjudication_no": adj_no,
                "adjudicated_at": adj_ts,
                "sample_adjudicated": sample_adjudicated,
                "note": body.note,
            },
        )
        # 成功：在同一事务里释放一次性锁并提交，挂起/指标/裁定/审计原子落库。
        release(conn, sample_lock, holder)
        release(conn, hold_lock, holder)
        conn.commit()
        return {
            "ok": True,
            "hold_id": body.hold_id,
            "hold_key": hold["hold_key"],
            "hold_status": "已关闭",
            "lane": lane,
            "kind": kind,
            "path": path_code,
            "verdict": verdict,
            "recalculated": True,
            "new_adjudication": True,
            "adjudication_id": adj_id,
            "adjudication_no": adj_no,
            "adjudicated_at": adj_ts,
            "recalc_at": adj_ts,
            "ticket_adjudicated": True,
            "sample_adjudicated": sample_adjudicated,
            "shadow_pending": lane != LANE_FORMAL,
        }


@router.post("/api/exceeds/{hold_id}/close")
def close_exceed_by_path(
    hold_id: int,
    payload: dict | None = None,
    authorization: AuthHeader = None,
):
    """路径 id 别名：部分验收口径 POST /api/exceeds/{id}/close。"""
    data = dict(payload or {})
    data["hold_id"] = hold_id
    return close_exceed(ExceedCloseIn(**data), authorization)


@router.post("/api/exceeds/close/{hold_id}")
def close_exceed_by_suffix(
    hold_id: int,
    payload: dict | None = None,
    authorization: AuthHeader = None,
):
    data = dict(payload or {})
    data["hold_id"] = hold_id
    return close_exceed(ExceedCloseIn(**data), authorization)
