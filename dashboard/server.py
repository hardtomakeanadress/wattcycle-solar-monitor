"""Small standard-library dashboard. One serial reader; browsers use cached data."""
import datetime
from contextlib import contextmanager
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from bluetooth_sensor import XiaomiSensor

ROOT = Path(__file__).resolve().parent
READER = ROOT.parent / 'read_controller.py'
DB = Path(os.environ.get('SOLAR_DATABASE', str(ROOT / 'history.sqlite3'))).expanduser()
INTERVAL = 60
STALE_AFTER = INTERVAL * 3
LOCK = threading.Lock()
STATE = {'reading': None, 'error': None, 'last_attempt': None}
SENSOR = XiaomiSensor(os.environ.get('BLUETOOTH_SENSOR_ADDRESS', ''),
                     os.environ.get('BLUETOOTH_SENSOR_NAME', 'Cabana climate'))

# Measured scalar fields only: raw register arrays and connection metadata are
# retained in SQLite but are not independent physical measurements.
METRICS = [
    ('charge_W', 'Charging power', 'W'),
    ('battery_V', 'Battery voltage', 'V'),
    ('PV_V', 'Solar input voltage', 'V'),
    ('charge_A', 'Charging current', 'A'),
    ('today_generation_Wh', 'Generated energy today', 'Wh'),
    ('today_peak_W', 'Peak charging power today', 'W'),
    ('controller_estimated_SOC_percent', 'Battery level (controller estimate)', '%'),
    ('controller_C', 'Controller temperature', '°C'),
    ('battery_temperature_field_C', 'Battery temperature field', '°C'),
    ('load_V', 'Load voltage', 'V'),
    ('load_A', 'Load current', 'A'),
    ('load_W', 'Load power', 'W'),
    ('today_load_Wh', 'Load energy today', 'Wh'),
    ('load_on', 'Load on/off', ''),
    ('charge_status', 'Charge stage', ''),
    ('fault_bits', 'Fault bits', ''),
    ('system_V', 'Nominal system voltage', 'V'),
    ('running_days', 'Operating days', 'days'),
]
CHARGE_STATES = ['idle', 'open', 'MPPT', 'equalizing', 'boost', 'float', 'limited']


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
        first = db.execute('SELECT ts FROM samples ORDER BY ts LIMIT 1').fetchone()
        last = db.execute('SELECT ts FROM samples ORDER BY ts DESC LIMIT 1').fetchone()
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
    rows = []
    for ts, raw in selected:
        reading = json.loads(raw)
        row = {'ts': ts}
        for key, _, _ in METRICS:
            value = reading.get(key)
            if key == 'charge_status':
                value = CHARGE_STATES.index(value) if value in CHARGE_STATES else None
            elif key == 'load_on':
                value = int(value) if isinstance(value, bool) else None
            row[key] = value if isinstance(value, (int, float)) and math.isfinite(value) else None
        rows.append(row)
    disk = os.statvfs(DB.parent)
    return dict(start=start, end=end, bucket_seconds=bucket, sample_count=count,
                storage=dict(database_bytes=DB.stat().st_size,
                             free_bytes=disk.f_bavail * disk.f_frsize),
                available_start=first[0] if first else None,
                available_end=last[0] if last else None,
                sampling='Last reading per display bucket; full samples retained indefinitely.',
                metrics=[dict(key=k, label=label, unit=unit,
                              states=CHARGE_STATES if k == 'charge_status' else
                              ['Off', 'On'] if k == 'load_on' else None)
                         for k, label, unit in METRICS], rows=rows)


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
    DB.parent.mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.execute('CREATE TABLE IF NOT EXISTS samples (ts REAL PRIMARY KEY, data TEXT NOT NULL)')
        row = db.execute('SELECT data FROM samples ORDER BY ts DESC LIMIT 1').fetchone()
        if row:
            STATE['reading'] = json.loads(row[0])


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
        return
    error = None
    try:
        with database() as db:
            # Keep every sample indefinitely; the graph's time window is separate.
            # Never replace an existing reading if the system clock repeats a timestamp.
            db.execute('INSERT INTO samples VALUES (?, ?)', (attempt, json.dumps(reading)))
    except Exception:
        logging.exception('Saving controller reading failed')
        error = 'Live reading received, but could not save history. Check storage on the Pi.'
    with LOCK:
        STATE.update(reading=reading, error=error, last_attempt=attempt)


def poll_loop():
    while True:
        start = time.monotonic()
        poll_once()
        time.sleep(max(1, INTERVAL - (time.monotonic() - start)))


def snapshot():
    with LOCK:
        data = dict(STATE)
    reading = data['reading']
    age = None if reading is None else max(0, time.time() - datetime.datetime.fromisoformat(reading['timestamp_utc']).timestamp())
    data.update(age_seconds=age, stale=age is None or age > STALE_AFTER, server_time_seconds=time.time(),
                stale_after_seconds=STALE_AFTER, poll_interval_seconds=INTERVAL)
    data['sensor'] = SENSOR.snapshot()
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


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    initialize()
    threading.Thread(target=poll_loop, daemon=True).start()
    SENSOR.start()
    server = ThreadingHTTPServer((os.environ.get('DASHBOARD_BIND', '0.0.0.0'), int(os.environ.get('DASHBOARD_PORT', '8080'))), Handler)
    server.daemon_threads = True
    logging.info('Dashboard listening on %s:%s', *server.server_address)
    server.serve_forever()
