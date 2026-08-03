#!/bin/bash
# Snapshot the telemetry database.
#
# `VACUUM INTO` takes the copy inside a read transaction, so the result is
# coherent even while packets are arriving. Copying the file directly can catch
# a torn state under WAL -- the .db, .db-wal and .db-shm are only consistent
# with each other at a checkpoint.
#
# Installed as a systemd timer; also safe to run by hand after a flight.

set -euo pipefail

DB="${WENET_DB_PATH:-/var/lib/wenet-telemetry/telemetry.db}"
DEST="${WENET_BACKUP_DIR:-/var/lib/wenet-telemetry/backups}"
KEEP="${WENET_BACKUP_KEEP:-14}"

if [[ ! -f "$DB" ]]; then
    echo "no database at $DB" >&2
    exit 1
fi

mkdir -p "$DEST"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$DEST/telemetry-$stamp.db"

# VACUUM INTO refuses to overwrite, which is the behaviour we want.
sqlite3 "$DB" "VACUUM INTO '$target'"

# Verify before trusting it. A backup nobody has opened is a hope, not a backup.
if ! sqlite3 "$target" "PRAGMA integrity_check" | grep -qx "ok"; then
    echo "integrity check FAILED for $target" >&2
    exit 1
fi

packets="$(sqlite3 "$target" "SELECT COUNT(*) FROM packets")"
gzip -f "$target"
echo "backed up $packets packets to $target.gz"

# Retention: keep the newest $KEEP, drop the rest.
mapfile -t old < <(ls -1t "$DEST"/telemetry-*.db.gz 2>/dev/null | tail -n "+$((KEEP + 1))")
for file in "${old[@]:-}"; do
    [[ -n "$file" ]] && rm -f -- "$file" && echo "pruned $(basename "$file")"
done

# These snapshots live on the same disk as the thing they protect. Copy them
# somewhere else -- a lost VPS takes both otherwise.
if [[ -n "${WENET_BACKUP_RSYNC_TARGET:-}" ]]; then
    rsync -a --delete "$DEST"/ "$WENET_BACKUP_RSYNC_TARGET"/
    echo "mirrored to $WENET_BACKUP_RSYNC_TARGET"
fi
