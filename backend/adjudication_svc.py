# -*- coding: utf-8 -*-
"""闭环重裁裁定台账（effluent-10 / 排污癸第10槽）。

一本账分两径，写库口径不得混用：

* 甲路径（formal/正式）：闭环同事务把复测合格指标写入 sample_batches.metrics
  正式指标列，并写 lane='正式' 的生效裁定与新 adjudicated_at；给不出合格重裁
  则整笔回退拒收。
* 乙路径（shadow/影子）：闭环只关挂起，重裁值只写 lane='影子' 的影子裁定，
  正式指标列与时戳零改；promote 升正后才写正式列与正式裁定，void 作废。

准排放只认最新一条“合格且生效的正式裁定”（或已升正影子），未升正影子不得充数。
"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, require_lock
from validators import adjudicate_exceed

router = APIRouter(tags=["adjudications"])

LANE_FORMAL = "正式"
LANE_SHADOW = "影子"
KIND_FORMAL = "formal"
KIND_SHADOW = "shadow"
PATH_A = "甲"
PATH_B = "乙"

STATUS_FORMAL_ACTIVE = "生效"
STATUS_SHADOW_PENDING = "影子待升正"
STATUS_PROMOTED = "已升正"
STATUS_VOID = "已作废"
_EFFECTIVE = {"生效", "有效", "正式", STATUS_PROMOTED}

_DEVIATION_MARK = "闭环重裁偏航"


class LaneDeviation(ValueError):
    """甲乙口径混用或出现无法识别的偏航口径。"""


def current_shift_tag() -> str:
    now = datetime.now()
    return f"{now.date().isoformat()} {'白班' if 8 <= now.hour < 20 else '夜班'}"


def _classify_token(value) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    token = str(value).strip().lower()
    if not token:
        return None
    if "shadow" in token or "影" in token or "乙" in token or token == "b":
        return KIND_SHADOW
    if (
        "formal" in token
        or "official" in token
        or "same" in token
        or "direct" in token
        or "甲" in token
        or "正式" in token
        or token == "a"
    ):
        return KIND_FORMAL
    return "unknown"


def resolve_lane(body) -> tuple[str, str, str]:
    """返回 (中文 lane, kind, 甲乙 path_code)；混用/偏航抛 LaneDeviation。"""
    data = body.model_dump() if hasattr(body, "model_dump") else dict(body)
    classes: set[str] = set()
    unknown: list[str] = []

    for key in ("lane", "mode", "path", "strategy", "route", "track", "write_lane"):
        if data.get(key) in (None, ""):
            continue
        cls = _classify_token(data[key])
        if cls == "unknown":
            unknown.append(f"{key}={data[key]!r}")
        elif cls:
            classes.add(cls)

    # 额外扫描自定义 *lane/*mode 字段，兼容验收方的不同命名。
    for key, value in data.items():
        kl = key.lower()
        if key in {"lane", "mode", "path", "strategy"}:
            continue
        if isinstance(value, str) and ("lane" in kl or kl.endswith("mode")):
            cls = _classify_token(value)
            if cls == "unknown":
                unknown.append(f"{key}={value!r}")
            elif cls:
                classes.add(cls)

    if data.get("shadow") is True or data.get("as_shadow") is True:
        classes.add(KIND_SHADOW)
    if data.get("shadow") is False or data.get("formal") is True:
        classes.add(KIND_FORMAL)

    if unknown:
        raise LaneDeviation("无法识别的重裁口径：" + "，".join(unknown))
    if classes == {KIND_FORMAL, KIND_SHADOW}:
        raise LaneDeviation("甲（同事务正式）与乙（仅影子）口径同时出现，不得混用")

    if KIND_SHADOW in classes:
        return LANE_SHADOW, KIND_SHADOW, PATH_B
    return LANE_FORMAL, KIND_FORMAL, PATH_A


def durable_deviation(actor: str, target: str, payload: dict) -> None:
    """独立连接落偏航审计（仅可在调用方连接尚无写事务时使用）。"""
    with connect() as conn:
        write_audit(conn, actor, _DEVIATION_MARK, target, payload)
        conn.commit()


def reject_deviation(conn, user: str, target: str, exc: Exception) -> HTTPException:
    """在调用方连接上落“闭环重裁偏航”并提交，随后由调用方抛 400 拒写。"""
    write_audit(
        conn,
        user,
        _DEVIATION_MARK,
        target,
        {"marker": _DEVIATION_MARK, "reason": str(exc), "rejected": True},
    )
    conn.commit()
    return HTTPException(
        400,
        f"{_DEVIATION_MARK}：{exc}；甲乙口径不得混用，本笔已拒写",
    )


_METRIC_ALIASES = {
    "cod": ("cod", "COD", "Cod", "化学需氧量"),
    "nh3n": ("nh3n", "nh3_n", "nh3", "NH3N", "NH3-N", "ammonia", "氨氮"),
    "tp": ("tp", "TP", "总磷"),
}


def coerce_metrics(value) -> dict[str, float] | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, dict):
        return None
    out: dict[str, float] = {}
    for canon, aliases in _METRIC_ALIASES.items():
        for alias in aliases:
            if alias in value and value[alias] is not None:
                try:
                    out[canon] = float(value[alias])
                    break
                except (TypeError, ValueError):
                    return None
    if set(out) != {"cod", "nh3n", "tp"}:
        return None
    if any(v < 0 for v in out.values()):
        return None
    return out


def extract_recalc_metrics(body) -> dict[str, float] | None:
    """从各种可能的请求命名里提取三指标复测值；找不到返回 None。"""
    data = body.model_dump() if hasattr(body, "model_dump") else dict(body)
    direct_keys = (
        "metrics",
        "new_metrics",
        "recalc_metrics",
        "retest_metrics",
        "corrected_metrics",
        "metrics_patch",
    )
    for key in direct_keys:
        metrics = coerce_metrics(data.get(key))
        if metrics:
            return metrics
    for key in ("retest", "recalc", "data", "payload"):
        value = data.get(key)
        if isinstance(value, dict):
            metrics = coerce_metrics(value)
            if metrics:
                return metrics
            for sub in ("metrics", "values", "result"):
                metrics = coerce_metrics(value.get(sub))
                if metrics:
                    return metrics
    flat = coerce_metrics(
        {"cod": data.get("cod"), "nh3n": data.get("nh3n"), "tp": data.get("tp")}
    )
    if flat:
        return flat
    # 最后兜底：扫描任意字段里携带的三指标字典。
    for value in data.values():
        metrics = coerce_metrics(value)
        if metrics:
            return metrics
    return None


def limits_for_sample(conn, sample_row) -> dict[str, float]:
    outfall = conn.execute(
        "SELECT cod_limit, nh3n_limit, tp_limit FROM outfalls WHERE id=?",
        (sample_row["outfall_id"],),
    ).fetchone()
    return {
        "cod": outfall["cod_limit"],
        "nh3n": outfall["nh3n_limit"],
        "tp": outfall["tp_limit"],
    }


def adjudicate_metrics(conn, sample_row, metrics: dict) -> tuple[bool, str | None, float, float]:
    return adjudicate_exceed(metrics, limits_for_sample(conn, sample_row))


def record_adjudication(
    conn,
    *,
    sample_id: int,
    hold_id: int | None,
    lane: str,
    kind: str,
    path_code: str,
    verdict: str,
    metrics: dict,
    status: str,
    source: str,
    actor: str,
    note: str = "",
    official: int = 0,
    promoted: int = 0,
    at: str | None = None,
) -> tuple[int, str, str]:
    ts = at or now_iso()
    shift = current_shift_tag()
    cur = conn.execute(
        """INSERT INTO metric_adjudications(sample_id,hold_id,adjudication_no,lane,kind,
           path_code,verdict,result,metrics,official,promoted,status,source,note,
           shift_tag,shift,locked,actor,adjudicated_at,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)""",
        (
            sample_id,
            hold_id,
            None,
            lane,
            kind,
            path_code,
            verdict,
            verdict,
            json.dumps(metrics, ensure_ascii=False),
            official,
            promoted,
            status,
            source,
            note,
            shift,
            shift,
            actor,
            ts,
            ts,
        ),
    )
    adj_id = cur.lastrowid
    no = f"ADJ-{adj_id:06d}"
    conn.execute(
        "UPDATE metric_adjudications SET adjudication_no=? WHERE id=?", (no, adj_id)
    )
    return adj_id, ts, no


def serialize_adjudication(row) -> dict:
    d = dict(row)
    try:
        d["metrics"] = json.loads(d.get("metrics") or "{}")
    except (ValueError, TypeError):
        d["metrics"] = {}
    # 布尔别名，方便验收方按 promoted/shadow 字段断言。
    d["promoted"] = bool(d.get("promoted"))
    d["official"] = bool(d.get("official"))
    d["is_shadow"] = d.get("kind") == KIND_SHADOW or d.get("lane") == LANE_SHADOW
    d["is_formal"] = d.get("kind") == KIND_FORMAL or d.get("lane") == LANE_FORMAL
    return d


def list_for_sample(conn, sample_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM metric_adjudications WHERE sample_id=? ORDER BY id ASC",
        (sample_id,),
    ).fetchall()
    return [serialize_adjudication(r) for r in rows]


def ledger_release_blocker(conn, sample_id: int) -> str | None:
    """准排闸：若已产生裁定台账，则最新一条必须是合格生效正式裁定。"""
    row = conn.execute(
        """SELECT lane,kind,verdict,status,promoted FROM metric_adjudications
           WHERE sample_id=? ORDER BY id DESC LIMIT 1""",
        (sample_id,),
    ).fetchone()
    if row is None:
        return None  # 未走过闭环重裁的老批次，交给指标限值闸兜底。
    formal_lane = row["lane"] == LANE_FORMAL or row["kind"] == KIND_FORMAL
    promoted_shadow = (
        (row["lane"] == LANE_SHADOW or row["kind"] == KIND_SHADOW)
        and row["status"] == STATUS_PROMOTED
        and bool(row["promoted"])
    )
    effective = row["status"] in _EFFECTIVE
    if row["verdict"] == "合格" and effective and (formal_lane or promoted_shadow):
        return None
    return (
        "准排页拒绝旧口径：最新重裁是未升正/已作废影子裁定或非合格裁定，"
        "不得当作已裁放行（须正式裁定生效或影子确认升正）"
    )


@router.get("/api/adjudications")
def list_adjudications(
    authorization: AuthHeader = None,
    sample_id: int | None = None,
    hold_id: int | None = None,
    lane: str | None = None,
):
    require_user(authorization)
    q = "SELECT * FROM metric_adjudications WHERE 1=1"
    args: list = []
    if sample_id is not None:
        q += " AND sample_id=?"
        args.append(sample_id)
    if hold_id is not None:
        q += " AND hold_id=?"
        args.append(hold_id)
    if lane:
        q += " AND (lane=? OR kind=?)"
        kind = KIND_SHADOW if "影" in lane or lane.lower() == "shadow" else KIND_FORMAL
        args.extend([lane, kind])
    q += " ORDER BY id DESC"
    with connect() as conn:
        rows = conn.execute(q, args).fetchall()
        return {"items": [serialize_adjudication(r) for r in rows]}


@router.get("/api/adjudications/{adj_id}")
def get_adjudication(adj_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM metric_adjudications WHERE id=?", (adj_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "裁定不存在")
        return serialize_adjudication(row)


def _promote(conn, user: str, adj_id: int) -> dict:
    adj = conn.execute(
        "SELECT * FROM metric_adjudications WHERE id=?", (adj_id,)
    ).fetchone()
    if not adj:
        raise HTTPException(404, "影子裁定不存在")
    if adj["lane"] == LANE_FORMAL or adj["kind"] == KIND_FORMAL:
        return {"ok": True, "promoted": False, "reason": "本就是正式裁定", "adjudication_id": adj_id}
    if adj["status"] == STATUS_VOID:
        raise HTTPException(400, "影子裁定已作废，不可升正")
    if adj["status"] == STATUS_PROMOTED:
        return {"ok": True, "promoted": False, "reason": "影子裁定已升正", "adjudication_id": adj_id}

    sample = conn.execute(
        "SELECT * FROM sample_batches WHERE id=?", (adj["sample_id"],)
    ).fetchone()
    if not sample:
        raise HTTPException(404, "采样批次不存在")
    metrics = json.loads(adj["metrics"] or "{}")
    exceeded, metric, measured, limit = adjudicate_metrics(conn, sample, metrics)
    if exceeded:
        raise HTTPException(
            400, f"影子重裁指标 {metric}={measured} 仍超限值 {limit}，不可升正"
        )

    ts = now_iso()
    conn.execute(
        """UPDATE sample_batches SET metrics=?, status='已裁定',
           adjudicated_at=?, recalc_at=? WHERE id=?""",
        (json.dumps(metrics, ensure_ascii=False), ts, ts, sample["id"]),
    )
    conn.execute(
        """UPDATE metric_adjudications SET status=?, promoted=1, promoted_at=?
           WHERE id=?""",
        (STATUS_PROMOTED, ts, adj_id),
    )
    formal_id, formal_ts, no = record_adjudication(
        conn,
        sample_id=sample["id"],
        hold_id=adj["hold_id"],
        lane=LANE_FORMAL,
        kind=KIND_FORMAL,
        path_code=PATH_B,
        verdict="合格",
        metrics=metrics,
        status=STATUS_FORMAL_ACTIVE,
        source="影子升正",
        actor=user,
        note=f"影子裁定 #{adj_id} 确认升正为正式裁定",
        official=1,
        promoted=1,
    )
    write_audit(
        conn,
        user,
        "adjudication_promote",
        sample["sample_code"],
        {"shadow_id": adj_id, "formal_id": formal_id, "adjudicated_at": ts},
    )
    return {
        "ok": True,
        "promoted": True,
        "shadow_id": adj_id,
        "adjudication_id": formal_id,
        "adjudication_no": no,
        "adjudicated_at": ts,
    }


@router.post("/api/adjudications/promote")
def promote_by_body(payload: dict, authorization: AuthHeader = None):
    user = require_writer(authorization)
    adj_id = payload.get("adjudication_id") or payload.get("id") or payload.get("shadow_id")
    if adj_id is None:
        raise HTTPException(422, "缺少 adjudication_id")
    lock_key = None
    with connect() as conn:
        adj = conn.execute(
            "SELECT sample_id FROM metric_adjudications WHERE id=?", (adj_id,)
        ).fetchone()
        if not adj:
            raise HTTPException(404, "影子裁定不存在")
        lock_key = f"sample:{adj['sample_id']}"
        require_lock(conn, lock_key, user)
        try:
            result = _promote(conn, user, int(adj_id))
            conn.commit()
            return result
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/adjudications/{adj_id}/promote")
@router.post("/api/adjudications/{adj_id}/confirm")
def promote_by_id(adj_id: int, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        adj = conn.execute(
            "SELECT sample_id FROM metric_adjudications WHERE id=?", (adj_id,)
        ).fetchone()
        if not adj:
            raise HTTPException(404, "影子裁定不存在")
        lock_key = f"sample:{adj['sample_id']}"
        require_lock(conn, lock_key, user)
        try:
            result = _promote(conn, user, adj_id)
            conn.commit()
            return result
        finally:
            release(conn, lock_key, user)
            conn.commit()


def _void(conn, user: str, adj_id: int) -> dict:
    adj = conn.execute(
        "SELECT * FROM metric_adjudications WHERE id=?", (adj_id,)
    ).fetchone()
    if not adj:
        raise HTTPException(404, "裁定不存在")
    if adj["status"] == STATUS_VOID:
        return {"ok": True, "voided": True, "id": adj_id, "reason": "已作废"}
    if adj["lane"] == LANE_FORMAL or adj["kind"] == KIND_FORMAL:
        raise HTTPException(400, "生效正式裁定当班锁死，不可作废；仅影子裁定可作废")
    if adj["status"] == STATUS_PROMOTED:
        raise HTTPException(400, "影子已升正，请走后续冲销流程，不可直接作废")
    ts = now_iso()
    conn.execute(
        "UPDATE metric_adjudications SET status=?, voided_at=? WHERE id=?",
        (STATUS_VOID, ts, adj_id),
    )
    sample = conn.execute(
        "SELECT sample_code FROM sample_batches WHERE id=?", (adj["sample_id"],)
    ).fetchone()
    write_audit(
        conn,
        user,
        "adjudication_void",
        sample["sample_code"] if sample else str(adj["sample_id"]),
        {"shadow_id": adj_id, "voided_at": ts, "official_metrics_changed": False},
    )
    return {"ok": True, "voided": True, "id": adj_id, "voided_at": ts}


@router.post("/api/adjudications/void")
def void_by_body(payload: dict, authorization: AuthHeader = None):
    user = require_writer(authorization)
    adj_id = payload.get("adjudication_id") or payload.get("id") or payload.get("shadow_id")
    if adj_id is None:
        raise HTTPException(422, "缺少 adjudication_id")
    with connect() as conn:
        result = _void(conn, user, int(adj_id))
        conn.commit()
        return result


@router.post("/api/adjudications/{adj_id}/void")
@router.post("/api/adjudications/{adj_id}/cancel")
def void_by_id(adj_id: int, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        result = _void(conn, user, adj_id)
        conn.commit()
        return result
