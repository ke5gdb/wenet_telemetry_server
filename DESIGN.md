# Wenet Telemetry Server — Design

Real-time, self-describing telemetry display for Wenet high-altitude balloon flights.

## 1. The pipeline as it exists today

```
Pico W payload (MicroPython)          ── BLE (CBOR on NUS TX) ──┐
  main.py: sensor_task()                                        │
    packet_dict = {time, id, count, ...arbitrary sensor fields} │
    id = "<payload_name>_<sensor_name>"  e.g. RAB_HAT_ENV       │
    cbor2.dumps(), asserted < 254 bytes                         │
                                                                ▼
Pi on the balloon — wenet_ble_scripts/wenet_ble_client.py
  scans for service fb63feb8-…, holds up to 10 payloads at once
  pads CBOR to 254 B, wraps as
    {'type':'WENET_TX_SEC_PAYLOAD','id':55,'repeats':1,'packet':[…]}
  → UDP 127.0.0.1:55674
                                                                ▼
              Wenet FSK downlink  (black box — leave alone)
                                                                ▼
Ground station — wenet_forwarder.py, UDP :55672
  receives {'type':'WENET','packet':[…]}
  packet[0] is the Wenet packet type:
    0x01 GPS_TELEMETRY   ← currently DISCARDED
    0x03 SEC_PAYLOAD     ← kept when packet[1] == 55, CBOR from packet[2:]
  adds reporter_id = callsign, batches 15 / 5 s
  → PUT https://api.k5nbc.com/balloon/telemetry/
```

### Facts that drive the design

1. **Packets are schemaless.** Only `time`, `id`, `count` are universal
   ([main.py:214-227](../Wenet_BLE_Sensor/main.py#L214-L227)). Everything else depends on
   which I2C/OneWire sensors were detected at boot. DS18X20 fields are named at runtime from
   the ROM (`temp-a4f2`), so the field set is not even enumerable in advance. **The UI must
   build itself from the data that arrives.**

2. **One physical payload emits multiple sources.** `sensor_task` and `sensor_task_lsm6dso`
   each publish their own `id` (`RAB_HAT_ENV`, `RAB_HAT_LSM6DSOX`) with their own `count`
   sequence and their own rate. `count`/`time` exist in both and must never be merged into one
   series — the configurator already learned this lesson
   ([configurator README](../Wenet_BLE_Sensor/README.md#L267-L273)).

3. **Multiple ground stations hear the same packet.** `reporter_id` is per-reception, not per
   packet. The same `(id, count)` will arrive from several callsigns. Storing them as distinct
   samples would triple-plot every series. Dedup is mandatory, and the duplicates are
   *valuable* — they tell you receive coverage per station.

4. **GPS is free.** `GPS_TELEMETRY` (0x01) carries `latitude`, `longitude`, `altitude`,
   `ascent_rate`, `ground_speed`, `heading`, `numSV`, `gpsFix`, plus payload housekeeping
   (`batt_v`, `cpu_temp`, …) — decoded by
   [WenetPackets.py:200-240](../wenet/rx/WenetPackets.py#L200-L240). It is already on UDP
   :55672. The forwarder just doesn't look at it. This is what makes sensor data *mean*
   something: pressure vs. altitude, temperature vs. altitude, tropopause identification.

5. **Packets can arrive truncated.** A host that never negotiates ATT MTU cuts the CBOR short;
   the transmit side pads to 254 B with `\x00`. The server must decode leniently and record
   what it got rather than dropping the packet.

## 2. Architecture

```
        ground station(s)                       VPS (Debian)
   ┌────────────────────────┐        ┌──────────────────────────────────┐
   │ wenet_forwarder.py     │        │  nginx :443 (TLS, certbot)       │
   │  UDP :55672            │        │    /            → static files   │
   │  decode 0x01 + 0x03    │ HTTPS  │    /api, /events → uvicorn :8000 │
   │  disk-backed spool ────┼───────▶│                                  │
   └────────────────────────┘  POST  │  FastAPI (Python 3.11, asyncio)  │
                                     │    ingest → dedup → SQLite (WAL) │
                                     │    fan-out → SSE subscribers     │
                                     │                                  │
                                     │  SQLite: telemetry.db            │
                                     └──────────────────────────────────┘
```

**Why this stack**

| Choice | Reasoning |
|---|---|
| **FastAPI + uvicorn** | Every other component in this system is Python/asyncio/`cbor2`. The server can import the *same* decode logic rather than reimplementing it in another language. |
| **SQLite (WAL mode)** | Peak load is ~2 packets/s per source; a 5-hour flight with five payloads is well under 200k rows. That is nothing for SQLite. It removes a whole service from the VPS, and "export the database" becomes `cp telemetry.db`. Postgres/TimescaleDB is the escape hatch if you ever archive hundreds of flights — the schema below ports unchanged. |
| **SSE, not WebSocket** | Traffic is one-directional server→browser. SSE reconnects automatically, survives nginx buffering with one config line, needs no library, and degrades to a plain `curl` you can debug. |
| **No build step** | [configurator.html](../Wenet_BLE_Sensor/configurator.html) proves the pattern works well here: vanilla JS, self-contained, no CDN. Vendored **uPlot** (~40 KB, handles 100k+ points smoothly) and **Leaflet** for the map. Nothing to `npm install` on the VPS, nothing to break at 2 a.m. before a launch. |
| **systemd + nginx** | Matches the `wenet_ble.service` pattern already in use. Docker is a fine alternative if you'd rather mirror the balloonatics compose setup. |

## 3. Data model

The central tension: the data is schemaless, but charting wants indexed columns. Resolved with
a **hybrid** — a lossless packet archive plus a narrow, indexed table of numeric readings.

```sql
-- A flight groups everything for scoping and export.
CREATE TABLE flights (
  id           INTEGER PRIMARY KEY,
  name         TEXT,                    -- editable; auto "Flight 2026-07-26"
  started_at   TEXT NOT NULL,
  ended_at     TEXT,
  notes        TEXT
);

-- One row per distinct 'id' string seen, e.g. RAB_HAT_ENV.
CREATE TABLE sources (
  id           INTEGER PRIMARY KEY,
  flight_id    INTEGER NOT NULL REFERENCES flights(id),
  source_key   TEXT NOT NULL,           -- the packet's 'id'
  payload_name TEXT NOT NULL,           -- 'RAB_HAT'   (source_key before last '_')
  task_name    TEXT NOT NULL,           -- 'ENV'
  first_seen   TEXT NOT NULL,
  last_seen    TEXT NOT NULL,
  UNIQUE(flight_id, source_key)
);

-- Lossless archive. One row per unique packet, NOT per reception.
CREATE TABLE packets (
  id           INTEGER PRIMARY KEY,
  source_id    INTEGER NOT NULL REFERENCES sources(id),
  payload_time TEXT NOT NULL,           -- 'time' from the payload RTC (ISO, ms)
  count        INTEGER NOT NULL,
  data         TEXT NOT NULL,           -- decoded dict as JSON
  raw_cbor     BLOB,                    -- exactly what came off the air
  truncated    INTEGER NOT NULL DEFAULT 0,
  UNIQUE(source_id, payload_time, count)   -- ← the dedup key
);

-- Every reception of a packet, one per ground station that heard it.
CREATE TABLE receptions (
  packet_id    INTEGER NOT NULL REFERENCES packets(id),
  reporter_id  TEXT NOT NULL,
  received_at  TEXT NOT NULL,
  PRIMARY KEY (packet_id, reporter_id)
);

-- Discovered fields, with display metadata. Seeded from the configurator's UNITS map.
CREATE TABLE fields (
  id           INTEGER PRIMARY KEY,
  source_id    INTEGER NOT NULL REFERENCES sources(id),
  name         TEXT NOT NULL,           -- 'bmp_pres', 'temp-a4f2'
  unit         TEXT,                    -- auto-seeded, user-editable
  label        TEXT,
  kind         TEXT NOT NULL,           -- 'numeric' | 'text'
  UNIQUE(source_id, name)
);

-- Narrow numeric series. This is what the charts read.
CREATE TABLE readings (
  field_id     INTEGER NOT NULL REFERENCES fields(id),
  ts           TEXT NOT NULL,
  value        REAL NOT NULL,
  packet_id    INTEGER NOT NULL REFERENCES packets(id)
);
CREATE INDEX idx_readings_series ON readings(field_id, ts);

-- Wenet GPS_TELEMETRY (0x01), keyed by the transmitting Wenet payload.
CREATE TABLE gps (
  id           INTEGER PRIMARY KEY,
  flight_id    INTEGER NOT NULL REFERENCES flights(id),
  timestamp    TEXT NOT NULL,
  latitude     REAL, longitude REAL, altitude REAL,
  ascent_rate  REAL, ground_speed REAL, heading REAL,
  numSV        INTEGER, gps_fix INTEGER,
  extra        TEXT,                    -- batt_v, cpu_temp, … as JSON
  UNIQUE(flight_id, timestamp)
);
```

**Dedup key is `(source_id, payload_time, count)`.** `count` alone is unsafe — it resets on
reboot. `payload_time` alone is unsafe — an unset RTC starts at 2021-01-01 on every boot. The
pair is unique in practice, and a duplicate insert becomes an `INSERT OR IGNORE` on `packets`
followed by an insert into `receptions`, which is exactly the desired semantics: *one sample,
N reporters*.

**Flight segmentation.** A new flight is opened automatically after a configurable silence gap
(default 30 min), and can be renamed, split, or merged from the UI. Without this, "export the
data" is ambiguous the moment you fly twice.

## 4. HTTP API

```
POST /api/v1/ingest              ← ground stations
GET  /api/v1/flights
GET  /api/v1/flights/{id}/sources          discovered sources + fields + live stats
GET  /api/v1/flights/{id}/series?field=…&from=…&to=…&max_points=…
GET  /api/v1/flights/{id}/track            GPS polyline
GET  /api/v1/flights/{id}/reporters        per-callsign packet counts, coverage
GET  /api/v1/flights/{id}/export.{csv,json,cbor,db}
PATCH /api/v1/fields/{id}                  set unit/label
GET  /events?flight={id}                   SSE live stream
```

Ingest body — versioned, batched, and carrying the raw bytes so the archive stays lossless:

```json
{
  "reporter_id": "KE5GDB",
  "packets": [
    { "kind": "sensor", "received_at": "2026-07-26T18:22:04.512Z",
      "raw": "a4647469…", "data": { "time": "…", "id": "RAB_HAT_ENV", "count": 412, "…": 0 } },
    { "kind": "gps", "received_at": "2026-07-26T18:22:05.001Z",
      "data": { "timestamp": "…", "latitude": 32.9, "longitude": -97.2, "altitude": 18422.0 } }
  ]
}
```

Response returns `{accepted, duplicates, rejected}` so the forwarder can log honestly.

`series` downsamples server-side via min/max bucketing when a range exceeds `max_points`, so a
whole-flight chart stays responsive without lying about spikes.

**Ingest is unauthenticated**, per your decision. Concretely that means anyone who finds the
endpoint can insert telemetry that will appear on the live display and in exports. The endpoint
reads an optional `INGEST_API_KEY` env var — unset by default, so nothing changes; set it and
the forwarder's `X-API-KEY` header (which it already sends) starts being enforced. No code
change needed either way. Rate limiting per IP is on regardless, since that costs nothing.

> Housekeeping, unrelated to this design: the current API key is committed in plaintext at
> [wenet_forwarder.py:15](../wenet_forwarder/wenet_forwarder.py#L15).
> Since we're cutting over, rotate it on api.k5nbc.com rather than carrying it forward.

## 5. The display

Single page, three panes, all driven by discovery — nothing is hardcoded to a sensor.

```
┌───────────────────────────────────────────────────────────────────────┐
│ RAB Flight 2026-07-26          ● LIVE   3 sources · 2 reporters       │
├─────────────────────────────┬─────────────────────────────────────────┤
│  ALT 18,422 m  ↑ 5.2 m/s    │                                         │
│  ─────────────────────────  │            [ Leaflet map ]              │
│  RAB_HAT_ENV      2.0 Hz  ● │         track coloured by altitude      │
│    bmp_temp     -54.2 °C    │         ▲ launch  ✈ now  ★ predicted    │
│    bmp_pres      72.4 hPa   │                                         │
│    v_in           4.02 V    │                                         │
│  RAB_HAT_LSM6DSOX 5.0 Hz  ● ├─────────────────────────────────────────┤
│    accel_z        0.98 g    │  [ uPlot — multi-series, x = time|alt ] │
│  BALLOON2_ENV    stalled  ○ │                                         │
└─────────────────────────────┴─────────────────────────────────────────┘
```

- **Auto-discovering source list.** Sources appear as their first packet lands, grouped by
  payload, each with live rate, last-heard age, packet count, and a stalled indicator. Fields
  render with units seeded from the configurator's `UNITS` map
  ([configurator.html:466](../Wenet_BLE_Sensor/configurator.html#L466)); unknown fields show
  raw and can be given a unit/label inline, which persists.
- **Chart with an x-axis toggle: time or altitude.** The altitude mode is the payoff from
  ingesting GPS — a temperature-vs-altitude trace showing the tropopause is the plot people
  actually want from a sounding, and it's impossible without joining the two streams.
- **Map** with the track coloured by altitude, launch/current markers, and per-reporter
  reception overlay.
- **Reporter panel** — packets heard per callsign over time, so you can see which station was
  carrying the flight and when a handover happened.
- **Replay.** Because everything is persisted, a past flight scrubs through on the same page
  with the live indicator off. Same code path, `?flight=12&t=…`.

Theme-aware, responsive, readable on a phone in a chase vehicle in direct sun (high-contrast
mode, large-type option).

## 6. Changes to wenet_forwarder — **done**

Three changes, all contained. A fourth emerged from testing:

4. **GPS without a fix keeps everything except the position.** A payload
   transmits before it has a lock and those packets read 0,0 at zero altitude,
   which would drag the track into the Gulf of Guinea and corrupt every altitude
   interpolated near it. So the position fields are nulled — detected via
   `gpsFix` plus a zero-island check — and the packet is forwarded anyway: the
   battery, temperatures and satellite count it carries are most valuable
   precisely when the GPS is the thing failing. The server stores those rows and
   excludes them from the map and from altitude interpolation by testing for
   NULL, which it already did.

   One consequence worth knowing: a receiver that has never locked has no usable
   clock either, and its packets all decode to the same 1980 timestamp — the GPS
   dedup key. They would collapse to a single row. The forwarder drops an
   implausible timestamp and the server falls back to reception time, tagging
   the row `time_source: receiver`.

Deployment note: the spool directory **must be a mounted volume** under Docker.
On the container filesystem it is ephemeral, so a restart during an outage would
discard exactly the telemetry the spool exists to protect.


1. **Point at the new server** and switch `PUT` → `POST /api/v1/ingest` with the envelope above.
2. **Stop discarding GPS.** The filter at
   [wenet_forwarder.py:68](../wenet_forwarder/wenet_forwarder.py#L68)
   becomes a branch: `packet[0] == 0x03 and packet[1] == 55` → sensor (unchanged); `packet[0]
   == 0x01` → `gps_telemetry_decoder()` from `WenetPackets.py`. Include `raw` hex on the
   sensor branch.
3. **Disk-backed spool.** Today a failed batch retries 10× over ~40 s and is then **lost
   forever** ([wenet_forwarder.py:26-44](../wenet_forwarder/wenet_forwarder.py#L26-L44)),
   and `data_queue` is capped at 150 — a chase vehicle in an LTE hole silently drops
   telemetry. Since persistence is a requirement, unsent batches spool to a local file and
   drain when connectivity returns. This matters more than anything else in this list: it is
   the difference between a gap in the flight record and no gap.

## 7. Deployment — **written up in [DEPLOY.md](DEPLOY.md)**

Working configs live in [deploy/](deploy/). Two constraints emerged that the
sketch below did not anticipate, both silent failures if missed: uvicorn must
run **one worker** (the SSE hub is in-process, so a second worker serves
browsers that never receive the first one's events), and it needs
**`--proxy-headers`** or the per-IP ingest rate limit collapses every ground
station into one `127.0.0.1` bucket.


```
/opt/wenet-telemetry/          app + venv
/var/lib/wenet-telemetry/      telemetry.db  (WAL; nightly VACUUM INTO backup)
/etc/systemd/system/wenet-telemetry.service
/etc/nginx/sites-enabled/wenet-telemetry
```

- uvicorn on 127.0.0.1:8000, nginx terminates TLS (certbot), serves static assets directly,
  proxies `/api` and `/events` (with `proxy_buffering off` for SSE).
- systemd unit mirrors `wenet_ble.service`: `Restart=always`, journal logging.
- Backups: `VACUUM INTO` a timestamped copy nightly and after each flight ends — a consistent
  snapshot without stopping ingest.
- Sizing: this runs comfortably in 1 GB RAM. Storage is roughly 40 MB per flight-hour per
  source including the raw CBOR archive.

## 8. Build order

1. **Ingest + storage + dedup.** Prove `(source, time, count)` dedup against a replay of a real
   `sensor_data_*.log`.
2. **Forwarder changes** (GPS branch, new endpoint, spool). Run against a local server.
3. **Read API + SSE.**
4. **Frontend**: source list → charts → map → reporters → replay.
5. **Export + flight segmentation.**
6. **Deploy**: nginx/systemd/TLS, then a bench soak with a real payload before flight day.

A replay tool that plays a captured log at real speed into the ingest endpoint is worth
building at step 1 — it makes every later step testable without a balloon.

## 8a. What step 1 verified, and what it changed

Step 1 (ingest, storage, dedup, replay) is built and tested against the real
RAB-8 flight logs. 172,572 packets and 22,757 GPS points ingested with zero
rejects; the stored `pres` series reproduces the actual flight profile — 995 hPa
on the ground, 20.7 hPa minimum at altitude, 1000 hPa after landing.

Four things the real data changed or exposed:

1. **`payload_name` cannot be derived from `id` alone.** RAB-8 sent
   `id: "generic"` with the real name in a separate `payload` field. The split
   heuristic is now a fallback behind an explicit `payload`, and `source_key`
   is the only identity that matters.

2. **The field vocabulary really does change between firmware revisions.**
   RAB-8 flew `temp`/`pres`/`humi`; current firmware sends
   `bmp_temp`/`bmp_pres`/`bme_humi`. Both ingest without a migration, which is
   the whole point of fields-as-rows.

3. **A tight ingest rate limit converts a recovered outage back into data
   loss.** A forwarder draining its disk spool arrives as a burst of hundreds of
   batches. The limit defaults to 6000/min for that reason, and any client must
   treat 429 as retryable — the replay tool originally dropped those batches
   silently, which is exactly the bug the spool exists to prevent.

4. **Sensor readings and GPS never share a timestamp.** GPS runs ~1 Hz on the
   receiver's clock, sensors 2 Hz on the payload RTC. An equality join yields
   nothing, and bucketing with `substr()` defeats the index — the ad-hoc version
   took over two minutes across 1.4M readings.

   **Resolved:** altitude is now stored per packet in `packets.altitude`, filled
   by a backfill pass that interpolates between bracketing GPS fixes within a
   configurable window (default 10 s). Beyond the window the packet gets no
   altitude, so a GPS dropout shows as an honest gap rather than a smooth lie.
   The join is now fully indexed: **27 ms** over 171k readings, against >120 s
   for the ad-hoc `substr()` version.

   It had to be a backfill rather than an ingest-time lookup, and the spool test
   proved why: with the server down, GPS and sensor packets both queue to disk
   and arrive together on recovery, GPS *after* the samples it belongs with.
   A lookup at ingest would have left every one of those packets altitude-less.

   Two refinements fell out of testing. A packet is only resolved once GPS
   exists past it by a full window, since a nearer fix may still arrive and
   would give a better answer; and because that leaves the tail of a flight
   permanently pending, closing a flight flushes it against whatever track it
   ended up with.

## 9. Open questions

- ~~Is the Wenet GPS payload the same physical payload as the BLE sensors?~~ **Confirmed: the
  GPS transmitter is always on the same balloon string.** GPS is therefore keyed per flight,
  and any sensor reading may be joined to the altitude track. Revisit only if you ever fly two
  Wenet transmitters into one server.
- **Burst detection / predicted landing?** Ascent rate is in the GPS stream, so burst detection
  is nearly free. A landing prediction would mean pulling GFS data — real scope, out for now.
- **Retention.** Nothing is deleted under this design. At ~40 MB/flight-hour/source that's fine
  for years on a modest VPS, but say the word if you want a policy.
