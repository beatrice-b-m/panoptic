"""Configuration constants for panoptic."""

import os
from dataclasses import dataclass

# Dashboard server binding
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 7680

# Control bridge defaults
CONTROL_BRIDGE_COLS = 220
CONTROL_BRIDGE_ROWS = 50
TMUX_BINARY = "tmux"

# beamux — used for pane-layout creation when shelling out.
# Install from https://github.com/beatrice-b-m/beamux or override via env var.
BEAMUX_BINARY = os.getenv("BEAMUX_BINARY", "beamux")

# Terminal font for dashboard terminals
TERMINAL_FONT_FAMILY = os.getenv("TERMINAL_FONT_FAMILY", "'Hack Nerd Font', 'Hack Nerd Font Mono', Menlo, Consolas, monospace")

# Polling intervals (seconds)
POLL_INTERVAL_ACTIVE = 5
POLL_INTERVAL_IDLE = 30

# Client activity detection
CLIENT_ACTIVE_TIMEOUT = 60          # seconds — "active" if last request within this window
CLIENT_DEEP_IDLE_TIMEOUT = 300      # seconds — stop polling entirely after this long

# Dashboard pagination
SESSION_PAGE_SIZE = 8

# Logging
LOG_LEVEL = "INFO"


# TLS — set both to enable HTTPS (e.g. via `tailscale cert`).
# When unset or empty, the server runs plain HTTP.
TLS_CERT = os.getenv("TLS_CERT", "")
TLS_KEY = os.getenv("TLS_KEY", "")

# Multi-host configuration
HOSTS_CONFIG_PATH = os.getenv(
    "HOSTS_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "hosts.json"),
)

TEMPLATES_CONFIG_PATH = os.getenv(
    "TEMPLATES_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates.json"),
)

# SSH tunables for remote host polling (BatchMode is always on).
SSH_CONNECT_TIMEOUT = int(os.getenv("SSH_CONNECT_TIMEOUT", "5"))


# ---------------------------------------------------------------------------
# RuntimeSettings — structured runtime configuration for CLI / programmatic use
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeSettings:
    """All runtime-configurable values, threaded through server + session manager.

    Use ``RuntimeSettings.from_defaults()`` to construct an instance from the
    module-level constants above (which incorporate env-var overrides).
    """

    host: str = DASHBOARD_HOST
    port: int = DASHBOARD_PORT
    control_bridge_cols: int = CONTROL_BRIDGE_COLS
    control_bridge_rows: int = CONTROL_BRIDGE_ROWS
    tmux_binary: str = TMUX_BINARY
    beamux_binary: str = BEAMUX_BINARY
    terminal_font_family: str = TERMINAL_FONT_FAMILY
    poll_interval_active: int = POLL_INTERVAL_ACTIVE
    poll_interval_idle: int = POLL_INTERVAL_IDLE
    client_active_timeout: int = CLIENT_ACTIVE_TIMEOUT
    client_deep_idle_timeout: int = CLIENT_DEEP_IDLE_TIMEOUT
    session_page_size: int = SESSION_PAGE_SIZE
    log_level: str = LOG_LEVEL
    tls_cert: str = TLS_CERT
    tls_key: str = TLS_KEY
    hosts_config_path: str = HOSTS_CONFIG_PATH
    templates_config_path: str = TEMPLATES_CONFIG_PATH
    ssh_connect_timeout: int = SSH_CONNECT_TIMEOUT
    headless: bool = False

    @classmethod
    def from_defaults(cls) -> "RuntimeSettings":
        """Build settings from the module-level constants (env-var aware)."""
        return cls()