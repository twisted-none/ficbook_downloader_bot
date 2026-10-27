"""Per-job CONNECT proxy with public DNS validation and pinned sockets."""

from contextlib import contextmanager
import select
import socket
import socketserver
import threading
import time
from urllib.parse import urlsplit

from src.sources.safe_http import require_https_url, resolve_addresses

HOSTS = frozenset({'archiveofourown.org', 'www.archiveofourown.org',
                   'download.archiveofourown.org', 'challenges.cloudflare.com'})


def allowed_url(url):
    try:
        require_https_url(url.split('#', 1)[0], HOSTS)
        return True
    except Exception:
        return False


def connect_target(authority):
    if authority not in {host + ':443' for host in HOSTS}:
        raise ValueError('Unsafe destination')
    addresses = resolve_addresses(authority[:-4], 443)
    for family, kind, protocol, _, address in addresses:
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(8)
            connection.connect(address)
            return connection
        except OSError:
            connection.close()
    raise OSError('Public AO3 connection failed')


class _Tunnel(socketserver.BaseRequestHandler):
    def handle(self):
        if not self.server.slots.acquire(blocking=False):
            return
        try:
            self.request.settimeout(8)
            header = bytearray()
            while not header.endswith(b'\r\n\r\n'):
                byte = self.request.recv(1)
                if not byte or len(header) >= 8192:
                    return
                header.extend(byte)
            method, authority, protocol = header.split(b'\r\n', 1)[0].decode('ascii').split(' ')
            if method != 'CONNECT' or protocol != 'HTTP/1.1':
                return
            with self.server.lock:
                self.server.connections += 1
                if self.server.connections > 200:
                    return
            with connect_target(authority) as upstream:
                self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                while time.monotonic() < self.server.deadline:
                    readable, _, _ = select.select([self.request, upstream], [], [], 1)
                    for current in readable:
                        data = current.recv(65536)
                        if not data:
                            return
                        with self.server.lock:
                            self.server.bytes += len(data)
                            if self.server.bytes > 64_000_000:
                                return
                        (upstream if current is self.request else self.request).sendall(data)
        except (OSError, ValueError, UnicodeError):
            pass
        finally:
            self.server.slots.release()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False


@contextmanager
def native_transport():
    with _Server(('127.0.0.1', 0), _Tunnel) as server:
        server.slots = threading.BoundedSemaphore(32)
        server.lock = threading.Lock()
        server.connections = server.bytes = 0
        server.deadline = time.monotonic() + 155
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_address[1]}'
        finally:
            server.deadline = 0
            server.shutdown()
            thread.join(timeout=2)
