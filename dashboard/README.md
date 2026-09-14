# Dashboard implementation

See the [project README](../README.md) for setup and
[deployment guide](../docs/deployment.md) for automatic startup and backups.

`server.py` runs a single collector thread. It launches `read_controller.py`
once per minute and saves each successful JSON snapshot in SQLite. Browser
requests return cached readings; opening more tabs does not create more Modbus
pollers. The serial connection is released between readings.

The database retains all samples indefinitely. It has one table:

```sql
CREATE TABLE samples (ts REAL PRIMARY KEY, data TEXT NOT NULL);
```

`ts` is the collection attempt's Unix timestamp in seconds; `data` contains the
full JSON reading, including its own completion timestamp in UTC. Existing
records are never replaced on a timestamp collision. Correct system time is
needed for meaningful timestamps and the 24-hour graph.

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
readings. It never loads years of raw JSON into memory at once. The timestamp
primary-key index supports range filtering; no schema migration is needed.
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
