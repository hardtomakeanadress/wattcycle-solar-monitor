# Dashboard implementation

See the [project README](../README.md) for setup and
[deployment guide](../docs/deployment.md) for automatic startup and backups.

`server.py` runs a single collector thread. It launches `read_controller.py`
once per minute and saves each successful JSON snapshot in SQLite. Browser
requests return cached readings; opening more tabs does not create more Modbus
pollers. The serial connection is released between readings.

When `BLUETOOTH_SENSOR_ADDRESS` is configured, `bluetooth_sensor.py` runs a
separate ten-minute collector using the existing BlueZ command-line tools.
`/api/status` includes a `sensor` object with its own reading, error, age, and
staleness. Unconfigured installations hide the climate panel. Bluetooth reads
do not touch the serial adapter. Each successful read is saved once in the
same SQLite database and cached for the page. Storage failures keep the live
reading and show an error; failed writes are not buffered for replay. Readings
become stale after 30 minutes. A restart restores the latest saved reading and
waits for the remainder of its ten-minute interval before connecting again.

The database retains all samples indefinitely. Its tables are:

```sql
CREATE TABLE samples (ts REAL PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE sensor_samples (
    sensor_address TEXT NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (sensor_address, ts)
);
```

For solar samples, `ts` is the collection attempt's Unix timestamp in seconds; `data` contains the
full JSON reading, including its own completion timestamp in UTC. Existing
records are never replaced on a timestamp collision. Correct system time is
needed for meaningful timestamps and the 24-hour graph.

Sensor `ts` is its actual notification receipt time, not the solar polling time.
Its JSON contains temperature, humidity, battery voltage, and UTC/Unix timestamps.
Startup adds the sensor table if missing without changing the solar table.

| Endpoint | Response |
| --- | --- |
| `GET /` | Self-contained dashboard; no external scripts or fonts |
| `GET /api/status` | Latest reading, error, freshness, and collection interval |
| `GET /api/history` | Legacy: last 24 hours, charge watts and battery volts, five-minute buckets |
| `GET /api/history/range?start=…&end=…` | Selected Unix time range, metric definitions, all measured scalar fields, storage/coverage metadata |
| `GET /api/history/range?range=all` | All saved dates using bounded display sampling |

The page offers presets and custom date ranges, with an overview, individual
metric selection, and all-metric mode. The range API selects the last timestamp
per display bucket in SQLite before loading JSON, returning at most 1,000
readings per source. It never loads years of raw JSON into memory at once. The timestamp
primary-key indexes support range filtering. Sensor series use their own
`sensor_rows` array and ten-minute cadence;
cached sensor values are never duplicated into solar minute records.
The graph's display sampling does not expire or alter stored records. Retention
is indefinite, meeting the requested minimum of five years while storage is
available. See [history API details](../docs/history.md).

The interface keeps system sans-serif fonts, the dark green/lime palette, and
responsive cards. Charts use a two-column desktop layout and one column on
mobile, larger mobile axis labels, SVG point titles, and tap-to-inspect values.
All styles and scripts are bundled in `index.html`; no CDN or chart library is
required. Late responses from an earlier date selection are ignored.

Controller read failures preserve the previous reading and show a warning.
Storage failures show a distinct warning while displaying the latest live value;
failed database writes are not buffered for later replay. Readings more than
three minutes old are marked stale. Requests for unavailable history return 503.

Database connections close after each operation. SQLite's normal transactional
behavior protects completed writes; the project does not promise protection
against failing hardware or replace an off-device backup.
