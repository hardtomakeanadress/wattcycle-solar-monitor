# Raspberry Pi deployment and maintenance

First complete the [quick start](../README.md#quick-start-on-raspberry-pi--linux)
and verify one read. These instructions assume the checkout is located at
`~/wattcycle-solar-monitor`. If it is elsewhere, edit the service's
`WorkingDirectory` and `ExecStart` paths accordingly.

## Automatic startup with systemd

Stop any foreground reader/server before starting the service. The service
runs as your existing Linux user, which needs access to the serial adapter.

```sh
mkdir -p ~/.config/systemd/user
cp dashboard/wattcycle-solar.service ~/.config/systemd/user/
```

Create an optional configuration file at
`~/.config/wattcycle-solar-monitor.env`. Use literal absolute paths; systemd
environment files do not expand `~` or `$HOME`.

```ini
SOLAR_PORT=/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0
SOLAR_SLAVE=1
DASHBOARD_PORT=8080
DASHBOARD_BIND=0.0.0.0
```

Choose the bind address for the intended clients. `0.0.0.0` preserves LAN and
VPN access but listens on every IPv4 interface; the dashboard has no login or
TLS, and reachable clients can read telemetry and host information. For VPN-only
access, set `DASHBOARD_BIND` to the Pi's own Tailscale IPv4 address (shown by
`tailscale ip -4`). That address must exist when the service starts. For a local
authenticated reverse proxy or SSH tunnel, use `127.0.0.1`. Binding to a specific
LAN address still allows other clients on that LAN; it is not authentication.
Do not forward port 8080 from the internet. The Cabana service can use the same
variable in a systemd user-service environment drop-in.

Start the service and enable it at login:

```sh
systemctl --user daemon-reload
systemctl --user enable --now wattcycle-solar
systemctl --user status wattcycle-solar
```

To start it at boot even when you are not logged in, enable user lingering:

```sh
sudo loginctl enable-linger "$USER"
```

Verify with `loginctl show-user "$USER" -p Linger`; the value should be `yes`.
The service restarts after an unexpected process failure. Serial read errors are
retried during the next scheduled cycle, five minutes later, without restarting
the service.

## Maintenance

```sh
systemctl --user restart wattcycle-solar
journalctl --user -u wattcycle-solar -n 50
df -h .
```

On distributions where the user journal is unavailable, use
`systemctl --user status wattcycle-solar` or ask the host administrator for
access to the system journal.

To stop collection and disable startup:

```sh
systemctl --user disable --now wattcycle-solar
```

To update the checkout, stop the service, run `git pull --ff-only`, run the test
suite, and start the service again. Back up the database before updates.

### Existing five-minute history

The `solar_intervals(start_ts, interval_seconds)` table records cadence changes
without modifying stored measurements. Startup adds a change at the current
time when no metadata exists, treating preceding records as legacy one-minute
history. If a database already contains five-minute readings from an earlier
version, import the **verified first five-minute sample timestamp** before
starting the upgraded service. Do not guess it from gaps, which may be outages.
With the service stopped and a backup made, use Python's `sqlite3` module to
create the table if absent and insert that timestamp with interval `300`.
Keep this metadata in backups. Cabana's verified transition was imported during
the October 2026 fix; subsequent restarts preserve it automatically.

## Backups

The default database is `dashboard/history.sqlite3` inside the checkout.
If `SOLAR_DATABASE` is set, use that path instead. Solar collection runs every
five minutes: about 105,120 samples per non-leap year if collection runs
continuously. Readings are committed in one SQLite transaction per cycle,
including pending climate readings; a normal stop flushes them, and a failed
transaction keeps them for the next cycle. The optional climate sensor adds
52,560 samples per year at ten-minute intervals in a second table in this same
file; a normal SQLite backup includes both sample tables and cadence metadata. Disk usage depends on the
stored JSON and SQLite overhead; monitor free space.

The retention target is **at least five years**. The application never purges
samples by age. At 105,120 samples per non-leap year, five average years hold
about 525,960 solar samples. The older one-minute database measured about
852 bytes per stored sample; applying that figure as an estimate gives roughly
**0.45 GB** of solar history over five years, with climate samples and OS,
application, and backup copies adding more. This installation had 25.7 GB free
when measured on 14 September 2026. Inspect the dashboard's storage figures and
`df -h` periodically; two full backup copies need roughly twice the live
database's size in additional storage.

Over the years, collection retries failed reads, keeps failed transaction
readings for the next cycle, and resumes at service startup. A normal
SIGTERM/systemctl stop or Ctrl+C stops the collectors and flushes pending
readings; sudden power loss can lose readings since the last successful commit,
normally about five minutes, longer if storage is failing. Storage uses SQLite
rollback journaling and synchronous FULL, unchanged by batching, and adds no new
dependency. An outage leaves a gap; the application cannot reconstruct readings it never
received. Standard OS maintenance such as package jobs, log rotation, temporary
file cleanup, and filesystem maintenance does not back up this database. Keep
an off-device backup and check that it can be restored. No automatic solar-data
backup, disk expansion, SD-card replacement, or recovery service is installed.

Pending RAM history is bounded to 2,016 solar and 1,008 climate readings
(seven days at their collection rates). When a source buffer is full, accepted
readings remain queued and newer history entries are skipped until storage
recovers; live values still update. The page shows pending counts, oldest age,
and skipped counts since the process started, including after recovery.
Counters and pending readings do not survive process restart. Repair storage
promptly; the buffer is not a replacement for a working disk or backups.

Make a consistent backup with Python's SQLite backup API, even while the
dashboard is collecting. Run from the repository root:

```sh
python3 - <<'PY'
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3

source_path = Path(os.environ.get('SOLAR_DATABASE', 'dashboard/history.sqlite3')).expanduser().resolve()
destination = Path('backups')
destination.mkdir(exist_ok=True)
backup_path = destination / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite3')
with closing(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)) as source:
    with closing(sqlite3.connect(backup_path)) as backup:
        source.backup(backup)
print(backup_path)
PY
```

If using a systemd environment file, its settings are not automatically loaded
into your shell; set `SOLAR_DATABASE` explicitly when backing up custom storage.
Copy backups to another machine or drive. A backup stored only on the same SD
card does not protect against that card failing. No automatic backup destination
is configured by this project.

To restore, stop the service, preserve the current database separately, copy the
chosen backup to the configured database path, and start the service. Ensure
the service user can read and write the restored file and parent directory.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Device path missing | `ls -l /dev/serial/by-id/`; verify USB connection |
| Permission denied | `id`; verify membership in `dialout`, then log out/in |
| Device busy / traffic present | Stop other readers; `fuser /dev/ttyUSB0` can help identify them |
| No reply / invalid CRC | Correct RS485 adapter and documented wiring, controller power, slave address, 9600 8N1 |
| Unexpected model | Do not assume another controller uses this register map; see compatibility documentation |
| History cannot be saved | Free disk space, database path/permissions, service logs |
| Stored readings shown as gaps | Back up the database, inspect unreadable records, and compare with an off-device backup; retries cannot repair damaged records |
| Graph timestamps wrong | Correct system time and time synchronization on the Pi |
| Browser cannot connect | Correct Pi address, port, interface binding, firewall, and active service |

An identical CH340 adapter may have the same non-unique by-id name. If you have
multiple adapters, identify the correct port carefully; a physical by-path
device name can be useful. USB reconnects can change `/dev/ttyUSB0` numbering.

History responses tolerate malformed JSON and JSON values that are not objects.
The range endpoint reports how many selected display records were unreadable;
these remain gaps and are never deleted or rewritten. This is not a full database
integrity scan: records not selected for display are not decoded. If SQLite's
JSON functions are available, inspect malformed solar records with the read-only
query `SELECT ts FROM samples WHERE NOT json_valid(data);`, and climate records
with `SELECT sensor_address, ts FROM sensor_samples WHERE NOT json_valid(data);`.
Valid JSON with an unexpected structure also needs inspection. Startup logs an
unreadable latest record and resumes collection without restoring that value.

HTTP connections time out after 30 seconds of socket inactivity. Identical
history-range queries share a 30-second in-process response cache with at most
eight entries, cleared after a successful history commit. Queries with different
date bounds are separate entries; this is not a request-rate limit or protection
against arbitrary expensive queries. Queries still scan the selected interval,
and all saved dates remain accessible. Keep access limited to trusted clients.
