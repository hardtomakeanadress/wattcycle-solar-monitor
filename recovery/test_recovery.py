import json
import unittest
from recovery import Policy, Recovery
from unittest.mock import patch
import threading

class RecoveryTest(unittest.TestCase):
    def test_backoff_survives_reboots(self):
        p=Policy()
        self.assertEqual(p.tick(['network'],1000),[])
        self.assertEqual(p.tick(['network'],1060),[('restart','network')])
        self.assertEqual(p.tick(['network'],1599),[])
        self.assertEqual(p.tick(['network'],1600),[('reboot','network')])
        p=Policy(json.loads(json.dumps(p.state)))
        self.assertNotIn(('reboot','network'),p.tick(['network'],2201))
        self.assertIn(('reboot','network'),p.tick(['network'],5200))
        self.assertEqual(p.state['due'],23200)
        self.assertNotIn(('reboot','network'),p.tick(['network'],23199))
        self.assertIn(('reboot','network'),p.tick(['network'],23200))
        self.assertEqual(p.state['due'],41200)
    def test_healthy_30_minutes_resets_and_short_recovery_does_not(self):
        p=Policy();p.tick(['ssh'],0);p.tick(['ssh'],600)
        p.tick([],700);p.tick([],2499)
        self.assertEqual(p.state['stage'],1)
        p.tick([],2500)
        self.assertEqual(p.state['stage'],0)
        self.assertIsNone(p.state['due'])
    def test_brief_new_failure_does_not_reboot_on_expired_deadline(self):
        p=Policy();p.tick(['ssh'],0);p.tick([],500)
        self.assertEqual(p.tick(['ssh'],650),[])
        self.assertNotIn(('reboot','ssh'),p.tick(['ssh'],1249))
        self.assertIn(('reboot','ssh'),p.tick(['ssh'],1250))
    def test_healthy_machine_has_no_recovery_actions(self):
        p=Policy()
        for t in range(0,86400,20):self.assertEqual(p.tick([],t),[])

    def test_reboot_rebases_monotonic_deadline_and_preserves_stage(self):
        p=Policy();p.tick(['network'],1000);p.tick(['network'],1600)
        p=Policy(json.loads(json.dumps(p.state)))
        p.rebase_after_boot(10)
        self.assertEqual(p.state['stage'],1)
        self.assertEqual(p.state['due'],3610)
        self.assertNotIn(('reboot','network'),p.tick(['network'],3609))
        self.assertIn(('reboot','network'),p.tick(['network'],3610))

    def test_kernel_filter_deduplicates_and_ignores_routine_messages(self):
        recovery=object.__new__(Recovery)
        recovery.event_lock=threading.RLock();recovery.events=[]
        with patch('recovery.atomic') as save:
            recovery.ingest_kernel({'__CURSOR':'normal','MESSAGE':'USB device connected'})
            self.assertEqual(recovery.events,[])
            event={'__CURSOR':'error1','MESSAGE':'Out of memory: Killed process 123', '__REALTIME_TIMESTAMP':'1000000'}
            recovery.ingest_kernel(event);recovery.ingest_kernel(event)
            self.assertEqual(len(recovery.events),1)
            self.assertEqual(recovery.events[0]['timestamp'],1)
            save.assert_called_once()
