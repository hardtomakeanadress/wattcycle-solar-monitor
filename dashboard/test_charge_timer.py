"""Charging ETA uses net current and verified capacity; never hardware writes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from charge_timer import ChargeTimer


class ChargeTimerTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)/'charge-timer.json'
        self.config = dict(capacity_Ah=300, charge_efficiency_percent=100,
                           discharge_floor_percent=30, show_charge_timer=True,
                           show_discharge_timer=True, averaging_minutes=3)
        self.path.write_text(json.dumps(self.config))
        self.clock = patch('charge_timer.time.monotonic', return_value=100).start()
        self.addCleanup(patch.stopall)
        self.timer = ChargeTimer(self.path)
        self.shunt = dict(reading={'soc_percent':50, 'current_A':10}, age_seconds=0, stale=False, error=None)

    def sample(self, stamp, amps):
        self.clock.return_value = stamp
        self.timer.observe({'current_A':amps})

    def test_full_target_and_net_charging_current(self):
        self.sample(100,10);self.sample(130,10);self.sample(160,10)
        result=self.timer.snapshot(self.shunt)
        self.assertEqual(result['charge_minutes'],900)  # 150 Ah / 10 A = 15 hours.
        self.assertEqual(result['target_percent'],100)
        self.assertEqual(result['source'],'calculated')

    def test_efficiency_and_time_weighted_average(self):
        self.sample(100,10);self.sample(120,20);self.sample(140,20);self.clock.return_value=160
        result=self.timer.snapshot(self.shunt)
        self.assertAlmostEqual(result['average_current_A'],16.667,places=3)
        self.assertEqual(result['charge_minutes'],540)
        self.timer.settings['charge_efficiency_percent']=95
        self.assertEqual(self.timer.snapshot(self.shunt)['charge_minutes'],569)

    def test_paused_unknown_full_and_stale_have_distinct_states(self):
        self.sample(100,10);self.sample(110,10)
        for changes,state in [({'current_A':-10},'paused'),({'current_A':0},'paused'),
                              ({'soc_percent':None},'unavailable'),({'soc_percent':100},'full')]:
            with self.subTest(state=state):
                shunt=dict(self.shunt,reading=dict(self.shunt['reading'],**changes))
                result=self.timer.snapshot(shunt)
                self.assertEqual(result['state'],state)
                self.assertEqual(result['charge_minutes'],0 if state=='full' else None)
        self.assertEqual(self.timer.snapshot(dict(self.shunt,stale=True))['state'],'stale')

    def test_restart_or_radio_gap_does_not_reuse_old_current(self):
        self.sample(100,10)
        self.assertEqual(self.timer.snapshot(self.shunt)['state'],'warming')
        self.sample(110,10)
        self.sample(160,20)
        self.assertEqual(self.timer.snapshot(self.shunt)['state'],'warming')
        self.assertEqual(ChargeTimer(self.path).snapshot(self.shunt)['state'],'warming')

    def test_brief_dip_keeps_full_net_current_window(self):
        for stamp in range(0,181,5):self.sample(stamp,3)
        self.sample(185,0);self.sample(190,10);self.sample(195,10)
        self.clock.return_value=196
        result=self.timer.snapshot(self.shunt)
        self.assertAlmostEqual(result['average_current_A'],3.15,places=3)
        self.assertEqual(result['observed_seconds'],180)

    def test_negative_current_is_included_and_startup_waits_a_minute(self):
        self.sample(100,10);self.sample(120,-10);self.sample(140,10)
        self.assertEqual(self.timer.snapshot(self.shunt)['state'],'warming')
        self.clock.return_value=160
        self.assertAlmostEqual(self.timer.snapshot(self.shunt)['average_current_A'],3.333,places=3)

    def test_legacy_visibility_and_averaging_preferences_are_ignored(self):
        self.path.write_text(json.dumps(dict(self.config, show_charge_timer=False,
                                            show_discharge_timer=False, averaging_minutes=5)))
        timer = ChargeTimer(self.path)
        for stamp in range(100, 401, 10):
            self.clock.return_value = stamp
            timer.observe({'current_A': 10 if stamp < 200 else 20 if stamp < 300 else 30})
        self.clock.return_value = 400
        result = timer.snapshot(self.shunt)
        self.assertEqual(result['state'], 'charging')
        self.assertAlmostEqual(result['average_current_A'], 25.556, places=3)
        self.assertEqual(set(result['settings']),
                         {'capacity_Ah', 'charge_efficiency_percent', 'discharge_floor_percent'})

    def test_missing_or_invalid_metadata_never_invents_capacity(self):
        self.path.unlink()
        self.assertFalse(ChargeTimer(self.path).snapshot(self.shunt)['configured'])
        self.path.write_text(json.dumps(dict(self.config,capacity_Ah=0)))
        self.assertIsNone(ChargeTimer(self.path).snapshot(self.shunt)['charge_minutes'])

    def test_sync_advisory_requires_full_shunt_readback_and_persists(self):
        self.path.write_text(json.dumps(dict(self.config,soc_needs_sync=True)))
        timer=ChargeTimer(self.path)
        self.assertTrue(timer.snapshot(self.shunt)['soc_needs_sync'])
        full=dict(self.shunt,reading={'soc_percent':100,'current_A':1,'battery_V':28.1,'consumed_Ah':0})
        self.assertFalse(timer.snapshot(full)['soc_needs_sync'])
        self.assertFalse(ChargeTimer(self.path).soc_needs_sync)
