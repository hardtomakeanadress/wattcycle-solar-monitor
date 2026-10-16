"""Exercise serial framing and cleanup without talking to the controller."""
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('reader', Path(__file__).resolve().parent.parent / 'read_controller.py')
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


class ReaderTest(unittest.TestCase):
    def read(self, chunks, count=1):
        with patch.object(reader.os, 'write', return_value=8) as write, \
             patch.object(reader.os, 'read', side_effect=chunks), \
             patch.object(reader.termios, 'tcdrain'), \
             patch.object(reader.time, 'sleep'), \
             patch.object(reader.select, 'select', return_value=([7], [], [])):
            result = reader.read_registers(7, 0x0100, count)
            request = bytes.fromhex('01030100') + struct.pack('>H', count)
            write.assert_called_once_with(7, request + reader.crc(request))
            return result

    def test_fragmented_valid_frame_and_transient_empty_buffer(self):
        self.assertEqual(self.read([BlockingIOError(), bytes.fromhex('010302'), bytes.fromhex('0018b84e')]), [24])

    def test_bad_checksum_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'invalid CRC'):
            self.read([bytes.fromhex('0103020018b84f')])

    def test_modbus_exception_rejected(self):
        frame = bytes.fromhex('018301')
        with self.assertRaisesRegex(RuntimeError, 'Modbus exception'):
            self.read([frame + reader.crc(frame)])

    def test_wrong_slave_rejected(self):
        frame = bytes.fromhex('0203020018')
        with self.assertRaisesRegex(RuntimeError, 'Unexpected reply'):
            self.read([frame + reader.crc(frame)])

    def test_disconnect_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'disconnected'):
            self.read([b''])

    def test_no_reply_times_out(self):
        with patch.object(reader.os, 'write', return_value=8), \
             patch.object(reader.termios, 'tcdrain'), \
             patch.object(reader.time, 'sleep'), \
             patch.object(reader.time, 'monotonic', side_effect=[0, 0.1, 0.2, 1.1]), \
             patch.object(reader.select, 'select', return_value=([], [], [])):
            with self.assertRaisesRegex(RuntimeError, 'No reply'):
                reader.read_registers(7, 0x0100, 1)

    def test_serial_settings_and_exclusive_mode_released_after_error(self):
        attrs = [0, 0, 0, 0, 0, 0, [0] * 32]
        with patch.object(reader.os, 'open', return_value=7), \
             patch.object(reader.os, 'close') as close, \
             patch.object(reader.fcntl, 'flock'), \
             patch.object(reader.fcntl, 'ioctl') as ioctl, \
             patch.object(reader.termios, 'tcgetattr', side_effect=[attrs, [*attrs[:6], attrs[6][:]]]), \
             patch.object(reader.termios, 'tcsetattr') as settings, \
             patch.object(reader.select, 'select', return_value=([], [], [])), \
             patch.object(reader, 'read_registers', side_effect=RuntimeError('test failure')):
            with self.assertRaisesRegex(RuntimeError, 'test failure'):
                reader.main()
            settings.assert_called_with(7, reader.termios.TCSANOW, attrs)
            configured = settings.call_args_list[0].args
            self.assertEqual(configured[:2], (7, reader.termios.TCSANOW))
            self.assertEqual(configured[2][:6], [0, 0,
                reader.termios.CS8 | reader.termios.CREAD | reader.termios.CLOCAL,
                0, reader.termios.B9600, reader.termios.B9600])
            self.assertEqual(configured[2][6][reader.termios.VMIN], 0)
            self.assertEqual(configured[2][6][reader.termios.VTIME], 0)
            self.assertEqual(settings.call_count, 2)
            ioctl.assert_called_with(7, reader.termios.TIOCNXCL)
            close.assert_called_once_with(7)

    def test_multiple_registers_and_wrong_byte_count(self):
        frame = bytes.fromhex('01030400180111')
        self.assertEqual(self.read([frame[:4], frame[4:] + reader.crc(frame)], count=2), [24, 273])
        bad = bytes.fromhex('01030200180111')
        with self.assertRaisesRegex(RuntimeError, 'Unexpected reply'):
            self.read([bad + reader.crc(bad)], count=2)

    def test_short_write_stops_before_reading(self):
        with patch.object(reader.os, 'write', return_value=7), patch.object(reader.os, 'read') as read, \
                patch.object(reader.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'Incomplete serial request'):
                reader.read_registers(7, 0x100, 1)
        read.assert_not_called()

    def run_main(self, replies, traffic=False):
        attrs = [0, 0, 0, 0, 0, 0, [0] * 32]
        with patch.object(reader.os, 'open', return_value=7), patch.object(reader.os, 'close'), \
                patch.object(reader.fcntl, 'flock'), patch.object(reader.fcntl, 'ioctl'), \
                patch.object(reader.termios, 'tcgetattr', side_effect=lambda _: [*attrs[:6], attrs[6][:]]), \
                patch.object(reader.termios, 'tcsetattr'), \
                patch.object(reader.select, 'select', return_value=([7] if traffic else [], [], [])), \
                patch.object(reader, 'read_registers', side_effect=replies) as read, \
                patch.object(sys, 'stdout', new_callable=io.StringIO) as output:
            try:
                reader.main()
            finally:
                self.requests = read.call_args_list
        return json.loads(output.getvalue())

    def test_model_guard_and_existing_serial_traffic(self):
        model = list(struct.unpack('>8H', b'OTHER-MODEL     '))
        with self.assertRaisesRegex(RuntimeError, 'Unexpected model'):
            self.run_main([model])
        self.assertEqual(len(self.requests), 1)
        with self.assertRaisesRegex(RuntimeError, 'Serial traffic already present'):
            self.run_main([], traffic=True)
        self.assertEqual(self.requests, [])

    def test_decode_known_registers_and_json_contract(self):
        model = list(struct.unpack('>8H', b'M-2430N         '))
        registers = [24, 67, 273, 187, 51, 0x8519, 270, 123, 33, 493,
                     231, 336, 12, 0x0105, 0x1234, 120, 0xabcd, 0x4321]
        result = self.run_main([model, [123, 456], registers, [1, 24, 3]])
        expected = dict(port=reader.PORT, baud=9600, format='8N1', slave=reader.SLAVE,
            all_crc_valid=True, model='M-2430N', version_registers_raw=[123, 456],
            system_V=24, controller_estimated_SOC_percent=67, battery_V=27.3,
            charge_A=1.87, charge_W=51, controller_C=-5, battery_temperature_field_C=25,
            load_V=27, load_A=1.23, load_W=33, PV_V=49.3, today_peak_W=231,
            today_generation_Wh=336, today_load_Wh=12, load_on=True, charge_status='float',
            fault_bits=0x1234, running_days=120, cumulative_generation_words_raw=[0xabcd, 0x4321],
            settings_0201_0203_raw=[1, 24, 3], telemetry_0100_0111_raw=registers)
        timestamp = result.pop('timestamp_utc')
        self.assertIsNotNone(reader.datetime.datetime.fromisoformat(timestamp).tzinfo)
        self.assertEqual(result, expected)
        self.assertEqual([c.args for c in self.requests], [(7, 0xc, 8), (7, 0x14, 2), (7, 0x100, 18), (7, 0x201, 3)])
        registers[5], registers[13] = 0x0585, 0xff
        result = self.run_main([model, [123, 456], registers, [1, 24, 3]])
        self.assertEqual((result['controller_C'], result['battery_temperature_field_C']), (5, -5))
        self.assertFalse(result['load_on'])
        self.assertEqual(result['charge_status'], 'unknown')

    def test_temperature_sign_magnitude_boundaries(self):
        for raw, expected in [(0, 0), (127, 127), (128, 0), (129, -1), (255, -127)]:
            self.assertEqual(reader.temperature(raw), expected)

    def test_invalid_slave_rejected_before_device_open(self):
        for value in ('0', '248', '-1', 'abc'):
            result = subprocess.run([sys.executable, reader.__file__], capture_output=True, text=True,
                                    env=dict(os.environ, SOLAR_SLAVE=value), timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('ValueError', result.stderr)
            self.assertNotIn('os.open', result.stderr)


if __name__ == '__main__':
    unittest.main()
