"""Sensor persistence, upgrade safety, and independent ten-minute collection."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, Mock
import bluetooth_sensor as ble
import server


class SensorHistoryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.sensor = ble.XiaomiSensor('AA:BB:CC:DD:EE:FF')
        for name, value in (('DB', Path(directory.name)/'history.sqlite3'),
                            ('SENSOR', self.sensor),
                            ('STATE', dict(reading=None, error=None, last_attempt=None))):
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        # Start with the deployed legacy schema and an existing solar sample.
        with sqlite3.connect(server.DB) as db:
            db.execute('CREATE TABLE samples (ts REAL PRIMARY KEY, data TEXT NOT NULL)')
            db.execute('INSERT INTO samples VALUES (1, ?)', ('{"original": true}',))
        server.initialize()
        self.reading = dict(ble.decode(bytes.fromhex('cc 06 2d 40 0c')),
                            timestamp_seconds=1000, timestamp_utc='1970-01-01T00:16:40+00:00')

    def test_save_restore_and_preserve_existing_solar_history(self):
        with patch.object(self.sensor, 'read', return_value=self.reading):
            self.sensor.poll_once()
        with server.database() as db:
            self.assertEqual(db.execute('SELECT * FROM samples').fetchall(), [(1, '{"original": true}')])
            address, ts, data = db.execute('SELECT * FROM sensor_samples').fetchone()
            self.assertEqual((address, ts, json.loads(data)), (self.sensor.address, 1000, self.reading))
        self.sensor.restore(None)
        server.initialize()
        self.assertEqual(self.sensor.snapshot()['reading'], self.reading)
        self.assertEqual(self.sensor.snapshot()['poll_interval_seconds'], 600)
        self.assertEqual(self.sensor.snapshot()['stale_after_seconds'], 1800)

    def test_failures_do_not_invent_samples_and_duplicate_never_overwrites(self):
        server.save_sensor_reading(self.sensor.address, self.reading)
        changed = dict(self.reading, temperature_C=30)
        with patch.object(self.sensor, 'read', return_value=changed), self.assertLogs(level='ERROR'):
            self.sensor.poll_once()
        self.assertEqual(self.sensor.snapshot()['reading'], changed)
        self.assertIn('could not save history', self.sensor.snapshot()['error'])
        with patch.object(self.sensor, 'read', side_effect=TimeoutError), self.assertLogs(level='ERROR'):
            self.sensor.poll_once()
        with server.database() as db:
            rows = db.execute('SELECT data FROM sensor_samples').fetchall()
        self.assertEqual([json.loads(r[0]) for r in rows], [self.reading])
        with patch.object(self.sensor, 'read', return_value=dict(self.reading, timestamp_seconds=1600)):
            self.sensor.poll_once()
        self.assertIsNone(self.sensor.snapshot()['error'])

    def test_sensor_history_keeps_actual_timestamps_and_filters_address(self):
        for ts in (1000, 1600, 2200):
            server.save_sensor_reading(self.sensor.address, dict(self.reading, timestamp_seconds=ts,
                                                                temperature_C=-5, humidity_percent=0))
        server.save_sensor_reading('11:22:33:44:55:66', dict(self.reading, timestamp_seconds=1300))
        data = server.history_range('start=1000&end=2200')
        self.assertEqual(data['rows'], [])
        self.assertEqual(data['sample_count'], 0)
        self.assertEqual(data['sensor_sample_count'], 2)
        self.assertEqual(data['sensor_rows'], [dict(ts=ts, sensor_temperature_C=-5, sensor_humidity_percent=0)
                                                for ts in (1000, 1600)])
        self.assertEqual(len(data['metrics']), 20)
        self.assertEqual(data['sensor_bucket_seconds'], 600)
        self.assertEqual(data['available_end'], 2200)

    def test_sensor_query_is_bounded_and_retains_all_records(self):
        with server.database() as db:
            db.executemany('INSERT INTO sensor_samples VALUES (?, ?, ?)',
                           [(self.sensor.address, i*600, json.dumps(self.reading)) for i in range(2001)])
        data = server.history_range('start=0&end=1200600')
        self.assertLessEqual(len(data['sensor_rows']), 1000)
        self.assertEqual(data['sensor_sample_count'], 2001)
        with server.database() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sensor_samples').fetchone()[0], 2001)

    def test_restart_waits_for_remaining_interval(self):
        self.sensor.restore(self.reading)
        with patch.object(ble.time, 'time', return_value=1120), \
             patch.object(ble.time, 'sleep', side_effect=InterruptedError) as sleep, \
             patch.object(self.sensor, 'poll_once') as poll:
            with self.assertRaises(InterruptedError):
                self.sensor.poll_loop()
        sleep.assert_called_once_with(480)
        poll.assert_not_called()

    def test_polling_waits_ten_minutes_less_time_spent_reading(self):
        with patch.object(ble.time, 'monotonic', side_effect=[100, 105]), \
             patch.object(ble.time, 'sleep', side_effect=InterruptedError) as sleep, \
             patch.object(self.sensor, 'poll_once') as poll:
            with self.assertRaises(InterruptedError):
                self.sensor.poll_loop()
        sleep.assert_called_once_with(595)
        poll.assert_called_once()

    def test_failed_storage_keeps_live_data_and_recovers(self):
        self.sensor.on_reading = Mock(side_effect=sqlite3.OperationalError('disk full'))
        with patch.object(self.sensor, 'read', return_value=self.reading), self.assertLogs(level='ERROR'):
            self.sensor.poll_once()
        self.assertEqual(self.sensor.snapshot()['reading'], self.reading)
        self.assertIn('could not save history', self.sensor.snapshot()['error'])
        self.sensor.on_reading = server.save_sensor_reading
        with patch.object(self.sensor, 'read', return_value=self.reading):
            self.sensor.poll_once()
        self.assertIsNone(self.sensor.snapshot()['error'])
