# -*- coding: utf-8 -*-
"""effluent-10 — close_recalc_c：双会话并发抢闭同一个 EX-J60 挂起。

两路会话同时 POST /api/exceeds/close 打同一 hold：
  * 只允许一路成功并写出恰好一条新裁定；
  * 后到者必须吃 409 争用失败、零裁定、零业务改写，只在 contention_log 留痕。

对应并发闸名：race_exceed_close（本文件内 test_race_exceed_close，
另有 tests/race_exceed_close.py 同名源文件）。
旧代码先在锁外读 status 再用 sample 键可重入锁，同用户双会话会双双 200，
且没有挂起级原子认领 —— 本用例见红。
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
SAMPLE_CODE = "SM-EX-J60-C"
GOOD_METRICS = {"cod": 39.0, "nh3n": 2.5, "tp": 0.2}


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "close_recalc_c.db")
    init_db()
    return tmp_path


def _token() -> str:
    c = TestClient(app)
    return c.post(
        "/api/login", json={"username": "admin", "password": "admin123"}
    ).json()["token"]


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
                json.dumps({"cod": 60.0, "nh3n": 4.6, "tp": 0.42}),
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
            (sid, HOLD_KEY, "COD", 60.0, 50.0, "开放", "2026-11-24T08:05:00", "race"),
        )
        hid = cur.lastrowid
        conn.commit()
        return sid, hid


def _run_race():
    """可直接调用的并发双请求抢闭流程（调用前须已把 db.DB_PATH 指向临时库并 init_db）。"""
    sid, hid = _seed_open()
    token = _token()
    barrier = threading.Barrier(2, timeout=15)
    results: list[tuple[int, dict]] = []

    def worker() -> tuple[int, dict]:
        c = TestClient(app)
        headers = {"Authorization": f"Bearer {token}"}
        barrier.wait()
        r = c.post(
            "/api/exceeds/close",
            headers=headers,
            json={
                "hold_id": hid,
                "note": "双会话抢闭 EX-J60",
                "metrics": GOOD_METRICS,
            },
        )
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}
        results.append((r.status_code, body))
        return r.status_code, body

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(worker) for _ in range(2)]
        pair = [f.result(timeout=30) for f in futs]

    statuses = sorted(code for code, _ in pair)
    # 唯一赢家 200，唯一输家 409 争用（sorted 后为 [200, 409]）
    assert statuses == [200, 409], f"期望恰好一路 200 一路 409，实际 {pair}"

    import db

    with db.connect() as conn:
        hold = conn.execute("SELECT * FROM exceed_holds WHERE id=?", (hid,)).fetchone()
        assert hold["status"] == "已关闭"

        try:
            adj = conn.execute(
                "SELECT * FROM metric_adjudications WHERE hold_id=?", (hid,)
            ).fetchall()
        except Exception:
            adj = []
        assert len(adj) == 1, f"赢家须恰好落一条裁定、败方零裁定，实际 {len(adj)}"

        traces = conn.execute(
            "SELECT * FROM contention_log WHERE action='争用失败'"
        ).fetchall()
        hit = [
            dict(t)
            for t in traces
            if (str(hid) in (t["lock_key"] or ""))
            or (t["detail"] and "EX-J60" in t["detail"])
            or (t["lock_key"] and "EX-J60" in t["lock_key"])
        ]
        assert hit, f"后到者须留争用痕，contention_log={[dict(t) for t in traces]}"

        # 正式指标只被赢家改写一次（合格值），不存在败方的第二份写入
        s = conn.execute("SELECT metrics FROM sample_batches WHERE id=?", (sid,)).fetchone()
        assert json.loads(s["metrics"])["cod"] == GOOD_METRICS["cod"]

    return pair


def race_exceed_close(tmp_path=None, monkeypatch=None):
    """闸名入口：并发用 race_exceed_close。

    若被 pytest 按该函数名收集，tmp_path/monkeypatch 由夹具注入；
    若被外部直接无参调用，则自建隔离临时库。
    """
    if tmp_path is not None and monkeypatch is not None:
        monkeypatch.setattr("db.DB_PATH", tmp_path / "race_exceed_close.db")
        init_db()
    else:
        import tempfile
        from pathlib import Path

        import db

        db.DB_PATH = Path(tempfile.mkdtemp(prefix="race-exj60-")) / "race.db"
        init_db()
    _run_race()


def test_race_exceed_close(db_env):
    _run_race()


def test_sequential_late_comer_leaves_single_trace_and_zero_adjudication(db_env):
    """非并发确定性补刀：第一次闭环 200，第二次 409 且只多一条争用痕、零裁定。"""
    sid, hid = _seed_open()
    token = _token()
    headers = {"Authorization": f"Bearer {token}"}
    c = TestClient(app)
    r1 = c.post(
        "/api/exceeds/close",
        headers=headers,
        json={"hold_id": hid, "metrics": GOOD_METRICS},
    )
    assert r1.status_code == 200, r1.text
    r2 = c.post(
        "/api/exceeds/close",
        headers=headers,
        json={"hold_id": hid, "metrics": GOOD_METRICS},
    )
    assert r2.status_code == 409

    import db

    with db.connect() as conn:
        n_adj = conn.execute(
            "SELECT COUNT(*) c FROM metric_adjudications WHERE hold_id=?", (hid,)
        ).fetchone()["c"]
        assert n_adj == 1
        traces = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE action='争用失败'"
        ).fetchone()["c"]
        assert traces >= 1
        # 闭环结束后一次性挂起锁必须释放干净。
        locks = conn.execute(
            "SELECT COUNT(*) c FROM effluent_locks WHERE lock_key=?",
            (f"exceed_hold:{hid}",),
        ).fetchone()["c"]
        assert locks == 0
