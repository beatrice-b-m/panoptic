# REWORK_SPEC.md — panoptic: tmux Control Mode Terminal Renderer

**Repo:** `https://github.com/beatrice-b-m/panoptic`  
**Motivation:** Replace the current ttyd-per-session architecture with a tmux control mode (`tmux -CC`) bridge that renders each pane as an independent xterm.js instance in the browser. This eliminates the mouse event conflict that currently forces users to hold Option while selecting text, while preserving full tmux mouse pane-switching/resizing functionality.

---

## 1. Problem Statement

The current architecture spawns one `ttyd` process per tmux session and serves the entire tmux rendering (all panes, status bar, borders) as a single xterm.js terminal. Because tmux has mouse mode enabled (`set -g mouse on`), all mouse events are captured by tmux and forwarded as escape sequences to the PTY. This prevents the browser from performing native text selection without a modifier key (Option on macOS).

This is a fundamental protocol conflict: tmux's mouse mode and browser text selection both want ownership of mouse drag events, and there is no configuration path that satisfies both simultaneously within a single xterm.js instance.

**The root cause is architectural.** The fix is to move multiplexing out of the terminal wire protocol and into the browser UI layer.

---

## 2. Target Architecture

### 2.1 Core Concept

Replace ttyd with a **tmux control mode bridge**: a persistent server-side connection to `tmux -CC attach` that speaks the tmux control mode protocol over stdin/stdout. The bridge demultiplexes pane output and layout events and relays them to the browser over a single WebSocket connection per session view.

The browser receives per-pane output streams and layout geometry, and renders **one independent xterm.js instance per pane** in a CSS grid that mirrors the tmux layout. Each xterm.js instance has no knowledge of tmux; it is a plain terminal renderer. Mouse events in each pane div are fully owned by the browser, so text selection works natively without any modifier key or mode switching.

Keyboard input from the focused pane is sent back to the server as a `send-keys -t %<pane_id>` tmux control command. Pane switching (click to focus) is sent as `select-pane -t %<pane_id>`. The tmux session continues to run normally on the server; this architecture merely changes how the browser renders it.

### 2.2 Architecture Diagram

```
Browser (session view)
  │
  │  WebSocket  /ws/hosts/{host_id}/sessions/{session_name}
  │
  ▼
server.py  (aiohttp, port 7680)
  │
  ├── ControlBridge (per open session view)
  │     ├── asyncio.subprocess: tmux -CC attach -t {session_name}
  │     ├── Parser: reads %output / %layout-change / %window-* notifications
  │     ├── Writer: sends tmux commands to stdin (send-keys, select-pane, etc.)
  │     └── WebSocket relay: forwards parsed events as JSON to browser
  │
  └── Existing HTTP API (unchanged)
        GET/POST/DELETE /api/hosts/...
        GET/POST/DELETE /api/hosts/{host_id}/sessions/...
        GET/POST/.../templates/...
        GET /api/health

Browser (per-pane rendering)
  ├── PaneLayout component
  │     ├── CSS grid driven by %layout-change geometry
  │     └── Map<pane_id, Terminal>  (xterm.js instances, one per pane)
  ├── Input dispatch: keydown in focused pane → WS send-keys message
  └── Pane focus: click on pane div → WS select-pane message
```

### 2.3 What Changes vs. Current Codebase

| Component                 | Current State                                                                                           | Target State                                                                                                                                                              |
| ------------------------- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `session_manager.py`      | Spawns `ttyd` subprocess per session; manages port pool (7681–7699)                                     | Removes ttyd spawn/kill/port pool; adds `ControlBridge` class managing `tmux -CC` subprocess per open session view                                                        |
| `server.py`               | Routes `/terminal/{host_id}/{session_name}/` → HTTP+WS reverse proxy to ttyd                            | Adds WebSocket route `/ws/hosts/{host_id}/sessions/{session_name}`; removes terminal reverse proxy; removes `_proxy_http`, `_proxy_ws`, `handle_terminal`, `_ttyd_target` |
| `static/app.js`           | Opens session view → sets `src` on an `<iframe>` pointing to the ttyd proxy URL                         | Opens session view → opens WebSocket to `/ws/...`; renders one xterm.js div per pane; lays them out via CSS grid; dispatches keyboard/click events as WS messages         |
| `static/index.html`       | Contains `<iframe>` element for terminal display                                                        | Replaces iframe with a `<div id="pane-grid">` container                                                                                                                   |
| `config.py`               | Has `TTYD_PORT_RANGE_START`, `TTYD_PORT_RANGE_END`, `TTYD_BIND_HOST`, `TTYD_BINARY`, `TTYD_FONT_FAMILY` | Remove ttyd config constants; add `CONTROL_BRIDGE_COLS` (default 220), `CONTROL_BRIDGE_ROWS` (default 50), `TERMINAL_FONT_FAMILY` (replaces `TTYD_FONT_FAMILY`)           |
| `README.md`               | Lists ttyd as a prerequisite with install instructions                                                  | Remove ttyd prerequisite; add xterm.js CDN/npm note; update architecture description                                                                                      |
| `setup-service.sh`        | Installs ttyd via brew                                                                                  | Remove ttyd install step                                                                                                                                                  |
| `com.user.panoptic.plist` | No ttyd-specific config                                                                                 | No change needed                                                                                                                                                          |

### 2.4 What Does Not Change

- All existing REST API routes and their implementations (`/api/hosts/...`, `/api/templates/...`, `/api/health`)
- `session_manager.py` session polling logic (`_poll_host_sessions`, `_run_tmux`, `poll_sessions`)
- `session_manager.py` session creation/deletion (`create_session`, `delete_session`, `_apply_row_layout`, `_apply_col_layout`, `_send_pane_commands`)
- `host_config.py` — unchanged
- `template_store.py` — unchanged
- `template_macros.py` — unchanged
- `panoptic_cli.py` — unchanged
- Dashboard view (session gallery, session cards, thumbnails, create/delete UI, templates UI)
- All middleware (`client_tracking_middleware`, `security_headers_middleware`, `origin_validation_middleware`)
- TLS support, headless mode, launchd/systemd service management
- Multi-host SSH support

---

## 3. tmux Control Mode Protocol Reference

The implementation must handle the following subset of the tmux control mode protocol. The full specification is at `https://github.com/tmux/tmux/wiki/Control-Mode`.

### 3.1 Starting a Control Mode Client

```bash
# Attach to an existing session in control mode (no echo, application-mode)
tmux -CC attach -t {session_name}
```

The `-CC` flag disables canonical mode (no echo). The process communicates via its stdin/stdout; stderr is not used for protocol messages.

**Size negotiation:** After attach, immediately send the client size command so the bridge controls window dimensions (rather than inheriting whatever the spawning pty reports):

```
refresh-client -C {cols},{rows}
```

This must be sent before the first `%output` notifications arrive if possible, but the bridge should be prepared to send it at any time as the browser viewport resizes.

### 3.2 Notification Format

All notifications arrive as complete lines on stdout. A notification line begins with `%`:

```
%output %<pane_id> <escaped_text>
%layout-change @<window_id> <layout_string>
%window-add @<window_id>
%window-close @<window_id>
%window-renamed @<window_id> <new_name>
%session-window-changed $<session_id> @<window_id>
%pane-mode-changed %<pane_id>
%begin <timestamp> <command_number> <flags>
%end <timestamp> <command_number> <flags>
%error <timestamp> <command_number> <flags>
```

Command responses are wrapped in `%begin` / `%end` (or `%begin` / `%error`) blocks. Notifications appear between these blocks or before any commands are issued.

### 3.3 Output Escaping

The `%output` line uses a specific escaping scheme:

> Any character with ASCII value less than 32, and the backslash character (`\`), is replaced with its octal equivalent. So `\n` becomes `\012`, `\r` becomes `\015`, `\033` (ESC) becomes `\033`, and `\` becomes `\134`.

The parser must unescape these sequences before writing bytes to the xterm.js instance.

**Unescape algorithm:**

```python
import re

_OCTAL_RE = re.compile(r'\\([0-7]{3})')

def unescape_output(s: str) -> bytes:
    """Convert a tmux control mode %output payload to raw bytes."""
    def replace(m):
        return bytes([int(m.group(1), 8)])
    # Process octal escapes, yielding bytes
    result = bytearray()
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 3 < len(s) and s[i+1:i+4].isdigit():
            # Check it's octal digits
            oct_str = s[i+1:i+4]
            if all(c in '01234567' for c in oct_str):
                result.append(int(oct_str, 8))
                i += 4
                continue
        result.extend(s[i].encode('utf-8', errors='replace'))
        i += 1
    return bytes(result)
```

The resulting bytes are the raw terminal output that would have been written to the PTY — ANSI escape sequences, cursor movements, etc. These bytes are forwarded as-is to the browser and written directly into the corresponding xterm.js instance via `terminal.write(data)`.

### 3.4 Layout String Format

The `%layout-change` notification includes a layout string describing all panes in the window. Example:

```
%layout-change @0 5f2d,220x50,0,0[220x25,0,0,%0,220x24,0,26,%1]
```

Format (simplified grammar):

```
layout     := checksum,WxH,X,Y[,children] | checksum,WxH,X,Y,%pane_id
checksum   := 4 hex chars
WxH        := cols 'x' rows
X,Y        := col offset, row offset
children   := '[' layout (',' layout)* ']'   (for split containers)
```

The checksum can be ignored for rendering. The parser needs to extract from each leaf node: `pane_id` (the `%N` token), `cols`, `rows`, `x`, `y`.

A reference Python parser:

```python
import re
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class PaneGeometry:
    pane_id: str   # e.g. "%0"
    cols: int
    rows: int
    x: int         # left offset in characters
    y: int         # top offset in characters

def parse_layout(layout_str: str) -> List[PaneGeometry]:
    """Parse a tmux layout string into a flat list of pane geometries."""
    # Strip optional leading checksum (4 hex chars + comma)
    s = re.sub(r'^[0-9a-f]{4},', '', layout_str.strip())
    panes = []
    _parse_node(s, panes)
    return panes

def _parse_node(s: str, out: List[PaneGeometry]) -> int:
    """Recursively parse one layout node; returns chars consumed."""
    # WxH,X,Y
    m = re.match(r'(\d+)x(\d+),(\d+),(\d+)', s)
    if not m:
        raise ValueError(f"Cannot parse layout node: {s!r}")
    cols, rows, x, y = int(m[1]), int(m[2]), int(m[3]), int(m[4])
    pos = m.end()

    if pos < len(s) and s[pos] == ',':
        pos += 1
        if pos < len(s) and s[pos] == '%':
            # Leaf node: %pane_id
            m2 = re.match(r'%(\d+)', s[pos:])
            if not m2:
                raise ValueError(f"Expected pane id at: {s[pos:]!r}")
            pane_id = f"%{m2[1]}"
            out.append(PaneGeometry(pane_id=pane_id, cols=cols, rows=rows, x=x, y=y))
            pos += m2.end()
        elif pos < len(s) and s[pos] == '[':
            # Container node: [...children...]
            pos += 1  # skip '['
            while pos < len(s) and s[pos] != ']':
                consumed = _parse_node(s[pos:], out)
                pos += consumed
                if pos < len(s) and s[pos] == ',':
                    pos += 1
            if pos < len(s) and s[pos] == ']':
                pos += 1
    return pos
```

### 3.5 Commands Sent to tmux

The bridge writes commands to tmux stdin, one per line. Commands do not need a `:` prefix (unlike the tmux command prompt).

```
# Resize the client (sent on connect and on browser resize)
refresh-client -C {cols},{rows}

# Send a keystroke to a specific pane
send-keys -t %{pane_id} -H {hex_byte} [{hex_byte} ...]

# Alternative: send literal string (use -H for binary safety)
send-keys -t %{pane_id} {string} ""

# Focus a pane (browser-side pane click)
select-pane -t %{pane_id}

# Query current layout (on initial attach, before first %layout-change)
list-windows -F "#{window_layout}"
```

**Note on `send-keys -H`:** For forwarding arbitrary keystrokes (function keys, arrow keys, Ctrl sequences), it is safest to hex-encode each byte and use `send-keys -H`. The `-H` flag accepts a space-separated list of hex byte values:

```
send-keys -t %0 -H 1b 5b 41   # ESC [ A = Up arrow
```

---

## 4. Server-Side Implementation

### 4.1 New File: `control_bridge.py`

Create `control_bridge.py` in the project root. This module owns the subprocess lifecycle for one `tmux -CC` client and provides an async interface for the WebSocket handler.

```python
# control_bridge.py — skeleton

import asyncio
import logging
from typing import AsyncIterator, Callable

log = logging.getLogger(__name__)

class ControlBridge:
    """Manages a single `tmux -CC attach -t <session>` subprocess.

    Parses control mode protocol lines and emits structured events.
    Accepts commands to send to tmux stdin.

    Usage:
        bridge = ControlBridge(session_name, cols, rows, tmux_path, ssh_alias=None)
        await bridge.start()
        async for event in bridge.events():
            ...  # {"type": "output", "pane_id": "%0", "data": bytes}
                 # {"type": "layout", "window_id": "@0", "panes": [...]}
                 # {"type": "window_add", "window_id": "@0"}
                 # {"type": "window_close", "window_id": "@0"}
                 # {"type": "exit"}
        await bridge.send_keys(pane_id="%0", data=b"\x1b[A")
        await bridge.select_pane(pane_id="%0")
        await bridge.resize(cols=220, rows=50)
        await bridge.stop()
    """

    def __init__(
        self,
        session_name: str,
        cols: int,
        rows: int,
        tmux_path: str = "tmux",
        ssh_alias: str | None = None,
    ) -> None:
        self.session_name = session_name
        self.cols = cols
        self.rows = rows
        self.tmux_path = tmux_path
        self.ssh_alias = ssh_alias
        self._process: asyncio.subprocess.Process | None = None
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Spawn the tmux -CC subprocess and begin reading stdout."""
        if self.ssh_alias:
            cmd = ["ssh", self.ssh_alias, "tmux", "-CC", "attach", "-t", self.session_name]
        else:
            cmd = [self.tmux_path, "-CC", "attach", "-t", self.session_name]

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        # Negotiate client size immediately
        await self.resize(self.cols, self.rows)

    async def _read_loop(self) -> None:
        """Read stdout line by line and push parsed events to the queue."""
        assert self._process and self._process.stdout
        try:
            async for raw_line in self._process.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                event = self._parse_line(line)
                if event:
                    await self._event_queue.put(event)
        except Exception as exc:
            log.exception("ControlBridge reader error: %s", exc)
        finally:
            await self._event_queue.put({"type": "exit"})

    def _parse_line(self, line: str) -> dict | None:
        """Parse one control mode protocol line into an event dict."""
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
                "data": unescape_output(raw_payload),  # bytes
            }
        elif notification == "%layout-change" and len(parts) >= 3:
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
        elif notification in ("%window-add", "%window-close", "%window-renamed"):
            window_id = parts[1] if len(parts) > 1 else ""
            return {"type": notification[1:].replace("-", "_"), "window_id": window_id}
        elif notification in ("%begin", "%end", "%error"):
            return None  # command response bookends — ignored
        return None

    async def events(self) -> AsyncIterator[dict]:
        """Yield events until the subprocess exits."""
        while True:
            event = await self._event_queue.get()
            yield event
            if event["type"] == "exit":
                return

    async def _send_command(self, cmd: str) -> None:
        if self._process and self._process.stdin and not self._process.stdin.is_closing():
            self._process.stdin.write((cmd + "\n").encode())
            await self._process.stdin.drain()

    async def send_keys(self, pane_id: str, data: bytes) -> None:
        """Forward raw bytes from the browser to a specific pane."""
        hex_bytes = " ".join(f"{b:02x}" for b in data)
        await self._send_command(f"send-keys -t {pane_id} -H {hex_bytes}")

    async def select_pane(self, pane_id: str) -> None:
        await self._send_command(f"select-pane -t {pane_id}")

    async def resize(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows
        await self._send_command(f"refresh-client -C {cols},{rows}")

    async def stop(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
```

### 4.2 Changes to `session_manager.py`

**Remove entirely:**

- `allocate_port()`, `release_port()` methods
- `_port_pool`, `_allocated_ports` deque/set in `__init__`
- `_spawn_ttyd()` method
- `_kill_ttyd()` method (rename logic: session removal is now just dict cleanup)
- `_wait_for_port_ready()` method
- `_kill_stale_ttyd()` and `kill_stale_ttyd()` methods
- `_record_ttyd_pid()`, `_remove_ttyd_pid()`, `_read_pid_file()`, `_write_pid_file()` methods
- `self._ttyd_pid_file`, `self._ttyd_path` in `__init__`
- `ttyd_pid` and `port` fields from `SessionInfo` dataclass (keep `_process` field removed too since ttyd no longer runs)

**Modify `SessionInfo`:**

```python
@dataclass
class SessionInfo:
    host_id: str
    name: str
    windows: int
    attached: bool
    created_epoch: int
    # port and ttyd_pid are removed
```

**Modify `_poll_host_sessions`:** Remove the ttyd spawn/kill calls. The polling loop now only maintains `self._host_sessions` as a registry of known sessions; no processes are managed here.

```python
# Where previously: await self._spawn_ttyd(host_id, new)
# Now: just register the session, no subprocess
host_sessions[new] = SessionInfo(
    host_id=host_id,
    name=new,
    windows=info["windows"],
    attached=info["attached"],
    created_epoch=info["created_epoch"],
)

# Where previously: await self._kill_ttyd(host_id, gone)
# Now: just remove from registry
host_sessions.pop(gone, None)
host_cache = self._snapshot_cache.get(host_id)
if host_cache:
    host_cache.pop(gone, None)
```

**Modify `remove_host_sessions`:** Remove the ttyd kill loop; just clear the registry dict.

**Modify `_poll_host_sessions` dead-ttyd-respawn block:** Remove entirely (no ttyd to respawn).

**Modify `create_session` return value:** Remove `ttyd_url` from the returned dict. The session view no longer uses a ttyd URL; it connects via the new WebSocket route.

**Modify `get_panes`:** Remove `port` from returned pane dicts (it's no longer meaningful).

**Keep unchanged:** All `_run_tmux`, `poll_sessions`, `_poll_host_sessions` session discovery logic, `create_session` tmux subprocess logic, `delete_session`, `_apply_row_layout`, `_apply_col_layout`, `_send_pane_commands`, `get_thumbnail_svg`, `list_directories`, `start_polling`, `cleanup`.

### 4.3 Changes to `server.py`

**Remove:**

- `handle_terminal()` route handler
- `_proxy_http()` helper
- `_proxy_ws()` helper
- `_ttyd_target()` helper
- `_HOP_HEADERS` constant
- `_proxy_request_headers()` helper
- The `app["client_session"]` aiohttp.ClientSession (used only for proxy)
- The router entry: `app.router.add_get("/terminal/{host_id}/{session_name}/{path:.*}", handle_terminal)`
- `on_cleanup` cleanup of `client_session`

**Add — WebSocket handler for control mode bridge:**

```python
# Bridge registry: (host_id, session_name) -> ControlBridge
# Scoped to the app, not the request
_active_bridges: dict[tuple[str, str], ControlBridge] = {}

async def handle_terminal_ws(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint bridging the browser to a tmux control mode client."""
    host_id = request.match_info["host_id"]
    session_name = request.match_info["session_name"]
    mgr: SessionManager = request.app["session_manager"]
    settings: RuntimeSettings = request.app["settings"]

    # Verify session exists
    host_sessions = mgr.sessions_for_host(host_id)
    if session_name not in host_sessions:
        return web.Response(status=404, text="Session not found")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Determine cols/rows from query params or config defaults
    try:
        cols = int(request.query.get("cols", settings.control_bridge_cols))
        rows = int(request.query.get("rows", settings.control_bridge_rows))
    except ValueError:
        cols, rows = settings.control_bridge_cols, settings.control_bridge_rows

    host = request.app["host_config"].get_host(host_id)
    ssh_alias = host.get("ssh_alias") if host and host["type"] == "ssh" else None

    bridge = ControlBridge(
        session_name=session_name,
        cols=cols,
        rows=rows,
        tmux_path=mgr._tmux_path,
        ssh_alias=ssh_alias,
    )

    bridge_key = (host_id, session_name)
    request.app["active_bridges"][bridge_key] = bridge

    try:
        await bridge.start()

        async def relay_events():
            """Push bridge events to the browser as JSON."""
            async for event in bridge.events():
                if ws.closed:
                    break
                if event["type"] == "output":
                    # Binary data: send as bytes with a framing prefix
                    # Frame: 1 byte type (0x01=output) + 2 bytes pane_id length
                    # + pane_id UTF-8 + raw terminal bytes
                    # See §5 WebSocket Protocol below for exact framing.
                    pane_id_bytes = event["pane_id"].encode()
                    frame = (
                        b"\x01"
                        + len(pane_id_bytes).to_bytes(2, "big")
                        + pane_id_bytes
                        + event["data"]
                    )
                    await ws.send_bytes(frame)
                elif event["type"] == "layout":
                    import json
                    await ws.send_str(json.dumps({
                        "type": "layout",
                        "window_id": event["window_id"],
                        "panes": event["panes"],
                    }))
                elif event["type"] == "exit":
                    await ws.send_str('{"type":"exit"}')
                    break

        relay_task = asyncio.create_task(relay_events())

        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                msg_type = data.get("type")
                if msg_type == "input":
                    # Browser sends keystrokes as hex-encoded bytes
                    raw = bytes.fromhex(data.get("data", ""))
                    await bridge.send_keys(data.get("pane_id", ""), raw)
                elif msg_type == "select_pane":
                    await bridge.select_pane(data.get("pane_id", ""))
                elif msg_type == "resize":
                    await bridge.resize(
                        int(data.get("cols", cols)),
                        int(data.get("rows", rows)),
                    )
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break

        relay_task.cancel()
        try:
            await relay_task
        except asyncio.CancelledError:
            pass

    finally:
        await bridge.stop()
        request.app["active_bridges"].pop(bridge_key, None)

    return ws
```

**Add to `build_app`:**

```python
app["active_bridges"] = {}
app.router.add_get(
    "/ws/hosts/{host_id}/sessions/{session_name}",
    handle_terminal_ws,
)
```

**Add to `on_cleanup`:** Stop all active bridges.

```python
for bridge in list(app.get("active_bridges", {}).values()):
    await bridge.stop()
```

**Modify `handle_session_detail`:** Remove `ttyd_url` from response; add `ws_url`:

```python
safe_host = urlquote(host_id, safe="")
safe_name = urlquote(session_name, safe="")
return web.json_response({
    "name": sess.name,
    "host_id": sess.host_id,
    "windows": sess.windows,
    "attached": sess.attached,
    "created_epoch": sess.created_epoch,
    "ws_url": f"/ws/hosts/{safe_host}/sessions/{safe_name}",
})
```

**Modify `handle_panes`:** Remove `ttyd_url` from pane dicts; remove `port` handling.

**Modify `handle_sessions`:** If `ttyd_url` is currently included in session list items, remove it.

**Modify `handle_create_session`:** Remove `ttyd_url` from returned dict; add `ws_url`.

**Modify `on_startup`:** Remove `kill_stale_ttyd()` call; remove `aiohttp.ClientSession` initialization.

### 4.4 Changes to `config.py`

Remove:

```python
TTYD_PORT_RANGE_START = 7681
TTYD_PORT_RANGE_END = 7699
TTYD_BIND_HOST = "127.0.0.1"
TTYD_BINARY = "ttyd"
TTYD_FONT_FAMILY = "Hack Nerd Font, ..."
```

Add:

```python
CONTROL_BRIDGE_COLS = 220   # default terminal width for new bridge connections
CONTROL_BRIDGE_ROWS = 50    # default terminal height
TERMINAL_FONT_FAMILY = "Hack Nerd Font, Hack Nerd Font Mono, Menlo, Consolas, monospace"
```

Update `RuntimeSettings` dataclass / namedtuple accordingly, including CLI flag renaming: `--ttyd-font-family` → `--font-family`.

---

## 5. WebSocket Protocol (Browser ↔ Server)

The WebSocket at `/ws/hosts/{host_id}/sessions/{session_name}` carries two message types:

### 5.1 Server → Browser

**Binary frame (type `0x01`): pane output**

```
Byte 0:       0x01  (message type = output)
Bytes 1–2:    uint16 big-endian: length of pane_id string N
Bytes 3–(3+N-1): pane_id as UTF-8 (e.g. "%0", "%1")
Bytes (3+N)…: raw terminal bytes (ANSI sequences, printable chars, etc.)
```

**Text frame: layout or control event**

```json
{"type": "layout", "window_id": "@0", "panes": [
    {"pane_id": "%0", "cols": 110, "rows": 50, "x": 0,   "y": 0},
    {"pane_id": "%1", "cols": 110, "rows": 50, "x": 110, "y": 0}
]}

{"type": "window_add",   "window_id": "@1"}
{"type": "window_close", "window_id": "@1"}
{"type": "exit"}
```

### 5.2 Browser → Server

All messages are JSON text frames.

```json
// Keystroke in a pane (data is hex-encoded bytes)
{"type": "input", "pane_id": "%0", "data": "1b5b41"}

// User clicked on a pane to focus it
{"type": "select_pane", "pane_id": "%1"}

// Browser viewport/terminal size changed
{"type": "resize", "cols": 200, "rows": 48}
```

---

## 6. Frontend Implementation (`static/app.js` + `static/index.html`)

### 6.1 xterm.js Dependency

xterm.js must be loaded in `static/index.html`. Since the project currently has no build step, use the CDN:

```html
<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5/lib/xterm.js"></script>
<link
	href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5/css/xterm.css"
	rel="stylesheet"
/>
<!-- Optional: FitAddon for auto-sizing -->
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10/lib/addon-fit.js"></script>
```

Alternatively, vendor these files into `static/` to avoid external CDN dependency (preferred for an always-on local tool).

### 6.2 Session View State Machine

The session view (currently opened by clicking a session card) must transition from iframe-based to WebSocket-based:

**Current flow:**

1. User clicks session card
2. `GET /api/hosts/{host_id}/sessions/{name}` → get `ttyd_url`
3. Set `<iframe src=ttyd_url>`

**New flow:**

1. User clicks session card
2. `GET /api/hosts/{host_id}/sessions/{name}` → get `ws_url`
3. Open `new WebSocket(ws_url + "?cols=" + cols + "&rows=" + rows)`
4. Initialize pane grid (empty, waiting for first `layout` event)
5. On `layout` event: create/update xterm.js instances and CSS grid
6. On `output` event: find xterm.js instance by `pane_id`; call `terminal.write(data)`
7. On `exit` event: show reconnect UI
8. On WS close: attempt reconnect with backoff

### 6.3 Pane Grid Layout

The pane container replaces the `<iframe>`. In `index.html`:

```html
<!-- Replace the existing iframe element with: -->
<div id="pane-grid" class="pane-grid"></div>
```

In `app.js`, the `PaneGrid` component:

```javascript
class PaneGrid {
	constructor(container, ws, fontFamily) {
		this.container = container;
		this.ws = ws;
		this.fontFamily = fontFamily;
		this.panes = new Map(); // pane_id -> { terminal: Terminal, el: HTMLElement }
		this.activePaneId = null;
	}

	applyLayout(panes) {
		// panes: [{pane_id, cols, rows, x, y}, ...]
		// Determine total grid dimensions
		const maxX = Math.max(...panes.map((p) => p.x + p.cols));
		const maxY = Math.max(...panes.map((p) => p.y + p.rows));

		// Use CSS grid with character-unit columns/rows
		// Each grid cell = 1 character width/height
		// Use ch units for width, em/lh approximation for height
		this.container.style.display = "grid";
		this.container.style.gridTemplateColumns = `repeat(${maxX}, 1ch)`;
		// Height unit: approximate with a CSS variable set to measured char height
		this.container.style.gridTemplateRows = `repeat(${maxY}, var(--char-height, 1.2em))`;

		const existingIds = new Set(this.panes.keys());
		const incomingIds = new Set(panes.map((p) => p.pane_id));

		// Remove panes that disappeared
		for (const id of existingIds) {
			if (!incomingIds.has(id)) {
				const pane = this.panes.get(id);
				pane.terminal.dispose();
				pane.el.remove();
				this.panes.delete(id);
			}
		}

		// Add or update panes
		for (const geom of panes) {
			if (!this.panes.has(geom.pane_id)) {
				this._createPane(geom);
			} else {
				this._updatePaneGeometry(geom);
			}
		}
	}

	_createPane(geom) {
		const el = document.createElement("div");
		el.className = "pane-cell";
		el.dataset.paneId = geom.pane_id;
		this._positionPane(el, geom);

		const terminal = new Terminal({
			cols: geom.cols,
			rows: geom.rows,
			fontFamily: this.fontFamily,
			fontSize: 14,
			theme: {
				background: "#1a1a1a",
				foreground: "#f0f0f0",
			},
			// Key: disable all mouse event forwarding
			// Each pane div owns its mouse events natively
			mouseEvents: false, // xterm.js option
			scrollback: 5000,
			rightClickSelectsWord: true,
		});

		terminal.open(el);

		// Forward keyboard input to server
		terminal.onKey(({ key, domEvent }) => {
			const hex = Array.from(new TextEncoder().encode(key))
				.map((b) => b.toString(16).padStart(2, "0"))
				.join("");
			this.ws.send(
				JSON.stringify({
					type: "input",
					pane_id: geom.pane_id,
					data: hex,
				}),
			);
		});

		// Pane focus on click
		el.addEventListener("click", () => {
			this.activePaneId = geom.pane_id;
			this.ws.send(
				JSON.stringify({
					type: "select_pane",
					pane_id: geom.pane_id,
				}),
			);
			terminal.focus();
		});

		this.container.appendChild(el);
		this.panes.set(geom.pane_id, { terminal, el });
	}

	_positionPane(el, geom) {
		// CSS grid placement: grid lines are 1-indexed
		el.style.gridColumn = `${geom.x + 1} / span ${geom.cols}`;
		el.style.gridRow = `${geom.y + 1} / span ${geom.rows}`;
	}

	_updatePaneGeometry(geom) {
		const pane = this.panes.get(geom.pane_id);
		this._positionPane(pane.el, geom);
		pane.terminal.resize(geom.cols, geom.rows);
	}

	writeOutput(paneId, data) {
		const pane = this.panes.get(paneId);
		if (pane) pane.terminal.write(data);
	}

	dispose() {
		for (const { terminal, el } of this.panes.values()) {
			terminal.dispose();
			el.remove();
		}
		this.panes.clear();
	}
}
```

### 6.4 WebSocket Message Handling

```javascript
function openSessionView(hostId, sessionName, wsUrl) {
	const container = document.getElementById("pane-grid");
	const ws = new WebSocket(
		`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${wsUrl}` +
			`?cols=${Math.floor(container.clientWidth / CHAR_WIDTH)}` +
			`&rows=${Math.floor(container.clientHeight / CHAR_HEIGHT)}`,
	);

	const grid = new PaneGrid(container, ws, FONT_FAMILY);

	ws.binaryType = "arraybuffer";

	ws.onmessage = (event) => {
		if (event.data instanceof ArrayBuffer) {
			// Binary frame: pane output
			const buf = new Uint8Array(event.data);
			if (buf[0] === 0x01) {
				const paneIdLen = (buf[1] << 8) | buf[2];
				const paneId = new TextDecoder().decode(
					buf.slice(3, 3 + paneIdLen),
				);
				const termData = buf.slice(3 + paneIdLen);
				grid.writeOutput(paneId, termData);
			}
		} else {
			// Text frame: JSON event
			const msg = JSON.parse(event.data);
			if (msg.type === "layout") {
				grid.applyLayout(msg.panes);
			} else if (msg.type === "exit") {
				showReconnectBanner();
			}
		}
	};

	ws.onclose = () => {
		grid.dispose();
		scheduleReconnect(hostId, sessionName);
	};

	return { ws, grid };
}
```

### 6.5 Character Dimension Measurement

To correctly size the CSS grid, the frontend needs to know the rendered character width and height in pixels for the chosen font. Measure on load:

```javascript
function measureCharDimensions(fontFamily, fontSize) {
	const probe = document.createElement("span");
	probe.style.cssText = `font-family:${fontFamily};font-size:${fontSize}px;position:absolute;visibility:hidden;white-space:pre`;
	probe.textContent = "M";
	document.body.appendChild(probe);
	const rect = probe.getBoundingClientRect();
	document.body.removeChild(probe);
	// Set CSS variable for grid row height
	document.documentElement.style.setProperty(
		"--char-height",
		`${rect.height}px`,
	);
	return { width: rect.width, height: rect.height };
}
```

### 6.6 CSS for Pane Grid

Add to `static/style.css`:

```css
.pane-grid {
	display: grid;
	width: 100%;
	height: 100%;
	overflow: hidden;
	background: #1a1a1a;
	/* gap creates visible pane borders */
	gap: 1px;
	background-color: #333; /* gap color = pane border color */
}

.pane-cell {
	overflow: hidden;
	background: #1a1a1a;
	/* Active pane highlight */
	outline: none;
}

.pane-cell.active {
	outline: 1px solid #4fc3f7;
}

/* xterm.js containers fill their pane cell */
.pane-cell .xterm {
	width: 100%;
	height: 100%;
}
```

---

## 7. SSH (Remote Host) Support

The existing remote host mechanism uses SSH to forward tmux commands. The control bridge must support SSH hosts by wrapping the `tmux -CC` invocation:

```python
# Local host:
cmd = [tmux_path, "-CC", "attach", "-t", session_name]

# SSH host:
cmd = [
    "ssh",
    "-o", "BatchMode=yes",
    "-o", f"ConnectTimeout={ssh_connect_timeout}",
    ssh_alias,
    "tmux", "-CC", "attach", "-t", session_name,
]
```

All other bridge logic is identical. The protocol flows through SSH's stdio transparently.

**Important:** SSH hosts that use ControlMaster (`ControlMaster auto` in `~/.ssh/config`) will reuse an existing SSH connection, making bridge startup fast. This is already the recommended config in the existing README and should be mentioned in updated documentation.

---

## 8. Things to Preserve Exactly

The following must not be changed during this rework:

- `AGENTS.md` — agent configuration file
- `session_manager.py` `get_thumbnail_svg()` — SVG thumbnail generation via `tmux capture-pane` is unrelated to this change
- All template machinery (`template_store.py`, `template_macros.py`, all `/api/templates/` routes)
- The session polling loop and all tmux discovery logic
- Session creation and deletion (the `create_session`/`delete_session` flow creates real tmux sessions via subprocess; this is unchanged)
- Dashboard gallery view (session cards, pagination, thumbnails, new session form, template UI)
- All security middleware
- Host management (`host_config.py`, all `/api/hosts/` routes)
- launchd/systemd service files and `setup-service.sh` (with ttyd install step removed)
- `panoptic_cli.py` (minus ttyd-specific flags)
- `NEXT_STEPS.md` — do not modify unless instructed

---

## 9. Dependency Changes

### Remove

- `ttyd` binary (no longer spawned)
- `aiohttp.ClientSession` in `server.py` (was used only for the ttyd HTTP/WS proxy)

### Add

- `xterm.js` v5.x — loaded in `static/index.html` (CDN or vendored into `static/`)
- `@xterm/addon-fit` v0.10.x — optional but recommended for auto-sizing xterm.js to container
- No new Python dependencies — `asyncio.create_subprocess_exec` is stdlib

### `requirements.txt` / install instructions

Update README and `setup-service.sh`:

- Remove: `brew install ttyd`
- Add: note that xterm.js is loaded from CDN (or instruction to vendor it)
- Python deps remain: `aiohttp` only

---

## 10. Testing Checklist

The implementing agent should verify the following scenarios before considering the rework complete:

- [ ] Single-pane session: terminal renders, keyboard input works, text can be selected and copied without holding any modifier key
- [ ] Multi-pane session (2+ panes): correct CSS grid layout, each pane independent, clicking between panes switches focus
- [ ] tmux mouse mode ON (`set -g mouse on`): pane click-to-focus works via `select_pane` command; scroll wheel works in each pane's xterm.js independently; text selection works without modifier key
- [ ] tmux pane resize (from inside tmux): `%layout-change` event received; grid updates; each xterm.js resized correctly
- [ ] Browser window resize: `resize` message sent; `refresh-client -C` updates tmux; layout recalculates
- [ ] Session disappears (tmux session killed externally): bridge subprocess exits; browser receives `exit` event; reconnect UI shown
- [ ] Remote SSH host: control bridge connects via SSH; pane output relayed correctly
- [ ] Multiple concurrent session views: each view has its own independent bridge instance
- [ ] Server restart: no stale ttyd processes to clean up; bridges start fresh on next client connect
- [ ] Thumbnail SVG generation still works (unrelated code path — regression check)
- [ ] Session create/delete API still works (regression check)
- [ ] Template create/load still works (regression check)

---

## 11. File Change Summary

| File                      | Action        | Summary                                                                                             |
| ------------------------- | ------------- | --------------------------------------------------------------------------------------------------- |
| `control_bridge.py`       | **CREATE**    | New module: `ControlBridge` class managing `tmux -CC` subprocess                                    |
| `session_manager.py`      | **MODIFY**    | Remove all ttyd-related code; simplify `SessionInfo`; clean session registry on poll                |
| `server.py`               | **MODIFY**    | Remove terminal proxy; add `/ws/...` WebSocket route and handler; add `active_bridges` to app state |
| `config.py`               | **MODIFY**    | Remove ttyd constants; add `CONTROL_BRIDGE_COLS`, `CONTROL_BRIDGE_ROWS`, `TERMINAL_FONT_FAMILY`     |
| `static/index.html`       | **MODIFY**    | Replace `<iframe>` with `<div id="pane-grid">`; add xterm.js script/link tags                       |
| `static/app.js`           | **MODIFY**    | Replace iframe/ttyd session view with WebSocket + `PaneGrid` + xterm.js rendering                   |
| `static/style.css`        | **MODIFY**    | Add `.pane-grid`, `.pane-cell`, `.pane-cell.active` styles                                          |
| `setup-service.sh`        | **MODIFY**    | Remove `brew install ttyd` step                                                                     |
| `README.md`               | **MODIFY**    | Remove ttyd prerequisite; update architecture section; add xterm.js note                            |
| `SYSTEM_SPEC.md`          | **MODIFY**    | Update architecture diagram and technology stack table to reflect control mode design               |
| `host_config.py`          | **NO CHANGE** | —                                                                                                   |
| `template_store.py`       | **NO CHANGE** | —                                                                                                   |
| `template_macros.py`      | **NO CHANGE** | —                                                                                                   |
| `panoptic_cli.py`         | **MODIFY**    | Remove `--ttyd-font-family` flag; add `--font-family`; remove ttyd port range flags                 |
| `com.user.panoptic.plist` | **NO CHANGE** | —                                                                                                   |
| `panoptic.service`        | **NO CHANGE** | —                                                                                                   |
| `AGENTS.md`               | **NO CHANGE** | —                                                                                                   |
| `NEXT_STEPS.md`           | **NO CHANGE** | —                                                                                                   |
