"""control_bridge.py — tmux control mode bridge for per-pane terminal rendering.

Manages a single ``tmux -CC attach -t <session>`` subprocess, parses the
control mode protocol, and exposes structured events via an async queue.

One ControlBridge exists per active WebSocket session view.  The server
creates it on WS connect and stops it on disconnect.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pty
import re
import tty
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
# Regex for a pane-id leaf: bare digits in layout strings (tmux uses plain
# numeric IDs in layout notation; the "%N" form only appears in control mode
# notifications like %output).
_PANE_ID_RE = re.compile(r"(\d+)")


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
        if pos < len(s) and (s[pos].isdigit() or s[pos] == "%"):
            # Leaf node: pane_id — bare "104" (real tmux) or "%104" (legacy).
            if s[pos] == "%":
                pos += 1  # skip the % prefix
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
        # parts[2] contains: LAYOUT [VISIBLE_LAYOUT] [FLAGS]
        # We only need the first space-delimited token (the window layout).
        layout_str = parts[2].split(" ", 1)[0]
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

    tmux 3.6+ requires a TTY on stdin for control mode (``tcgetattr``).  We
    allocate a PTY pair, pass the slave to tmux, and perform all I/O through
    the master fd.

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

    # tmux wraps control mode output in a DCS envelope when the outer fd is a
    # terminal.  The prefix appears exactly once, at the start of the stream.
    _DCS_PREFIX = "\x1bP1000p"

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
        # PTY master fd — used for reading tmux output and writing commands.
        self._master_fd: int | None = None
        # Async reader wrapping _master_fd for non-blocking line reads.
        self._pty_reader: asyncio.StreamReader | None = None
        self._pty_transport: asyncio.BaseTransport | None = None

        # Command-response tracking for capture-pane initial content.
        # tmux numbers commands from 0, incrementing per command received.
        self._cmd_counter: int = 0
        self._capture_targets: dict[int, str] = {}  # cmd_num -> pane_id
        self._in_response: bool = False
        self._response_lines: list[str] = []
        self._response_cmd_num: int = -1
        # Guard: trigger_initial_redraw() only fires once per bridge instance.
        self._initial_redraw_done: bool = False

    async def start(self) -> None:
        """Spawn the tmux -CC subprocess with a PTY and begin reading output."""
        # Allocate a PTY so tmux's tcgetattr() succeeds on stdin.
        master_fd, slave_fd = pty.openpty()
        tty.setraw(slave_fd)
        self._master_fd = master_fd

        if self.ssh_alias:
            cmd = [
                "ssh",
                "-t",  # force remote PTY for the remote tmux process
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={self.ssh_connect_timeout}",
                self.ssh_alias,
                "tmux", "-CC", "attach", "-t", self.session_name,
            ]
        else:
            cmd = [self.tmux_path, "-CC", "attach", "-t", self.session_name]

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=asyncio.subprocess.DEVNULL,
        )
        os.close(slave_fd)

        # Wrap master_fd in an asyncio StreamReader for non-blocking line reads.
        loop = asyncio.get_running_loop()
        self._pty_reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(self._pty_reader)
        read_fd = os.dup(master_fd)
        self._pty_transport, _ = await loop.connect_read_pipe(
            lambda: protocol,
            os.fdopen(read_fd, "rb", 0),
        )

        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"bridge-reader-{self.session_name}"
        )
        # Negotiate client size immediately so tmux renders at our dimensions.
        await self.resize(self.cols, self.rows)

    async def _read_loop(self) -> None:
        """Read PTY output line by line and push parsed events to the queue.

        Handles tmux command responses (``%begin`` / ``%end`` blocks) in
        addition to asynchronous notifications.  Lines starting with ``%`` are
        always protocol messages — either response delimiters or notifications.
        All other lines between ``%begin`` and ``%end`` are command response
        text.  Notifications can be interleaved with command responses and are
        forwarded normally regardless of response state.

        Capture-pane responses (tracked via ``_capture_targets``) are emitted
        as synthetic ``output`` events so the browser receives initial pane
        content.
        """
        assert self._pty_reader is not None
        first_line = True
        try:
            async for raw_line in self._pty_reader:
                # Fast path for %output: process the payload at bytes level,
                # bypassing the decode('utf-8', errors='replace') roundtrip.
                #
                # tmux passes bytes >= 0x80 through %output unescaped (only
                # bytes < 0x20 and '\' are octal-encoded).  When a process
                # inside a pane writes a multi-byte UTF-8 sequence and tmux's
                # read() returns it in fragments across two calls, each
                # resulting %output line ends with an *incomplete* byte
                # sequence before its CRLF terminator.  decode('utf-8',
                # errors='replace') silently replaces each orphaned byte with
                # U+FFFD, which then re-encodes as \xef\xbf\xbd — three
                # replacement characters in xterm.js where one glyph belonged.
                #
                # Staying at bytes level forwards the raw (possibly split)
                # bytes to xterm.js whose own stateful UTF-8 decoder reassembles
                # them correctly across Terminal.write() calls.
                if raw_line.startswith(b"%output "):
                    parts = raw_line.rstrip(b"\r\n").split(b" ", 2)
                    if len(parts) == 3:
                        await self._event_queue.put({
                            "type": "output",
                            "pane_id": parts[1].decode("ascii"),
                            "data": _OCTAL_RE.sub(
                                lambda m: bytes([int(m.group(1), 8)]), parts[2]
                            ),
                        })
                    continue
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                # Strip DCS envelope prefix from the first control mode line.
                if first_line:
                    if line.startswith(self._DCS_PREFIX):
                        line = line[len(self._DCS_PREFIX):]
                    first_line = False

                if line.startswith("%"):
                    # Protocol message: delimiter or notification.
                    if line.startswith("%begin "):
                        self._in_response = True
                        self._response_lines = []
                        parts = line.split()
                        try:
                            self._response_cmd_num = int(parts[2]) if len(parts) >= 3 else -1
                        except ValueError:
                            self._response_cmd_num = -1
                        continue

                    if line.startswith("%end ") or line.startswith("%error "):
                        if self._in_response and self._response_cmd_num in self._capture_targets:
                            pane_id = self._capture_targets.pop(self._response_cmd_num)
                            if line.startswith("%end ") and self._response_lines:
                                # Emit captured content as a synthetic output event.
                                text = "\r\n".join(self._response_lines) + "\r\n"
                                await self._event_queue.put({
                                    "type": "output",
                                    "pane_id": pane_id,
                                    "data": text.encode("utf-8"),
                                })
                        self._in_response = False
                        self._response_lines = []
                        self._response_cmd_num = -1
                        continue

                    # Any other %-prefixed line is a notification — process it
                    # normally even if we are inside a command response block.
                    event = parse_control_line(line)
                    if event:
                        await self._event_queue.put(event)
                else:
                    # Non-% line: if we are inside a response block, accumulate
                    # it as response text.  Otherwise ignore stray lines.
                    if self._in_response:
                        self._response_lines.append(line)
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

    # ---- commands sent to tmux via PTY master ----

    async def _send_command(self, cmd: str) -> int:
        """Write a command line to tmux and return its sequence number."""
        num = self._cmd_counter
        self._cmd_counter += 1
        if self._master_fd is not None:
            try:
                os.write(self._master_fd, (cmd + "\n").encode())
            except OSError:
                pass  # PTY closed or process dead — stop() will clean up.
        return num

    async def capture_panes(self, pane_ids: list[str]) -> None:
        """Request initial content capture for the given panes.

        Sends ``capture-pane -p -e`` for each pane.  The read loop tracks the
        command responses and emits synthetic ``output`` events so the browser
        receives the current visible content of each pane.
        """
        for pane_id in pane_ids:
            num = await self._send_command(f"capture-pane -p -e -t {pane_id}")
            self._capture_targets[num] = pane_id


    async def trigger_initial_redraw(self) -> None:
        """Bounce the client width by +1 column to force tmux to re-emit all pane content.

        Sends ``refresh-client -C {cols+1},{rows}`` immediately followed by
        ``refresh-client -C {cols},{rows}``.  The size change causes tmux to
        re-render every visible pane and emit ``%output`` events carrying the
        full current screen state — cursor position, alternate-screen buffers,
        and SGR attributes — as a genuine VT100 stream.  xterm.js interprets
        this correctly regardless of what the pane was running.

        ``SIGWINCH`` is delivered to all pane foreground processes so they
        redraw.  This does not clear scroll history.

        Idempotent: the bounce is sent at most once per bridge instance.
        Subsequent calls are silent no-ops.
        """
        if self._initial_redraw_done:
            return
        self._initial_redraw_done = True
        # Step 1: report one extra column — tmux re-renders and emits %output.
        # Step 2: restore actual dimensions — tmux re-renders again at the
        #          correct size, overwriting the +1-column artefacts.
        await self._send_command(f"refresh-client -C {self.cols + 1},{self.rows}")
        await self._send_command(f"refresh-client -C {self.cols},{self.rows}")
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

    async def resize_pane(self, pane_id: str, cols: int, rows: int) -> None:
        """Resize an individual tmux pane to the given character dimensions."""
        await self._send_command(f"resize-pane -t {pane_id} -x {cols} -y {rows}")

    async def stop(self) -> None:
        """Terminate the subprocess and release PTY resources."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._pty_transport:
            self._pty_transport.close()
            self._pty_transport = None
            self._pty_reader = None
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None