"""Storage batching, rollback/retry, interval and real shutdown regression tests."""
from contextlib import contextmanager
import datetime
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import bluetooth_sensor
import server


class BatchHistoryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for name, value in (
            ('DB', Path(directory.name) / 'history.sqlite3'),
            ('STATE', dict(reading=None, error=None, last_attempt=None)),
            ('SENSOR', bluetooth_sensor.XiaomiSensor('AA:BB:CC:DD:EE:FF')),
            ('STOP', threading.Event()),
        ):
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        server.initialize()
        self.reading = dict(timestamp_seconds=1000, timestamp_utc='1970-01-01T00:16:40+00:00',
                            temperature_C=20, humidity_percent=50, battery_V=3)
        self.solar = dict(timestamp_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          charge_W=42, battery_V=27.2)
        self.result = subprocess.CompletedProcess([], 0, json.dumps(self.solar), '')

    def test_both_sources_share_one_transaction_and_keep_original_times(self):
        server.save_sensor_reading(server.SENSOR.address, self.reading)
        with server.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sensor_samples').fetchone()[0], 0)
        statements = []
        connect = sqlite3.connect
        def traced(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with patch.object(server.sqlite3, 'connect', side_effect=traced), \
             patch.object(server.subprocess, 'run', return_value=self.result), \
             patch.object(server.time, 'time', return_value=1300):
            server.poll_once()
        self.assertEqual(sum(s.startswith('BEGIN') for s in statements), 1)
        self.assertEqual(statements.count('COMMIT'), 1)
        with server.database() as db:
            self.assertEqual(db.execute('SELECT ts FROM samples').fetchall(), [(1300,)])
            self.assertEqual(db.execute('SELECT ts FROM sensor_samples').fetchall(), [(1000,)])
        self.assertEqual(server.history_status()['pending_sensor_samples'], 0)
        # Empty batches do not even open SQLite.
        with patch.object(server, 'database', side_effect=AssertionError('empty commit')):
            self.assertTrue(server.flush_history())

    def test_rollback_retains_whole_batch_and_retries_without_new_controller_data(self):
        database = server.database
        @contextmanager
        def fail_after_inserts():
            with database() as db:
                yield db
                raise sqlite3.OperationalError('simulated disk full at commit')
        server.save_sensor_reading(server.SENSOR.address, self.reading)
        with patch.object(server, 'database', fail_after_inserts), \
             patch.object(server.subprocess, 'run', return_value=self.result), \
             self.assertLogs(level='ERROR'):
            server.poll_once()
        with database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM samples').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM sensor_samples').fetchone()[0], 0)
        status = server.snapshot()
        self.assertEqual(status['reading'], self.solar)
        self.assertIn('could not save history', status['error'])
        self.assertEqual(status['history']['pending_solar_samples'], 1)
        self.assertEqual(status['history']['pending_sensor_samples'], 1)
        with patch.object(server.subprocess, 'run', side_effect=TimeoutError), self.assertLogs(level='ERROR'):
            server.poll_once()
        with database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM samples').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM sensor_samples').fetchone()[0], 1)
        self.assertIsNone(server.history_status()['error'])
        self.assertIn('Controller read failed', server.snapshot()['error'])

    def test_timestamp_collision_preserves_old_row_without_blocking_batch(self):
        with server.database() as db:
            db.execute('INSERT INTO samples VALUES (1300, ?)', ('{"original":true}',))
        server.save_sensor_reading(server.SENSOR.address, self.reading)
        server.save_sensor_reading(server.SENSOR.address, dict(self.reading, temperature_C=99))
        with patch.object(server.time, 'time', return_value=1300), \
             patch.object(server.subprocess, 'run', return_value=self.result), self.assertLogs(level='ERROR'):
            server.poll_once()
        with server.database() as db:
            self.assertEqual(json.loads(db.execute('SELECT data FROM samples').fetchone()[0]), {'original': True})
            self.assertEqual(json.loads(db.execute('SELECT data FROM sensor_samples').fetchone()[0]), self.reading)
        self.assertIsNone(server.history_status()['error'])
        self.assertEqual(server.history_status()['pending_sensor_samples'], 0)

    def test_arrival_during_commit_is_saved_in_next_batch(self):
        entered = threading.Event()
        release = threading.Event()
        database = server.database
        @contextmanager
        def delayed():
            with database() as db:
                yield db
                entered.set()
                if not release.wait(2):
                    raise TimeoutError('test commit not released')
        server.save_sensor_reading(server.SENSOR.address, self.reading)
        with patch.object(server, 'database', delayed):
            writer = threading.Thread(target=server.flush_history)
            writer.start()
            self.assertTrue(entered.wait(2))
            incoming = threading.Thread(target=server.save_sensor_reading,
                                        args=(server.SENSOR.address, dict(self.reading, timestamp_seconds=1600)))
            incoming.start()
            release.set()
            writer.join(2)
            incoming.join(2)
            self.assertFalse(writer.is_alive() or incoming.is_alive())
        self.assertEqual(server.history_status()['pending_sensor_samples'], 1)
        server.flush_history()
        with database() as db:
            self.assertEqual(db.execute('SELECT ts FROM sensor_samples ORDER BY ts').fetchall(), [(1000,), (1600,)])

    def test_full_buffer_preserves_accepted_rows_counts_loss_and_recovers(self):
        with patch.object(server, 'PENDING_LIMITS', {'solar': 3, 'sensor': 2}):
            with patch.object(server, 'database', side_effect=sqlite3.OperationalError('read-only')), \
                 patch.object(server.subprocess, 'run', return_value=self.result), self.assertLogs(level='ERROR'):
                for i in range(8):
                    with patch.object(server.time, 'time', return_value=1000 + i*300):
                        server.save_sensor_reading(server.SENSOR.address, dict(self.reading, timestamp_seconds=1000+i*300))
                        server.poll_once()
            status = server.snapshot()
            self.assertEqual(status['reading'], self.solar)  # Live data still updates.
            h = status['history']
            self.assertEqual((h['pending_solar_samples'], h['pending_sensor_samples']), (3, 2))
            self.assertEqual((h['dropped_solar_samples'], h['dropped_sensor_samples']), (5, 6))
            self.assertEqual(h['pending_limits'], {'solar': 3, 'sensor': 2})
            with patch.object(server.time, 'time', return_value=3700):
                self.assertEqual(server.history_status()['oldest_pending_age_seconds'], 2700)
            self.assertTrue(server.flush_history())
            with server.database() as db:
                self.assertEqual(db.execute('SELECT ts FROM samples ORDER BY ts').fetchall(), [(1000,), (1300,), (1600,)])
                self.assertEqual(db.execute('SELECT ts FROM sensor_samples ORDER BY ts').fetchall(), [(1000,), (1300,)])
            h = server.history_status()
            self.assertIsNone(h['error'])
            self.assertIsNone(h['oldest_pending_age_seconds'])
            self.assertEqual(h['dropped_solar_samples'], 5)  # Loss stays visible after recovery.
            server.save_sensor_reading(server.SENSOR.address, dict(self.reading, timestamp_seconds=4000))
            self.assertTrue(server.flush_history())
            with server.database() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM sensor_samples').fetchone()[0], 3)

    def test_default_capacity_is_seven_days_per_source(self):
        self.assertEqual(server.PENDING_LIMITS, {'solar': 2016, 'sensor': 1008})

    def test_restart_waits_remaining_five_minutes(self):
        with server.database() as db:
            db.execute('INSERT INTO samples VALUES (1000, ?)', (json.dumps(self.solar),))
        server.initialize()
        with patch.object(server.time, 'time', return_value=1120), \
             patch.object(server.STOP, 'wait', return_value=True) as wait, \
             patch.object(server, 'poll_once') as poll:
            server.poll_loop()
        wait.assert_called_once_with(180)
        poll.assert_not_called()
        self.assertEqual(server.snapshot()['reading'], self.solar)
        self.assertEqual(server.history_range('start=0&end=2000')['metrics'][0]['poll_interval_seconds'], 300)

    def test_sigterm_flushes_pending_sensor_and_exits_cleanly(self):
        # Exercise the actual HTTP server, collector threads and signal handler,
        # substituting only hardware I/O. Readiness is printed after enqueueing.
        code = '''
import json, subprocess, time
import server
from bluetooth_sensor import XiaomiSensor
server.SENSOR = XiaomiSensor('AA:BB:CC:DD:EE:FF')
server.subprocess.run = lambda *a, **k: subprocess.CompletedProcess([], 0,
    json.dumps({'timestamp_utc':'2026-10-07T00:00:00+00:00','charge_W':1,'battery_V':27}), '')
original_initialize = server.initialize
def initialize():
    original_initialize()
    original_save = server.SENSOR.on_reading
    def saved(address, reading):
        original_save(address, reading)
        print('queued', flush=True)
    server.SENSOR.on_reading = saved
server.initialize = initialize
def read():
    time.sleep(0.15)
    return {'timestamp_seconds':1000,'timestamp_utc':'1970-01-01T00:16:40+00:00',
            'temperature_C':20,'humidity_percent':50,'battery_V':3}
server.SENSOR.read = read
raise SystemExit(server.main())
'''
        env = dict(os.environ, SOLAR_DATABASE=str(server.DB), DASHBOARD_BIND='127.0.0.1', DASHBOARD_PORT='0',
                   XDG_RUNTIME_DIR=str(server.DB.parent))
        process = subprocess.Popen([sys.executable, '-u', '-c', code], env=env,
                                   cwd=Path(server.__file__).parent,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate()
        self.addCleanup(cleanup)
        # A timeout on readiness prevents a broken child from hanging the suite.
        import selectors
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(ready.select(5), 'collector did not queue its reading')
            message = process.stdout.readline().strip()
            if message != 'queued':
                _, stderr = process.communicate(timeout=5)
                self.fail('collector exited before queueing: ' + stderr)
        with server.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sensor_samples').fetchone()[0], 0)
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        with server.database() as db:
            self.assertEqual(db.execute('SELECT ts FROM sensor_samples').fetchall(), [(1000,)])


if __name__ == '__main__':
    unittest.main()
