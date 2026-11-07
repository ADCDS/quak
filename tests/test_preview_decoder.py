"""The desktop preview's ffmpeg can exit at the start of a stream: the decoder has to notice and recover."""

import asyncio
import unittest
from unittest import mock

from quak.gui import preview
from quak.gui.preview import FrameDecoder

JPEG = b"\xff\xd8picture\xff\xd9"


class FakeFfmpeg:
    def __init__(self, output: bytes = b"", exits: bool = True) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output)
        if exits:
            self.stdout.feed_eof()
        self.returncode = None
        self.terminated = False

    async def wait(self) -> int:
        self.returncode = 234
        return 234

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


class Decoder(unittest.IsolatedAsyncioTestCase):
    async def run_with(self, *processes, on_failed=None):
        spawn = mock.AsyncMock(side_effect=list(processes))
        frames = []
        patches = (
            mock.patch("quak.gui.preview.asyncio.create_subprocess_exec", spawn),
            mock.patch.object(preview, "RETRY_SECONDS", 0),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        decoder = FrameDecoder(8554, frames.append, on_failed=on_failed)
        await decoder.start()
        return decoder, frames, spawn

    async def settle(self):
        for _ in range(50):
            await asyncio.sleep(0)

    async def test_ffmpeg_giving_up_at_the_start_is_tried_again(self):
        failed = mock.Mock()
        decoder, frames, spawn = await self.run_with(FakeFfmpeg(), FakeFfmpeg(JPEG, exits=False), on_failed=failed)
        await self.settle()
        self.assertEqual(frames, [JPEG])
        self.assertEqual(spawn.await_count, 2)
        failed.assert_not_called()
        await decoder.stop()

    async def test_reports_failure_when_ffmpeg_keeps_giving_up(self):
        failed = mock.Mock()
        await self.run_with(*[FakeFfmpeg() for _ in range(preview.MAX_TRIES)], on_failed=failed)
        await self.settle()
        failed.assert_called_once()

    async def test_a_frame_earns_a_fresh_set_of_tries(self):
        failed = mock.Mock()
        processes = [FakeFfmpeg() for _ in range(preview.MAX_TRIES - 1)]
        processes += [FakeFfmpeg(JPEG)] + [FakeFfmpeg() for _ in range(preview.MAX_TRIES - 2)]  # its own exit counts
        processes += [FakeFfmpeg(exits=False)]
        decoder, frames, _ = await self.run_with(*processes, on_failed=failed)
        await self.settle()
        self.assertEqual(frames, [JPEG])
        failed.assert_not_called()
        await decoder.stop()

    async def test_stopping_ends_the_retries(self):
        failed = mock.Mock()
        running = FakeFfmpeg(exits=False)
        decoder, _, spawn = await self.run_with(running, FakeFfmpeg(), on_failed=failed)
        await self.settle()
        await decoder.stop()
        await self.settle()
        self.assertTrue(running.terminated)
        self.assertEqual(spawn.await_count, 1)
        failed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
