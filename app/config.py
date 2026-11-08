"""Runtime configuration, all via environment variables."""

from __future__ import annotations

import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


#: Where the database lives. A single file -- backing up a flight is a copy.
DB_PATH = Path(os.getenv("WENET_DB_PATH", "telemetry.db"))

#: Silence longer than this starts a new flight.
FLIGHT_GAP_MINUTES = _int("WENET_FLIGHT_GAP_MINUTES", 30)

#: Ingest authentication. Unset by default -- ingest is open, as specified.
#: Setting it turns on enforcement of the X-API-KEY header that wenet_forwarder
#: already sends, with no code change on either side.
INGEST_API_KEY = os.getenv("WENET_INGEST_API_KEY") or None

#: Per-IP rate limit on ingest. Steady-state a ground station sends about 12
#: requests a minute, so this is nowhere near it -- the headroom is sized for a
#: forwarder draining a disk spool after an outage, which arrives as a burst of
#: hundreds of batches. Setting this too tight turns a recovered outage back
#: into permanent data loss, which is the exact failure the spool exists to
#: prevent. Set to 0 to disable.
RATE_LIMIT_PER_MINUTE = _int("WENET_RATE_LIMIT_PER_MINUTE", 6000)

#: How far a sensor packet may reach for a GPS fix when resolving its altitude.
#: A balloon climbs about 50 m in 10 s, so beyond this the interpolated figure
#: stops being a measurement and starts being a guess -- the packet is left with
#: no altitude instead, which is honest and visibly a gap on a plot.
ALTITUDE_WINDOW_SECONDS = _float("WENET_ALTITUDE_WINDOW_SECONDS", 10.0)

#: Password for the admin panel and every write endpoint other than ingest --
#: rename, close, merge, split, hide, field edits. Unset, those are refused
#: outright rather than left open.
ADMIN_PASSWORD = os.getenv("WENET_ADMIN_PASSWORD") or None

#: How long an admin login lasts before the panel asks again.
ADMIN_SESSION_HOURS = _int("WENET_ADMIN_SESSION_HOURS", 168)

#: Refuse absurd batches outright rather than parsing them.
MAX_BATCH_PACKETS = _int("WENET_MAX_BATCH_PACKETS", 2000)
