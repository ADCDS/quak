"""The single unauthenticated camera request: ``GET /gp/gpCamera?cmd=SC&p=%02``.

These tests exercise the real HTTP and TCP code paths against loopback servers:

* a ``ThreadingHTTPServer`` that records every request path and returns a scripted
  sequence of status codes, so we can assert the exact SC path, the 200 happy path, the
  "500 then 200" retry, and that no extra HTTP request is ever made; and
* a tiny TCP server that sends an SSH identification banner, standing in for Dropbear on
  port 22 so the arm flow's "wait for SSH" step can succeed.

Nothing here talks to a camera, and ``retry_delay``/``wait`` are kept small.
"""

import asyncio
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from quak.ssh import arm

SSH_BANNER = b"SSH-2.0-dropbear_2020.81\r\n"


class _SCHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        server = self.server
        with server.lock:
            server.paths.append(self.path)
            index = server.request_count
            server.request_count += 1
        status = server.status_for(index)
        if server.delay:
            import time

            time.sleep(server.delay)
        try:
            self.send_response(status)
            self.send_header("Content-Length", "1")
            self.end_headers()
            self.wfile.write(b"\x00")
        except OSError:
            pass  # client gave up (e.g. a timeout test); nothing more to do

    def log_message(self, *args):  # silence the test server
        pass


class SCServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, statuses, delay=0.0):
        super().__init__(("127.0.0.1", 0), _SCHandler)
        self.statuses = list(statuses)
        self.delay = delay
        self.request_count = 0
        self.paths = []
        self.lock = threading.Lock()
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def status_for(self, index):
        if index < len(self.statuses):
            return self.statuses[index]
        return self.statuses[-1]

    @property
    def port(self):
        return self.server_address[1]

    def stop(self):
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=2)


class BannerServer:
    """Accepts TCP connections and sends an SSH banner, like Dropbear on port 22."""

    def __init__(self, banner=SSH_BANNER):
        self.banner = banner
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self.connections = 0
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            try:
                conn.sendall(self.banner)
            except OSError:
                pass
            finally:
                conn.close()

    def stop(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


def free_port() -> int:
    """A port number with (almost certainly) nothing listening on it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_arm(**kwargs):
    defaults = dict(
        host="127.0.0.1",
        timeout=2.0,
        retries=3,
        retry_delay=0.01,
        wait=2.0,
    )
    defaults.update(kwargs)
    return asyncio.run(arm.arm(**defaults))


class ArmHappyPathTests(unittest.TestCase):
    def test_sc_path_is_exactly_documented(self):
        # Guard the constant directly and via a real request below.
        self.assertEqual(arm.SC_PATH, "/gp/gpCamera?cmd=SC&p=%02")

    def test_200_accepts_and_sees_ssh_banner(self):
        http = SCServer([200])
        ssh = BannerServer()
        self.addCleanup(http.stop)
        self.addCleanup(ssh.stop)

        result = run_arm(http_port=http.port, ssh_port=ssh.port)

        self.assertEqual(result.http_status, 200)
        self.assertTrue(result.accepted)
        self.assertTrue(result.ssh_up)
        self.assertTrue(result.banner.startswith("SSH-"))
        self.assertEqual(http.paths, ["/gp/gpCamera?cmd=SC&p=%02"])

    def test_no_extra_http_request_on_success(self):
        http = SCServer([200])
        ssh = BannerServer()
        self.addCleanup(http.stop)
        self.addCleanup(ssh.stop)

        run_arm(http_port=http.port, ssh_port=ssh.port)

        self.assertEqual(http.request_count, 1)

    def test_accepted_but_ssh_never_comes_up(self):
        http = SCServer([200])
        dead_ssh = free_port()
        self.addCleanup(http.stop)

        result = run_arm(http_port=http.port, ssh_port=dead_ssh, wait=0.3)

        self.assertEqual(result.http_status, 200)
        self.assertTrue(result.accepted)
        self.assertFalse(result.ssh_up)
        self.assertEqual(http.request_count, 1)


class ArmRetryTests(unittest.TestCase):
    def test_500_then_200_retries_once(self):
        http = SCServer([500, 200])
        ssh = BannerServer()
        self.addCleanup(http.stop)
        self.addCleanup(ssh.stop)

        result = run_arm(http_port=http.port, ssh_port=ssh.port, retry_delay=0.01)

        self.assertTrue(result.accepted)
        self.assertEqual(result.http_status, 200)
        self.assertTrue(result.ssh_up)
        self.assertEqual(http.request_count, 2)
        self.assertEqual(http.paths, ["/gp/gpCamera?cmd=SC&p=%02"] * 2)

    def test_500_every_time_is_not_accepted(self):
        http = SCServer([500])
        dead_ssh = free_port()
        self.addCleanup(http.stop)

        result = run_arm(http_port=http.port, ssh_port=dead_ssh, retries=3, retry_delay=0.01)

        self.assertFalse(result.accepted)
        self.assertEqual(result.http_status, 500)
        self.assertEqual(http.request_count, 3)


class ArmFailureTests(unittest.TestCase):
    def test_connection_refused_is_not_accepted(self):
        dead_http = free_port()
        dead_ssh = free_port()

        result = run_arm(http_port=dead_http, ssh_port=dead_ssh)

        self.assertFalse(result.accepted)
        self.assertIsNone(result.http_status)
        self.assertFalse(result.ssh_up)
        self.assertIn("not accepted", result.message.lower())

    def test_http_timeout_is_not_accepted(self):
        # The handler sleeps far longer than the client timeout, so the GET times out.
        http = SCServer([200], delay=1.0)
        dead_ssh = free_port()
        self.addCleanup(http.stop)

        result = run_arm(http_port=http.port, ssh_port=dead_ssh, timeout=0.2, retries=1)

        self.assertFalse(result.accepted)
        self.assertIsNone(result.http_status)
        self.assertFalse(result.ssh_up)


class BannerProbeTests(unittest.TestCase):
    def test_ssh_banner_reads_identification_string(self):
        ssh = BannerServer()
        self.addCleanup(ssh.stop)
        up, banner = arm.ssh_banner("127.0.0.1", ssh.port, timeout=1.0)
        self.assertTrue(up)
        self.assertTrue(banner.startswith("SSH-"))

    def test_ssh_banner_false_when_nothing_listening(self):
        up, banner = arm.ssh_banner("127.0.0.1", free_port(), timeout=0.5)
        self.assertFalse(up)
        self.assertEqual(banner, "")


if __name__ == "__main__":
    unittest.main()
