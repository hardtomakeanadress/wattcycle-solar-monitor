"""Manual commands, socket access, monitoring contention and failure behavior."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import load_control
import server


class LoadControlTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'private' / 'control.sock'
        for name, value in [('DB', Path(self.directory.name) / 'history.sqlite3'),
                            ('STOP', threading.Event()), ('SERIAL_LOCK', threading.Lock()),
                            ('STATE', dict(reading=None, error=None, last_attempt=None))]:
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        server.initialize()

    def start(self, callback):
        control = load_control.ControlServer(callback, self.path)
        self.addCleanup(control.close)
        return control

    def test_real_socket_permissions_invalid_input_and_single_owner(self):
        callback = Mock(return_value={'load_on': False})
        self.start(callback)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(BlockingIOError):
            load_control.ControlServer(callback, self.path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(self.path))
            client.sendall(b'toggle\n')
            result = json.loads(client.recv(4096))
        self.assertFalse(result['ok'])
        callback.assert_not_called()
        self.assertTrue(load_control.request('off', self.path)['ok'])
        callback.assert_called_once_with(False)

    def test_stale_socket_recovery_and_insecure_directory_rejected(self):
        self.path.parent.mkdir(mode=0o700)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(str(self.path))
        control = load_control.ControlServer(lambda _: {}, self.path)
        control.close()
        self.assertFalse(self.path.exists())
        self.path.parent.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, '0700'):
            load_control.ControlServer(lambda _: {}, self.path)

    def test_command_publishes_and_persists_readback(self):
        self.start(server.control_load)
        reading = dict(load_on=False, load_V=0, load_A=0, load_W=0, timestamp_utc='2026-10-16T20:00:00+00:00')
        with patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(reading))) as run:
            result = load_control.request('off', self.path)
        self.assertTrue(result['ok'])
        self.assertEqual(run.call_args.args[0][-2:], ['--load', 'off'])
        self.assertEqual(server.snapshot()['reading'], reading)
        with server.database() as db:
            self.assertEqual(json.loads(db.execute('SELECT data FROM samples').fetchone()[0]), reading)

    def test_command_waits_for_poll_without_overlapping_or_restarting(self):
        polling, release = threading.Event(), threading.Event()
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if '--load' not in args:
                polling.set()
                if not release.wait(3):
                    raise TimeoutError('test did not release poll')
            return subprocess.CompletedProcess(args, 0, json.dumps({'load_on': '--load' in args}))
        with patch.object(server.subprocess, 'run', side_effect=run):
            with ThreadPoolExecutor(max_workers=2) as pool:
                poll = pool.submit(server.poll_once)
                self.assertTrue(polling.wait(2))
                command = pool.submit(server.control_load, True)
                self.assertEqual(len(calls), 1)
                # Dashboard snapshots remain available while the serial reader is busy.
                self.assertIsNone(server.snapshot()['reading'])
                release.set()
                poll.result(timeout=3)
                self.assertTrue(command.result(timeout=3)['load_on'])
        self.assertEqual(len(calls), 2)
        self.assertTrue(server.snapshot()['reading']['load_on'])
        with server.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM samples').fetchone()[0], 2)

    def test_failure_is_not_retried_or_published_as_success(self):
        self.start(server.control_load)
        for failure in [subprocess.TimeoutExpired('reader', 15),
                        subprocess.CalledProcessError(1, 'reader', stderr='RuntimeError: echo mismatch')]:
            with patch.object(server.subprocess, 'run', side_effect=failure) as run, self.assertLogs(level='ERROR'):
                result = load_control.request('on', self.path)
            self.assertFalse(result['ok'])
            self.assertIn('unconfirmed', result['error'])
            run.assert_called_once()
        self.assertIsNone(server.snapshot()['reading'])
        self.assertIn('unconfirmed', server.snapshot()['error'])
        with server.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM samples').fetchone()[0], 0)

    def test_shutdown_rejects_command_without_accessing_hardware(self):
        server.STOP.set()
        with patch.object(server.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'shutting down'):
                server.control_load(False)
        run.assert_not_called()

    def test_overlapping_commands_are_rejected_without_a_delayed_switch(self):
        self.start(server.control_load)
        entered, release = threading.Event(), threading.Event()
        def read(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test did not release command')
            return subprocess.CompletedProcess([], 0, '{"load_on":true}')
        with patch.object(server.subprocess, 'run', side_effect=read) as run:
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(load_control.request, 'on', self.path)
                self.assertTrue(entered.wait(2))
                second = load_control.request('off', self.path)
                self.assertFalse(second['ok'])
                self.assertIn('no command sent', second['error'])
                release.set()
                self.assertTrue(first.result(timeout=3)['ok'])
        run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
