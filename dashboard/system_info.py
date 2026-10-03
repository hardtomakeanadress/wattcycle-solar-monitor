"""Allowlisted Linux host information; no identifiers, configuration or secrets."""
import datetime
import math
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import threading
import time

INTERVAL = 30
THROTTLE_LABELS = ('Under-voltage', 'Frequency capped', 'Throttled', 'Temperature limit')


def read_text(path):
    try:
        return Path(path).read_text(encoding='utf-8').strip('\x00\n ')
    except (OSError, UnicodeError):
        return ''


def measurement(value, scale=1):
    try:
        number = float(value) * scale
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def os_name():
    for line in read_text('/etc/os-release').splitlines():
        if line.startswith('PRETTY_NAME='):
            try:
                words = shlex.split(line.partition('=')[2])
                return ' '.join(words) or None
            except ValueError:
                return None
    return None


def throttle_flags():
    for binary in ('/usr/bin/vcgencmd', '/opt/vc/bin/vcgencmd'):
        try:
            result = subprocess.run([binary, 'get_throttled'], capture_output=True,
                                    text=True, timeout=1, check=True)
        except FileNotFoundError:
            continue
        except (OSError, subprocess.SubprocessError):
            return None, None
        match = re.fullmatch(r'throttled=0x([0-9a-fA-F]+)', result.stdout.strip())
        if not match:
            return None, None
        flags = int(match[1], 16)
        return tuple([label for bit, label in enumerate(THROTTLE_LABELS)
                      if flags & (1 << (bit + offset))] for offset in (0, 16))
    return None, None


class SystemInfo:
    def __init__(self, storage_path):
        self.storage_path = Path(storage_path)
        self.lock = threading.Lock()
        self.cached = None
        self.collected_at = 0

    def snapshot(self):
        with self.lock:
            if self.cached is None or time.monotonic() - self.collected_at >= INTERVAL:
                self.cached = self.collect()
                self.collected_at = time.monotonic()
            return dict(self.cached, age_seconds=max(0, time.monotonic() - self.collected_at),
                        refresh_interval_seconds=INTERVAL)

    def collect(self):
        # Only select the processor description, never the board serial or ID.
        cpu = {}
        for line in read_text('/proc/cpuinfo').splitlines():
            key, separator, value = line.partition(':')
            if separator and key.strip() in ('model name', 'Processor'):
                cpu.setdefault(key.strip(), value.strip())
        memory = {}
        for line in read_text('/proc/meminfo').splitlines():
            match = re.fullmatch(r'(MemTotal|MemAvailable):\s+(\d+) kB', line.strip())
            if match:
                memory[match[1]] = int(match[2]) * 1024
        total_memory = memory.get('MemTotal') or None
        available_memory = memory.get('MemAvailable')
        if total_memory is not None and available_memory is not None and available_memory > total_memory:
            available_memory = None
        total_storage = free_storage = None
        try:
            disk = os.statvfs(self.storage_path)
            total_storage = measurement(disk.f_blocks, disk.f_frsize)
            free_storage = measurement(disk.f_bavail, disk.f_frsize)
        except OSError:
            pass
        uptime = read_text('/proc/uptime').split()
        current, since_boot = throttle_flags()
        return dict(
            timestamp_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            model=read_text('/proc/device-tree/model') or
                  read_text('/sys/firmware/devicetree/base/model') or None,
            cpu_model=cpu.get('model name') or cpu.get('Processor') or None,
            architecture=platform.machine() or None, cpu_cores=os.cpu_count(),
            os=os_name(), kernel=platform.release() or None,
            python_version=platform.python_version(),
            memory_total_bytes=total_memory, memory_available_bytes=available_memory,
            storage_total_bytes=total_storage, storage_free_bytes=free_storage,
            uptime_seconds=measurement(uptime[0] if uptime else None),
            temperature_C=measurement(read_text('/sys/class/thermal/thermal_zone0/temp'), .001),
            throttle_current=current, throttle_since_boot=since_boot)
