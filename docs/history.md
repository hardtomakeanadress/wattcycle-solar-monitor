# History browsing and five-year retention

Every successful controller read is saved as full JSON in SQLite once per
minute. Retention is indefinite: the requested five-year minimum does not
introduce a five-year deletion cutoff. Older readings remain available as long
as the storage remains usable. Backups are described in [deployment](deployment.md#backups).

## Using the page

1. Open the dashboard and scroll to **Explore your history**.
2. Select 15 minutes, an hour, 6 hours, 24 hours, 7 days, 30 days, a year,
   all saved history, or custom dates. The default remains 24 hours.
3. For custom dates, enter a start and end in the browser's local time, then
   choose **Apply dates**. The end is exclusive. Automatic refresh waits while
   you edit dates; it does not apply an unfinished date range.
4. Keep the three-chart overview, choose an individual measurement, or choose
   **Every metric**. An individual measurement fills the chart width. Move
   across or tap anywhere in a chart to inspect its nearest saved reading;
   a vertical marker shows which timestamp was selected.
5. **Zoom in** halves the displayed period, down to a five-minute window.
   **Zoom out** doubles it. **Earlier** and **Later** move one window at a time.
   These views stay on fixed dates while new readings arrive. **Latest** keeps
   the current window length and follows incoming data again.

Short views use hour/minute labels and vertical time grid lines, with fewer
labels on narrow phone screens. The full selected dates and timezone appear
above the charts, including when a window crosses midnight or a clock change.
Zoom changes only the displayed time window. The collector still reads once
per minute for solar and every ten minutes for the optional climate sensor;
there are no measurements between those saved readings.

The page shows the earliest/latest saved dates, the number of readings in the
selected period, displayed-point count, display interval, database size, and
remaining filesystem space. No data is invented for dates before collection
started or during outages. Existing saved readings become browsable immediately
after upgrading. Startup adds an independent sensor table inside the same
database; existing solar records are not modified. Sensor history starts when
this feature is enabled and is not backfilled from old solar records.

## Available charts

| Group | Measurements |
| --- | --- |
| Charging | Power (W), current (A), charge stage |
| Battery | Voltage (V), controller-estimated percentage, battery-temperature field (°C) |
| Solar | Input voltage (V), generated energy today (Wh), peak charging power today (W) |
| Controller | Temperature (°C), raw fault bits, nominal system voltage, operating days |
| Load | Voltage (V), current (A), power (W), energy today (Wh), on/off state |
| Optional climate sensor | Temperature (°C), relative humidity (%) |

This covers all 18 solar fields plus two climate charts when a sensor is configured.
Sensor battery voltage is also retained in the stored JSON. Serial
metadata and raw register arrays stay in the stored JSON but are not separate
physical metrics. Cumulative energy words remain undecoded because their word
order and rollover have not been independently verified.

Solar input is **voltage**, not measured panel current or panel watts. Charging
power is output toward the battery. The daily Wh counters reset each day; their
plots are counters, not period totals. Battery percentage is the controller's
estimate, and a temperature-field value does not prove an external sensor exists.

## Display sampling

Queries return at most 1,000 points per source, taking the last reading from
each bucket. The interval grows with the selected range (minimum 60 seconds
for solar and 600 seconds for climate). Climate charts use only actual sensor
measurements and timestamps; minute solar records do not repeat cached values. Full original
minute records remain unchanged. Broad ranges can miss brief peaks or state
changes; the displayed min/max apply only to the returned readings. Choose a
narrower custom range for more detail. Gaps between returned samples are drawn
as breaks rather than uninterrupted lines. Missing fields remain absent rather
than becoming zero; genuine zero and negative values are supported.

## Range API

```text
GET /api/history/range
GET /api/history/range?start=1789257600&end=1789344000
GET /api/history/range?range=all
```

`start` and `end` are finite Unix timestamps in seconds, with `0 <= start < end`.
Start is inclusive; end is exclusive. Without parameters, the previous 24 hours
are returned. `range=all` cannot be combined with timestamps. Unknown,
duplicated, empty, or invalid parameters return HTTP 400; storage/read errors
return HTTP 503. The UI reports errors and retries automatically.

The JSON object contains:

- `start`, `end`, `bucket_seconds`, `sample_count` for the selected range.
- `available_start`, `available_end` for overall saved date coverage, or null.
- `storage.database_bytes`, `storage.free_bytes` in bytes.
- `metrics`: keys, labels, units, and optional state-label arrays.
- `rows`: collection timestamps (`ts`) and each measured scalar field.
- `sensor_rows`: separate actual sensor timestamps and `sensor_temperature_C` /
  `sensor_humidity_percent` values for the configured sensor address.
- `sensor_sample_count`, `sensor_bucket_seconds`: independent sensor sampling statistics.
- Climate metrics have `source: "sensor"` and `poll_interval_seconds: 600`; their rows
  are in `sensor_rows`, not the solar `rows` array.
- `sampling`: description of the display selection method.

`charge_status` becomes an index into the supplied state labels; `load_on`
becomes 0/1. Unknown states and missing/non-finite fields become null. Full
original JSON, including textual states, remains in SQLite.

The legacy `/api/history` endpoint still returns its original 24-hour array of
five-minute representative readings with `charge_W` and `battery_V`. Existing
clients can keep using it; the updated page uses the range API. Neither endpoint
triggers a serial read or writes/deletes database rows.
