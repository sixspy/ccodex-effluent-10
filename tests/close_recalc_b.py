# -*- coding: utf-8 -*-
"""effluent-10 / 排污癸第10槽 — 闭环重裁 乙路径（close_recalc_b）。

口径（乙）：闭环只关挂起，重裁只落【影子裁定】，正式指标列与时戳零改；
影子未经“确认升正”不得被准排页当作已裁，升正后才改写正式指标并落正式裁定；
作废后影子失效，正式指标始终零改。

失败测乙点破的事故：影子裁定未升正，准排页却把它当已裁直接放行。
旧代码没有裁定台账、close 直接把批态翻成“已裁定”，release 随即 200 —— 见红。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db

HOLD_KEY = "EX-J60"
SAMPLE_CODE = "SM-EX-J60-B"
GOOD_METRICS = {"cod": 41.0, "nh3n": 2.8, "tp": 0.22}
BAD_METRICS = {"cod": 60.0, "nh3n": 4.6, "tp": 0.42}


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "close_recalc_b.db")
    init_db()
    yield


@pytest.fixture()
def client():
    return TestClient(app)


def _auth(c: TestClient) -> dict:
    tok = c.post(
        "/api/login", json={"username": "admin", "password": "admin123"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _seed_open() -> tuple[int, int]:
    import db

    with db.connect() as conn:
        of = conn.execute(
            "SELECT id FROM outfalls WHERE outfall_code='OF-E1-A'"
        ).fetchone()
        cur = conn.execute(
            """INSERT INTO sample_batches(sample_code,outfall_id,permit_ref,metrics,
               flow_m3h,generation,status,adjudicated_at,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                SAMPLE_CODE,
                of["id"],
                "DP-2026-E01",
                json.dumps(BAD_METRICS),
                2600.0,
                1,
                "旁路挂起",
                None,
                "2026-11-24T08:00:00",
            ),
        )
        sid = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO exceed_holds(sample_id,hold_key,exceed_metric,measured,
               limit_value,status,opened_at,note) VALUES (?,?,?,?,?,?,?,?)""",
            (sid, HOLD_KEY, "COD", 60.0, 50.0, "开放", "2026-11-24T08:05:00", "EX-J60 乙"),
        )
        hid = cur.lastrowid
        conn.commit()
        return sid, hid


def _adjs(c: TestClient, h: dict, sid: int) -> list[dict]:
    r = c.get(f"/api/adjudications?sample_id={sid}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_b1_shadow_close_keeps_official_columns_untouched(client):
    h = _auth(client)
    sid, hid = _seed_open()

    resp = client.post(
        "/api/exceeds/close",
        headers=h,
        json={
            "hold_id": hid,
            "lane": "shadow",
            "note": "乙路径：先落影子裁定",
            "metrics": GOOD_METRICS,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("lane") in ("shadow", "影子", "乙")
    assert body.get("sample_adjudicated") is False

    import db

    with db.connect() as conn:
        s = conn.execute("SELECT * FROM sample_batches WHERE id=?", (sid,)).fetchone()
        hold = conn.execute("SELECT status FROM exceed_holds WHERE id=?", (hid,)).fetchone()
        official = json.loads(s["metrics"])
        assert hold["status"] == "已关闭"
        assert official == BAD_METRICS, "影子口径下正式指标必须零改"
        assert s["adjudicated_at"] is None, "影子口径下正式裁定时戳必须零改"

    items = _adjs(client, h, sid)
    shadows = [a for a in items if a["lane"] in ("影子", "shadow")]
    assert len(shadows) == 1
    sh = shadows[0]
    assert sh["verdict"] == "合格"
    assert sh["status"] in ("影子待升正", "待升正", "影子", "未升正")
    assert not sh.get("promoted", 0)


def test_b2_unpromoted_shadow_is_not_releasable(client):
    """关键乙红测：影子未升正，准排页不得当已裁放行。"""
    h = _auth(client)
    sid, hid = _seed_open()
    r = client.post(
        "/api/exceeds/close",
        headers=h,
        json={"hold_id": hid, "mode": "乙", "metrics": GOOD_METRICS},
    )
    assert r.status_code == 200, r.text

    rel = client.post("/api/releases", headers=h, json={"sample_id": sid})
    assert rel.status_code in (400, 409), rel.text

    import db

    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM discharge_releases WHERE sample_id=?", (sid,)
        ).fetchone()["c"]
    assert n == 0, "未升正的影子裁定被准排页当成了正式已裁"


def test_b3_compliant_official_metrics_but_only_shadow_still_blocks(client):
    """即便正式指标被外部改成合格，只要台账里仅有未升正影子，仍不得视为已裁。"""
    h = _auth(client)
    sid, hid = _seed_open()
    r = client.post(
        "/api/exceeds/close",
        headers=h,
        json={"hold_id": hid, "lane": "shadow", "metrics": GOOD_METRICS},
    )
    assert r.status_code == 200, r.text

    # 模拟正式指标列已被别的流程改合格，但没有任何生效正式裁定
    import db

    with db.connect() as conn:
        conn.execute(
            "UPDATE sample_batches SET metrics=? WHERE id=?",
            (json.dumps(GOOD_METRICS), sid),
        )
        conn.commit()

    rel = client.post("/api/releases", headers=h, json={"sample_id": sid})
    assert rel.status_code in (400, 409), rel.text
    assert "影子" in rel.json()["detail"] or "正式裁定" in rel.json()["detail"]


def test_b4_promote_shadow_then_release_allowed(client):
    h = _auth(client)
    sid, hid = _seed_open()
    r = client.post(
        "/api/exceeds/close",
        headers=h,
        json={"hold_id": hid, "lane": "shadow", "metrics": GOOD_METRICS},
    )
    adj_id = r.json()["adjudication_id"]

    p = client.post(f"/api/adjudications/{adj_id}/promote", headers=h, json={})
    assert p.status_code == 200, p.text
    assert p.json().get("promoted") is True

    import db

    with db.connect() as conn:
        s = conn.execute("SELECT * FROM sample_batches WHERE id=?", (sid,)).fetchone()
        assert json.loads(s["metrics"]) == GOOD_METRICS, "升正后影子指标须写入正式列"
        assert s["adjudicated_at"]

    rel = client.post(
        "/api/releases", headers=h, json={"sample_id": sid, "officer": "乙验收"}
    )
    assert rel.status_code == 200, rel.text


def test_b5_void_shadow_keeps_official_zero_change_and_blocks_release(client):
    h = _auth(client)
    sid, hid = _seed_open()
    r = client.post(
        "/api/exceeds/close",
        headers=h,
        json={"hold_id": hid, "lane": "shadow", "metrics": GOOD_METRICS},
    )
    adj_id = r.json()["adjudication_id"]

    v = client.post(f"/api/adjudications/{adj_id}/void", headers=h, json={})
    assert v.status_code == 200, v.text
    assert v.json().get("voided") is True

    import db

    with db.connect() as conn:
        s = conn.execute("SELECT * FROM sample_batches WHERE id=?", (sid,)).fetchone()
        assert json.loads(s["metrics"]) == BAD_METRICS, "作废影子后正式指标必须零改"
        assert s["adjudicated_at"] is None

    rel = client.post("/api/releases", headers=h, json={"sample_id": sid})
    assert rel.status_code in (400, 409)
