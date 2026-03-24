"""Configuration constants for tmux-dash."""

# Dashboard server binding
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 7680

# ttyd port pool (supports up to 19 concurrent sessions)
TTYD_PORT_RANGE_START = 7681
TTYD_PORT_RANGE_END = 7699
TTYD_BIND_HOST = "0.0.0.0"
TTYD_BINARY = "ttyd"

# Polling intervals (seconds)
POLL_INTERVAL_ACTIVE = 5
POLL_INTERVAL_IDLE = 30

# Dashboard pagination
SESSION_PAGE_SIZE = 8

# Logging
LOG_LEVEL = "INFO"
