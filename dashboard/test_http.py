"""HTTP contracts, damaged history and bounded query reuse; no hardware I/O."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import bluetooth_sensor
import server


class HTTPTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for name, value in [('DB', Path(directory.name) / 'history.sqlite3'),
                            ('STOP', threading.Event()),
                            ('SHUTDOWN_PENDING', False),
                            ('STATE', dict(reading=None, error=None, last_attempt=None)),
                            ('SENSOR', bluetooth_sensor.XiaomiSensor('AA:BB:CC:DD:EE:FF'))]:
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        server.initialize()

    def request(self, path):
        handler = object.__new__(server.Handler)
        handler.path, handler.wfile = path, io.BytesIO()
        handler.send_response, handler.send_error = Mock(), Mock()
        handler.send_header, handler.end_headers = Mock(), Mock()
        handler.do_GET()
        return handler

    def test_page_status_and_headers_without_hardware_reads(self):
        with patch.object(server, 'poll_once', side_effect=AssertionError('hardware read')):
            for path, content_type in [('/', 'text/html; charset=utf-8'), ('/api/status', 'application/json')]:
                h = self.request(path)
                h.send_response.assert_called_once_with(200)
                h.send_error.assert_not_called()
                headers = dict(c.args for c in h.send_header.call_args_list)
                self.assertEqual(headers, {'Content-Type': content_type,
                    'Content-Length': str(len(h.wfile.getvalue())), 'Cache-Control': 'no-store',
                    'X-Content-Type-Options': 'nosniff'})
                h.end_headers.assert_called_once()
                if path == '/':
                    self.assertEqual(h.wfile.getvalue(), (server.ROOT / 'index.html').read_bytes())
                else:
                    self.assertIsNone(json.loads(h.wfile.getvalue())['reading'])
        self.request('/missing').send_error.assert_called_once_with(404)

    def test_browser_disconnects_are_ignored(self):
        for error in (BrokenPipeError, ConnectionResetError):
            with patch.object(server.Handler, 'respond', side_effect=error):
                self.request('/api/status').send_error.assert_not_called()

    def test_restored_record_without_timestamp_remains_visible_but_stale(self):
        with server.database() as db:
            db.execute('INSERT INTO samples VALUES (100, ?)', ('{"charge_W":42}',))
        server.initialize()
        h = self.request('/api/status')
        h.send_response.assert_called_once_with(200)
        data = json.loads(h.wfile.getvalue())
        self.assertEqual(data['reading']['charge_W'], 42)
        self.assertTrue(data['stale'])
        self.assertIsNone(data['age_seconds'])
        self.assertIn('timestamp', data['error'])

    def test_damaged_and_incomplete_records_are_gaps_without_rewriting(self):
        records = [(100, '{broken'), (700, '[1, 2]'), (1300, '{"battery_V":27}'),
                   (1900, '{"charge_W":0}'), (2500, '{"charge_W":NaN}')]
        with server.database() as db:
            db.executemany('INSERT INTO samples VALUES (?, ?)', records)
            db.executemany('INSERT INTO sensor_samples VALUES (?, ?, ?)',
                [(server.SENSOR.address, 100, 'null'),
                 (server.SENSOR.address, 700, '{"temperature_C":0,"humidity_percent":50}')])
        with patch.object(server.time, 'time', return_value=3000):
            for path in ('/api/history/range?range=all', '/api/history'):
                h = self.request(path)
                h.send_response.assert_called_once_with(200)
                data = json.loads(h.wfile.getvalue())
                rows = data['rows'] if isinstance(data, dict) else data
                self.assertEqual([r['ts'] for r in rows], [100, 700, 1300, 1900, 2500])
                self.assertEqual([r['charge_W'] for r in rows], [None, None, None, 0, None])
                self.assertEqual(rows[2]['battery_V'], 27)
                if isinstance(data, dict):
                    self.assertEqual((data['invalid_solar_rows'], data['invalid_sensor_rows']), (2, 1))
                    self.assertIsNone(data['sensor_rows'][0]['sensor_temperature_C'])
                    self.assertEqual(data['sensor_rows'][1]['sensor_temperature_C'], 0)
                    self.assertEqual(data['sample_count'], 5)
        with server.database() as db:
            self.assertEqual(db.execute('SELECT * FROM samples ORDER BY ts').fetchall(), records)

    def test_unreadable_latest_records_do_not_prevent_startup(self):
        with server.database() as db:
            db.execute('INSERT INTO samples VALUES (100, ?)', ('{broken',))
            db.execute('INSERT INTO sensor_samples VALUES (?, 100, ?)', (server.SENSOR.address, '{}'))
        with self.assertLogs(level='ERROR'):
            server.initialize()
        self.assertIsNone(server.snapshot()['reading'])
        self.assertIsNone(server.STATE['last_attempt'])
        self.assertIsNone(server.SENSOR.snapshot()['reading'])

    def test_nonfinite_restored_values_are_null_and_bad_sensor_is_not_restored(self):
        with server.database() as db:
            db.execute('INSERT INTO samples VALUES (100, ?)',
                       ('{"timestamp_utc":"2026-10-25T00:00:00+00:00","charge_W":NaN,"battery_V":1e999}',))
            db.execute('INSERT INTO sensor_samples VALUES (?, 100, ?)',
                       (server.SENSOR.address, '{"timestamp_seconds":100,"temperature_C":20}'))
        with self.assertLogs(level='ERROR'):
            server.initialize()
        raw = self.request('/api/status').wfile.getvalue()
        data = json.loads(raw, parse_constant=lambda value: self.fail('Non-JSON number ' + value))
        self.assertIsNone(data['reading']['charge_W'])
        self.assertIsNone(data['reading']['battery_V'])
        self.assertIsNone(data['sensor']['reading'])

    def test_cache_reuse_expiry_size_and_commit_invalidation(self):
        with patch.object(server.time, 'monotonic', return_value=10) as clock, \
                patch.object(server, 'history_range', wraps=server.history_range) as query:
            first = server.history_response('range=all')
            clock.return_value = 39
            self.assertEqual(server.history_response('range=all'), first)
            self.assertEqual(query.call_count, 1)
            clock.return_value = 40
            server.history_response('range=all')
            self.assertEqual(query.call_count, 2)
            server.queue_reading('solar', (100, '{"charge_W":42}'))
            self.assertTrue(server.flush_history())
            refreshed = json.loads(server.history_response('range=all'))
            self.assertEqual(refreshed['sample_count'], 1)
            self.assertEqual(query.call_count, 3)
            for end in range(200, 220):
                server.history_response(f'start=0&end={end}')
            self.assertEqual(len(server.HISTORY_CACHE), server.HISTORY_CACHE_LIMIT)
        with patch.object(server, 'history_range', side_effect=ValueError) as query:
            for _ in range(2):
                with self.assertRaises(ValueError):
                    server.history_response('start=invalid')
            self.assertEqual(query.call_count, 2)

    def test_concurrent_identical_queries_share_one_database_result(self):
        def slow_query(query):
            time.sleep(.02)
            return {'rows': []}
        with patch.object(server, 'history_range', side_effect=slow_query) as query:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(server.history_response, ['range=all'] * 4))
        query.assert_called_once_with('range=all')
        self.assertEqual(results, [b'{"rows": []}'] * 4)

    def test_idle_connection_closes_after_timeout(self):
        # Real socket lifecycle, with a shorter timeout only for this test.
        with patch.object(server.Handler, 'timeout', .1):
            http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            thread = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': .01})
            thread.start()
            try:
                with socket.create_connection(http.server_address, timeout=2) as client:
                    self.assertEqual(client.recv(1), b'')
            finally:
                http.shutdown()
                thread.join(2)
                http.server_close()
        self.assertEqual(server.Handler.timeout, 30)

    def test_shutdown_without_sensor_and_failed_final_flush(self):
        with patch.object(server, 'ImportantEvents'), patch.object(server, 'initialize'), patch.object(server, 'SENSOR', bluetooth_sensor.XiaomiSensor()), \
                patch.object(server, 'setup_heating'), patch.object(server, 'HEATING', None), \
                patch.object(server, 'ControlServer') as control, \
                patch.object(server, 'ThreadingHTTPServer') as http, \
                patch.object(server.threading, 'Thread') as thread, \
                patch.object(server.signal, 'signal'), \
                patch.object(server, 'flush_history', return_value=False) as flush:
            lifecycle = Mock()
            lifecycle.attach_mock(http.return_value.server_close, 'close_http')
            lifecycle.attach_mock(flush, 'flush')
            http.return_value.serve_forever.side_effect = KeyboardInterrupt
            self.assertEqual(server.main(), 1)
            self.assertEqual(thread.return_value.start.call_count, 2)
            thread.return_value.join.assert_called_once()
            http.return_value.server_close.assert_called_once()
            control.return_value.close.assert_called_once()
            self.assertEqual([call[0] for call in lifecycle.mock_calls], ['close_http', 'flush'])
            self.assertFalse(http.return_value.daemon_threads)

    def post(self, body=b'{"armed":true}', key='test-key', origin='http://cabana:8080', **headers):
        h = object.__new__(server.Handler)
        h.connection = Mock()
        h.connection.getsockname.return_value = ('127.0.0.1', 8080)
        h.path, h.rfile, h.wfile = '/api/heating', io.BytesIO(body), io.BytesIO()
        h.headers = {'Host':'cabana:8080', 'Origin':origin, 'Authorization':'Bearer '+key,
                     'Content-Type':'application/json', 'Content-Length':str(len(body)), **headers}
        h.send_response, h.send_error = Mock(), Mock()
        h.send_header, h.end_headers = Mock(), Mock()
        with patch.object(server.socket, 'gethostname', return_value='cabana'):
            h.do_POST()
        return h

    def test_heating_auth_origin_and_payload_before_hardware_access(self):
        with patch.object(server, 'HEATING_KEY', 'test-key'), patch.object(server, 'HEATING') as heating:
            for kwargs, code in [({'key':'wrong'},403), ({'origin':'http://evil.invalid'},403),
                                 ({'body':b'{"armed":1}'},400), ({'body':b'null'},400),
                                 ({'body':b'{"armed":true,"extra":1}'},400),
                                 ({'Content-Type':'text/plain'},400),
                                 ({'Content-Length':'99999'},400)]:
                h = self.post(**kwargs)
                self.assertEqual(h.send_error.call_args.args[0], code)
            heating.request.assert_not_called()

    def test_heating_post_returns_accepted_not_assumed_physical_state(self):
        with patch.object(server, 'HEATING_KEY', 'test-key'), patch.object(server, 'HEATING') as heating:
            heating.snapshot.return_value = {'armed':False, 'pending':True, 'phase':'disabled'}
            h = self.post()
            h.send_response.assert_called_once_with(202)
            heating.request.assert_called_once_with(True)
            self.assertFalse(json.loads(h.wfile.getvalue())['heating']['armed'])
            heating.request.side_effect = ValueError('Installation not confirmed')
            self.assertEqual(self.post().send_error.call_args.args[0], 400)

    def test_key_is_private_persistent_and_not_in_status(self):
        key = server.heating_key()
        self.assertGreaterEqual(len(key), 32)
        self.assertEqual((server.DB.parent/'heating-control.key').stat().st_mode & 0o777, 0o600)
        self.assertEqual(server.heating_key(), key)
        self.assertNotIn(key, json.dumps(server.snapshot()))

    def test_bad_key_does_not_prevent_shutdown_recovery(self):
        with patch.object(server, 'HEATING'), patch.object(server, 'HEATING_KEY'), \
                patch.object(server, 'heating_key', side_effect=ValueError), \
                patch.object(server, 'Heating') as heating, self.assertLogs(level='ERROR'):
            server.setup_heating()
            heating.return_value.start.assert_called_once()
            self.assertIsNone(server.HEATING_KEY)
