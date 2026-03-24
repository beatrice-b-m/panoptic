from __future__ import annotations

"""Session manager: tracks tmux sessions and owns ttyd subprocess lifecycle."""

import asyncio
import html as html_mod
import os
from pathlib import Path
import logging
import math
import re
import shutil
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote as urlquote

from config import (
    POLL_INTERVAL_ACTIVE,
    POLL_INTERVAL_IDLE,
    SESSION_PAGE_SIZE,
    TMUX_BINARY,
    BEAMUX_BINARY,
    TTYD_BIND_HOST,
    TTYD_BINARY,
    TTYD_FONT_FAMILY,
    TTYD_PORT_RANGE_END,
    TTYD_PORT_RANGE_START,
)

log = logging.getLogger(__name__)



# Thumbnail snapshot: cached captured-pane text per session.
SNAPSHOT_FRESHNESS_SECS = 30
SNAPSHOT_MAX_LINES = 24
SNAPSHOT_MAX_COLS = 80

# Strip ANSI escape sequences (CSI, OSC, and simple ESC sequences).
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[A-Za-z]|\].*?(?:\x07|\x1b\\)|[()][AB012]|[>=<78HMDE])")
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
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

        # Thumbnail snapshot cache: session_name -> (text, timestamp).
        self._snapshot_cache: dict[str, tuple[str, float]] = {}

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

    async def _wait_for_port_ready(self, port: int, timeout: float = 3.0) -> bool:
        """Block until *port* accepts a TCP connection, or *timeout* expires.

        Used after spawning ttyd so callers (e.g. create_session) don't
        return a ttyd_url that isn't listening yet.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port),
                    timeout=0.5,
                )
                writer.close()
                await writer.wait_closed()
                return True
            except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
                await asyncio.sleep(0.1)
        return False

    async def _spawn_ttyd(self, session_name: str) -> None:
        """Allocate a port and start a ttyd process for the given session."""
        port = self.allocate_port()
        if port is None:
            log.warning(
                "Port pool exhausted; session %r will not have a ttyd instance", session_name
            )
            return

        base_path = f"/terminal/{urlquote(session_name, safe='')}/"
        cmd = [
            self._ttyd_path,
            "--port", str(port),
            "--interface", TTYD_BIND_HOST,
            "--writable",
            "--base-path", base_path,
            "-t", f"fontFamily={TTYD_FONT_FAMILY}",
            self._tmux_path, "-u", "attach-session", "-t", session_name,
        ]

        # Ensure the child process (and tmux client) sees a UTF-8 locale
        # so that wide/Nerd-Font glyphs are transmitted correctly.
        env = os.environ.copy()
        env.setdefault("LANG", "en_US.UTF-8")
        env.setdefault("LC_ALL", "en_US.UTF-8")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
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

        self._snapshot_cache.pop(session_name, None)
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

    async def _kill_stale_ttyd(self) -> None:
        """Kill orphaned ttyd processes left over from a previous server run.

        Must be called before the first poll so freed ports are available
        in the pool.  Matches only ttyd processes that were spawned with
        `tmux attach-session` (our unique command-line signature).
        """
        try:
            # Find orphaned ttyd processes from a prior run.
            proc = await asyncio.create_subprocess_exec(
                "pkill", "-f", "ttyd.*tmux attach-session",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0:
                log.info("Killed stale ttyd processes from a previous run")
                # Brief pause so the OS releases the listening sockets.
                await asyncio.sleep(0.5)
        except FileNotFoundError:
            log.debug("pkill not available; skipping orphan cleanup")


    async def _run_tmux(self, *args: str) -> tuple[int, str]:
        """Run a tmux subcommand and return (returncode, stdout_stripped)."""
        proc = await asyncio.create_subprocess_exec(
            self._tmux_path, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return proc.returncode, stdout.decode().strip()

    async def _apply_row_layout(self, session_name: str, counts: list[int]) -> bool:
        """Port of beamux apply_row_layout. Split rows first, then columns per row."""
        rc, pane_id = await self._run_tmux(
            "display-message", "-p", "-t", f"{session_name}:0.0", "#{pane_id}"
        )
        if rc != 0:
            return False

        n_rows = len(counts)
        row_anchors: list[str] = [pane_id]

        # Create additional rows by splitting vertically.
        for _ in range(1, n_rows):
            rc, new_pane = await self._run_tmux(
                "split-window", "-v", "-t", f"{session_name}:0", "-P", "-F", "#{pane_id}"
            )
            if rc != 0:
                return False
            row_anchors.append(new_pane)

        rc, _ = await self._run_tmux("select-layout", "-t", f"{session_name}:0", "even-vertical")
        if rc != 0:
            return False

        # For each row, split horizontally counts[r]-1 times.
        for r, anchor in enumerate(row_anchors):
            for _ in range(counts[r] - 1):
                rc, _ = await self._run_tmux("split-window", "-h", "-t", anchor)
                if rc != 0:
                    return False

        await self._run_tmux("select-pane", "-t", f"{session_name}:0.0")
        return True

    async def _apply_col_layout(self, session_name: str, counts: list[int]) -> bool:
        """Mirror of _apply_row_layout with columns as the primary split axis."""
        rc, pane_id = await self._run_tmux(
            "display-message", "-p", "-t", f"{session_name}:0.0", "#{pane_id}"
        )
        if rc != 0:
            return False

        n_cols = len(counts)
        col_anchors: list[str] = [pane_id]

        # Create additional columns by splitting horizontally.
        for _ in range(1, n_cols):
            rc, new_pane = await self._run_tmux(
                "split-window", "-h", "-t", f"{session_name}:0", "-P", "-F", "#{pane_id}"
            )
            if rc != 0:
                return False
            col_anchors.append(new_pane)

        rc, _ = await self._run_tmux("select-layout", "-t", f"{session_name}:0", "even-horizontal")
        if rc != 0:
            return False

        # For each column, split vertically counts[c]-1 times.
        for c, anchor in enumerate(col_anchors):
            for _ in range(counts[c] - 1):
                rc, _ = await self._run_tmux("split-window", "-v", "-t", anchor)
                if rc != 0:
                    return False

        await self._run_tmux("select-pane", "-t", f"{session_name}:0.0")
        return True

    @staticmethod
    def _parse_layout_spec(spec: str) -> list[int] | None:
        """Parse colon-separated positive integers. Returns None on invalid input."""
        try:
            counts = [int(x) for x in spec.split(":")]
            if counts and all(c >= 1 for c in counts):
                return counts
        except ValueError:
            pass
        return None

    async def create_session(
        self,
        name: str,
        cwd: str | None = None,
        layout_type: str | None = None,
        layout_spec: str | None = None,
    ) -> dict:
        """Create a new tmux session. Returns session info dict on success, or {\"error\": \"...\"} on failure."""
        if not SESSION_NAME_RE.match(name):
            return {"error": f"Invalid session name {name!r}: must match ^[A-Za-z0-9_-]+$"}

        if name in self.sessions:
            return {"error": f"Session {name!r} already exists"}

        if cwd is not None:
            cwd = os.path.expanduser(cwd)
            if not os.path.isdir(cwd):
                return {"error": f"cwd {cwd!r} is not a directory"}

        counts: list[int] | None = None
        if layout_type is not None:
            if layout_type not in ("row", "col"):
                return {"error": f"Invalid layout_type {layout_type!r}: must be 'row' or 'col'"}
            if not layout_spec:
                return {"error": "layout_spec is required when layout_type is provided"}
            counts = self._parse_layout_spec(layout_spec)
            if counts is None:
                return {"error": f"Invalid layout_spec {layout_spec!r}: must be colon-separated positive integers"}

        cmd = [self._tmux_path, "new-session", "-d", "-s", name]
        if cwd is not None:
            cmd += ["-c", cwd]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return {"error": f"tmux new-session failed: {stderr.decode().strip()}"}

        if layout_type is not None and counts is not None:
            ok = await (
                self._apply_row_layout(name, counts)
                if layout_type == "row"
                else self._apply_col_layout(name, counts)
            )
            if not ok:
                log.warning("Layout application failed for session %r", name)

        await self.poll_sessions()

        sess = self.sessions.get(name)
        if sess is None:
            return {"error": f"Session {name!r} was created but not found after polling"}

        # Block until ttyd is actually accepting connections so the caller
        # can hand the ttyd_url to the frontend without a race.
        if sess.port is not None:
            ready = await self._wait_for_port_ready(sess.port)
            if not ready:
                log.warning("ttyd for session %r not ready within timeout", name)

        return {
            "name": sess.name,
            "windows": sess.windows,
            "attached": sess.attached,
            "created_epoch": sess.created_epoch,
            "ttyd_url": f"/terminal/{urlquote(name, safe='')}/",
        }

    async def delete_session(self, name: str) -> dict:
        """Kill a tmux session and release its ttyd process/port."""
        if name not in self.sessions:
            return {"error": f"Session '{name}' not found"}

        attached = self.sessions[name].attached

        returncode, stdout = await self._run_tmux("kill-session", "-t", name)
        if returncode != 0:
            return {"error": f"tmux kill-session failed: {stdout}"}

        await self._kill_ttyd(name)

        return {"name": name, "deleted": True, "was_attached": attached}

    def list_directories(self, prefix: str, limit: int = 50) -> list[str]:
        """List directories matching prefix for path autocompletion."""
        if not prefix:
            prefix = os.path.expanduser("~/")
        prefix = os.path.expanduser(prefix)

        if prefix.endswith("/"):
            parent = Path(prefix)
            partial = ""
        else:
            p = Path(prefix)
            parent = p.parent
            partial = p.name

        if not parent.is_dir():
            return []

        results: list[str] = []
        try:
            for entry in sorted(parent.iterdir()):
                if entry.is_symlink():
                    continue
                if not entry.is_dir():
                    continue
                if partial and not entry.name.lower().startswith(partial.lower()):
                    continue
                results.append(str(entry) + "/")
                if len(results) >= limit:
                    break
        except PermissionError:
            return []

        return results

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


    # --------------------------------------------------- session thumbnails

    async def get_thumbnail_svg(self, session_name: str) -> str | None:
        """Return an SVG thumbnail for the session, or None if unknown.

        Uses a cached snapshot if fresher than SNAPSHOT_FRESHNESS_SECS;
        otherwise re-captures from tmux.
        """
        if session_name not in self.sessions:
            return None

        cached = self._snapshot_cache.get(session_name)
        if cached is not None:
            text, ts = cached
            if time.monotonic() - ts < SNAPSHOT_FRESHNESS_SECS:
                return self._render_svg(text)

        text = await self._capture_pane(session_name)
        if text is None:
            # Capture failed — return stale cache if available, else a fallback.
            if cached is not None:
                return self._render_svg(cached[0])
            return self._render_svg("(no snapshot available)")

        self._snapshot_cache[session_name] = (text, time.monotonic())
        return self._render_svg(text)

    async def _capture_pane(self, session_name: str) -> str | None:
        """Run tmux capture-pane and return cleaned text, or None on failure."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._tmux_path,
                "capture-pane", "-p", "-t", session_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except (asyncio.TimeoutError, FileNotFoundError, OSError) as exc:
            log.warning("capture-pane failed for %r: %s", session_name, exc)
            return None

        if proc.returncode != 0:
            return None

        raw = stdout.decode(errors="replace")
        # Strip ANSI escape sequences.
        cleaned = _ANSI_RE.sub("", raw)
        # Truncate to max dimensions for a compact thumbnail.
        lines = cleaned.splitlines()[:SNAPSHOT_MAX_LINES]
        truncated = [line[:SNAPSHOT_MAX_COLS] for line in lines]
        return "\n".join(truncated)

    @staticmethod
    def _render_svg(text: str) -> str:
        """Produce a dark-bg monospace SVG from plain text."""
        lines = text.splitlines()
        # Pad to minimum height so empty sessions don't collapse.
        while len(lines) < 4:
            lines.append("")

        char_w = 7.2  # approximate width of a monospace char at 12px
        char_h = 16   # line height
        pad_x = 10
        pad_y = 10
        width = int(SNAPSHOT_MAX_COLS * char_w + 2 * pad_x)
        height = len(lines) * char_h + 2 * pad_y

        escaped_lines: list[str] = []
        for i, line in enumerate(lines):
            y = pad_y + (i + 1) * char_h
            safe = html_mod.escape(line) if line else "&#160;"
            escaped_lines.append(
                f'<text x="{pad_x}" y="{y}" xml:space="preserve">{safe}</text>'
            )

        body = "\n".join(escaped_lines)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg"'
            f' width="{width}" height="{height}"'
            f' viewBox="0 0 {width} {height}">'
            f'<rect width="100%" height="100%" rx="6" fill="#1a1a2e"/>'
            f'<g font-family="\'Hack Nerd Font\', \'Hack Nerd Font Mono\', Menlo, Consolas, monospace"'
            f' font-size="12" fill="#c8c8d0">'
            f'{body}'
            f'</g></svg>'
        )