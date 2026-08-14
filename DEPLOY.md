# Deployment — Debian + nginx

> **These configs have not been run on a Debian box.** They were written and
> reviewed here but the machine this was built on is a Windows dev PC, so
> nothing below has been executed end to end. Treat the first deploy as a test:
> work through [Verify](#verify) before trusting it with a flight.

Target: a small VPS. This runs comfortably in 1 GB RAM — the load is a few
hundred rows a second at worst and a handful of browsers.

## Two things that will bite you if skipped

**`--workers 1`, permanently.** The live SSE fan-out is an in-process hub, so a
second worker serves browsers that never receive events published by the first.
The display would look fine, sit there showing nothing, and reconnect cleanly —
the worst kind of failure. SQLite also wants one writer. If this ever needs to
scale, the answer is a shared broker and Postgres, not more workers.

**`--proxy-headers` on uvicorn.** The per-IP ingest rate limit keys on
`request.client.host`. Behind nginx without this, every ground station in the
world shares one bucket labelled `127.0.0.1`, and one busy station rate-limits
everyone else.

Both are already set in the unit file.

## 1. Packages and user

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx sqlite3 rsync
sudo apt install -y certbot python3-certbot-nginx

sudo useradd --system --home /var/lib/wenet-telemetry --shell /usr/sbin/nologin wenet
sudo mkdir -p /opt/wenet-telemetry /var/lib/wenet-telemetry/backups
sudo chown -R wenet:wenet /var/lib/wenet-telemetry
```

`sqlite3` is the CLI, needed by the backup script — not just the Python module.

## 2. Application

```bash
sudo git clone https://github.com/ke5gdb/wenet_telemetry_server.git /opt/wenet-telemetry
cd /opt/wenet-telemetry
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo chmod +x deploy/backup.sh
```

The code is read-only to the service; only `/var/lib/wenet-telemetry` is
writable (`ProtectSystem=strict` in the unit enforces that).

## 3. Configuration

```bash
sudo cp deploy/wenet-telemetry.env.example /etc/wenet-telemetry.env
sudo chown root:wenet /etc/wenet-telemetry.env
sudo chmod 640 /etc/wenet-telemetry.env
sudo nano /etc/wenet-telemetry.env
```

Read the comments in that file before moving on — particularly the one about
ingest being open by default.

## 4. Service

```bash
sudo cp deploy/wenet-telemetry.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wenet-telemetry
systemctl status wenet-telemetry
curl -s localhost:8000/api/v1/health
```

Logs go to the journal: `journalctl -fu wenet-telemetry`.

## 5. nginx and TLS

```bash
sudo cp deploy/nginx-wenet-telemetry.conf /etc/nginx/sites-available/wenet-telemetry
sudo sed -i 's/telemetry.example.com/YOUR.DOMAIN/' \
    /etc/nginx/sites-available/wenet-telemetry
sudo ln -s /etc/nginx/sites-available/wenet-telemetry /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d YOUR.DOMAIN
```

certbot rewrites the file for TLS and adds the :80 redirect. Renewal installs
its own timer; check it with `systemctl list-timers certbot.timer`.

The site config serves `static/` straight off disk and proxies only `/api` and
`/events`, so uvicorn never handles an asset request.

**The part that matters is the `/events` block.** `proxy_buffering off` is what
makes SSE stream; without it nginx holds messages until its buffer fills and the
display appears frozen and then jumps. gzip is off for that location for the
same reason — compression buffers.

## 6. Backups

```bash
sudo cp deploy/wenet-telemetry-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wenet-telemetry-backup.timer

sudo systemctl start wenet-telemetry-backup      # run one now
journalctl -u wenet-telemetry-backup --no-pager | tail
```

The script uses `VACUUM INTO`, which snapshots inside a read transaction, so the
copy is coherent while packets are still arriving. Copying the file directly can
catch a torn state — the `.db`, `.db-wal` and `.db-shm` are only consistent with
each other at a checkpoint. It then runs `integrity_check` on the result and
fails loudly if it does not pass, because a backup nobody has opened is a hope
rather than a backup.

Set `WENET_BACKUP_RSYNC_TARGET` to get the snapshots off the box. As shipped
they sit on the same disk as the database they protect, which does not survive
losing the VPS.

**Run one by hand after every flight**, before anyone starts editing flight
names or merging things.

## 7. Point the ground station at it

On the ground station, per
[wenet_forwarder/README.md](../wenet_forwarder/README.md):

```bash
export MYCALL=KE5GDB
export WENET_TELEMETRY_URL=https://YOUR.DOMAIN/api/v1/ingest
export WENET_SPOOL_DIR=/var/lib/wenet-forwarder/spool   # mount this if Dockerised
python wenet_forwarder.py
```

If you set `WENET_INGEST_API_KEY` on the server, set the matching
`WENET_API_KEY` here.

## Verify

Work through this before a flight depends on it.

```bash
# 1. API is up through nginx, not just on localhost
curl -s https://YOUR.DOMAIN/api/v1/health | python3 -m json.tool

# 2. Ingest accepts a packet
curl -s -X POST https://YOUR.DOMAIN/api/v1/ingest \
  -H 'Content-Type: application/json' \
  -d '{"reporter_id":"TEST","packets":[{"kind":"sensor","data":
      {"time":"20260726T120000.000Z","id":"DEPLOY_TEST","count":1,"v_in":4.0}}]}'
# -> {"accepted":1,...}

# 3. SSE actually streams. Leave this running and POST another packet from a
#    second terminal: the frame must appear immediately, not in a burst later.
curl -N https://YOUR.DOMAIN/events

# 4. Export streams and is well formed
curl -s https://YOUR.DOMAIN/api/v1/flights/1/export.csv | head -3

# 5. Backup runs and verifies
sudo systemctl start wenet-telemetry-backup && \
  journalctl -u wenet-telemetry-backup --no-pager | tail -3

# 6. Survives a reboot
sudo reboot   # then re-run 1
```

Then open the display in a browser and confirm the live badge goes green and
numbers move — the one thing that cannot be checked from a terminal.

Finally, rehearse the failure you will actually have: stop the server
(`systemctl stop wenet-telemetry`), leave the forwarder running for a few
minutes, confirm batches pile up in its spool directory, start the server again,
and confirm the spool drains to empty with no gap in the flight record.

## Sizing and retention

Roughly **10–15 MB per source-hour** including the raw CBOR archive — a
five-hour flight with three payloads lands near 200 MB. Nothing is deleted
automatically. A 40 GB VPS holds years of flights; if that ever changes, drop
old `raw_cbor` blobs first, since they are the bulk and the decoded data
survives without them.

## Redeploying

```bash
cd /opt/wenet-telemetry
sudo systemctl start wenet-telemetry-backup    # cheap; the migration is one-way
sudo git pull
sudo .venv/bin/pip install -r requirements.txt # only needed if requirements changed
sudo systemctl restart wenet-telemetry
```

Schema changes apply themselves at startup: new columns are added and backfilled
from the data already present (`app/db.py`).

### Then check the deploy actually took

A restart proves the *service* restarted, not that it is running the code you
just pulled — and it says nothing at all about the browser. Check both layers
separately, because they fail independently.

**Is the server running the new code?** Ask it for something only the new code
returns:

```bash
git -C /opt/wenet-telemetry log --oneline -1        # what the disk has
systemctl show -p ExecMainStartTimestamp wenet-telemetry   # when it last started

# does the API expose the field the new build added?
curl -s localhost:8000/api/v1/flights/1/latest | grep -o '"server_now"' \
  || echo "SERVER IS RUNNING OLD CODE"
```

If the field is missing, the service did not restart onto the new tree — check
`journalctl -u wenet-telemetry -n 50` for a start failure that left the old
process up.

**Is the browser running the new code?** This is the one that catches people
out: two of the three dashboard fixes live in `static/js/app.js`, and a browser
holding a cached copy keeps running the old logic against a correctly updated
server. The symptom is a partial fix — the parts implemented server-side work,
the parts implemented in the page do not.

```bash
curl -sI https://YOUR.DOMAIN/static/js/app.js | grep -iE 'cache-control|etag|last-modified'
# want: Cache-Control: no-cache  (revalidates every load)
```

Then hard-refresh the page: **Ctrl-Shift-R**, or Shift-Reload, or DevTools →
Network → *Disable cache*. On a phone, close the tab rather than trusting pull
to refresh.

If that header still reads `max-age=3600`, the nginx site file predates this
change — re-copy `deploy/nginx-wenet-telemetry.conf` and
`sudo nginx -t && sudo systemctl reload nginx`.

**Anyone watching the display should hard-refresh after every deploy.** The
`no-cache` header makes this automatic for anyone opening the page fresh, but a
tab that has been sitting open since before the deploy is still running whatever
it loaded then.

## Troubleshooting

**A deploy appears to have done nothing, or only half worked.** Almost always a
cached `static/js/app.js` — the server is new and the page is not, so fixes made
server-side land while fixes made in the page do not. Hard-refresh and see
[Redeploying](#redeploying). Confirm with
`curl -sI https://YOUR.DOMAIN/static/js/app.js | grep -i cache-control`.

**The display connects but never updates.** SSE is being buffered. Confirm
`proxy_buffering off` is in the `/events` location, that gzip is not applied to
it, and that nothing else — a CDN, another reverse proxy — sits in front.

**Live works for some viewers and not others.** `--workers` is above 1. See the
top of this file.

**Every ground station gets 429 at once.** `--proxy-headers` is missing, so they
all share the `127.0.0.1` rate-limit bucket.

**`attempt to write a readonly database`.** The `wenet` user cannot write
`/var/lib/wenet-telemetry`. It needs the *directory*, not just the file — WAL
mode creates `-wal` and `-shm` alongside.

**Ingest returns 413.** A batch exceeded `client_max_body_size` in nginx or
`WENET_MAX_BATCH_PACKETS` in the app. Raise whichever is lower.

**Timestamps look hours off.** The payload RTC is unset and the packets are
carrying boot-relative time; check the RTC per the payload README. The server
stores everything as UTC and assumes naive timestamps are UTC.

**A flight got split in two by a signal gap.** Expected, and repairable:
`POST /api/v1/flights/{keep}/merge?from={other}`. Raise
`WENET_FLIGHT_GAP_MINUTES` if it keeps happening.
