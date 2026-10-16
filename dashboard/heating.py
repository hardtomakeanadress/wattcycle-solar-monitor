"""Opt-in enclosure heating. Software supervision cannot guarantee hardware OFF."""
import json
import logging
import math
import os
from pathlib import Path
import threading
import time

ON_C, OFF_C = 5.0, 10.0
SENSOR_INTERVAL, MAX_SENSOR_AGE = 60, 90
MAX_ON_SECONDS = 1800  # Initial commissioning limit; not a manufacturer value.


class Heating:
    def __init__(self, sensor, command, health, state_path, ready=False):
        self.sensor, self.command, self.health = sensor, command, health
        self.path, self.ready = Path(state_path), ready
        self.lock = threading.Lock()
        self.operation = threading.Lock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.intent = None
        self.owned = False
        self.armed = False
        self.started = None
        self.on_since = None
        self.last_sample = None
        self.output = None
        self.alarm = None
        self.phase = 'disabled'
        self.reason = 'Installation not confirmed. Heating cannot be armed.' if not ready else 'Heating disabled.'
        self.restart_off = False
        try:
            saved = json.loads(self.path.read_text())
            if (not isinstance(saved, dict) or type(saved.get('owned')) is not bool
                    or (saved.get('alarm') is not None and not isinstance(saved['alarm'], str))):
                raise ValueError('Invalid heating state')
            self.owned = saved['owned']
            self.alarm = saved.get('alarm')
            self.restart_off = self.owned
            if self.owned or self.alarm:
                self.phase = 'alarm'
                self.reason = self.alarm or 'Service restarted. Heating requires manual re-arming.'
                self.alarm = self.reason
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            # Unknown ownership after state damage: request OFF, never resume ON.
            self.owned = self.restart_off = True
            self.alarm = 'Heating state could not be restored. Inspect storage and re-arm manually.'
            self.phase, self.reason = 'alarm', self.alarm

    def save(self):
        temporary = self.path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(dict(owned=self.owned, alarm=self.alarm), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def publish(self, **values):
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)

    def snapshot(self):
        with self.lock:
            return dict(available=True, installation_ready=self.ready, armed=self.armed,
                        pending=self.intent is not None, phase=self.phase, message=self.reason,
                        alarm=self.alarm, output_on=self.output, on_C=ON_C, off_C=OFF_C,
                        sensor_interval_seconds=SENSOR_INTERVAL, max_sensor_age_seconds=MAX_SENSOR_AGE,
                        max_on_seconds=MAX_ON_SECONDS, owns_load=self.owned)

    def request(self, armed):
        if type(armed) is not bool:
            raise ValueError('armed must be a boolean')
        with self.lock:
            if self.stop.is_set():
                raise ValueError('Service is stopping')
            if armed and not self.ready:
                raise ValueError('Installation is not confirmed; heating remains disabled')
            if armed and (self.armed or self.intent is True):
                return  # Idempotent requests never reset the maximum-run timer.
            self.intent = armed
        self.wake.set()

    def output_command(self, enabled):
        result = self.command(enabled)
        if result.get('load_on') is not enabled:
            raise RuntimeError('LOAD readback mismatch')
        if enabled and result.get('fault_bits') != 0:
            raise RuntimeError('Controller reports a fault')
        now = time.monotonic()
        if enabled and self.output is not True:
            self.on_since = now
        elif not enabled:
            self.on_since = None
        self.publish(output=enabled)
        return result

    def trip(self, reason):
        self.publish(armed=False, phase='alarm', alarm=reason, reason=reason)
        self.sensor.set_interval(600)
        try:
            self.output_command(False)
            self.publish(owned=False)
        except Exception:
            logging.exception('Heating emergency OFF could not be verified')
            self.publish(output=None, owned=True, reason=reason + ' LOAD OFF unconfirmed; inspect the controller.')
        try:
            self.save()
        except Exception:
            logging.exception('Heating state save failed')
            self.publish(alarm=self.reason + ' Heating state storage failed.', reason=self.reason + ' Heating state storage failed.')

    def disable(self):
        # OFF is an explicit request, including a retry after an unconfirmed OFF.
        self.publish(armed=False, phase='disarming', reason='Disabling heating; verifying LOAD OFF.')
        self.sensor.set_interval(600)
        try:
            result = self.output_command(False)
            self.publish(owned=False, phase='disabled', alarm=None, reason='Heating disabled; LOAD OFF verified.')
            self.save()
            return result
        except Exception:
            logging.exception('Disabling heating failed')
            self.publish(output=None, owned=True, phase='alarm', alarm='Could not confirm heating disabled.',
                         reason='LOAD OFF unconfirmed; inspect the controller. Use Disable to retry.')
            try:
                self.save()
            except Exception:
                logging.exception('Saving heating disable failure failed')

    def step(self):
        with self.operation:
            with self.lock:
                intent, self.intent = self.intent, None
            if self.restart_off:
                self.restart_off = False
                self.trip(self.alarm or 'Service restarted. Re-arm manually.')
                return
            if intent is False:
                self.disable()
                return
            if intent is True:
                try:
                    # Durable ownership precedes any possible ON. Restarts request OFF.
                    self.publish(owned=True, armed=True, phase='arming',
                                 reason='Checking LOAD OFF and waiting for a new enclosure temperature.')
                    self.save()
                    self.output_command(False)
                    self.started = time.monotonic()
                    self.last_sample = None
                    self.sensor.set_interval(SENSOR_INTERVAL, refresh=True)
                except Exception:
                    logging.exception('Arming heating failed')
                    self.trip('Could not safely arm heating. Check controller and storage.')
                return
            if not self.armed:
                return
            now = time.monotonic()
            sensor = self.sensor.snapshot()
            received = sensor.get('received_monotonic')
            reading = sensor.get('reading') or {}
            temperature = reading.get('temperature_C')
            health_error = self.health()
            if health_error:
                self.trip(health_error)
                return
            if not sensor.get('enabled') or sensor.get('error'):
                self.trip('Bluetooth temperature unavailable or reporting an error.')
                return
            if received is not None and (type(received) not in (int, float) or not math.isfinite(received)):
                self.trip('Enclosure temperature timestamp is invalid.')
                return
            if received is None or received < self.started:
                if self.last_sample is not None or now - self.started > MAX_SENSOR_AGE:
                    self.trip('No fresh enclosure temperature received after arming.')
                return
            if (not isinstance(received, (int, float)) or not math.isfinite(received)
                    or now - received < 0 or now - received > MAX_SENSOR_AGE):
                self.trip('Enclosure temperature is stale. Heating stopped.')
                return
            if (type(temperature) not in (float, int) or not math.isfinite(temperature)
                    or not -20 <= temperature <= 60):
                self.trip('Enclosure temperature is invalid or outside the control range.')
                return
            if temperature >= 25:
                self.trip('Enclosure is at or above 25°C; inspect the heater and sensor.')
                return
            if self.on_since is not None and now - self.on_since >= MAX_ON_SECONDS:
                self.trip('Heating reached the 30-minute commissioning limit. Inspect before re-arming.')
                return
            if received == self.last_sample:
                return
            self.last_sample = received
            desired = temperature < OFF_C if self.output else temperature <= ON_C
            try:
                # Each fresh minute sample verifies the controller, even without a transition.
                self.output_command(desired)
                self.publish(alarm=None, phase='heating' if desired else 'waiting',
                             reason='Heating enclosure.' if desired else 'Armed; enclosure is warm enough.')
            except Exception:
                logging.exception('Automatic heating command failed')
                self.trip('Heating control failed. Manual inspection and re-arming required.')

    def manual(self, enabled, callback):
        if not self.operation.acquire(blocking=False):
            raise RuntimeError('Heating control is busy; no manual command sent')
        try:
            if enabled and (self.owned or self.armed or self.intent is True):
                raise RuntimeError('Heating owns LOAD. Disable heating before manual ON.')
            if not enabled and (self.owned or self.armed or self.intent is not None):
                with self.lock:
                    self.intent = None
                result = self.disable()
                if self.output is not False:
                    raise RuntimeError(self.reason)
                return dict(result, heating_disarmed=True)
            return callback(enabled)
        finally:
            self.operation.release()

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    self.step()
                except Exception:
                    logging.exception('Heating supervisor failed')
                    with self.operation:
                        self.trip('Heating supervisor error. Inspect before re-arming.')
                self.wake.wait(1)
                self.wake.clear()
        finally:
            if self.owned or self.armed:
                with self.operation:
                    self.trip('Service stopped. Heating requires manual re-arming.')

    def start(self):
        self.thread = threading.Thread(target=self.run, name='battery-heating', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join()
