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
retried during the next scheduled minute without restarting the service.

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

## Backups

The default database is `dashboard/history.sqlite3` inside the checkout.
If `SOLAR_DATABASE` is set, use that path instead. Reading frequency is once per
minute: about 525,600 samples per non-leap year if collection runs continuously.
Disk usage depends on the stored JSON and SQLite overhead; monitor free space.

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
| Graph timestamps wrong | Correct system time and time synchronization on the Pi |
| Browser cannot connect | Correct Pi address, port, interface binding, firewall, and active service |

An identical CH340 adapter may have the same non-unique by-id name. If you have
multiple adapters, identify the correct port carefully; a physical by-path
device name can be useful. USB reconnects can change `/dev/ttyUSB0` numbering.
