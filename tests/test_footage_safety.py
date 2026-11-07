"""Ways Quak could lose someone's footage or leave their camera in a surprising state."""

import asyncio
import http.server
import tempfile
import threading
import unittest
from pathlib import Path

from quak import flows
from quak.connect import Target, open_camera
from quak.core.errors import NetworkError
from quak.core.registry import Status
from quak.net.http import CameraHttp, free_path, is_plain_name
from quak.transport.sim import SimCamera


def serve(handler_body):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            handler_body(self)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def api(self, body):
        server, url = serve(body)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return CameraHttp(url)

    def test_a_cut_off_download_is_an_error_and_leaves_nothing_behind(self):
        def truncated(h):
            h.send_response(200)
            h.send_header("Content-Length", str(5_000_000))
            h.end_headers()
            h.wfile.write(b"x" * 1_000_000)

        with self.assertRaisesRegex(NetworkError, "1000000 of 5000000"):
            self.api(truncated).download("100GOPRO/GX010001.MP4", self.dir / "GX010001.MP4")
        self.assertEqual(list(self.dir.iterdir()), [])

    def complete(self, h):
        h.send_response(200)
        h.send_header("Content-Length", "4")
        h.end_headers()
        h.wfile.write(b"data")

    def test_a_complete_download_gets_its_name(self):
        size, _, where = self.api(self.complete).download("100GOPRO/GX010001.MP4", self.dir / "GX010001.MP4")
        self.assertEqual((size, where.read_bytes()), (4, b"data"))
        self.assertEqual(where.name, "GX010001.MP4")

    def test_a_download_never_replaces_an_existing_file(self):
        old = self.dir / "GX010001.MP4"
        old.write_bytes(b"an earlier clip from another card")
        _, _, where = self.api(self.complete).download("100GOPRO/GX010001.MP4", old)
        self.assertEqual(where.name, "GX010001_1.MP4")
        self.assertEqual(old.read_bytes(), b"an earlier clip from another card")
        self.assertEqual(free_path(old).name, "GX010001_2.MP4")

    def test_files_named_to_escape_the_download_folder_are_ignored(self):
        def listing(h):
            body = (
                b'{"media":[{"d":"100GOPRO","fs":[{"n":"GX010001.MP4"},{"n":"../../evil.sh"},{"n":"/etc/passwd"},'
                b'{"n":"..\\\\evil.exe"},{"n":"C:evil"}]},{"d":"..","fs":[{"n":"a.MP4"}]}]}'
            )
            h.send_response(200)
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)

        self.assertEqual([f.name for f in self.api(listing).media_list()], ["GX010001.MP4"])

    def test_plain_names(self):
        for good in ("GX010001.MP4", "100GOPRO", "a b.jpg"):
            self.assertTrue(is_plain_name(good), good)
        for bad in ("", ".", "..", "a/b", "a\\b", "C:x", "a\0b"):
            self.assertFalse(is_plain_name(bad), repr(bad))


class CameraStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_interrupted_recording_stops_the_camera(self):
        cam_sim = SimCamera(latency=0.0005)
        async with open_camera(Target(sim=True, sim_camera=cam_sim, keepalive=None)) as cam:
            task = asyncio.create_task(flows.record_clip(cam, 60))
            await cam.wait_status(Status.ENCODING, lambda v: v == 1, 5, poll=0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(await cam.wait_status(Status.ENCODING, lambda v: v == 0, 5, poll=0.02), 0)

    async def test_arming_says_what_stays_changed(self):
        import datetime as dt

        cam_sim = SimCamera(latency=0.0005)
        async with open_camera(Target(sim=True, sim_camera=cam_sim, keepalive=None)) as cam:
            when = dt.datetime.now() + dt.timedelta(minutes=30)
            r = await flows.arm_schedule(cam, when, duration="15s")
            joined = " ".join(r.warnings)
            self.assertIn("Pro controls", joined)
            self.assertIn("Video Duration is now 15 Seconds", joined)
            self.assertIn("also limits recordings", joined)
            again = await flows.arm_schedule(cam, when, duration="15s")  # nothing newly changed: nothing to say
            self.assertNotIn("Video Duration", " ".join(again.warnings))
            await flows.disarm_schedule(cam)


if __name__ == "__main__":
    unittest.main()
