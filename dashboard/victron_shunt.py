"""Read-only Victron Instant Readout using existing BlueZ and Linux kernel AES.

Format: Victron Extra Manufacturer Data, 2022-12-14, Battery Monitor record.
Only advertisements are read: no pairing, connections, settings or GATT writes.
"""
import datetime
from pathlib import Path
import re
import socket
import subprocess
import threading
import time

INTERVAL = 5
STALE_AFTER = 30
ALARM_LABELS = ('Low voltage', 'High voltage', 'Low state of charge',
                'Low starter voltage', 'High starter voltage', 'Low temperature',
                'High temperature', 'Midpoint voltage', 'Overload', 'DC ripple',
                'Low AC output voltage', 'High AC output voltage', 'Short circuit', 'BMS lockout')


def aes_block(key, block):
    """Encrypt one AES block without packages or credentials in process arguments."""
    with socket.socket(socket.AF_ALG, socket.SOCK_SEQPACKET, 0) as algorithm:
        algorithm.bind(('skcipher', 'ecb(aes)'))
        algorithm.setsockopt(socket.SOL_ALG, socket.ALG_SET_KEY, key)
        connection, _ = algorithm.accept()
        with connection:
            connection.settimeout(2)
            connection.sendmsg_afalg([block], op=socket.ALG_OP_ENCRYPT)
            result = connection.recv(16)
    if len(result) != 16:
        raise ValueError('Incomplete AES result')
    return result


def decode(data, key):
    if len(key) != 16 or len(data) < 23 or data[:2] != b'\x10\x02' or data[4] != 2:
        raise ValueError('Not a complete Victron battery monitor advertisement')
    if data[7] != key[0]:
        raise ValueError('Shunt encryption key does not match; check Instant Readout key')
    # The defined 120-bit battery record fits one AES counter block. Future
    # appended fields are ignored, as required by the manufacturer's format.
    stream = aes_block(key, data[5:7] + bytes(14))
    bits = int.from_bytes(bytes(a ^ b for a, b in zip(data[8:23], stream)), 'little')
    offset = 0
    def take(width):
        nonlocal offset
        value = (bits >> offset) & ((1 << width) - 1)
        offset += width
        return value
    def signed(value, width):
        return value - (1 << width) if value & (1 << (width - 1)) else value
    minutes, voltage, alarms, aux = take(16), take(16), take(16), take(16)
    mode, current, consumed, soc = take(2), take(22), take(20), take(10)
    if soc != 0x3ff and soc > 1000:
        raise ValueError('Invalid shunt state of charge; verify encryption key')
    volts = signed(voltage, 16) / 100 if voltage != 0x7fff else None
    amps = signed(current, 22) / 1000 if current != 0x3fffff else None
    return dict(battery_V=volts, current_A=amps,
                net_power_W=round(volts * amps, 2) if volts is not None and amps is not None else None,
                soc_percent=soc / 10 if soc != 0x3ff else None,
                consumed_Ah=-consumed / 10 if consumed != 0xfffff else None,
                time_to_go_minutes=minutes if minutes != 0xffff else None,
                alarm_bits=alarms,
                alarms=[label for bit, label in enumerate(ALARM_LABELS) if alarms & (1 << bit)] +
                       (['Other alarm'] if alarms >> len(ALARM_LABELS) else []),
                aux_mode=mode,
                starter_V=signed(aux, 16) / 100 if mode == 0 and aux != 0x7fff else None,
                midpoint_V=aux / 100 if mode == 1 and aux != 0xffff else None,
                temperature_C=round(aux / 100 - 273.15, 2) if mode == 2 and aux != 0xffff else None)


def manufacturer_data(output):
    """busctl's text format avoids its byte-array JSON bug on Cabana's version."""
    fields = output.split()
    if len(fields) < 5 or fields[:4] != ['a{qv}', '1', '737', 'ay']:
        raise ValueError('No Victron broadcast available')
    length = int(fields[4])
    if not 0 <= length <= 255 or len(fields) != length + 5:
        raise ValueError('Incomplete Bluetooth broadcast')
    return bytes(int(value) for value in fields[5:])


class VictronShunt:
    def __init__(self, address='', key_path=None, on_reading=None):
        self.address = address.strip().upper()
        self.key_path = Path(key_path) if key_path else None
        self.lock = threading.Lock()
        self.reading = None
        self.error = None
        self.received = None
        self.last_frame = None
        self.on_reading = on_reading

    def snapshot(self):
        with self.lock:
            age = None if self.received is None else max(0, time.monotonic() - self.received)
            return dict(enabled=bool(self.address), name='Battery bank', model='Victron SmartShunt',
                        reading=dict(self.reading) if self.reading else None,
                        error=self.error, age_seconds=age, stale=age is None or age > STALE_AFTER,
                        stale_after_seconds=STALE_AFTER, poll_interval_seconds=INTERVAL)

    def accept(self, frame, key):
        # Ignore BlueZ's cached frame on startup and repeated data thereafter.
        # Only an observed change counts as a newly received measurement.
        previous, self.last_frame = self.last_frame, frame
        if previous is None or previous == frame:
            return False
        reading = decode(frame, key)
        reading['timestamp_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.lock:
            self.reading, self.received, self.error = reading, time.monotonic(), None
        if self.on_reading is not None:
            self.on_reading(reading)
        return True

    def poll_loop(self, stop):
        scanner = None
        try:
            if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', self.address):
                raise ValueError('Invalid shunt Bluetooth address')
            key = bytes.fromhex(self.key_path.read_text().strip())
            if len(key) != 16:
                raise ValueError('Shunt key must contain 32 hexadecimal characters')
            device = '/org/bluez/hci0/dev_' + self.address.replace(':', '_')
            while not stop.is_set():
                try:
                    if scanner is None or scanner.poll() is not None:
                        scanner = subprocess.Popen(['bluetoothctl', '--timeout', '3600', 'scan', 'le'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    result = subprocess.run(['busctl', 'get-property', 'org.bluez', device,
                        'org.bluez.Device1', 'ManufacturerData'], capture_output=True,
                        text=True, timeout=4, check=True)
                    self.accept(manufacturer_data(result.stdout), key)
                except ValueError as exc:
                    with self.lock:
                        self.error = str(exc)
                except (OSError, subprocess.SubprocessError):
                    with self.lock:
                        self.error = 'Waiting for the shunt. Check Bluetooth range and disconnect VictronConnect.'
                stop.wait(INTERVAL)
        except (OSError, ValueError, AttributeError):
            with self.lock:
                self.error = 'Shunt configuration unavailable. Check its private key file on the Pi.'
        finally:
            if scanner is not None:
                if scanner.poll() is None:
                    scanner.terminate()
                try:
                    scanner.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    scanner.kill()
                    scanner.wait()

    def start(self, stop):
        if self.address:
            thread = threading.Thread(target=self.poll_loop, args=(stop,), daemon=True, name='victron-shunt')
            thread.start()
            return thread
