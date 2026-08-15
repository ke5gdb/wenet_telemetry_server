# Wenet Telemetry Server

Watch a high-altitude balloon's sensors live while it is in the air, and keep
everything it sent in a form you can actually analyze afterwards.

Point your ground stations at it and open the page. The payload's sensors appear
on their own as packets arrive -- no configuration, no schema to edit, nothing to
change when you fly a different payload. Several ground stations can report the
same flight without the data being counted twice, and every reading ends up with
an altitude attached, so a pressure trace becomes a sounding.

**Status:** deployed and running behind nginx. [HANDOFF.md](HANDOFF.md) records
what is *proven* against real flight data versus merely built -- worth reading
before a flight depends on it. [DEPLOY.md](DEPLOY.md) is the setup guide.

## What you get

- **A live display that builds itself.** Sources, fields, units and charts all
  come from what arrives. For example, DS18X20 names itself from its ROM at runtime, so
  `temp-004a` can never appear in a static schema -- nothing here needs it to.
- **One link per transmitter.** `{server}/W1ABC` opens the display filtered to
  that balloon. Hand it to the general public; it needs no explanation, and it works
  before the launch, waiting and picking the flight up when the first packet
  lands.
- **Soundings, not just time series.** Switch the x-axis to altitude and a
  pressure or temperature trace becomes a profile of the atmosphere. Each packet
  gets its altitude interpolated from the GPS track.
- **Several ground stations, one copy of the data.** The same transmission heard
  by three receivers is stored once, and who heard it is recorded separately --
  where it becomes useful coverage data instead of a series plotted three times.
- **The transmitter's own health.** Wenet GPS packets carry CPU and radio
  temperature, load, disk, camera focus, battery volts and amps. All of it charts
  beside the sensors, so thermal throttling on the ascent is visible.
- **Nothing is thrown away.** Raw packets are archived exactly as they came off
  the air, so a decoder fix can be replayed over an old flight. Exports are CSV,
  JSON Lines, raw CBOR, or the whole database.
- **Outages are survivable.** A ground station that loses its link spools to disk
  and drains later; GPS data that arrives hours after the samples it belongs with
  still backfills their altitudes.

## Where it sits

```
balloon                       ground                          you
┌──────────────┐   RF   ┌──────────────┐  UDP  ┌───────────┐       ┌─────────┐
│ BLE sensor   │───────▶│ Wenet        │──────▶│ wenet_    │──HTTP▶│ this    │
│ payloads     │  BLE   │ transmitter  │       │ forwarder │       │ server  │
│ (many, each  │───────▶│ (GPS + radio)│       │ (spools   │       │         │
│  many sensors)│       └──────────────┘       │  on fail) │       └────┬────┘
└──────────────┘                               └───────────┘            │
                        …and any number of other ground stations ───────┤
                                                                        ▼
                                                              browsers, exports
```

This server is the last box. The receiver and forwarder live in
[wenet_forwarder](../wenet_forwarder); everything upstream of the UDP frame is
somebody else's problem, which is the point.

## Quick start

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt        # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest tests/ -q                 # 159 tests

WENET_DB_PATH=telemetry.db .venv/bin/python -m uvicorn app.main:app --port 8000
```

Open <http://localhost:8000/>. With no balloon to hand, replay a captured flight
into it:

```bash
# real time, three ground stations with patchy copy, plus synthetic GPS
python tools/replay.py sensor_data_2026-07-26.log \
    --reporters K5RWK,K1ABC,W5ABC --hear-rate 0.8 --synth-gps

# as fast as possible (set WENET_RATE_LIMIT_PER_MINUTE=0 on the server first)
python tools/replay.py flight.log --speed 0
```

`tools/fake_receiver.py` sits one stage earlier and broadcasts the UDP frames a
real receiver would, so the whole ground-station chain can be exercised with no
SDR and no balloon.

## Using the display

| URL | Shows |
|---|---|
| `/` | every flight, newest first |
| `/N1XYZ` | only that transmitter's flights |
| `/?flight=12` | a specific flight |
| `/?axis=altitude` | the sounding view |
| `/?range=300` or `/?from=…&to=…` | a time window |
| `/?theme=light` | pinned light mode, for a tablet in the sun |

Tick fields in the left-hand list to chart them. Drag on any chart to zoom every
chart and the map together; double-click or **Reset** to go back. The browser's
Back button steps back out of each zoom, and the view is in the URL throughout,
so any of it can be pasted to someone else.

A flight is one transmitter's up-burst-down, segmented automatically on a silence
gap. Click the title to rename it -- "RAB-9" beats "Flight 2026-08-15 - KE5GDB #1"
once it has landed. If a signal gap wrongly split one flight in two, merge them;
if two flights ran together, split them.

The source list keeps showing the payload's *latest* values even while a window
is open -- it is a status panel, alongside rate, packet count and stalled state,
which are all "now" concepts. The charts and the cards above them are the
windowed view.

## Getting the data out

```
GET /api/v1/flights/{id}/export.csv            long format, every source
GET /api/v1/flights/{id}/export.csv?source=…   wide format, one source
GET /api/v1/flights/{id}/gps.csv               the track
GET /api/v1/flights/{id}/export.jsonl          one decoded packet per line
GET /api/v1/flights/{id}/export.cbor           raw packets, lossless archive
GET /api/v1/export.db                          whole-database snapshot
```

**Wide CSV** mirrors the payload's own SD-card CSV with the resolved altitude
added, so it loads straight into pandas. **Long CSV** is the default across a
whole flight, because sources share neither a field set nor a rate and a wide
table across all of them would be mostly empty cells. **JSON Lines** matches the
shape of the payload-side analysis logs, so existing notebooks read an export
unchanged. **The CBOR archive** is what survives a decoder bug -- re-run a fixed
decoder over it and recover fields the original ingest could not read.

Everything streams, so a flight with the raw archive does not have to fit in
memory. The database snapshot uses `VACUUM INTO` and is safe on a live server.

## Feeding it

Ground stations POST batches. This is the whole contract:

```json
{
  "reporter_id": "N9XYZ",
  "packets": [
    {"kind": "sensor", "raw": "05a1...", "payload_callsign": "N9XYZ",
     "data": {"id": "GDB_ENV_ENV", "count": 27472, "time": "..."}},
    {"kind": "gps", "payload_callsign": "N9XYZ", "data": {"latitude": 32.9}}
  ]
}
```

Three identities meet there and none can stand in for another:

| | means |
|---|---|
| `reporter_id` | the ground station that **heard** it |
| `payload_callsign` | the Wenet transmitter that **sent** it -- what a flight is keyed on |
| `data.id` | the sensor board on one of the BLE payloads talking to that transmitter |

`payload_callsign` is optional: older receivers do not report one, and packets
without it group together rather than being assumed to belong to a transmitter
that was named. `raw` is optional but preferred -- it is what makes the archive
lossless. A bare JSON list is also accepted, which is what an unmodified
forwarder sends.

Clients must treat **429 as retryable**. The rate limit is deliberately loose:
a forwarder draining its spool after an outage arrives as a burst of hundreds of
batches, and a limit tuned for steady state would turn a recovered outage back
into permanent data loss.

## Reading it programmatically

```
GET   /api/v1/health                     status + row counts + subscriber count
GET   /api/v1/flights
GET   /api/v1/flights/{id}/sources       discovered sources and their fields
GET   /api/v1/flights/{id}/reporters     per-callsign reception counts
GET   /api/v1/flights/{id}/latest        newest sample per source + current fix
GET   /api/v1/flights/{id}/series        downsampled values (?field=…&axis=…)
GET   /api/v1/flights/{id}/track         GPS polyline, thinned
GET   /events                            live SSE stream (?flight=…)

PATCH /api/v1/flights/{id}               rename, set notes
POST  /api/v1/flights/{id}/close         end it now
POST  /api/v1/flights/{id}/merge?from=N  fold N into this flight
POST  /api/v1/flights/{id}/split?at=…    cut in two at a timestamp
POST  /api/v1/flights/{id}/backfill      re-resolve altitudes (?force)
PATCH /api/v1/fields/{id}                set a field's unit or label
```

`series?field=` takes a field id, `source_key:name`, or a bare `name` -- the last
returns that field from every source that has it, since the same sensor name
legitimately appears on more than one payload. Downsampling keeps **min, max and
average per bucket** rather than every Nth point, because in flight data the
spike is usually the interesting part. The response is columnar (`x`, `min`,
`max`, `avg`), ready to hand to a plotting library.

## Configuration

All via environment variables.

| Variable | Default | Notes |
|---|---|---|
| `WENET_DB_PATH` | `telemetry.db` | One file. Backing up a flight is a copy. |
| `WENET_FLIGHT_GAP_MINUTES` | `30` | Silence longer than this starts a new flight, per transmitter. |
| `WENET_ALTITUDE_WINDOW_SECONDS` | `10` | How far a packet may reach for a GPS fix. |
| `WENET_INGEST_API_KEY` | *(unset)* | Unset means **ingest is open** -- see below. |
| `WENET_RATE_LIMIT_PER_MINUTE` | `6000` | Per IP. `0` disables -- do that for bulk replay. |
| `WENET_MAX_BATCH_PACKETS` | `2000` | Larger batches are refused. |

**Ingest is unauthenticated by default.** Concretely: anyone who finds the
endpoint can insert telemetry that appears on the live display and in exports.
Setting `WENET_INGEST_API_KEY` enforces the `X-API-KEY` header the forwarder
already sends, with no code change on either side.

## Deployment

[DEPLOY.md](DEPLOY.md) walks through a Debian VPS with nginx, TLS and nightly
backups; the config files are in [deploy/](deploy/). Two constraints there are
load-bearing and easy to break:

- **`--workers 1`.** The SSE fan-out is an in-process hub, so a second worker
  serves browsers that never receive the first worker's events -- a display that
  looks fine and shows nothing.
- **`--proxy-headers`.** The per-IP rate limit keys on the client address;
  behind nginx without it, every ground station shares one bucket.

The nginx config in `deploy/` is a pre-TLS template. Do not copy it over a live
config that certbot has already rewritten -- add the changed block by hand
instead, or you will drop the `listen 443` block and take the site off the air.

## Layout

```
app/
  main.py      FastAPI app: ingest, read routes, SSE, export, rate limit
  store.py     ingest, dedup, flight segmentation/admin, discovery, queries
  export.py    streaming CSV / JSONL / raw-CBOR / database exports
  hub.py       fan-out of live updates to connected browsers
  schema.sql   the data model, with the reasoning in comments
  decode.py    CBOR decode, tolerant of BLE truncation
  units.py     seed units/descriptions, ported from configurator.html
  timeutil.py  timestamp normalisation (the payload emits ISO *basic* format)
static/        the display: no build step, no dependencies, canvas charts and map
tools/
  replay.py           replay a captured flight into the ingest endpoint
  fake_receiver.py    emit Wenet UDP frames as the receiver would
  materialize_gps.py  backfill GPS housekeeping as chartable series (old flights)
  delete_payload.py   remove a payload that was never on your balloon
tests/         159 tests, chiefly pinning the dedup, altitude and merge rules
```

## Where the reasoning lives

Design decisions and the arguments behind them are in
[DESIGN.md](DESIGN.md) -- the data model, why fields are rows rather than columns,
why the display draws small multiples on a shared x domain, and what was
rejected. [HANDOFF.md](HANDOFF.md) is the state of play: what is verified against
real data, which failures are silent, and the open items. The `schema.sql`
comments explain each table's constraints, which is usually the fastest way to
understand why something is stored the way it is.
