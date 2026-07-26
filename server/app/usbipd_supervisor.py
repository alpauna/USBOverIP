"""Runs usbipd (the userspace USB/IP export daemon) as a supervised child
process of the FastAPI app so the whole server ships as one container
process without needing a separate init system.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess

logger = logging.getLogger("usbip.usbipd")


class UsbipdSupervisor:
    def __init__(self, port: int = 3240):
        self.port = port
        self._proc: subprocess.Popen | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _spawn(self) -> None:
        self._proc = subprocess.Popen(
            ["usbipd", "-t", str(self.port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        logger.info("usbipd started (pid=%s, port=%s)", self._proc.pid, self.port)

    async def start(self) -> None:
        self._stopping = False
        self._spawn()
        self._task = asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        while not self._stopping:
            await asyncio.sleep(5)
            if self._stopping:
                return
            if not self.is_running():
                logger.warning("usbipd exited unexpectedly, restarting")
                self._spawn()

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
