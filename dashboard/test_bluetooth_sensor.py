"""Protocol fixtures captured from the sensor; tests require no hardware."""
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import bluetooth_sensor as ble


class BluetoothSensorTest(unittest.TestCase):
    def test_discovery_sets_handles_from_tool_output_and_reuses_them(self):
        sensor = ble.XiaomiSensor('aa:bb:cc:dd:ee:ff')
        characteristics = (
            f'handle = 0x0032, char properties = 0x12, char value handle = 0x0033, uuid = {ble.MEASUREMENT_UUID}\n'
            'handle = 0x0036, char properties = 0x02, char value handle = 0x0037, uuid = ebe0ccc4-7a0a-4b0c-8a1a-6ff2997da3a6\n')
        descriptors = f'handle = 0x0035, uuid = {ble.CCCD_UUID}\n'
        results = [subprocess.CompletedProcess([], 0, text) for text in (characteristics, descriptors)]
        with patch.object(ble.subprocess, 'run', side_effect=results) as run:
            sensor.discover()
        self.assertEqual(sensor.handles, (0x33, 0x35))
        self.assertEqual(run.call_args_list[0].args[0], ['gatttool', '-b', sensor.address, '--characteristics'])
        self.assertEqual(run.call_args_list[1].args[0],
            ['gatttool', '-b', sensor.address, '--char-desc', '--start=0x0034', '--end=0x0035'])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['timeout'], 12)
            self.assertTrue(call.kwargs['check'])
        with patch.object(sensor, 'discover') as discover, \
                patch.object(ble.subprocess, 'Popen', side_effect=OSError('test stop')):
            with self.assertRaises(OSError):
                sensor.read()
        discover.assert_not_called()

    def test_last_characteristic_and_ambiguous_descriptors(self):
        output = f'handle = 0x0032, char properties = 0x12, char value handle = 0x0033, uuid = {ble.MEASUREMENT_UUID}'
        self.assertEqual(ble.measurement_range(output), (0x33, 0xffff))
        output = '\n'.join(f'handle = 0x{h:04x}, uuid = {ble.CCCD_UUID}' for h in (0x34, 0x35))
        with self.assertRaisesRegex(ValueError, 'unambiguously'):
            ble.notification_descriptor(output, 0x33, 0xffff)

    def test_decode(self):
        self.assertEqual(ble.decode(bytes.fromhex('cc 06 2d 40 0c')),
                         dict(temperature_C=17.4, humidity_percent=45,
                              battery_V=3.136))
        self.assertEqual(ble.decode(bytes.fromhex('0c fe 00 34 08'))['temperature_C'], -5)
        self.assertEqual(ble.decode(bytes.fromhex('00 00 64 d0 07'))['battery_V'], 2.0)
        for packet in ('00 00', '00 00 65 40 0c', '00 00 2d 00 00'):
            with self.assertRaises(ValueError):
                ble.decode(bytes.fromhex(packet))

    def test_actual_handles_and_unverified_descriptors(self):
        for value, end in ((0x33, 0x35), (0x36, 0x38)):
            output = (
                f'handle = 0x{value-1:04x}, char properties = 0x12, char value handle = 0x{value:04x}, uuid = {ble.MEASUREMENT_UUID}\n'
                f'handle = 0x{end+1:04x}, char properties = 0x02, char value handle = 0x{end+2:04x}, uuid = ebe0ccc4-7a0a-4b0c-8a1a-6ff2997da3a6\n')
            self.assertEqual(ble.measurement_range(output), (value, end))
            descriptor = f'handle = 0x{end:04x}, uuid = {ble.CCCD_UUID}\n'
            self.assertEqual(ble.notification_descriptor(descriptor, value, end), end)
        with self.assertRaises(ValueError):
            ble.measurement_range(output.replace('0x12', '0x02'))
        with self.assertRaises(ValueError):
            ble.notification_descriptor(f'handle = 0x0040, uuid = {ble.CCCD_UUID}', 0x33, 0x35)
        with self.assertRaises(ValueError):
            ble.notification_descriptor('', 0x33, 0x35)

    def test_first_measurement_closes_connection(self):
        sensor = ble.XiaomiSensor('AA:BB:CC:DD:EE:FF')
        sensor.handles = 0x33, 0x35
        real_popen, children = subprocess.Popen, []
        def fake_tool(command, **kwargs):
            self.assertIn('--handle=0x0035', command)
            child = real_popen([sys.executable, '-u', '-c',
                "import time; print('Notification handle = 0x0001 value: 00 00'); "
                "print('Notification handle = 0x0033 value: cc 06 2d 40 0c '); time.sleep(10)"], **kwargs)
            children.append(child)
            return child
        with patch.object(ble.subprocess, 'Popen', side_effect=fake_tool):
            reading = sensor.read()
        self.assertEqual(reading['humidity_percent'], 45)
        self.assertLess(abs(reading['timestamp_seconds'] - time.time()), 3)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)

    def test_failure_preserves_last_reading_and_recovers(self):
        sensor = ble.XiaomiSensor('AA:BB:CC:DD:EE:FF')
        reading = dict(ble.decode(bytes.fromhex('cc 06 2d 40 0c')), timestamp_seconds=100)
        with patch.object(sensor, 'read', return_value=reading):
            sensor.poll_once()
        with patch.object(ble.time, 'time', return_value=220):
            self.assertFalse(sensor.snapshot()['stale'])
        with patch.object(sensor, 'read', side_effect=TimeoutError), self.assertLogs(level='ERROR'):
            sensor.poll_once()
        self.assertEqual(sensor.snapshot()['reading'], reading)
        self.assertIsNotNone(sensor.snapshot()['error'])
        self.assertIsNone(sensor.handles)
        with patch.object(ble.time, 'time', return_value=1901):
            self.assertTrue(sensor.snapshot()['stale'])
        with patch.object(sensor, 'read', return_value=reading):
            sensor.poll_once()
        self.assertIsNone(sensor.snapshot()['error'])

    def test_disabled_sensor_never_starts(self):
        sensor = ble.XiaomiSensor()
        with patch.object(ble.threading, 'Thread') as thread:
            sensor.start()
        thread.assert_not_called()
        self.assertFalse(sensor.snapshot()['enabled'])

    def test_discovery_failure_prevents_notification_write(self):
        sensor = ble.XiaomiSensor('AA:BB:CC:DD:EE:FF')
        with patch.object(ble.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '')), \
             patch.object(ble.subprocess, 'Popen') as popen:
            with self.assertRaises(ValueError):
                sensor.read()
        popen.assert_not_called()
