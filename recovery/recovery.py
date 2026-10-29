"""Independent Pi recovery with persisted backoff; stdlib only, root owned."""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import threading
import uuid
from urllib.request import urlopen

ROOT = Path('/var/lib/cabana-recovery')
INTERVALS = (600, 3600, 18000)


def atomic(path, value):
    data = json.dumps(value, indent=2).encode()
    temporary = path.with_suffix('.tmp')
    with temporary.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o644)
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run(args, timeout=8):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def probes():
    bad = []
    routes = run(['ip', '-j', 'route', 'show', 'default'])
    try:
        gateways = [r['gateway'] for r in json.loads(routes.stdout) if r.get('gateway')]
    except (AttributeError, ValueError):
        gateways = []
    gateway_ok = False
    for gateway in gateways:
        result = run(['ping', '-n', '-c', '1', '-W', '2', gateway], 4)
        gateway_ok |= result is not None and result.returncode == 0
    if not gateway_ok:
        bad.append('network')
    result = run(['tailscale', 'status', '--json'], 5)
    try:
        backend = json.loads(result.stdout)['BackendState']
    except (AttributeError, ValueError, KeyError):
        backend = 'Unresponsive'
    # Authentication/coordination outages need attention, not repeated reboots.
    if backend in ('Unresponsive', 'Stopped', 'Starting'):
        bad.append('tailscale')
    try:
        with socket.create_connection(('127.0.0.1', 22), timeout=3) as sock:
            sock.settimeout(3)
            if not sock.recv(256).startswith(b'SSH-'):
                bad.append('ssh')
    except OSError:
        bad.append('ssh')
    try:
        with urlopen('http://127.0.0.1:8080/api/status', timeout=4) as response:
            status = json.load(response)
            if 'server_time_seconds' not in status:
                bad.append('dashboard')
    except (OSError, ValueError):
        bad.append('dashboard')
    return sorted(set(bad)), backend


class Policy:
    def __init__(self, state=None):
        self.state = state or dict(stage=0, due=None, healthy_since=None, restarted=[], reasons=[])

    def rebase_after_boot(self, now):
        # Only the escalation stage crosses boots. Monotonic deadlines are
        # rebuilt, so NTP corrections and a Pi without an RTC cannot cause loops.
        s = self.state
        if s['due'] is not None:
            s['due'] = now + INTERVALS[s['stage']]
        s.update(healthy_since=None, failure_since=now, restarted=[])

    def step(self, bad, now):
        s = self.state
        actions = []
        if not bad:
            s['reasons'] = []
            if s['healthy_since'] is None:
                s['healthy_since'] = now
            if now - s['healthy_since'] >= 1800:
                s.update(stage=0, due=None, restarted=[], reasons=[])
            return actions
        s['healthy_since'] = None
        if s['due'] is None:
            s['due'] = now + INTERVALS[s['stage']]
            s['failure_since'] = now
        s['reasons'] = bad
        # Backoff is retained across short recoveries; require a fresh sustained
        # ten-minute failure before reboot, even if an older deadline has passed.
        if self._previous_healthy:
            s['failure_since'] = now
        since = s.setdefault('failure_since', now)
        if now - since >= 60:
            for reason in bad:
                if reason not in s['restarted']:
                    s['restarted'].append(reason)
                    actions.append(('restart', reason))
        if now >= s['due'] and now - since >= 600:
            s['stage'] = min(2, s['stage'] + 1)
            s['due'] = now + INTERVALS[s['stage']]
            s['failure_since'] = now
            s['restarted'] = []
            actions.append(('reboot', ', '.join(bad)))
        return actions

    def tick(self, bad, now):
        self._previous_healthy = self.state.get('healthy_since') is not None
        return self.step(bad, now)


class Recovery:
    def __init__(self):
        ROOT.mkdir(mode=0o755, exist_ok=True)
        self.event_lock = threading.RLock()
        try:
            state = json.loads((ROOT/'state.json').read_text())
        except FileNotFoundError:
            state = None
        # Corrupted state deliberately fails startup instead of resetting backoff.
        self.policy = Policy(state)
        self.policy.rebase_after_boot(time.monotonic())
        self.last_saved = None
        try:
            self.events = json.loads((ROOT/'events.json').read_text())
        except FileNotFoundError:
            self.events = []
        self.journal_cursor = None
        self.boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.incident('Pi recovery monitor started; boot ' + self.boot_id, 'info')

    def incident(self, message, severity='error'):
        with self.event_lock:
            self.events.append(dict(id=str(uuid.uuid4()), timestamp=time.time(), message=message[:500], severity=severity))
            self.events = self.events[-100:]
            atomic(ROOT/'events.json', self.events)

    def ingest_kernel(self, item):
        cursor = item.get('__CURSOR')
        message = item.get('MESSAGE', '')
        if not cursor or not isinstance(message, str) or not re.search(r'out of memory|oom-kill|killed process|I/O error|EXT4-fs error|mmc.*(error|timeout)|watchdog.*(lockup|timeout)|hung task|blocked for more than', message, re.I):
            return
        identity = 'kernel:' + str(cursor)
        with self.event_lock:
            if any(e['id'] == identity for e in self.events):
                return
            stamp = float(item.get('__REALTIME_TIMESTAMP', time.time()*1000000))/1000000
            self.events.append(dict(id=identity, timestamp=stamp, message='Pi kernel: ' + message[:450], severity='error'))
            self.events = self.events[-100:]
            atomic(ROOT/'events.json', self.events)

    def follow_kernel(self):
        # Dedicated stream: a failed network probe cannot delay a critical log.
        while True:
            try:
                with subprocess.Popen(['journalctl', '-k', '-b', '-f', '-p', '0..3',
                                       '-o', 'json', '--no-pager', '-n', '100'],
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as process:
                    for line in process.stdout:
                        try:
                            self.ingest_kernel(json.loads(line))
                        except (ValueError, TypeError):
                            continue
            except OSError:
                pass  # The main-loop read also retries; journald remains persistent.
            time.sleep(5)

    def save(self):
        encoded = json.dumps(self.policy.state, sort_keys=True)
        if encoded != self.last_saved:
            atomic(ROOT/'state.json', self.policy.state)
            self.last_saved = encoded

    def kernel_errors(self):
        args = ['journalctl', '-k', '-b', '-p', '0..3', '-o', 'json', '--no-pager', '-n', '100']
        if self.journal_cursor:
            args += ['--after-cursor', self.journal_cursor]
        result = run(args, 5)
        if result is None or result.returncode:
            return
        for line in result.stdout.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            self.ingest_kernel(item)
            self.journal_cursor = item.get('__CURSOR') or self.journal_cursor


    def cycle(self):
        self.kernel_errors()
        bad, backend = probes()
        inhibited = Path('/run/cabana-recovery.inhibit').exists()
        now = time.monotonic()
        actions = [] if inhibited else self.policy.tick(bad, now)
        since = self.policy.state.get('failure_since', now)
        reported = bad if bad and now - since >= 60 else []
        Path('/run/cabana-recovery').mkdir(exist_ok=True)
        atomic(Path('/run/cabana-recovery/status.json'), dict(checked_at=time.time(),
               failed_probes=reported, pending_probes=bad, tailscale_backend=backend,
               reboot_stage=self.policy.state['stage'], inhibited=inhibited))
        if inhibited:
            return
        if reported != self.policy.state.get('reported', []):
            self.incident('Pi health: ' + (', '.join(reported) + ' failing for at least 1 minute' if reported else 'recovered'), 'error' if reported else 'info')
            self.policy.state['reported'] = reported
        self.save()  # Required before any restart/reboot; failure prevents action.
        for action, reason in actions:
            self.incident(f'Automatic {action}: {reason}; reboot backoff stage {self.policy.state["stage"]}')
            if action == 'reboot':
                # Leave a delivery window for HA before orderly reboot.
                time.sleep(5)
                run(['systemctl', 'reboot'], 10)
            elif reason == 'dashboard':
                run(['runuser', '-u', 'alex', '--', 'env', 'XDG_RUNTIME_DIR=/run/user/1000',
                     'systemctl', '--user', 'restart', '--no-block', 'cabana-solar.service'])
            else:
                unit = {'network': 'NetworkManager', 'ssh': 'ssh', 'tailscale': 'tailscaled'}[reason]
                run(['systemctl', 'restart', '--no-block', unit])


def notify(message):
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect('\0' + address[1:] if address.startswith('@') else address)
            sock.sendall(message.encode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.check:
        bad, backend = probes()
        print(json.dumps(dict(failed_probes=bad, tailscale_backend=backend)))
        return
    recovery = Recovery()
    threading.Thread(target=recovery.follow_kernel, daemon=True, name='critical-kernel-events').start()
    notify('READY=1')
    while True:
        recovery.cycle()
        notify('WATCHDOG=1')
        time.sleep(10)

if __name__ == '__main__':
    main()
