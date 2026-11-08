# Wenet Telemetry Server

This project serves as the display and archive server for data conveyed through Wenet CBOR telemetry packets.

Data follows these steps:

1. It is collected by a raw sensor that is connected to a Pi Pico 2W. The Pico 2W runs [Wenet BLE Sensor](https://github.com/ke5gdb/wenet_ble_sensor).
1. Data is received by the larger Wenet payload on your flight string using [Wenet Payload BLE Bridge](https://github.com/ke5gdb/wenet_payload_ble_bridge). This utility connects to BLE devices that identify with a special Wenet UUID. A timestamp correction is applied to incoming data, and then it is relayed via UDP packet to the standard Wenet process.
1. The [Wenet](https://github.com/projecthorus/wenet/) process transmits the data as a CBOR-type packet (type `0x05`). 
1. Data is received via the standard Wenet ground stations and broadcast locally on the ground station network via UDP. 
   1. NOTE: This does not yet work with WebHorus/WebWenet. You must be running the Docker or bare metal Linux versions.
1. The UDP telemetry packets are received by [Welent Telemetry Forwarder](https://github.com/ke5gdb/wenet_telemetry_forwarder) and relayed to a user-configured telemetry server.
1. Data is displayed and archived by **this** project, Wenet Telemetry Server.

Features of Wenet Telemetry Server: 
- Live displays build themselves -- all sources, fields, units, and charts are dynamically generated from the data that arrives. No configuration is required for a BLE sensor payload's data to be displayed.
- Deduplication of data received by multiple ground stations. A unique record is maintained for packet counts, but data itself is deduplicated. 
- Raw packets are archived for future analysis and flight data replays. 
- Outages are survivable -- ground stations spool data locally if their internet connection is lost. This data is then uploaded when possible. 

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
gap. There is only ever one open flight per callsign. Flight housekeeping lives in
the **admin panel at `/admin/`** (password from `WENET_ADMIN_PASSWORD`): rename
-- "RAB-9" beats "Flight 2026-08-15 - KE5GDB #1" once it has landed -- annotate,
close, hide test flights from the public list, merge a flight a signal gap split
in two, or split two that ran together. The panel flags overlapping flights that
are probably one flight in two halves. Logged in, the display's title is also
click-to-rename.

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

Ground stations POST batches. Example:

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

`payload_callsign` is optional, and often missing: the Wenet receiver learns the
callsign from SSDV image packets, so it sends none until its first image decodes,
and older receivers never send one. An untagged packet joins the flight it
evidently belongs to -- the one already holding its sensor source, else the
nearest GPS track, else the only tagged flight in the air -- and stays apart only
when that is genuinely ambiguous. The first tagged packet adopts an untagged
flight already under way. `raw` is optional but preferred -- it is what makes the archive
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

```

Administration needs the admin password, as a login cookie
(`POST /api/v1/admin/login`, which the panel does) or an `X-Admin-Password`
header for scripts. With `WENET_ADMIN_PASSWORD` unset these are all refused.

```
GET   /api/v1/admin/flights              every flight incl. hidden, with overlaps
PATCH /api/v1/flights/{id}               rename, notes, hidden (hiding closes it)
POST  /api/v1/flights/{id}/close         end it now
POST  /api/v1/flights/{id}/merge?from=N  fold N into this flight
POST  /api/v1/flights/{id}/split?at=…    cut in two at a timestamp
POST  /api/v1/flights/{id}/backfill      re-resolve altitudes (?force)
PATCH /api/v1/fields/{id}                set a field's unit or label
```

Hidden flights leave `GET /api/v1/flights` but are still served by id -- hiding
tidies the list, it does not protect anything.

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
tests/         200 tests, chiefly pinning the dedup, altitude and merge rules
```

## Where the reasoning lives

Design decisions and the arguments behind them are in
[DESIGN.md](DESIGN.md) -- the data model, why fields are rows rather than columns,
why the display draws small multiples on a shared x domain, and what was
rejected. [HANDOFF.md](HANDOFF.md) is the state of play: what is verified against
real data, which failures are silent, and the open items. The `schema.sql`
comments explain each table's constraints, which is usually the fastest way to
understand why something is stored the way it is.
