"""Protocol and freshness tests; no real radio or battery operations."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import victron_shunt as shunt

# Synthetic known-answer frame, encrypted independently with OpenSSL AES-128-CTR.
FRAME = bytes.fromhex('100239c00222010013608a6e80f07a3098645a7453afad')
KEY = bytes(16)


class ShuntTest(unittest.TestCase):
    def test_kernel_aes_known_answer_and_real_record_layout(self):
        self.assertEqual(shunt.aes_block(KEY, KEY).hex(), '66e94bd4ef8a2c3b884cfa59ca342b2e')
        r = shunt.decode(FRAME, KEY)
        self.assertEqual((r['battery_V'], r['current_A'], r['soc_percent']), (26.27, -17.027, 93.9))
        self.assertEqual((r['consumed_Ah'], r['time_to_go_minutes']), (-19.0, 456))
        self.assertAlmostEqual(r['net_power_W'], -447.3)
        self.assertEqual(r['alarms'], [])
        self.assertIsNone(r['temperature_C'])

    def test_bad_key_type_and_truncated_records_rejected(self):
        for frame, key in [(FRAME[:-1], KEY), (FRAME, bytes([1])*16),
                           (FRAME[:4]+b'\x01'+FRAME[5:], KEY), (FRAME, b'bad')]:
            with self.subTest(frame=frame, key=key), self.assertRaises(ValueError):
                shunt.decode(frame, key)

    def test_unavailable_sentinels_are_not_measurements(self):
        values = [(65535,16), (32767,16), (4,16), (65535,16), (3,2),
                  (0x3fffff,22), (0xfffff,20), (1023,10)]
        bits, offset = 0, 0
        for value, width in values:
            bits |= value << offset
            offset += width
        with patch.object(shunt, 'aes_block', return_value=bytes(16)):
            r = shunt.decode(FRAME[:8] + bits.to_bytes(15, 'little'), KEY)
        for name in ['battery_V','current_A','soc_percent','consumed_Ah','net_power_W','time_to_go_minutes']:
            self.assertIsNone(r[name], name)
        self.assertEqual(r['alarms'], ['Low state of charge'])

    def test_busctl_parser_validates_type_and_length(self):
        text = 'a{qv} 1 737 ay 23 ' + ' '.join(str(v) for v in FRAME)
        self.assertEqual(shunt.manufacturer_data(text), FRAME)
        for invalid in ['a{qv} 0', text.replace('737','738'), text+' 0', text.replace('ay 23','ay 255')]:
            with self.assertRaises(ValueError):
                shunt.manufacturer_data(invalid)

    def test_cached_and_duplicate_frames_do_not_refresh_age_or_expose_key(self):
        sensor = shunt.VictronShunt('AA:BB:CC:DD:EE:FF', '/private/victron.key')
        with patch.object(shunt.time, 'monotonic', return_value=100) as clock, \
                patch.object(shunt, 'decode', return_value={'soc_percent': 93.9}):
            self.assertFalse(sensor.accept(FRAME, KEY))
            self.assertIsNone(sensor.snapshot()['reading'])
            next_frame = FRAME[:5]+b'\x23'+FRAME[6:]
            self.assertTrue(sensor.accept(next_frame, KEY))
            clock.return_value = 131
            self.assertFalse(sensor.accept(next_frame, KEY))
            self.assertTrue(sensor.snapshot()['stale'])
            self.assertEqual(sensor.snapshot()['age_seconds'], 31)
            self.assertEqual(sensor.snapshot()['reading']['soc_percent'], 93.9)
            self.assertNotIn('key', json.dumps(sensor.snapshot()))

    def test_missing_configuration_keeps_monitoring_available(self):
        sensor = shunt.VictronShunt('AA:BB:CC:DD:EE:FF', '/missing/victron.key')
        with patch.object(shunt.subprocess, 'Popen') as process:
            sensor.poll_loop(threading.Event())
            process.assert_not_called()
        self.assertIn('configuration unavailable', sensor.snapshot()['error'])
        self.assertIsNone(shunt.VictronShunt().start(threading.Event()))

    def test_scan_process_is_released_on_service_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            key_path = Path(folder)/'victron.key'
            key_path.write_text(KEY.hex())
            sensor = shunt.VictronShunt('AA:BB:CC:DD:EE:FF', key_path)
            stop = threading.Event()
            def capture(*args, **kwargs):
                stop.set()
                return Mock(stdout='a{qv} 1 737 ay 23 '+' '.join(str(v) for v in FRAME))
            with patch.object(shunt.subprocess, 'Popen') as process, \
                    patch.object(shunt.subprocess, 'run', side_effect=capture):
                process.return_value.poll.return_value = None
                sensor.poll_loop(stop)
                process.return_value.terminate.assert_called_once()
                process.return_value.wait.assert_called_once()
