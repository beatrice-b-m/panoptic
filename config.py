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
TTYD_FONT_FAMILY = os.getenv("TTYD_FONT_FAMILY", "'Hack Nerd Font', 'Hack Nerd Font Mono', Menlo, Consolas, monospace")

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

# Multi-host configuration
HOSTS_CONFIG_PATH = os.getenv(
    "HOSTS_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "hosts.json"),
)

# SSH tunables for remote host polling (BatchMode is always on).
SSH_CONNECT_TIMEOUT = int(os.getenv("SSH_CONNECT_TIMEOUT", "5"))


# ---------------------------------------------------------------------------
# RuntimeSettings — structured runtime configuration for CLI / programmatic use
# ---------------------------------------------------------------------------

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeSettings:
    """All runtime-configurable values, threaded through server + session manager.

    Use ``RuntimeSettings.from_defaults()`` to construct an instance from the
    module-level constants above (which incorporate env-var overrides).
    """

    host: str = DASHBOARD_HOST
    port: int = DASHBOARD_PORT
    ttyd_port_start: int = TTYD_PORT_RANGE_START
    ttyd_port_end: int = TTYD_PORT_RANGE_END
    ttyd_bind_host: str = TTYD_BIND_HOST
    ttyd_binary: str = TTYD_BINARY
    tmux_binary: str = TMUX_BINARY
    beamux_binary: str = BEAMUX_BINARY
    ttyd_font_family: str = TTYD_FONT_FAMILY
    poll_interval_active: int = POLL_INTERVAL_ACTIVE
    poll_interval_idle: int = POLL_INTERVAL_IDLE
    session_page_size: int = SESSION_PAGE_SIZE
    log_level: str = LOG_LEVEL
    tls_cert: str = TLS_CERT
    tls_key: str = TLS_KEY
    hosts_config_path: str = HOSTS_CONFIG_PATH
    ssh_connect_timeout: int = SSH_CONNECT_TIMEOUT
    headless: bool = False

    @classmethod
    def from_defaults(cls) -> "RuntimeSettings":
        """Build settings from the module-level constants (env-var aware)."""
        return cls()