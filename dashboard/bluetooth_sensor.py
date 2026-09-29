"""Optional Xiaomi LYWSD03MMC live readings using the host's existing BlueZ tools.

No pairing or firmware change is required. Only the verified measurement
characteristic's notification descriptor is written. Connections are brief.
"""
import datetime
import logging
import os
import re
import selectors
import struct
import subprocess
import threading
import time

MEASUREMENT_UUID = 'ebe0ccc1-7a0a-4b0c-8a1a-6ff2997da3a6'
CCCD_UUID = '00002902-0000-1000-8000-00805f9b34fb'
INTERVAL = 600
STALE_AFTER = INTERVAL * 3
CHARACTERISTIC = re.compile(
    r'handle = 0x([\da-f]+), char properties = 0x([\da-f]+), '
    r'char value handle = 0x([\da-f]+), uuid = ([\da-f-]+)', re.I)
DESCRIPTOR = re.compile(r'handle = 0x([\da-f]+), uuid = ([\da-f-]+)', re.I)
NOTIFICATION = re.compile(r'Notification handle = 0x([\da-f]+) value: ([\da-f ]+)\s*', re.I)


def decode(payload):
    """Stock Xiaomi payload: signed centidegrees, RH %, battery millivolts.

    Format: github.com/JsBergbau/MiTemperature2 (handleNotification).
    """
    if len(payload) != 5:
        raise ValueError('Expected a five-byte Xiaomi measurement')
    temperature, humidity, millivolts = struct.unpack('<hBH', payload)
    if not (-5000 <= temperature <= 10000 and 0 <= humidity <= 100 and 1000 <= millivolts <= 4000):
        raise ValueError('Invalid Xiaomi measurement')
    return dict(temperature_C=temperature / 100, humidity_percent=humidity,
                battery_V=millivolts / 1000)


def measurement_range(output):
    entries = [(int(h, 16), int(p, 16), int(v, 16), u.lower())
               for h, p, v, u in CHARACTERISTIC.findall(output)]
    for declaration, properties, value, uuid in entries:
        if uuid == MEASUREMENT_UUID and properties & 0x10:
            end = min((h for h, _, _, _ in entries if h > declaration), default=0x10000) - 1
            return value, end
    raise ValueError('Xiaomi measurement characteristic not found')


def notification_descriptor(output, value, end):
    matches = [int(h, 16) for h, uuid in DESCRIPTOR.findall(output)
               if uuid.lower() == CCCD_UUID and value < int(h, 16) <= end]
    if len(matches) != 1:
        raise ValueError('Xiaomi notification descriptor not found unambiguously')
    return matches[0]


class XiaomiSensor:
    def __init__(self, address='', name='Cabana climate', on_reading=None):
        self.address = address.strip().upper()
        self.name = name
        self.lock = threading.Lock()
        self.state = dict(reading=None, error=None, last_attempt=None)
        self.handles = None
        self.on_reading = on_reading

    def restore(self, reading):
        with self.lock:
            self.state.update(reading=reading, error=None,
                              last_attempt=reading['timestamp_seconds'] if reading else None)

    def snapshot(self):
        with self.lock:
            data = dict(self.state)
        reading = data['reading']
        age = None if reading is None else max(0, time.time() - reading['timestamp_seconds'])
        return dict(data, enabled=bool(self.address), name=self.name, model='LYWSD03MMC',
                    age_seconds=age, stale=age is None or age > STALE_AFTER,
                    stale_after_seconds=STALE_AFTER, poll_interval_seconds=INTERVAL)

    def discover(self):
        if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', self.address):
            raise ValueError('Invalid BLUETOOTH_SENSOR_ADDRESS')
        def query(*args):
            return subprocess.run(['gatttool', '-b', self.address, *args],
                                  capture_output=True, text=True, timeout=12, check=True).stdout
        value, end = measurement_range(query('--characteristics'))
        descriptor = notification_descriptor(
            query('--char-desc', f'--start=0x{value + 1:04x}', f'--end=0x{end:04x}'), value, end)
        self.handles = value, descriptor

    def read(self):
        if self.handles is None:
            self.discover()
        value, descriptor = self.handles
        command = ['stdbuf', '-oL', 'gatttool', '-b', self.address, '--char-write-req',
                   f'--handle=0x{descriptor:04x}', '--value=0100', '--listen']
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 18
                pending = b''
                while time.monotonic() < deadline:
                    if not selector.select(max(0, deadline - time.monotonic())):
                        break
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    pending += chunk
                    if len(pending) > 65536:
                        raise ValueError('Unexpected Bluetooth response size')
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        match = NOTIFICATION.fullmatch(line.decode('ascii', errors='replace').strip())
                        if match and int(match[1], 16) == value:
                            reading = decode(bytes.fromhex(match[2]))
                            now = time.time()
                            reading.update(timestamp_seconds=now, timestamp_utc=
                                           datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat())
                            return reading
            raise TimeoutError('No Xiaomi measurement received; check Bluetooth power and sensor range')
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()

    def poll_once(self):
        attempt = time.time()
        try:
            reading = self.read()
        except Exception:
            logging.exception('Bluetooth sensor read failed')
            # Rediscover after a failure, including firmware/handle changes.
            self.handles = None
            with self.lock:
                self.state.update(error='Sensor unavailable. Retrying every 10 minutes.', last_attempt=attempt)
            return
        error = None
        if self.on_reading is not None:
            try:
                self.on_reading(self.address, reading)
            except Exception:
                logging.exception('Saving Bluetooth sensor reading failed')
                error = 'Live sensor reading received, but could not save history. Check storage on the Pi.'
        with self.lock:
            self.state.update(reading=reading, error=error, last_attempt=attempt)

    def poll_loop(self):
        # A restart must not trigger another radio connection for a recent sample.
        reading = self.snapshot()['reading']
        if reading:
            remaining = min(INTERVAL, max(0, INTERVAL - (time.time() - reading['timestamp_seconds'])))
            if remaining:
                time.sleep(remaining)
        while True:
            start = time.monotonic()
            self.poll_once()
            time.sleep(max(1, INTERVAL - (time.monotonic() - start)))

    def start(self):
        if self.address:
            threading.Thread(target=self.poll_loop, daemon=True, name='bluetooth-sensor').start()
