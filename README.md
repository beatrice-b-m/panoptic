# tmux-dash

A lightweight web dashboard that discovers your tmux sessions and exposes each one as a live terminal in the browser via [ttyd](https://github.com/tsl0922/ttyd). Supports multiple hosts — monitor local and remote machines from a single dashboard.

<!-- screenshot: dashboard view -->

## Features

- **Multi-host support** -- monitor tmux sessions on localhost and remote SSH hosts from one dashboard; host tabs switch context instantly
- **SSH alias-first model** -- remote hosts use your `~/.ssh/config` aliases; tmux-dash never stores passwords or private keys
- **Automatic session discovery** -- polls tmux every 5 s (active) / 30 s (idle), no manual registration
- **Single terminal view** -- clicking a session opens one full ttyd terminal showing the real tmux layout (panes, splits, status bar); up to 19 concurrent sessions across all hosts
- **Session thumbnails** -- each gallery card shows a live text snapshot (SVG) of the session, refreshed approximately every 30 seconds
- **Auto-refresh** -- dashboard reflects session create/destroy in real time without a page reload
- **Light/dark theme** -- toggle in the header; persists to localStorage; follows `prefers-color-scheme` when no explicit choice is stored
- **HTTPS via Tailscale** -- optional TLS termination using Tailscale-provisioned certificates; all ttyd traffic is reverse-proxied through a single port
- **Configurable terminal font** -- terminal font family defaults to Hack Font Mono and is overridable via environment variable
- **Always-on via launchd** -- survives reboots; crashes restart automatically
- **Remote access** -- bind on `0.0.0.0`; reach from any device on your Tailscale network
- **Create sessions from UI** -- click "+New" to spawn a tmux session with optional working directory and pane layout
- **Pane layout support** -- row or column layouts via colon-separated spec (e.g. `2:1:3`); live CSS grid preview before creation
- **Directory autocompletion** -- server-side path completion when typing a working directory for new sessions (localhost only)

## Prerequisites

| Requirement | Notes |
|---|---|
| macOS | launchd integration is macOS-only |
| [Homebrew](https://brew.sh) | used to install `ttyd` |
| Python 3.9+ | standard macOS install or `brew install python` |
| tmux | `brew install tmux` |
| [Tailscale](https://tailscale.com) | optional -- required only for remote/HTTPS access |

For remote hosts: SSH access with key-based authentication (or ssh-agent / ControlMaster).

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

## Multi-Host Setup

### How it works

tmux-dash can monitor tmux sessions on remote machines over SSH. The architecture is simple:

1. **Polling:** The server runs `ssh <alias> tmux list-sessions` periodically to discover remote sessions.
2. **Terminal access:** Each remote session gets a local ttyd process that runs `ssh <alias> tmux -u attach-session -t <name>`.
3. **No remote installation needed** -- only SSH access and tmux on the remote machine.

### Adding a remote host

1. Click the **+** tab in the host bar.
2. Enter a display **label** and the **SSH alias** from your `~/.ssh/config`.
3. Click **Add Host**.

The host appears as a new tab. Sessions are discovered on the next poll cycle.

### SSH configuration

All authentication and connection options are handled by OpenSSH via your `~/.ssh/config`. tmux-dash never stores passwords or private keys.

Example `~/.ssh/config` entry:

```
Host pi
    HostName 192.168.1.50
    User pi
    IdentityFile ~/.ssh/id_ed25519
    # Optional: persistent connection for faster polling
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h-%p
    ControlPersist 600
```

### Authentication requirements

Background polling uses `ssh -o BatchMode=yes` to avoid hanging on password prompts. This means:

- **Key-based auth** (with or without ssh-agent) works seamlessly.
- **Password-only hosts** will fail polling unless you have an active ControlMaster session.
- If polling fails, the host tab shows an error indicator. The app remains responsive.

### Host configuration file

Hosts are stored in `hosts.json` (configurable via `HOSTS_CONFIG_PATH`):

```json
{
  "hosts": [
    { "id": "localhost", "label": "localhost", "type": "local", "enabled": true },
    { "id": "pi", "label": "Raspberry Pi", "type": "ssh", "ssh_alias": "pi", "enabled": true }
  ]
}
```

- `localhost` always exists and cannot be removed.
- `id` is auto-derived from the label (slug-safe, unique).
- `type` is `"local"` or `"ssh"`.
- `ssh_alias` matches a `Host` entry in `~/.ssh/config`.

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
| `HOSTS_CONFIG_PATH` | `hosts.json` | `HOSTS_CONFIG_PATH` | Path to JSON host configuration file |
| `SSH_CONNECT_TIMEOUT` | `5` | `SSH_CONNECT_TIMEOUT` | SSH connect timeout in seconds for remote polling |
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

- Generated server-side via `tmux capture-pane` (works for both local and remote sessions)
- Stripped of ANSI escape sequences for clean rendering
- Cached for 30 seconds to avoid excessive tmux queries
- Refreshed on a ~30 s bucket timer in the browser (independent of the 5/10 s poll cycle)

## API

All session endpoints are scoped under `/api/hosts/{host_id}/`.

### Host management

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/hosts` | List configured hosts with runtime status |
| `POST` | `/api/hosts` | Add SSH host (`{label, ssh_alias}`) |
| `DELETE` | `/api/hosts/{host_id}` | Remove a host |

### Session operations

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/hosts/{host_id}/sessions` | Paginated session list |
| `POST` | `/api/hosts/{host_id}/sessions` | Create new session |
| `GET` | `/api/hosts/{host_id}/sessions/{name}` | Session metadata + ttyd_url |
| `DELETE` | `/api/hosts/{host_id}/sessions/{name}` | Kill session |
| `GET` | `/api/hosts/{host_id}/sessions/{name}/panes` | Pane layout |
| `GET` | `/api/hosts/{host_id}/sessions/{name}/thumbnail.svg` | SVG snapshot |
| `GET` | `/api/hosts/{host_id}/completions/path` | Directory autocompletion (localhost only) |

### System

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/health` | Liveness check |

### Terminal proxy

All terminal traffic is proxied through `GET /terminal/{host_id}/{session_name}/{path}`.

## HTTPS (Tailscale)

To serve the dashboard over HTTPS using Tailscale-provisioned certificates:

### 1. Provision certificates

```bash
tailscale cert \
  --cert-file ~/.local/share/tmux-dash/cert.pem \
  --key-file  ~/.local/share/tmux-dash/key.pem \
  beas-mac-mini.fable-cobia.ts.net
```

### 2. Configure the server

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

All terminal traffic is reverse-proxied through the dashboard port, so **only port 7680** needs to be reachable.

### Certificate renewal

Tailscale certificates are valid for ~90 days. Re-run `tailscale cert` periodically to refresh them, then restart the service.

## Architecture

```
Browser
  |  HTTP(S) GET /                                             -> dashboard
  |  HTTP(S) GET /api/hosts                                    -> host list + status
  |  HTTP(S) POST /api/hosts                                   -> add SSH host
  |  HTTP(S) GET /api/hosts/{host_id}/sessions                 -> session list
  |  HTTP(S) POST /api/hosts/{host_id}/sessions                -> create session
  |  HTTP(S) GET /api/hosts/{host_id}/sessions/{name}          -> session detail
  |  HTTP(S) GET /api/hosts/{host_id}/sessions/{name}/thumbnail.svg
  |  HTTP(S) GET /api/hosts/{host_id}/completions/path         -> dir autocomplete
  |  HTTP(S) + WebSocket /terminal/{host_id}/{name}/...        -> reverse proxy to ttyd
  v
server.py  (aiohttp, port 7680, optional TLS)
  |
  +-- host_config.py    (JSON host persistence)
  +-- session_manager.py
  |     +-- polls `tmux list-sessions` per host (local or via SSH)
  |     +-- spawns local ttyd per session (direct tmux or ssh + tmux attach)
  |     +-- captures pane text for thumbnails (local or via SSH)
  |     +-- kills ttyd when session disappears
  |
  +-- static/
        index.html, app.js, style.css
```

Each tmux session gets its own local `ttyd` process on a port from the pool. For remote hosts, ttyd execs `ssh <alias> tmux -u attach-session -t <name>` instead of attaching directly. The dashboard reverse-proxies all traffic through `/terminal/{host_id}/{session_name}/`.

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
