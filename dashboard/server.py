"""Small standard-library dashboard. One serial reader; browsers use cached data."""
import datetime
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
READER = ROOT.parent / 'read_controller.py'
DB = Path(os.environ.get('SOLAR_DATABASE', str(ROOT / 'history.sqlite3'))).expanduser()
INTERVAL = 60
STALE_AFTER = INTERVAL * 3
LOCK = threading.Lock()
STATE = {'reading': None, 'error': None, 'last_attempt': None}


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
    return data


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            self.respond()
        except (BrokenPipeError, ConnectionResetError):
            pass  # A browser closed the connection.
        except (sqlite3.Error, OSError, ValueError, KeyError):
            logging.exception('Dashboard request failed')
            self.send_error(503, 'Dashboard data temporarily unavailable')

    def respond(self):
        route = self.path.split('?', 1)[0]
        if route == '/':
            body, kind = (ROOT / 'index.html').read_bytes(), 'text/html; charset=utf-8'
        elif route == '/api/status':
            body, kind = json.dumps(snapshot()).encode(), 'application/json'
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
    server = ThreadingHTTPServer((os.environ.get('DASHBOARD_BIND', '0.0.0.0'), int(os.environ.get('DASHBOARD_PORT', '8080'))), Handler)
    server.daemon_threads = True
    logging.info('Dashboard listening on %s:%s', *server.server_address)
    server.serve_forever()
