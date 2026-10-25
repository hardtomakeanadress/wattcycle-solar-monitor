# Dashboard implementation

See the [project README](../README.md) for setup and
[deployment guide](../docs/deployment.md) for automatic startup and backups.

`victron_shunt.py` optionally listens for Victron SmartShunt Instant Readout
broadcasts using existing BlueZ tools and Linux kernel AES (`AF_ALG`). Configure
`VICTRON_SHUNT_ADDRESS` and save the 32-hex-digit advertisement key as private
mode 0600 `victron.key` beside the database, or set `VICTRON_SHUNT_KEY_FILE`.
The key never appears in the page, status API or process arguments. No packages,
pairing, GATT writes or battery-setting changes are needed. The five-second
collector is independent of solar and climate collection. Only changed broadcast
frames refresh the measurement timestamp; startup ignores the old cached frame.
The battery panel marks readings stale after 30 seconds and shows shunt alarms.
The new `/api/status.shunt` object provides measured voltage, signed current,
state of charge, consumed Ah, estimated time to the configured discharge floor,
and optional auxiliary readings. Net battery power is calculated as voltage ×
current; positive is charging. Unavailable fields stay null, including SOC
before synchronization. These are live readings held in memory; shunt history
is not recorded in this release. Existing solar/climate history is unchanged.
Disconnect VictronConnect to allow measurement broadcasting. Format source:
[Victron Extra Manufacturer Data](https://communityarchive.victronenergy.com/storage/attachments/extra-manufacturer-data-2022-12-14.pdf).

`server.py` runs a single collector thread. It launches `read_controller.py`
every five minutes; each cycle drains all pending solar and climate readings
into one SQLite transaction, even when the controller read fails. Live values
update the cache immediately, but saved climate history can lag the live
reading by about five minutes. A failed transaction keeps its pending readings
for the next cycle and reports a storage error. Browser requests return cached
readings; opening more tabs does not create more Modbus pollers. The serial
connection is released between readings.
The integrated LOAD control path shares serial ownership with collection; command
readbacks and heating controller checks also save telemetry and flush pending history.

RAM retry buffers hold at most 10,080 readings per source (seven days at one
reading per minute; this is capacity, not polling frequency). A full buffer keeps
accepted readings and skips new history entries while live values keep updating.
`/api/status.history` exposes
`pending_limits`, `oldest_pending_age_seconds`, and `dropped_solar_samples` /
`dropped_sensor_samples` alongside pending counts. The page displays storage
warnings and keeps loss counts visible after recovery. Counts cover the
current process lifetime; they reset on restart.

When `BLUETOOTH_SENSOR_ADDRESS` is configured, `bluetooth_sensor.py` runs a
separate collector using the existing BlueZ command-line tools: every ten minutes
while heating is disarmed, or every five minutes while armed.
`/api/status` includes a `sensor` object with its own reading, error, age, and
staleness. Unconfigured installations hide the climate panel. Bluetooth reads
do not touch the serial adapter. Each successful read is cached immediately and
saved in the same SQLite database during the next five-minute cycle's
transaction. Storage failures keep the live reading and show an error; the
pending reading is retried on the next cycle. Climate readings become stale
after 30 minutes. A restart restores the latest committed reading and waits for
the remainder of its ten-minute interval before connecting again.
Heating requires a fresh reading on arming, uses a stricter 390-second stale
limit, and trips OFF on sensor errors. The supervisor checks cached state every
second without extra Bluetooth reads. See [heating behavior](../docs/heating.md).

The database retains all samples indefinitely. Its tables are:

```sql
CREATE TABLE samples (ts REAL PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE sensor_samples (
    sensor_address TEXT NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (sensor_address, ts)
);
CREATE TABLE solar_intervals (
    start_ts REAL PRIMARY KEY, interval_seconds INTEGER NOT NULL
);
```

For solar samples, `ts` is the collection attempt's Unix timestamp in seconds; `data` contains the
full JSON reading, including its own completion timestamp in UTC. Existing
records are never replaced on a timestamp collision. Correct system time is
needed for meaningful timestamps and the 24-hour graph.

Sensor `ts` is its actual notification receipt time, not the solar polling time.
Its JSON contains temperature, humidity, battery voltage, and UTC/Unix timestamps.
Startup adds the sensor table if missing without changing the solar table.
`solar_intervals` records collection cadence changes separately from samples.
Rows before the first known change use the historical 60-second cadence;
startup records the current cadence only when it changes. See the deployment
guide before upgrading a database already collected at five-minute intervals.

| Endpoint | Response |
| --- | --- |
| `GET /` | Self-contained dashboard; no external scripts or fonts |
| `GET /api/status` | Latest reading, error, freshness, and collection interval |
| `GET /api/system` | Allowlisted host specifications and health, cached for 30 seconds |
| `GET /api/history` | Legacy: last 24 hours, charge watts and battery volts, five-minute buckets |
| `GET /api/history/range?start=…&end=…` | Selected Unix time range, metric definitions, all measured scalar fields, storage/coverage metadata |
| `GET /api/history/range?range=all` | All saved dates using bounded display sampling |
| `POST /api/heating` | Authenticated arm/disarm request; 202 means accepted, status confirms the result |

`load_control.py` serves manual SSH requests through the monitoring process's
private Unix socket. `heating.py` supervises temperature, output ownership and
latched alarms. Heating is installation-locked by default and never automatically
re-arms after restart. Its private key and recovery metadata live beside the
database and must remain outside Git. Telemetry GET endpoints do not require a
login; control POST requests require the key. The HTTP server does not provide TLS.

The page offers presets and custom date ranges, with an overview, individual
metric selection, and all-metric mode. The range API selects the last timestamp
per display bucket in SQLite before loading JSON, returning at most 1,000
readings per source. It never loads years of raw JSON into memory at once. The timestamp
primary-key indexes support range filtering. Sensor series use their own
`sensor_rows` array and actual measurement timestamps (the metric's gap-display
cadence remains the ten-minute baseline);
cached sensor values are never duplicated into solar records.
The graph's display sampling does not expire or alter stored records. Retention
is indefinite, meeting the requested minimum of five years while storage is
available. See [history API details](../docs/history.md).

Malformed JSON or a non-object stored value becomes a gap at its original
timestamp. The range response includes `invalid_solar_rows` and
`invalid_sensor_rows`, counting unreadable selected records only, and the page
shows a warning. Original database rows and total sample counts are preserved.
The legacy endpoint emits `null` for missing or non-finite measurements.
An unreadable latest record cannot stop startup; the collector retries normally.

HTTP idle connections time out after 30 seconds. Range responses use a bounded
cache (eight exact queries, 30 seconds), invalidated after history commits.
Concurrent range requests are serialized, so identical concurrent queries share
one calculation. This does not limit arbitrary requests; retain trusted-network
access. No range limit or change to indefinite retention is introduced.

The interface keeps system sans-serif fonts, the dark green/lime palette, and
responsive cards. Charts use a two-column desktop layout and one column on
mobile, larger mobile axis labels, SVG point titles, and tap-to-inspect values.
All styles and scripts are bundled in `index.html`; no CDN or chart library is
required. Late responses from an earlier date selection are ignored.

Controller read failures preserve the previous reading, show a warning, and the
cycle still commits any pending readings. Storage failures show a distinct
warning while displaying the latest live value; accepted pending readings are retained
for retry on the next cycle, and no stored samples are deleted or rewritten.
Solar readings more than 15 minutes old are marked stale; climate readings
after 30 minutes. Requests for unavailable history return 503.

Database connections close after each operation. SQLite rollback journaling
with synchronous FULL is unchanged by the batching. A normal stop
(SIGTERM/Ctrl+C) stops the collectors and flushes pending readings; sudden
power loss can lose readings since the last successful commit, normally about
five minutes, longer if storage is failing. The project does not promise
protection against failing hardware or replace an off-device backup.


## Battery timers

The compact timer card shows the shunt's native time to its configured discharge
floor and a separate calculated time to **100%**. Direction icons distinguish
both rows; `≈` identifies the calculated charging value. Both timers stay visible;
there are no display switches or timer settings dialog.

`charge_timer.py` receives only fresh, changed shunt advertisements. It uses a
three-minute time-weighted average of net positive battery current, configured Ah capacity,
reported SOC and the configured charge-efficiency factor. A discharge or radio
gap resets the average. Charging ETA waits for fresh samples after startup,
pauses when charging stops, and disappears for stale/unknown data. A full SOC
shows a checkmark. Target charge is always 100%; it is not user-adjustable.

The private `charge-timer.json` beside the history database contains verified
`capacity_Ah`, `charge_efficiency_percent` and `discharge_floor_percent`.
Legacy display preferences are ignored.
Provision hardware metadata from actual shunt readback; do not guess it.
Cabana's metadata was read on 25 October 2026 (300 Ah, 95% efficiency, 30% floor).
If these hardware settings are changed separately, refresh that metadata.
`/api/status.timers` identifies the ETA source as `calculated`. There is no
writable timer endpoint; this module cannot change the shunt's floor or capacity.

This is a projection at the observed net current, not a solar-weather forecast.
It does not model charging-current taper, and an uncalibrated SOC affects its
accuracy. It does not replace the shunt's full-charge synchronization.
