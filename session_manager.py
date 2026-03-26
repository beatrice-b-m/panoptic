from __future__ import annotations

"""Session manager: tracks tmux sessions across multiple hosts and owns ttyd lifecycle.

Hosts are either local (direct tmux subprocess) or remote (tmux over SSH).
The port pool and ttyd processes always run locally — for remote hosts, ttyd
execs ``ssh <alias> tmux -u attach-session -t <name>`` instead of a direct
tmux attach.
"""

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

from config import RuntimeSettings
from host_config import HostConfig
from template_macros import contains_placeholders

log = logging.getLogger(__name__)


# Thumbnail snapshot: cached captured-pane text per session.
SNAPSHOT_FRESHNESS_SECS = 30
SNAPSHOT_MAX_LINES = 24
SNAPSHOT_MAX_COLS = 80

# Strip ANSI escape sequences (CSI, OSC, and simple ESC sequences).
_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-9;?]*[A-Za-z]|\].*?(?:\x07|\x1b\\)|[()][AB012]|[>=<78HMDE])"
)
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class HostStatus:
    """Runtime connectivity/health state for a host (not persisted)."""

    status: str = "unknown"  # "ok" | "unreachable" | "auth_error" | "error" | "unknown"
    message: str = ""
    last_ok: float = 0.0  # monotonic timestamp of last successful poll


@dataclass
class SessionInfo:
    host_id: str
    name: str
    windows: int
    attached: bool
    created_epoch: int
    port: int | None = None
    ttyd_pid: int | None = None
    # The live asyncio Process handle — not part of the serialisable API surface.
    _process: asyncio.subprocess.Process | None = field(
        default=None, repr=False, compare=False
    )


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class SessionManager:
    def __init__(self, host_config: HostConfig, settings: RuntimeSettings) -> None:
        self._host_config = host_config
        self._settings = settings

        # Port pool: deque gives O(1) allocate/release. Shared across all hosts.
        self._port_pool: deque[int] = deque(
            range(settings.ttyd_port_start, settings.ttyd_port_end + 1)
        )
        self._allocated_ports: set[int] = set()

        # Per-host session registries: host_id -> {session_name -> SessionInfo}
        self._host_sessions: dict[str, dict[str, SessionInfo]] = {}

        # Per-host runtime status
        self._host_status: dict[str, HostStatus] = {}

        # Per-host snapshot caches: host_id -> {session_name -> (text, ts)}
        self._snapshot_cache: dict[str, dict[str, tuple[str, float]]] = {}

        self._poll_task: asyncio.Task | None = None

        # Resolve local binaries to absolute paths once so ttyd's child process
        # does not depend on PATH propagation (which fails under launchd).
        self._ttyd_path = shutil.which(settings.ttyd_binary) or settings.ttyd_binary
        self._tmux_path = shutil.which(settings.tmux_binary) or settings.tmux_binary
        self._ssh_path = shutil.which("ssh") or "ssh"
        if self._ttyd_path == settings.ttyd_binary:
            log.warning(
                "Could not resolve ttyd to absolute path; using %r", settings.ttyd_binary
            )
        if self._tmux_path == settings.tmux_binary:
            log.warning(
                "Could not resolve tmux to absolute path; using %r", settings.tmux_binary
            )

        # PID file tracking ttyd processes spawned by this instance.
        # Lives next to the source files so it persists across restarts.
        _project_dir = os.path.dirname(os.path.abspath(__file__))
        self._ttyd_pid_file: str = os.path.join(_project_dir, '.ttyd.pids')

        # Initialise per-host structures for all configured hosts.
        self._sync_host_structures()

    # --------------------------------------------------------- host structures

    def _sync_host_structures(self) -> None:
        """Ensure per-host dicts exist for every configured host."""
        for host in self._host_config.list_hosts():
            hid = host["id"]
            self._host_sessions.setdefault(hid, {})
            self._host_status.setdefault(hid, HostStatus())
            self._snapshot_cache.setdefault(hid, {})

    def reload_hosts(self) -> None:
        """Re-sync after host config changes (add/remove)."""
        self._sync_host_structures()

    def sessions_for_host(self, host_id: str) -> dict[str, SessionInfo]:
        """Return the session dict for a host (empty dict if unknown)."""
        return self._host_sessions.get(host_id, {})

    def get_host_statuses(self) -> dict[str, dict]:
        """Return runtime status for every tracked host."""
        return {
            hid: {
                "status": hs.status,
                "message": hs.message,
                "last_ok": hs.last_ok,
            }
            for hid, hs in self._host_status.items()
        }

    def total_session_count(self) -> int:
        """Return the total number of tracked sessions across all hosts."""
        return sum(len(sessions) for sessions in self._host_sessions.values())

    async def remove_host_sessions(self, host_id: str) -> None:
        """Kill all ttyd processes for a host and clear its session registry."""
        for name in list(self.sessions_for_host(host_id)):
            await self._kill_ttyd(host_id, name)

    async def kill_stale_ttyd(self) -> None:
        """Kill orphaned ttyd processes left over from a previous server run.

        Public entry point for server startup.  Delegates to the private
        implementation.
        """
        await self._kill_stale_ttyd()

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

    # ---------------------------------------------------------- tmux commands

    async def _run_tmux(
        self, host_id: str, *args: str
    ) -> tuple[int, str, str]:
        """Run a tmux subcommand on a host.  Returns (returncode, stdout, stderr).

        Local hosts use the resolved tmux binary path directly.
        SSH hosts run through ``ssh -o BatchMode=yes`` with a connect timeout.
        """
        host = self._host_config.get_host(host_id)
        if host is None:
            return 1, "", ""

        if host["type"] == "local":
            proc = await asyncio.create_subprocess_exec(
                self._tmux_path,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        else:
            proc = await asyncio.create_subprocess_exec(
                self._ssh_path,
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={self._settings.ssh_connect_timeout}",
                host["ssh_alias"],
                "tmux", *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self._settings.ssh_connect_timeout + 15
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return 1, "", "timeout"

        return (
            proc.returncode,
            stdout.decode(errors="replace").strip(),
            stderr.decode(errors="replace").strip(),
        )

    # ---------------------------------------------------------- tmux polling

    async def poll_sessions(self) -> None:
        """Reconcile session registries for all enabled hosts.

        Polls hosts concurrently (bounded to 4 at a time) to prevent
        one slow/unreachable host from blocking updates for all others.
        Never raises — callers depend on this contract.
        """
        self._sync_host_structures()
        hosts = [
            h for h in self._host_config.list_hosts()
            if h.get("enabled", True)
        ]
        if not hosts:
            return

        sem = asyncio.Semaphore(4)  # Bound concurrent SSH sessions.
        poll_start = time.monotonic()

        async def _poll_one(host: dict) -> None:
            async with sem:
                try:
                    await self._poll_host_sessions(host["id"])
                except Exception:
                    log.exception("Unexpected error polling host %s", host["id"])
                    hs = self._host_status.get(host["id"])
                    if hs:
                        hs.status = "error"
                        hs.message = "Unexpected polling error"

        await asyncio.gather(*[_poll_one(h) for h in hosts])

        elapsed = time.monotonic() - poll_start
        if elapsed > 2.0:
            log.debug(
                "Poll cycle completed in %.1fs across %d host(s)",
                elapsed, len(hosts),
            )

    async def _poll_host_sessions(self, host_id: str) -> None:
        """Reconcile the session registry for a single host."""
        host = self._host_config.get_host(host_id)
        if host is None:
            return

        host_sessions = self._host_sessions.setdefault(host_id, {})
        hs = self._host_status.setdefault(host_id, HostStatus())

        fmt = "#{session_name}|#{session_windows}|#{session_attached}|#{session_created}"

        returncode, stdout_text, stderr_text = await self._run_tmux(
            host_id, "list-sessions", "-F", fmt,
        )

        # _run_tmux returns "timeout" as stderr on SSH timeout.
        if stderr_text == "timeout":
            hs.status = "unreachable"
            hs.message = "SSH connection timed out"
            log.warning("SSH timeout polling host %s", host_id)
            return

        if returncode != 0:
            if host["type"] == "ssh" and returncode == 255:
                # SSH connection-level failure — classify error, keep stale data.
                if "permission denied" in stderr_text.lower():
                    hs.status = "auth_error"
                    hs.message = "SSH authentication failed"
                else:
                    hs.status = "unreachable"
                    hs.message = stderr_text[:200] or "SSH connection failed"
                log.warning(
                    "SSH error polling host %s (rc=%d): %s",
                    host_id, returncode, stderr_text[:200],
                )
                return  # preserve stale session data for display

            # For everything else (local tmux gone, or remote tmux exited
            # non-255), the host is reachable but has no sessions.
            hs.status = "ok"
            hs.message = ""
            hs.last_ok = time.monotonic()

            if host_sessions:
                log.info(
                    "tmux unavailable on host %s (rc=%d); clearing sessions",
                    host_id, returncode,
                )
                for name in list(host_sessions):
                    await self._kill_ttyd(host_id, name)
            return

        # Success — host is reachable and tmux returned sessions.
        hs.status = "ok"
        hs.message = ""
        hs.last_ok = time.monotonic()

        live: dict[str, dict] = {}
        for line in stdout_text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) != 4:
                log.warning("Unexpected tmux output from host %s: %r", host_id, line)
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
                log.warning(
                    "Could not parse tmux line from host %s: %r", host_id, line
                )

        current = set(host_sessions)
        incoming = set(live)

        # Sessions that disappeared.
        for gone in current - incoming:
            await self._kill_ttyd(host_id, gone)

        # Sessions that are new.
        for new in incoming - current:
            info = live[new]
            host_sessions[new] = SessionInfo(
                host_id=host_id,
                name=new,
                windows=info["windows"],
                attached=info["attached"],
                created_epoch=info["created_epoch"],
            )
            await self._spawn_ttyd(host_id, new)

        # Existing sessions: update mutable fields and detect dead ttyd.
        for name in current & incoming:
            info = live[name]
            sess = host_sessions[name]
            sess.windows = info["windows"]
            sess.attached = info["attached"]
            sess.created_epoch = info["created_epoch"]
            # Respawn ttyd if it died between polls.
            if sess._process is not None and sess._process.returncode is not None:
                log.info(
                    "ttyd for %s/%s exited (rc=%d); respawning",
                    host_id, name, sess._process.returncode,
                )
                if sess.port is not None:
                    self.release_port(sess.port)
                    sess.port = None
                sess.ttyd_pid = None
                sess._process = None
                await self._spawn_ttyd(host_id, name)

    # ------------------------------------------------------- pane discovery

    async def get_panes(self, host_id: str, session_name: str) -> list[dict]:
        """Return pane metadata for a session on a host.

        The caller (server endpoint) is responsible for constructing ttyd_url
        using the returned port and the inbound request host.
        """
        rc, stdout, _ = await self._run_tmux(host_id,
        "list-panes", "-t", session_name,
        "-F",
        "#{pane_id}|#{pane_index}|#{pane_width}|#{pane_height}|#{pane_active}|#{pane_title}",)

        if rc != 0:
            log.warning(
                "tmux list-panes failed for %s/%s", host_id, session_name
            )
            return []

        host_sessions = self._host_sessions.get(host_id, {})
        sess = host_sessions.get(session_name)
        port = sess.port if sess else None

        panes: list[dict] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 5)
            if len(parts) != 6:
                log.warning(
                    "Unexpected pane line for %s/%s: %r",
                    host_id, session_name, line,
                )
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
                log.warning(
                    "Could not parse pane line for %s/%s: %r",
                    host_id, session_name, line,
                )

        return panes

    # ------------------------------------------------------- ttyd lifecycle

    async def _wait_for_port_ready(self, port: int, timeout: float = 3.0) -> bool:
        """Block until *port* accepts a TCP connection, or *timeout* expires."""
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

    async def _spawn_ttyd(self, host_id: str, session_name: str) -> None:
        """Allocate a port and start a ttyd process for the given session."""
        port = self.allocate_port()
        if port is None:
            log.warning(
                "Port pool exhausted; %s/%s will not have a ttyd instance",
                host_id, session_name,
            )
            return

        safe_host = urlquote(host_id, safe="")
        safe_name = urlquote(session_name, safe="")
        base_path = f"/terminal/{safe_host}/{safe_name}/"

        host = self._host_config.get_host(host_id)
        if host is None:
            self.release_port(port)
            return

        # Build the attach command that ttyd will exec.
        # Local:  <tmux_path> -u attach-session -t <name>
        # SSH:    <ssh_path> <alias> tmux -u attach-session -t <name>
        if host["type"] == "local":
            attach_cmd = [
                self._tmux_path, "-u", "attach-session", "-t", session_name,
            ]
        else:
            attach_cmd = [
                self._ssh_path, host["ssh_alias"],
                "tmux", "-u", "attach-session", "-t", session_name,
            ]

        cmd = [
            self._ttyd_path,
            "--port", str(port),
            "--interface", self._settings.ttyd_bind_host,
            "--writable",
            "--base-path", base_path,
            "-t", f"fontFamily={self._settings.ttyd_font_family}",
            *attach_cmd,
        ]

        # Ensure the child process sees a UTF-8 locale.
        env = os.environ.copy()
        env.setdefault("LANG", "en_US.UTF-8")
        env.setdefault("LC_ALL", "en_US.UTF-8")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            log.error(
                "ttyd binary not found (%r); %s/%s will have no terminal",
                self._ttyd_path, host_id, session_name,
            )
            self.release_port(port)
            return
        except Exception:
            log.exception(
                "Failed to spawn ttyd for %s/%s on port %d",
                host_id, session_name, port,
            )
            self.release_port(port)
            return

        host_sessions = self._host_sessions.get(host_id, {})
        sess = host_sessions.get(session_name)
        if sess is None:
            # Session removed between port allocation and spawn — clean up.
            process.kill()
            self.release_port(port)
            return

        sess.port = port
        sess.ttyd_pid = process.pid
        sess._process = process
        self._record_ttyd_pid(process.pid)
        log.info(
            "Spawned ttyd for %s/%s on port %d (pid=%d)",
            host_id, session_name, port, process.pid,
        )

    async def _kill_ttyd(self, host_id: str, session_name: str) -> None:
        """Terminate the ttyd process for a session and reclaim its port.

        Removes the session from the host registry unconditionally.
        """
        host_sessions = self._host_sessions.get(host_id)
        if host_sessions is None:
            return

        sess = host_sessions.pop(session_name, None)

        # Clean snapshot cache for this session.
        host_cache = self._snapshot_cache.get(host_id)
        if host_cache:
            host_cache.pop(session_name, None)

        if sess is None:
            return

        if sess.port is not None:
            self.release_port(sess.port)

        proc = sess._process
        if proc is None or proc.returncode is not None:
            # Already dead — still clean up PID tracking.
            if sess.ttyd_pid is not None:
                self._remove_ttyd_pid(sess.ttyd_pid)
            return

        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning(
                "ttyd for %s/%s did not exit after SIGTERM; sending SIGKILL",
                host_id, session_name,
            )
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

        log.info(
            "Killed ttyd for %s/%s (pid=%s)", host_id, session_name, sess.ttyd_pid
        )
        if sess.ttyd_pid is not None:
            self._remove_ttyd_pid(sess.ttyd_pid)

    def _read_pid_file(self) -> set[int]:
        """Read recorded ttyd PIDs from the PID file."""
        try:
            with open(self._ttyd_pid_file) as f:
                return {int(line.strip()) for line in f if line.strip().isdigit()}
        except FileNotFoundError:
            return set()

    def _write_pid_file(self, pids: set[int]) -> None:
        """Write the current set of ttyd PIDs to the PID file."""
        os.makedirs(os.path.dirname(self._ttyd_pid_file), exist_ok=True)
        with open(self._ttyd_pid_file, 'w') as f:
            for pid in sorted(pids):
                f.write(f'{pid}\n')

    def _record_ttyd_pid(self, pid: int) -> None:
        """Add a PID to the PID file."""
        pids = self._read_pid_file()
        pids.add(pid)
        self._write_pid_file(pids)

    def _remove_ttyd_pid(self, pid: int) -> None:
        """Remove a PID from the PID file."""
        pids = self._read_pid_file()
        pids.discard(pid)
        self._write_pid_file(pids)

    async def _kill_stale_ttyd(self) -> None:
        """Kill ttyd processes recorded by a previous server run."""
        stale_pids = self._read_pid_file()
        if not stale_pids:
            return

        killed = False
        for pid in stale_pids:
            # Verify the PID is still a ttyd process (protects against PID recycling).
            try:
                proc = await asyncio.create_subprocess_exec(
                    'ps', '-p', str(pid), '-o', 'comm=',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await proc.communicate()
                if 'ttyd' not in stdout.decode().strip():
                    continue
            except Exception:
                continue

            try:
                os.kill(pid, signal.SIGTERM)
                killed = True
            except ProcessLookupError:
                pass

        # Clear the file regardless — stale PIDs are either killed or gone.
        self._write_pid_file(set())

        if killed:
            log.info('Killed stale ttyd processes from a previous run')
            await asyncio.sleep(0.5)

    # ------------------------------------------------------- layout helpers

    async def _apply_row_layout(
        self, host_id: str, session_name: str, counts: list[int]
    ) -> bool:
        """Split rows first, then columns per row."""
        rc, pane_id, _ = await self._run_tmux(host_id,
        "display-message", "-p", "-t", f"{session_name}:0.0", "#{pane_id}",)
        if rc != 0:
            return False

        n_rows = len(counts)
        row_anchors: list[str] = [pane_id]

        for _ in range(1, n_rows):
            rc, new_pane, _ = await self._run_tmux(host_id,
            "split-window", "-v", "-t", f"{session_name}:0",
            "-P", "-F", "#{pane_id}",)
            if rc != 0:
                return False
            row_anchors.append(new_pane)

        rc, _, _ = await self._run_tmux(host_id, "select-layout", "-t", f"{session_name}:0", "even-vertical")
        if rc != 0:
            return False

        for r, anchor in enumerate(row_anchors):
            for _ in range(counts[r] - 1):
                rc, _, _ = await self._run_tmux(host_id, "split-window", "-h", "-t", anchor)
                if rc != 0:
                    return False

        await self._run_tmux(
            host_id, "select-pane", "-t", f"{session_name}:0.0"
        )
        return True

    async def _apply_col_layout(
        self, host_id: str, session_name: str, counts: list[int]
    ) -> bool:
        """Mirror of _apply_row_layout with columns as the primary split axis."""
        rc, pane_id, _ = await self._run_tmux(host_id,
        "display-message", "-p", "-t", f"{session_name}:0.0", "#{pane_id}",)
        if rc != 0:
            return False

        n_cols = len(counts)
        col_anchors: list[str] = [pane_id]

        for _ in range(1, n_cols):
            rc, new_pane, _ = await self._run_tmux(host_id,
            "split-window", "-h", "-t", f"{session_name}:0",
            "-P", "-F", "#{pane_id}",)
            if rc != 0:
                return False
            col_anchors.append(new_pane)

        rc, _, _ = await self._run_tmux(host_id, "select-layout", "-t", f"{session_name}:0", "even-horizontal")
        if rc != 0:
            return False

        for c, anchor in enumerate(col_anchors):
            for _ in range(counts[c] - 1):
                rc, _, _ = await self._run_tmux(host_id, "split-window", "-v", "-t", anchor)
                if rc != 0:
                    return False

        await self._run_tmux(
            host_id, "select-pane", "-t", f"{session_name}:0.0"
        )
        return True

    @staticmethod
    def _parse_layout_spec(spec: str) -> tuple[list[int], list[str]] | None:
        """Parse a Beamux-compatible layout spec.

        NOTE: static/app.js:parseLayoutSpec() mirrors this grammar for
        client-side preview.  Keep both in sync when changing the spec format.

        Segments are colon-separated.  Each segment is either:
          - a positive integer  (e.g. '2')  → that many panes, no default command
          - comma-separated commands (e.g. 'npm run dev,jest')  → one pane per command

        Returns ``(counts, commands)`` where *counts* has one int per segment
        (total panes in that segment) and *commands* is a flat list of default
        commands in pane order (empty string when no command was specified).

        Returns None when the spec is empty or fundamentally unparseable.
        """
        if not spec or not spec.strip():
            return None

        segments = spec.split(':')
        counts: list[int] = []
        commands: list[str] = []

        for seg in segments:
            seg = seg.strip()
            if not seg:
                return None  # empty segment (e.g. '2::3')

            # Try pure integer first.
            try:
                n = int(seg)
                if n < 1:
                    return None
                counts.append(n)
                commands.extend([''] * n)
                continue
            except ValueError:
                pass

            # Command segment: comma-separated command strings, one pane each.
            cmds = [c.strip() for c in seg.split(',')]
            if not cmds or any(c == '' for c in cmds):
                return None  # empty command in segment (e.g. 'vim,,ls')
            counts.append(len(cmds))
            commands.extend(cmds)

        if not counts:
            return None
        return counts, commands

    @staticmethod
    def _merge_pane_commands(
        spec_commands: list[str],
        overlay_commands: list[str] | None,
        total_panes: int,
    ) -> list[str]:
        """Merge layout-spec default commands with explicit overlay commands.

        For each pane index, the overlay command takes precedence if non-empty;
        otherwise the spec-embedded command (if any) is used.
        """
        result: list[str] = []
        overlay = overlay_commands or []
        for i in range(total_panes):
            ov = overlay[i] if i < len(overlay) else ''
            sp = spec_commands[i] if i < len(spec_commands) else ''
            result.append(ov if ov else sp)
        return result

    async def _send_pane_commands(
        self, host_id: str, session_name: str, commands: list[str],
    ) -> None:
        """Send shell commands to panes via ``tmux send-keys``.

        Panes are addressed by index within window 0 (the default window
        created by new-session + split operations).
        """
        for i, cmd in enumerate(commands):
            if not cmd:
                continue
            rc, _, _ = await self._run_tmux(
                host_id, "send-keys", "-t", f"{session_name}:0.{i}", cmd, "Enter"
            )
            if rc != 0:
                log.warning(
                    "send-keys failed for %s/%s pane %d: cmd=%r",
                    host_id, session_name, i, cmd,
                )


    # ------------------------------------------------------- session CRUD

    async def create_session(
        self,
        host_id: str,
        name: str,
        cwd: str | None = None,
        layout_type: str | None = None,
        layout_spec: str | None = None,
        pane_commands: list[str] | None = None,
        *,
        _from_template: bool = False,
    ) -> dict:
        """Create a new tmux session on a host.

        When *_from_template* is False (direct create), brace characters in any
        text field are rejected — macro placeholders are template-only.

        *pane_commands* is an optional list of shell commands, one per pane.
        If shorter than the total pane count, remaining panes get no command.
        An entry of ``''`` means "no command for this pane".

        Returns session info dict on success, or ``{"error": "..."}`` on failure.
        """
        # --- macro guard for direct (non-template) create ---
        # Validate pane_commands element types first — before any iteration.
        if pane_commands is not None:
            if not isinstance(pane_commands, list):
                return {"error": "'pane_commands' must be a list"}
            for i, cmd in enumerate(pane_commands):
                if not isinstance(cmd, str):
                    return {
                        "error": f"pane_commands[{i}] must be a string, got {type(cmd).__name__}"
                    }

        if not _from_template:
            for field_name, value in [("name", name), ("cwd", cwd or ""), ("layout_spec", layout_spec or "")]:
                if contains_placeholders(value):
                    return {
                        "error": (
                            f"Macro placeholders are only allowed in templates. "
                            f"Field '{field_name}' contains '{{' or '}}'. "
                            f"Save as a template first."
                        )
                    }
            if pane_commands:
                for i, cmd in enumerate(pane_commands):
                    if contains_placeholders(cmd):
                        return {
                            "error": (
                                f"Macro placeholders are only allowed in templates. "
                                f"Pane command {i} contains '{{' or '}}'. "
                                f"Save as a template first."
                            )
                        }

        if not SESSION_NAME_RE.match(name):
            return {
                "error": f"Invalid session name {name!r}: must match ^[A-Za-z0-9_-]+$"
            }

        host = self._host_config.get_host(host_id)
        if host is None:
            return {"error": f"Unknown host: {host_id}"}

        host_sessions = self._host_sessions.get(host_id, {})
        if name in host_sessions:
            return {"error": f"Session {name!r} already exists"}

        # Validate cwd — local paths are checked on disk; remote paths are
        # passed through to the remote tmux (no local validation possible).
        if cwd is not None and host["type"] == "local":
            cwd = os.path.expanduser(cwd)
            if not os.path.isdir(cwd):
                return {"error": f"cwd {cwd!r} is not a directory"}

        counts: list[int] | None = None
        spec_commands: list[str] = []
        if layout_type is not None:
            if layout_type not in ("row", "col"):
                return {
                    "error": f"Invalid layout_type {layout_type!r}: must be 'row' or 'col'"
                }
            if not layout_spec:
                return {
                    "error": "layout_spec is required when layout_type is provided"
                }
            parsed = self._parse_layout_spec(layout_spec)
            if parsed is None:
                return {
                    "error": f"Invalid layout_spec {layout_spec!r}: use colon-separated integers or command segments (e.g. '2:1' or 'vim,ls:3')"
                }
            counts, spec_commands = parsed

        tmux_args = ["new-session", "-d", "-s", name]
        if cwd is not None:
            tmux_args += ["-c", cwd]

        rc, output, _ = await self._run_tmux(host_id, *tmux_args)
        if rc != 0:
            return {"error": f"tmux new-session failed: {output}"}

        if layout_type is not None and counts is not None:
            ok = await (
                self._apply_row_layout(host_id, name, counts)
                if layout_type == "row"
                else self._apply_col_layout(host_id, name, counts)
            )
            if not ok:
                log.warning("Layout application failed for %s/%s", host_id, name)

        # --- dispatch pane commands ---
        total_panes = sum(counts) if counts else 1
        effective_commands = self._merge_pane_commands(
            spec_commands, pane_commands, total_panes
        )
        if any(effective_commands):
            await self._send_pane_commands(host_id, name, effective_commands)

        await self._poll_host_sessions(host_id)

        sess = self._host_sessions.get(host_id, {}).get(name)
        if sess is None:
            return {
                "error": f"Session {name!r} was created but not found after polling"
            }

        # Block until ttyd is actually accepting connections.
        if sess.port is not None:
            ready = await self._wait_for_port_ready(sess.port)
            if not ready:
                log.warning("ttyd for %s/%s not ready within timeout", host_id, name)

        safe_host = urlquote(host_id, safe="")
        safe_name = urlquote(name, safe="")
        return {
            "name": sess.name,
            "host_id": host_id,
            "windows": sess.windows,
            "attached": sess.attached,
            "created_epoch": sess.created_epoch,
            "ttyd_url": f"/terminal/{safe_host}/{safe_name}/",
        }

    async def delete_session(self, host_id: str, name: str) -> dict:
        """Kill a tmux session on a host and release its ttyd process/port."""
        host_sessions = self._host_sessions.get(host_id, {})
        if name not in host_sessions:
            return {"error": f"Session '{name}' not found"}

        attached = host_sessions[name].attached

        rc, output, _ = await self._run_tmux(host_id, "kill-session", "-t", name)
        if rc != 0:
            return {"error": f"tmux kill-session failed: {output}"}

        await self._kill_ttyd(host_id, name)

        return {
            "name": name,
            "host_id": host_id,
            "deleted": True,
            "was_attached": attached,
        }

    def list_directories(self, prefix: str, limit: int = 50) -> list[str]:
        """List directories matching prefix for path autocompletion.

        Only meaningful for localhost — remote path completion is not supported.
        Restricted to the user's home directory to prevent filesystem enumeration.
        """
        home = Path.home()
        if not prefix:
            prefix = str(home) + "/"
        prefix = os.path.expanduser(prefix)

        # Resolve to catch traversal via '..' components.
        try:
            resolved = Path(prefix).resolve()
        except (OSError, ValueError):
            return []

        # Ancestry check: resolved must be home itself or a descendant of home.
        # String prefix matching is unsafe (e.g. /home/bee vs /home/beekeeper).
        if resolved != home and home not in resolved.parents:
            return []

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
        """Kill all managed ttyd processes.  Call on server shutdown."""
        await self.stop_polling()
        for host_id in list(self._host_sessions):
            for name in list(self._host_sessions[host_id]):
                await self._kill_ttyd(host_id, name)
        self._write_pid_file(set())

    # --------------------------------------------------------- polling loop

    async def start_polling(
        self,
        get_last_activity: Callable[[], float],
        get_wake_event: Callable[[], asyncio.Event | None] | None = None,
    ) -> None:
        """Run the reconciliation loop with three-tier idle management.

        Active:     poll every ``poll_interval_active`` seconds.
        Idle:       poll every ``poll_interval_idle`` seconds.
        Deep idle:  skip polling entirely; sleep at idle interval.

        The tier is determined by the elapsed time since the last client
        HTTP request (provided by *get_last_activity*).

        An optional *get_wake_event* callback supplies an ``asyncio.Event``
        that is set when a request arrives, allowing the loop to cut short
        a deep-idle sleep and poll immediately.
        """
        _prev_state: str | None = None

        async def _interruptible_sleep(seconds: float) -> None:
            """Sleep for *seconds*, but return early if the wake event fires."""
            evt = get_wake_event() if get_wake_event else None
            if evt is None:
                await asyncio.sleep(seconds)
                return
            evt.clear()
            try:
                await asyncio.wait_for(evt.wait(), timeout=seconds)
            except asyncio.TimeoutError:
                pass

        async def _loop() -> None:
            nonlocal _prev_state
            while True:
                now = time.monotonic()
                idle_secs = now - get_last_activity()

                if idle_secs < self._settings.client_active_timeout:
                    state = "active"
                    await self.poll_sessions()
                    interval = self._settings.poll_interval_active
                elif idle_secs < self._settings.client_deep_idle_timeout:
                    state = "idle"
                    await self.poll_sessions()
                    interval = self._settings.poll_interval_idle
                else:
                    state = "deep_idle"
                    # Skip polling — nobody is looking.
                    interval = self._settings.poll_interval_idle

                if state != _prev_state:
                    if _prev_state is not None:
                        log.info("Polling state: %s -> %s", _prev_state, state)
                    _prev_state = state

                await _interruptible_sleep(interval)

                # If we were in deep idle and got woken, do an immediate poll
                # before the next loop iteration determines state.
                if state == "deep_idle":
                    evt = get_wake_event() if get_wake_event else None
                    if evt and evt.is_set():
                        log.info("Waking from deep idle — client activity detected")
                        await self.poll_sessions()

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

    def get_sessions(
        self, host_id: str, page: int = 1, page_size: int | None = None
    ) -> dict:
        """Return a paginated session list for a specific host."""
        if page_size is None:
            page_size = self._settings.session_page_size
        page_size = max(1, page_size)  # Defensive: prevent ZeroDivisionError
        host_sessions = self._host_sessions.get(host_id, {})
        all_sessions = sorted(host_sessions.values(), key=lambda s: s.name)
        total = len(all_sessions)
        pages = math.ceil(total / page_size) if total else 1

        page = max(1, min(page, pages))
        start = (page - 1) * page_size
        slice_ = all_sessions[start : start + page_size]

        return {
            "sessions": [
                {
                    "name": s.name,
                    "host_id": s.host_id,
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

    async def get_thumbnail_svg(
        self, host_id: str, session_name: str
    ) -> str | None:
        """Return an SVG thumbnail for the session, or None if unknown."""
        host_sessions = self._host_sessions.get(host_id, {})
        if session_name not in host_sessions:
            return None

        host_cache = self._snapshot_cache.setdefault(host_id, {})
        cached = host_cache.get(session_name)
        if cached is not None:
            text, ts = cached
            if time.monotonic() - ts < SNAPSHOT_FRESHNESS_SECS:
                return self._render_svg(text)

        text = await self._capture_pane(host_id, session_name)
        if text is None:
            # Capture failed — return stale cache if available, else fallback.
            if cached is not None:
                return self._render_svg(cached[0])
            return self._render_svg("(no snapshot available)")

        host_cache[session_name] = (text, time.monotonic())
        return self._render_svg(text)

    async def _capture_pane(
        self, host_id: str, session_name: str
    ) -> str | None:
        """Run tmux capture-pane and return cleaned text, or None on failure."""
        rc, raw, _ = await self._run_tmux(host_id, "capture-pane", "-p", "-t", session_name)
        if rc != 0:
            return None

        # Strip ANSI escape sequences.
        cleaned = _ANSI_RE.sub("", raw)
        # Truncate to max dimensions for a compact thumbnail.
        lines = cleaned.splitlines()[:SNAPSHOT_MAX_LINES]
        truncated = [line[:SNAPSHOT_MAX_COLS] for line in lines]
        return "\n".join(truncated)

    @staticmethod
    def _render_svg(text: str) -> str:
        """Produce a dark-bg monospace SVG from plain text."""
        lines = text.splitlines()[:SNAPSHOT_MAX_LINES]
        # Pad to exactly SNAPSHOT_MAX_LINES so every thumbnail has
        # identical dimensions regardless of pane content length.
        while len(lines) < SNAPSHOT_MAX_LINES:
            lines.append("")

        char_w = 7.2  # approximate width of a monospace char at 12px
        char_h = 16  # line height
        pad_x = 10
        pad_y = 10
        width = int(SNAPSHOT_MAX_COLS * char_w + 2 * pad_x)
        height = SNAPSHOT_MAX_LINES * char_h + 2 * pad_y

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
            f'<g font-family="\'Hack Nerd Font\', \'Hack Nerd Font Mono\','
            f' Menlo, Consolas, monospace"'
            f' font-size="12" fill="#c8c8d0">'
            f'{body}'
            f'</g></svg>'
        )
