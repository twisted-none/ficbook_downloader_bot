"""Private bounded work-ID API. No credentials, arbitrary URLs or shared profiles."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import signal
import subprocess
import tempfile
import threading

MAX_BYTES = 16 * 1024 * 1024
SLOT = threading.BoundedSemaphore(1)


def download(identifier):
    with tempfile.TemporaryDirectory(prefix='ao3-') as home, tempfile.TemporaryFile() as output:
        environment = {'PATH': '/opt/browser/bin:/usr/local/bin:/usr/bin:/bin', 'HOME': home,
                       'XDG_CACHE_HOME': home + '/cache', 'XDG_CONFIG_HOME': home + '/config',
                       'TMPDIR': home, 'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
        process = subprocess.Popen([
            '/usr/bin/xvfb-run', '-a', '-s', '-screen 0 1440x1100x24 -nolisten tcp',
            '/opt/browser/bin/python', '-I', '/app/browser/run.py', identifier,
        ], stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
            env=environment, start_new_session=True)
        try:
            if process.wait(timeout=165) != 0:
                raise ValueError('Browser did not return a complete work')
            if output.tell() > MAX_BYTES:
                raise ValueError('Browser response too large')
            output.seek(0)
            payload = output.read(MAX_BYTES + 1)
            result = json.loads(payload)
            if result.get('work_id') != identifier:
                raise ValueError('Unexpected work')
            return payload
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(180)

    def log_message(self, *args):
        pass  # No URL, user data, browser output or request headers in access logs.

    def respond(self, code, body):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            return self.respond(200, b'{"ok":true}')
        match = re.fullmatch(r'/works/([1-9][0-9]{0,15})', self.path)
        if match is None and self.path != '/status':
            return self.respond(404, b'{}')
        if not SLOT.acquire(blocking=False):
            return self.respond(503, b'{"error":"busy"}')
        try:
            payload = download('status' if self.path == '/status' else match[1])
            self.respond(409 if json.loads(payload).get('maintenance') is True else 200, payload)
        except subprocess.TimeoutExpired:
            try:
                self.respond(502, b'{"error":"timeout"}')
                self.wfile.flush()
            finally:
                # Firefox may create a separate process group. Ending PID 1's child
                # lets Docker reap the whole cgroup before restart, including those processes.
                os._exit(1)
        except (ValueError, OSError):
            self.respond(502, b'{"error":"unavailable"}')
        finally:
            SLOT.release()


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
