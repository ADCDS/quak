"""Each screen is a function that takes the App and returns its controls."""

from quak.gui.screens.add_camera import add_camera
from quak.gui.screens.cameras import cameras
from quak.gui.screens.live import live
from quak.gui.screens.media import media
from quak.gui.screens.more import more
from quak.gui.screens.remote import remote
from quak.gui.screens.schedule import schedule
from quak.gui.screens.settings import settings

__all__ = ["cameras", "add_camera", "remote", "settings", "schedule", "media", "live", "more"]
