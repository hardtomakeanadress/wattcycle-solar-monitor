"""Durable, bounded fault transitions and resumable long polling; no hardware writes."""
from contextlib import contextmanager
import datetime
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
import uuid

LIMIT = 2000

class ImportantEvents:
    def __init__(self, path):
        self.path = Path(path)
        self.condition = threading.Condition()
        self.active = {}
        self.events = []
        self.generation = str(uuid.uuid4())
        self.sequence = 0
        self.storage_error = None
        self.pending = []
        self.started = time.monotonic()
        self.candidates = {}
        try:
            with self.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, data TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY, data TEXT NOT NULL)')
                row = db.execute('SELECT data FROM metadata WHERE id=1').fetchone()
                if row:
                    data = json.loads(row[0])
                    self.generation = data['generation']
                    self.active = data['active']
                self.events = [json.loads(r[0]) for r in db.execute('SELECT data FROM events ORDER BY seq')]
                self.sequence = self.events[-1]['seq'] if self.events else 0
        except (OSError, sqlite3.Error, ValueError, KeyError):
            self.storage_error = 'Important-event storage unavailable'
            logging.exception('Cannot load important-event history')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def persist(self):
        try:
            with self.connect() as db:
                db.executemany('INSERT OR REPLACE INTO events VALUES (?,?)',
                               [(e['seq'], json.dumps(e)) for e in self.pending])
                db.execute('INSERT OR REPLACE INTO metadata VALUES (1,?)',
                           (json.dumps(dict(generation=self.generation, active=self.active)),))
                db.execute('DELETE FROM events WHERE seq <= ?', (self.sequence - LIMIT,))
            self.pending.clear()
            self.storage_error = None
        except (OSError, sqlite3.Error):
            self.storage_error = 'Important-event storage unavailable; HA delivery continues'
            self.pending = self.pending[-LIMIT:]

    def emit(self, code, message, active=True, severity='error', timestamp_utc=None):
        with self.condition:
            self.sequence += 1
            event = dict(seq=self.sequence, generation=self.generation, code=code,
                         message=str(message)[:500], active=active, severity=severity,
                         timestamp_utc=timestamp_utc or datetime.datetime.now(datetime.timezone.utc).isoformat())
            if active:
                self.active[code] = event
            else:
                self.active.pop(code, None)
            self.events.append(event)
            self.events = self.events[-LIMIT:]
            self.pending.append(event)
            self.persist()
            self.condition.notify_all()
            return event

    def update(self, problems, delays=None):
        now = time.monotonic()
        delays = delays or {}
        with self.condition:
            for code in set(self.candidates) - set(problems):
                self.candidates.pop(code, None)
            for code, message in problems.items():
                since = self.candidates.setdefault(code, now)
                if code not in self.active and now - since >= delays.get(code, 0):
                    self.emit(code, message)
                elif code in self.active and self.active[code]['message'] != message:
                    self.emit(code, message)
            for code in set(self.active) - set(problems):
                # Kernel/recovery events are historical incidents, not persistent conditions.
                if not code.startswith(('kernel_', 'recovery_')):
                    self.emit(code, self.active[code]['message'] + ' — recovered', False, 'info')
            if self.pending:
                self.persist()

    def response(self, after=0, generation=None, wait=0):
        with self.condition:
            reset = bool(generation and generation != self.generation) or after > self.sequence
            if not reset and after == self.sequence and wait:
                self.condition.wait(timeout=min(wait, 25))
            oldest = self.events[0]['seq'] if self.events else self.sequence + 1
            gap = not reset and after > 0 and after < oldest - 1
            selected = [e for e in self.events if reset or e['seq'] > after][:200]
            return dict(generation=self.generation, cursor=selected[-1]['seq'] if selected else self.sequence,
                        events=selected, active=list(self.active.values()), reset=reset, gap=gap,
                        storage_error=self.storage_error)


def problems(status, system):
    result = {}
    reading = status.get('reading') or {}
    if reading.get('fault_bits'):
        result['controller_fault'] = 'Solar controller fault code: ' + str(reading['fault_bits'])
    if status.get('stale'):
        result['controller_missing'] = 'Solar controller data is stale'
    shunt = status.get('shunt') or {}
    sr = shunt.get('reading') or {}
    if sr.get('alarm_bits'):
        result['battery_alarm'] = 'Battery alarm: ' + (', '.join(sr.get('alarms') or []) or str(sr['alarm_bits']))
    if shunt.get('enabled') and shunt.get('stale'):
        result['shunt_missing'] = 'SmartShunt data is stale'
    history = status.get('history') or {}
    if history.get('error'):
        result['history_storage'] = 'Solar/climate history cannot be saved'
    dropped = (history.get('dropped_solar_samples') or 0) + (history.get('dropped_sensor_samples') or 0)
    if dropped:
        result['history_dropped'] = f'History buffer lost {dropped} readings'
    heating = status.get('heating') or {}
    if heating.get('alarm'):
        result['heating_alarm'] = str(heating['alarm'])
    if system.get('throttle_current'):
        result['pi_power_thermal'] = 'Pi: ' + ', '.join(system['throttle_current'])
    if system.get('throttle_since_boot'):
        result['pi_power_thermal_since_boot'] = 'Pi fault recorded since boot: ' + ', '.join(system['throttle_since_boot'])
    free, total = system.get('storage_free_bytes'), system.get('storage_total_bytes')
    if free is not None and total and free < max(200 * 1024**2, total * .02):
        result['pi_disk_low'] = 'Pi storage critically low'
    free, total = system.get('memory_available_bytes'), system.get('memory_total_bytes')
    if free is not None and total and free < total * .05:
        result['pi_memory_low'] = 'Pi available memory below 5%'
    return result


def monitor(events, snapshot, system, stop):
    while not stop.is_set():
        try:
            # Allow collectors to start before diagnosing missing data after a restart.
            values = problems(snapshot(), system.snapshot())
            if time.monotonic() - events.started < 60:
                values.pop('shunt_missing', None)
                values.pop('controller_missing', None)
            recovery_path = Path('/run/cabana-recovery/status.json')
            if recovery_path.exists():
                recovery = json.loads(recovery_path.read_text())
                if time.time() - recovery['checked_at'] > 90:
                    values['pi_recovery_missing'] = 'Pi automatic recovery monitor is not responding'
                else:
                    for name in recovery.get('failed_probes', []):
                        values['pi_' + name + '_health'] = 'Pi ' + name + ' health check failing'
            events.update(values, {'pi_memory_low': 60, 'history_storage': 10})
            incident_file = Path('/var/lib/cabana-recovery/events.json')
            if incident_file.exists():
                incidents = json.loads(incident_file.read_text())
                known = {e['code'] for e in events.events}
                for incident in incidents[-100:]:
                    code = 'recovery_' + incident['id']
                    if code not in known:
                        events.emit(code, incident['message'], False, incident.get('severity', 'error'),
                                    datetime.datetime.fromtimestamp(incident['timestamp'], datetime.timezone.utc).isoformat())
        except Exception:
            logging.exception('Important-event monitor failed')
        stop.wait(1)
