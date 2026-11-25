# -*- coding: utf-8 -*-
"""effluent-10 并发闸：race_exceed_close。

并发双请求打同一个 EX-J60 挂起闭环：一路 200 并写唯一新裁定，后到一路 409、
零裁定，只在 contention_log 留争用痕。

与 tests/close_recalc_c.py 内的 test_race_exceed_close 同源同口径，本文件按
“并发用 race_exceed_close”的测文件闸名单独成文，可直接：
    pytest tests/race_exceed_close.py
    pytest tests/race_exceed_close.py::race_exceed_close
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app import app
from db import init_db

HOLD_KEY = "EX-J60"
SAMPLE_CODE = "SM-EX-J60-RACE"
GOOD_METRICS = {"cod": 38.0, "nh3n": 2.4, "tp": 0.19}


def _token() -> str:
    c = TestClient(app)
    return c.post(
        "/api/login", json={"username": "admin", "password": "admin123"}
    ).json()["token"]


def _seed_open() -> int:
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
        return hid


def _run_race() -> list[tuple[int, dict]]:
    hid = _seed_open()
    headers = {"Authorization": f"Bearer {_token()}"}
    barrier = threading.Barrier(2, timeout=15)
    results: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def worker() -> tuple[int, dict]:
        c = TestClient(app)
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
        code = r.status_code
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}
        with lock:
            results.append((code, body))
        return code, body

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        pair = [f.result(timeout=30) for f in futures]

    statuses = sorted(code for code, _ in pair)
    assert statuses == [200, 409], f"期望一路 200 一路 409，实际 {pair}"

    import db

    with db.connect() as conn:
        hold = conn.execute("SELECT status FROM exceed_holds WHERE id=?", (hid,)).fetchone()
        assert hold["status"] == "已关闭"

        n_adj = conn.execute(
            "SELECT COUNT(*) c FROM metric_adjudications WHERE hold_id=?", (hid,)
        ).fetchone()["c"]
        assert n_adj == 1, f"败方零裁定、赢家一裁定，实际 {n_adj}"

        traces = conn.execute(
            "SELECT lock_key, detail FROM contention_log WHERE action='争用失败'"
        ).fetchall()
        hit = [
            t
            for t in traces
            if str(hid) in (t["lock_key"] or "")
            or (t["detail"] and "EX-J60" in t["detail"])
        ]
        assert hit, f"后到者未留争用痕：{[dict(t) for t in traces]}"

        locks = conn.execute(
            "SELECT COUNT(*) c FROM effluent_locks WHERE lock_key=?",
            (f"exceed_hold:{hid}",),
        ).fetchone()["c"]
        assert locks == 0, "闭环后挂起锁未释放"
    return pair


def race_exceed_close(tmp_path=None, monkeypatch=None):
    """闸名入口；pytest 收集时注入 tmp_path/monkeypatch，直调时自建临时库。"""
    if tmp_path is not None and monkeypatch is not None:
        monkeypatch.setattr("db.DB_PATH", tmp_path / "race_exceed_close_standalone.db")
        init_db()
    else:
        import tempfile
        from pathlib import Path

        import db

        db.DB_PATH = Path(tempfile.mkdtemp(prefix="race-exj60-standalone-")) / "race.db"
        init_db()
    _run_race()


def test_race_exceed_close(tmp_path, monkeypatch):
    race_exceed_close(tmp_path, monkeypatch)
