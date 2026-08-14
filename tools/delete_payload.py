#!/usr/bin/env python3
"""Delete every trace of one payload from the store.

A payload that was never on the balloon -- a bench rig left transmitting, a
neighbour's beacon the receiver happened to hear -- lands in the flight like any
other source and then sits in the source list, the exports and the tallies
forever. There is no API for removing it: ingest is append-only by design, since
"drop a packet" is the one thing a telemetry store must never do on its own.
So this is a deliberate, out-of-band operation.

Everything below a source is wired with ON DELETE CASCADE -- packets, fields,
readings, receptions -- so deleting the source rows is enough, *provided*
foreign keys are switched on. They are off by default in SQLite, which is the
trap this script exists to avoid: DELETE the sources with the pragma unset and
the packets stay behind, orphaned and invisible, still counted by the exports.

Dry run by default; pass --apply to write.

    python tools/delete_payload.py "XBnd RPT"
    python tools/delete_payload.py "XBnd RPT" --apply

Stop the server first. It caches source_key -> source_id in memory for the life
of the process, so a live server that hears the payload again would write
packets against a source_id that no longer exists.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

COUNTS = {
    "sources": """
        SELECT COUNT(*) FROM sources s WHERE {where}""",
    "packets": """
        SELECT COUNT(*) FROM packets p
         WHERE p.source_id IN (SELECT s.id FROM sources s WHERE {where})""",
    "fields": """
        SELECT COUNT(*) FROM fields f
         WHERE f.source_id IN (SELECT s.id FROM sources s WHERE {where})""",
    "readings": """
        SELECT COUNT(*) FROM readings r
         WHERE r.field_id IN (SELECT f.id FROM fields f
                               WHERE f.source_id IN (SELECT s.id FROM sources s
                                                      WHERE {where}))""",
    "receptions": """
        SELECT COUNT(*) FROM receptions rc
         WHERE rc.packet_id IN (SELECT p.id FROM packets p
                                 WHERE p.source_id IN (SELECT s.id FROM sources s
                                                        WHERE {where}))""",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("payload", help='payload_name to remove, e.g. "XBnd RPT"')
    ap.add_argument("--db", default=os.getenv("WENET_DB_PATH", "telemetry.db"),
                    help="database path (default: $WENET_DB_PATH or telemetry.db)")
    ap.add_argument("--flight", type=int, default=None,
                    help="limit to one flight id (default: every flight)")
    ap.add_argument("--apply", action="store_true", help="actually delete")
    ap.add_argument("--vacuum", action="store_true",
                    help="reclaim the freed pages afterwards (rewrites the file)")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"no such database: {path}", file=sys.stderr)
        return 1

    where = "s.payload_name = :payload"
    params: dict[str, object] = {"payload": args.payload}
    if args.flight is not None:
        where += " AND s.flight_id = :flight"
        params["flight"] = args.flight

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # The whole point: without this the cascades silently do nothing.
    conn.execute("PRAGMA foreign_keys=ON")

    rows = conn.execute(
        f"SELECT s.id, s.flight_id, s.source_key, s.packet_count FROM sources s "
        f"WHERE {where} ORDER BY s.flight_id, s.source_key", params).fetchall()
    if not rows:
        known = [r[0] for r in conn.execute(
            "SELECT DISTINCT payload_name FROM sources ORDER BY 1")]
        print(f'no sources with payload_name = {args.payload!r}')
        print("payloads present:", ", ".join(repr(k) for k in known) or "(none)")
        return 1

    print(f"database: {path}")
    print(f"payload:  {args.payload!r}")
    for r in rows:
        print(f"  flight {r['flight_id']}  {r['source_key']}  "
              f"{r['packet_count']:,} packets")

    print("\nrows that will go:")
    for name, sql in COUNTS.items():
        n = conn.execute(sql.format(where=where), params).fetchone()[0]
        print(f"  {name:<11} {n:>12,}")

    if not args.apply:
        print("\ndry run -- nothing written. Re-run with --apply to delete.")
        return 0

    with conn:   # one transaction; a failure part-way leaves the store untouched
        cur = conn.execute(f"DELETE FROM sources AS s WHERE {where}", params)
        print(f"\ndeleted {cur.rowcount} source row(s); cascades did the rest.")

    left = conn.execute(
        "SELECT COUNT(*) FROM packets WHERE source_id NOT IN (SELECT id FROM sources)"
    ).fetchone()[0]
    print(f"orphaned packets remaining: {left}")

    if args.vacuum:
        conn.execute("VACUUM")
        print("vacuumed.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
