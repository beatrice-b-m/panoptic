import os


"""Configuration constants for tmux-dash."""

# Dashboard server binding
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 7680

# ttyd port pool (supports up to 19 concurrent sessions)
TTYD_PORT_RANGE_START = 7681
TTYD_PORT_RANGE_END = 7699
TTYD_BIND_HOST = "0.0.0.0"
TTYD_BINARY = "ttyd"
TMUX_BINARY = "tmux"

# beamux — used for pane-layout creation when shelling out.
# Defaults to the local bea-sh tools path; override via env var.
BEAMUX_BINARY = os.getenv(
    "BEAMUX_BINARY",
    os.path.expanduser("~/AgentFiles/projects/bea-sh/tools/beamux/beamux"),
)

# Terminal font (passed to ttyd via -t fontFamily=...)
TTYD_FONT_FAMILY = os.getenv("TTYD_FONT_FAMILY", "Hack Font Mono, Menlo, Consolas, monospace")

# Polling intervals (seconds)
POLL_INTERVAL_ACTIVE = 5
POLL_INTERVAL_IDLE = 30

# Dashboard pagination
SESSION_PAGE_SIZE = 8

# Logging
LOG_LEVEL = "INFO"


# TLS — set both to enable HTTPS (e.g. via `tailscale cert`).
# When unset or empty, the server runs plain HTTP.
TLS_CERT = os.getenv("TLS_CERT", "")
TLS_KEY = os.getenv("TLS_KEY", "")