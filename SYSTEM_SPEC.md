# System Specification: tmux Web Dashboard

**Project:** `tmux-dash` — Browser-based tmux session monitor and terminal interface  
**Target platform:** macOS (Mac Mini, Apple Silicon or Intel)  
**Access method:** Tailscale private network  
**Primary use case:** Monitoring and interacting with Oh-My-Pi (OMP) agentic coding agent sessions

---

## 1. Overview

`tmux-dash` is a persistent local web server running on a Mac Mini that exposes all active tmux sessions as interactive browser terminals. It provides a session-picker dashboard and full terminal interaction (keyboard and mouse) for multi-pane tmux sessions. It is designed to be always-on, resource-efficient when idle, and accessible exclusively over a Tailscale private network.

---

## 2. Goals and Non-Goals

### Goals

- Always-running service with automatic startup via `launchd`
- Real-time discovery of active tmux sessions (no manual registration)
- Browser UI: paginated session list → click into session → full multi-pane terminal view
- Full keyboard input and mouse support in terminal panes
- Efficient resource usage when no client is connected
- Safe for Tailscale-only access (no public internet exposure)

### Non-Goals

- Authentication/TLS (Tailscale handles this)
- Session creation or tmux configuration management
- Mobile-optimised layout (desktop browser is primary)
- Multi-user simultaneous access (single-user personal tool)

---

## 3. Architecture

### 3.1 Component Overview

```
Mac Mini
├── tmux-dash server (Python, port 7680)
│   ├── Dashboard HTTP endpoint    GET /
│   ├── Session API endpoint       GET /api/sessions
│   ├── WebSocket terminal proxy   WS  /ws/{session_name}/{pane_id}
│   └── Static assets              GET /static/*
└── ttyd processes (one per active tmux session, ports 7681–7699)
    └── spawned/killed dynamically by session watcher
```

### 3.2 Process Roles

**tmux-dash server** (always running, `launchd`-managed):

- Serves the dashboard HTML/JS frontend
- Maintains a session registry by polling `tmux ls` on a configurable interval
- Spawns and tracks one `ttyd` process per discovered session
- Tears down `ttyd` processes when sessions disappear
- Idles gracefully: polling slows to a long interval (~30s) when no WebSocket clients are connected

**ttyd processes** (one per session, managed by tmux-dash):

- Each instance attaches to a specific tmux session: `ttyd tmux attach -t {session_name}`
- Bound to `127.0.0.1` only (not exposed directly; proxied by the dashboard server or accessed via port)
- Killed and cleaned up when the tmux session ends

### 3.3 Technology Stack

| Layer              | Choice                              | Rationale                             |
| ------------------ | ----------------------------------- | ------------------------------------- |
| Server language    | Python 3.11+                        | Available on macOS, minimal deps      |
| HTTP/WS framework  | `aiohttp` or `FastAPI` + `uvicorn`  | Async, low idle overhead              |
| Terminal server    | `ttyd` (Homebrew)                   | Production-quality, xterm.js built-in |
| Frontend           | Vanilla HTML/CSS/JS (no build step) | Simple, maintainable, no npm required |
| Terminal renderer  | xterm.js (bundled with ttyd)        | Full VT emulation, mouse support      |
| Process management | `launchd` plist                     | macOS-native, survives login/reboot   |

**Install dependencies:**

```bash
brew install ttyd
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

- **New session detected:** spawn `ttyd` process for it, assign port from pool
- **Session disappears:** send close signal to connected WebSocket clients, kill `ttyd` process, release port
- **Server startup:** enumerate existing sessions, spawn `ttyd` for each
- **Server shutdown:** kill all child `ttyd` processes cleanly

---

## 5. API Specification

### 5.1 REST Endpoints

#### `GET /`

Returns dashboard HTML page (single-page app).

#### `GET /api/sessions`

Returns JSON list of active sessions.

**Response:**

```json
{
  "sessions": [
    {
      "name": "omp-instance-1",
      "windows": 2,
      "attached": false,
      "created_epoch": 1700000000,
      "port": 7681
    },
    ...
  ],
  "total": 5,
  "page": 1,
  "page_size": 8,
  "pages": 1
}
```

**Query params:** `?page=1&page_size=8`

#### `GET /api/sessions/{session_name}/panes`

Returns pane layout for a session.

**Response:**

```json
{
	"session": "omp-instance-1",
	"panes": [
		{
			"id": "%0",
			"index": 0,
			"width": 220,
			"height": 50,
			"active": true,
			"title": "bash",
			"ttyd_url": "http://localhost:7681"
		}
	]
}
```

#### `GET /api/health`

Returns server status and session count. Used for liveness checks.

### 5.2 WebSocket (via ttyd)

Each `ttyd` process handles its own WebSocket connections at `ws://localhost:{port}/ws`. The dashboard frontend embeds the ttyd web UI via `<iframe>` pointed at `http://{tailscale-ip}:{port}` for full xterm.js terminal functionality.

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

**URL:** `/{session_name}` or `/?session={session_name}`

**Layout:**

- Back button → returns to session list
- Session name in header
- Pane layout rendered as a CSS grid matching the actual tmux pane geometry
- Each pane rendered as an `<iframe>` embedding the ttyd UI for that session/pane

**Pane rendering:**

- Pane iframes sized proportionally to actual tmux pane dimensions (width/height from `tmux list-panes`)
- Active pane highlighted with a visible border
- Clicking a pane focuses that iframe for keyboard input
- Pane titles shown in small label above each pane

**Keyboard handling:**

- Default keyboard input goes to whichever pane iframe is focused
- Tab/click to switch focus between panes
- No keyboard shortcuts captured at the dashboard level (all input passes through to xterm.js)

**Mouse handling:**

- Mouse events forwarded to xterm.js within each pane iframe natively
- Scroll in pane scrolls terminal history

**Auto-refresh:** Pane layout refreshes every 5s to detect pane splits/closures while session view is open.

### 6.3 Visual Design

- **Theme:** Dark (terminal-native aesthetic), e.g. dark grey `#1a1a1a` background, `#f0f0f0` text
- **Font:** System monospace stack (`"SF Mono", "Menlo", "Consolas", monospace`)
- **Accent colour:** Single accent (e.g. `#4fc3f7` light blue) for active states and focus borders
- **No external CSS frameworks** — pure CSS, small footprint
- **No JavaScript frameworks** — vanilla JS with `fetch` and `EventSource` only

---

## 7. Process Management

### 7.1 ttyd Port Allocation

- Port pool: `7681` to `7699` (supports up to 19 concurrent sessions)
- Ports assigned sequentially from pool on session discovery
- Released back to pool when session ends
- Port assignments stored in memory (not persisted; re-assigned on server restart)

### 7.2 ttyd Launch Command

```bash
ttyd \
  --port {port} \
  --interface 127.0.0.1 \
  --once \
  --writable \
  --title-format "tmux: {session_name}" \
  tmux attach-session -t {session_name}
```

**Flags:**

- `--interface 127.0.0.1` — bind only to loopback; dashboard server or direct Tailscale access handles routing
- `--writable` — allow keyboard input (not read-only)
- `--once` — ttyd exits when the terminal session ends (tmux detach or session close)

> **Note:** If direct iframe access over Tailscale is used (recommended, see §8), remove `--interface 127.0.0.1` and bind to `0.0.0.0` instead, so the Tailscale IP can reach each port directly.

### 7.3 Process Lifecycle

```
Server starts
    │
    ├─► enumerate tmux sessions
    ├─► for each session: spawn ttyd, record PID + port
    └─► start polling loop

Polling tick
    ├─► tmux ls → compare to registry
    ├─► new sessions: spawn ttyd
    └─► gone sessions: kill ttyd, emit close event to UI

Server stops (SIGTERM/SIGINT)
    └─► kill all child ttyd PIDs
```

### 7.4 launchd Configuration

**Plist location:** `~/Library/LaunchAgents/com.user.tmux-dash.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "...">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.tmux-dash</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/tmux-dash/server.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/path/to/tmux-dash/logs/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/tmux-dash/logs/stderr.log</string>
    <key>WorkingDirectory</key>
    <string>/path/to/tmux-dash</string>
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
launchctl load ~/Library/LaunchAgents/com.user.tmux-dash.plist

# Stop and unload
launchctl unload ~/Library/LaunchAgents/com.user.tmux-dash.plist

# Check status
launchctl list | grep tmux-dash

# View logs
tail -f /path/to/tmux-dash/logs/stdout.log
```

---

## 8. Network and Access

### 8.1 Tailscale Access Pattern

The recommended access pattern uses direct browser access to ttyd ports over Tailscale:

```
Your device (Tailscale client)
    │
    └─► https://mac-mini.tailnet.ts.net:7680   ← dashboard
    └─► http://mac-mini.tailnet.ts.net:7681    ← ttyd for session 1
    └─► http://mac-mini.tailnet.ts.net:7682    ← ttyd for session 2
    ...
```

The dashboard page references ttyd iframes using the Tailscale hostname/IP directly (served to the browser at page load time). The server API returns the full URL for each session's ttyd endpoint.

### 8.2 Dashboard Server Binding

Bind the dashboard server to `0.0.0.0` (all interfaces) or explicitly to the Tailscale interface IP. Since Tailscale creates a private encrypted tunnel, no additional TLS or authentication is required for personal use.

```python
# Bind to all interfaces; Tailscale firewall handles access control
HOST = "0.0.0.0"
DASHBOARD_PORT = 7680
```

### 8.3 Firewall Considerations

- macOS firewall may prompt on first run for each port — accept or pre-configure
- No port-forwarding on home router required (Tailscale is peer-to-peer)
- Optionally restrict to Tailscale interface only using `100.x.x.x` bind address

---

## 9. Efficiency When Idle

The following measures keep resource usage low when the dashboard has no active users:

| Mechanism                | Idle behaviour                                 | Active behaviour                         |
| ------------------------ | ---------------------------------------------- | ---------------------------------------- |
| Session polling interval | 30s                                            | 5s                                       |
| Client tracking          | Server counts active WebSocket/SSE connections | Switches to active mode on first connect |
| ttyd processes           | Persistent (low idle CPU) per session          | Full terminal I/O on demand              |
| Log rotation             | Handled by launchd/logrotate                   | —                                        |
| Python event loop        | `asyncio` sleep between polls                  | Immediate response to requests           |

`ttyd` itself is very lightweight at idle (it is essentially a sleeping process waiting for a WebSocket connection). With 10 sessions, expect ~10 idle `ttyd` processes consuming negligible CPU and a few MB each.

---

## 10. Project File Structure

```
tmux-dash/
├── server.py              # Main aiohttp/FastAPI server
├── session_manager.py     # tmux session discovery + ttyd lifecycle
├── config.py              # Configuration constants
├── static/
│   ├── index.html         # Single-page dashboard
│   ├── app.js             # Frontend logic (vanilla JS)
│   └── style.css          # Dark theme styles
├── logs/
│   ├── stdout.log
│   └── stderr.log
├── com.user.tmux-dash.plist  # launchd plist template
├── install.sh             # Setup script (brew deps, launchd registration)
└── README.md
```

---

## 11. Configuration

All configuration via constants in `config.py` (no external config file needed for a personal tool):

```python
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 7680
TTYD_PORT_RANGE_START = 7681
TTYD_PORT_RANGE_END = 7699
TTYD_BIND_HOST = "0.0.0.0"       # Set to "127.0.0.1" to restrict ttyd to loopback
POLL_INTERVAL_ACTIVE = 5          # seconds, when clients connected
POLL_INTERVAL_IDLE = 30           # seconds, when no clients
SESSION_PAGE_SIZE = 8
TTYD_BINARY = "ttyd"              # or absolute path: /opt/homebrew/bin/ttyd
LOG_LEVEL = "INFO"
```

---

## 12. Error Handling and Edge Cases

| Scenario                              | Expected behaviour                                                                                           |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| tmux not running                      | API returns empty session list; UI shows "No sessions found"                                                 |
| ttyd binary not found                 | Server logs error on startup; dashboard shows warning banner                                                 |
| Session ends while UI open            | UI detects pane closure (iframe disconnect), shows "Session ended" overlay, returns to session list after 3s |
| Port pool exhausted (>19 sessions)    | Log warning; excess sessions listed in UI as "unavailable" with port count shown                             |
| Server restart with existing sessions | Re-enumerates tmux; re-spawns ttyd for all active sessions                                                   |
| ttyd process dies unexpectedly        | Session watcher detects missing PID on next tick; attempts respawn                                           |
| Browser iframe CORS                   | All resources served from same origin or known Tailscale hostname; no cross-origin issues expected           |

---

## 13. Implementation Sequence (Suggested for OMP)

1. **`config.py`** — configuration constants
2. **`session_manager.py`** — tmux polling, ttyd spawn/kill, port pool
3. **`server.py`** — aiohttp server wiring up REST API endpoints, static file serving, client tracking for idle mode
4. **`static/style.css`** — dark terminal theme
5. **`static/index.html`** — dashboard shell, session card template
6. **`static/app.js`** — fetch-based session list, polling, pane layout renderer, iframe management
7. **`com.user.tmux-dash.plist`** — launchd plist with correct paths
8. **`install.sh`** — brew install ttyd, pip install deps, launchd load
9. **`README.md`** — setup instructions, tailscale access guide

---

## 14. Out of Scope / Future Extensions

These are explicitly out of scope for the initial build but noted for future iteration:

- **Session creation UI** — creating new `tmux new-session` from the browser
- **Named session aliases** — friendly names mapped to tmux session names
- **Read-only mode** — view-only access per session
- **Notification badges** — detect OMP agent output/activity and highlight in session list
- **Log capture** — snapshot terminal scrollback to a file from the UI
- **HTTPS** — could be added via Tailscale HTTPS cert or local nginx with self-signed cert
- **Window switcher** — navigate between tmux windows within a session (currently attaches to default window)
