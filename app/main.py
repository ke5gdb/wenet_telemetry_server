"""FastAPI application: ingest, the read API, live SSE, export, and the display."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import admin, config, export
from .hub import Hub
from .models import IngestBatch, IngestPacket
from .store import Store
from .timeutil import to_epoch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("wenet.server")

store: Store | None = None
hub = Hub()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store
    store = Store(
        config.DB_PATH,
        flight_gap_minutes=config.FLIGHT_GAP_MINUTES,
        altitude_window=config.ALTITUDE_WINDOW_SECONDS,
    )
    hub.bind(asyncio.get_running_loop())
    log.info("database at %s", config.DB_PATH.resolve())
    if config.INGEST_API_KEY:
        log.info("ingest requires X-API-KEY")
    else:
        log.warning("ingest is UNAUTHENTICATED (set WENET_INGEST_API_KEY to require a key)")
    if not admin.enabled():
        log.warning("admin is disabled (set WENET_ADMIN_PASSWORD to enable it)")
    try:
        yield
    finally:
        store.close()


app = FastAPI(title="Wenet Telemetry Server", version="0.1.0", lifespan=lifespan)


def get_store() -> Store:
    if store is None:  # pragma: no cover - only before startup
        raise HTTPException(503, "starting up")
    return store


# -- rate limiting --------------------------------------------------------

_hits: dict[str, deque[float]] = defaultdict(deque)


def rate_limit(request: Request) -> None:
    limit = config.RATE_LIMIT_PER_MINUTE
    if limit <= 0:
        return
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    window = _hits[client]
    while window and now - window[0] > 60.0:
        window.popleft()
    if len(window) >= limit:
        raise HTTPException(429, "rate limit exceeded")
    window.append(now)


def check_key(x_api_key: str | None = Header(default=None)) -> None:
    if config.INGEST_API_KEY and x_api_key != config.INGEST_API_KEY:
        raise HTTPException(401, "invalid or missing X-API-KEY")


# -- ingest ---------------------------------------------------------------


@app.post("/api/v1/ingest", dependencies=[Depends(rate_limit), Depends(check_key)])
async def ingest(request: Request):
    """Accept a batch of packets from a ground station.

    Two body shapes are accepted. The documented one is an IngestBatch. A bare
    JSON list is also taken, because that is what the current unmodified
    wenet_forwarder sends -- so it can be pointed here by changing only its URL,
    which makes the cutover testable before the forwarder is touched.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body is not valid JSON")

    # The body is validated by hand rather than via a signature annotation so
    # that both shapes can be accepted, which means the ValidationError has to
    # be translated here -- otherwise a malformed batch surfaces to the ground
    # station as an opaque 500 instead of something it can log and act on.
    try:
        if isinstance(body, list):
            batch = _batch_from_legacy(body)
        elif isinstance(body, dict):
            batch = IngestBatch.model_validate(body)
        else:
            raise HTTPException(400, "body must be an object or a list")
    except ValidationError as exc:
        # `ctx` holds live exception objects and `input` echoes the untrusted
        # body back; neither is JSON-serialisable nor useful to the caller.
        raise HTTPException(
            422,
            exc.errors(include_url=False, include_context=False, include_input=False),
        )

    if len(batch.packets) > config.MAX_BATCH_PACKETS:
        raise HTTPException(413, f"batch exceeds {config.MAX_BATCH_PACKETS} packets")

    st = get_store()
    items = [p.model_dump() for p in batch.packets]
    # SQLite work is synchronous; keep it off the event loop so ingest from one
    # station cannot stall reads for everyone else.
    result = await asyncio.to_thread(st.ingest, batch.reporter_id, items)

    if result.new_sources:
        log.info("new sources: %s", ", ".join(result.new_sources))

    # One event per flight. A batch is a time window, so it can carry two
    # transmitters at once -- and a browser watching one of them must not be
    # handed the other's track. `/events?flight=N` filters on `flight_id`, so a
    # single mixed event would have to claim one flight and smuggle in the rest.
    by_flight: dict[int, dict] = {}
    for source_key, packet in result.latest.items():
        entry = by_flight.setdefault(packet["flight_id"], {"sources": {}, "gps": []})
        # Stripped on the way out: the tag exists to route the event, and the
        # browser already knows which flight it asked for.
        entry["sources"][source_key] = {k: v for k, v in packet.items() if k != "flight_id"}
    for point in result.gps_points:
        entry = by_flight.setdefault(point["flight_id"], {"sources": {}, "gps": []})
        entry["gps"].append({k: v for k, v in point.items() if k != "flight_id"})

    for flight_id, entry in by_flight.items():
        hub.publish({
            "type": "update",
            "flight_id": flight_id,
            "reporter_id": batch.reporter_id,
            "sources": entry["sources"],
            "gps": entry["gps"],
            "new_sources": result.new_sources,
            "new_fields": result.new_fields,
        })
    return result.as_dict()


def _batch_from_legacy(body: list) -> IngestBatch:
    """Adapt the current forwarder's payload: a flat list of decoded dicts."""
    reporter = "UNKNOWN"
    packets: list[IngestPacket] = []
    for entry in body:
        if not isinstance(entry, dict):
            continue
        data = dict(entry)
        reporter = data.pop("reporter_id", None) or reporter
        # Lifted out of `data` rather than left in it: it is provenance from the
        # receiver, not something the payload transmitted, and leaving it inline
        # would discover it as a text field on every source.
        callsign = data.pop("payload_callsign", None)
        kind = "gps" if ("latitude" in data or "longitude" in data) else "sensor"
        packets.append(IngestPacket(kind=kind, data=data, payload_callsign=callsign))
    return IngestBatch(reporter_id=reporter, packets=packets)


# -- the display ----------------------------------------------------------
#
# In production nginx serves these directly and never troubles uvicorn; this
# mount exists so the whole thing runs from one process on a bench.

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    # A directory rather than `/admin`, because a single segment is a callsign
    # to nginx: `/admin` would be served the display, filtered to "ADMIN".
    @app.get("/admin", include_in_schema=False)
    async def admin_redirect():
        return RedirectResponse("/admin/", status_code=301)

    @app.get("/admin/", include_in_schema=False)
    async def admin_page():
        return FileResponse(STATIC_DIR / "admin" / "index.html")


@app.get("/api/v1/health")
async def health():
    return {
        "status": "ok",
        "stats": get_store().stats(),
        "subscribers": hub.subscriber_count,
        "dropped_events": hub.dropped,
    }


@app.get("/api/v1/flights")
async def flights():
    return get_store().flights()


@app.get("/api/v1/flights/{flight_id}/sources")
async def sources(flight_id: int):
    return get_store().sources(flight_id)


@app.get("/api/v1/flights/{flight_id}/reporters")
async def reporters(flight_id: int):
    return get_store().reporters(flight_id)


@app.get("/api/v1/flights/{flight_id}/series")
async def series(
    flight_id: int,
    field: list[str] = Query(..., description="field id, source:name, or bare name"),
    start: str | None = None,
    end: str | None = None,
    max_points: int = Query(2000, ge=10, le=50000),
    axis: str = Query("time", pattern="^(time|altitude)$"),
):
    """Downsampled values for one or more fields.

    `axis=altitude` plots against height instead of time -- the sounding view,
    and the reason GPS is ingested at all.
    """
    return await asyncio.to_thread(
        get_store().series, flight_id, field, start, end, max_points, axis
    )


@app.get("/api/v1/flights/{flight_id}/track")
async def track(
    flight_id: int,
    max_points: int = Query(5000, ge=10, le=100000),
    start: str | None = None,
    end: str | None = None,
):
    return await asyncio.to_thread(get_store().track, flight_id, max_points, start, end)


@app.get("/api/v1/flights/{flight_id}/latest")
async def latest(flight_id: int):
    """Newest sample per source -- what the live tiles render."""
    return await asyncio.to_thread(get_store().latest, flight_id)


class FieldUpdate(BaseModel):
    unit: str | None = None
    label: str | None = None


@app.patch("/api/v1/fields/{field_id}", dependencies=[Depends(admin.require_admin)])
async def patch_field(field_id: int, update: FieldUpdate):
    """Name or unit a field the server had no seed metadata for."""
    row = await asyncio.to_thread(
        get_store().update_field, field_id, update.unit, update.label
    )
    if row is None:
        raise HTTPException(404, "no such field")
    return row


# -- export ---------------------------------------------------------------


def _download(generator, filename: str, media_type: str) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _slug(flight_id: int) -> str:
    return export.flight_slug(get_store().pool.get(), flight_id)


def _require_flight(flight_id: int) -> None:
    row = get_store().pool.get().execute(
        "SELECT 1 FROM flights WHERE id=?", (flight_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"no flight {flight_id}")


@app.get("/api/v1/flights/{flight_id}/export.csv")
async def export_csv(flight_id: int, source: str | None = None):
    """Wide CSV for one source, or long-format CSV across the whole flight.

    Long by default because sources share neither a field set nor a rate, so a
    wide table across all of them would be mostly empty cells.
    """
    st = get_store()
    # Validate up front: the generators are lazy, so an unknown flight or source
    # would otherwise surface as a truncated 200 rather than a 404.
    _require_flight(flight_id)
    if source:
        exists = st.pool.get().execute(
            "SELECT 1 FROM sources WHERE flight_id=? AND source_key=?", (flight_id, source)
        ).fetchone()
        if exists is None:
            raise HTTPException(404, f"no source {source!r} in flight {flight_id}")
        return _download(export.csv_wide(st.pool.path, flight_id, source),
                         f"{_slug(flight_id)}-{source}.csv", "text/csv")

    return _download(export.csv_long(st.pool.path, flight_id),
                     f"{_slug(flight_id)}-readings.csv", "text/csv")


@app.get("/api/v1/flights/{flight_id}/gps.csv")
async def export_gps(flight_id: int):
    _require_flight(flight_id)
    st = get_store()
    return _download(export.csv_gps(st.pool.path, flight_id),
                     f"{_slug(flight_id)}-gps.csv", "text/csv")


@app.get("/api/v1/flights/{flight_id}/export.jsonl")
async def export_jsonl(flight_id: int):
    """One decoded packet per line, matching the payload-side analysis logs."""
    _require_flight(flight_id)
    st = get_store()
    return _download(export.jsonl(st.pool.path, flight_id),
                     f"{_slug(flight_id)}.jsonl", "application/x-ndjson")


@app.get("/api/v1/flights/{flight_id}/export.cbor")
async def export_cbor(flight_id: int):
    """Length-prefixed raw packets -- the lossless archive."""
    _require_flight(flight_id)
    st = get_store()
    return _download(export.raw_cbor(st.pool.path, flight_id),
                     f"{_slug(flight_id)}-raw.cbor", "application/octet-stream")


@app.get("/api/v1/export.db")
async def export_db():
    """A consistent snapshot of the whole database, taken without pausing ingest."""
    st = get_store()
    _, stream = await asyncio.to_thread(export.database_snapshot, st.pool.path)
    return _download(stream, "wenet-telemetry.db", "application/x-sqlite3")


# -- admin session ---------------------------------------------------------


class Login(BaseModel):
    password: str


@app.get("/api/v1/admin/session")
async def admin_session(request: Request):
    """Whether admin is switched on, and whether this browser is logged in.

    Open to anyone: the display asks it to decide whether to offer renaming.
    """
    return {"enabled": admin.enabled(), "authenticated": admin.is_admin(request)}


@app.post("/api/v1/admin/login", dependencies=[Depends(admin.check_login_rate)])
async def admin_login(body: Login, request: Request, response: Response):
    if not admin.enabled():
        raise HTTPException(403, "admin is disabled: set WENET_ADMIN_PASSWORD")
    if not admin.password_ok(body.password):
        raise HTTPException(401, "wrong password")
    admin.set_cookie(response, request)
    return {"enabled": True, "authenticated": True}


@app.post("/api/v1/admin/logout")
async def admin_logout(response: Response):
    admin.clear_cookie(response)
    return {"enabled": admin.enabled(), "authenticated": False}


#: Flights this close in time count as touching. The untagged twin from a
#: receiver's first seconds -- before it has decoded an image and learned the
#: callsign -- ends moments before the tagged flight begins, without overlapping.
_TOUCH_SECONDS = 300


def _overlaps(flights: list[dict]) -> dict[int, list[int]]:
    """For each flight, the others it could be half of.

    Two flights overlapping (or touching) in time are one flight split in two
    when their callsigns agree or one has none -- the untagged twins that
    ingest used to open beside every tagged flight. Different callsigns
    overlapping is two balloons, which is not a candidate.
    """
    def span(f):
        return to_epoch(f["started_at"]), to_epoch(f["ended_at"] or f["last_activity"])

    out: dict[int, list[int]] = {f["id"]: [] for f in flights}
    for i, a in enumerate(flights):
        a0, a1 = span(a)
        for b in flights[i + 1:]:
            ca, cb = a["payload_callsign"], b["payload_callsign"]
            if ca and cb and ca != cb:
                continue
            b0, b1 = span(b)
            if a0 <= b1 + _TOUCH_SECONDS and b0 <= a1 + _TOUCH_SECONDS:
                out[a["id"]].append(b["id"])
                out[b["id"]].append(a["id"])
    return out


@app.get("/api/v1/admin/flights", dependencies=[Depends(admin.require_admin)])
async def admin_flights():
    """Every flight, hidden ones included, with likely merge partners."""
    rows = await asyncio.to_thread(get_store().flights, True)
    overlaps = _overlaps(rows)
    for row in rows:
        row["overlaps"] = overlaps[row["id"]]
    return rows


# -- flight administration ------------------------------------------------


class FlightUpdate(BaseModel):
    name: str | None = None
    notes: str | None = None
    hidden: bool | None = None


@app.patch("/api/v1/flights/{flight_id}", dependencies=[Depends(admin.require_admin)])
async def patch_flight(flight_id: int, update: FlightUpdate):
    """Rename, annotate, hide or unhide. Hiding an open flight also closes it."""
    name = update.name.strip() if update.name is not None else None
    if name == "":
        raise HTTPException(400, "name cannot be blank")
    row = await asyncio.to_thread(
        get_store().update_flight, flight_id, name, update.notes, update.hidden
    )
    if row is None:
        raise HTTPException(404, "no such flight")
    return row


@app.post("/api/v1/flights/{flight_id}/close", dependencies=[Depends(admin.require_admin)])
async def close_flight(flight_id: int):
    row = await asyncio.to_thread(get_store().close_flight, flight_id)
    if row is None:
        raise HTTPException(404, "no such flight")
    return row


@app.post("/api/v1/flights/{flight_id}/merge", dependencies=[Depends(admin.require_admin)])
async def merge_flight(flight_id: int, from_flight: int = Query(..., alias="from")):
    """Fold `from` into this flight.

    The repair for a flight the gap heuristic split wrongly -- half an hour
    behind a ridge should not become two flights in the record.
    """
    try:
        return await asyncio.to_thread(get_store().merge_flights, flight_id, from_flight)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/v1/flights/{flight_id}/split", dependencies=[Depends(admin.require_admin)])
async def split_flight(flight_id: int, at: str):
    try:
        return await asyncio.to_thread(get_store().split_flight, flight_id, at)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/events")
async def events(request: Request, flight: int | None = None):
    """Server-sent events: one message per ingest batch.

    SSE rather than a WebSocket because the traffic is one-directional, the
    browser reconnects on its own, and it stays debuggable with curl.
    """
    queue = hub.subscribe()

    async def stream():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Proxies and phone radios drop an idle connection; a comment
                    # frame keeps it open without pretending there was data.
                    yield ": keepalive\n\n"
                    continue
                if flight is not None and event.get("flight_id") != flight:
                    continue
                yield f"data: {json.dumps(event, default=str)}\n\n"
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Without this nginx buffers the stream and live stops being live.
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/v1/flights/{flight_id}/backfill", dependencies=[Depends(admin.require_admin)])
async def backfill(flight_id: int, force: bool = False):
    """Re-resolve altitudes.

    Runs automatically after every ingest, so this is for the case where GPS
    arrives long after the samples it belongs with -- a ground station draining
    a disk spool from an outage, say. `force=true` reconsiders packets already
    written off as having no fix nearby.
    """
    return await asyncio.to_thread(get_store().backfill_altitudes, flight_id, None, force)


# -- /KE5GDB --------------------------------------------------------------
#
# Declared last on purpose. FastAPI matches in declaration order, and this is a
# single-segment catch-all: put earlier, it would shadow `/events`. Multi-segment
# routes like /api/v1/... cannot collide with it, and neither can the /static
# mount.

#: What may appear as a callsign in a path. No dot, so /favicon.ico misses; no
#: slash, so nothing multi-segment reaches here. Portable suffixes (KE5GDB/P)
#: are deliberately out -- a stroke inside a path segment is a different URL.
_CALLSIGN_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{2,15}$")

if STATIC_DIR.is_dir():

    @app.get("/{callsign}", include_in_schema=False)
    async def index_for_callsign(callsign: str):
        """The display, filtered to one transmitter.

        Serves the same file as `/`; the page reads the callsign from its own
        path. Deliberately not validated against the database -- a link to a
        transmitter that has not flown yet should open and wait, which is the
        normal case when the link is sent out before the launch.
        """
        if not _CALLSIGN_PATH.match(callsign):
            raise HTTPException(404, "not found")
        return FileResponse(STATIC_DIR / "index.html")
