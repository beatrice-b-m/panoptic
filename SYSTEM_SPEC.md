# System Specification: tmux Web Dashboard

**Project:** `panoptic` — Browser-based tmux session monitor and terminal interface  
**Target platform:** macOS (Mac Mini, Apple Silicon or Intel)  
**Access method:** Tailscale private network  
**Primary use case:** Monitoring and interacting with Oh-My-Pi (OMP) agentic coding agent sessions

---

## 1. Overview

`panoptic` is a persistent local web server running on a Mac Mini that exposes all active tmux sessions as interactive browser terminals. It provides a session-picker dashboard and full terminal interaction (keyboard and mouse) for multi-pane tmux sessions. It is designed to be always-on, resource-efficient when idle, and accessible exclusively over a Tailscale private network.

---

## 2. Goals and Non-Goals

### Goals

- Always-running service with automatic startup via `launchd`
- Real-time discovery of active tmux sessions (no manual registration)
- Browser UI: paginated session list → click into session → full multi-pane terminal view
- Full keyboard input and mouse support in terminal panes
- Efficient resource usage when no client is connected
- Safe for Tailscale-only access (no public internet exposure)
- Session template management: save, load, rename, and delete templates with macro variable support

### Non-Goals

- Authentication/TLS (Tailscale handles this)
- ~~Session creation or tmux configuration management~~ (session creation now implemented via templates)
- Mobile-optimised layout (desktop browser is primary)
- Multi-user simultaneous access (single-user personal tool)

---

## 3. Architecture

### 3.1 Component Overview

```
Mac Mini
├── panoptic server (Python, port 7680, optional TLS)
│   ├── Dashboard HTTP endpoint    GET /
│   ├── Host API                  GET/POST/DELETE /api/hosts/...
│   ├── Session API               GET/POST/DELETE /api/hosts/{host_id}/sessions/...
│   ├── Template API              GET/POST/PUT/PATCH/DELETE /api/templates/...
│   ├── Session WS endpoint       /ws/hosts/{host_id}/sessions/{session_name}
│   └── Static assets             GET /static/*
└── tmux control-mode bridges (one bridge per active host/session view)
    └── connects to tmux via `tmux -CC` and streams pane updates over WebSocket
```

### 3.2 Process Roles

**panoptic server** (always running, `launchd`-managed):

- Serves the dashboard HTML/JS frontend
- Maintains a session registry by polling `tmux ls` on a configurable interval
- Manages tmux control-mode bridges for active terminal views
- Routes browser WebSocket clients to the corresponding control-mode bridge
- Idles gracefully: polling slows to a long interval (~30s) when no WebSocket clients are connected

**tmux control-mode bridge** (host/session scoped, managed by panoptic):

- Starts `tmux -CC attach-session -t {session_name}` (directly or via SSH for remote hosts)
- Parses control-mode output and tracks pane metadata/state
- Emits pane-specific terminal frames to browser clients over WebSocket
- Applies browser input (keys, resize, mouse) back to tmux control mode
- Stops when the session view closes or the tmux session ends

### 3.3 Technology Stack

| Layer              | Choice                              | Rationale                             |
| ------------------ | ----------------------------------- | ------------------------------------- |
| Server language    | Python 3.11+                        | Available on macOS, minimal deps      |
| HTTP/WS framework  | `aiohttp` or `FastAPI` + `uvicorn`  | Async, low idle overhead              |
| Terminal transport | tmux control mode (`tmux -CC`)      | Native pane events + bidirectional control |
| Frontend           | Vanilla HTML/CSS/JS (no build step) | Simple, maintainable, no npm required |
| Terminal renderer  | xterm.js (per-pane instances)       | Full VT emulation, mouse support      |
| Process management | `launchd` plist                     | macOS-native, survives login/reboot   |

**Install dependencies:**

```bash
pip3 install aiohttp   # or: pip3 install fastapi uvicorn
```

---

## 4. Session Discovery

### 4.1 Session Polling

The server polls tmux on a configurable interval:

```bash
tmux list-sessions -F "#{session_name}|#{session_windows}|#{session_attached}|#{session_created}"
```

Output is parsed into a session registry dict keyed by session name.

**Polling intervals:**

- Active clients connected: every **5 seconds**
- No clients connected: every **30 seconds**
- On WebSocket connect event: immediate refresh

### 4.2 Pane Discovery

When a session is selected in the UI, the server queries pane layout:

```bash
tmux list-panes -t {session_name} -F "#{pane_id}|#{pane_index}|#{pane_width}|#{pane_height}|#{pane_active}|#{pane_title}"
```

This is called on session selection and refreshed on a 5s interval while the session view is open.

### 4.3 Session Registry Lifecycle

- **New session detected:** add to registry; bridge starts only when a client opens that session
- **Session disappears:** send close signal to connected WebSocket clients and tear down any active bridge
- **Server startup:** enumerate existing sessions and populate registry
- **Server shutdown:** close active control-mode bridges cleanly
---

## 5. API Specification

### 5.1 REST Endpoints

#### `GET /`

Returns dashboard HTML page (single-page app).

#### `GET /api/hosts`

Returns JSON list of configured hosts.

#### `POST /api/hosts`

Add a new host. Body: `{host_id, hostname, ssh_user, ssh_port?}`.

#### `DELETE /api/hosts/{host_id}`

Remove a host and tear down its active sessions.

#### `GET /api/hosts/{host_id}/sessions`

Returns JSON list of active sessions on the specified host.

**Response:**

```json
{
  "sessions": [
    {
      "host_id": "mac-mini",
      "name": "omp-instance-1",
      "windows": 2,
      "attached": false,
      "created_epoch": 1700000000
    }
  ],
  "total": 5,
  "page": 1,
  "page_size": 8,
  "pages": 1
}
```

**Query params:** `?page=1&page_size=8`

#### `POST /api/hosts/{host_id}/sessions`

Create a new session on the specified host.

#### `DELETE /api/hosts/{host_id}/sessions/{session_name}`

Kill a session on the specified host.


#### `GET /api/health`

Returns server status and session count. Used for liveness checks.

#### Template Management

##### `GET /api/templates`
Returns all templates with extracted macro variable names.

##### `POST /api/templates`
Create a new template. Body: `{template_name, name, directory, layout_type, layout_spec, pane_commands}`.
Validates macro placeholders in all content fields.

##### `PUT /api/templates/{template_name}`
Update all content fields of an existing template.

##### `PATCH /api/templates/{template_name}`
Rename a template. Body: `{"new_name": "..."}`. Returns updated entry.

##### `DELETE /api/templates/{template_name}`
Delete a template by name.

##### `POST /api/hosts/{host_id}/sessions/from-template`
Create session by rendering a template. Body: `{template_name, variables: {var: value}, pane_commands?}`.
All template variables must be provided with non-empty values.

### 5.2 Terminal WebSocket Bridge

Terminal traffic uses a dedicated WebSocket endpoint:
`/ws/hosts/{host_id}/sessions/{session_name}`.

- Browser clients connect directly to this endpoint (no reverse proxy subroute).
- The server binds the socket to the corresponding host/session control-mode bridge.
- Outbound frames carry pane-targeted terminal output for per-pane xterm instances.
- Inbound frames carry user input, resize events, and pane focus/mouse actions.
- Only the dashboard port (7680) needs to be exposed.
---

## 6. Frontend Specification

### 6.1 Dashboard View (Session List)

**URL:** `/`

**Layout:**

- Full-viewport dark-themed page
- Header: application title, active session count, last-refreshed timestamp
- Session grid: cards arranged in a responsive grid (2–3 columns)
- Pagination controls if session count > `page_size` (default 8)

**Session card contents:**

- Session name (large, monospaced)
- Window count badge
- Attached indicator (green dot if currently attached elsewhere)
- Time since creation
- "Open" button → navigates to session view

**Auto-refresh:** Session list polls `GET /api/sessions` every 10 seconds passively; immediately on page load.

### 6.2 Session View (Terminal)

**URL:** `/?host={host_id}&session={session_name}`

**Layout:**

- Back button → returns to session list
- Session name in header with actions menu
- Pane grid with one xterm.js instance per tmux pane
- Terminal data connects via WebSocket at `/ws/hosts/{host_id}/sessions/{session_name}`
**Keyboard handling:** All input passes through to xterm.js. No dashboard-level shortcuts.

### 6.3 Visual Design

- **Theme:** Dark (terminal-native aesthetic), e.g. dark grey `#1a1a1a` background, `#f0f0f0` text
- **Font:** System monospace stack (`"SF Mono", "Menlo", "Consolas", monospace`)
- **Accent colour:** Single accent (e.g. `#4fc3f7` light blue) for active states and focus borders
- **No external CSS frameworks** — pure CSS, small footprint
- **No JavaScript frameworks** — vanilla JS with `fetch` and `EventSource` only

---

## 7. Process Management

### 7.1 Control-Mode Bridge Allocation

- No per-session port pool is used.
- Bridge instances are keyed by `{host_id, session_name}` and multiplex over dashboard port `7680`.
- Bridge lifecycle is demand-driven: start on first viewer, stop when no viewers remain or session ends.
- Runtime bridge state is kept in memory (not persisted across server restarts).

### 7.2 Bridge Launch Command

```bash
tmux -CC -u attach-session -t {session_name}
```

**Flags:**

- `-CC` — enables control mode for structured pane/event stream
- `-u` — forces UTF-8 mode for full terminal fidelity

For remote hosts, the server runs the equivalent command over SSH.

### 7.3 Process Lifecycle

```
Server starts
    │
    ├─► enumerate tmux sessions
    └─► start polling loop

User opens session view
    ├─► start (or reuse) control-mode bridge for {host_id, session_name}
    └─► attach WebSocket clients to pane streams

Polling tick
    ├─► tmux ls → compare to registry
    ├─► new sessions: add to registry
    └─► gone sessions: close bridge + emit close event to UI

Server stops (SIGTERM/SIGINT)
    └─► close all active control-mode bridges
```

### 7.4 launchd Configuration

**Plist location:** `~/Library/LaunchAgents/com.user.panoptic.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "...">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.panoptic</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/panoptic/server.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/path/to/panoptic/logs/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/panoptic/logs/stderr.log</string>
    <key>WorkingDirectory</key>
    <string>/path/to/panoptic</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>/Users/yourusername</string>
    </dict>
</dict>
</plist>
```

**launchd commands:**

```bash
# Load and start
launchctl load ~/Library/LaunchAgents/com.user.panoptic.plist

# Stop and unload
launchctl unload ~/Library/LaunchAgents/com.user.panoptic.plist

# Check status
launchctl list | grep panoptic

# View logs
tail -f /path/to/panoptic/logs/stdout.log
```

---

## 8. Network and Access

### 8.1 Access Pattern

All browser traffic flows through the dashboard port:

```
Browser
  └─► https://<host>:7680/                                        ← dashboard + APIs + static assets
  └─► wss://<host>:7680/ws/hosts/{host_id}/sessions/{session_name} ← terminal stream
```

Terminal rendering uses WebSocket frames from tmux control-mode bridges.
Only port 7680 needs to be reachable.
### 8.2 Dashboard Server Binding

The dashboard binds to `127.0.0.1` by default.  Set `DASHBOARD_HOST = "0.0.0.0"`
(or the Tailscale interface IP) to make it reachable from other machines.
Since Tailscale creates a private encrypted tunnel, no additional TLS or
authentication is required for personal use.

### 8.3 Firewall Considerations

- Only port 7680 needs to be open (terminal WebSocket uses same port)
- No port-forwarding on home router required (Tailscale is peer-to-peer)
- Optionally restrict to Tailscale interface only using `100.x.x.x` bind address
---

## 9. Efficiency When Idle

The following measures keep resource usage low when the dashboard has no active users:

| Mechanism                | Idle behaviour                                 | Active behaviour                         |
| ------------------------ | ---------------------------------------------- | ---------------------------------------- |
| Session polling interval | 30s                                            | 5s                                       |
| Client tracking          | Server counts active WebSocket/SSE connections | Switches to active mode on first connect |
| Bridge instances         | None when no session view is open               | Active only for viewed sessions           |
| Log rotation             | Handled by launchd/logrotate                   | —                                        |
| Python event loop        | `asyncio` sleep between polls                  | Immediate response to requests           |

Control-mode bridges are spawned on demand and shut down when unused, so idle overhead remains low even with many discovered sessions.

---

## 10. Project File Structure

```
panoptic/
├── server.py              # Main aiohttp server, route wiring
├── session_manager.py     # tmux session discovery + control-bridge lifecycle
├── host_config.py         # Host registry: add/remove/list remote hosts
├── config.py              # Configuration constants
├── panoptic_cli.py        # CLI entry point (serve, add-host, etc.)
├── template_store.py      # Template persistence (JSON-backed CRUD)
├── template_macros.py     # Macro placeholder validation, extraction, rendering
├── static/
│   ├── index.html         # Single-page dashboard
│   ├── app.js             # Frontend logic (vanilla JS)
│   └── style.css          # Dark theme styles
├── logs/
│   ├── stdout.log
│   └── stderr.log
├── hosts.json             # Persisted host registry
├── templates.json         # Persisted session templates
├── panoptic.service       # systemd unit template (Linux)
├── com.user.panoptic.plist  # launchd plist template (macOS)
├── setup-service.sh       # Sets up panoptic as a background launchd service
└── README.md
```

---

## 11. Configuration

All configuration via constants in `config.py` (no external config file needed for a personal tool):

```python
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 7680
CONTROL_BRIDGE_COLS = 220          # default width for new pane streams
CONTROL_BRIDGE_ROWS = 60           # default height for new pane streams
POLL_INTERVAL_ACTIVE = 5           # seconds, when clients connected
POLL_INTERVAL_IDLE = 30            # seconds, when no clients
SESSION_PAGE_SIZE = 8
TERMINAL_FONT_FAMILY = "monospace"  # xterm.js font family
LOG_LEVEL = "INFO"
HOSTS_CONFIG_PATH = "hosts.json"  # path to host registry
TEMPLATES_CONFIG_PATH = "templates.json"  # path to template store
SSH_CONNECT_TIMEOUT = 10           # seconds for SSH connection attempts
BEAMUX_BINARY = "beamux"           # remote tmux session launcher
CLIENT_ACTIVE_TIMEOUT = 30         # seconds before client considered inactive
CLIENT_DEEP_IDLE_TIMEOUT = 300     # seconds before switching to deep-idle polling
```

---

## 12. Error Handling and Edge Cases

| Scenario                              | Expected behaviour                                                                                           |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| tmux not running                      | API returns empty session list; UI shows "No sessions found"                                                 |
| control bridge launch fails            | Server logs error and returns terminal connection failure for the requested session                      |
| Session ends while UI open             | Bridge emits close event; browser disconnects session cleanly                                            |
| Bridge unavailable or disconnected      | WebSocket closes with explicit error payload; UI can retry connection                                    |
| Server restart with existing sessions   | Re-enumerates tmux; bridges are recreated on demand when clients reconnect                                |
| Browser WebSocket reconnect             | Client reconnects to `/ws/...`; server reattaches to active bridge or starts a new one as needed         |
| Pane renderer initialization mismatch   | UI falls back to configured `CONTROL_BRIDGE_COLS/ROWS`; resize event corrects dimensions after connect   |

---

## 13. Implementation Sequence (Suggested for OMP)

1. **`config.py`** — configuration constants
2. **`session_manager.py`** — tmux polling, control-bridge start/stop, bridge client fan-out
3. **`server.py`** — aiohttp server wiring up REST API endpoints, WebSocket terminal endpoint, static file serving, client tracking for idle mode
4. **`static/style.css`** — dark terminal theme
5. **`static/index.html`** — dashboard shell, session card template
6. **`static/app.js`** — fetch-based session list, polling, pane layout renderer, per-pane xterm management
7. **`com.user.panoptic.plist`** — launchd plist with correct paths
8. **`setup-service.sh`** — install Python deps, register and start launchd service
9. **`README.md`** — setup instructions, tailscale access guide

---

## 14. Out of Scope / Future Extensions

These are explicitly out of scope for the initial build but noted for future iteration:

- ~~**Session creation UI**~~ — implemented via template rendering (`POST /api/hosts/{host_id}/sessions/from-template`)
- **Named session aliases** — friendly names mapped to tmux session names
- **Read-only mode** — view-only access per session
- **Notification badges** — detect OMP agent output/activity and highlight in session list
- **Log capture** — snapshot terminal scrollback to a file from the UI
- **HTTPS** — could be added via Tailscale HTTPS cert or local nginx with self-signed cert
- **Window switcher** — navigate between tmux windows within a session (currently attaches to default window)
