"""History navigation must preserve all data and work on old JSON records."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import server


class HistoryTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        p = patch.object(server, 'DB', Path(folder.name) / 'history.sqlite3')
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(server, 'STATE', dict(reading=None, error=None, last_attempt=None))
        p.start()
        self.addCleanup(p.stop)
        server.initialize()

    def insert(self, rows):
        with server.database() as db:
            db.executemany('INSERT INTO samples VALUES (?, ?)', [(ts, json.dumps(r)) for ts, r in rows])

    def test_all_metrics_old_records_missing_values_and_zero(self):
        self.insert([(100, {'charge_W': 0, 'battery_V': 26.4, 'PV_V': 42,
                            'charge_status': 'MPPT', 'load_on': False, 'controller_C': -4}),
                     (200, {'charge_W': 5, 'charge_status': 'new-state'})])
        data = server.history_range('start=100&end=201')
        first, second = data['rows']
        self.assertEqual(first['charge_W'], 0)
        self.assertEqual(first['load_on'], 0)
        self.assertEqual(first['charge_status'], 2)
        self.assertEqual(first['controller_C'], -4)
        self.assertEqual(first['PV_V'], 42)
        self.assertIsNone(second['battery_V'])
        self.assertIsNone(second['charge_status'])
        self.assertEqual({m['key'] for m in data['metrics']}, set(first) - {'ts'})
        self.assertGreater(data['storage']['free_bytes'], 0)

    def test_custom_range_inclusive_start_exclusive_end(self):
        self.insert([(99, {}), (100, {}), (160, {}), (200, {})])
        data = server.history_range('start=100&end=200')
        self.assertEqual([r['ts'] for r in data['rows']], [100, 160])
        self.assertEqual(data['sample_count'], 2)
        self.assertEqual((data['available_start'], data['available_end']), (99, 200))

    def test_large_range_bounded_and_does_not_delete_five_year_old_data(self):
        self.insert([(60*i, {'charge_W': i}) for i in range(2001)])
        with patch.object(server.time, 'time', return_value=5*366*86400):
            data = server.history_range('range=all')
        self.assertLessEqual(len(data['rows']), 1000)
        self.assertEqual(data['sample_count'], 2001)
        self.assertEqual(data['rows'][-1]['charge_W'], 2000)
        with server.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM samples').fetchone()[0], 2001)
        self.assertEqual(data['available_start'], 0)

    def test_last_sample_per_bucket_and_sorted_results(self):
        self.insert([(121, {'charge_W': 3}), (61, {'charge_W': 1}), (62, {'charge_W': 2})])
        data = server.history_range('start=0&end=180')
        self.assertEqual([r['charge_W'] for r in data['rows']], [2, 3])

    def test_empty_database_and_empty_selected_range(self):
        data = server.history_range('range=all')
        self.assertEqual(data['rows'], [])
        self.assertIsNone(data['available_start'])
        self.insert([(100, {})])
        self.assertEqual(server.history_range('start=200&end=300')['rows'], [])

    def test_invalid_ranges_return_400(self):
        for query in ['start=nan', 'end=inf', 'start=-1', 'start=20&end=10',
                      'start=10&end=10', 'start=x', 'start=', 'range=other',
                      'range=all&start=1', 'start=1&start=2', 'unknown=1']:
            with self.subTest(query=query):
                handler = server.Handler.__new__(server.Handler)
                handler.path = '/api/history/range?' + query
                handler.send_error = Mock()
                handler.do_GET()
                self.assertEqual(handler.send_error.call_args.args[0], 400)

    def test_http_range_response(self):
        self.insert([(100, {'battery_V': 27.3})])
        handler = server.Handler.__new__(server.Handler)
        handler.path = '/api/history/range?start=0&end=200'
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(json.loads(handler.wfile.getvalue())['rows'][0]['battery_V'], 27.3)
