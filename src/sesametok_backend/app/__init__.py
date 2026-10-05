"""Menu bar application layer.

``app.status`` is pure logic and imports anywhere, including headless CI.
``app.menubar`` is the PyObjC view layer and requires AppKit on macOS.
"""

from __future__ import annotations
