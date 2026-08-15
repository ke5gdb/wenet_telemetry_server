"""Tests for export formats and flight administration."""

from __future__ import annotations

import csv
import io
import json
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cbor2
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, export, main  # noqa: E402
from app.store import Store  # noqa: E402
from app.timeutil import iso  # noqa: E402

T0 = datetime(2026, 7, 26, 18, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "export.db")
    monkeypatch.setattr(config, "INGEST_API_KEY", None)
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "export.db", altitude_window=10.0)
    yield s
    s.close()


def sensor(count, offset_s, source="GDB_ENV_ENV", **fields):
    data = {"time": iso(T0 + timedelta(seconds=offset_s)), "id": source, "count": count}
    data.update(fields or {"hdc_temp": 27.2})
    return {"kind": "sensor", "data": data}


def gps(offset_s, altitude):
    return {"kind": "gps", "data": {
        "timestamp": iso(T0 + timedelta(seconds=offset_s)),
        "latitude": 32.9, "longitude": -97.2, "altitude": altitude, "ascent_rate": 5.0}}


def populate(store):
    """Two sources on one payload, plus a GPS track -- the real shape."""
    batch = [gps(i, 200.0 + 5.0 * i) for i in range(0, 40, 2)]
    for i in range(20):
        batch.append(sensor(i, i, "GDB_ENV_ENV",
                            hdc_temp=27.0 + i * 0.1, hdc_humi=41.0, v_in=6.126))
        batch.append(sensor(i, i, "GDB_ENV_LSM6DSOX", a_x=-0.96, imu_temp=26.6))
    store.ingest("KE5GDB", batch)
    store.backfill_altitudes(1, flush=True)


def collect(gen):
    return "".join(gen)


# -- CSV ------------------------------------------------------------------


def test_csv_wide_is_one_row_per_packet(store):
    populate(store)
    text = collect(export.csv_wide(store.pool.path, 1, "GDB_ENV_ENV"))
    rows = list(csv.DictReader(io.StringIO(text)))

    assert len(rows) == 20
    assert rows[0]["time"].startswith("2026-07-26T18:00:00")
    assert rows[0]["hdc_temp"] == "27.0"
    assert rows[5]["count"] == "5"
    # Altitude is the column that makes the rest of the row mean something.
    assert float(rows[5]["altitude_m"]) == pytest.approx(225.0)


def test_csv_wide_only_holds_that_source(store):
    populate(store)
    text = collect(export.csv_wide(store.pool.path, 1, "GDB_ENV_LSM6DSOX"))
    header = next(csv.reader(io.StringIO(text)))

    assert "a_x" in header and "imu_temp" in header
    assert "hdc_temp" not in header


def test_csv_long_covers_every_source(store):
    populate(store)
    rows = list(csv.DictReader(io.StringIO(collect(export.csv_long(store.pool.path, 1)))))

    # The transmitter's own telemetry exports alongside the BLE payloads': it
    # is a source like any other, so "every source" includes it.
    assert {r["source"] for r in rows} == {
        "GDB_ENV_ENV", "GDB_ENV_LSM6DSOX", "WENET_GPS"
    }
    assert {r["field"] for r in rows} >= {"hdc_temp", "hdc_humi", "v_in", "a_x", "imu_temp"}
    temp = next(r for r in rows if r["field"] == "hdc_temp")
    assert temp["unit"] == "°C"

    gps_rows = [r for r in rows if r["source"] == "WENET_GPS"]
    assert {r["field"] for r in gps_rows} >= {"latitude", "longitude", "altitude"}
    assert next(r for r in gps_rows if r["field"] == "altitude")["unit"] == "m"


def test_csv_gps_export(store):
    populate(store)
    rows = list(csv.DictReader(io.StringIO(collect(export.csv_gps(store.pool.path, 1)))))

    assert len(rows) == 20
    assert float(rows[0]["altitude_m"]) == 200.0
    assert float(rows[-1]["latitude"]) == 32.9


def test_export_of_unknown_flight_raises(store):
    with pytest.raises(LookupError):
        collect(export.csv_long(store.pool.path, 999))


# -- JSON Lines -----------------------------------------------------------


def test_jsonl_matches_the_analysis_log_shape(store):
    """Existing notebooks read the old payload logs; an export should too."""
    populate(store)
    lines = collect(export.jsonl(store.pool.path, 1)).strip().split("\n")

    # 40 sensor packets and the 20 GPS telemetry packets, which are exported as
    # packets because that is what they are.
    assert len(lines) == 60
    record = json.loads(lines[0])
    assert set(record) >= {"time", "id", "count"}
    assert record["id"] in ("GDB_ENV_ENV", "GDB_ENV_LSM6DSOX")
    assert "altitude" in record
    assert record["reporters"] == ["KE5GDB"]


def test_jsonl_records_every_reporter_that_heard_a_packet(store):
    packet = sensor(1, 0, hdc_temp=27.0)
    store.ingest("KE5GDB", [packet])
    store.ingest("K5NBC", [packet])

    record = json.loads(collect(export.jsonl(store.pool.path, 1)).strip())
    assert record["reporters"] == ["K5NBC", "KE5GDB"]


# -- raw archive ----------------------------------------------------------


def test_raw_cbor_round_trips(store):
    """The archive has to survive a decoder fix, so it must decode back exactly."""
    originals = [
        cbor2.dumps({"time": iso(T0 + timedelta(seconds=i)), "id": "GDB_ENV_ENV",
                     "count": i, "hdc_temp": 27.0 + i})
        for i in range(5)
    ]
    store.ingest("KE5GDB", [{"kind": "sensor", "raw": raw.hex()} for raw in originals])

    blob = b"".join(export.raw_cbor(store.pool.path, 1))

    recovered, offset = [], 0
    while offset < len(blob):
        (length,) = struct.unpack(">I", blob[offset:offset + 4])
        offset += 4
        recovered.append(blob[offset:offset + length])
        offset += length

    assert recovered == originals
    assert [cbor2.loads(r)["count"] for r in recovered] == [0, 1, 2, 3, 4]


def test_raw_cbor_skips_packets_that_had_no_raw_bytes(store):
    store.ingest("KE5GDB", [
        {"kind": "sensor", "raw": cbor2.dumps(
            {"time": iso(T0), "id": "A_ENV", "count": 1, "v": 1}).hex()},
        sensor(2, 1, "A_ENV", v=2),   # decoded only, no raw
    ])
    blob = b"".join(export.raw_cbor(store.pool.path, 1))
    (length,) = struct.unpack(">I", blob[:4])
    assert len(blob) == 4 + length


# -- database snapshot ----------------------------------------------------


def test_database_snapshot_is_a_usable_database(store, tmp_path):
    populate(store)
    _, stream = export.database_snapshot(store.pool.path)
    out = tmp_path / "snap.db"
    out.write_bytes(b"".join(stream))

    import sqlite3
    conn = sqlite3.connect(out)
    assert conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0] == 60
    assert conn.execute("SELECT COUNT(*) FROM gps").fetchone()[0] == 20
    conn.close()


def test_flight_slug_is_filename_safe(store):
    populate(store)
    store.update_flight(1, "RAB-9 / test flight #2", None)
    assert export.flight_slug(store.pool.get(), 1) == "rab-9-test-flight-2"


# -- flight administration ------------------------------------------------


def test_rename_and_note_a_flight(store):
    populate(store)
    row = store.update_flight(1, "RAB-9", "burst at 31km, recovered near Bowie")

    assert row["name"] == "RAB-9"
    assert "Bowie" in row["notes"]


def test_close_flight_resolves_its_tail(store):
    store.ingest("KE5GDB", [gps(0, 1000.0), gps(10, 1100.0), sensor(1, 12, v_in=4.0)])
    # The sensor packet specifically: a GPS telemetry packet is resolved on
    # arrival against its own fix and would never be pending.
    assert store.pool.get().execute(
        "SELECT p.altitude_resolved FROM packets p JOIN sources s ON s.id=p.source_id "
        "WHERE s.kind != 'gps'").fetchone()[0] == 0

    store.close_flight(1)

    row = store.pool.get().execute(
        "SELECT p.altitude, p.altitude_resolved FROM packets p "
        "JOIN sources s ON s.id=p.source_id WHERE s.kind != 'gps'").fetchone()
    assert row["altitude_resolved"] == 1
    assert row["altitude"] == 1100.0
    assert store.flights()[0]["ended_at"] is not None


def test_merge_undoes_a_wrongly_split_flight(store):
    """Half an hour behind a ridge should not be two flights in the record."""
    store.ingest("KE5GDB", [sensor(1, 0, v_in=4.0), gps(0, 500.0)],
                 received_default=iso(T0))
    store.ingest("KE5GDB", [sensor(2, 3600, v_in=4.1), gps(3600, 900.0)],
                 received_default=iso(T0 + timedelta(minutes=45)))
    assert len(store.flights()) == 2

    result = store.merge_flights(1, 2)

    # The BLE payload's source and the transmitter's GPS telemetry source, both
    # folded onto their twins on the other side of the gap.
    assert result["merged_sources"] == 2
    flights = store.flights()
    assert len(flights) == 1
    assert flights[0]["id"] == 1
    assert flights[0]["packet_count"] == 4      # two sensor, two GPS telemetry
    assert flights[0]["gps_count"] == 2
    # Two sources, not four -- the same payload and the same transmitter on
    # both sides of the gap.
    assert len(store.sources(1)) == 2


def test_merge_keeps_readings_attached_to_their_fields(store):
    store.ingest("KE5GDB", [sensor(1, 0, hdc_temp=20.0)], received_default=iso(T0))
    store.ingest("KE5GDB", [sensor(2, 3600, hdc_temp=25.0)],
                 received_default=iso(T0 + timedelta(minutes=45)))
    store.merge_flights(1, 2)

    (series,) = store.series(1, ["hdc_temp"], max_points=100)
    assert sorted(series["avg"]) == [20.0, 25.0]


def test_merge_drops_a_packet_both_flights_already_hold(store):
    packet = sensor(7, 0, v_in=4.0)
    store.ingest("KE5GDB", [packet], received_default=iso(T0))
    store.ingest("K5NBC", [packet], received_default=iso(T0 + timedelta(minutes=45)))

    store.merge_flights(1, 2)

    assert store.flights()[0]["packet_count"] == 1


def test_merge_rejects_a_flight_into_itself(store):
    populate(store)
    with pytest.raises(ValueError):
        store.merge_flights(1, 1)


def test_merge_unknown_flight_raises(store):
    populate(store)
    with pytest.raises(LookupError):
        store.merge_flights(1, 42)


def test_split_cuts_a_flight_in_two(store):
    populate(store)
    cut = iso(T0 + timedelta(seconds=10))

    result = store.split_flight(1, cut)

    # 40 sensor packets and 20 GPS telemetry packets. Past the cut: 20 sensor
    # (t=10..19 on two sources) and 15 GPS (t=10,12..38).
    assert result["moved_packets"] == 35
    flights = {f["id"]: f for f in store.flights()}
    assert flights[1]["packet_count"] == 25
    assert flights[result["new_flight_id"]]["packet_count"] == 35
    assert flights[1]["ended_at"] == cut


def test_split_keeps_series_readable_on_both_sides(store):
    populate(store)
    new_id = store.split_flight(1, iso(T0 + timedelta(seconds=10)))["new_flight_id"]

    (before,) = store.series(1, ["hdc_temp"], max_points=100)
    (after,) = store.series(new_id, ["hdc_temp"], max_points=100)

    assert before["n"] == 10 and after["n"] == 10
    assert max(before["avg"]) < min(after["avg"])


def test_split_with_nothing_after_the_cut_is_rejected(store):
    populate(store)
    with pytest.raises(ValueError):
        store.split_flight(1, iso(T0 + timedelta(days=1)))


# -- HTTP surface ---------------------------------------------------------


def test_export_endpoints_over_http(client):
    client.post("/api/v1/ingest", json={"reporter_id": "KE5GDB", "packets": [
        sensor(1, 0, hdc_temp=27.0), gps(0, 500.0)]})

    csv_resp = client.get("/api/v1/flights/1/export.csv")
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_resp.headers["content-disposition"]
    assert "hdc_temp" in csv_resp.text

    assert client.get("/api/v1/flights/1/export.jsonl").status_code == 200
    assert client.get("/api/v1/flights/1/gps.csv").status_code == 200
    assert client.get("/api/v1/flights/1/export.cbor").status_code == 200
    assert client.get("/api/v1/export.db").content[:15] == b"SQLite format 3"


def test_export_wide_csv_by_source_over_http(client):
    client.post("/api/v1/ingest", json={"reporter_id": "KE5GDB",
                                        "packets": [sensor(1, 0, hdc_temp=27.0)]})
    resp = client.get("/api/v1/flights/1/export.csv", params={"source": "GDB_ENV_ENV"})

    assert resp.status_code == 200
    assert "GDB_ENV_ENV.csv" in resp.headers["content-disposition"]


def test_export_404s_rather_than_streaming_a_broken_file(client):
    """A lazy generator would otherwise turn a bad request into a truncated 200."""
    client.post("/api/v1/ingest", json={"reporter_id": "KE5GDB",
                                        "packets": [sensor(1, 0, hdc_temp=27.0)]})

    assert client.get("/api/v1/flights/99/export.csv").status_code == 404
    assert client.get("/api/v1/flights/99/export.jsonl").status_code == 404
    assert client.get("/api/v1/flights/1/export.csv",
                      params={"source": "NOPE"}).status_code == 404


def test_flight_admin_over_http(client):
    client.post("/api/v1/ingest", json={"reporter_id": "KE5GDB",
                                        "packets": [sensor(1, 0, v_in=4.0)]})

    patched = client.patch("/api/v1/flights/1", json={"name": "RAB-9", "notes": "windy"})
    assert patched.json()["name"] == "RAB-9"

    closed = client.post("/api/v1/flights/1/close")
    assert closed.json()["ended_at"] is not None

    assert client.patch("/api/v1/flights/99", json={"name": "x"}).status_code == 404
    assert client.post("/api/v1/flights/1/merge", params={"from": 1}).status_code == 400
    assert client.post("/api/v1/flights/1/merge", params={"from": 99}).status_code == 404
