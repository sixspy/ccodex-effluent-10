"""采样批次入库与超标裁定。"""
from __future__ import annotations

import json

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
from locks import release, require_lock
from models import RetestIn, SampleIn
from validators import (
    adjudicate_exceed,
    metric_summary,
    validate_flow_cap,
    validate_metrics_nonneg,
    validate_outfall_open,
    validate_plant_running,
)

router = APIRouter(tags=["samples"])


def _next_sample_code(conn, outfall_id: int) -> str:
    outfall = conn.execute(
        "SELECT outfall_code FROM outfalls WHERE id=?", (outfall_id,)
    ).fetchone()
    prefix = outfall["outfall_code"] if outfall else f"OF-{outfall_id}"
    n = conn.execute(
        "SELECT COUNT(*) c FROM sample_batches WHERE outfall_id=?", (outfall_id,)
    ).fetchone()["c"]
    return f"SM-{prefix}-{n + 1:03d}"


@router.get("/api/samples")
def list_samples(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        q = """SELECT s.*, o.outfall_code, p.plant_code FROM sample_batches s
               JOIN outfalls o ON o.id=s.outfall_id
               JOIN plants p ON p.id=o.plant_id"""
        if status:
            rows = conn.execute(
                q + " WHERE s.status=? ORDER BY s.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(q + " ORDER BY s.id DESC").fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["metrics"] = json.loads(d["metrics"] or "{}")
            latest = conn.execute(
                """SELECT id, adjudication_no, lane, kind, verdict, status,
                          adjudicated_at, promoted
                   FROM metric_adjudications WHERE sample_id=?
                   ORDER BY id DESC LIMIT 1""",
                (r["id"],),
            ).fetchone()
            if latest:
                lad = dict(latest)
                d["latest_adjudication"] = lad
                # 采样页“指标/裁定时戳”列的多命名别名，五处同源。
                d["recalc_at"] = lad["adjudicated_at"]
                d["new_adjudication_timestamp"] = lad["adjudicated_at"]
                d["adjudication_verdict"] = lad["verdict"]
                d["adjudication_lane"] = lad["lane"]
            else:
                d["latest_adjudication"] = None
                d["recalc_at"] = d.get("adjudicated_at")
            items.append(d)
        return {"items": items}


@router.get("/api/samples/{sample_id}")
def get_sample(sample_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT s.*, o.outfall_code, o.cod_limit, o.nh3n_limit, o.tp_limit,
                      p.name plant_name
               FROM sample_batches s
               JOIN outfalls o ON o.id=s.outfall_id
               JOIN plants p ON p.id=o.plant_id
               WHERE s.id=?""",
            (sample_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "采样批次不存在")
        data = dict(row)
        data["metrics"] = json.loads(data["metrics"] or "{}")
        holds = conn.execute(
            "SELECT * FROM exceed_holds WHERE sample_id=? ORDER BY id DESC",
            (sample_id,),
        ).fetchall()
        seals = conn.execute(
            "SELECT * FROM discharge_seals WHERE sample_id=? ORDER BY id DESC",
            (sample_id,),
        ).fetchall()
        releases = conn.execute(
            "SELECT * FROM discharge_releases WHERE sample_id=? ORDER BY id DESC",
            (sample_id,),
        ).fetchall()
        adjudications = conn.execute(
            "SELECT * FROM metric_adjudications WHERE sample_id=? ORDER BY id ASC",
            (sample_id,),
        ).fetchall()
        adj_items = []
        for a in adjudications:
            ad = dict(a)
            try:
                ad["metrics"] = json.loads(ad.get("metrics") or "{}")
            except (ValueError, TypeError):
                ad["metrics"] = {}
            ad["promoted"] = bool(ad.get("promoted"))
            ad["official"] = bool(ad.get("official"))
            adj_items.append(ad)
        return {
            "sample": data,
            "exceed_holds": [dict(h) for h in holds],
            "discharge_seals": [dict(s) for s in seals],
            "discharge_releases": [dict(r) for r in releases],
            "adjudications": adj_items,
            "metric_adjudications": adj_items,
        }


@router.post("/api/samples")
def create_sample(body: SampleIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"outfall:{body.outfall_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            outfall = conn.execute(
                """SELECT o.*, p.status plant_status, p.design_cap_m3d
                   FROM outfalls o JOIN plants p ON p.id=o.plant_id WHERE o.id=?""",
                (body.outfall_id,),
            ).fetchone()
            if not outfall:
                raise HTTPException(404, "排放口不存在")
            validate_outfall_open(outfall["status"])
            validate_plant_running(outfall["plant_status"])
            metrics = body.metrics.model_dump()
            validate_metrics_nonneg(metrics)
            validate_flow_cap(body.flow_m3h, float(outfall["design_cap_m3d"]))
            permit = conn.execute(
                "SELECT * FROM discharge_permits WHERE permit_code=?",
                (body.permit_ref,),
            ).fetchone()
            if not permit:
                raise HTTPException(400, "排放许可不存在")
            if permit["status"] != "有效":
                raise HTTPException(400, "排放许可未生效")
            exceeded, metric, measured, limit = adjudicate_exceed(
                metrics,
                {
                    "cod": outfall["cod_limit"],
                    "nh3n": outfall["nh3n_limit"],
                    "tp": outfall["tp_limit"],
                },
            )
            code = body.sample_code or _next_sample_code(conn, body.outfall_id)
            if conn.execute(
                "SELECT id FROM sample_batches WHERE sample_code=?", (code,)
            ).fetchone():
                raise HTTPException(400, "采样编号已存在")
            status = "旁路挂起" if exceeded else "已裁定"
            adjudicated = None if exceeded else now_iso()
            cur = conn.execute(
                """INSERT INTO sample_batches(sample_code,outfall_id,permit_ref,metrics,
                   flow_m3h,generation,status,adjudicated_at,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    code,
                    body.outfall_id,
                    body.permit_ref,
                    json.dumps(metrics, ensure_ascii=False),
                    body.flow_m3h,
                    1,
                    status,
                    adjudicated,
                    now_iso(),
                ),
            )
            sid = cur.lastrowid
            hold = None
            if exceeded:
                hold_key = f"EX-{code}-{metric}"
                conn.execute(
                    """INSERT INTO exceed_holds(sample_id,hold_key,exceed_metric,measured,
                       limit_value,status,opened_at,note)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        sid,
                        hold_key,
                        metric,
                        measured,
                        limit,
                        "开放",
                        now_iso(),
                        "入库自动挂起",
                    ),
                )
                hold = hold_key
            write_audit(
                conn,
                user,
                "sample_create",
                code,
                {
                    "status": status,
                    "metrics": metrics,
                    "summary": metric_summary(metrics),
                    "exceed": exceeded,
                },
            )
            conn.commit()
            return {
                "id": sid,
                "sample_code": code,
                "status": status,
                "exceed": exceeded,
                "hold_key": hold,
            }
        finally:
            release(conn, lock_key, user)
            conn.commit()


def _apply_retest(sample_id: int, body: RetestIn, user: str):
    """复测/重裁入口：甲路径直接改写正式指标列并落生效裁定；乙路径只落影子。"""
    lock_key = f"sample:{sample_id}"
    with connect() as conn:
        sample = conn.execute(
            """SELECT s.*, o.cod_limit, o.nh3n_limit, o.tp_limit
               FROM sample_batches s JOIN outfalls o ON o.id=s.outfall_id
               WHERE s.id=?""",
            (sample_id,),
        ).fetchone()
        if not sample:
            raise HTTPException(404, "采样批次不存在")
        try:
            lane, kind, path_code = resolve_lane(body)
        except LaneDeviation as exc:
            raise reject_deviation(conn, user, sample["sample_code"], exc) from exc

        metrics = extract_recalc_metrics(body)
        if metrics is None:
            raise HTTPException(422, "复测重裁须携带 cod/nh3n/tp 指标")
        validate_metrics_nonneg(metrics)
        exceeded, metric, measured, limit = adjudicate_metrics(conn, sample, metrics)

        require_lock(conn, lock_key, user)
        try:
            ts = now_iso()
            if lane == LANE_FORMAL:
                if exceeded:
                    raise HTTPException(
                        400,
                        f"复测仍超标：{metric}={measured:g} 超限值 {limit:g}，拒收且正式指标零改",
                    )
                adj_id, adj_ts, adj_no = record_adjudication(
                    conn,
                    sample_id=sample_id,
                    hold_id=None,
                    lane=lane,
                    kind=kind,
                    path_code=path_code,
                    verdict="合格",
                    metrics=metrics,
                    status=STATUS_FORMAL_ACTIVE,
                    source="采样复测重裁",
                    actor=user,
                    note=body.note or "采样服务复测重裁",
                    official=1,
                    at=ts,
                )
                conn.execute(
                    """UPDATE sample_batches SET metrics=?, status='已裁定',
                       adjudicated_at=?, recalc_at=? WHERE id=?""",
                    (json.dumps(metrics, ensure_ascii=False), ts, ts, sample_id),
                )
            else:
                verdict = "超标" if exceeded else "合格"
                adj_id, adj_ts, adj_no = record_adjudication(
                    conn,
                    sample_id=sample_id,
                    hold_id=None,
                    lane=lane,
                    kind=kind,
                    path_code=path_code,
                    verdict=verdict,
                    metrics=metrics,
                    status=STATUS_SHADOW_PENDING,
                    source="采样复测影子裁",
                    actor=user,
                    note=body.note or "复测仅落影子裁定",
                    official=0,
                    at=ts,
                )
            write_audit(
                conn,
                user,
                "sample_retest",
                sample["sample_code"],
                {
                    "sample_id": sample_id,
                    "lane": lane,
                    "adjudication_id": adj_id,
                    "adjudicated_at": adj_ts,
                    "metrics": metrics,
                },
            )
            conn.commit()
            return {
                "ok": True,
                "sample_id": sample_id,
                "lane": lane,
                "adjudication_id": adj_id,
                "adjudication_no": adj_no,
                "adjudicated_at": adj_ts,
                "recalc_at": adj_ts,
            }
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/samples/{sample_id}/retest")
def retest_sample(sample_id: int, body: RetestIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    return _apply_retest(sample_id, body, user)


@router.post("/api/samples/{sample_id}/recalc")
def recalc_sample(sample_id: int, body: RetestIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    return _apply_retest(sample_id, body, user)


@router.patch("/api/samples/{sample_id}")
def patch_sample(sample_id: int, body: RetestIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    return _apply_retest(sample_id, body, user)


@router.put("/api/samples/{sample_id}")
def put_sample(sample_id: int, body: RetestIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    return _apply_retest(sample_id, body, user)


@router.post("/api/samples/{sample_id}/adjudicate")
def adjudicate_sample(sample_id: int, body: RetestIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    return _apply_retest(sample_id, body, user)


@router.put("/api/samples/{sample_id}/metrics")
def put_sample_metrics(sample_id: int, body: RetestIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    return _apply_retest(sample_id, body, user)


@router.patch("/api/samples/{sample_id}/metrics")
def patch_sample_metrics(sample_id: int, body: RetestIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    return _apply_retest(sample_id, body, user)
