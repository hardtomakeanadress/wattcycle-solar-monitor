"""Small standard-library dashboard. One serial reader; browsers use cached data."""
import datetime
from bisect import bisect_right
from collections import OrderedDict
from contextlib import contextmanager
import json
import hmac
import logging
import math
import os
from pathlib import Path
import signal
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from bluetooth_sensor import XiaomiSensor, INTERVAL as SENSOR_INTERVAL
from system_info import SystemInfo
from load_control import ControlServer
from heating import Heating
from victron_shunt import VictronShunt
from charge_timer import ChargeTimer

ROOT = Path(__file__).resolve().parent
READER = ROOT.parent / 'read_controller.py'
DB = Path(os.environ.get('SOLAR_DATABASE', str(ROOT / 'history.sqlite3'))).expanduser()
SYSTEM = SystemInfo(DB.parent)
CHARGE_TIMER = ChargeTimer(DB.parent / 'charge-timer.json')
SHUNT = VictronShunt(os.environ.get('VICTRON_SHUNT_ADDRESS', ''),
                     os.environ.get('VICTRON_SHUNT_KEY_FILE', str(DB.parent / 'victron.key')),
                     on_reading=CHARGE_TIMER.observe)
INTERVAL = 300
STALE_AFTER = INTERVAL * 3
LOCK = threading.Lock()
SERIAL_LOCK = threading.Lock()
COMMAND_LOCK = threading.Lock()
HEATING = None
HEATING_KEY = None
SHUTDOWN_LOCK = threading.Lock()
SHUTDOWN_PENDING = False
STATE = {'reading': None, 'error': None, 'last_attempt': None}
STOP = threading.Event()
PENDING_LOCK = threading.Lock()
PENDING = {'solar': [], 'sensor': []}
# Keep accepted readings during a storage outage without exhausting Pi RAM.
PENDING_LIMITS = {'solar': 7 * 86400 // 60, 'sensor': 7 * 86400 // 60}
DROPPED = {'solar': 0, 'sensor': 0}
HISTORY_ERROR = None
HISTORY_CACHE = OrderedDict()
HISTORY_CACHE_LOCK = threading.Lock()
HISTORY_CACHE_SECONDS = 30
HISTORY_CACHE_LIMIT = 8
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


def stored_reading(raw):
    """An unreadable record is a gap, never a reason to rewrite history."""
    try:
        reading = finite_json(json.loads(raw))
    except (ValueError, TypeError, UnicodeError):
        return None
    return reading if isinstance(reading, dict) else None


def finite_json(value):
    """Keep unknown numeric fields JSON-compatible, including damaged history."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: finite_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [finite_json(item) for item in value]
    return value


def clear_history_cache():
    with HISTORY_CACHE_LOCK:
        HISTORY_CACHE.clear()


def history_response(query):
    # Serialize concurrent queries and bound retained responses on the Pi.
    # Failed queries are not cached. Commits invalidate all cached ranges.
    key = (str(DB), SENSOR.address, query)
    with HISTORY_CACHE_LOCK:
        cached = HISTORY_CACHE.get(key)
        if cached is not None and time.monotonic() - cached[0] < HISTORY_CACHE_SECONDS:
            HISTORY_CACHE.move_to_end(key)
            return cached[1]
        body = json.dumps(history_range(query)).encode()
        HISTORY_CACHE[key] = (time.monotonic(), body)
        HISTORY_CACHE.move_to_end(key)
        while len(HISTORY_CACHE) > HISTORY_CACHE_LIMIT:
            HISTORY_CACHE.popitem(last=False)
        return body


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
        sensor_bucket = max(60, bucket)
        sensor_selected = db.execute('''
            SELECT ts, data FROM sensor_samples WHERE sensor_address=? AND ts IN (
                SELECT MAX(ts) FROM sensor_samples WHERE sensor_address=? AND ts >= ? AND ts < ?
                GROUP BY CAST((ts - ?) / ? AS INTEGER)
            ) ORDER BY ts
        ''', (SENSOR.address, SENSOR.address, start, end, start, sensor_bucket)).fetchall()
        sensor_count = db.execute('SELECT COUNT(*) FROM sensor_samples WHERE sensor_address=? AND ts >= ? AND ts < ?',
                                  (SENSOR.address, start, end)).fetchone()[0]
    rows = []
    invalid_solar_rows = invalid_sensor_rows = 0
    cadence_starts = [change[0] for change in cadence]
    for ts, raw in selected:
        reading = stored_reading(raw)
        if reading is None:
            invalid_solar_rows += 1
            reading = {}
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
        reading = stored_reading(raw)
        if reading is None:
            invalid_sensor_rows += 1
            reading = {}
        row = {'ts': ts}
        for key, _, _ in SENSOR_METRICS:
            value = reading.get(key.removeprefix('sensor_'))
            row[key] = value if isinstance(value, (int, float)) and math.isfinite(value) else None
        sensor_rows.append(row)
    disk = os.statvfs(DB.parent)
    return dict(start=start, end=end, bucket_seconds=bucket, sample_count=count,
                invalid_solar_rows=invalid_solar_rows, invalid_sensor_rows=invalid_sensor_rows,
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
    clear_history_cache()
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
        reading = stored_reading(row[1]) if row else None
        if row and reading is None:
            logging.error('Latest solar history record is unreadable at timestamp %s', row[0])
        STATE.update(reading=reading, last_attempt=row[0] if reading is not None else None)
        sensor_row = db.execute('SELECT data FROM sensor_samples WHERE sensor_address=? ORDER BY ts DESC LIMIT 1',
                                (SENSOR.address,)).fetchone()
    SENSOR.on_reading = save_sensor_reading
    sensor_reading = stored_reading(sensor_row[0]) if sensor_row else None
    if sensor_reading is not None:
        values = [sensor_reading.get(key) for key in
                  ('timestamp_seconds', 'temperature_C', 'humidity_percent', 'battery_V')]
        if not all(type(value) in (int, float) and math.isfinite(value) for value in values):
            sensor_reading = None
    if sensor_row and sensor_reading is None:
        logging.error('Latest climate history record is unreadable; retrying the sensor')
    SENSOR.restore(sensor_reading)


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
        clear_history_cache()
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
    with SERIAL_LOCK:
        poll_controller()
    # Climate collection and storage retries continue even if Modbus is offline.
    flush_history()


def poll_controller():
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


def control_load(enabled):
    # Scheduling poweroff waits for accepted manual commands to complete.
    if not SHUTDOWN_LOCK.acquire(blocking=False):
        raise RuntimeError('Another LOAD command or shutdown is in progress; no command sent')
    try:
        if enabled and SHUTDOWN_PENDING:
            raise RuntimeError('Pi shutdown already requested; LOAD ON is blocked')
        if HEATING is not None:
            return HEATING.manual(enabled, execute_load)
        return execute_load(enabled)
    finally:
        SHUTDOWN_LOCK.release()


def execute_load(enabled):
    # Reject overlapping commands rather than queueing a switch for later.
    if not COMMAND_LOCK.acquire(blocking=False):
        raise RuntimeError('Another LOAD command is in progress; no command sent')
    try:
        return run_load_command(enabled)
    finally:
        COMMAND_LOCK.release()


def run_load_command(enabled):
    # Commands run immediately, or after the current bounded telemetry request.
    # Keep readback and cache publication inside the same lock to preserve order.
    if not SERIAL_LOCK.acquire(timeout=20):
        raise RuntimeError('Controller busy; no command sent')
    try:
        if STOP.is_set() or (enabled and SHUTDOWN_PENDING):
            raise RuntimeError('Monitoring is shutting down; no command sent')
        try:
            result = subprocess.run([sys.executable, str(READER), '--load', 'on' if enabled else 'off'],
                                    capture_output=True, text=True, timeout=15, check=True)
            reading = json.loads(result.stdout)
            if reading.get('load_on') is not enabled:
                raise RuntimeError('LOAD state not verified')
        except Exception as exc:
            logging.error('LOAD command failed: %s', getattr(exc, 'stderr', None) or exc)
            with LOCK:
                STATE['error'] = 'LOAD command failed; physical state unconfirmed. Awaiting fresh telemetry.'
            detail = (exc.stderr or '').strip().splitlines()[-1:] if isinstance(exc, subprocess.CalledProcessError) else [str(exc)]
            raise RuntimeError('LOAD command failed; state unconfirmed; no automatic retry. ' + ' '.join(detail)) from exc
        attempt = time.time()
        queue_reading('solar', (attempt, json.dumps(reading)))
        with LOCK:
            STATE.update(reading=reading, error=None, last_attempt=attempt)
        logging.info('LOAD %s verified', 'ON' if enabled else 'OFF')
    finally:
        SERIAL_LOCK.release()
    flush_history()
    return reading


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
    try:
        age = None if reading is None else max(0, time.time() - datetime.datetime.fromisoformat(reading['timestamp_utc']).timestamp())
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        age = None
        data['error'] = data['error'] or 'Saved reading has an invalid timestamp; waiting for a fresh controller reading.'
    data.update(age_seconds=age, stale=age is None or age > STALE_AFTER, server_time_seconds=time.time(),
                stale_after_seconds=STALE_AFTER, poll_interval_seconds=INTERVAL)
    data['sensor'] = SENSOR.snapshot()
    data['shunt'] = SHUNT.snapshot()
    data['timers'] = CHARGE_TIMER.snapshot(data['shunt'])
    data['history'] = history_status()
    data['heating'] = heating_status()
    return data


def heating_status():
    data = HEATING.snapshot() if HEATING is not None else dict(available=False, armed=False,
        installation_ready=False, phase='unavailable', alarm=None, message='Heating control is unavailable.')
    data['control_configured'] = HEATING_KEY is not None
    return data


def heating_health():
    with LOCK:
        state = dict(STATE)
    if state['error']:
        return 'Controller telemetry failed. Inspect before re-arming.'
    if (state['reading'] or {}).get('fault_bits', 0):
        return 'Controller reports faults. Inspect before re-arming.'
    if history_status()['error']:
        return 'History storage failed. Inspect storage before re-arming.'
    return None


def setup_heating():
    global HEATING, HEATING_KEY
    HEATING_KEY = None
    try:
        HEATING_KEY = heating_key()
    except (OSError, ValueError):
        # Authentication failure must not prevent recovery of a previously ON heater.
        logging.exception('Heating UI control key unavailable')
    HEATING = Heating(SENSOR, execute_load, heating_health, DB.parent / 'heating-state.json',
                      ready=os.environ.get('HEATING_INSTALLATION_READY') == '1')
    HEATING.start()


def heating_key():
    key_path = DB.parent / 'heating-control.key'
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(32) + '\n')
    key = key_path.read_text().strip()
    if len(key) < 32 or not key.isascii():
        raise ValueError('Heating control key must contain at least 32 ASCII characters')
    return key


class Handler(BaseHTTPRequestHandler):
    timeout = 30

    def trusted_control_host(self):
        """Reject rebinding hostnames; preserve direct local-IP and host access."""
        host = self.headers.get('Host', '')
        parsed = urlsplit('//' + host)
        if not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return False
        # A rebinding domain does not become trusted by resolving to this Pi.
        allowed = {'localhost', socket.gethostname().lower(), socket.gethostname().lower() + '.local'}
        allowed.update(name.strip().lower() for name in os.environ.get('DASHBOARD_ALLOWED_HOSTS', '').split(',') if name.strip())
        allowed.add(self.connection.getsockname()[0].lower())
        return parsed.hostname.lower() in allowed

    def do_POST(self):
        if self.path not in ('/api/heating', '/api/shutdown'):
            self.send_error(404)
            return
        origin = self.headers.get('Origin')
        if self.path == '/api/shutdown' and not origin:
            self.send_error(403, 'A same-origin browser request is required')
            return
        try:
            if origin and (urlsplit(origin).scheme not in ('http', 'https') or urlsplit(origin).netloc != self.headers.get('Host')):
                self.send_error(403, 'Cross-origin control is forbidden')
                return
            if not self.trusted_control_host():
                self.send_error(403, 'Unrecognized dashboard host')
                return
        except ValueError:
            self.send_error(403, 'Invalid dashboard host')
            return
        credential = self.headers.get('Authorization', '')
        if self.path == '/api/heating' and (HEATING_KEY is None or not hmac.compare_digest(credential.encode(), ('Bearer ' + HEATING_KEY).encode())):
            self.send_error(403, 'A valid control key is required')
            return
        try:
            if self.headers.get('Content-Type') != 'application/json' or self.headers.get('Transfer-Encoding'):
                raise ValueError('Expected application/json')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 128:
                raise ValueError('Invalid request length')
            data = json.loads(self.rfile.read(length))
            if self.path == '/api/shutdown':
                if not isinstance(data, dict) or set(data) != {'shutdown'} or data['shutdown'] is not True:
                    raise ValueError('Expected shutdown: true')
                self.shutdown_pi()
                return
            if not isinstance(data, dict) or set(data) != {'armed'} or type(data['armed']) is not bool:
                raise ValueError('Expected a boolean armed value')
            if HEATING is None:
                raise ValueError('Heating unavailable')
            with SHUTDOWN_LOCK:
                if SHUTDOWN_PENDING:
                    raise ValueError('Pi shutdown already requested')
                HEATING.request(data['armed'])
        except (OSError, ValueError) as exc:
            self.send_error(400, str(exc))
            return
        body = json.dumps({'heating': heating_status()}).encode()
        self.send_response(202)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def shutdown_pi(self):
        """Ask logind for an orderly shutdown; never run the website as root."""
        global SHUTDOWN_PENDING
        with SHUTDOWN_LOCK:
            if SHUTDOWN_PENDING:
                self.send_error(409, 'Pi shutdown already requested')
                return
            h = heating_status()
            if h.get('armed') or h.get('pending') or h.get('owns_load'):
                self.send_error(409, 'Turn off heating and wait for confirmed OFF before shutting down')
                return
            SHUTDOWN_PENDING = True
            try:
                # logind keeps this timer even if the HTTP client disconnects.
                subprocess.run(['/usr/bin/busctl', '--system', '--allow-interactive-authorization=no',
                    '--timeout=5s', 'call', 'org.freedesktop.login1', '/org/freedesktop/login1',
                    'org.freedesktop.login1.Manager', 'ScheduleShutdown', 'st', 'poweroff',
                    str(int((time.time() + 10) * 1_000_000))],
                    check=True, capture_output=True, timeout=7)
            except subprocess.CalledProcessError:
                SHUTDOWN_PENDING = False
                logging.exception('Pi shutdown authorization failed')
                self.send_error(503, 'Shutdown rejected by the operating system')
                return
            except OSError:
                SHUTDOWN_PENDING = False
                logging.exception('Pi shutdown command could not start')
                self.send_error(503, 'Shutdown command unavailable; no shutdown scheduled')
                return
            except subprocess.TimeoutExpired:
                logging.exception('Pi shutdown outcome unconfirmed')
                self.send_error(503, 'Shutdown outcome unconfirmed; check the Pi before retrying')
                return
        body = json.dumps({'shutdown': 'scheduled', 'delay_seconds': 10}).encode()
        self.send_response(202)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

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
            body, kind = json.dumps(finite_json(snapshot()), allow_nan=False).encode(), 'application/json'
        elif route == '/api/system':
            body, kind = json.dumps(SYSTEM.snapshot()).encode(), 'application/json'
        elif route == '/api/history/range':
            body, kind = history_response(url.query), 'application/json'
        elif route == '/api/history':
            with database() as db:
                rows = db.execute('SELECT ts, data FROM samples WHERE ts >= ? ORDER BY ts', (time.time() - 86400,)).fetchall()
            # Five-minute buckets keep the browser payload small on the Pi 2.
            buckets = {}
            for ts, raw in rows:
                r = stored_reading(raw) or {}
                values = {key: r.get(key) for key in ('charge_W', 'battery_V')}
                buckets[int(ts // 300)] = dict(ts=ts, **{
                    key: value if isinstance(value, (int, float)) and math.isfinite(value) else None
                    for key, value in values.items()})
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
    global SHUTDOWN_PENDING
    logging.basicConfig(level=logging.INFO)
    initialize()
    STOP.clear()
    SHUTDOWN_PENDING = False
    server = ThreadingHTTPServer((os.environ.get('DASHBOARD_BIND', '0.0.0.0'), int(os.environ.get('DASHBOARD_PORT', '8080'))), Handler)
    server.daemon_threads = False
    def stop(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    collector = threading.Thread(target=poll_loop, daemon=True, name='solar-collector')
    sensor_thread = None
    shunt_thread = None
    control = None
    try:
        try:
            setup_heating()
        except (OSError, ValueError):
            logging.exception('Heating unavailable; monitoring continues')
        try:
            control = ControlServer(control_load)
        except (OSError, RuntimeError):
            logging.exception('Local LOAD control unavailable; monitoring continues')
        collector.start()
        sensor_thread = SENSOR.start(STOP)
        shunt_thread = SHUNT.start(STOP)
        logging.info('Dashboard listening on %s:%s', *server.server_address)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with SHUTDOWN_LOCK:
            SHUTDOWN_PENDING = True
        # Finish HTTP reads before the final write, so readers cannot prevent
        # SQLite committing accepted samples during an orderly shutdown.
        server.server_close()
        if control is not None:
            control.close()
        if HEATING is not None:
            HEATING.close()
        STOP.set()
        # Hardware calls have bounded timeouts; finish accepted readings before
        # flushing. systemd's normal stop timeout allows both collectors to exit.
        collector.join()
        if sensor_thread is not None:
            sensor_thread.join()
        if shunt_thread is not None:
            shunt_thread.join()
        saved = flush_history()
    return 0 if saved else 1


if __name__ == '__main__':
    sys.exit(main())
