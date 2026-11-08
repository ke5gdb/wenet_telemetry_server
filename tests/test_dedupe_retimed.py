"""tools/dedupe_retimed.py against a flight shaped like 2026-10-24's BLN_NECK.

Every sample sent three times -- twice by current bridges on the host clock,
3 ms apart, and once by a legacy bridge on a Pico RTC 544 s fast, padded out to
the Wenet frame -- plus samples where only the legacy copy got through and a
host clock briefly five hours fast.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cbor2
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.store import Store  # noqa: E402

T0 = datetime(2026, 10, 24, 15, 58, 0, tzinfo=timezone.utc)
RTC_ERROR = 544.5
SOURCE = "BLN_NECK_LSM6DSOX"


def wire(t: datetime) -> str:
    return f"{t.strftime('%Y%m%dT%H%M%S')}.{t.microsecond // 1000:03d}Z"


def sample(count: int, fall: int, t: datetime, pad: bool = False) -> dict:
    data = {"time": wire(t), "id": SOURCE, "count": count, "fall_cnt": fall, "a_x": count / 7}
    raw = cbor2.dumps(data)
    if pad:
        raw += b"\0" * 64
    return {"kind": "sensor", "payload_callsign": "K5RWK", "raw": raw.hex(), "data": data}


def build(store: Store) -> list[int]:
    """Thirty samples at 2 Hz; fall_cnt steps up at the tenth. Returns the counts
    that arrived only as a legacy copy."""
    legacy_only = [1012, 1020]
    for i in range(30):
        count, fall = 1000 + i, 9 + (i >= 10) + (i >= 20)
        t = T0 + timedelta(seconds=i * 0.5)
        heard = (t + timedelta(seconds=0.6)).isoformat().replace("+00:00", "Z")
        batch = [sample(count, fall, t + timedelta(seconds=RTC_ERROR), pad=True)]
        if count not in legacy_only:
            host = t + timedelta(hours=5) if i == 3 else t   # the UTC/CDT slip
            batch += [sample(count, fall, host),
                      sample(count, fall, host + timedelta(milliseconds=3))]
        # The track the re-timed samples must land back on: 1 m per sample.
        batch.append({"kind": "gps", "payload_callsign": "K5RWK", "data": {
            "timestamp": t.isoformat(), "latitude": 32.9, "longitude": -97.2,
            "altitude": 1000.0 + i}})
        store.ingest("K5WFR", batch, received_default=heard)
        store.ingest("KA5BOU", batch[:1], received_default=heard)
    store.close_flight(1)
    return legacy_only


def run(db: Path, *args: str) -> str:
    out = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "dedupe_retimed.py"), "--db", str(db),
         "--flight", "1", *args],
        capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.fixture
def flight(tmp_path):
    db = tmp_path / "t.db"
    store = Store(db)
    legacy_only = build(store)
    store.close()
    return db, legacy_only


def rows(db: Path, sql: str):
    s = Store(db)
    try:
        return s.pool.get().execute(sql).fetchall()
    finally:
        s.close()


def test_dry_run_writes_nothing(flight):
    db, _ = flight
    sensor = "SELECT COUNT(*) FROM packets WHERE count >= 0"   # not the GPS telemetry
    before = rows(db, sensor)[0][0]
    out = run(db)
    assert "dry run" in out
    assert rows(db, sensor)[0][0] == before == 30 * 3 - 2 * 2


def test_repair_leaves_one_correctly_timed_packet_per_sample(flight):
    db, legacy_only = flight
    run(db, "--apply")

    got = rows(db, "SELECT count, time_epoch, json_extract(data,'$.fall_cnt') fall, "
                   "json_extract(data,'$.time') wt FROM packets WHERE count >= 0 "
                   "ORDER BY time_epoch")
    assert [r["count"] for r in got] == list(range(1000, 1030))     # one each, in order
    falls = [r["fall"] for r in got]
    assert falls == sorted(falls)                                    # the bug report
    for i, r in enumerate(got):
        truth = (T0 + timedelta(seconds=i * 0.5)).timestamp()
        # Re-timed ones are estimated from the measured error, so close, not exact.
        assert abs(r["time_epoch"] - truth) < (0.01 if r["count"] in legacy_only else 1e-6)
        assert r["wt"] == wire(datetime.fromtimestamp(r["time_epoch"], timezone.utc))


def test_repair_keeps_every_station_s_reception(flight):
    db, _ = flight
    run(db, "--apply")
    # K5WFR heard every copy; KA5BOU only the legacy one. Both stay on every sample.
    per = rows(db, "SELECT reporter_id, COUNT(*) FROM receptions GROUP BY reporter_id")
    assert dict((r[0], r[1]) for r in per) == {"K5WFR": 30, "KA5BOU": 30}


def test_readings_and_tallies_follow(flight):
    db, _ = flight
    run(db, "--apply")
    assert rows(db, "SELECT COUNT(*) FROM readings r JOIN packets p ON p.id=r.packet_id "
                    "WHERE r.ts != p.payload_time")[0][0] == 0
    src = rows(db, "SELECT packet_count, last_seen FROM sources WHERE kind='sensor'")[0]
    assert src["packet_count"] == 30
    assert src["last_seen"] < (T0 + timedelta(minutes=1)).isoformat()   # no +5 h tail
    assert rows(db, "SELECT COUNT(*) FROM packets WHERE altitude_resolved=0")[0][0] == 0
    # Re-timed samples were re-resolved at their corrected time, not the stale one.
    alt = {r[0]: r[1] for r in rows(db, "SELECT count, altitude FROM packets WHERE count>=1000")}
    for count in (1012, 1020, 1003):
        assert alt[count] == pytest.approx(1000.0 + count - 1000, abs=0.05)


def test_running_it_twice_changes_nothing(flight):
    db, _ = flight
    run(db, "--apply")
    snapshot = rows(db, "SELECT id, payload_time FROM packets ORDER BY id")
    out = run(db, "--apply")
    assert "deleted 0 packets, re-timed 0" in out
    assert rows(db, "SELECT id, payload_time FROM packets ORDER BY id") == snapshot
