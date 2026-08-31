# SPDX-License-Identifier: MIT
"""Supervise the Microsoft Dev Tunnel that exposes the local bridge listener."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

from .network_link import DIAL_RETRY_DELAY

log = logging.getLogger(__name__)

# Dev Tunnels' documented maximum expiration is 30 days. Reapplying that value
# whenever the host starts keeps the persistent ID alive; if the service has
# already expired it, the supervisor recreates the same ID and port.
TUNNEL_EXPIRATION = "30d"


class DevTunnelHost:
    """Keep ``devtunnel host`` running for the daemon's lifetime."""

    def __init__(
        self,
        tunnel_id: str,
        port: int,
        log_file: Path,
        command: str = "devtunnel",
    ) -> None:
        self.tunnel_id = tunnel_id
        self.port = port
        self.log_file = Path(log_file)
        self.command = command
        self._resolved_command: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        resolved = shutil.which(self.command)
        if not resolved and Path(self.command).is_file():
            resolved = str(Path(self.command))
        if not resolved:
            raise FileNotFoundError(f"dev tunnel command not found: {self.command}")

        self._resolved_command = resolved
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_tunnel()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="macropad-devtunnel", daemon=True
        )
        self._thread.start()
        log.info("hosting dev tunnel %s", self.tunnel_id)

    def _run_cli(self, arguments: list[str]) -> int:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with self.log_file.open("ab") as output:
            result = subprocess.run(
                [self._resolved_command, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                check=False,
            )
        return result.returncode

    def _ensure_tunnel(self) -> None:
        if self._run_cli(
            ["update", self.tunnel_id, "--expiration", TUNNEL_EXPIRATION]
        ) != 0:
            # The suffix identifies the service cluster to clients, but create
            # expects the custom ID without that suffix.
            create_id = self.tunnel_id.split(".", 1)[0]
            if self._run_cli(
                [
                    "create",
                    create_id,
                    "--allow-anonymous",
                    "--expiration",
                    TUNNEL_EXPIRATION,
                    "--description",
                    "Copilot Keybow bridge",
                ]
            ) != 0:
                raise RuntimeError(f"could not create dev tunnel {create_id}")

        if self._run_cli(
            ["port", "show", self.tunnel_id, "--port-number", str(self.port)]
        ) != 0:
            if self._run_cli(
                [
                    "port",
                    "create",
                    self.tunnel_id,
                    "--port-number",
                    str(self.port),
                    "--protocol",
                    "auto",
                    "--description",
                    "Authenticated macropad bridge",
                ]
            ) != 0:
                raise RuntimeError(
                    f"could not create port {self.port} on dev tunnel {self.tunnel_id}"
                )

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        while not self._stop.is_set():
            try:
                self._ensure_tunnel()
            except (OSError, RuntimeError):
                log.exception("could not prepare dev tunnel")
                self._stop.wait(DIAL_RETRY_DELAY)
                continue
            try:
                with self.log_file.open("ab") as output:
                    process = subprocess.Popen(
                        [self._resolved_command, "host", self.tunnel_id],
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        creationflags=creationflags,
                    )
                    with self._lock:
                        self._process = process

                    while process.poll() is None and not self._stop.wait(0.5):
                        pass
                    if self._stop.is_set() and process.poll() is None:
                        process.terminate()
                    try:
                        code = process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        code = process.wait()
            except OSError:
                log.exception("could not start dev tunnel host")
                code = None
            finally:
                with self._lock:
                    self._process = None

            if not self._stop.is_set():
                log.warning(
                    "dev tunnel host exited%s; retrying",
                    "" if code is None else f" with code {code}",
                )
                self._stop.wait(DIAL_RETRY_DELAY)
