"""Regression check: new readings must never expire historical records."""
import datetime
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import server


class RetentionTest(unittest.TestCase):
    def test_old_readings_survive_poll_and_restart(self):
        now = time.time()
        old_samples = [
            (now - days * 86400, json.dumps({'historical_sample': days}))
            for days in (8, 365, 365 * 5)
        ]
        reading = {
            'timestamp_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'charge_W': 42,
            'battery_V': 27.2,
        }
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(server, 'DB', Path(directory) / 'history.sqlite3'), \
             patch.object(server, 'STATE', {'reading': None, 'error': None, 'last_attempt': None}):
            server.initialize()
            with server.database() as db:
                db.executemany('INSERT INTO samples VALUES (?, ?)', old_samples)
            result = subprocess.CompletedProcess([], 0, json.dumps(reading), '')
            with patch.object(server.subprocess, 'run', return_value=result):
                server.poll_once()
            self.assertIsNone(server.STATE['error'])
            # Simulate loading the same database after a service restart.
            server.STATE['reading'] = None
            server.initialize()
            self.assertEqual(server.STATE['reading'], reading)
            with server.database() as db:
                rows = db.execute('SELECT ts, data FROM samples ORDER BY ts').fetchall()
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[:3], sorted(old_samples))


if __name__ == '__main__':
    unittest.main()
