"""Tests for altitude resolution against the GPS track."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import Store, interpolate_altitude  # noqa: E402
from app.timeutil import iso  # noqa: E402

T0 = datetime(2026, 7, 26, 18, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "alt.db", altitude_window=10.0)
    yield s
    s.close()


def sensor(count, offset_s, **fields):
    data = {"time": iso(T0 + timedelta(seconds=offset_s)), "id": "RAB_HAT_ENV", "count": count}
    data.update(fields or {"bmp_temp": -54.2})
    return {"kind": "sensor", "data": data}


def gps(offset_s, altitude):
    return {
        "kind": "gps",
        "data": {
            "timestamp": iso(T0 + timedelta(seconds=offset_s)),
            "latitude": 32.9,
            "longitude": -97.2,
            "altitude": altitude,
        },
    }


#: GPS telemetry is stored as a source of its own, so `packets` holds those too.
#: They are resolved on arrival against their own fix and are not what the
#: interpolation is being tested on -- these tests are about sensor packets
#: reaching backwards and forwards to a track.
SENSOR_ONLY = "JOIN sources s ON s.id = p.source_id WHERE s.kind != 'gps'"


def altitudes(store):
    return [
        (r["count"], r["altitude"], r["altitude_resolved"])
        for r in store.pool.get().execute(
            "SELECT p.count, p.altitude, p.altitude_resolved FROM packets p "
            f"{SENSOR_ONLY} ORDER BY p.time_epoch"
        )
    ]


# -- the interpolation itself ---------------------------------------------


def test_interpolates_between_bracketing_fixes():
    times, alts = [0.0, 10.0], [1000.0, 2000.0]
    assert interpolate_altitude(times, alts, 5.0, 10.0) == 1500.0
    assert interpolate_altitude(times, alts, 2.5, 10.0) == 1250.0


def test_exact_match_returns_the_fix():
    assert interpolate_altitude([0.0, 10.0], [1000.0, 2000.0], 10.0, 10.0) == 2000.0


def test_beyond_the_window_returns_none():
    """A GPS dropout must produce a gap, not an invented altitude."""
    times, alts = [0.0, 600.0], [1000.0, 5000.0]
    # 300 s from either fix -- interpolating here would be pure invention.
    assert interpolate_altitude(times, alts, 300.0, 10.0) is None


def test_edge_of_track_holds_the_nearest_fix_within_the_window():
    times, alts = [100.0, 110.0], [1000.0, 2000.0]
    assert interpolate_altitude(times, alts, 95.0, 10.0) == 1000.0   # before the start
    assert interpolate_altitude(times, alts, 118.0, 10.0) == 2000.0  # after the end
    assert interpolate_altitude(times, alts, 85.0, 10.0) is None     # too far before


def test_window_boundary_is_inclusive():
    times, alts = [0.0], [1234.0]
    assert interpolate_altitude(times, alts, 10.0, 10.0) == 1234.0
    assert interpolate_altitude(times, alts, 10.001, 10.0) is None


def test_empty_track_returns_none():
    assert interpolate_altitude([], [], 5.0, 10.0) is None


# -- end to end through the store -----------------------------------------


def test_altitude_is_resolved_after_ingest(store):
    store.ingest("KE5GDB", [gps(0, 1000.0), gps(10, 1100.0), gps(20, 1200.0)])
    store.ingest("KE5GDB", [sensor(1, 5)])

    resolved = altitudes(store)
    assert resolved == [(1, 1050.0, 1)]


def test_packet_is_left_pending_until_gps_passes_it(store):
    """A nearer fix may still be in flight, and would give a better answer."""
    store.ingest("KE5GDB", [gps(0, 1000.0)])
    store.ingest("KE5GDB", [sensor(1, 5)])

    # GPS only reaches t=0, the packet is at t=5; nothing past t+window yet.
    assert altitudes(store) == [(1, None, 0)]

    store.ingest("KE5GDB", [gps(10, 1100.0), gps(20, 1200.0)])
    assert altitudes(store) == [(1, 1050.0, 1)]


def test_interpolation_is_not_stretched_across_a_wide_gap(store):
    """Fixes 20 s apart: the far one is outside the window, so hold the near one.

    Reaching across the gap would use a fix 15 s away, which is exactly what the
    window exists to forbid.
    """
    store.ingest("KE5GDB", [gps(0, 1000.0), gps(20, 1200.0)])
    store.ingest("KE5GDB", [sensor(1, 5)])

    assert altitudes(store) == [(1, 1000.0, 1)]


def test_gps_arriving_after_the_samples_still_fills_them(store):
    """The ordering that forced this to be a backfill rather than a lookup."""
    store.ingest("KE5GDB", [sensor(1, 2), sensor(2, 4), sensor(3, 6)])
    assert [a for _, a, _ in altitudes(store)] == [None, None, None]

    store.ingest("KE5GDB", [gps(0, 1000.0), gps(10, 2000.0), gps(20, 3000.0)])

    assert altitudes(store) == [(1, 1200.0, 1), (2, 1400.0, 1), (3, 1600.0, 1)]


def test_packets_in_a_gps_dropout_are_resolved_as_unavailable(store):
    store.ingest("KE5GDB", [gps(0, 1000.0), gps(600, 5000.0)])
    store.ingest("KE5GDB", [sensor(1, 5), sensor(2, 300), sensor(3, 595)])

    resolved = altitudes(store)
    assert resolved[0][1] == 1000.0          # within 10 s of the first fix
    assert resolved[1][1] is None            # mid-dropout: no altitude
    assert resolved[1][2] == 1               # but decided, not left pending
    # t=595 sits inside the last fix's window, so it is not yet decidable: a
    # nearer fix could still arrive. It resolves on flush.
    assert resolved[2] == (3, None, 0)
    store.backfill_altitudes(1, flush=True)
    assert altitudes(store)[2] == (3, 5000.0, 1)


def test_flush_resolves_the_tail_of_a_flight(store):
    """Trailing packets have no GPS after them and would otherwise never resolve."""
    store.ingest("KE5GDB", [gps(0, 1000.0), gps(10, 1100.0), gps(20, 1200.0)])
    store.ingest("KE5GDB", [sensor(1, 5), sensor(2, 18)])

    # t=5 is decidable (GPS reaches 20, well past 5+10); t=18 is not.
    assert altitudes(store) == [(1, 1050.0, 1), (2, None, 0)]

    result = store.backfill_altitudes(1, flush=True)
    assert result["filled"] == 1
    assert altitudes(store)[1] == (2, 1180.0, 1)


def test_closing_a_flight_flushes_its_altitudes(store):
    store.ingest("KE5GDB", [gps(0, 1000.0), gps(10, 1100.0), sensor(1, 12)],
                 received_default=iso(T0))
    assert altitudes(store) == [(1, None, 0)]

    # Silence long enough to close the flight; its tail is resolved on the way out.
    store.ingest("KE5GDB", [sensor(9, 4000)],
                 received_default=iso(T0 + timedelta(minutes=31)))

    assert altitudes(store)[0] == (1, 1100.0, 1)


def test_backfill_is_idempotent(store):
    store.ingest("KE5GDB", [gps(0, 1000.0), gps(20, 2000.0)])
    store.ingest("KE5GDB", [sensor(1, 10)])

    first = altitudes(store)
    again = store.backfill_altitudes(1)

    assert again["resolved"] == 0
    assert altitudes(store) == first


def test_force_reconsiders_packets_written_off(store):
    """Covers a station delivering old GPS long after we gave up on a stretch."""
    store.ingest("KE5GDB", [gps(0, 1000.0), gps(600, 5000.0)])
    store.ingest("KE5GDB", [sensor(1, 300)])
    assert altitudes(store) == [(1, None, 1)]

    # The missing middle of the track turns up late.
    store.ingest("KE5GDB", [gps(295, 3000.0), gps(305, 3100.0)])
    assert altitudes(store) == [(1, None, 1)]  # already written off

    result = store.backfill_altitudes(1, force=True)
    assert result["filled"] == 1
    assert altitudes(store) == [(1, 3050.0, 1)]


def test_no_gps_at_all_leaves_everything_pending(store):
    store.ingest("KE5GDB", [sensor(1, 0), sensor(2, 1)])
    assert [r[2] for r in altitudes(store)] == [0, 0]
    assert store.backfill_altitudes(1) == {
        "resolved": 0, "filled": 0, "unavailable": 0, "pending": 0
    }


def test_custom_window_is_honoured(tmp_path):
    s = Store(tmp_path / "w.db", altitude_window=1.0)
    try:
        s.ingest("KE5GDB", [gps(0, 1000.0), gps(30, 2000.0)])
        s.ingest("KE5GDB", [sensor(1, 5)])
        # 5 s from the nearest fix, but the window is 1 s.
        assert altitudes(s) == [(1, None, 1)]
    finally:
        s.close()


def test_ingest_reports_how_many_altitudes_it_filled(store):
    store.ingest("KE5GDB", [gps(0, 1000.0), gps(20, 2000.0)])
    result = store.ingest("KE5GDB", [sensor(1, 10)])
    assert result.altitudes_filled == 1
