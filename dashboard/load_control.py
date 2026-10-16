"""Private local command socket, reached remotely through authenticated SSH."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import threading
import time


def socket_path():
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
    if not runtime.is_dir():
        runtime = Path(__file__).resolve().parent
    return runtime / 'wattcycle-control' / 'control.sock'


class Handler(socketserver.StreamRequestHandler):
    timeout = 2

    def handle(self):
        started = time.monotonic()
        try:
            command = self.rfile.readline(16)
            if command not in (b'on\n', b'off\n'):
                raise ValueError('Expected on or off')
            reading = self.server.control(command == b'on\n')
            response = dict(ok=True, reading=reading)
        except Exception as exc:
            response = dict(ok=False, error=str(exc))
        response['elapsed_seconds'] = round(time.monotonic() - started, 3)
        try:
            self.wfile.write(json.dumps(response).encode() + b'\n')
        except OSError:
            pass  # A disconnected client does not cancel an accepted command.


class ControlServer:
    def __init__(self, control, path=None):
        self.path = Path(path) if path is not None else socket_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError('Control directory must be owned by this user with permissions 0700')
        self.lock = (self.path.parent / 'owner.lock').open('a')
        self.server = None
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Only the lock owner can remove a socket left by a crashed process.
            self.path.unlink(missing_ok=True)
            self.server = socketserver.ThreadingUnixStreamServer(str(self.path), Handler)
            os.chmod(self.path, 0o600)
            self.server.control = control
            self.thread = threading.Thread(target=self.server.serve_forever,
                                           kwargs={'poll_interval': .1}, name='load-control', daemon=True)
            self.thread.start()
        except BaseException:
            if self.server is not None:
                self.server.server_close()
                self.path.unlink(missing_ok=True)
            self.lock.close()
            raise

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.path.unlink(missing_ok=True)
        self.lock.close()


def request(command, path=None):
    if command not in ('on', 'off'):
        raise ValueError('Expected on or off')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(40)
        client.connect(str(path if path is not None else socket_path()))
        client.sendall(command.encode() + b'\n')
        with client.makefile('rb') as stream:
            reply = stream.readline(65536)
    if not reply.endswith(b'\n'):
        raise RuntimeError('Incomplete response; LOAD state unconfirmed. Do not retry automatically.')
    return json.loads(reply)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('state', choices=('on', 'off'))
    args = parser.parse_args()
    try:
        result = request(args.state)
    except (OSError, ValueError, RuntimeError) as exc:
        result = dict(ok=False, error=f'{exc}; LOAD state unconfirmed. No automatic retry.')
    print(json.dumps(result, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
