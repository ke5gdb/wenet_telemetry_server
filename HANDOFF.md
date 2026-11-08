# Handoff notes

Working state as of **2026-07-31**, plus the dashboard clock and reporter-tally
fixes that followed it. [DESIGN.md](DESIGN.md) is the architecture,
[README.md](README.md) the usage, [DEPLOY.md](DEPLOY.md) the server build. This
file is the connective tissue: what is done, what is *proven* done, what is not,
and the decisions whose reasoning does not survive in a diff.

Read the two sections that will cost you time otherwise: **Traps** and
**Where the reasoning lives**.

---

## 1. Repos

| Repo | Role | State |
|---|---|---|
| `wenet_telemetry_server` | server, display, exports | `ke5gdb/wenet_telemetry_server`, committed |
| `wenet_forwarder` | ground-station forwarder | `ke5gdb/wenet_telemetry_forwarder`, committed |
| `balloonatics` | ground station (`k5rwk`, branch `gpsl`) | **still holds an old copy** of the forwarder |

**The balloonatics copy will drift.** `ground_station/wenet_forwarder/` was
deliberately left in place, and it has uncommitted changes there from before the
split. Everything since — the compose file, the `WENET_UDP_PORT` fallback, the
no-fix GPS behaviour — landed only in the standalone repo. Decide which is
canonical before someone fixes a bug in the wrong one.

Upstream, unchanged, referenced for decoding: `Wenet_BLE_Sensor` (payload
firmware), `wenet_ble_scripts` (balloon-side BLE bridge), `wenet` (modem, packet
formats).

---

## 2. What is built

All six planned steps. 116 tests pass (`pytest tests/ -q`).

1. **Ingest, storage, dedup** — `app/store.py`, `app/schema.sql`
2. **Forwarder** — separate repo; GPS decode, raw-hex upload, disk spool
3. **Read API + live SSE** — `app/main.py`, `app/hub.py`
4. **Display** — `static/`, no build step, no dependencies
5. **Export + flight admin** — `app/export.py`
6. **Deployment guide** — `DEPLOY.md`, `deploy/`

---

## 3. What is actually verified, and how

Being precise here matters more than the feature list.

### Proven against real data

| Thing | Evidence |
|---|---|
| Ingest, dedup, discovery | 172,572 packets from the RAB-8 logs; 13,038 from a real `sensor_data_*.log`; zero rejects |
| Stored data is faithful | RAB-8 `pres` reproduces the flight: 995 hPa ground → 20.7 hPa → 1000 hPa after landing |
| Forwarder against a real receiver | Your bench run: 2,084 packets, 511 GPS fixes, 0 rejects, all raw CBOR archived |
| Real Wenet GPS decode | Real fix `32.96681,-96.72942 alt 170.2 m, 26 sats`, plus every extended housekeeping field |
| Two firmware vocabularies at once | Same flight carried `hdc_temp`/`max31725_temp` **and** `temp`/`pres`/`humi`/`vbatt` |
| Altitude interpolation | Matches the barometric formula to 0.00–0.18%; join 27 ms over 171k readings |
| Spool survives an outage | Server killed mid-run: 60 packets spooled, drained with no loss; late GPS backfilled altitudes onto already-stored packets |
| SSE | 100 frames captured off the wire during a 60× replay; headers correct |
| Exports | All six formats; CBOR byte-identical round trip; 15.5 MB snapshot passes `integrity_check` |
| Split / merge | Round trip conserved every packet and GPS fix; series still readable |
| Merging the prod twin flights | All 8 pairs on a 613 MB prod snapshot (2026-11-08): every distinct packet, fix and station reception kept, one GPS source per flight, `integrity_check` ok |
| Twin flights no longer form | Replay of the 2026-10-23/24 receptions in arrival order: the untagged start is claimed by K5RWK, no untagged flight |
| No-fix GPS | 18 no-fix + 27 fixed through the full chain; 0 rejects, none mapped, interpolation uncorrupted |

### Not verified — do these before trusting a flight

1. **The live display has never been looked at by a human.** Everything about
   the live path is proven server-side (SSE frames, `latest`, the update
   payload), but the rendering of a *live* flight is unseen: headless Edge
   cannot screenshot it, because an open SSE stream means the page never reaches
   an idle state. Replay views are screenshotted and correct.
   *Do this first — it is ten minutes.* Start the server, run
   `python tools/fake_receiver.py --duration 120`, open the page, watch the
   badge go green and the numbers move.
2. **Drag-to-zoom and the shared cursor line.** Need a mouse. The shared
   x-domain they rely on is verified visually; the interactions are review-only.
3. **Everything in `DEPLOY.md`.** No Debian, nginx, or systemd on the dev box.
   The backup script's SQL and retention logic are tested; the units and nginx
   config are not.
4. **The Docker image never built** — daemon not running here. `docker compose
   config` validates; `build` and `up` are untested.
5. **No balloon has flown.** Every altitude profile so far is synthetic GPS or
   real GPS sitting on the ground at ~170 m. The sounding view is mechanically
   correct and physically unexercised.

---

## 4. Traps

Things that fail *silently*. Each cost real time or nearly did.

- **`--workers 1` on uvicorn, permanently.** The SSE hub is in-process. A second
  worker serves browsers that never receive the first worker's events: green
  badge, no data, clean reconnects.
- **`--proxy-headers`.** The per-IP rate limit keys on client address; behind
  nginx without it every ground station shares the `127.0.0.1` bucket.
- **nginx `proxy_buffering off` on `/events`**, and gzip off there. Otherwise the
  display freezes and jumps.
- **The spool must be a mounted volume under Docker**, or a restart during an
  outage discards exactly what the spool exists to protect.
- **`.dockerignore` must exclude `spool/`.** The Dockerfile ends in `COPY . .`;
  building with a populated spool bakes stale batches into the image, and every
  container from it replays them.
- **Rate limits sized for spool drain, not steady state.** A forwarder draining
  an outage arrives as hundreds of batches. Too tight and a recovered outage
  becomes permanent loss. Clients must treat 429 as retryable.
- **A never-locked GPS has no clock.** Its packets decode to 1980-01-05 — which
  is the GPS dedup key, so they would all collapse to one row. Handled
  (`time_source: receiver`), but do not "simplify" it away.
- **Payload `id` is not decomposable.** RAB-8 sent `id: "generic"` with the real
  name in a separate `payload` field. `source_key` is the only identity.
- **Never measure age against `time_epoch`.** That is the payload's own RTC,
  which is real time only if a PCF8523 was found at boot; otherwise it restarts
  at 2021-01-01 every power cycle. Staleness uses `heard_epoch` (the server's
  reception clock) corrected by `server_now` for browser skew. Comparing two
  different clocks is what produced a source reading `STALLED · 0.5 Hz` while
  every value on the row kept updating. `rate_hz` is *correctly* on the payload
  clock — a rate is a difference measured on one clock, so it is unaffected.
- **The rate is over the last `RATE_SAMPLES` packets, not a fixed window.**
  Sources here run 0.05–2 Hz and no single window serves both: the old 60 s
  count quantised a slow source to multiples of 1/60. It follows that a source
  that has stopped keeps reporting the rate it had — "still transmitting?" is a
  separate question, answered by `heard_epoch`, and the display drops the figure
  entirely once a source reads STALLED rather than printing both at once.
- **Live figures come from re-fetching `/latest`, not from the event stream.**
  One SSE event carries the *newest* packet per source, not the thirty that were
  in the batch, so counting events undercounts packets and says nothing about
  rate. The 5 s refresh re-asks the server; before it did, the rate and the
  packet count on screen were whatever they had been at page load.
- **GPS receptions live in their own table.** Anything counting per-callsign
  activity must union `receptions` and `gps_receptions`. Joining only the former
  dropped a GPS-only station out of the reporters panel completely.

- **A missing callsign is not a different transmitter.** The Wenet receiver
  learns the callsign from SSDV image packets (`rx_ssdv.py`, `current_callsign`
  starts as `""`), so everything before the first decoded image is untagged, and
  receivers older than wenet `22f3779` never tag at all. Treating NULL as its own
  identity gave every prod flight from 2026-08-16 to 2026-10-24 an untagged twin
  running beside it with part of its telemetry. Untagged traffic is now
  attributed (`Store._attribute_untagged`), the first tagged packet claims an
  untagged start (`_claim_untagged`), and a partial unique index allows one open
  flight per callsign. Do not put the "NULL is its own flight" rule back.
- **Every FK child column needs an index.** `readings.packet_id` had none, so
  each packet deleted by a merge's dedup scanned all 3.7M readings for its
  cascade. Merging two real flights went from hours to seconds with
  `idx_readings_packet`.

### Local dev gotchas

- `max_points` has `ge=10`; passing less returns 422, which looks like a server
  bug in an ad-hoc script. Tripped this three times.
- `TestClient` + SSE deadlocks (one portal serves both the stream and the nested
  request). `/events` is deliberately untested there — see the note in
  `tests/test_read_api.py`.
- Headless screenshots need a **closed** flight, or virtual time never drains.
- Windows console mangles `°`/`µ`; set `PYTHONIOENCODING=utf-8`. The database is
  fine.
- Git Bash passes POSIX paths that the Windows Python cannot open.

---

## 5. Where the reasoning lives

Decisions whose "why" is load-bearing and non-obvious. All are commented in
place; this is the index.

| Decision | Why | Where |
|---|---|---|
| Fields are rows, not columns | The vocabulary genuinely changes between firmware revisions, and DS18X20 names itself from its ROM at runtime | `app/schema.sql` |
| Dedup key `(source, payload_time, count)` | `count` wraps at 65536 and resets on reboot; an unset RTC replays timestamps. Neither alone is safe | `app/store.py` |
| Receptions stored separately from packets | Several stations hearing one transmission is diversity, not duplicate data. Stored naively it triple-plots every series | `app/schema.sql` |
| Altitude by backfill, not lookup at ingest | GPS and sensor packets travel separate paths; GPS routinely arrives *later*, and after a spool drain it arrives hours later | `app/store.py` |
| 10 s interpolation window | Beyond it the figure stops being a measurement. A gap is honest; a smooth line is a lie | `app/store.py` |
| Resolve only once GPS passes by a full window | A nearer fix may still be arriving; resolving early locks in a worse answer. Hence the flush on flight close | `app/store.py` |
| Small multiples, one series each | Discovered fields have unrelated units; shared axes would be the dual-axis anti-pattern | `static/js/chart.js` |
| One shared x-domain across charts | Per-chart domains put the same instant at different screen positions, making a "shared" cursor a lie | `static/js/app.js` |
| Chart and map written by hand | No outbound network to vendor uPlot/Leaflet, and an untested CDN reference is worse than code that can be tested | `static/js/` |
| Merge exists; split is secondary | The gap heuristic's real failure is *over*-splitting on lost signal. Splitting wrongly is easy and otherwise permanent | `app/store.py` |
| No-fix GPS kept, position nulled | The housekeeping is most valuable when the GPS is what is failing | `wenet_packets.py` |

---

## 6. Running it

```bash
# server
WENET_DB_PATH=telemetry.db .venv/Scripts/python -m uvicorn app.main:app --port 8000
#   ... on the LAN, add --host 0.0.0.0   (127.0.0.1 is the default and bit us once)

# tests
.venv/Scripts/python -m pytest tests/ -q

# replay a capture into the server (tests the server)
python tools/replay.py sensor_data_2026-07-26_15-55-35.log --speed 0 \
    --reporters KE5GDB,K5NBC --hear-rate 0.85 --synth-gps
#   bulk replay wants WENET_RATE_LIMIT_PER_MINUTE=0 on the server

# emit Wenet UDP frames as the receiver would (tests the forwarder)
python tools/fake_receiver.py --duration 60 --rate 2 --nofix-seconds 10

# forwarder
cd ../wenet_forwarder && python wenet_forwarder.py \
    --callsign KE5GDB --url http://127.0.0.1:8000/api/v1/ingest
```

Linkable views: `?flight=12`, `?axis=altitude`, `?theme=light`, `?range=300`,
`?from=…&to=…`.

### Test data on hand

- `sensor_data_2026-07-26_15-55-35.log` — real capture, 13,357 lines, two
  sources. The parser tests embed verbatim lines from it, so the suite does not
  need the file.
- `../BLE Data Analysis/RAB-8/*.json` — 2025 flight logs, older field
  vocabulary. Good schema-drift stress.
- `telemetry.db` — **your real bench run**, 2,084 packets and 511 real GPS
  fixes. Gitignored. Worth keeping; it is the only real-GPS data in existence
  for this system.

---

## 7. Open items

**Before a flight**

1. Look at the live display in a real browser (§3).
2. Deploy and work through the `DEPLOY.md` verification list, including the
   spool-drain rehearsal.
3. Decide on ingest auth. It is open by default: anyone who finds the endpoint
   can inject telemetry into the live display and the exports. One env var
   (`WENET_INGEST_API_KEY`) turns it on, no code change either side.
4. Rotate the old API key still committed in the balloonatics history.

**Known gaps, none blocking**

- Pre-lock GPS packets are keyed on reception time, so several stations hearing
  the same one may store it twice. Only affects a receiver that has *never*
  locked. Fix if it matters: dedup those rows on a hash of the raw packet bytes,
  which are identical across receivers.
- The left-hand source list keeps showing *latest* values while a chart window
  is open — deliberate (it is a status panel), easily reversed.
- Altitude-axis drag zooms the view without re-querying, so it gains no
  resolution; the time axis does re-query.
- No retention policy. ~10–15 MB per source-hour. Drop `raw_cbor` first if it
  ever matters.
- Landing prediction was considered and left out — it needs GFS data.

**Questions I never got a firm answer to**

- Will more than one ground station ever report a flight? The dedup and coverage
  work assumes yes; nothing breaks if the answer is no.
- Two Wenet transmitters on one string would break the per-flight GPS keying.
  Confirmed as not a case today.

---

## 8. If you are an AI agent picking this up

Read `DESIGN.md` §8a first — it records what the real data changed about the
original design, which is where most of the non-obvious constraints came from.

Do not trust a feature list over the verification table in §3. Several things
look finished and are only *mechanically* finished — the sounding view has never
seen a real balloon, and the live display has never been seen by a human at all.

When you change ingest or dedup, run the suite before anything else: those tests
encode failure modes that are invisible in a single-station bench test — count
wrap, RTC replay after reboot, and the same transmission arriving from three
callsigns.
