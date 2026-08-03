"""Tests for ingest, deduplication and schema discovery."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cbor2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.decode import decode_packet  # noqa: E402
from app.store import Store, derive_names  # noqa: E402
from app.timeutil import iso  # noqa: E402

T0 = datetime(2026, 7, 26, 18, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db", flight_gap_minutes=30)
    yield s
    s.close()


def packet(source="RAB_HAT_ENV", count=1, offset_s=0.0, **fields):
    data = {
        "time": iso(T0 + timedelta(seconds=offset_s)),
        "id": source,
        "count": count,
    }
    data.update(fields)
    return {"kind": "sensor", "data": data}


def rows(store, sql, params=()):
    return store.pool.get().execute(sql, params).fetchall()


def scalar(store, sql, params=()):
    return rows(store, sql, params)[0][0]


# -- deduplication --------------------------------------------------------


def test_same_packet_from_several_reporters_stores_one_sample(store):
    """The core rule: N stations hearing one transmission is one sample."""
    p = packet(count=7, bmp_temp=-54.2)

    first = store.ingest("KE5GDB", [p])
    second = store.ingest("K5NBC", [p])
    third = store.ingest("W5ABC", [p])

    assert first.accepted == 1
    assert second.accepted == 0 and second.duplicates == 1
    assert third.accepted == 0 and third.duplicates == 1

    assert scalar(store, "SELECT COUNT(*) FROM packets") == 1
    assert scalar(store, "SELECT COUNT(*) FROM receptions") == 3
    # Critically: one reading, not three. Three would triple-plot the series.
    assert scalar(store, "SELECT COUNT(*) FROM readings") == 1


def test_same_reporter_twice_does_not_duplicate_reception(store):
    p = packet(count=7, bmp_temp=-54.2)
    store.ingest("KE5GDB", [p])
    store.ingest("KE5GDB", [p])
    assert scalar(store, "SELECT COUNT(*) FROM receptions") == 1


def test_count_wrap_is_not_treated_as_a_duplicate(store):
    """count wraps at 65536, so the same count recurs within one flight."""
    store.ingest("KE5GDB", [packet(count=42, offset_s=0, bmp_temp=1.0)])
    store.ingest("KE5GDB", [packet(count=42, offset_s=600, bmp_temp=2.0)])

    assert scalar(store, "SELECT COUNT(*) FROM packets") == 2
    assert scalar(store, "SELECT COUNT(*) FROM readings") == 2


def test_reboot_replaying_timestamps_is_not_a_duplicate(store):
    """An unset RTC restarts at the same time on every boot; count separates."""
    store.ingest("KE5GDB", [packet(count=0, offset_s=0, v_in=4.0)])
    store.ingest("KE5GDB", [packet(count=1, offset_s=0, v_in=4.1)])

    assert scalar(store, "SELECT COUNT(*) FROM packets") == 2


def test_distinct_sources_never_collide(store):
    """ENV and LSM6DSOX share count/time namespaces but are separate series."""
    store.ingest("KE5GDB", [packet(source="RAB_HAT_ENV", count=1, bmp_temp=-54.0)])
    store.ingest("KE5GDB", [packet(source="RAB_HAT_LSM6DSOX", count=1, a_z=0.98)])

    assert scalar(store, "SELECT COUNT(*) FROM sources") == 2
    assert scalar(store, "SELECT COUNT(*) FROM packets") == 2


# -- schemaless discovery -------------------------------------------------


def test_unknown_fields_are_discovered_without_migration(store):
    result = store.ingest(
        "KE5GDB",
        [packet(count=1, wildly_novel_sensor=3.5, **{"temp-a4f2": 15.4})],
    )

    assert set(result.new_fields) >= {"wildly_novel_sensor", "temp-a4f2"}
    names = {r["name"]: r for r in rows(store, "SELECT * FROM fields")}
    # A runtime-named OneWire field still gets a unit, by rule rather than table.
    assert names["temp-a4f2"]["unit"] == "°C"
    assert names["wildly_novel_sensor"]["unit"] == ""


def test_known_fields_are_seeded_with_units(store):
    store.ingest("KE5GDB", [packet(count=1, bmp_pres=72.4, v_in=4.02)])
    units = dict(rows(store, "SELECT name, unit FROM fields"))
    assert units["bmp_pres"] == "hPa"
    assert units["v_in"] == "V"


def test_text_fields_are_registered_but_not_charted(store):
    store.ingest("KE5GDB", [packet(count=1, payload="XBnd RPT", pres=994.8)])
    kinds = dict(rows(store, "SELECT name, kind FROM fields"))
    assert kinds["payload"] == "text"
    assert kinds["pres"] == "numeric"
    # Only the numeric field produced a reading.
    assert scalar(store, "SELECT COUNT(*) FROM readings") == 1


def test_envelope_fields_are_not_charted(store):
    store.ingest("KE5GDB", [packet(count=1, bmp_temp=-54.0)])
    names = {r[0] for r in rows(store, "SELECT name FROM fields")}
    assert names == {"bmp_temp"}


def test_rab8_style_records_ingest(store):
    """Field vocabulary from the 2025 RAB-8 flight, which differs from current."""
    record = {
        "time": "20251101T132850.002Z",
        "vbatt": 7.9537984,
        "temp-004a": 15.375,
        "payload": "XBnd RPT",
        "id": "generic",
        "humi": 69.84036,
        "v_in": 6.2616152,
        "adc0": 1744,
        "pi_temp": 20,
        "pres": 994.80992,
        "count": 2,
        "temp": 13.655586,
    }
    result = store.ingest("KE5GDB", [{"kind": "sensor", "data": record}])

    assert result.accepted == 1
    source = rows(store, "SELECT * FROM sources")[0]
    # `id` was "generic"; the human name lives in a separate field and wins.
    assert source["payload_name"] == "XBnd RPT"
    assert source["source_key"] == "generic"


@pytest.mark.parametrize(
    "source_key, data, expected",
    [
        ("RAB_HAT_ENV", {}, ("RAB_HAT", "ENV")),
        ("ADC_Neck_LSM6DSOX", {}, ("ADC_Neck", "LSM6DSOX")),
        ("generic", {"payload": "XBnd RPT"}, ("XBnd RPT", "generic")),
        ("solo", {}, ("solo", "")),
    ],
)
def test_derive_names(source_key, data, expected):
    assert derive_names(source_key, data) == expected


# -- truncation and raw bytes ---------------------------------------------


def test_padded_cbor_decodes_cleanly():
    """The transmit path right-pads every packet to 254 bytes with NULs."""
    raw = cbor2.dumps({"time": iso(T0), "id": "RAB_HAT_ENV", "count": 1, "v_in": 4.0})
    data, truncated = decode_packet(raw.ljust(254, b"\x00"))

    assert not truncated
    assert data["id"] == "RAB_HAT_ENV"
    assert data["v_in"] == 4.0


def test_truncated_cbor_keeps_the_fields_that_survived():
    """A small ATT MTU cuts the tail off; keep what arrived rather than none."""
    raw = cbor2.dumps(
        {"time": iso(T0), "id": "RAB_HAT_ENV", "count": 5, "v_in": 4.0, "bmp_temp": -54.2}
    )
    data, truncated = decode_packet(raw[: len(raw) - 6])

    assert truncated
    assert data["id"] == "RAB_HAT_ENV"
    assert data["count"] == 5
    assert "bmp_temp" not in data


def test_truncated_packet_is_stored_and_flagged(store):
    raw = cbor2.dumps(
        {"time": iso(T0), "id": "RAB_HAT_ENV", "count": 5, "v_in": 4.0, "bmp_temp": -54.2}
    )
    result = store.ingest(
        "KE5GDB", [{"kind": "sensor", "raw": raw[: len(raw) - 6].hex()}]
    )

    assert result.accepted == 1
    row = rows(store, "SELECT * FROM packets")[0]
    assert row["truncated"] == 1
    assert json.loads(row["data"])["v_in"] == 4.0


def test_raw_bytes_are_archived_and_win_over_supplied_data(store):
    """Re-decoding server-side keeps the archive independent of forwarder bugs."""
    raw = cbor2.dumps({"time": iso(T0), "id": "RAB_HAT_ENV", "count": 3, "v_in": 4.25})
    store.ingest(
        "KE5GDB",
        [{"kind": "sensor", "raw": raw.hex(), "data": {"id": "WRONG", "count": 999}}],
    )

    row = rows(store, "SELECT * FROM packets")[0]
    assert bytes(row["raw_cbor"]) == raw
    assert json.loads(row["data"])["v_in"] == 4.25
    assert rows(store, "SELECT source_key FROM sources")[0][0] == "RAB_HAT_ENV"


def test_undecodable_packet_is_kept_as_a_reject(store):
    result = store.ingest("KE5GDB", [{"kind": "sensor", "raw": "ff" * 40}])

    assert result.rejected == 1
    assert scalar(store, "SELECT COUNT(*) FROM rejects") == 1
    assert scalar(store, "SELECT COUNT(*) FROM packets") == 0


def test_missing_id_is_rejected_not_crashed(store):
    result = store.ingest("KE5GDB", [{"kind": "sensor", "data": {"time": iso(T0)}}])
    assert result.rejected == 1
    assert "missing id" in result.reasons[0]


def test_one_bad_packet_does_not_lose_the_rest_of_the_batch(store):
    batch = [
        packet(count=1, v_in=4.0),
        {"kind": "sensor", "data": {"nonsense": True}},
        packet(count=2, v_in=4.1),
    ]
    result = store.ingest("KE5GDB", batch)

    assert result.accepted == 2
    assert result.rejected == 1


# -- flights --------------------------------------------------------------


def test_silence_gap_starts_a_new_flight(store):
    store.ingest("KE5GDB", [packet(count=1)], received_default=iso(T0))
    store.ingest(
        "KE5GDB",
        [packet(count=2, offset_s=1)],
        received_default=iso(T0 + timedelta(minutes=31)),
    )

    flights = store.flights()
    assert len(flights) == 2
    closed = [f for f in flights if f["ended_at"]]
    assert len(closed) == 1
    # The old flight ends at its last real activity, not at the moment we noticed.
    assert closed[0]["ended_at"] == iso(T0)


def test_activity_within_the_gap_stays_one_flight(store):
    store.ingest("KE5GDB", [packet(count=1)], received_default=iso(T0))
    store.ingest(
        "KE5GDB",
        [packet(count=2, offset_s=1)],
        received_default=iso(T0 + timedelta(minutes=29)),
    )
    assert len(store.flights()) == 1


def test_flight_summary_counts(store):
    store.ingest("KE5GDB", [packet(count=1, v_in=4.0), packet(count=2, v_in=4.1)])
    store.ingest("K5NBC", [packet(count=1, v_in=4.0)])

    flight = store.flights()[0]
    assert flight["packet_count"] == 2
    assert flight["source_count"] == 1

    reporters = store.reporters(flight["id"])
    assert {r["reporter_id"]: r["packets"] for r in reporters} == {"KE5GDB": 2, "K5NBC": 1}


# -- GPS ------------------------------------------------------------------


def gps(offset_s=0.0, **fields):
    data = {
        "timestamp": iso(T0 + timedelta(seconds=offset_s)),
        "latitude": 32.9,
        "longitude": -97.2,
        "altitude": 18422.0,
        "ascent_rate": 5.2,
        "numSV": 9,
        "gpsFix": 3,
    }
    data.update(fields)
    return {"kind": "gps", "data": data}


def test_gps_is_stored_and_deduplicated(store):
    store.ingest("KE5GDB", [gps()])
    result = store.ingest("K5NBC", [gps()])

    assert result.gps_duplicates == 1
    assert scalar(store, "SELECT COUNT(*) FROM gps") == 1
    assert scalar(store, "SELECT COUNT(*) FROM gps_receptions") == 2

    row = rows(store, "SELECT * FROM gps")[0]
    assert row["altitude"] == 18422.0
    assert row["num_sv"] == 9
    assert row["gps_fix"] == 3


def test_unknown_gps_fields_are_preserved_in_extra(store):
    store.ingest("KE5GDB", [gps(batt_v=7.9, cpu_temp=41.2, focus_fom=1234)])
    extra = json.loads(rows(store, "SELECT extra FROM gps")[0][0])
    assert extra["batt_v"] == 7.9
    assert extra["focus_fom"] == 1234


def nofix(offset_s=0.0, **fields):
    """What the forwarder now sends when the receiver has no lock: housekeeping
    intact, every position-derived field explicitly null."""
    data = {
        "timestamp": iso(T0 + timedelta(seconds=offset_s)),
        "latitude": None, "longitude": None, "altitude": None,
        "ascent_rate": None, "ground_speed": None, "heading": None,
        "numSV": 4, "gpsFix": 0, "batt_v": 7.9, "cpu_temp": 41.2,
    }
    data.update(fields)
    return {"kind": "gps", "data": data}


def test_gps_without_a_fix_is_recorded_not_discarded(store):
    """The housekeeping is most valuable exactly when the GPS is misbehaving."""
    result = store.ingest("KE5GDB", [nofix()])

    assert result.gps_accepted == 1
    assert result.rejected == 0
    row = rows(store, "SELECT * FROM gps")[0]
    assert row["latitude"] is None and row["altitude"] is None
    assert row["num_sv"] == 4 and row["gps_fix"] == 0
    extra = json.loads(row["extra"])
    assert extra["batt_v"] == 7.9 and extra["cpu_temp"] == 41.2


def test_gps_without_a_fix_never_reaches_the_map(store):
    store.ingest("KE5GDB", [gps(0, altitude=500.0), nofix(10), nofix(20), gps(30, altitude=800.0)])
    track = store.track(1)

    assert track["n"] == 2
    assert None not in track["lat"]
    assert track["alt"] == [500.0, 800.0]


def test_no_fix_packets_cannot_corrupt_an_interpolated_altitude(store):
    """The original reason for dropping them -- 0,0 at zero altitude would drag
    every nearby altitude toward the ground."""
    store.ingest("KE5GDB", [gps(0, altitude=1000.0), nofix(5), gps(10, altitude=1100.0)])
    store.ingest("KE5GDB", [packet(count=1, offset_s=5, bmp_temp=-40.0)])
    store.backfill_altitudes(1, flush=True)

    assert scalar(store, "SELECT altitude FROM packets") == 1050.0


def test_no_fix_packets_do_not_skew_the_peak(store):
    store.ingest("KE5GDB", [gps(0, altitude=9000.0), nofix(10)])
    assert store.latest(1)["peak_altitude"] == 9000.0


def test_latest_keeps_the_last_position_but_flags_it_stale(store):
    store.ingest("KE5GDB", [gps(0, altitude=9000.0)])
    store.ingest("KE5GDB", [nofix(10)])
    out = store.latest(1)

    # The last real position is still shown -- it is the best available.
    assert out["gps"]["altitude"] == 9000.0
    # But the display is told it is no longer current.
    assert out["gps_status"]["has_position"] is False
    assert out["gps_status"]["position_is_stale"] is True
    assert out["gps_status"]["num_sv"] == 4


def test_gps_status_is_not_stale_while_fixes_keep_arriving(store):
    store.ingest("KE5GDB", [nofix(0), gps(10, altitude=500.0)])
    status = store.latest(1)["gps_status"]

    assert status["has_position"] is True
    assert status["position_is_stale"] is False


def test_unlocked_receiver_packets_fall_back_to_reception_time(store):
    """A receiver that never locked has no clock. Without this fallback every
    one of these would land on the same 1980 instant and dedup to a single row,
    losing the housekeeping series they were kept for."""
    batch = [
        {"kind": "gps", "data": {"latitude": None, "longitude": None, "altitude": None,
                                 "numSV": 0, "gpsFix": 0, "batt_v": 7.9 - i * 0.1,
                                 "time_source": "receiver"}}
        for i in range(3)
    ]
    result = store.ingest("KE5GDB", batch, received_default=iso(T0))

    # Same reception time collapses them, which is correct -- they are one
    # batch. The point is that they are accepted, not rejected.
    assert result.rejected == 0
    assert result.gps_accepted >= 1
    row = rows(store, "SELECT * FROM gps")[0]
    assert json.loads(row["extra"])["time_source"] == "receiver"
    assert row["timestamp"] == iso(T0)


def test_unlocked_packets_across_batches_form_a_series(store):
    for i in range(3):
        store.ingest(
            "KE5GDB",
            [{"kind": "gps", "data": {"latitude": None, "numSV": 0, "gpsFix": 0,
                                      "batt_v": 7.9 - i * 0.1}}],
            received_default=iso(T0 + timedelta(seconds=i * 10)),
        )
    stored = rows(store, "SELECT timestamp, extra FROM gps ORDER BY time_epoch")

    assert len(stored) == 3
    assert [round(json.loads(r["extra"])["batt_v"], 1) for r in stored] == [7.9, 7.8, 7.7]


def test_gps_shares_the_flight_with_sensor_data(store):
    """The join that makes a sensor-vs-altitude plot possible."""
    store.ingest("KE5GDB", [packet(count=1, bmp_temp=-54.2), gps()])

    flight = store.flights()[0]
    assert flight["packet_count"] == 1
    assert flight["gps_count"] == 1
    assert scalar(store, "SELECT flight_id FROM gps") == flight["id"]
