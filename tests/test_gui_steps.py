"""The GUI E2E deletes clips from the real camera, so its "only what this run recorded" rules are tested
on their own: each case here is a way the run could otherwise have deleted someone's footage."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from quak.e2e import gui_steps as steps

CARD = [f"GX01{i:04}.MP4" for i in range(1, 58)]  # 57 files that aren't this run's
OURS = "GX010100.MP4"


class FakeUI:
    """The Media screen as the steps see it: tiles, the "N items" summary, the delete dialog."""

    def __init__(self, files, *, connected=True, hidden=0, lost_tap=False):
        self.files, self.connected, self.hidden, self.lost_tap = list(files), connected, hidden, lost_tap
        self.selected = self.files[0] if self.files else None  # the desktop detail panel starts on the first file
        self.dialog = None
        self.deleted = []

    async def labels(self):
        labels = ["Media", "Select"]
        if self.files:  # like the real screen, no summary on an empty card
            labels.append(f"{len(self.files)} items · 1 GB")
        labels += [f"{f} {f} Video" for f in self.files[self.hidden :]]
        if self.connected:
            labels.append("Camera Wi-Fi connected")
        if not self.files:
            labels.append("No media on the card.")
        if self.dialog:
            labels.append(f"Delete {self.dialog} from the camera?")
        if self.deleted:
            labels.append("Deleted")
        return labels

    async def has(self, text):
        return any(text in label for label in await self.labels())

    async def wait_text(self, text, timeout=15):
        assert await self.has(text), f"{text!r} never appeared"

    async def click(self, label, exact=False, timeout=10, settle=0.8):
        if label == "Delete":
            if self.dialog:
                self.files.remove(self.dialog)
                self.deleted.append(self.dialog)
                self.dialog = None
            else:
                self.dialog = self.selected
        elif label in self.files and not self.lost_tap:
            self.selected = label


def run_with(baseline):
    return SimpleNamespace(state={"baseline": baseline})


class GuiCleanupSafetyTests(unittest.TestCase):
    def setUp(self):
        clock = iter(range(0, 100000, 10))
        for target, new in (("asyncio.sleep", mock.AsyncMock()), ("time.monotonic", lambda: next(clock))):
            patcher = mock.patch(f"quak.e2e.gui_steps.{target}", new)
            patcher.start()
            self.addCleanup(patcher.stop)

    def clean(self, ui, baseline):
        return asyncio.run(steps.g11_cleanup(ui, run_with(baseline)))

    def test_no_baseline_deletes_nothing(self):
        ui = FakeUI(CARD)
        with self.assertRaisesRegex(AssertionError, "no media baseline"):
            self.clean(ui, None)
        self.assertEqual(ui.deleted, [])

    def test_too_many_new_files_deletes_nothing(self):
        ui = FakeUI(CARD)
        with self.assertRaisesRegex(AssertionError, "refusing to delete"):
            self.clean(ui, CARD[:50])
        self.assertEqual(ui.deleted, [])

    def test_lost_tap_deletes_nothing(self):
        ui = FakeUI(CARD + [OURS], lost_tap=True)  # the panel stays on a file that isn't ours
        with self.assertRaisesRegex(AssertionError, "never appeared"):
            self.clean(ui, CARD)
        self.assertEqual(ui.deleted, [])

    def test_deletes_only_what_this_run_recorded(self):
        ui = FakeUI(CARD + [OURS])
        ui.selected = CARD[0]
        self.clean(ui, CARD)
        self.assertEqual(ui.deleted, [OURS])
        self.assertEqual(ui.files, CARD)

    def test_failed_baseline_stays_unset_when_the_list_is_partial(self):
        ui, run = FakeUI(CARD, hidden=5), run_with(None)
        with self.assertRaisesRegex(AssertionError, "says 57 items, 52 names"):
            asyncio.run(steps.g03_media_baseline(ui, run))
        self.assertIsNone(run.state["baseline"])

    def test_failed_baseline_stays_unset_when_wifi_never_connects(self):
        ui, run = FakeUI(CARD, connected=False), run_with(None)
        with self.assertRaisesRegex(AssertionError, "never connected"):
            asyncio.run(steps.g03_media_baseline(ui, run))
        self.assertIsNone(run.state["baseline"])

    def test_empty_list_only_counts_once_the_screen_says_the_card_is_empty(self):
        ui = FakeUI([])
        self.assertEqual(asyncio.run(steps.read_card(ui)), set())
        ui.has = mock.AsyncMock(return_value=False)  # still loading: no files and no answer yet
        with self.assertRaisesRegex(AssertionError, "couldn't read the whole card"):
            asyncio.run(steps.read_card(ui))

    def test_complete_card_gives_the_baseline(self):
        ui, run = FakeUI(CARD), run_with(None)
        asyncio.run(steps.g03_media_baseline(ui, run))
        self.assertEqual(run.state["baseline"], sorted(CARD))


if __name__ == "__main__":
    unittest.main()
