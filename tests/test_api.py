"""Tests for the HTTP ingest endpoint, including forwarder compatibility."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, main  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    monkeypatch.setattr(config, "INGEST_API_KEY", None)
    with TestClient(main.app) as c:
        yield c


SENSOR = {
    "time": "20260726T182204.512Z",
    "id": "RAB_HAT_ENV",
    "count": 12,
    "v_in": 4.02,
    "bmp_temp": -54.2,
}


def test_ingest_batch(client):
    resp = client.post(
        "/api/v1/ingest",
        json={"reporter_id": "KE5GDB", "packets": [{"kind": "sensor", "data": SENSOR}]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 1
    assert body["new_sources"] == ["RAB_HAT_ENV"]


def test_dedup_across_two_stations_over_http(client):
    body = {"packets": [{"kind": "sensor", "data": SENSOR}]}
    client.post("/api/v1/ingest", json={**body, "reporter_id": "KE5GDB"})
    resp = client.post("/api/v1/ingest", json={**body, "reporter_id": "K5NBC"})

    assert resp.json() == {**resp.json(), "accepted": 0, "duplicates": 1}
    assert client.get("/api/v1/health").json()["stats"]["receptions"] == 2


def test_legacy_forwarder_body_is_accepted(client):
    """The unmodified wenet_forwarder sends a bare list with reporter_id inline.

    Accepting it means the cutover can be tested by changing only its URL.
    """
    resp = client.post(
        "/api/v1/ingest",
        json=[
            {**SENSOR, "reporter_id": "KE5GDB"},
            {**SENSOR, "count": 13, "reporter_id": "KE5GDB"},
        ],
    )

    assert resp.status_code == 200
    assert resp.json()["accepted"] == 2
    assert client.get("/api/v1/flights").json()[0]["packet_count"] == 2


def test_legacy_body_routes_gps_by_shape(client):
    client.post(
        "/api/v1/ingest",
        json=[{"timestamp": "20260726T182204.000Z", "latitude": 32.9,
               "longitude": -97.2, "altitude": 18422.0, "reporter_id": "KE5GDB"}],
    )
    assert client.get("/api/v1/health").json()["stats"]["gps"] == 1


def test_payload_callsign_is_accepted_and_keys_the_flight(client):
    """The wire shape the receivers now send."""
    resp = client.post("/api/v1/ingest", json={"reporter_id": "KE5GDB", "packets": [
        {"kind": "sensor", "data": SENSOR, "payload_callsign": "KE5GDB"},
        {"kind": "sensor", "data": {**SENSOR, "id": "OTHER_ENV"},
         "payload_callsign": "W5XYZ"},
    ]})

    assert resp.status_code == 200
    assert resp.json()["accepted"] == 2
    # One POST, two transmitters, two flights -- batching is by time, so a batch
    # spanning two payloads is normal rather than an error.
    flights = client.get("/api/v1/flights").json()
    assert len(flights) == 2
    assert {f["packet_count"] for f in flights} == {1}


def test_batch_without_callsign_still_ingests(client):
    """Old receiver firmware, and spool files written before the field existed.

    These drain hours later and must not be rejected: a 4xx here would grow a
    spool on every ground station rather than lose data visibly.
    """
    resp = client.post("/api/v1/ingest", json={
        "reporter_id": "KE5GDB",
        "packets": [{"kind": "sensor", "data": SENSOR}],
    })

    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_callsign_is_treated_as_absent(client, blank):
    """A blank must not become a callsign that a flight is keyed on."""
    client.post("/api/v1/ingest", json={"reporter_id": "KE5GDB", "packets": [
        {"kind": "sensor", "data": SENSOR, "payload_callsign": blank},
    ]})
    client.post("/api/v1/ingest", json={"reporter_id": "KE5GDB", "packets": [
        {"kind": "sensor", "data": {**SENSOR, "count": 13}},
    ]})

    # Both land in the one untagged flight rather than splitting in two.
    assert len(client.get("/api/v1/flights").json()) == 1


def test_legacy_list_body_carries_the_callsign(client):
    """The flat-list shape gets it too, and it does not become a sensor field."""
    client.post("/api/v1/ingest", json=[
        {**SENSOR, "reporter_id": "KE5GDB", "payload_callsign": "KE5GDB"},
    ])

    flight = client.get("/api/v1/flights").json()[0]
    assert flight["name"].endswith(" - KE5GDB #1")
    # And as its own field, which is what the display reads: a flight renamed
    # by hand must not lose the transmitter it belongs to.
    assert flight["payload_callsign"] == "KE5GDB"
    sources = client.get(f"/api/v1/flights/{flight['id']}/sources").json()
    names = {f["name"] for f in sources[0]["fields"]}
    assert "payload_callsign" not in names


def test_live_push_is_split_per_flight(client, monkeypatch):
    """A mixed batch must not hand one transmitter's track to the other's tab.

    `/events?flight=N` filters on the event's flight_id, so a single event for
    a two-transmitter batch would have to claim one flight and smuggle in the
    other's GPS -- which the display appends straight onto the map.
    """
    published = []
    monkeypatch.setattr(main.hub, "publish", published.append)

    client.post("/api/v1/ingest", json={"reporter_id": "KE5GDB", "packets": [
        {"kind": "sensor", "data": SENSOR, "payload_callsign": "KE5GDB"},
        {"kind": "gps", "payload_callsign": "KE5GDB",
         "data": {"timestamp": "20260726T182204.000Z", "latitude": 32.9,
                  "longitude": -97.2, "altitude": 18422.0}},
        {"kind": "gps", "payload_callsign": "W5XYZ",
         "data": {"timestamp": "20260726T182204.000Z", "latitude": 30.1,
                  "longitude": -97.7, "altitude": 900.0}},
    ]})

    assert len(published) == 2
    by_flight = {e["flight_id"]: e for e in published}
    assert len(by_flight) == 2
    for event in published:
        assert len(event["gps"]) == 1
        # The routing tag is stripped before it reaches a browser.
        assert "flight_id" not in event["gps"][0]
        for packet in event["sources"].values():
            assert "flight_id" not in packet

    mine = next(e for e in published if e["sources"])
    assert mine["gps"][0]["alt"] == 18422.0


def test_packet_without_raw_or_data_is_a_validation_error(client):
    resp = client.post("/api/v1/ingest", json={"packets": [{"kind": "sensor"}]})
    assert resp.status_code == 422


def test_sources_endpoint_reports_discovered_fields(client):
    client.post(
        "/api/v1/ingest",
        json={"reporter_id": "KE5GDB", "packets": [{"kind": "sensor", "data": SENSOR}]},
    )
    flight_id = client.get("/api/v1/flights").json()[0]["id"]
    sources = client.get(f"/api/v1/flights/{flight_id}/sources").json()

    assert len(sources) == 1
    assert sources[0]["payload_name"] == "RAB_HAT"
    assert sources[0]["task_name"] == "ENV"
    fields = {f["name"]: f for f in sources[0]["fields"]}
    assert fields["bmp_temp"]["unit"] == "°C"


def test_api_key_is_enforced_when_configured(client, monkeypatch):
    monkeypatch.setattr(config, "INGEST_API_KEY", "s3cret")
    body = {"reporter_id": "KE5GDB", "packets": [{"kind": "sensor", "data": SENSOR}]}

    assert client.post("/api/v1/ingest", json=body).status_code == 401
    assert client.post(
        "/api/v1/ingest", json=body, headers={"X-API-KEY": "s3cret"}
    ).status_code == 200
