"""The removed display settings have no writable endpoint."""
import unittest
from unittest.mock import Mock

import server


class TimerHTTPTest(unittest.TestCase):
    def test_removed_timer_endpoint_returns_not_found(self):
        handler = object.__new__(server.Handler)
        handler.path = '/api/timers'
        handler.send_error = Mock()
        handler.do_POST()
        handler.send_error.assert_called_once_with(404)
