# Wenet Telemetry Server

Real-time, self-describing telemetry display for Wenet high-altitude balloon
flights. See [DESIGN.md](DESIGN.md) for the architecture and the reasoning
behind it.

**Status:** all six steps built — ingest, storage, deduplication, altitude
resolution, the ground-station forwarder, the read API with a live SSE stream,
the display, export, and the deployment guide. Nothing has been deployed to a
real server yet: see [DEPLOY.md](DEPLOY.md).

**Picking this up cold?** Start with [HANDOFF.md](HANDOFF.md) — it records what
is *proven* working versus merely built, the failures that are silent, and the
open items. Do not take a feature list at face value over its verification
table.

## Why this is not just a table of numbers

The payload does not tell you what it is going to send. Fields are named at
runtime — a DS18X20 sensor names itself from its ROM, so `temp-004a` cannot
appear in any static schema — and the vocabulary changes between firmware
revisions: the RAB-8 flight sent `temp`/`pres`/`humi` where current firmware
sends `bmp_temp`/`bmp_pres`/`bme_humi`. So fields are rows, not columns, and a
new sensor never needs a migration.

The other half is deduplication. Several ground stations hear the same
transmission, which is the point of having several ground stations. Stored
naively, a flight received by three stations plots every series three times.
A packet is stored once; who heard it is recorded separately, where it becomes
coverage data instead of noise.

## Four identities, and none of them substitute

Everything downstream depends on keeping these apart:

| | scope | means |
|---|---|---|
| `reporter_id` | batch | the ground station that **heard** it |
| `payload_callsign` | item | the Wenet transmitter that **sent** it |
| `payload` name | source | a BLE sensor payload talking to that transmitter |
| `data["id"]` | source | a sensor board on that BLE payload |

Many ground stations report one transmitter; one ground station hears many
transmitters; one transmitter carries several BLE payloads; each BLE payload
carries several sensors. Folding the transmitter into the reporter would make
"who heard it" and "who sent it" unrecoverable.

**A flight is one transmitter's up-burst-down**, so flight identity is
`(payload_callsign, silence gap)` rather than time alone — see below. The BLE
payloads and their sensors are sources *within* a flight, which is what the
source list groups by.

`payload_callsign` is absent when the receiver's firmware predates it, or when a
spool file written before it drains hours later. Absent is stored as NULL and is
its own identity, not a wildcard: untagged traffic groups together rather than
being assumed to belong to a transmitter that was named. `merge` folds the two
together when they were in fact the same balloon.

## Altitude

A pressure reading is a number; a pressure reading with an altitude is a
sounding. Wenet's GPS telemetry arrives on the same UDP port as the sensor
packets, so the altitude is free — but the two never share a timestamp, because
GPS runs about 1 Hz on the receiver's clock and sensors 2 Hz on the payload RTC.

So each packet's altitude is interpolated from the GPS track by a **backfill**
pass, not looked up at ingest: GPS and sensor packets travel separate paths, and
GPS routinely arrives *after* the samples it belongs with — a ground station
draining its spool after an outage delivers hours of it at once.

GPS packets **without** a fix are stored too, with `latitude`, `longitude`,
`altitude` and the other position-derived fields as NULL. They carry battery,
temperatures and satellite count — the telemetry that matters most when the GPS
is what is failing — so they are recorded but never mapped and never used for
altitude. `latest` reports a `gps_status` alongside the last known fix, so the
display can say "no fix" rather than presenting a stale altitude as current.

Interpolation is bounded by `WENET_ALTITUDE_WINDOW_SECONDS` (default 10). Within
the window the figure is good to a few tens of metres at a 5 m/s ascent; across a
GPS dropout of minutes it would be invention, so the packet gets no altitude and
the plot shows an honest gap. A packet is only resolved once GPS exists past it
by a full window — until then a nearer fix may still arrive — and the tail of a
flight is flushed when the flight closes.

## The transmitter's own telemetry

A Wenet GPS packet is not only a fix. It also reports the transmitter's health —
radio and CPU temperature, CPU clock, load averages, disk, camera lens position
and focus figure of merit, battery volts and amps, an auxiliary temperature.
Filed only as a map point, all of that ends up in the `gps.extra` blob: stored,
exported, and impossible to plot.

So the same packet is stored twice, on purpose. The fix goes to `gps`, which is
what the map and the altitude backfill read. The whole packet is *also* filed as
a source (`kind='gps'`, named for the transmitter's callsign) with fields and
readings, exactly like a CBOR packet from a BLE payload — so thermal throttling
on the ascent, or a battery sagging in the cold, charts beside the sensors
instead of hiding in a JSON column.

Consequences worth knowing:

- `packets` and the packet tally now include GPS telemetry packets. They are
  packets, and the exports carry them.
- Only the first station to report a fix writes the series; the rest are receive
  diversity, recorded in `gps_receptions`. A row in `receptions` too would make
  the reporters tally count every fix twice.
- These packets need no altitude interpolation — they *are* the fix, so they are
  resolved on arrival against themselves, and `--force` leaves them alone.
- The week/second pair, `time_source` and the decoder's `error` are not
  registered as series: they describe the packet rather than measure anything.

For flights already in the store, `tools/materialize_gps.py` backfills the same
thing from the rows and the blob. It is idempotent, so an interrupted run
finishes cleanly on the next:

```bash
python tools/materialize_gps.py --list      # what each flight would gain
python tools/materialize_gps.py --all
```

## Quick start

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt        # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest tests/ -q

WENET_DB_PATH=telemetry.db .venv/bin/python -m uvicorn app.main:app --port 8000
```

Then replay a captured flight into it:

```bash
# real time, three ground stations with patchy copy, plus synthetic GPS
python tools/replay.py sensor_data_2026-07-26.log \
    --reporters KE5GDB,K5NBC,W5ABC --hear-rate 0.8 --synth-gps

# as fast as possible (disable the rate limit first, see below)
python tools/replay.py flight.log --speed 0

# just check a capture parses, without sending
python tools/replay.py flight.log --dry-run --limit 20
```

`replay.py` sniffs three capture formats per line: the self-describing CSV that
`wenet_ble_client.py` writes (`time,count,id,key,value,…` — preferred, since it
carries its own field names), JSON Lines, and the payload's positional SD-card
CSV. Only the last needs `--columns`.

Simulating several reporters is how the dedup path gets exercised: the same
packet arrives under different callsigns and must collapse to one sample with
several receptions.

## Configuration

All via environment variables.

| Variable | Default | Notes |
|---|---|---|
| `WENET_DB_PATH` | `telemetry.db` | One file. Backing up a flight is a copy. |
| `WENET_FLIGHT_GAP_MINUTES` | `30` | Silence longer than this starts a new flight, per transmitter. |
| `WENET_ALTITUDE_WINDOW_SECONDS` | `10` | How far a packet may reach for a GPS fix. |
| `WENET_INGEST_API_KEY` | *(unset)* | Unset means **ingest is open** — see below. |
| `WENET_RATE_LIMIT_PER_MINUTE` | `6000` | Per IP. `0` disables — do that for bulk replay. |
| `WENET_MAX_BATCH_PACKETS` | `2000` | Batches larger than this are refused. |

### A note on open ingest

Ingest is unauthenticated by default, as specified. Concretely: anyone who finds
the endpoint can insert telemetry that will appear on the live display and in
exports. Setting `WENET_INGEST_API_KEY` enforces the `X-API-KEY` header that
`wenet_forwarder` already sends, with no code change on either side.

The rate limit is deliberately loose rather than tight. A forwarder draining its
disk spool after an LTE outage arrives as a burst of hundreds of batches, and a
limit tuned for steady state would turn a recovered outage back into permanent
data loss — the exact failure the spool exists to prevent. Clients must treat
429 as retryable.

## API

Implemented so far:

```
POST  /api/v1/ingest                     accept a batch from a ground station
GET   /api/v1/health                     status + row counts + subscriber count
GET   /api/v1/flights
GET   /api/v1/flights/{id}/sources       discovered sources and their fields
GET   /api/v1/flights/{id}/reporters     per-callsign reception counts
GET   /api/v1/flights/{id}/latest        newest sample per source + current fix
                                         (includes heard_epoch + server_now)
GET   /api/v1/flights/{id}/series        downsampled values (?field=…&axis=…)
GET   /api/v1/flights/{id}/track         GPS polyline, thinned
POST  /api/v1/flights/{id}/backfill      re-resolve altitudes (?force)
PATCH /api/v1/fields/{id}                set a field's unit/label
GET   /events                            live SSE stream (?flight=…)
```

### series

`?field=` accepts a field id, `source_key:name`, or a bare `name` — the last
returns that field from every source that has it, because the same sensor name
legitimately appears on more than one payload. Repeat the parameter for several
fields in one call.

`?axis=altitude` plots against height instead of time. That is the sounding
view, and the reason GPS is ingested at all.

Downsampling keeps **min, max and average per bucket** rather than sampling
every Nth point. A decimating sampler steps over spikes; in flight data the
spike is usually the interesting part. The response is columnar (`x`, `min`,
`max`, `avg`) so it can be handed to a plotting library without reshaping.

### Export

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
whole flight because sources share neither a field set nor a rate — a wide table
across all of them would be mostly empty cells. **JSON Lines** deliberately
matches the shape of the payload-side analysis logs from previous flights, so
existing notebooks read an export unchanged, with `altitude` and `reporters`
added.

**The CBOR archive** is `[4-byte big-endian length][packet]` repeating. Framing
is needed because CBOR self-delimits only if the packet is whole, and these may
be truncated. It is the file that survives a decoder bug: re-run a fixed decoder
over it and recover fields the original ingest could not read. It only contains
packets that arrived with raw bytes — that is, via the forwarder. A replayed CSV
log has no raw bytes to archive, so its archive is legitimately empty.

**The database snapshot** uses `VACUUM INTO`, which snapshots inside a read
transaction, so the copy is coherent even while packets are arriving. Copying
the file directly can catch a torn state under WAL.

Every export streams. A flight with the raw archive runs to hundreds of
megabytes, and building that in memory to answer one download would take the
server down mid-flight.

### Flight administration

```
PATCH /api/v1/flights/{id}                 rename, set notes
POST  /api/v1/flights/{id}/close           end it now
POST  /api/v1/flights/{id}/merge?from=N    fold N into this flight
POST  /api/v1/flights/{id}/split?at=…      cut in two at a timestamp
```

Merge exists because of the failure mode the gap heuristic actually has: lose
the balloon behind a ridge for half an hour and one flight is recorded as two.
Splitting wrongly is easy and, without merge, impossible to undo. Where both
flights hold the same source, the sources are folded together — fields remapped,
readings repointed, and any packet both sides already hold dropped rather than
duplicated.

Merge refuses two flights carrying **different** callsigns: that is not a split
flight, it is two balloons, and gluing them together would put two GPS tracks in
one flight and interpolate one balloon's altitude onto the other's sensors. An
untagged flight merges freely into a tagged one and adopts its callsign, which
is the cutover case — the same balloon before and after the receivers started
naming it.

### Flights

A flight is one Wenet transmitter's up-burst-down. `_current_flight` matches an
open flight on `(payload_callsign, gap)`, so two balloons in the air at once —
or a neighbour's beacon your receiver happens to hear — are separate flights
however much their times overlap. Nothing about the calendar is involved: a
flight that crosses midnight stays one flight, and only its default name takes
the start date.

The gap is measured on the **reception** clock, never the payload's: a board
with no PCF8523 restarts its RTC at 2021-01-01 on every power cycle, and judging
silence against that would file a healthy flight under the wrong one.

Because several transmitters can be up at once, every batch also sweeps closed
any flight that has gone quiet past the gap. Without that, a flight is only ever
closed by traffic from its own transmitter, so one that lands or is switched off
would sit `open` forever — which the display reads as live.

### /events

One SSE message per ingest batch, carrying the newest sample per source (not
every sample — a batch may hold thirty and the tiles show one) plus any new GPS
fixes. A comment frame every 15 s keeps proxies from dropping an idle
connection, and `X-Accel-Buffering: no` stops nginx buffering the stream.

A subscriber that stops reading gets its oldest events dropped rather than being
allowed to slow ingest down; for a live display the newest data is what matters.

```bash
curl -N http://localhost:8000/events
```

The endpoint is verified against a real server rather than in the test suite:
its generator only ends on client disconnect, which `TestClient` cannot signal
cleanly, so a streaming request there hangs the suite. What the tests pin is the
part with logic in it — that ingest publishes a correctly shaped, JSON-safe
event, and that a stalled subscriber is dropped.

Ingest body:

```json
{
  "reporter_id": "KE5GDB",
  "packets": [
    { "kind": "sensor", "raw": "a4647469…", "data": { "time": "…", "id": "…" } },
    { "kind": "gps", "data": { "timestamp": "…", "latitude": 32.9, "altitude": 18422.0 } }
  ]
}
```

`raw` is the hex of the bytes as they came off the air. It is optional but
preferred: it is re-decoded server-side, so the archive does not inherit a
forwarder decoding bug and a future decoder fix can be replayed offline.

A **bare JSON list** is also accepted, because that is what the current
unmodified `wenet_forwarder` sends — so it can be pointed here by changing only
its URL, before step 2 touches it.

## The display

Open `http://localhost:8000/`. The whole page is generated from what arrives —
source list, field rows and charts are all built from the server's discovery
output, so a payload nobody has flown before needs no frontend change.

- **Small multiples**, one chart per `source:field`, single series each.
  Discovered fields have unrelated units (hPa, °C, m/s², counts); putting them on
  shared axes would be a dual-axis chart, which invents correlations that are not
  in the data. A crosshair on one panel moves them all, since they share an x axis.
- **One shared x domain across every chart.** All panels are drawn on the same
  span, so a value at time T sits at the same screen position in all of them —
  a source that only reported for part of a flight occupies only that part of
  its panel instead of being stretched across it. That is what makes the shared
  cursor honest: the marker line is drawn at the exact x value rather than
  snapped to any one series' nearest sample, so it stays continuous down the
  stack even where sources disagree on rate or have gaps. Each panel's dot shows
  what *that* series reads there.
- **A card per checked field**, beside the altitude and ascent-rate tiles. With
  the cursor on a chart the cards read at the cursor, so the header describes
  the same instant as the marker line.
- **Grafana-style time range**: presets, drag on any chart to zoom, double-click
  or Reset to go back. A narrowed window re-queries the server, so zooming in
  buys real resolution rather than magnifying the same downsampled points. The
  window filters the map track too, and lives in the URL (`?from=…&to=…` or
  `?range=300`) so a zoomed view can be shared.
- **Time or altitude x-axis.** The altitude view is the sounding, and the reason
  GPS is ingested at all. A time window applies there too — narrowing to the
  ascent is how you read a sounding without the descent drawn back over it.
- **Track map** coloured by altitude on a single-hue sequential ramp, with a
  scale legend. If tiles fail to load — offline chase vehicle, blocked tile
  server — the track still draws over a plain grid. Losing the basemap must not
  lose the flight path.
- **Colour follows the entity, never its rank**: a source keeps its hue for the
  life of the page, so a new arrival or a filter never repaints the others. Every
  source carries a visible text label beside its colour dot, so identity is never
  colour alone.
- **Both themes are selected**, not an automatic flip, and the palette is
  validated for colour-vision deficiency in both.

Linkable views: `?flight=12`, `?axis=altitude`, `?theme=light`,
`?range=300`, `?from=…&to=…`.

The left-hand source list deliberately keeps showing the payload's *latest*
values while a window is open — it is a status panel, alongside rate, packet
count and stalled state, all of which are "now" concepts. The cards and charts
are the windowed view.

No build step and no dependencies — the chart and map are drawn on canvas
directly. That was forced by having no outbound network to vendor uPlot and
Leaflet, and a CDN reference that cannot be tested is worse than code that can.
Both are self-contained modules, so swapping in a real library later is a
contained change.

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
static/
  index.html   the display
  style.css    colour roles for both themes
  js/app.js    discovery, live updates, wiring
  js/chart.js  canvas series chart
  js/map.js    canvas slippy map
tools/
  replay.py        replay a captured flight into the ingest endpoint (tests the server)
  fake_receiver.py emit Wenet UDP frames as the receiver would (tests the forwarder)
tests/             105 tests, chiefly pinning the dedup, altitude and merge rules
```

The capture-format tests embed verbatim lines from a real
`sensor_data_*.log`, so the suite needs no large fixture file on disk.

`fake_receiver.py` sits one stage earlier than `replay.py`: it broadcasts the
same UDP frames `rx_ssdv.py` produces, so the whole ground-station chain can be
exercised on a bench with no SDR and no balloon.

```bash
python tools/fake_receiver.py --duration 30 --rate 2
```

## Verification

The suite covers the dedup rules that are easy to get wrong: `count` wraps at
65536 so the same count recurs within a flight; an unset RTC replays identical
timestamps after every reboot; neither alone is a safe key, so the key is
`(source, payload_time, count)`.

End-to-end against the real RAB-8 logs: 172,572 packets and 22,757 GPS points
from two payloads and three simulated ground stations, zero rejects, and the
stored `pres` series reproduces the real flight — 995 hPa on the ground,
20.7 hPa minimum at altitude, 1000 hPa after landing.

End-to-end through the forwarder, driven by `fake_receiver.py`: 90 sensor
packets and 42 GPS fixes stored from 90 and 42 emitted, zero rejects, no-fix GPS
correctly discarded, and interpolated altitudes agreeing with the barometric
formula to within 0.2%. With the server killed mid-run, 60 packets spooled to
disk and drained without loss when it came back — the late GPS backfilling
altitudes onto sensor packets stored before it.

Read API and SSE verified against a live server: 100 SSE frames delivered during
a 60x replay with two reporters, correct headers including `X-Accel-Buffering`,
and series/track/latest returning consistent data across both axes.

The display was rendered and inspected in both themes against 11,726 real RAB-8
packets from two payloads, and the sounding view verified end-to-end through the
whole chain — UDP frames to chart API — with pressure matching the barometric
formula to 0.02%.

**Not visually verified: the live view.** An open SSE stream keeps headless Edge
from ever reaching an idle state, so it cannot be screenshotted the way the
replay view can. Its data was checked through the API and its event frames
captured off the wire, but the live rendering itself wants a human with a real
browser.

Export and flight admin were verified against a real 13,357-line
`sensor_data_*.log` from the balloon Pi: 13,038 packets ingested with zero
rejects across two simulated ground stations, 6,517-row wide CSV,
104,304-row long CSV, 13,038-line JSONL, a byte-identical CBOR round trip, and a
15.5 MB database snapshot passing `integrity_check`. A split and a merge back
conserved every packet and GPS fix and left the series readable.

## Deployment

[DEPLOY.md](DEPLOY.md) walks through a Debian VPS with nginx, TLS and nightly
backups; the config files are in [deploy/](deploy/). **None of it has been run
on a Debian box** — it was written on a Windows dev machine — so treat the first
deploy as a test and work through the verification checklist before a flight
depends on it.

Two constraints there are load-bearing and easy to break:

- **`--workers 1`.** The SSE fan-out is an in-process hub, so a second worker
  serves browsers that never receive the first worker's events — a display that
  looks fine and shows nothing.
- **`--proxy-headers`.** The per-IP ingest rate limit keys on the client
  address; behind nginx without it, every ground station shares one bucket.
