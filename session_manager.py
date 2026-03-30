from __future__ import annotations

"""Session manager: tracks tmux sessions across multiple hosts.

Hosts are either local (direct tmux subprocess) or remote (tmux over SSH).
"""

import asyncio
import html as html_mod
import os
from pathlib import Path
import logging
import math
import re
import time
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class SessionManager:
    def __init__(self, host_config: HostConfig, settings: RuntimeSettings) -> None:
        self._host_config = host_config
        self._settings = settings


        # Per-host session registries: host_id -> {session_name -> SessionInfo}
        self._host_sessions: dict[str, dict[str, SessionInfo]] = {}

        # Per-host runtime status
        self._host_status: dict[str, HostStatus] = {}

        # Per-host snapshot caches: host_id -> {session_name -> (text, ts, svg)}
        # svg is pre-rendered by _render_svg; fresh cache hits skip re-rendering.
        self._snapshot_cache: dict[str, dict[str, tuple[str, float, str]]] = {}

        # Per-host sorted session views; None means dirty (re-sort on next read).
        # Invalidated only when session set changes — not on field updates.
        self._sorted_sessions_cache: dict[str, list | None] = {}
        self._poll_task: asyncio.Task | None = None

        # Resolve local binaries to absolute paths once so child processes do not
        # depend on PATH propagation (which can fail under launchd).
        def _resolve_binary(binary: str) -> str:
            if os.path.sep in binary:
                return binary
            for path_entry in os.get_exec_path():
                candidate = os.path.join(path_entry, binary)
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    return candidate
            return binary

        self._tmux_path = _resolve_binary(settings.tmux_binary)
        self._ssh_path = _resolve_binary("ssh")
        if self._tmux_path == settings.tmux_binary:
            log.warning(
                "Could not resolve tmux to absolute path; using %r", settings.tmux_binary
            )

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
            self._sorted_sessions_cache.setdefault(hid, None)

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
        self._host_sessions.pop(host_id, None)
        self._snapshot_cache.pop(host_id, None)
        self._sorted_sessions_cache.pop(host_id, None)

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
                host_sessions.clear()
                self._sorted_sessions_cache[host_id] = None
                host_cache = self._snapshot_cache.get(host_id)
                if host_cache:
                    host_cache.clear()
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
            host_sessions.pop(gone, None)
            host_cache = self._snapshot_cache.get(host_id)
            if host_cache:
                host_cache.pop(gone, None)
        if current - incoming:
            self._sorted_sessions_cache[host_id] = None

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
        if incoming - current:
            self._sorted_sessions_cache[host_id] = None

        # Existing sessions: update mutable fields.
        for name in current & incoming:
            info = live[name]
            sess = host_sessions[name]
            sess.windows = info["windows"]
            sess.attached = info["attached"]
            sess.created_epoch = info["created_epoch"]

    # ------------------------------------------------------- pane discovery

    async def get_panes(self, host_id: str, session_name: str) -> list[dict]:
        """Return pane metadata for a session on a host."""
        rc, stdout, _ = await self._run_tmux(host_id,
        "list-panes", "-t", session_name,
        "-F",
        "#{pane_id}|#{pane_index}|#{pane_width}|#{pane_height}|#{pane_active}|#{pane_title}",)

        if rc != 0:
            log.warning(
                "tmux list-panes failed for %s/%s", host_id, session_name
            )
            return []

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
                })
            except ValueError:
                log.warning(
                    "Could not parse pane line for %s/%s: %r",
                    host_id, session_name, line,
                )

        return panes

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

        safe_host = urlquote(host_id, safe="")
        safe_name = urlquote(name, safe="")
        return {
            "name": sess.name,
            "host_id": host_id,
            "windows": sess.windows,
            "attached": sess.attached,
            "created_epoch": sess.created_epoch,
            "ws_url": f"/ws/hosts/{safe_host}/sessions/{safe_name}",
        }

    async def delete_session(self, host_id: str, name: str) -> dict:
        """Kill a tmux session on a host."""
        host_sessions = self._host_sessions.get(host_id, {})
        if name not in host_sessions:
            return {"error": f"Session '{name}' not found"}

        attached = host_sessions[name].attached

        rc, output, _ = await self._run_tmux(host_id, "kill-session", "-t", name)
        if rc != 0:
            return {"error": f"tmux kill-session failed: {output}"}

        host_sessions.pop(name, None)
        host_cache = self._snapshot_cache.get(host_id)
        if host_cache:
            host_cache.pop(name, None)
        self._sorted_sessions_cache[host_id] = None

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

        names: list[str] = []
        try:
            # os.scandir yields lighter DirEntry objects than pathlib.iterdir.
            # Filtering before sorting means we only sort the matching subset,
            # which is typically much smaller than the full directory.
            with os.scandir(parent) as it:
                for de in it:
                    if de.is_symlink():
                        continue
                    if not de.is_dir(follow_symlinks=False):
                        continue
                    if partial and not de.name.lower().startswith(partial.lower()):
                        continue
                    names.append(de.name)
        except PermissionError:
            return []

        names.sort()
        return [str(parent / name) + "/" for name in names[:limit]]

    # --------------------------------------------------------- cleanup hook

    async def cleanup(self) -> None:
        """Stop background polling.  Call on server shutdown."""
        await self.stop_polling()

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
        # Use memoized sorted view; only re-sort when the session set changes.
        sorted_cache = self._sorted_sessions_cache.get(host_id)
        if sorted_cache is None:
            all_sessions = sorted(host_sessions.values(), key=lambda s: s.name)
            self._sorted_sessions_cache[host_id] = all_sessions
        else:
            all_sessions = sorted_cache
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
            text, ts, svg = cached
            if time.monotonic() - ts < SNAPSHOT_FRESHNESS_SECS:
                log.debug("Thumbnail cache hit for %s/%s", host_id, session_name)
                return svg  # pre-rendered; no rebuild needed

        text = await self._capture_pane(host_id, session_name)
        if text is None:
            # Capture failed — return stale cached SVG if available, else fallback.
            if cached is not None:
                return cached[2]  # stale svg
            return self._render_svg("(no snapshot available)")

        log.debug("Thumbnail rendered for %s/%s", host_id, session_name)
        svg = self._render_svg(text)
        host_cache[session_name] = (text, time.monotonic(), svg)
        return svg

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
