"""Time-to-100% calculation using verified battery metadata.

Battery capacity and efficiency are provisioned from verified shunt settings.
This module never connects to, configures, or controls battery hardware.
"""
from collections import deque
import json
import math
import os
from pathlib import Path
import threading
import time

TARGET_PERCENT = 100
STALE_SECONDS = 30
MIN_CURRENT_A = .05
AVERAGING_SECONDS = 180
MIN_AVERAGING_SECONDS = 60


class ChargeTimer:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.samples = deque()
        self.settings = None
        self.verified_date = None
        self.soc_needs_sync = False
        try:
            settings = json.loads(self.path.read_text())
            for key, low, high in [('capacity_Ah', 1, 32500), ('charge_efficiency_percent', 1, 100),
                                    ('discharge_floor_percent', 0, 99)]:
                value = settings[key]
                if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                    raise ValueError('Invalid verified battery setting')
            self.verified_date = settings.get('verified_date')
            self.soc_needs_sync = bool(settings.get('soc_needs_sync', False))
            self.settings = {key: settings[key] for key in
                             ('capacity_Ah', 'charge_efficiency_percent', 'discharge_floor_percent')}
        except (OSError, ValueError, TypeError, KeyError):
            pass  # Optional feature: existing monitoring remains available.

    def observe(self, reading):
        current = reading.get('current_A')
        now = time.monotonic()
        with self.lock:
            if type(current) not in (int, float) or not math.isfinite(current):
                self.samples.clear()
                return
            if self.samples and now - self.samples[-1][0] > STALE_SECONDS:
                self.samples.clear()
            self.samples.append((now, current))
            # Keep a predecessor to integrate the edge of the averaging window.
            while len(self.samples) > 2 and self.samples[1][0] <= now - AVERAGING_SECONDS:
                self.samples.popleft()

    def snapshot(self, shunt):
        now = time.monotonic()
        with self.lock:
            settings = dict(self.settings) if self.settings else None
            result = dict(configured=settings is not None, target_percent=TARGET_PERCENT,
                          settings=settings, charge_minutes=None, average_current_A=None,
                          state='unavailable', source='calculated',
                          settings_verified_date=self.verified_date, soc_needs_sync=self.soc_needs_sync,
                          assumption='At recent net charging rate; sunlight and taper are not forecast',
                          averaging_seconds=AVERAGING_SECONDS, observed_seconds=0)
            if settings is None:
                return result
            reading = shunt.get('reading') or {}
            soc, current = reading.get('soc_percent'), reading.get('current_A')
            age = shunt.get('age_seconds')
            if shunt.get('error') or shunt.get('stale') or age is None or age > STALE_SECONDS:
                return dict(result, state='stale')
            if type(soc) not in (int, float) or not math.isfinite(soc) or not 0 <= soc <= 100:
                return result
            if soc >= TARGET_PERCENT:
                # Clear only the advisory after the shunt itself reports full,
                # consumed Ah resets, and the verified charged voltage is met.
                # This never commands synchronization or writes battery hardware.
                if self.soc_needs_sync and reading.get('consumed_Ah') == 0 and (reading.get('battery_V') or 0) >= 28:
                    try:
                        settings_file = json.loads(self.path.read_text())
                        settings_file['soc_needs_sync'] = False
                        temporary = self.path.with_suffix('.sync-tmp')
                        temporary.write_text(json.dumps(settings_file, indent=2) + '\n')
                        temporary.chmod(0o600)
                        os.replace(temporary, self.path)
                        self.soc_needs_sync = False
                        result['soc_needs_sync'] = False
                    except (OSError, ValueError):
                        pass
                return dict(result, charge_minutes=0, state='full')
            if type(current) not in (int, float) or not math.isfinite(current) or current <= MIN_CURRENT_A:
                return dict(result, state='paused')
            if len(self.samples) < 2 or now - self.samples[-1][0] > STALE_SECONDS:
                return dict(result, state='warming')
            start = max(now - AVERAGING_SECONDS, self.samples[0][0])
            duration = now - start
            result['observed_seconds'] = round(duration)
            if duration < MIN_AVERAGING_SECONDS:
                return dict(result, state='warming')
            samples = list(self.samples)
            amp_seconds = 0
            for index, (stamp, amps) in enumerate(samples):
                end = samples[index+1][0] if index+1 < len(samples) else now
                amp_seconds += amps * max(0, end - max(stamp, start))
            average = amp_seconds / duration
            if average <= MIN_CURRENT_A:
                return dict(result, state='warming')
            needed_Ah = settings['capacity_Ah'] * (TARGET_PERCENT - soc) / 100
            minutes = math.ceil(60 * needed_Ah / (average * settings['charge_efficiency_percent'] / 100))
            return dict(result, charge_minutes=minutes, average_current_A=round(average, 3), state='charging')
