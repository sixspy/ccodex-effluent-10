# -*- coding: utf-8 -*-
"""effluent-10 / 排污癸第10槽 — 闭环重裁 甲路径（close_recalc_a）。

口径（甲）：超标挂起 EX-J60 闭环必须在【同一事务】内重裁指标并写出新裁定，
正式指标列与新裁定一致、裁定时戳刷新；若给不出合格重裁/写不出新裁定，整笔
回退拒收（挂起仍开放、正式指标零改、裁定零落库）。

本文件在修复前必须 pytest 见红：旧 /api/exceeds/close 只翻 exceed_holds 状态
与 sample_batches.status/adjudicated_at，metrics 原封不动，且根本没有裁定
台账 —— 正是“闭环后正式指标列仍超且未落新裁定”。
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db

HOLD_KEY = "EX-J60"
SAMPLE_CODE = "SM-UT-J60-A"
OLD_TS = "2026-11-24T08:00:00"
GOOD_METRICS = {"cod": 42.0, "nh3n": 3.1, "tp": 0.24}
BAD_METRICS = {"cod": 60.0, "nh3n": 4.6, "tp": 0.42}


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "close_recalc_a.db")
    init_db()
    yield


@pytest.fixture()
def client():
    return TestClient(app)


def _auth(c: TestClient, user: str = "admin", password: str = "admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _seed_incident(*, closed: bool) -> tuple[int, int]:
    """布置 EX-J60 事故夹具。

    closed=True  -> 复现“已闭环但指标未重裁”的事故脏态：挂起已关闭、批态已裁定、
                    旧裁定时戳、正式指标列仍超标、无任何新裁定。
    closed=False -> 开放中的 EX-J60，供闭环/回退用例调用。
    """
    import db

    with db.connect() as conn:
        of = conn.execute(
            "SELECT id FROM outfalls WHERE outfall_code='OF-E1-A'"
        ).fetchone()
        status = "已裁定" if closed else "旁路挂起"
        cur = conn.execute(
            """INSERT INTO sample_batches(sample_code,outfall_id,permit_ref,metrics,
               flow_m3h,generation,status,adjudicated_at,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                SAMPLE_CODE,
                of["id"],
                "DP-2026-E01",
                json.dumps(BAD_METRICS, ensure_ascii=False),
                2600.0,
                1,
                status,
                OLD_TS if closed else None,
                OLD_TS,
            ),
        )
        sid = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO exceed_holds(sample_id,hold_key,exceed_metric,measured,
               limit_value,status,opened_at,closed_at,note)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                sid,
                HOLD_KEY,
                "COD",
                60.0,
                50.0,
                "已关闭" if closed else "开放",
                OLD_TS,
                OLD_TS if closed else None,
                "事故夹具：闭环未重裁" if closed else "EX-J60 开放挂起",
            ),
        )
        hid = cur.lastrowid
        conn.commit()
        return sid, hid


def _adjudication_rows(sample_id: int) -> list[dict]:
    import db

    with db.connect() as conn:
        try:
            rows = conn.execute(
                "SELECT * FROM metric_adjudications WHERE sample_id=? ORDER BY id",
                (sample_id,),
            ).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]


def test_a1_incident_fixture_closed_but_metrics_still_exceed():
    """事故夹具本身必须成立：闭了环、批态可准排，正式指标却仍超标。"""
    sid, hid = _seed_incident(closed=True)
    import db

    with db.connect() as conn:
        s = conn.execute("SELECT * FROM sample_batches WHERE id=?", (sid,)).fetchone()
        h = conn.execute("SELECT * FROM exceed_holds WHERE id=?", (hid,)).fetchone()
        metrics = json.loads(s["metrics"])
        assert h["status"] == "已关闭"
        assert s["status"] == "已裁定"
        assert metrics["cod"] > 50.0  # 旧超标值原样留在正式指标列


def test_a2_dirty_incident_must_not_be_releasable(client):
    """失败测甲（事故态）：闭环未重裁的 EX-J60 不得被准排页放行。

    旧代码下 release 只看 status='已裁定' + 无开放挂起 -> 200 假绿，本用例见红。
    """
    h = _auth(client)
    sid, _hid = _seed_incident(closed=True)

    resp = client.post("/api/releases", headers=h, json={"sample_id": sid})
    assert resp.status_code in (400, 409), resp.text

    import db

    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM discharge_releases WHERE sample_id=?", (sid,)
        ).fetchone()["c"]
    assert n == 0, "旧超标指标被准排页放行，形成假绿"


def test_a3_close_same_txn_recalc_writes_new_adjudication(client):
    """甲主路径：带复测合格指标闭环 -> 同事务更新正式指标列 + 新裁定 + 新时戳。"""
    h = _auth(client)
    sid, hid = _seed_incident(closed=False)

    resp = client.post(
        "/api/exceeds/close",
        headers=h,
        json={
            "hold_id": hid,
            "note": "加药回调后复测合格，甲路径同裁",
            "metrics": GOOD_METRICS,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("adjudication_id"), f"响应未给新裁定 id：{body}"
    new_ts = body.get("adjudicated_at") or body.get("recalc_at")
    assert new_ts and new_ts > OLD_TS

    import db

    with db.connect() as conn:
        s = conn.execute("SELECT * FROM sample_batches WHERE id=?", (sid,)).fetchone()
        hold = conn.execute("SELECT * FROM exceed_holds WHERE id=?", (hid,)).fetchone()
        metrics = json.loads(s["metrics"])
        assert hold["status"] == "已关闭"
        assert metrics["cod"] == GOOD_METRICS["cod"], "正式指标列未同事务重写"
        assert metrics["nh3n"] == GOOD_METRICS["nh3n"]
        assert s["adjudicated_at"] == new_ts

    rows = _adjudication_rows(sid)
    close_rows = [r for r in rows if r.get("hold_id") == hid]
    assert len(close_rows) == 1, f"须恰好落一条闭环新裁定，实际 {close_rows}"
    row = close_rows[0]
    assert row["verdict"] == "合格"
    assert row["lane"] in ("正式", "formal", "甲")
    assert row["adjudicated_at"] == new_ts

    # 采样页详情/列表必须同源带出新生效裁定与时戳
    detail = client.get(f"/api/samples/{sid}", headers=h).json()
    assert detail["sample"]["adjudicated_at"] == new_ts
    adjs = detail.get("adjudications") or detail.get("metric_adjudications") or []
    assert any(a["id"] == row["id"] for a in adjs)
    listed = client.get("/api/samples", headers=h).json()["items"]
    item = next(x for x in listed if x["id"] == sid)
    assert item["adjudicated_at"] == new_ts


def test_a4_still_exceed_metrics_close_is_rejected_and_rolls_back(client):
    """甲口径“否则整笔回退拒收”：重裁仍超标 -> 拒收，挂起仍开放、零落裁定。"""
    h = _auth(client)
    sid, hid = _seed_incident(closed=False)

    # 即便显式带指标，只要仍超标就必须整笔拒收
    resp = client.post(
        "/api/exceeds/close",
        headers=h,
        json={"hold_id": hid, "note": "试图硬闭", "metrics": BAD_METRICS},
    )
    assert resp.status_code in (400, 409, 422), resp.text

    import db

    with db.connect() as conn:
        s = conn.execute("SELECT * FROM sample_batches WHERE id=?", (sid,)).fetchone()
        hold = conn.execute("SELECT * FROM exceed_holds WHERE id=?", (hid,)).fetchone()
        metrics = json.loads(s["metrics"])
        assert hold["status"] == "开放", "拒收后挂起不得被关闭"
        assert metrics["cod"] == BAD_METRICS["cod"], "回退后正式指标不得被改写"
        assert s["adjudicated_at"] is None
    assert _adjudication_rows(sid) == [], "拒收不得落任何裁定"
    # 拒收回滚后不得遗留一次性挂起锁，否则后续正常闭环会被永久误判争用。
    import db

    with db.connect() as conn:
        leaked = conn.execute(
            "SELECT COUNT(*) c FROM effluent_locks WHERE lock_key LIKE 'exceed_hold:%'"
        ).fetchone()["c"]
    assert leaked == 0, "拒收后遗留挂起锁行"


def test_a5_note_only_close_writes_truthful_exceed_ruling_but_release_blocked(client):
    """不带复测值的常规闭环（与旧 smoke 同形）：闭环 200 且落新裁定时戳，

    但在库正式指标仍超标，新正式裁定结论须如实为“超标”，正式指标零改写，
    准排闸依旧拒绝 —— 闭环成功不等于假绿放行。
    """
    h = _auth(client)
    sid, hid = _seed_incident(closed=False)

    resp = client.post(
        "/api/exceeds/close", headers=h, json={"hold_id": hid, "note": "只有说明"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["adjudication_id"]
    assert body["verdict"] == "超标"

    import db

    with db.connect() as conn:
        s = conn.execute("SELECT * FROM sample_batches WHERE id=?", (sid,)).fetchone()
        hold = conn.execute("SELECT status FROM exceed_holds WHERE id=?", (hid,)).fetchone()
        metrics = json.loads(s["metrics"])
        assert hold["status"] == "已关闭"
        assert metrics["cod"] == BAD_METRICS["cod"], "未带复测值时正式指标不得改写"
        assert s["adjudicated_at"]

    rows = _adjudication_rows(sid)
    close_rows = [r for r in rows if r.get("hold_id") == hid]
    assert len(close_rows) == 1
    assert close_rows[0]["verdict"] == "超标"
    assert close_rows[0]["lane"] in ("正式", "formal", "甲")

    rel = client.post("/api/releases", headers=h, json={"sample_id": sid})
    assert rel.status_code in (400, 409), rel.text


# 保留 threading 引用，表明本文件也可承载并发探针（真正并发用例在 close_recalc_c / race_exceed_close）
_ = (threading, ThreadPoolExecutor)


def test_a6_five_surfaces_same_source_writeoff(client):
    """独验收段后端同源核销：超标闭环后采样时戳、准排放行、对账一致、总览减数。"""
    h = _auth(client)
    sid, hid = _seed_incident(closed=False)

    dash_before = client.get("/api/dashboard", headers=h).json()
    open_before = dash_before["open_exceeds"]

    resp = client.post(
        "/api/exceeds/close",
        headers=h,
        json={"hold_id": hid, "note": "五页同源验收", "metrics": GOOD_METRICS},
    )
    assert resp.status_code == 200, resp.text
    ts = resp.json()["adjudicated_at"]

    # 超标页：开放列表不再有 EX-J60，已关闭行带来重裁时戳。
    opens = client.get("/api/exceeds?status=开放", headers=h).json()["items"]
    assert all(x["hold_key"] != HOLD_KEY for x in opens)
    closed = client.get("/api/exceeds?status=已关闭", headers=h).json()["items"]
    j60 = next(x for x in closed if x["hold_key"] == HOLD_KEY)
    assert j60["recalc_at"] == ts

    # 采样页：列表与详情同一新裁定时戳。
    item = next(
        x for x in client.get("/api/samples", headers=h).json()["items"] if x["id"] == sid
    )
    assert item["adjudicated_at"] == ts
    assert item["latest_adjudication"]["adjudicated_at"] == ts

    # 准排页：新裁定合格后放行（旧超标态在 a2 已点破会被拒）。
    rel = client.post("/api/releases", headers=h, json={"sample_id": sid})
    assert rel.status_code == 200, rel.text

    # 对账页：指标与新裁定一致且 matched。
    recon = client.get("/api/reconcile/DP-2026-E01", headers=h).json()
    row = next(x for x in recon["samples"] if x["sample_code"] == SAMPLE_CODE)
    assert row["adjudicated_at"] == ts
    assert row["adjudication_consistent"] is True
    assert row["adjudication"]["status"] == "生效"
    assert row["matched"] is True

    # 总览页：开放超标数随闭环递减 1。
    dash_after = client.get("/api/dashboard", headers=h).json()
    assert dash_after["open_exceeds"] == open_before - 1
