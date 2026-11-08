"""Tests for the read API: series, track, latest, field metadata, SSE."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, main  # noqa: E402
from app.store import Store  # noqa: E402
from app.timeutil import iso  # noqa: E402

T0 = datetime(2026, 7, 26, 18, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "read.db")
    monkeypatch.setattr(config, "INGEST_API_KEY", None)
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def admin_client(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "hunter2")
    client.headers["X-Admin-Password"] = "hunter2"
    return client


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "read.db", altitude_window=10.0)
    yield s
    s.close()


def sensor(count, offset_s, source="RAB_HAT_ENV", **fields):
    data = {"time": iso(T0 + timedelta(seconds=offset_s)), "id": source, "count": count}
    data.update(fields)
    return {"kind": "sensor", "data": data}


def gps(offset_s, altitude, lat=32.9, lon=-97.2):
    return {"kind": "gps", "data": {
        "timestamp": iso(T0 + timedelta(seconds=offset_s)),
        "latitude": lat, "longitude": lon, "altitude": altitude, "ascent_rate": 5.0}}


def populate(store, n=200):
    """A short climb with a sensor sweeping a known range."""
    batch = [gps(i, 200.0 + 5.0 * i) for i in range(0, n, 2)]
    batch += [sensor(i, i, bmp_temp=15.0 - i * 0.05, bmp_pres=1013.0 - i) for i in range(n)]
    store.ingest("KE5GDB", batch)
    store.backfill_altitudes(1, flush=True)


# -- series ---------------------------------------------------------------


def test_series_returns_raw_points_when_under_the_limit(store):
    populate(store, 50)
    (s,) = store.series(1, ["bmp_temp"], max_points=2000)

    assert s["name"] == "bmp_temp"
    assert s["unit"] == "°C"
    assert s["source"] == "RAB_HAT_ENV"
    assert s["downsampled"] is False
    assert len(s["x"]) == 50
    assert s["min"] == s["max"] == s["avg"]


def test_series_downsamples_and_preserves_extremes(store):
    """A decimating sampler would step over a spike; min/max bucketing cannot."""
    store.ingest("KE5GDB", [sensor(i, i, bmp_temp=0.0) for i in range(500)])
    store.ingest("KE5GDB", [sensor(999, 250.5, bmp_temp=99.0)])  # lone spike

    (s,) = store.series(1, ["bmp_temp"], max_points=20)

    assert s["downsampled"] is True
    assert len(s["x"]) <= 21
    assert max(s["max"]) == 99.0        # the spike survives
    assert s["n"] == 501


def test_series_respects_a_time_window(store):
    populate(store, 100)
    (s,) = store.series(
        1, ["bmp_temp"],
        start=iso(T0 + timedelta(seconds=20)),
        end=iso(T0 + timedelta(seconds=40)),
        max_points=2000,
    )
    assert s["n"] == 21


def test_series_by_altitude(store):
    """The sounding view: sensor against height rather than time."""
    populate(store, 200)
    (s,) = store.series(1, ["bmp_pres"], axis="altitude", max_points=20)

    assert s["axis"] == "altitude"
    assert len(s["x"]) > 1
    assert s["x"] == sorted(s["x"])          # ascending altitude
    assert s["avg"] == sorted(s["avg"], reverse=True)  # pressure falls with height


def test_altitude_series_honours_a_time_window(store):
    """Narrowing to the ascent is how you read a sounding without the descent
    drawn back over it."""
    populate(store, 200)
    full = store.series(1, ["bmp_pres"], axis="altitude", max_points=100)[0]
    windowed = store.series(
        1, ["bmp_pres"], axis="altitude", max_points=100,
        start=iso(T0), end=iso(T0 + timedelta(seconds=50)),
    )[0]

    assert windowed["n"] < full["n"]
    assert max(windowed["x"]) < max(full["x"])


def test_track_honours_a_time_window(store):
    populate(store, 200)
    assert store.track(1)["n"] == 100
    windowed = store.track(1, start=iso(T0), end=iso(T0 + timedelta(seconds=40)))

    assert windowed["n"] == 21
    assert max(windowed["alt"]) < 200.0 + 5.0 * 199


def test_track_window_with_nothing_in_it(store):
    populate(store, 50)
    empty = store.track(1, start=iso(T0 + timedelta(days=1)),
                        end=iso(T0 + timedelta(days=2)))
    assert empty["n"] == 0
    assert empty["lat"] == [] and empty["stride"] == 1


def test_series_across_sources_with_a_bare_name(store):
    """`temp` legitimately exists on several payloads; a bare name gets them all."""
    store.ingest("KE5GDB", [
        sensor(1, 0, source="RAB_HAT_ENV", bmp_temp=1.0),
        sensor(1, 0, source="NECK_ENV", bmp_temp=2.0),
    ])
    out = store.series(1, ["bmp_temp"], max_points=100)

    assert {s["source"] for s in out} == {"RAB_HAT_ENV", "NECK_ENV"}


def test_series_qualified_by_source(store):
    store.ingest("KE5GDB", [
        sensor(1, 0, source="RAB_HAT_ENV", bmp_temp=1.0),
        sensor(1, 0, source="NECK_ENV", bmp_temp=2.0),
    ])
    out = store.series(1, ["NECK_ENV:bmp_temp"], max_points=100)

    assert len(out) == 1
    assert out[0]["source"] == "NECK_ENV"


def test_series_ignores_text_fields(store):
    store.ingest("KE5GDB", [sensor(1, 0, payload="XBnd RPT", bmp_temp=1.0)])
    assert store.series(1, ["payload"], max_points=100) == []


def test_series_for_unknown_field_is_empty_not_an_error(store):
    populate(store, 10)
    assert store.series(1, ["no_such_sensor"], max_points=100) == []


# -- track and latest -----------------------------------------------------


def test_track_returns_the_polyline(store):
    populate(store, 100)
    t = store.track(1)

    assert t["n"] == 50
    assert len(t["lat"]) == len(t["lon"]) == len(t["alt"]) == 50
    assert t["alt"] == sorted(t["alt"])


def test_track_thins_a_long_flight(store):
    store.ingest("KE5GDB", [gps(i, 200.0 + i) for i in range(400)])
    t = store.track(1, max_points=50)

    assert t["n"] == 400
    assert t["stride"] == 8
    assert len(t["lat"]) == 50


# -- reported rate --------------------------------------------------------


def rate_of(store, source_key="RAB_HAT_ENV"):
    return next(s["rate_hz"] for s in store.latest(1)["sources"]
                if s["source_key"] == source_key)


@pytest.mark.parametrize("interval, expected", [
    (0.5, 2.0),      # sensors
    (1.0, 1.0),      # GPS
    (1.25, 0.8),     # the X-RPT rate seen in the field
])
def test_rate_matches_the_packet_interval(store, interval, expected):
    store.ingest("KE5GDB", [
        sensor(i, i * interval, v_in=4.0) for i in range(30)
    ])
    assert rate_of(store) == pytest.approx(expected, rel=0.01)


def test_a_slow_source_is_measured_accurately(store):
    """What the fixed 60 s window could not do.

    At one packet every 20 s it saw three of them, so the answer could only be a
    multiple of 1/60 -- 0.05 Hz reported as 0.033 or 0.067. Measuring over a
    fixed number of packets stretches the window instead.
    """
    store.ingest("KE5GDB", [sensor(i, i * 20.0, v_in=4.0) for i in range(25)])
    assert rate_of(store) == pytest.approx(0.05, rel=0.01)


def test_rate_is_measured_over_the_newest_packets_only(store):
    """A source that speeds up should read fast, not the average of its life."""
    slow = [sensor(i, i * 10.0, v_in=4.0) for i in range(30)]          # 0.1 Hz
    fast = [sensor(30 + i, 300.0 + i * 0.5, v_in=4.0) for i in range(30)]  # 2 Hz
    store.ingest("KE5GDB", slow + fast)

    assert rate_of(store) == pytest.approx(2.0, rel=0.05)


def test_lost_packets_lower_the_reported_rate(store):
    """The figure is the rate packets are *arriving*, not the payload's nominal
    rate -- half of them going missing is the thing worth seeing."""
    every_other = [sensor(i, i * 0.5, v_in=4.0) for i in range(60) if i % 2 == 0]
    store.ingest("KE5GDB", every_other)

    assert rate_of(store) == pytest.approx(1.0, rel=0.01)


def test_a_single_packet_has_no_rate(store):
    store.ingest("KE5GDB", [sensor(1, 0, v_in=4.0)])
    assert rate_of(store) == 0.0


def test_packets_sharing_a_timestamp_do_not_divide_by_zero(store):
    """Legal: uniqueness is (source, time, count), and a truncated packet loses
    its count while a coarse payload clock repeats a second."""
    store.ingest("KE5GDB", [sensor(i, 0.0, v_in=4.0) for i in range(5)])
    assert rate_of(store) == 0.0


def test_rate_is_measured_on_the_payload_clock(store):
    """A payload with no PCF8523 restarts its RTC at 2021-01-01 on every power
    cycle. Measuring against the server's clock would read zero forever on a
    source that is transmitting perfectly well."""
    stale = datetime(2021, 1, 1, tzinfo=timezone.utc)
    store.ingest("KE5GDB", [
        {"kind": "sensor", "data": {
            "time": iso(stale + timedelta(seconds=i * 0.5)),
            "id": "RAB_HAT_ENV", "count": i, "v_in": 4.0,
        }} for i in range(30)
    ])
    assert rate_of(store) == pytest.approx(2.0, rel=0.01)


def test_latest_reports_current_state(store):
    populate(store, 100)
    out = store.latest(1)

    assert out["gps"]["altitude"] == pytest.approx(200.0 + 5.0 * 98)
    assert out["peak_altitude"] == pytest.approx(200.0 + 5.0 * 98)

    by_key = {s["source_key"]: s for s in out["sources"]}
    src = by_key["RAB_HAT_ENV"]
    assert src["count"] == 99
    assert src["data"]["bmp_temp"] == pytest.approx(15.0 - 99 * 0.05)
    assert src["altitude"] is not None
    assert src["rate_hz"] == pytest.approx(1.0, abs=0.05)

    # The transmitter's own telemetry reports as a source alongside it, so the
    # display can show battery and CPU temperature on the same footing as the
    # BLE sensors rather than burying them in the map layer.
    wenet = by_key["WENET_GPS"]
    assert wenet["data"]["altitude"] == pytest.approx(200.0 + 5.0 * 98)


def test_reporters_counts_gps_receptions_too(store):
    """A station forwarding mostly GPS was reading as nearly idle."""
    store.ingest("KE5GDB", [sensor(1, 0, bmp_temp=1.0), gps(0, 500.0), gps(1, 505.0)])
    store.ingest("K5NBC", [gps(2, 510.0)])          # GPS only

    by_call = {r["reporter_id"]: r for r in store.reporters(1)}

    assert by_call["KE5GDB"]["packets"] == 3        # 1 sensor + 2 gps
    assert by_call["KE5GDB"]["sensor"] == 1
    assert by_call["KE5GDB"]["gps"] == 2
    # Would have been absent entirely before: it forwarded no sensor packets.
    assert by_call["K5NBC"]["packets"] == 1
    assert by_call["K5NBC"]["sensor"] == 0
    assert by_call["K5NBC"]["gps"] == 1


def test_reporters_span_covers_both_reception_tables(store):
    store.ingest("KE5GDB", [sensor(1, 0, bmp_temp=1.0)], received_default=iso(T0))
    store.ingest("KE5GDB", [gps(0, 500.0)],
                 received_default=iso(T0 + timedelta(minutes=5)))

    (row,) = store.reporters(1)
    assert row["first"] == iso(T0)
    assert row["last"] == iso(T0 + timedelta(minutes=5))


def test_latest_reports_reception_time_not_just_payload_time(store):
    """Staleness must not be judged on the payload's RTC.

    An unset RTC restarts at 2021-01-01 on every boot, so a perfectly healthy
    source would read as years stale while its values kept updating.
    """
    unset_rtc = {
        "kind": "sensor",
        "data": {"time": "2021-01-01T00:00:03.000000Z", "id": "RAB_HAT_ENV",
                 "count": 3, "bmp_temp": -40.0},
    }
    store.ingest("KE5GDB", [unset_rtc], received_default=iso(T0))

    (src,) = store.latest(1)["sources"]

    # The payload's own clock is still reported, unaltered...
    assert src["time"].startswith("2021-01-01")
    # ...but the reception clock is what staleness can be measured against.
    assert src["heard_epoch"] == pytest.approx(T0.timestamp())


def test_latest_exposes_server_clock_for_skew_correction(store):
    populate(store, 10)
    out = store.latest(1)

    assert out["server_now"] == pytest.approx(datetime.now(timezone.utc).timestamp(), abs=30)
    # The gap the browser must correct for, on the data above, is years.
    assert out["server_now"] - out["sources"][0]["heard_epoch"] < 30


def test_heard_epoch_tracks_the_newest_packet(store):
    store.ingest("KE5GDB", [sensor(1, 0, bmp_temp=1.0)], received_default=iso(T0))
    store.ingest("KE5GDB", [sensor(2, 1, bmp_temp=2.0)],
                 received_default=iso(T0 + timedelta(seconds=45)))

    (src,) = store.latest(1)["sources"]
    assert src["heard_epoch"] == pytest.approx((T0 + timedelta(seconds=45)).timestamp())


def test_latest_with_no_gps_yet(store):
    store.ingest("KE5GDB", [sensor(1, 0, bmp_temp=1.0)])
    out = store.latest(1)

    assert out["gps"] is None
    assert out["sources"][0]["altitude"] is None


# -- field metadata -------------------------------------------------------


def test_field_metadata_can_be_set(admin_client):
    client = admin_client
    client.post("/api/v1/ingest", json={"reporter_id": "KE5GDB", "packets": [
        sensor(1, 0, mystery_sensor=42.0)]})
    sources = client.get("/api/v1/flights/1/sources").json()
    field = next(f for f in sources[0]["fields"] if f["name"] == "mystery_sensor")
    assert field["unit"] == ""

    resp = client.patch(f"/api/v1/fields/{field['id']}",
                        json={"unit": "ppm", "label": "CO2"})

    assert resp.status_code == 200
    assert resp.json()["unit"] == "ppm"
    assert resp.json()["user_edited"] == 1


def test_patching_an_unknown_field_is_404(admin_client):
    assert admin_client.patch("/api/v1/fields/999", json={"unit": "x"}).status_code == 404


# -- HTTP surface ---------------------------------------------------------


def test_series_endpoint(client):
    client.post("/api/v1/ingest", json={"reporter_id": "KE5GDB", "packets": [
        sensor(i, i, bmp_temp=float(i)) for i in range(30)]})

    resp = client.get("/api/v1/flights/1/series", params={"field": "bmp_temp"})

    assert resp.status_code == 200
    (s,) = resp.json()
    assert len(s["x"]) == 30


def test_series_endpoint_rejects_a_bad_axis(client):
    resp = client.get("/api/v1/flights/1/series",
                      params={"field": "bmp_temp", "axis": "sideways"})
    assert resp.status_code == 422


def test_latest_and_track_endpoints(client):
    client.post("/api/v1/ingest", json={"reporter_id": "KE5GDB", "packets": [
        gps(0, 1000.0), gps(10, 1100.0), sensor(1, 5, bmp_temp=1.0)]})

    assert client.get("/api/v1/flights/1/latest").json()["gps"]["altitude"] == 1100.0
    assert client.get("/api/v1/flights/1/track").json()["n"] == 2


# -- SSE ------------------------------------------------------------------


# The SSE endpoint itself is exercised against a real server (see the e2e run in
# the README) rather than TestClient: TestClient drives requests and the open
# stream through one portal, so a POST nested inside a live stream deadlocks.
# What is worth pinning here is the part with the logic in it -- that ingest
# publishes a correctly shaped event, and that a stalled subscriber is dropped
# rather than allowed to block ingest.


def test_ingest_publishes_a_live_update(client):
    """The live path: a packet arriving must reach a connected browser."""
    queue = main.hub.subscribe()
    try:
        client.post("/api/v1/ingest", json={"reporter_id": "KE5GDB", "packets": [
            sensor(7, 0, bmp_temp=-54.2), gps(0, 18422.0)]})
        event = queue.get_nowait()
    finally:
        main.hub.unsubscribe(queue)

    assert event["type"] == "update"
    assert event["flight_id"] == 1
    assert event["reporter_id"] == "KE5GDB"
    assert event["sources"]["RAB_HAT_ENV"]["data"]["bmp_temp"] == -54.2
    assert event["sources"]["RAB_HAT_ENV"]["count"] == 7
    assert event["gps"][0]["alt"] == 18422.0
    # The GPS telemetry source is discovered by the same batch.
    assert event["new_sources"] == ["RAB_HAT_ENV", "WENET_GPS"]
    assert event["sources"]["WENET_GPS"]["data"]["altitude"] == 18422.0


def test_live_update_carries_only_the_newest_sample_per_source(client):
    """A batch may hold thirty samples; the tiles only show the latest one."""
    queue = main.hub.subscribe()
    try:
        client.post("/api/v1/ingest", json={"reporter_id": "KE5GDB", "packets": [
            sensor(i, i, bmp_temp=float(i)) for i in range(10)]})
        event = queue.get_nowait()
    finally:
        main.hub.unsubscribe(queue)

    assert list(event["sources"]) == ["RAB_HAT_ENV"]
    assert event["sources"]["RAB_HAT_ENV"]["count"] == 9


def test_event_is_json_serialisable(client):
    """It is about to be written into an SSE frame, so it has to survive dumps."""
    queue = main.hub.subscribe()
    try:
        client.post("/api/v1/ingest", json={"reporter_id": "KE5GDB", "packets": [
            sensor(1, 0, bmp_temp=1.0, payload="XBnd RPT"), gps(0, 500.0)]})
        event = queue.get_nowait()
    finally:
        main.hub.unsubscribe(queue)

    assert json.loads(json.dumps(event, default=str))["type"] == "update"


# Note: the /events endpoint is deliberately not exercised through TestClient at
# all. Its generator only ends on client disconnect, which TestClient cannot
# signal cleanly, so the request hangs the suite. Headers and frame format are
# verified against a real uvicorn server instead -- see the SSE section of the
# README.


def test_slow_subscriber_never_blocks_ingest():
    """A browser that stops reading must lose events, not stall the server."""
    from app.hub import Hub

    hub = Hub(queue_size=4)
    queue = hub.subscribe()
    for i in range(20):
        hub.publish({"type": "update", "i": i})

    assert queue.qsize() == 4
    assert hub.dropped == 16
    # What survives is the newest, which is what a live display wants.
    assert queue.get_nowait()["i"] == 16
