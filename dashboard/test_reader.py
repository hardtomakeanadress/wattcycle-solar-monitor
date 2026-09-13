"""Exercise serial framing and cleanup without talking to the controller."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('reader', Path(__file__).resolve().parent.parent / 'read_controller.py')
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


class ReaderTest(unittest.TestCase):
    def read(self, chunks):
        with patch.object(reader.os, 'write', return_value=8) as write, \
             patch.object(reader.os, 'read', side_effect=chunks), \
             patch.object(reader.termios, 'tcdrain'), \
             patch.object(reader.time, 'sleep'), \
             patch.object(reader.select, 'select', return_value=([7], [], [])):
            result = reader.read_registers(7, 0x0100, 1)
            write.assert_called_once_with(7, bytes.fromhex('01030100000185f6'))
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
            ioctl.assert_called_with(7, reader.termios.TIOCNXCL)
            close.assert_called_once_with(7)


if __name__ == '__main__':
    unittest.main()
