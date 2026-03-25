#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_NAME="com.user.panoptic.plist"
PLIST_SRC="$SCRIPT_DIR/$PLIST_NAME"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_NAME"
LABEL="com.user.panoptic"
DASHBOARD_PORT=7680

# ── helpers ─────────────────────────────────────────────────────────────────

info()  { echo "[info]  $*"; }
warn()  { echo "[warn]  $*" >&2; }
error() { echo "[error] $*" >&2; exit 1; }

# ── uninstall ────────────────────────────────────────────────────────────────

if [[ "${1:-}" == "--uninstall" ]]; then
    info "Uninstalling panoptic..."

    if launchctl list | grep -q "$LABEL" 2>/dev/null; then
        launchctl unload "$PLIST_DEST" 2>/dev/null && info "Service stopped." || warn "Could not unload service (may already be stopped)."
    else
        info "Service not currently loaded."
    fi

    if [[ -f "$PLIST_DEST" ]]; then
        rm "$PLIST_DEST"
        info "Removed $PLIST_DEST"
    else
        info "Plist not found at $PLIST_DEST — nothing to remove."
    fi

    echo ""
    echo "panoptic uninstalled."
    exit 0
fi

# ── prerequisites ─────────────────────────────────────────────────────────────

# Homebrew
if ! command -v brew &>/dev/null; then
    error "Homebrew not found. Install it from https://brew.sh then re-run this script."
fi

# Python 3
if ! command -v python3 &>/dev/null; then
    error "python3 not found. Install Python 3.11+ via Homebrew: brew install python"
fi

PY_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="$(echo "$PY_VERSION" | cut -d. -f1)"
PY_MINOR="$(echo "$PY_VERSION" | cut -d. -f2)"

if [[ "$PY_MAJOR" -lt 3 || ( "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 11 ) ]]; then
    warn "Python $PY_VERSION detected (3.11+ recommended). Proceeding anyway."
else
    info "Python $PY_VERSION — OK"
fi

# ── system dependencies ───────────────────────────────────────────────────────

if brew list ttyd &>/dev/null 2>&1; then
    info "ttyd already installed — skipping."
else
    info "Installing ttyd via Homebrew..."
    brew install ttyd
fi

# ── Python dependencies ───────────────────────────────────────────────────────

info "Installing aiohttp..."
if [[ "$(id -u)" == "0" ]]; then
    pip3 install aiohttp
else
    pip3 install --user aiohttp
fi

# ── logs directory ────────────────────────────────────────────────────────────

mkdir -p "$SCRIPT_DIR/logs"
info "Logs directory: $SCRIPT_DIR/logs"

# ── launchd plist ─────────────────────────────────────────────────────────────

if [[ ! -f "$PLIST_SRC" ]]; then
    error "Plist template not found: $PLIST_SRC"
fi

mkdir -p "$HOME/Library/LaunchAgents"

sed \
    -e "s|__INSTALL_DIR__|$SCRIPT_DIR|g" \
    -e "s|__HOME_DIR__|$HOME|g" \
    "$PLIST_SRC" > "$PLIST_DEST"

info "Plist installed to $PLIST_DEST"

# ── load service ──────────────────────────────────────────────────────────────

if launchctl list | grep -q "$LABEL" 2>/dev/null; then
    info "Service already loaded — reloading to pick up any changes."
    launchctl unload "$PLIST_DEST"
fi

launchctl load "$PLIST_DEST"
info "Service loaded."

# ── summary ───────────────────────────────────────────────────────────────────

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " panoptic service installed"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Install dir : $SCRIPT_DIR"
echo " Plist       : $PLIST_DEST"
echo " Dashboard   : http://localhost:$DASHBOARD_PORT"
echo ""
echo " The server is now running as a background"
echo " service via launchd. It starts on boot and"
echo " restarts automatically on crash."
echo ""
echo " Status      : launchctl list | grep panoptic"
echo " Start       : python3 $SCRIPT_DIR/panoptic_cli.py serve"
echo " Logs        : tail -f $SCRIPT_DIR/logs/stderr.log"
echo " Uninstall   : $SCRIPT_DIR/setup-service.sh --uninstall"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
