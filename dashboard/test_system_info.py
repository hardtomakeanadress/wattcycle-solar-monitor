import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import server
import system_info as system


class SystemInfoTest(unittest.TestCase):
    def setUp(self):
        self.files = {
            '/proc/device-tree/model': 'Raspberry Pi 2 Model B Rev 1.1',
            '/proc/cpuinfo': 'processor : 0\nmodel name : ARMv7 Processor\nSerial : private-serial\n',
            '/etc/os-release': 'NAME=Linux\nPRETTY_NAME="Raspbian GNU/Linux 13 (trixie)"\n',
            '/proc/meminfo': 'MemTotal: 1000000 kB\nMemFree: 200000 kB\nMemAvailable: 700000 kB\n',
            '/proc/uptime': '172861.1 680000.0',
            '/sys/class/thermal/thermal_zone0/temp': '34700',
        }
        for target, value in [
            ('read_text', lambda name: self.files.get(name, '')),
            ('throttle_flags', lambda: ([], [])),
        ]:
            p = patch.object(system, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.disk = patch.object(system.os, 'statvfs', return_value=SimpleNamespace(
            f_blocks=1000, f_frsize=4096, f_bavail=600))
        self.disk.start()
        self.addCleanup(self.disk.stop)
        self.info = system.SystemInfo(Path('/history'))

    def test_units_and_allowlisted_information(self):
        data = self.info.snapshot()
        self.assertEqual(data['memory_total_bytes'], 1024000000)
        self.assertEqual(data['memory_available_bytes'], 716800000)
        self.assertEqual(data['storage_free_bytes'], 2457600)
        self.assertEqual(data['temperature_C'], 34.7)
        self.assertEqual(data['uptime_seconds'], 172861.1)
        self.assertEqual(data['cpu_model'], 'ARMv7 Processor')
        self.assertEqual(data['os'], 'Raspbian GNU/Linux 13 (trixie)')
        self.assertNotIn('private-serial', json.dumps(data))
        self.assertEqual(set(data), {
            'timestamp_utc', 'age_seconds', 'refresh_interval_seconds', 'model',
            'cpu_model', 'architecture', 'cpu_cores', 'os', 'kernel', 'python_version',
            'memory_total_bytes', 'memory_available_bytes', 'storage_total_bytes',
            'storage_free_bytes', 'uptime_seconds', 'temperature_C',
            'throttle_current', 'throttle_since_boot'})

    def test_missing_sources_are_unknown_and_independent(self):
        self.files.clear()
        with patch.object(system.os, 'statvfs', side_effect=PermissionError), \
                patch.object(system, 'throttle_flags', return_value=(None, None)):
            data = self.info.snapshot()
        for key in ('model', 'cpu_model', 'os', 'memory_total_bytes',
                    'memory_available_bytes', 'storage_total_bytes', 'storage_free_bytes',
                    'uptime_seconds', 'temperature_C', 'throttle_current', 'throttle_since_boot'):
            self.assertIsNone(data[key], key)
        self.assertTrue(data['python_version'])
        self.assertTrue(data['timestamp_utc'])

    def test_bad_values_do_not_invent_measurements(self):
        self.files['/proc/meminfo'] = 'MemTotal: 1 kB\nMemAvailable: 2 kB\n'
        self.files['/proc/uptime'] = 'nan 100'
        self.files['/sys/class/thermal/thermal_zone0/temp'] = '-1'
        self.files['/etc/os-release'] = 'PRETTY_NAME="unclosed'
        data = self.info.snapshot()
        for key in ('memory_available_bytes', 'uptime_seconds', 'temperature_C', 'os'):
            self.assertIsNone(data[key])
        self.files['/proc/meminfo'] = 'MemTotal: 100 kB\nMemFree: 5 kB\n'
        self.assertIsNone(self.info.collect()['memory_available_bytes'])

    def test_zero_and_device_tree_fallback(self):
        self.files.pop('/proc/device-tree/model')
        self.files['/sys/firmware/devicetree/base/model'] = 'Pi fallback'
        self.files['/proc/meminfo'] = 'MemTotal: 100 kB\nMemAvailable: 0 kB\n'
        self.files['/sys/class/thermal/thermal_zone0/temp'] = '0'
        self.files['/proc/uptime'] = '0 0'
        data = self.info.snapshot()
        self.assertEqual(data['model'], 'Pi fallback')
        self.assertEqual(data['memory_available_bytes'], 0)
        self.assertEqual(data['temperature_C'], 0)
        self.assertEqual(data['uptime_seconds'], 0)

    def test_cache_reuses_snapshot_and_expires(self):
        with patch.object(system.time, 'monotonic', return_value=10) as clock, \
                patch.object(self.info, 'collect', wraps=self.info.collect) as collect:
            first = self.info.snapshot()
            clock.return_value = 39
            cached = self.info.snapshot()
            self.assertEqual(cached['timestamp_utc'], first['timestamp_utc'])
            self.assertEqual(cached['age_seconds'], 29)
            self.assertEqual(collect.call_count, 1)
            clock.return_value = 40
            self.info.snapshot()
            self.assertEqual(collect.call_count, 2)

    def test_system_endpoint_does_not_poll_controller_or_query_database(self):
        handler = server.Handler.__new__(server.Handler)
        handler.path = '/api/system'
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        with patch.object(server, 'SYSTEM', self.info), \
                patch.object(server, 'poll_once', side_effect=AssertionError('serial read')), \
                patch.object(server, 'database', side_effect=AssertionError('database query')):
            handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(json.loads(handler.wfile.getvalue())['temperature_C'], 34.7)


class ThrottleTest(unittest.TestCase):
    def test_distinguishes_current_and_historical_flags(self):
        for raw, expected in [('0', ([], [])), ('10001', (['Under-voltage'], ['Under-voltage'])),
                              ('80004', (['Throttled'], ['Temperature limit']))]:
            with self.subTest(raw=raw), patch.object(system.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, 'throttled=0x'+raw, '')) as run:
                self.assertEqual(system.throttle_flags(), expected)
                self.assertEqual(run.call_args.kwargs['timeout'], 1)
                self.assertNotIn('shell', run.call_args.kwargs)

    def test_failures_are_unknown_not_healthy(self):
        for error in (FileNotFoundError(), PermissionError(), subprocess.TimeoutExpired('vcgencmd', 1),
                      subprocess.CalledProcessError(1, 'vcgencmd')):
            with self.subTest(error=error), patch.object(system.subprocess, 'run', side_effect=error):
                self.assertEqual(system.throttle_flags(), (None, None))
        with patch.object(system.subprocess, 'run', return_value=SimpleNamespace(stdout='bad output')):
            self.assertEqual(system.throttle_flags(), (None, None))


if __name__ == '__main__':
    unittest.main()
