"""Tests for scheduling, persistence, and failure handling without real hardware."""
from contextlib import contextmanager
import datetime
import json
import io
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import server


class DashboardTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for name, value in (
            ('DB', Path(directory.name) / 'history.sqlite3'),
            ('STATE', {'reading': None, 'error': None, 'last_attempt': None}),
        ):
            patched = patch.object(server, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        server.initialize()
        self.reading = {
            'timestamp_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'charge_W': 42, 'battery_V': 27.2,
        }
        self.result = subprocess.CompletedProcess([], 0, json.dumps(self.reading), '')

    def test_database_closes_on_success_and_failure(self):
        with server.database() as db:
            db.execute('INSERT INTO samples VALUES (1, ?)', ('{}',))
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute('SELECT 1')
        with self.assertRaises(RuntimeError):
            with server.database() as failed_db:
                failed_db.execute('INSERT INTO samples VALUES (2, ?)', ('{}',))
                raise RuntimeError('rollback')
        with self.assertRaises(sqlite3.ProgrammingError):
            failed_db.execute('SELECT 1')
        with server.database() as db:
            self.assertEqual(db.execute('SELECT ts FROM samples').fetchall(), [(1.0,)])

    def test_controller_timeout_preserves_last_reading_and_recovers(self):
        server.STATE['reading'] = self.reading
        with patch.object(server.subprocess, 'run', side_effect=subprocess.TimeoutExpired('reader', 15)), self.assertLogs(level='ERROR'):
            server.poll_once()
        self.assertEqual(server.STATE['reading'], self.reading)
        self.assertIn('Controller read failed', server.STATE['error'])
        with patch.object(server.subprocess, 'run', return_value=self.result):
            server.poll_once()
        self.assertIsNone(server.STATE['error'])

    def test_storage_failure_keeps_live_value_and_reports_storage(self):
        @contextmanager
        def unavailable():
            raise sqlite3.OperationalError('database or disk is full')
            yield
        with patch.object(server, 'database', unavailable), \
             patch.object(server.subprocess, 'run', return_value=self.result), \
             self.assertLogs(level='ERROR'):
            server.poll_once()
        self.assertEqual(server.STATE['reading'], self.reading)
        self.assertIn('could not save history', server.STATE['error'])
        with patch.object(server.subprocess, 'run', return_value=self.result):
            server.poll_once()
        self.assertIsNone(server.STATE['error'])

    def test_repeated_timestamp_does_not_overwrite_history(self):
        with server.database() as db:
            db.execute('INSERT INTO samples VALUES (123, ?)', ('{"original": true}',))
        with patch.object(server.time, 'time', return_value=123), \
             patch.object(server.subprocess, 'run', return_value=self.result), \
             self.assertLogs(level='ERROR'):
            server.poll_once()
        with server.database() as db:
            self.assertEqual(json.loads(db.execute('SELECT data FROM samples WHERE ts=123').fetchone()[0]), {'original': True})

    def test_freshness_and_interval(self):
        self.assertTrue(server.snapshot()['stale'])
        server.STATE['reading'] = self.reading
        timestamp = datetime.datetime.fromisoformat(self.reading['timestamp_utc']).timestamp()
        with patch.object(server.time, 'time', return_value=timestamp + 120):
            state = server.snapshot()
            self.assertFalse(state['stale'])
            self.assertEqual(state['poll_interval_seconds'], 60)
            self.assertEqual(state['stale_after_seconds'], 180)
        with patch.object(server.time, 'time', return_value=timestamp + 181):
            self.assertTrue(server.snapshot()['stale'])

    def test_schedule_accounts_for_time_spent_reading(self):
        with patch.object(server, 'poll_once') as poll, \
             patch.object(server.time, 'monotonic', side_effect=[100, 105]), \
             patch.object(server.time, 'sleep', side_effect=InterruptedError) as sleep:
            with self.assertRaises(InterruptedError):
                server.poll_loop()
            poll.assert_called_once()
            sleep.assert_called_once_with(55)

    def test_history_api_is_bounded_without_deleting_older_data(self):
        with server.database() as db:
            db.executemany('INSERT INTO samples VALUES (?, ?)', [
                (1, json.dumps(self.reading)),
                (1999810, json.dumps(self.reading)),
                (1999870, json.dumps(self.reading)),
            ])
        handler = server.Handler.__new__(server.Handler)
        handler.path = '/api/history'
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        with patch.object(server.time, 'time', return_value=2000000):
            handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        rows = json.loads(handler.wfile.getvalue())
        self.assertEqual([row['ts'] for row in rows], [1999870])
        with server.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM samples').fetchone()[0], 3)

    def test_history_failure_returns_service_unavailable(self):
        handler = server.Handler.__new__(server.Handler)
        handler.path = '/api/history'
        handler.send_error = Mock()
        with patch.object(server, 'database', side_effect=sqlite3.OperationalError('database unavailable')), \
             self.assertLogs(level='ERROR'):
            handler.do_GET()
        handler.send_error.assert_called_once_with(503, 'Dashboard data temporarily unavailable')


if __name__ == '__main__':
    unittest.main()
