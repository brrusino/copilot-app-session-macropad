# SPDX-License-Identifier: MIT
"""One logical pad link backed by more than one transport.

The remote machine can be reached from either a Windows client, where RDP
redirects the Keybow's serial port, or a Mac, where Windows App redirects its
keyboard input but not serial/COM devices. Running both transports means the
daemon does not have to be reconfigured when the client machine changes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable


class MultiLink:
    """Broadcast daemon output and accept pad input through any child link."""

    def __init__(self, links: Iterable[object]) -> None:
        self._links = tuple(links)

    @property
    def connected(self) -> bool:
        return any(bool(link.connected) for link in self._links)

    def set_on_connect(self, callback: Callable[[], None]) -> None:
        for link in self._links:
            link.set_on_connect(callback)

    def start(self) -> None:
        started = []
        try:
            for link in self._links:
                link.start()
                started.append(link)
        except Exception:
            for link in reversed(started):
                link.stop()
            raise

    def stop(self) -> None:
        for link in reversed(self._links):
            link.stop()

    def send(self, message: dict) -> bool:
        delivered = False
        for link in self._links:
            if link.send(message):
                delivered = True
        return delivered
