# tmux-dash

A lightweight web dashboard that discovers your tmux sessions and exposes each one as a live terminal in the browser via [ttyd](https://github.com/tsl0922/ttyd).

<!-- screenshot: dashboard view -->

## Features

- **Automatic session discovery** -- polls tmux every 5 s (active) / 30 s (idle), no manual registration
- **Single terminal view** -- clicking a session opens one full ttyd terminal showing the real tmux layout (panes, splits, status bar); up to 19 concurrent sessions
- **Session thumbnails** -- each gallery card shows a live text snapshot (SVG) of the session, refreshed approximately every 30 seconds
- **Auto-refresh** -- dashboard reflects session create/destroy in real time without a page reload
- **Light/dark theme** -- toggle in the header; persists to localStorage; follows `prefers-color-scheme` when no explicit choice is stored
- **HTTPS via Tailscale** -- optional TLS termination using Tailscale-provisioned certificates; all ttyd traffic is reverse-proxied through a single port
- **Configurable terminal font** -- terminal font family defaults to Hack Font Mono and is overridable via environment variable
- **Always-on via launchd** -- survives reboots; crashes restart automatically
- **Remote access** -- bind on `0.0.0.0`; reach from any device on your Tailscale network
- **Create sessions from UI** -- click "+New" to spawn a tmux session with optional working directory and pane layout
- **Pane layout support** -- row or column layouts via colon-separated spec (e.g. `2:1:3`); live CSS grid preview before creation
- **Directory autocompletion** -- server-side path completion when typing a working directory for new sessions

## Prerequisites

| Requirement | Notes |
|---|---|
| macOS | launchd integration is macOS-only |
| [Homebrew](https://brew.sh) | used to install `ttyd` |
| Python 3.9+ | standard macOS install or `brew install python` |
| tmux | `brew install tmux` |
| [Tailscale](https://tailscale.com) | optional -- required only for remote/HTTPS access |

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

All tuneable constants live in `config.py`. Most can also be set via environment variable.

| Constant | Default | Env Override | Controls |
|---|---|---|---|
| `DASHBOARD_HOST` | `0.0.0.0` | -- | Interface the dashboard binds on |
| `DASHBOARD_PORT` | `7680` | -- | Dashboard HTTP port |
| `TTYD_PORT_RANGE_START` | `7681` | -- | First port in the ttyd pool |
| `TTYD_PORT_RANGE_END` | `7699` | -- | Last port in the ttyd pool (19 slots) |
| `TTYD_BIND_HOST` | `0.0.0.0` | -- | Interface each ttyd process binds on |
| `TTYD_BINARY` | `ttyd` | -- | Path or name of the ttyd executable |
| `TTYD_FONT_FAMILY` | `Hack Font Mono, Menlo, ...` | `TTYD_FONT_FAMILY` | Font family passed to ttyd terminals |
| `POLL_INTERVAL_ACTIVE` | `5` | -- | Seconds between polls when clients are connected |
| `POLL_INTERVAL_IDLE` | `30` | -- | Seconds between polls when no clients are connected |
| `SESSION_PAGE_SIZE` | `8` | -- | Sessions shown per page on the dashboard |
| `TLS_CERT` | *(empty)* | `TLS_CERT` | Path to TLS certificate file (PEM) |
| `TLS_KEY` | *(empty)* | `TLS_KEY` | Path to TLS private key file (PEM) |
| `LOG_LEVEL` | `INFO` | -- | Python logging level |
| `BEAMUX_BINARY` | `~/AgentFiles/.../beamux` | `BEAMUX_BINARY` | Path to beamux script for pane layout creation |

Edit `config.py` or set environment variables and restart the service for changes to take effect.

### Terminal Font

The terminal font defaults to **Hack Font Mono** with a fallback chain of Menlo, Consolas, and generic monospace. Override it via the `TTYD_FONT_FAMILY` environment variable:

```bash
export TTYD_FONT_FAMILY="JetBrains Mono, Fira Code, monospace"
python3 server.py
```

The font must be installed on the **client device** (the browser). The setting is passed to each ttyd instance at spawn time via `-t fontFamily=...`.

### Session Thumbnails

Each session card on the dashboard gallery shows a text-based SVG thumbnail of the terminal content. Thumbnails are:

- Generated server-side via `tmux capture-pane`
- Stripped of ANSI escape sequences for clean rendering
- Cached for 30 seconds to avoid excessive tmux queries
- Refreshed on a ~30 s bucket timer in the browser (independent of the 5/10 s poll cycle)

The thumbnail API endpoint is `GET /api/sessions/{name}/thumbnail.svg`.

## HTTPS (Tailscale)

To serve the dashboard over HTTPS using Tailscale-provisioned certificates:

### 1. Provision certificates

```bash
# Replace with your machine's Tailscale FQDN
tailscale cert \
  --cert-file ~/.local/share/tmux-dash/cert.pem \
  --key-file  ~/.local/share/tmux-dash/key.pem \
  beas-mac-mini.fable-cobia.ts.net
```

### 2. Configure the server

Set the `TLS_CERT` and `TLS_KEY` environment variables before starting:

```bash
export TLS_CERT=~/.local/share/tmux-dash/cert.pem
export TLS_KEY=~/.local/share/tmux-dash/key.pem
python3 server.py
```

Or for launchd, add these to the plist's `EnvironmentVariables` dict.

### 3. Access

```
https://beas-mac-mini.fable-cobia.ts.net:7680
```

All terminal traffic is reverse-proxied through the dashboard port, so **only port 7680** needs to be reachable. The ttyd processes bind locally and are accessed via `/terminal/{session_name}/` paths on the main server.

### Certificate renewal

Tailscale certificates are valid for ~90 days. Re-run `tailscale cert` periodically to refresh them, then restart the service. A cron job or launchd timer can automate this.

## Architecture

```
Browser
  |  HTTP(S) GET /                              -> dashboard (index.html + app.js)
  |  HTTP(S) GET /api/sessions                  -> JSON list of live sessions
  |  HTTP(S) POST /api/sessions                 -> create new session
  |  HTTP(S) GET /api/sessions/{name}           -> session metadata + ttyd_url
  |  HTTP(S) GET /api/sessions/{name}/thumbnail.svg -> SVG snapshot
  |  HTTP(S) GET /api/completions/path?prefix=... -> directory autocompletion
  |  HTTP(S) + WebSocket /terminal/{name}/...   -> reverse proxy to ttyd
  v
server.py  (aiohttp, port 7680, optional TLS)
  |
  +-- session_manager.py
  |     +-- polls `tmux list-sessions` every N seconds
  |     +-- spawns ttyd subprocess per session  (ports 7681-7699)
  |     +-- captures pane text for SVG thumbnails (30 s cache)
  |     +-- kills ttyd when session disappears
  |
  +-- static/
        index.html, app.js, style.css
```

Each tmux session gets its own `ttyd` process on a port drawn from the pool. The dashboard reverse-proxies all ttyd HTTP and WebSocket traffic through `/terminal/{session_name}/`, so the browser only needs to reach port 7680. tmux renders its own pane layout inside the terminal, so no per-pane iframe splitting is needed.

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

Private project -- no license granted.
