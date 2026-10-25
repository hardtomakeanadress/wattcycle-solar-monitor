"""Run the real browser suite against a temporary, hardware-free dashboard.

Requires development-only Playwright/Chromium. Never starts solar/BLE collectors
or uses the configured production database. The system endpoint measures this host.
"""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'dashboard'))
import server
from bluetooth_sensor import XiaomiSensor


def main():
    with tempfile.TemporaryDirectory(prefix='wattcycle-browser-') as folder:
        server.DB = Path(folder) / 'history.sqlite3'
        server.SYSTEM = server.SystemInfo(Path(folder))
        server.SENSOR = XiaomiSensor('AA:BB:CC:DD:EE:FF', 'Test climate')
        server.initialize()
        now = int(time.time()) - 30
        solar = []
        climate = []
        for offset in range(2 * 86400, -1, -300):
            ts = now - offset
            timestamp = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
            reading = dict(timestamp_utc=timestamp, model='M-2430N', system_V=24,
                charge_W=20 + (offset // 300) % 80, battery_V=27.4, PV_V=44, charge_A=.7,
                today_generation_Wh=373, today_peak_W=231, controller_estimated_SOC_percent=80,
                charge_status='float', controller_C=20, battery_temperature_field_C=25,
                load_on=False, fault_bits=0)
            solar.append((ts, json.dumps(reading)))
            if offset % 600 == 0:
                sensor = dict(timestamp_seconds=ts, timestamp_utc=timestamp,
                              temperature_C=20, humidity_percent=45, battery_V=3)
                climate.append((server.SENSOR.address, ts, json.dumps(sensor)))
        with server.database() as db:
            db.executemany('INSERT INTO samples VALUES (?, ?)', solar)
            db.executemany('INSERT INTO sensor_samples VALUES (?, ?, ?)', climate)
            db.execute('INSERT INTO solar_intervals VALUES (?, 300)', (now - 2 * 86400,))
        server.initialize()
        # Show the real heating panel without starting any hardware supervisor.
        server.HEATING = server.Heating(server.SENSOR, lambda _: (_ for _ in ()).throw(AssertionError('hardware')),
                                       lambda: None, Path(folder)/'heating-state.json', ready=False)
        server.HEATING_KEY = 'browser-fixture-key'
        server.SHUNT = SimpleNamespace(snapshot=lambda: dict(enabled=True, error=None,
            stale=False, age_seconds=0, stale_after_seconds=30,
            reading=dict(battery_V=27.4, current_A=10.3, net_power_W=282.22,
                         soc_percent=71.5, consumed_Ah=-85.5, time_to_go_minutes=None,
                         alarm_bits=0, alarms=[])))
        server.CHARGE_TIMER = SimpleNamespace(snapshot=lambda _: dict(configured=True,
            target_percent=100, state='charging', charge_minutes=125, source='calculated',
            settings=dict(capacity_Ah=300, charge_efficiency_percent=95, discharge_floor_percent=30)))
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': .05})
        thread.start()
        try:
            env = dict(os.environ, DASHBOARD_TEST_URL=f'http://127.0.0.1:{http.server_port}', TEST_DEPLOYED='1')
            result = subprocess.run([os.environ.get('NODE_BINARY', 'node'), str(ROOT / 'scripts/test_browser.cjs')],
                                    env=env, timeout=180)
            return result.returncode
        finally:
            http.shutdown()
            thread.join()
            http.server_close()


if __name__ == '__main__':
    raise SystemExit(main())
