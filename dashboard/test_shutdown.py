"""Shutdown security contracts; all OS power operations are mocked."""
import io
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import Mock, patch

import server


class ShutdownTest(unittest.TestCase):
    def setUp(self):
        for name, value in [('HEATING_KEY', 'test-key'), ('SHUTDOWN_PENDING', False),
                            ('HEATING', None)]:
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.command = patch.object(server.subprocess, 'run').start()
        self.addCleanup(patch.stopall)

    def post(self, body=b'{"shutdown":true}', origin='http://cabana:8080', **headers):
        h = object.__new__(server.Handler)
        h.connection = Mock()
        h.connection.getsockname.return_value = ('127.0.0.1', 8080)
        h.path, h.rfile, h.wfile = '/api/shutdown', io.BytesIO(body), io.BytesIO()
        h.headers = {'Host': 'cabana:8080', 'Origin': origin,
                     'Content-Type': 'application/json', 'Content-Length': str(len(body)), **headers}
        h.send_response, h.send_error = Mock(), Mock()
        h.send_header, h.end_headers = Mock(), Mock()
        with patch.object(server.socket, 'gethostname', return_value='cabana'):
            h.do_POST()
        return h

    def test_origin_and_strict_payload_precede_power_operation(self):
        for kwargs, code in [({'origin': None}, 403), ({'origin': 'http://evil.invalid'}, 403),
                             ({'body': b'{"shutdown":1}'}, 400), ({'body': b'null'}, 400),
                             ({'body': b'{"shutdown":true,"extra":1}'}, 400),
                             ({'Content-Type': 'text/plain'}, 400),
                             ({'Transfer-Encoding': 'chunked'}, 400),
                             ({'Content-Length': '99999'}, 400)]:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.post(**kwargs).send_error.call_args.args[0], code)
        self.command.assert_not_called()

    def test_shutdown_requires_no_control_key(self):
        with patch.object(server, 'HEATING_KEY', None):
            self.post().send_response.assert_called_once_with(202)
        self.command.assert_called_once()

    def test_schedule_is_fixed_noninteractive_delayed_and_not_repeated(self):
        with patch.object(server.time, 'time', return_value=1000):
            h = self.post()
        h.send_response.assert_called_once_with(202)
        self.assertEqual(json.loads(h.wfile.getvalue()), {'shutdown': 'scheduled', 'delay_seconds': 10})
        args = self.command.call_args.args[0]
        self.assertEqual(args[-4:], ['ScheduleShutdown', 'st', 'poweroff', '1010000000'])
        self.assertIn('--allow-interactive-authorization=no', args)
        self.assertEqual(self.post().send_error.call_args.args[0], 409)
        self.command.assert_called_once()

    def test_active_pending_or_owned_heating_blocks_shutdown(self):
        for flag in ('armed', 'pending', 'owns_load'):
            with patch.object(server, 'heating_status', return_value={flag: True}):
                self.assertEqual(self.post().send_error.call_args.args[0], 409)
        self.command.assert_not_called()

    def test_permission_failure_is_reported_and_not_claimed_as_success(self):
        self.command.side_effect = subprocess.CalledProcessError(1, ['busctl'])
        with self.assertLogs(level='ERROR'):
            self.assertEqual(self.post().send_error.call_args.args[0], 503)
        self.assertFalse(server.SHUTDOWN_PENDING)

    def test_timeout_is_uncertain_and_blocks_retries(self):
        self.command.side_effect = subprocess.TimeoutExpired(['busctl'], 7)
        with self.assertLogs(level='ERROR'):
            self.assertEqual(self.post().send_error.call_args.args[0], 503)
        self.assertTrue(server.SHUTDOWN_PENDING)
        self.assertEqual(self.post().send_error.call_args.args[0], 409)
        self.command.assert_called_once()

    def test_heating_cannot_arm_after_shutdown_is_scheduled(self):
        self.post()
        with patch.object(server, 'HEATING') as heating:
            h = object.__new__(server.Handler)
            h.connection = Mock()
            h.connection.getsockname.return_value = ('127.0.0.1', 8080)
            h.path, h.rfile, h.wfile = '/api/heating', io.BytesIO(b'{"armed":true}'), io.BytesIO()
            h.headers = {'Host': 'cabana:8080', 'Authorization': 'Bearer test-key',
                         'Content-Type': 'application/json', 'Content-Length': '14'}
            h.send_error = Mock()
            with patch.object(server.socket, 'gethostname', return_value='cabana'):
                h.do_POST()
            self.assertEqual(h.send_error.call_args.args[0], 400)
            heating.request.assert_not_called()

    def test_spawn_failure_can_be_retried(self):
        self.command.side_effect = FileNotFoundError('busctl missing')
        with self.assertLogs(level='ERROR'):
            self.assertEqual(self.post().send_error.call_args.args[0], 503)
        self.assertFalse(server.SHUTDOWN_PENDING)
        self.command.side_effect = None
        self.post().send_response.assert_called_once_with(202)

    def test_rebinding_host_cannot_schedule_shutdown(self):
        h = self.post(origin='http://attacker.invalid:8080', Host='attacker.invalid:8080')
        self.assertEqual(h.send_error.call_args.args[0], 403)
        self.command.assert_not_called()

    def test_direct_ip_access_remains_available(self):
        self.post(origin='http://127.0.0.1:8080', Host='127.0.0.1:8080').send_response.assert_called_once_with(202)

    def test_shutdown_blocks_new_manual_on_but_allows_off(self):
        self.post()
        with patch.object(server, 'execute_load', return_value={'load_on': False}) as load:
            with self.assertRaisesRegex(RuntimeError, 'shutdown'):
                server.control_load(True)
            self.assertFalse(server.control_load(False)['load_on'])
            load.assert_called_once_with(False)

    def test_schedule_waits_for_inflight_manual_command(self):
        entered, release = threading.Event(), threading.Event()
        def command(enabled):
            entered.set()
            self.assertTrue(release.wait(2))
            return {'load_on': enabled}
        with patch.object(server, 'execute_load', side_effect=command), ThreadPoolExecutor(2) as pool:
            manual = pool.submit(server.control_load, True)
            self.assertTrue(entered.wait(2))
            shutdown = pool.submit(self.post)
            self.command.assert_not_called()
            release.set()
            self.assertTrue(manual.result(2)['load_on'])
            shutdown.result(2).send_response.assert_called_once_with(202)
