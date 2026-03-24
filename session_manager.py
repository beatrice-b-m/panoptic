from __future__ import annotations

"""Session manager: tracks tmux sessions and owns ttyd subprocess lifecycle."""

import asyncio
import logging
import math
import shutil
import signal
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from config import (
    POLL_INTERVAL_ACTIVE,
    POLL_INTERVAL_IDLE,
    SESSION_PAGE_SIZE,
    TMUX_BINARY,
    TTYD_BIND_HOST,
    TTYD_BINARY,
    TTYD_PORT_RANGE_END,
    TTYD_PORT_RANGE_START,
)

log = logging.getLogger(__name__)


@dataclass
class SessionInfo:
    name: str
    windows: int
    attached: bool
    created_epoch: int
    port: int | None = None
    ttyd_pid: int | None = None
    # The live asyncio Process handle — not part of the serialisable API surface.
    _process: asyncio.subprocess.Process | None = field(default=None, repr=False, compare=False)


class SessionManager:
    def __init__(self) -> None:
        # Port pool: deque gives O(1) allocate/release.
        self._port_pool: deque[int] = deque(
            range(TTYD_PORT_RANGE_START, TTYD_PORT_RANGE_END + 1)
        )
        self._allocated_ports: set[int] = set()

        self.sessions: dict[str, SessionInfo] = {}

        self._poll_task: asyncio.Task | None = None

        # Resolve binaries to absolute paths once so ttyd's child process
        # does not depend on PATH propagation (which fails under launchd).
        self._ttyd_path = shutil.which(TTYD_BINARY) or TTYD_BINARY
        self._tmux_path = shutil.which(TMUX_BINARY) or TMUX_BINARY
        if self._ttyd_path == TTYD_BINARY:
            log.warning("Could not resolve ttyd to absolute path; using %r", TTYD_BINARY)
        if self._tmux_path == TMUX_BINARY:
            log.warning("Could not resolve tmux to absolute path; using %r", TMUX_BINARY)

    # ------------------------------------------------------------------ ports

    def allocate_port(self) -> int | None:
        if not self._port_pool:
            return None
        port = self._port_pool.popleft()
        self._allocated_ports.add(port)
        return port

    def release_port(self, port: int) -> None:
        if port in self._allocated_ports:
            self._allocated_ports.discard(port)
            self._port_pool.append(port)

    # ---------------------------------------------------------- tmux polling

    async def poll_sessions(self) -> None:
        """Reconcile the session registry against live tmux output.

        Never raises — callers depend on this contract to keep the poll loop
        alive across transient failures.
        """
        try:
            await self._poll_sessions_inner()
        except Exception:
            log.exception("Unexpected error during poll_sessions")

    async def _poll_sessions_inner(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            self._tmux_path,
            "list-sessions",
            "-F",
            "#{session_name}|#{session_windows}|#{session_attached}|#{session_created}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            # tmux not running or no sessions — tear everything down.
            if self.sessions:
                log.info("tmux unavailable (rc=%d); clearing all sessions", proc.returncode)
                for name in list(self.sessions):
                    await self._kill_ttyd(name)
            return

        live: dict[str, dict] = {}
        for line in stdout.decode().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) != 4:
                log.warning("Unexpected tmux output line: %r", line)
                continue
            name, windows_s, attached_s, created_s = parts
            try:
                live[name] = {
                    "name": name,
                    "windows": int(windows_s),
                    "attached": attached_s != "0",
                    "created_epoch": int(created_s),
                }
            except ValueError:
                log.warning("Could not parse tmux line: %r", line)

        current = set(self.sessions)
        incoming = set(live)

        # Sessions that disappeared.
        for gone in current - incoming:
            await self._kill_ttyd(gone)

        # Sessions that are new.
        for new in incoming - current:
            info = live[new]
            self.sessions[new] = SessionInfo(
                name=new,
                windows=info["windows"],
                attached=info["attached"],
                created_epoch=info["created_epoch"],
            )
            await self._spawn_ttyd(new)

        # Existing sessions: update mutable fields and detect dead ttyd.
        for name in current & incoming:
            info = live[name]
            sess = self.sessions[name]
            sess.windows = info["windows"]
            sess.attached = info["attached"]
            sess.created_epoch = info["created_epoch"]
            # Respawn ttyd if it died between polls.
            if sess._process is not None and sess._process.returncode is not None:
                log.info("ttyd for session %r exited (rc=%d); respawning", name, sess._process.returncode)
                if sess.port is not None:
                    self.release_port(sess.port)
                    sess.port = None
                sess.ttyd_pid = None
                sess._process = None
                await self._spawn_ttyd(name)

    # ------------------------------------------------------- pane discovery

    async def get_panes(self, session_name: str) -> list[dict]:
        """Return pane metadata for a session.

        The caller (server endpoint) is responsible for constructing ttyd_url
        using the returned port and the inbound request host.
        """
        proc = await asyncio.create_subprocess_exec(
            self._tmux_path,
            "list-panes",
            "-t", session_name,
            "-F", "#{pane_id}|#{pane_index}|#{pane_width}|#{pane_height}|#{pane_active}|#{pane_title}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            log.warning("tmux list-panes failed for %r: %s", session_name, stderr.decode().strip())
            return []

        sess = self.sessions.get(session_name)
        port = sess.port if sess else None

        panes: list[dict] = []
        for line in stdout.decode().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 5)
            if len(parts) != 6:
                log.warning("Unexpected pane line for %r: %r", session_name, line)
                continue
            pane_id, index_s, width_s, height_s, active_s, title = parts
            try:
                panes.append({
                    "id": pane_id,
                    "index": int(index_s),
                    "width": int(width_s),
                    "height": int(height_s),
                    "active": active_s != "0",
                    "title": title,
                    "port": port,
                })
            except ValueError:
                log.warning("Could not parse pane line for %r: %r", session_name, line)

        return panes

    # ------------------------------------------------------- ttyd lifecycle

    async def _spawn_ttyd(self, session_name: str) -> None:
        """Allocate a port and start a ttyd process for the given session."""
        port = self.allocate_port()
        if port is None:
            log.warning(
                "Port pool exhausted; session %r will not have a ttyd instance", session_name
            )
            return

        cmd = [
            self._ttyd_path,
            "--port", str(port),
            "--interface", TTYD_BIND_HOST,
            "--writable",
            self._tmux_path, "attach-session", "-t", session_name,
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            log.error("ttyd binary not found (%r); session %r will have no terminal", self._ttyd_path, session_name)
            self.release_port(port)
            return
        except Exception:
            log.exception("Failed to spawn ttyd for session %r on port %d", session_name, port)
            self.release_port(port)
            return

        sess = self.sessions[session_name]
        sess.port = port
        sess.ttyd_pid = process.pid
        sess._process = process
        log.info("Spawned ttyd for session %r on port %d (pid=%d)", session_name, port, process.pid)

    async def _kill_ttyd(self, session_name: str) -> None:
        """Terminate the ttyd process for a session and reclaim its port.

        Removes the session from the registry unconditionally — the caller
        should not reference session_name after this returns.
        """
        sess = self.sessions.pop(session_name, None)
        if sess is None:
            return

        if sess.port is not None:
            self.release_port(sess.port)

        proc = sess._process
        if proc is None or proc.returncode is not None:
            # Already dead.
            return

        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("ttyd for session %r did not exit after SIGTERM; sending SIGKILL", session_name)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

        log.info("Killed ttyd for session %r (pid=%s)", session_name, sess.ttyd_pid)

    # --------------------------------------------------------- cleanup hook

    async def cleanup(self) -> None:
        """Kill all managed ttyd processes. Call on server shutdown."""
        await self.stop_polling()
        for name in list(self.sessions):
            await self._kill_ttyd(name)

    # --------------------------------------------------------- polling loop

    async def start_polling(self, get_client_count: Callable[[], int]) -> None:
        """Run the reconciliation loop until cancelled."""
        async def _loop() -> None:
            while True:
                await self.poll_sessions()
                interval = (
                    POLL_INTERVAL_ACTIVE if get_client_count() > 0 else POLL_INTERVAL_IDLE
                )
                await asyncio.sleep(interval)

        self._poll_task = asyncio.create_task(_loop(), name="session-poll")
        try:
            await self._poll_task
        except asyncio.CancelledError:
            pass

    async def stop_polling(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    # -------------------------------------------------- session list for API

    def get_sessions(self, page: int = 1, page_size: int = SESSION_PAGE_SIZE) -> dict:
        """Return a paginated session list matching the API contract."""
        all_sessions = sorted(self.sessions.values(), key=lambda s: s.name)
        total = len(all_sessions)
        pages = math.ceil(total / page_size) if total else 1

        # Clamp page to valid range so callers always get a coherent response.
        page = max(1, min(page, pages))
        start = (page - 1) * page_size
        slice_ = all_sessions[start : start + page_size]

        return {
            "sessions": [
                {
                    "name": s.name,
                    "windows": s.windows,
                    "attached": s.attached,
                    "created_epoch": s.created_epoch,
                    "port": s.port,
                }
                for s in slice_
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }
