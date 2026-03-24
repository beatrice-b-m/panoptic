# Repository Guidelines

## Project Overview

`tmux-dash` is a browser-based tmux session monitor and terminal interface. It runs as a persistent local web server on macOS, discovers active tmux sessions, and exposes them as interactive terminals in the browser via [ttyd](https://github.com/nickthecook/ttyd). Designed as a single-user personal tool accessed exclusively over a Tailscale private network.

**Primary use case:** Monitoring and interacting with Oh-My-Pi (OMP) agentic coding agent sessions from a remote browser.

`SYSTEM_SPEC.md` is the authoritative design document.

## Architecture & Data Flow

Two process types cooperate:

```
Browser (Tailscale client)
  │
  ├─► tmux-dash server (Python, port 7680)
  │     ├── GET /                        → Dashboard HTML (SPA)
  │     ├── GET /api/sessions            → Session list JSON (paginated)
  │     ├── GET /api/sessions/{name}/panes → Pane layout JSON
  │     ├── GET /api/health              → Liveness check
  │     └── GET /static/*               → CSS, JS assets
  │
  └─► ttyd processes (ports 7681–7699, one per tmux session)
        └── Each runs: ttyd tmux attach-session -t {name}
            Browser embeds as <iframe> pointed at ttyd's built-in xterm.js UI
```

**Key flow:**
1. Server polls `tmux list-sessions` on interval (5s active / 30s idle)
2. New session detected → spawn `ttyd` on next free port from pool
3. Session disappears → kill `ttyd`, release port, notify connected clients
4. Frontend fetches `/api/sessions`, renders cards; clicking a session fetches pane layout and embeds ttyd iframes sized to match tmux geometry

No WebSocket proxying — browser connects to ttyd ports directly over Tailscale.

## Key Directories

```
tmux-dash/
├── server.py              # Main aiohttp server: routes, static serving, client tracking
├── session_manager.py     # tmux polling, ttyd spawn/kill, port pool management
├── config.py              # All configuration constants (ports, intervals, paths)
├── static/
│   ├── index.html         # Single-page dashboard shell
│   ├── app.js             # Vanilla JS: fetch, polling, pane layout, iframe management
│   └── style.css          # Dark terminal theme (no frameworks)
├── logs/                  # stdout.log, stderr.log (launchd-managed)
├── com.user.tmux-dash.plist  # launchd plist template
├── install.sh             # Setup: brew deps, pip deps, launchd registration
├── SYSTEM_SPEC.md         # Authoritative design document
├── .env                   # Environment overrides (gitignored)
└── .env.example           # Template for .env
```

## Important Files

| File | Purpose |
|---|---|
| `SYSTEM_SPEC.md` | Full design specification — the source of truth for all behavior |
| `config.py` | Central configuration constants; no external config file |
| `session_manager.py` | Core logic: tmux discovery, ttyd lifecycle, port pool |
| `server.py` | HTTP server wiring: REST endpoints, static files, idle/active mode |
| `static/app.js` | Frontend logic: session list, pane renderer, iframe embedding |
| `com.user.tmux-dash.plist` | macOS launchd service definition |
| `install.sh` | One-shot setup script |

## Runtime & Tooling

| Concern | Choice |
|---|---|
| Language | Python 3.11+ (system Python on macOS) |
| HTTP framework | `aiohttp` (async, low idle overhead) |
| Terminal server | `ttyd` (Homebrew: `brew install ttyd`) |
| Frontend | Vanilla HTML/CSS/JS — no build step, no npm, no frameworks |
| Terminal renderer | xterm.js (bundled with ttyd, not managed by this project) |
| Process manager | `launchd` (macOS-native, survives reboot) |
| Package manager | `pip3` for Python deps |

**No `package.json`, no `tsconfig.json`, no bundler.** The frontend is served as static files directly.

### System Dependencies

```bash
brew install ttyd
pip3 install aiohttp
```

## Configuration

All config lives in `config.py` as module-level constants:

```python
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 7680
TTYD_PORT_RANGE_START = 7681
TTYD_PORT_RANGE_END = 7699          # Max 19 concurrent sessions
TTYD_BIND_HOST = "0.0.0.0"         # "127.0.0.1" to restrict to loopback
POLL_INTERVAL_ACTIVE = 5            # seconds, when clients connected
POLL_INTERVAL_IDLE = 30             # seconds, when no clients
SESSION_PAGE_SIZE = 8
TTYD_BINARY = "ttyd"                # or absolute: /opt/homebrew/bin/ttyd
LOG_LEVEL = "INFO"
```

No external config file or YAML — constants in a Python module.

## Code Conventions & Common Patterns

### Python Server

- **Async everywhere.** Server uses `asyncio` event loop via `aiohttp`. All I/O (tmux polling, process management, HTTP handlers) must be async.
- **tmux interaction** via subprocess calls to `tmux list-sessions` and `tmux list-panes` with `-F` format strings. Parse structured output, not freeform text.
- **Session registry** is an in-memory dict keyed by session name. Not persisted — rebuilt on server restart.
- **Port pool** is a simple range allocator (`7681`–`7699`). Assigned sequentially, released on session teardown.
- **Client tracking** counts active connections to switch between active/idle polling intervals.
- **ttyd spawning** via `subprocess` with specific flags:
  ```bash
  ttyd --port {port} --interface {host} --once --writable \
       --title-format "tmux: {name}" tmux attach-session -t {name}
  ```
- **Error handling:** Log and continue. Never crash the server for a single session failure. Missing tmux → empty list. Missing ttyd binary → log error, show warning banner. Dead ttyd → respawn on next poll tick.

### Frontend (Vanilla JS)

- **No frameworks.** No React, no Vue, no jQuery. Plain `fetch()` for API calls, DOM manipulation for rendering.
- **No build step.** Files in `static/` are served as-is.
- **Dark theme.** Background `#1a1a1a`, text `#f0f0f0`, accent `#4fc3f7`. Monospace font stack: `"SF Mono", "Menlo", "Consolas", monospace`.
- **Session cards** in a responsive CSS grid (2-3 columns). Each card shows: session name (monospaced), window count, attached indicator, creation time, "Open" button.
- **Pane layout** rendered as a CSS grid matching actual tmux pane geometry (width/height from `tmux list-panes`). Each pane is an `<iframe>` pointing at the ttyd URL.
- **Auto-refresh:** Session list polls every 10s; pane layout polls every 5s.
- **Keyboard:** All input passes through to xterm.js in the focused iframe. No dashboard-level shortcuts.

### General

- **No authentication/TLS.** Tailscale handles network security.
- **No multi-user support.** Single-user personal tool.
- **Indentation:** Follow Python community standard (4 spaces for Python, 2 spaces or tabs for HTML/CSS/JS — match whichever is established first).

## Development Commands

```bash
# Install system dependencies
brew install ttyd
pip3 install aiohttp

# Run server directly (development)
python3 server.py

# Test webhook endpoint manually
curl -s http://localhost:7680/api/health

# Test session API
curl -s http://localhost:7680/api/sessions | python3 -m json.tool

# launchd management
launchctl load ~/Library/LaunchAgents/com.user.tmux-dash.plist
launchctl unload ~/Library/LaunchAgents/com.user.tmux-dash.plist
launchctl list | grep tmux-dash
```

## Version Control

Every completed task **MUST** be committed immediately with a granular, descriptive commit message. This is non-negotiable.

- **One logical change per commit.** Do not batch unrelated work into a single commit.
- **Commit message format:** imperative mood, concise subject line describing *what* changed and *why* when non-obvious. Examples:
  - `Add config.py with port pool and polling interval constants`
  - `Implement session discovery polling via tmux list-sessions`
  - `Fix ttyd respawn race when session ends during poll tick`
- **Commit after each task completes** — not at the end of a session, not in bulk. If a task touched files, it gets its own commit before moving to the next task.
- **Never leave working changes uncommitted** when moving on to the next piece of work.

## Testing & QA

No test framework is specified. Verification is manual and scenario-based:

| Scenario | How to verify |
|---|---|
| Server starts | `python3 server.py` runs without error; `/api/health` returns 200 |
| Session discovery | Create a tmux session; `/api/sessions` includes it within one poll interval |
| ttyd spawning | Session appears → ttyd process visible in `ps aux \| grep ttyd` on assigned port |
| Session teardown | Kill tmux session → ttyd process cleaned up, port released |
| Dashboard renders | Open `http://localhost:7680` in browser; session cards appear |
| Terminal interaction | Click session card → pane iframes load → keyboard input works |
| Idle efficiency | Disconnect all clients → poll interval slows to 30s (visible in logs) |
| Port exhaustion | >19 sessions → excess marked "unavailable" in UI, warning logged |
| Graceful restart | Stop/start server → re-discovers existing sessions, re-spawns ttyd |

## Error Handling & Edge Cases

Reference `SYSTEM_SPEC.md` §12 for the full matrix. Key behaviors:

- **tmux not running:** API returns empty session list; UI shows "No sessions found"
- **ttyd binary missing:** Server logs error at startup; dashboard shows warning banner
- **Session ends while UI open:** Detect iframe disconnect, show "Session ended" overlay, return to list after 3s
- **Port pool exhausted:** Log warning; excess sessions listed as "unavailable"
- **ttyd dies unexpectedly:** Session watcher detects missing PID on next tick; attempts respawn