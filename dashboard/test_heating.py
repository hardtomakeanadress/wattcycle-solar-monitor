"""Thermostat safety transitions using simulated hardware, never real LOAD writes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import heating


class HeatingTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'heating-state.json'
        self.clock = patch.object(heating.time, 'monotonic', return_value=1000).start()
        self.addCleanup(patch.stopall)
        self.sensor = Mock()
        self.sample = dict(enabled=True, error=None, reading={'temperature_C': 4}, received_monotonic=None)
        self.sensor.snapshot.side_effect = lambda: dict(self.sample)
        self.command = Mock(side_effect=lambda enabled: {'load_on': enabled, 'fault_bits': 0})
        self.health = Mock(return_value=None)
        self.h = heating.Heating(self.sensor, self.command, self.health, self.path, ready=True)

    def arm(self):
        self.h.request(True)
        self.h.step()
        self.assertEqual(self.h.phase, 'arming')
        self.assertEqual(self.command.call_args.args, (False,))
        self.sensor.set_interval.assert_called_with(60, refresh=True)

    def sample_at(self, temperature, now):
        self.clock.return_value = now
        self.sample.update(reading={'temperature_C': temperature}, received_monotonic=now)
        self.h.step()

    def test_uninstalled_is_locked_and_never_commands_load(self):
        self.h.ready = False
        with self.assertRaisesRegex(ValueError, 'not confirmed'):
            self.h.request(True)
        self.h.step()
        self.command.assert_not_called()

    def test_arming_requires_new_sample_not_restored_or_cached_temperature(self):
        self.sample['received_monotonic'] = 999
        self.arm()
        self.h.step()
        self.assertEqual(self.h.phase, 'arming')
        self.command.assert_called_once_with(False)
        self.clock.return_value = 1091
        self.h.step()
        self.assertEqual(self.h.phase, 'alarm')
        self.assertFalse(self.h.armed)
        self.assertFalse(self.h.output)

    def test_hysteresis_and_readback_each_new_sample(self):
        self.arm()
        for temp, now, expected in [(6, 1001, False), (5, 1061, True), (7, 1121, True),
                                     (9.99, 1181, True), (10, 1241, False), (7, 1301, False)]:
            self.sample_at(temp, now)
            self.assertEqual(self.h.output, expected)
        count = self.command.call_count
        self.h.step()
        self.assertEqual(self.command.call_count, count)
        self.assertEqual(self.h.phase, 'waiting')

    def test_sensor_failure_turns_off_and_latches_until_explicit_rearm(self):
        self.arm()
        self.sample_at(4, 1001)
        self.sample['error'] = 'sensor disconnected'
        self.h.step()
        self.assertEqual(self.h.phase, 'alarm')
        self.assertFalse(self.h.output)
        self.sample['error'] = None
        self.sample_at(2, 1061)
        self.assertFalse(self.h.armed)
        self.assertEqual(self.h.phase, 'alarm')
        self.h.request(True)
        self.h.step()
        self.sample_at(3, 1062)
        self.assertTrue(self.h.output)
        self.assertIsNone(self.h.alarm)

    def test_staleness_uses_monotonic_age_not_wall_clock(self):
        self.arm()
        self.sample_at(4, 1001)
        self.clock.return_value = 1092
        with patch.object(heating.time, 'time', return_value=-999999):
            self.h.step()
        self.assertIn('stale', self.h.alarm)
        self.assertFalse(self.h.output)

    def test_invalid_missing_hot_and_extreme_temperatures_fail_off(self):
        for value in (None, float('nan'), float('inf'), '4', True, -21, 25, 70):
            with self.subTest(value=value):
                self.clock.return_value = 1000
                self.h = heating.Heating(self.sensor, self.command, self.health, self.path, ready=True)
                self.h.restart_off = False
                self.arm()
                self.sample_at(value, 1001)
                self.assertEqual(self.h.phase, 'alarm')
                self.assertFalse(self.h.output)

    def test_controller_or_storage_fault_fails_off(self):
        self.arm()
        self.sample_at(4, 1001)
        self.health.return_value = 'Storage failed'
        self.h.step()
        self.assertEqual(self.h.alarm, 'Storage failed')
        self.assertFalse(self.h.output)

    def test_continuous_limit_cannot_be_reset_by_repeated_arm(self):
        self.arm()
        self.sample_at(4, 1001)
        for now in range(1061, 2802, 60):
            self.h.request(True)
            self.sample_at(4, now)
        self.assertIn('30-minute', self.h.alarm)
        self.assertFalse(self.h.output)

    def test_ambiguous_on_then_failed_off_is_unknown_and_not_retried(self):
        self.arm()
        self.command.side_effect = RuntimeError('serial gone')
        with self.assertLogs(level='ERROR'):
            self.sample_at(4, 1001)
        self.assertIsNone(self.h.output)
        self.assertTrue(self.h.owned)
        self.assertIn('unconfirmed', self.h.reason)
        count = self.command.call_count
        self.h.step()
        self.assertEqual(count, self.command.call_count)
        self.assertTrue(json.loads(self.path.read_text())['owned'])

    def test_disable_while_heating_and_manual_off_disarm(self):
        self.arm()
        self.sample_at(4, 1001)
        with self.assertRaisesRegex(RuntimeError, 'owns LOAD'):
            self.h.manual(True, self.command)
        result = self.h.manual(False, self.command)
        self.assertFalse(result['load_on'])
        self.assertFalse(self.h.armed)
        self.assertFalse(self.h.owned)
        self.assertEqual(self.h.phase, 'disabled')
        self.sensor.set_interval.assert_called_with(600)

    def test_disabled_does_not_take_ownership_of_unrelated_manual_load(self):
        self.h.manual(True, self.command)
        self.h.step()
        self.command.assert_called_once_with(True)
        self.assertFalse(self.h.owned)

    def test_disable_cancels_pending_arm_and_clears_alarm_after_verified_off(self):
        self.h.request(True)
        self.h.request(False)
        self.h.step()
        self.assertEqual(self.h.phase, 'disabled')
        self.command.assert_called_once_with(False)

    def test_crash_recovery_disarms_even_when_installation_lock_is_off(self):
        self.arm()
        self.sample_at(4, 1001)
        self.assertTrue(json.loads(self.path.read_text())['owned'])
        recovered = heating.Heating(self.sensor, self.command, self.health, self.path, ready=False)
        recovered.step()
        self.command.assert_called_with(False)
        self.assertFalse(recovered.armed)
        self.assertEqual(recovered.phase, 'alarm')
        self.assertFalse(json.loads(self.path.read_text())['owned'])

    def test_corrupt_state_requests_off_without_turning_on(self):
        self.path.write_text('{bad')
        recovered = heating.Heating(self.sensor, self.command, self.health, self.path, ready=True)
        recovered.step()
        self.command.assert_called_once_with(False)
        self.assertEqual(recovered.phase, 'alarm')

    def test_durable_state_failure_prevents_on(self):
        with patch.object(self.h, 'save', side_effect=OSError('disk full')), self.assertLogs(level='ERROR'):
            self.h.request(True)
            self.h.step()
        self.assertTrue(all(call.args == (False,) for call in self.command.call_args_list))
        self.assertFalse(self.h.armed)
        self.assertEqual(self.h.phase, 'alarm')

    def test_orderly_shutdown_requests_off_and_persists_disarmed_state(self):
        self.arm()
        self.sample_at(4, 1001)
        self.h.stop.set()
        self.h.run()
        self.assertFalse(self.h.output)
        self.assertFalse(self.h.armed)
        self.assertFalse(json.loads(self.path.read_text())['owned'])


if __name__ == '__main__':
    unittest.main()
