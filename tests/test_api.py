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


def test_callsign_path_serves_the_display(client):
    """`{server}/KE5GDB` is a link you can hand to the chase team."""
    resp = client.get("/KE5GDB")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # The same file as `/`; the filtering happens in the page.
    assert resp.text == client.get("/").text


def test_callsign_path_does_not_have_to_exist_yet(client):
    """The link goes out before the launch, so it must open and wait."""
    assert client.get("/W5NOTFLOWNYET").status_code == 200


@pytest.mark.parametrize("path", ["/favicon.ico", "/a", "/this-name-is-far-too-long-x"])
def test_non_callsign_paths_are_not_the_display(client, path):
    assert client.get(path).status_code == 404


def test_callsign_route_does_not_shadow_the_api(client):
    """It is declared last for this reason; a single segment could eat /events."""
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/flights").status_code == 200
    # Routed to the SSE endpoint rather than the display. Not read here: the
    # generator only ends on client disconnect, which TestClient cannot signal.
    route = next(r for r in main.app.routes if getattr(r, "path", None) == "/events")
    assert route.endpoint.__name__ == "events"


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


# -- admin ------------------------------------------------------------------

from app import admin  # noqa: E402


@pytest.fixture
def admin_on(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "hunter2")
    admin._attempts.clear()


def ingest_one(client, callsign="KE5GDB", count=12):
    client.post("/api/v1/ingest", json={"reporter_id": "KE5GDB", "packets": [
        {"kind": "sensor", "data": {**SENSOR, "count": count},
         "payload_callsign": callsign}]})


def test_admin_endpoints_are_refused_when_no_password_is_set(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", None)
    ingest_one(client)

    assert client.get("/api/v1/admin/session").json() == {
        "enabled": False, "authenticated": False}
    assert client.patch("/api/v1/flights/1", json={"name": "x"}).status_code == 403
    assert client.post("/api/v1/admin/login", json={"password": ""}).status_code == 403
    # The header is no way around it either: there is no password to match.
    resp = client.post("/api/v1/flights/1/close", headers={"X-Admin-Password": ""})
    assert resp.status_code == 403


@pytest.mark.parametrize("method, path", [
    ("patch", "/api/v1/flights/1"),
    ("post", "/api/v1/flights/1/close"),
    ("post", "/api/v1/flights/1/merge?from=2"),
    ("post", "/api/v1/flights/1/split?at=2026-07-26T18:22:00Z"),
    ("post", "/api/v1/flights/1/backfill"),
    ("patch", "/api/v1/fields/1"),
    ("get", "/api/v1/admin/flights"),
])
def test_every_write_endpoint_needs_a_login(client, admin_on, method, path):
    ingest_one(client)
    kwargs = {"json": {}} if method == "patch" else {}
    assert getattr(client, method)(path, **kwargs).status_code == 401
    assert getattr(client, method)(
        path, headers={"X-Admin-Password": "wrong"}, **kwargs).status_code == 401


def test_login_cookie_grants_admin_and_logout_revokes_it(client, admin_on):
    ingest_one(client)

    assert client.post("/api/v1/admin/login", json={"password": "nope"}).status_code == 401
    resp = client.post("/api/v1/admin/login", json={"password": "hunter2"})
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie

    assert client.get("/api/v1/admin/session").json()["authenticated"] is True
    assert client.patch("/api/v1/flights/1", json={"name": "RAB-9"}).json()["name"] == "RAB-9"

    client.post("/api/v1/admin/logout")
    assert client.get("/api/v1/admin/session").json()["authenticated"] is False
    assert client.patch("/api/v1/flights/1", json={"name": "x"}).status_code == 401


def test_session_tokens_cannot_be_forged_or_outlive_their_expiry(admin_on):
    token = admin.issue_token()
    assert admin.token_valid(token)

    expiry, _, sig = token.partition(".")
    assert not admin.token_valid(f"{int(expiry) + 3600}.{sig}")      # extended
    assert not admin.token_valid(f"{expiry}.{'0' * len(sig)}")       # forged
    assert not admin.token_valid(token, now=int(expiry) + 1)         # expired
    assert not admin.token_valid("garbage")


def test_changing_the_password_logs_everyone_out(admin_on, monkeypatch):
    token = admin.issue_token()
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "correct horse")
    assert not admin.token_valid(token)


def test_login_attempts_are_rate_limited(client, admin_on):
    codes = [client.post("/api/v1/admin/login", json={"password": "x"}).status_code
             for _ in range(admin.LOGIN_ATTEMPTS_PER_MINUTE + 1)]
    assert codes[:-1] == [401] * admin.LOGIN_ATTEMPTS_PER_MINUTE
    assert codes[-1] == 429


def test_hidden_flights_leave_the_public_list_but_not_the_admin_one(client, admin_on):
    ingest_one(client)
    auth = {"X-Admin-Password": "hunter2"}

    resp = client.patch("/api/v1/flights/1", json={"hidden": True}, headers=auth)
    assert resp.json()["hidden"] == 1
    assert resp.json()["ended_at"] is not None     # hiding an open flight closes it

    assert client.get("/api/v1/flights").json() == []
    assert [f["id"] for f in client.get("/api/v1/admin/flights", headers=auth).json()] == [1]
    # Still served by id: hiding tidies the list, it does not protect data.
    assert client.get("/api/v1/flights/1/sources").status_code == 200


def test_admin_list_points_out_twin_flights(client, admin_on):
    """Overlapping, and the callsigns do not disagree: one flight in two halves."""
    st = main.get_store()
    conn = st.pool.get()
    with conn:
        for name, call, start, end in [
            ("tagged", "K5RWK", "2026-10-24T13:00:00Z", "2026-10-24T16:00:00Z"),
            ("twin", None, "2026-10-24T13:30:00Z", "2026-10-24T16:10:00Z"),
            ("other balloon", "VK5ARG", "2026-10-24T14:00:00Z", "2026-10-24T15:00:00Z"),
            ("later", "K5RWK", "2026-10-25T13:00:00Z", "2026-10-25T14:00:00Z"),
            # A receiver's untagged first seconds: ends just before "tagged" starts.
            ("startup", None, "2026-10-24T12:59:30Z", "2026-10-24T12:59:55Z"),
        ]:
            conn.execute(
                "INSERT INTO flights(name, started_at, last_activity, ended_at, "
                "payload_callsign) VALUES (?,?,?,?,?)", (name, start, end, end, call))

    rows = client.get("/api/v1/admin/flights", headers={"X-Admin-Password": "hunter2"}).json()
    overlaps = {r["name"]: sorted(r["overlaps"]) for r in rows}
    ids = {r["name"]: r["id"] for r in rows}
    assert overlaps["tagged"] == sorted([ids["twin"], ids["startup"]])
    assert overlaps["startup"] == [ids["tagged"]]
    # The untagged twin overlaps VK5ARG too, and could be half of either.
    assert overlaps["twin"] == sorted([ids["tagged"], ids["other balloon"]])
    assert overlaps["later"] == []


def test_admin_page_is_served_at_a_path_nginx_will_not_take_for_a_callsign(client):
    assert client.get("/admin", follow_redirects=False).headers["location"] == "/admin/"
    page = client.get("/admin/")
    assert page.status_code == 200
    assert "/static/js/admin.js" in page.text
