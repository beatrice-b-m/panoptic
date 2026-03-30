"""control_bridge.py — tmux control mode bridge for per-pane terminal rendering.

Manages a single ``tmux -CC attach -t <session>`` subprocess, parses the
control mode protocol, and exposes structured events via an async queue.

One ControlBridge exists per active WebSocket session view.  The server
creates it on WS connect and stops it on disconnect.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import AsyncIterator

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol parsing helpers
# ---------------------------------------------------------------------------

# Matches a 3-digit octal escape: \012  \033  \134  etc.
_OCTAL_RE = re.compile(rb"\\([0-7]{3})")


def unescape_output(s: str) -> bytes:
    """Convert a tmux control mode ``%output`` payload to raw bytes.

    tmux escapes every byte < 32 and the backslash character itself as a
    3-digit octal sequence (e.g. ``\\012`` for newline, ``\\134`` for ``\\``).
    Everything else is passed through as UTF-8.
    """
    raw = s.encode("utf-8", errors="replace")
    return _OCTAL_RE.sub(lambda m: bytes([int(m.group(1), 8)]), raw)


@dataclass(frozen=True, slots=True)
class PaneGeometry:
    """Leaf node from a parsed tmux layout string."""
    pane_id: str   # e.g. "%0"
    cols: int
    rows: int
    x: int         # left offset in characters
    y: int         # top offset in characters


def parse_layout(layout_str: str) -> list[PaneGeometry]:
    """Parse a tmux layout string into a flat list of pane geometries.

    Layout strings look like::

        5f2d,220x50,0,0[220x25,0,0,%0,220x24,0,26,%1]

    The leading 4-hex-char checksum is stripped.  Nested ``[...]`` containers
    are recursed into; leaf nodes carry ``%N`` pane IDs.
    """
    s = re.sub(r"^[0-9a-f]{4},", "", layout_str.strip())
    panes: list[PaneGeometry] = []
    _parse_node(s, panes)
    return panes


# Regex for the "WxH,X,Y" prefix of every layout node.
_GEOM_RE = re.compile(r"(\d+)x(\d+),(\d+),(\d+)")
# Regex for a pane-id leaf: "%N"
_PANE_ID_RE = re.compile(r"%(\d+)")


def _parse_node(s: str, out: list[PaneGeometry]) -> int:
    """Recursively parse one layout node.  Returns characters consumed."""
    m = _GEOM_RE.match(s)
    if not m:
        raise ValueError(f"Cannot parse layout node: {s!r}")
    cols, rows, x, y = int(m[1]), int(m[2]), int(m[3]), int(m[4])
    pos = m.end()

    if pos < len(s) and s[pos] in ("[", "{"):
        # Container node directly after geometry: WxH,X,Y[...] or WxH,X,Y{...}
        close_char = "]" if s[pos] == "[" else "}"
        pos += 1  # skip open bracket
        while pos < len(s) and s[pos] != close_char:
            consumed = _parse_node(s[pos:], out)
            pos += consumed
            if pos < len(s) and s[pos] == ",":
                pos += 1
        if pos < len(s) and s[pos] == close_char:
            pos += 1
    elif pos < len(s) and s[pos] == ",":
        pos += 1
        if pos < len(s) and s[pos] == "%":
            # Leaf node: %pane_id
            m2 = _PANE_ID_RE.match(s, pos)
            if not m2:
                raise ValueError(f"Expected pane id at: {s[pos:]!r}")
            pane_id = f"%{m2[1]}"
            out.append(PaneGeometry(pane_id=pane_id, cols=cols, rows=rows, x=x, y=y))
            pos = m2.end()
        elif pos < len(s) and s[pos] in ("[", "{"):
            # Container node after comma: WxH,X,Y,[...] or WxH,X,Y,{...}
            close_char = "]" if s[pos] == "[" else "}"
            pos += 1  # skip open bracket
            while pos < len(s) and s[pos] != close_char:
                consumed = _parse_node(s[pos:], out)
                pos += consumed
                if pos < len(s) and s[pos] == ",":
                    pos += 1
            if pos < len(s) and s[pos] == close_char:
                pos += 1
    return pos


# ---------------------------------------------------------------------------
# Protocol line classifier
# ---------------------------------------------------------------------------

def parse_control_line(line: str) -> dict | None:
    """Parse one tmux control mode notification line into an event dict.

    Returns ``None`` for lines that are not actionable notifications
    (command response bookends, unrecognised lines).
    """
    if not line.startswith("%"):
        return None

    parts = line.split(" ", 2)
    notification = parts[0]

    if notification == "%output" and len(parts) >= 3:
        pane_id = parts[1]
        raw_payload = parts[2]
        return {
            "type": "output",
            "pane_id": pane_id,
            "data": unescape_output(raw_payload),
        }

    if notification == "%layout-change" and len(parts) >= 3:
        window_id = parts[1]
        layout_str = parts[2]
        try:
            panes = parse_layout(layout_str)
        except Exception:
            log.warning("Failed to parse layout: %r", layout_str)
            return None
        return {
            "type": "layout",
            "window_id": window_id,
            "panes": [
                {
                    "pane_id": p.pane_id,
                    "cols": p.cols,
                    "rows": p.rows,
                    "x": p.x,
                    "y": p.y,
                }
                for p in panes
            ],
        }

    if notification in ("%window-add", "%window-close", "%window-renamed"):
        window_id = parts[1] if len(parts) > 1 else ""
        return {"type": notification[1:].replace("-", "_"), "window_id": window_id}

    if notification == "%session-window-changed" and len(parts) >= 3:
        return {"type": "session_window_changed", "window_id": parts[2]}

    if notification == "%pane-mode-changed":
        pane_id = parts[1] if len(parts) > 1 else ""
        return {"type": "pane_mode_changed", "pane_id": pane_id}

    # Command response bookends — not forwarded.
    if notification in ("%begin", "%end", "%error"):
        return None

    return None


# ---------------------------------------------------------------------------
# ControlBridge — subprocess lifecycle + event stream
# ---------------------------------------------------------------------------


class ControlBridge:
    """Manages a single ``tmux -CC attach -t <session>`` subprocess.

    Parses control mode protocol lines and emits structured events.
    Accepts commands to send to tmux stdin.

    Usage::

        bridge = ControlBridge("my-session", 220, 50, tmux_path="/opt/homebrew/bin/tmux")
        await bridge.start()
        async for event in bridge.events():
            if event["type"] == "output":
                # event["pane_id"], event["data"] (bytes)
                ...
            elif event["type"] == "layout":
                # event["window_id"], event["panes"] (list of dicts)
                ...
            elif event["type"] == "exit":
                break
        await bridge.stop()
    """

    def __init__(
        self,
        session_name: str,
        cols: int,
        rows: int,
        tmux_path: str = "tmux",
        ssh_alias: str | None = None,
        ssh_connect_timeout: int = 5,
    ) -> None:
        self.session_name = session_name
        self.cols = cols
        self.rows = rows
        self.tmux_path = tmux_path
        self.ssh_alias = ssh_alias
        self.ssh_connect_timeout = ssh_connect_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._event_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Spawn the tmux -CC subprocess and begin reading stdout."""
        if self.ssh_alias:
            cmd = [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={self.ssh_connect_timeout}",
                self.ssh_alias,
                "tmux", "-CC", "attach", "-t", self.session_name,
            ]
        else:
            cmd = [self.tmux_path, "-CC", "attach", "-t", self.session_name]

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"bridge-reader-{self.session_name}"
        )
        # Negotiate client size immediately so tmux renders at our dimensions.
        await self.resize(self.cols, self.rows)

    async def _read_loop(self) -> None:
        """Read stdout line by line and push parsed events to the queue."""
        assert self._process and self._process.stdout
        try:
            async for raw_line in self._process.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                event = parse_control_line(line)
                if event:
                    await self._event_queue.put(event)
        except Exception as exc:
            log.exception("ControlBridge reader error: %s", exc)
        finally:
            await self._event_queue.put({"type": "exit"})

    async def events(self) -> AsyncIterator[dict]:
        """Yield events until the subprocess exits."""
        while True:
            event = await self._event_queue.get()
            yield event
            if event["type"] == "exit":
                return

    # ---- commands sent to tmux stdin ----

    async def _send_command(self, cmd: str) -> None:
        """Write a single command line to tmux stdin."""
        if self._process and self._process.stdin and not self._process.stdin.is_closing():
            self._process.stdin.write((cmd + "\n").encode())
            await self._process.stdin.drain()

    async def send_keys(self, pane_id: str, data: bytes) -> None:
        """Forward raw bytes from the browser to a specific pane via hex encoding."""
        hex_bytes = " ".join(f"{b:02x}" for b in data)
        await self._send_command(f"send-keys -t {pane_id} -H {hex_bytes}")

    async def select_pane(self, pane_id: str) -> None:
        """Focus a pane (browser-side click)."""
        await self._send_command(f"select-pane -t {pane_id}")

    async def resize(self, cols: int, rows: int) -> None:
        """Set the control client size (sent on connect and browser resize)."""
        self.cols = cols
        self.rows = rows
        await self._send_command(f"refresh-client -C {cols},{rows}")

    async def stop(self) -> None:
        """Terminate the subprocess and cancel the reader task."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
