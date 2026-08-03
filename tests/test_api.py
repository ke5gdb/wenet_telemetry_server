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
