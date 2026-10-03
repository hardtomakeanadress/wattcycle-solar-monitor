# Cabana solar dashboard

Installed on cabana at `/home/PI_USER/WattCycle-RS485`.

- VPN: http://PI_TAILSCALE_IP:8080
- Local network (current address): http://PI_LAN_IP:8080

The Python standard-library server starts one reader subprocess every 60 seconds.
Browsers fetch cached readings every five seconds; they never access the serial port.
Only Modbus read requests are sent. Do not run the standalone reader while this
service is polling the same adapter.

SQLite keeps all samples indefinitely in `dashboard/history.sqlite3`, with no
automatic deletion or age limit. Every successful one-minute reading is retained
at its original resolution, including across service restarts. Available disk
space limits how much can be stored. The graph shows the last 24 hours with one
sample per five-minute bucket; this display window does not delete older records.
History starts with installation; long gaps in collection appear as gaps in the
chart. Old values remain visible with a warning after a read failure or when more
than three minutes old. A separate warning identifies a failure to save history,
while still displaying the latest live reading. Battery percent
is the controller estimate; the battery temperature field does not confirm an
external sensor is installed.

The dashboard listens on port 8080 on the Pi's network interfaces. It is a
read-only dashboard without login, intended for the local network or Tailscale;
no router port forwarding was configured.

The `cabana-solar.service` systemd user service is enabled for the Pi user. User lingering
is enabled so it starts at boot and stays running without an SSH login.

```sh
ssh PI_USER@PI_TAILSCALE_IP 'systemctl --user status cabana-solar'
ssh PI_USER@PI_TAILSCALE_IP 'journalctl --user -u cabana-solar -n 50'
ssh PI_USER@PI_TAILSCALE_IP 'systemctl --user restart cabana-solar'
```

To stop polling and disable startup:

```sh
ssh PI_USER@PI_TAILSCALE_IP 'systemctl --user disable --now cabana-solar'
```

API: `GET /api/status` returns the latest reading, error, age, and stale flag;
`GET /api/history` returns the graph samples. Other paths return 404.
No third-party Python packages, frontend packages, or Docker are required.

Run hardware-independent regression checks from the project directory:

```sh
python3 -m unittest discover -s dashboard -p 'test_*.py' -v
```

The tests cover serial responses and cleanup, polling intervals, database
transactions and closure, error recovery, API history, and multi-year retention.
`test_frontend.js` supplies DOM/network mocks for checking the inline page script
in a JavaScript engine; it is a development test and is not used by the dashboard.
