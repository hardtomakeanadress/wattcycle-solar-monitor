import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from important_events import ImportantEvents, problems

class EventsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'events.sqlite3'
        self.events = ImportantEvents(self.path)
    def test_transitions_are_durable_and_not_repeated(self):
        self.events.update({'disk':'Disk failed'})
        self.events.update({'disk':'Disk failed'})
        self.assertEqual(self.events.sequence,1)
        restored = ImportantEvents(self.path)
        self.assertEqual(restored.response()['active'][0]['code'],'disk')
        restored.update({})
        restored.update({})
        self.assertEqual(restored.sequence,2)
        self.assertFalse(restored.response()['events'][-1]['active'])
    def test_wait_wakes_on_event_and_replay_uses_cursor(self):
        result=[]
        t=threading.Thread(target=lambda:result.append(self.events.response(wait=2)))
        t.start();time.sleep(.02)
        self.events.emit('test','Test',False,'info')
        t.join(1)
        self.assertFalse(t.is_alive())
        self.assertEqual(result[0]['cursor'],1)
        self.assertEqual(self.events.response(after=1)['events'],[])
    def test_disk_failure_keeps_delivery_and_retries(self):
        with patch.object(self.events,'connect',side_effect=OSError('full')):
            self.events.emit('disk','Disk failed')
        self.assertTrue(self.events.response()['storage_error'])
        self.assertEqual(self.events.response()['events'][0]['code'],'disk')
        self.events.update({'disk':'Disk failed'})
        self.assertIsNone(self.events.storage_error)
        self.assertEqual(ImportantEvents(self.path).sequence,1)
    def test_bounded_history_and_gap_detection(self):
        with patch('important_events.LIMIT',3):
            for n in range(6):self.events.emit(str(n),'test',False)
        self.assertEqual(len(self.events.events),3)
        self.assertTrue(self.events.response(after=1)['gap'])
        self.assertTrue(self.events.response(after=10)['reset'])
        self.assertTrue(self.events.response(generation='old')['reset'])
    def test_memory_debounce_and_sticky_power_flags(self):
        status={'shunt':{'enabled':True,'stale':False},'history':{'error':'disk'}}
        values=problems(status,{'throttle_since_boot':['Under-voltage'],'memory_available_bytes':1,'memory_total_bytes':100})
        self.assertIn('pi_power_thermal_since_boot',values)
        with patch('important_events.time.monotonic',return_value=100):self.events.update(values,{'pi_memory_low':60})
        self.assertNotIn('pi_memory_low',self.events.active)
        with patch('important_events.time.monotonic',return_value=161):self.events.update(values,{'pi_memory_low':60})
        self.assertIn('pi_memory_low',self.events.active)
