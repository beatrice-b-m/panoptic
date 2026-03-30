# panoptic

A lightweight web dashboard that discovers your tmux sessions and exposes each pane as an independent live terminal in the browser via tmux control mode. Supports multiple hosts — monitor local and remote machines from a single dashboard.

<!-- screenshot: dashboard view -->

## Features

- **Multi-host support** -- monitor tmux sessions on localhost and remote SSH hosts from one dashboard; host tabs switch context instantly
- **SSH alias-first model** -- remote hosts use your `~/.ssh/config` aliases; panoptic never stores passwords or private keys
- **Automatic session discovery** -- polls tmux every 5 s (active) / 30 s (idle), no manual registration
- **Per-pane terminal view** -- clicking a session opens a split-pane terminal grid matching the tmux layout; each pane is an independent xterm.js instance with native text selection
- **Session thumbnails** -- each gallery card shows a live text snapshot (SVG) of the session, refreshed approximately every 30 seconds
- **Auto-refresh** -- dashboard reflects session create/destroy in real time without a page reload
- **Light/dark theme** -- toggle in the header; persists to localStorage; follows `prefers-color-scheme` when no explicit choice is stored
- **HTTPS via Tailscale** -- optional TLS termination using Tailscale-provisioned certificates; all terminal traffic flows through a single WebSocket port
- **Configurable terminal font** -- terminal font family defaults to Hack Nerd Font and is overridable via environment variable or CLI flag
- **Always-on via launchd** -- survives reboots; crashes restart automatically
- **Remote access** -- use `--host 0.0.0.0` for network access; reach from any device on your Tailscale network
- **Create sessions from UI** -- click "+New" to spawn a tmux session with optional working directory and pane layout
- **Pane layout support** -- row or column layouts via colon-separated spec (e.g. `2:1:3`); supports mixed command segments (e.g. `vim,jest:3`); live CSS grid preview before creation
- **Directory autocompletion** -- server-side path completion when typing a working directory for new sessions (localhost only)
- **Session templates** -- save, load, rename, and delete session templates with pre-configured name, directory, layout, and pane commands
- **Macro variables** -- templates support `{var}` placeholders that are filled at launch time; all variables must be provided before creation
- **Pane startup commands** -- assign shell commands to individual panes via the layout preview; commands run after session creation
- **Mixed layout specs** -- layout spec now supports command segments (e.g. `vim,jest:3`) in addition to pure numeric specs
- **Installable PWA** -- add to home screen on mobile or desktop; app-shell caching for instant loads

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | `python3 --version` to check |
| tmux | `brew install tmux` (macOS) or `sudo apt install tmux` (Ubuntu) |
| [Tailscale](https://tailscale.com) | optional -- required only for remote/HTTPS access |

For remote hosts: SSH access with key-based authentication (or ssh-agent / ControlMaster).

## Quick Start (macOS)

```bash
git clone <your-repo-url> && cd tmux-local-dash
./setup-service.sh
```

This installs dependencies (aiohttp), registers a launchd plist, and **starts panoptic as a persistent background service**. The server launches on boot and restarts automatically if it crashes. Open `http://localhost:7680` once the script finishes.

## Quick Start (Ubuntu / Linux)

```bash
git clone <your-repo-url> && cd tmux-local-dash
pip3 install aiohttp

# Start in foreground
python3 panoptic_cli.py serve
```

To run as a persistent service, see [systemd Setup](#systemd-setup-linux) below.

## Manual Setup

If you prefer not to use `setup-service.sh` (macOS) or want a minimal install on any platform:

```bash
# 1. Install Python dependency
pip3 install aiohttp

# 2. Start the server (foreground)
python3 panoptic_cli.py serve
```

Open `http://localhost:7680`. Press `Ctrl+C` to stop.
## CLI Usage

The CLI provides a `serve` subcommand with full control over runtime settings:

```bash
# Start with defaults
python3 panoptic_cli.py serve

# Custom port
python3 panoptic_cli.py serve --port 8080

# Custom log level
python3 panoptic_cli.py serve --log-level DEBUG

# See all flags
python3 panoptic_cli.py serve --help
```

All flags have sensible defaults from `config.py`. Passing no flags is equivalent to the previous `python3 server.py` behavior.

## Headless / Remote Server

Use `--headless` on a remote server where no browser is available. This forces the dashboard to bind on `127.0.0.1`, preventing external access, and prints SSH port-forwarding instructions:

```bash
# On the remote server
python3 panoptic_cli.py serve --headless
python3 panoptic_cli.py serve --headless --port 8080
```

Then from your local machine:

```bash
ssh -N -L 7680:127.0.0.1:7680 user@remote-host
```

Browse `http://127.0.0.1:7680` locally. All terminal traffic flows through the dashboard WebSocket port — no additional port forwards are needed.

`--headless` rejects conflicting flags:

```bash
# This will fail with a clear error:
python3 panoptic_cli.py serve --headless --host 0.0.0.0
```

## Multi-Host Setup

### How it works

panoptic can monitor tmux sessions on remote machines over SSH. The architecture is simple:

1. **Polling:** The server runs `ssh <alias> tmux list-sessions` periodically to discover remote sessions.
2. **Terminal access:** Each remote session is accessed via tmux control mode over SSH.
3. **No remote installation needed** -- only SSH access and tmux on the remote machine.

### Adding a remote host

1. Click the **+** tab in the host bar.
2. Enter a display **label** and the **SSH alias** from your `~/.ssh/config`.
3. Click **Add Host**.

The host appears as a new tab. Sessions are discovered on the next poll cycle.

### SSH configuration

All authentication and connection options are handled by OpenSSH via your `~/.ssh/config`. panoptic never stores passwords or private keys.

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
| `DASHBOARD_HOST` | `127.0.0.1` | -- | Interface the dashboard binds on (use `--host 0.0.0.0` for network access) |
| `DASHBOARD_PORT` | `7680` | -- | Dashboard HTTP port |
| `CONTROL_BRIDGE_COLS` | `220` | -- | Default terminal width (columns) for the control bridge |
| `CONTROL_BRIDGE_ROWS` | `50` | -- | Default terminal height (rows) for the control bridge |
| `TERMINAL_FONT_FAMILY` | `Hack Nerd Font, ...` | `TERMINAL_FONT_FAMILY` | Font family for browser-side terminal rendering |
| `POLL_INTERVAL_ACTIVE` | `5` | -- | Seconds between polls when clients are connected |
| `POLL_INTERVAL_IDLE` | `30` | -- | Seconds between polls when no clients are connected |
| `SESSION_PAGE_SIZE` | `8` | -- | Sessions shown per page on the dashboard |
| `TLS_CERT` | *(empty)* | `TLS_CERT` | Path to TLS certificate file (PEM) |
| `TLS_KEY` | *(empty)* | `TLS_KEY` | Path to TLS private key file (PEM) |
| `HOSTS_CONFIG_PATH` | `hosts.json` | `HOSTS_CONFIG_PATH` | Path to JSON host configuration file |
| `TEMPLATES_CONFIG_PATH` | `templates.json` | `TEMPLATES_CONFIG_PATH` | Path to JSON template configuration file |
| `SSH_CONNECT_TIMEOUT` | `5` | `SSH_CONNECT_TIMEOUT` | SSH connect timeout in seconds for remote polling |
| `LOG_LEVEL` | `INFO` | -- | Python logging level |
| `BEAMUX_BINARY` | `beamux` | `BEAMUX_BINARY` | Path to [beamux](https://github.com/beatrice-b-m/beamux) for pane layout creation |

Edit `config.py` or set environment variables and restart the service for changes to take effect.

### Terminal Font

The terminal font defaults to **Hack Nerd Font** with a fallback chain of Hack Nerd Font Mono, Menlo, Consolas, and generic monospace. Override it via the `TERMINAL_FONT_FAMILY` environment variable or the `--font-family` CLI flag:

```bash
export TERMINAL_FONT_FAMILY="JetBrains Mono, Fira Code, monospace"
python3 panoptic_cli.py serve
```

The font must be installed on the **client device** (the browser). The font is applied to each xterm.js instance in the browser.

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
| `GET` | `/api/hosts/{host_id}/sessions/{name}` | Session metadata + ws_url |
| `DELETE` | `/api/hosts/{host_id}/sessions/{name}` | Kill session |
| `GET` | `/api/hosts/{host_id}/sessions/{name}/panes` | Pane layout |
| `GET` | `/api/hosts/{host_id}/sessions/{name}/thumbnail.svg` | SVG snapshot |
| `GET` | `/api/hosts/{host_id}/completions/path` | Directory autocompletion (localhost only) |
| `POST` | `/api/hosts/{host_id}/sessions/from-template` | Create session from template |

### Template management

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/templates` | List all templates with extracted variable names |
| `POST` | `/api/templates` | Save a new template |
| `PUT` | `/api/templates/{template_name}` | Update template content |
| `PATCH` | `/api/templates/{template_name}` | Rename template (`{"new_name": "..."}`) |
| `DELETE` | `/api/templates/{template_name}` | Delete a template |

### System

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/health` | Liveness check |

### Terminal WebSocket

Terminal sessions connect via WebSocket at `GET /ws/hosts/{host_id}/sessions/{session_name}`.

## HTTPS (Tailscale)

To serve the dashboard over HTTPS using Tailscale-provisioned certificates:

### 1. Provision certificates

```bash
tailscale cert \
  --cert-file ~/.local/share/panoptic/cert.pem \
  --key-file  ~/.local/share/panoptic/key.pem \
  <tailscale-dns-name>.ts.net
```

### 2. Configure the server

```bash
export TLS_CERT=~/.local/share/panoptic/cert.pem
export TLS_KEY=~/.local/share/panoptic/key.pem
python3 panoptic_cli.py serve
```

Or pass them as CLI flags: `python3 panoptic_cli.py serve --tls-cert /path/to/cert.pem --tls-key /path/to/key.pem`. For launchd, add them to the plist's `EnvironmentVariables` dict. For systemd, add `Environment=` directives to the unit override.

### 3. Access

```
https://<tailscale-dns-name>.ts.net:7680
```

All terminal traffic flows through the dashboard WebSocket port, so **only port 7680** needs to be reachable.

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
  |  WebSocket /ws/hosts/{host_id}/sessions/{name}             -> control bridge
  v
server.py  (aiohttp, port 7680, optional TLS)
  |
  +-- host_config.py    (JSON host persistence)
  +-- session_manager.py
  |     +-- polls `tmux list-sessions` per host (local or via SSH)
  |     +-- captures pane text for thumbnails (local or via SSH)
  |
  +-- control_bridge.py
  |     +-- spawns `tmux -CC attach` per session view
  |     +-- parses control mode protocol (output, layout, window events)
  |     +-- relays per-pane output as binary WebSocket frames
  |
  +-- static/
        index.html, app.js, style.css
```

The browser opens a WebSocket to the control bridge when viewing a session. The bridge spawns `tmux -CC attach -t <session>` and parses the control mode protocol to demultiplex per-pane output and layout changes. Each pane is rendered as an independent xterm.js terminal in the browser, enabling native text selection without modifier keys. For remote hosts, the bridge runs `ssh <alias> tmux -CC attach -t <name>`.

## Service Management

### launchd (macOS)

The service label is `com.user.panoptic`.

```bash
# Load and start
launchctl load ~/Library/LaunchAgents/com.user.panoptic.plist

# Stop and unload
launchctl unload ~/Library/LaunchAgents/com.user.panoptic.plist

# Check status
launchctl list | grep panoptic

# Restart
launchctl unload ~/Library/LaunchAgents/com.user.panoptic.plist
launchctl load  ~/Library/LaunchAgents/com.user.panoptic.plist
```

### systemd Setup (Linux)

A systemd user unit is provided in `panoptic.service`. Install it:

```bash
# Edit the unit file: replace __INSTALL_DIR__ with the actual project path
sed "s|__INSTALL_DIR__|$(pwd)|g; s|__PYTHON3__|$(python3 -c 'import sys; print(sys.executable)')|g" panoptic.service > ~/.config/systemd/user/panoptic.service

# Reload and start
systemctl --user daemon-reload
systemctl --user enable --now panoptic

# Check status
systemctl --user status panoptic

# View logs
journalctl --user -u panoptic -f
```

### Log files

**macOS (launchd):** Logs are written to the install directory under `logs/`:

| File | Content |
|---|---|
| `logs/stdout.log` | Server output, session events |
| `logs/stderr.log` | Errors, tracebacks |

```bash
tail -f <install-dir>/logs/stdout.log
tail -f <install-dir>/logs/stderr.log
```

**Linux (systemd):** Logs go to journald by default:

```bash
journalctl --user -u panoptic -f
```

## Uninstall

**macOS:**

```bash
./setup-service.sh --uninstall
```

This unloads the launchd service and removes the plist from `~/Library/LaunchAgents/`. It does not delete the project directory or installed pip packages.

**Linux:**

```bash
systemctl --user disable --now panoptic
rm ~/.config/systemd/user/panoptic.service
systemctl --user daemon-reload
```

## License

Private project -- no license granted.
