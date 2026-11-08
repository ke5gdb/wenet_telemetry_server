"""SQLite connection management.

WAL mode gives us concurrent readers alongside the single writer, which is the
exact shape of this workload: one ingest path writing a few hundred rows a
second at worst, and any number of browsers reading.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "1"


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(path),
        check_same_thread=False,
        # We manage transactions explicitly via `with conn:` so that a whole
        # ingest batch commits or rolls back as one unit.
        isolation_level="DEFERRED",
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # A writer waits rather than failing outright if a reader is mid-query.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


#: Columns added after the first schema. CREATE TABLE IF NOT EXISTS will not add
#: a column to a table that already exists, so an existing database needs these
#: applied explicitly.
_ADDED_COLUMNS = [
    ("packets", "time_epoch", "REAL NOT NULL DEFAULT 0"),
    ("packets", "altitude", "REAL"),
    ("packets", "altitude_resolved", "INTEGER NOT NULL DEFAULT 0"),
    ("gps", "time_epoch", "REAL NOT NULL DEFAULT 0"),
    ("readings", "ts_epoch", "REAL NOT NULL DEFAULT 0"),
    # Nullable on purpose, and never backfilled: every row that predates the
    # receivers naming the transmitter genuinely has no callsign, and a spool
    # file drained hours after this deploy will still arrive without one. A
    # NOT NULL column here would reject exactly the backlog the spool exists
    # to protect.
    ("flights", "payload_callsign", "TEXT"),
    ("packets", "payload_callsign", "TEXT"),
    ("gps", "payload_callsign", "TEXT"),
    # Every source that predates this is a sensor source, which is what the
    # default says -- so no backfill is needed here either.
    ("sources", "kind", "TEXT NOT NULL DEFAULT 'sensor'"),
    ("flights", "hidden", "INTEGER NOT NULL DEFAULT 0"),
]

#: Unix epoch as a Julian day number, for converting stored ISO text in SQL.
_JULIAN_EPOCH = 2440587.5

#: Backfills for columns added to tables that already hold rows. Each runs once,
#: guarded by the sentinel value the ALTER left behind.
_BACKFILLS = [
    ("packets", "UPDATE packets SET time_epoch=(julianday(payload_time)-?)*86400.0 "
                "WHERE time_epoch=0"),
    ("gps", "UPDATE gps SET time_epoch=(julianday(timestamp)-?)*86400.0 WHERE time_epoch=0"),
    ("readings", "UPDATE readings SET ts_epoch=(julianday(ts)-?)*86400.0 WHERE ts_epoch=0"),
]


def init_db(conn: sqlite3.Connection) -> None:
    with conn:
        # Columns first: the schema script creates a partial index predicated on
        # altitude_resolved, which an older database does not have yet. On a
        # fresh database this is a no-op and CREATE TABLE does the work.
        _migrate(conn)
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )


def _migrate(conn: sqlite3.Connection) -> None:
    touched = set()
    for table, column, decl in _ADDED_COLUMNS:
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not info:
            continue  # fresh database; the schema script will create it whole
        if column not in {row[1] for row in info}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            touched.add(table)

    # An ALTER leaves existing rows at the default, which for an epoch column
    # would silently place every historic sample at 1970. Fill them from the
    # ISO text that is already there.
    for table, sql in _BACKFILLS:
        if table in touched:
            conn.execute(sql, (_JULIAN_EPOCH,))

    _close_duplicate_open_flights(conn)


def _close_duplicate_open_flights(conn: sqlite3.Connection) -> None:
    """Make an older database satisfy the one-open-flight-per-callsign index.

    The schema script would otherwise fail to create the index and the server
    would not start. The most recently active flight stays open; the others are
    closed at their own last activity, which is what the silence sweeper would
    have done. Their data is untouched -- merge them afterwards if they were
    the same flight.
    """
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='flights'"
    ).fetchone():
        return
    conn.execute(
        "UPDATE flights SET ended_at=last_activity WHERE ended_at IS NULL AND id NOT IN ("
        "  SELECT id FROM ("
        "    SELECT id, ROW_NUMBER() OVER ("
        "      PARTITION BY COALESCE(payload_callsign, '') "
        "      ORDER BY last_activity DESC, id DESC) AS rn "
        "    FROM flights WHERE ended_at IS NULL"
        "  ) WHERE rn = 1"
        ")"
    )


class ConnectionPool:
    """One connection per thread.

    SQLite connections are not safe to share across threads, and FastAPI runs
    sync work in a thread pool. Readers get their own connection; the single
    writer is serialised by Store's lock.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def get(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.path)
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    def close_all(self) -> None:
        with self._lock:
            for conn in self._all:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all.clear()
