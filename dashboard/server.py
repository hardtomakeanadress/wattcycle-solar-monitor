"""Small standard-library dashboard. One serial reader; browsers use cached data."""
import datetime
from bisect import bisect_right
from contextlib import contextmanager
import json
import logging
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from bluetooth_sensor import XiaomiSensor, INTERVAL as SENSOR_INTERVAL
from system_info import SystemInfo

ROOT = Path(__file__).resolve().parent
READER = ROOT.parent / 'read_controller.py'
DB = Path(os.environ.get('SOLAR_DATABASE', str(ROOT / 'history.sqlite3'))).expanduser()
SYSTEM = SystemInfo(DB.parent)
INTERVAL = 300
STALE_AFTER = INTERVAL * 3
LOCK = threading.Lock()
STATE = {'reading': None, 'error': None, 'last_attempt': None}
STOP = threading.Event()
PENDING_LOCK = threading.Lock()
PENDING = {'solar': [], 'sensor': []}
# Keep accepted readings during a storage outage without exhausting Pi RAM.
PENDING_LIMITS = {'solar': 7 * 86400 // INTERVAL, 'sensor': 7 * 86400 // SENSOR_INTERVAL}
DROPPED = {'solar': 0, 'sensor': 0}
HISTORY_ERROR = None
SENSOR = XiaomiSensor(os.environ.get('BLUETOOTH_SENSOR_ADDRESS', ''),
                     os.environ.get('BLUETOOTH_SENSOR_NAME', 'Cabana climate'))

# Measured scalar fields only: raw register arrays and connection metadata are
# retained in SQLite but are not independent physical measurements.
METRICS = [
    ('charge_W', 'Charging power', 'W'),
    ('battery_V', 'Battery voltage', 'V'),
    ('PV_V', 'Solar input voltage', 'V'),
    ('charge_A', 'Charging current', 'A'),
    ('today_generation_Wh', 'Generated energy (controller cycle)', 'Wh'),
    ('today_peak_W', 'Peak charging power (controller cycle)', 'W'),
    ('controller_estimated_SOC_percent', 'Battery level (controller estimate)', '%'),
    ('controller_C', 'Controller temperature', '°C'),
    ('battery_temperature_field_C', 'Battery temperature field', '°C'),
    ('load_V', 'Load voltage', 'V'),
    ('load_A', 'Load current', 'A'),
    ('load_W', 'Load power', 'W'),
    ('today_load_Wh', 'Load energy (controller counter)', 'Wh'),
    ('load_on', 'Load on/off', ''),
    ('charge_status', 'Charge stage', ''),
    ('fault_bits', 'Fault bits', ''),
    ('system_V', 'Nominal system voltage', 'V'),
    ('running_days', 'Operating days', 'days'),
]
CHARGE_STATES = ['idle', 'open', 'MPPT', 'equalizing', 'boost', 'float', 'limited']
SENSOR_METRICS = [('sensor_temperature_C', 'Climate temperature', '°C'),
                  ('sensor_humidity_percent', 'Climate humidity', '%')]


class InvalidRange(ValueError):
    pass


def history_range(query):
    """Bounded display sampling; the original samples are never altered."""
    params = parse_qs(query, keep_blank_values=True)
    if set(params) - {'start', 'end', 'range'} or any(len(v) != 1 for v in params.values()):
        raise InvalidRange('Use range=all or start/end Unix timestamps.')
    end = time.time()
    if params.get('range') == ['all'] and len(params) == 1:
        start = None
    elif 'range' in params:
        raise InvalidRange('range=all cannot be combined with start/end.')
    else:
        try:
            end = float(params.get('end', [end])[0])
            start = float(params.get('start', [end - 86400])[0])
        except ValueError as exc:
            raise InvalidRange('start/end must be Unix timestamps.') from exc
        if not all(math.isfinite(v) and 0 <= v <= 253402300799 for v in (start, end)) or start >= end:
            raise InvalidRange('Use finite timestamps with 0 <= start < end.')
    with database() as db:
        cadence = db.execute('SELECT start_ts, interval_seconds FROM solar_intervals ORDER BY start_ts').fetchall()
        first = db.execute('SELECT ts FROM samples ORDER BY ts LIMIT 1').fetchone()
        last = db.execute('SELECT ts FROM samples ORDER BY ts DESC LIMIT 1').fetchone()
        sensor_first = db.execute('SELECT ts FROM sensor_samples WHERE sensor_address=? ORDER BY ts LIMIT 1',
                                  (SENSOR.address,)).fetchone()
        sensor_last = db.execute('SELECT ts FROM sensor_samples WHERE sensor_address=? ORDER BY ts DESC LIMIT 1',
                                 (SENSOR.address,)).fetchone()
        first = min((r for r in (first, sensor_first) if r), default=None)
        last = max((r for r in (last, sensor_last) if r), default=None)
        if start is None:
            start = first[0] if first else end - 86400
            end = max(end, last[0] + 0.001 if last else end, start + 1)
        # Relative buckets cap the result at 1000 samples even over many years.
        bucket = max(60, math.ceil((end - start) / 1000))
        selected = db.execute('''
            SELECT s.ts, s.data FROM samples s JOIN (
                SELECT MAX(ts) AS ts FROM samples WHERE ts >= ? AND ts < ?
                GROUP BY CAST((ts - ?) / ? AS INTEGER)
            ) picked ON s.ts = picked.ts ORDER BY s.ts
        ''', (start, end, start, bucket)).fetchall()
        count = db.execute('SELECT COUNT(*) FROM samples WHERE ts >= ? AND ts < ?', (start, end)).fetchone()[0]
        sensor_bucket = max(SENSOR_INTERVAL, bucket)
        sensor_selected = db.execute('''
            SELECT ts, data FROM sensor_samples WHERE sensor_address=? AND ts IN (
                SELECT MAX(ts) FROM sensor_samples WHERE sensor_address=? AND ts >= ? AND ts < ?
                GROUP BY CAST((ts - ?) / ? AS INTEGER)
            ) ORDER BY ts
        ''', (SENSOR.address, SENSOR.address, start, end, start, sensor_bucket)).fetchall()
        sensor_count = db.execute('SELECT COUNT(*) FROM sensor_samples WHERE sensor_address=? AND ts >= ? AND ts < ?',
                                  (SENSOR.address, start, end)).fetchone()[0]
    rows = []
    cadence_starts = [change[0] for change in cadence]
    for ts, raw in selected:
        reading = json.loads(raw)
        change = bisect_right(cadence_starts, ts) - 1
        row = {'ts': ts, 'poll_interval_seconds': cadence[change][1] if change >= 0 else 60}
        for key, _, _ in METRICS:
            value = reading.get(key)
            if key == 'charge_status':
                value = CHARGE_STATES.index(value) if value in CHARGE_STATES else None
            elif key == 'load_on':
                value = int(value) if isinstance(value, bool) else None
            row[key] = value if isinstance(value, (int, float)) and math.isfinite(value) else None
        rows.append(row)
    sensor_rows = []
    for ts, raw in sensor_selected:
        reading = json.loads(raw)
        row = {'ts': ts}
        for key, _, _ in SENSOR_METRICS:
            value = reading.get(key.removeprefix('sensor_'))
            row[key] = value if isinstance(value, (int, float)) and math.isfinite(value) else None
        sensor_rows.append(row)
    disk = os.statvfs(DB.parent)
    return dict(start=start, end=end, bucket_seconds=bucket, sample_count=count,
                sensor_rows=sensor_rows, sensor_sample_count=sensor_count,
                sensor_bucket_seconds=sensor_bucket,
                storage=dict(database_bytes=DB.stat().st_size,
                             free_bytes=disk.f_bavail * disk.f_frsize),
                available_start=first[0] if first else None,
                available_end=last[0] if last else None,
                sampling='Last reading per display bucket; full samples retained indefinitely.',
                metrics=[dict(key=k, label=label, unit=unit, poll_interval_seconds=INTERVAL,
                              states=CHARGE_STATES if k == 'charge_status' else
                              ['Off', 'On'] if k == 'load_on' else None)
                         for k, label, unit in METRICS] +
                        ([dict(key=k, label=label, unit=unit, states=None, source='sensor',
                               poll_interval_seconds=SENSOR_INTERVAL)
                          for k, label, unit in SENSOR_METRICS] if SENSOR.address else []), rows=rows)


@contextmanager
def database():
    # sqlite3's context manager commits/rolls back, but does not close the file.
    db = sqlite3.connect(DB)
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize():
    global HISTORY_ERROR
    with PENDING_LOCK:
        PENDING['solar'].clear()
        PENDING['sensor'].clear()
        DROPPED.update(solar=0, sensor=0)
        HISTORY_ERROR = None
    DB.parent.mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.execute('CREATE TABLE IF NOT EXISTS samples (ts REAL PRIMARY KEY, data TEXT NOT NULL)')
        db.execute('''CREATE TABLE IF NOT EXISTS sensor_samples (
            sensor_address TEXT NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY (sensor_address, ts))''')
        db.execute('''CREATE TABLE IF NOT EXISTS solar_intervals (
            start_ts REAL PRIMARY KEY, interval_seconds INTEGER NOT NULL)''')
        # Untagged historical collection used 60 seconds. Record changes once,
        # separately from measurements; upgrades may import a known earlier change.
        cadence = db.execute('SELECT interval_seconds FROM solar_intervals ORDER BY start_ts DESC LIMIT 1').fetchone()
        if cadence is None or cadence[0] != INTERVAL:
            db.execute('INSERT INTO solar_intervals VALUES (?, ?)', (time.time(), INTERVAL))
        row = db.execute('SELECT ts, data FROM samples ORDER BY ts DESC LIMIT 1').fetchone()
        if row:
            STATE.update(reading=json.loads(row[1]), last_attempt=row[0])
        sensor_row = db.execute('SELECT data FROM sensor_samples WHERE sensor_address=? ORDER BY ts DESC LIMIT 1',
                                (SENSOR.address,)).fetchone()
    SENSOR.on_reading = save_sensor_reading
    SENSOR.restore(json.loads(sensor_row[0]) if sensor_row else None)


def save_sensor_reading(address, reading):
    # Preserve the actual measurement time while waiting for the next batch.
    row = (address, reading['timestamp_seconds'], json.dumps(reading))
    queue_reading('sensor', row)


def queue_reading(source, row):
    """Retain accepted history; keep live values updating if the buffer fills."""
    with PENDING_LOCK:
        if len(PENDING[source]) >= PENDING_LIMITS[source]:
            DROPPED[source] += 1
            if DROPPED[source] == 1:
                logging.error('%s history buffer full; new history readings will be skipped until storage recovers', source)
            return False
        PENDING[source].append(row)
        return True


def flush_history():
    """Commit both sources atomically; keep the whole batch after storage failure."""
    global HISTORY_ERROR
    with PENDING_LOCK:
        if not any(PENDING.values()):
            return True
        try:
            with database() as db:
                # A repeated timestamp must neither overwrite history nor block
                # unrelated readings in this and subsequent batches.
                solar = db.executemany('INSERT INTO samples VALUES (?, ?) ON CONFLICT(ts) DO NOTHING',
                                       PENDING['solar'])
                sensor = db.executemany('''INSERT INTO sensor_samples VALUES (?, ?, ?)
                    ON CONFLICT(sensor_address, ts) DO NOTHING''', PENDING['sensor'])
                duplicates = (len(PENDING['solar']) - solar.rowcount +
                              len(PENDING['sensor']) - sensor.rowcount)
        except Exception:
            logging.exception('Saving history batch failed; retaining readings for retry')
            HISTORY_ERROR = 'Live readings received, but could not save history. Retrying every 5 minutes; check storage on the Pi.'
            return False
        PENDING['solar'].clear()
        PENDING['sensor'].clear()
        HISTORY_ERROR = None
        if duplicates:
            logging.error('Skipped %s duplicate history timestamps; existing readings preserved', duplicates)
        return True


def history_status():
    with PENDING_LOCK:
        oldest = min((row[-2] for rows in PENDING.values() for row in rows), default=None)
        return dict(commit_interval_seconds=INTERVAL, error=HISTORY_ERROR,
                    pending_solar_samples=len(PENDING['solar']),
                    pending_sensor_samples=len(PENDING['sensor']),
                    pending_limits=dict(PENDING_LIMITS),
                    oldest_pending_age_seconds=None if oldest is None else max(0, time.time() - oldest),
                    dropped_solar_samples=DROPPED['solar'], dropped_sensor_samples=DROPPED['sensor'])


def poll_once():
    attempt = time.time()
    try:
        result = subprocess.run([sys.executable, str(READER)], capture_output=True,
                                text=True, timeout=15, check=True)
        reading = json.loads(result.stdout)
    except Exception as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            logging.error('Reader output: %s', exc.stderr)
        logging.exception('Controller read failed')
        with LOCK:
            STATE.update(error='Controller read failed; retrying automatically.', last_attempt=attempt)
    else:
        queue_reading('solar', (attempt, json.dumps(reading)))
        with LOCK:
            STATE.update(reading=reading, error=None, last_attempt=attempt)
    # Climate collection and storage retries continue even if Modbus is offline.
    flush_history()


def poll_loop():
    with LOCK:
        last_attempt = STATE['last_attempt']
    if last_attempt is not None:
        remaining = min(INTERVAL, max(0, INTERVAL - (time.time() - last_attempt)))
        if STOP.wait(remaining):
            return
    while not STOP.is_set():
        start = time.monotonic()
        poll_once()
        STOP.wait(max(1, INTERVAL - (time.monotonic() - start)))


def snapshot():
    with LOCK:
        data = dict(STATE)
    reading = data['reading']
    age = None if reading is None else max(0, time.time() - datetime.datetime.fromisoformat(reading['timestamp_utc']).timestamp())
    data.update(age_seconds=age, stale=age is None or age > STALE_AFTER, server_time_seconds=time.time(),
                stale_after_seconds=STALE_AFTER, poll_interval_seconds=INTERVAL)
    data['sensor'] = SENSOR.snapshot()
    data['history'] = history_status()
    data['error'] = data['error'] or data['history']['error']
    data['sensor']['error'] = data['sensor']['error'] or data['history']['error']
    return data


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            self.respond()
        except (BrokenPipeError, ConnectionResetError):
            pass  # A browser closed the connection.
        except InvalidRange as exc:
            self.send_error(400, str(exc))
        except (sqlite3.Error, OSError, ValueError, KeyError):
            logging.exception('Dashboard request failed')
            self.send_error(503, 'Dashboard data temporarily unavailable')

    def respond(self):
        url = urlsplit(self.path)
        route = url.path
        if route == '/':
            body, kind = (ROOT / 'index.html').read_bytes(), 'text/html; charset=utf-8'
        elif route == '/api/status':
            body, kind = json.dumps(snapshot()).encode(), 'application/json'
        elif route == '/api/system':
            body, kind = json.dumps(SYSTEM.snapshot()).encode(), 'application/json'
        elif route == '/api/history/range':
            body, kind = json.dumps(history_range(url.query)).encode(), 'application/json'
        elif route == '/api/history':
            with database() as db:
                rows = db.execute('SELECT ts, data FROM samples WHERE ts >= ? ORDER BY ts', (time.time() - 86400,)).fetchall()
            # Five-minute buckets keep the browser payload small on the Pi 2.
            buckets = {}
            for ts, raw in rows:
                r = json.loads(raw)
                buckets[int(ts // 300)] = {'ts': ts, 'charge_W': r['charge_W'], 'battery_V': r['battery_V']}
            body, kind = json.dumps(list(buckets.values())).encode(), 'application/json'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def main():
    logging.basicConfig(level=logging.INFO)
    initialize()
    STOP.clear()
    server = ThreadingHTTPServer((os.environ.get('DASHBOARD_BIND', '0.0.0.0'), int(os.environ.get('DASHBOARD_PORT', '8080'))), Handler)
    server.daemon_threads = True
    def stop(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    collector = threading.Thread(target=poll_loop, daemon=True, name='solar-collector')
    sensor_thread = None
    try:
        collector.start()
        sensor_thread = SENSOR.start(STOP)
        logging.info('Dashboard listening on %s:%s', *server.server_address)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        # Hardware calls have bounded timeouts; finish accepted readings before
        # flushing. systemd's normal stop timeout allows both collectors to exit.
        collector.join()
        if sensor_thread is not None:
            sensor_thread.join()
        saved = flush_history()
        server.server_close()
    return 0 if saved else 1


if __name__ == '__main__':
    sys.exit(main())
