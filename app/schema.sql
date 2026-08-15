-- Wenet telemetry store.
--
-- Design note: the packet format is not knowable in advance. Fields are named
-- at runtime by the firmware (DS18X20 sensors name themselves from their ROM),
-- and the vocabulary changes between firmware revisions -- RAB-8 flew `temp`,
-- `pres`, `humi` where current firmware sends `bmp_temp`, `bmp_pres`,
-- `bme_humi`. So fields are ROWS, not columns, and a new sensor never needs a
-- migration.
--
-- Two representations are kept deliberately:
--   packets.data / packets.raw_cbor  lossless archive, for export and re-decode
--   readings                         narrow indexed numeric series, for charts

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- A flight scopes everything, so "export the data" is unambiguous once you have
-- flown more than once. Segmented automatically on a silence gap.
CREATE TABLE IF NOT EXISTS flights (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  last_activity TEXT NOT NULL,
  ended_at      TEXT,
  notes         TEXT NOT NULL DEFAULT '',
  -- The Wenet transmitter this flight belongs to. A flight is one transmitter's
  -- up-burst-down, so identity is (callsign, silence gap) rather than time
  -- alone: two balloons in the air at once, or a neighbour's beacon the
  -- receiver happens to hear, are separate flights however much they overlap.
  -- NULL is its own identity, not a wildcard -- it means the receiver never
  -- named the transmitter (older firmware, or a spool file written before the
  -- field existed), which cannot be assumed to be any callsign in particular.
  payload_callsign TEXT
);
CREATE INDEX IF NOT EXISTS idx_flights_open ON flights(ended_at, last_activity);

-- One row per distinct packet `id` string. One physical payload emits several
-- of these (sensor_task -> RAB_HAT_ENV, sensor_task_lsm6dso -> RAB_HAT_LSM6DSOX),
-- each with its own count sequence and its own rate, so the source -- not the
-- payload -- is the unit that owns a series.
CREATE TABLE IF NOT EXISTS sources (
  id           INTEGER PRIMARY KEY,
  flight_id    INTEGER NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
  source_key   TEXT NOT NULL,              -- the packet's `id`, authoritative
  payload_name TEXT NOT NULL,              -- display hint, best effort
  task_name    TEXT NOT NULL DEFAULT '',   -- display hint, best effort
  first_seen   TEXT NOT NULL,
  last_seen    TEXT NOT NULL,
  packet_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(flight_id, source_key)
);

-- One row per unique packet -- NOT per reception. See receptions below.
CREATE TABLE IF NOT EXISTS packets (
  id           INTEGER PRIMARY KEY,
  source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  payload_time TEXT NOT NULL,              -- sample time, from the payload RTC
  count        INTEGER NOT NULL,
  time_epoch   REAL NOT NULL,              -- payload_time as epoch seconds
  data         TEXT NOT NULL,              -- decoded dict, JSON
  raw_cbor     BLOB,                       -- exactly what came off the air
  truncated    INTEGER NOT NULL DEFAULT 0,
  first_heard  TEXT NOT NULL,              -- earliest reception, server clock
  -- What the receiver said about this packet's transmitter. Redundant with the
  -- flight's callsign by construction, and kept anyway: it is the raw fact,
  -- and it survives a merge or a split that rewrites which flight owns the row.
  payload_callsign TEXT,
  -- Altitude interpolated from the GPS track. Resolved by a backfill pass
  -- rather than at ingest, because GPS and sensor packets travel separate
  -- paths and GPS routinely arrives later than the samples it belongs with.
  -- NULL with altitude_resolved=1 means "decided, and no fix was near enough".
  altitude          REAL,
  altitude_resolved INTEGER NOT NULL DEFAULT 0,
  -- The dedup key. `count` alone is unsafe: it wraps at 65536 and resets on
  -- reboot. `payload_time` alone is unsafe: an unset RTC restarts at 2021-01-01
  -- on every boot, so a power cycle replays identical timestamps. The pair is
  -- unique in practice.
  UNIQUE(source_id, payload_time, count)
);
CREATE INDEX IF NOT EXISTS idx_packets_source_time ON packets(source_id, payload_time);
-- Partial index: in steady state only a handful of packets await GPS, so the
-- backfill's "what is still pending" query stays cheap however long the flight.
CREATE INDEX IF NOT EXISTS idx_packets_pending_alt
  ON packets(time_epoch) WHERE altitude_resolved = 0;

-- Every ground station that heard a given packet. Several stations hearing the
-- same transmission is normal and desirable -- it is receive diversity, not
-- duplicate data -- so it is recorded here rather than as repeated samples.
CREATE TABLE IF NOT EXISTS receptions (
  packet_id   INTEGER NOT NULL REFERENCES packets(id) ON DELETE CASCADE,
  reporter_id TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY (packet_id, reporter_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_receptions_reporter ON receptions(reporter_id, received_at);

-- Fields discovered per source, with display metadata.
CREATE TABLE IF NOT EXISTS fields (
  id          INTEGER PRIMARY KEY,
  source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  unit        TEXT NOT NULL DEFAULT '',
  label       TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  kind        TEXT NOT NULL DEFAULT 'numeric',  -- 'numeric' | 'text'
  user_edited INTEGER NOT NULL DEFAULT 0,       -- protects hand-set units from reseeding
  first_seen  TEXT NOT NULL,
  UNIQUE(source_id, name)
);

-- The narrow series the charts read. Numeric fields only.
CREATE TABLE IF NOT EXISTS readings (
  field_id  INTEGER NOT NULL REFERENCES fields(id) ON DELETE CASCADE,
  ts        TEXT NOT NULL,                 -- = packets.payload_time, for export
  ts_epoch  REAL NOT NULL DEFAULT 0,       -- the same instant, for the charts
  value     REAL NOT NULL,
  packet_id INTEGER NOT NULL REFERENCES packets(id) ON DELETE CASCADE
);
-- The chart query is the hot path of the whole system. Bucketing for downsample
-- is arithmetic on the timestamp, and doing that arithmetic on the ISO text
-- means a function call per row, which is what made the first altitude join
-- take two minutes. Eight bytes a row buys an index that serves both.
CREATE INDEX IF NOT EXISTS idx_readings_series ON readings(field_id, ts_epoch);

-- Wenet GPS_TELEMETRY (packet type 0x01). Keyed per flight: the GPS transmitter
-- is assumed to be on the same balloon string as the BLE sensors, which is what
-- makes a sensor-vs-altitude plot meaningful.
CREATE TABLE IF NOT EXISTS gps (
  id           INTEGER PRIMARY KEY,
  flight_id    INTEGER NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
  timestamp    TEXT NOT NULL,
  time_epoch   REAL NOT NULL,              -- epoch seconds, for the altitude join
  latitude     REAL,
  longitude    REAL,
  altitude     REAL,
  ascent_rate  REAL,
  ground_speed REAL,
  heading      REAL,
  num_sv       INTEGER,
  gps_fix      INTEGER,
  extra        TEXT NOT NULL DEFAULT '{}', -- batt_v, cpu_temp, ... as JSON
  first_heard  TEXT NOT NULL,
  payload_callsign TEXT,
  -- Safe because one transmitter emits one GPS stream and a flight is one
  -- transmitter: two transmitters sharing a second land in different flights,
  -- so this no longer discards the second one's fix.
  UNIQUE(flight_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_gps_flight_ts ON gps(flight_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_gps_flight_epoch ON gps(flight_id, time_epoch);

CREATE TABLE IF NOT EXISTS gps_receptions (
  gps_id      INTEGER NOT NULL REFERENCES gps(id) ON DELETE CASCADE,
  reporter_id TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY (gps_id, reporter_id)
) WITHOUT ROWID;

-- Anything that could not be filed. Persistence was a hard requirement, so a
-- malformed packet is kept for inspection rather than dropped on the floor.
CREATE TABLE IF NOT EXISTS rejects (
  id          INTEGER PRIMARY KEY,
  received_at TEXT NOT NULL,
  reporter_id TEXT NOT NULL DEFAULT '',
  reason      TEXT NOT NULL,
  raw         BLOB,
  data        TEXT
);
