# tmux-dash

A lightweight web dashboard that discovers your tmux sessions and exposes each one as a live terminal in the browser via [ttyd](https://github.com/tsl0922/ttyd).

<!-- screenshot: dashboard view -->

## Features

- **Automatic session discovery** — polls tmux every 5 s (active) / 30 s (idle), no manual registration
- **Multi-pane terminal** — each session opens in a full ttyd terminal inside the browser; up to 19 concurrent sessions
- **Auto-refresh** — dashboard reflects session create/destroy in real time without a page reload
- **Dark theme** — clean, low-distraction UI
- **Always-on via launchd** — survives reboots; crashes restart automatically
- **Remote access** — bind on `0.0.0.0`; reach from any device on your Tailscale network

## Prerequisites

| Requirement | Notes |
|---|---|
| macOS | launchd integration is macOS-only |
| [Homebrew](https://brew.sh) | used to install `ttyd` |
| Python 3.11+ | standard macOS install or `brew install python` |
| tmux | `brew install tmux` |
| [Tailscale](https://tailscale.com) | optional — required only for remote access |

## Quick Start

```bash
git clone https://github.com/youruser/tmux-local-dash && cd tmux-local-dash
./install.sh
```

Then open:

- **Local:** `http://localhost:7680`
- **Remote (Tailscale):** `http://<tailscale-ip>:7680`

The install script installs dependencies, substitutes paths in the launchd plist, copies it to `~/Library/LaunchAgents/`, and loads the service.

## Manual Setup

If you prefer not to use `install.sh`:

```bash
# 1. Install ttyd
brew install ttyd

# 2. Install Python dependency
pip3 install aiohttp

# 3. Start the server (foreground)
python3 server.py
```

Open `http://localhost:7680`. Press `Ctrl+C` to stop.

## Configuration

All tuneable constants live in `config.py`:

| Constant | Default | Controls |
|---|---|---|
| `DASHBOARD_HOST` | `0.0.0.0` | Interface the dashboard binds on |
| `DASHBOARD_PORT` | `7680` | Dashboard HTTP port |
| `TTYD_PORT_RANGE_START` | `7681` | First port in the ttyd pool |
| `TTYD_PORT_RANGE_END` | `7699` | Last port in the ttyd pool (19 slots) |
| `TTYD_BIND_HOST` | `0.0.0.0` | Interface each ttyd process binds on |
| `TTYD_BINARY` | `ttyd` | Path or name of the ttyd executable |
| `POLL_INTERVAL_ACTIVE` | `5` | Seconds between polls when sessions are active |
| `POLL_INTERVAL_IDLE` | `30` | Seconds between polls when no sessions exist |
| `SESSION_PAGE_SIZE` | `8` | Sessions shown per page on the dashboard |
| `LOG_LEVEL` | `INFO` | Python logging level |

Edit `config.py` and restart the service for changes to take effect.

## Architecture

```
Browser
  │  HTTP GET /          → dashboard (index.html + app.js)
  │  HTTP GET /api/sessions → JSON list of live sessions
  │  WebSocket :768x      → ttyd terminal stream (one port per session)
  ▼
server.py  (aiohttp, port 7680)
  │
  ├── session_manager.py
  │     ├── polls `tmux list-sessions` every N seconds
  │     ├── spawns ttyd subprocess per session  (ports 7681–7699)
  │     └── kills ttyd when session disappears
  │
  └── static/
        index.html, app.js, style.css
```

Each tmux session gets its own `ttyd` process on a port drawn from the pool. The browser connects directly to that port via WebSocket for the terminal stream.

## launchd Management

The service label is `com.user.tmux-dash`.

```bash
# Load and start
launchctl load ~/Library/LaunchAgents/com.user.tmux-dash.plist

# Stop and unload
launchctl unload ~/Library/LaunchAgents/com.user.tmux-dash.plist

# Check status
launchctl list | grep tmux-dash

# Restart
launchctl unload ~/Library/LaunchAgents/com.user.tmux-dash.plist
launchctl load  ~/Library/LaunchAgents/com.user.tmux-dash.plist
```

### Log files

Logs are written to the install directory under `logs/`:

| File | Content |
|---|---|
| `logs/stdout.log` | Server output, session events |
| `logs/stderr.log` | Errors, tracebacks |

```bash
tail -f <install-dir>/logs/stdout.log
tail -f <install-dir>/logs/stderr.log
```

## Uninstall

```bash
./install.sh --uninstall
```

This unloads the launchd service and removes the plist from `~/Library/LaunchAgents/`. It does not delete the project directory or installed pip packages.

## License

Private project — no license granted.
