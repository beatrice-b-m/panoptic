"""tmux-dash server: HTTP API + static file serving + session lifecycle."""

import asyncio
import logging
import math
import os
import signal
import sys
import time
from pathlib import Path

from aiohttp import web

from config import DASHBOARD_HOST, DASHBOARD_PORT, LOG_LEVEL, SESSION_PAGE_SIZE
from session_manager import SessionManager

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# ---------------------------------------------------------------------------
# Client tracking — SessionManager uses this to decide polling interval
# ---------------------------------------------------------------------------

_active_clients: int = 0


def _get_client_count() -> int:
    return _active_clients


# ---------------------------------------------------------------------------
# Middleware: track active HTTP connections as a proxy for "someone is using
# the dashboard".  An SSE or WebSocket connection would be more precise, but
# the spec says poll-based frontend, so counting in-flight requests is the
# simplest correct signal.
# ---------------------------------------------------------------------------


@web.middleware
async def client_tracking_middleware(request: web.Request, handler):
    global _active_clients
    _active_clients += 1
    try:
        return await handler(request)
    finally:
        _active_clients -= 1


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def handle_index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def handle_sessions(request: web.Request) -> web.Response:
    mgr: SessionManager = request.app["session_manager"]

    page = _int_param(request, "page", 1)
    page_size = _int_param(request, "page_size", SESSION_PAGE_SIZE)

    data = mgr.get_sessions(page=page, page_size=page_size)
    return web.json_response(data)


async def handle_panes(request: web.Request) -> web.Response:
    mgr: SessionManager = request.app["session_manager"]
    session_name = request.match_info["session_name"]

    if session_name not in mgr.sessions:
        return web.json_response(
            {"error": f"Session '{session_name}' not found"},
            status=404,
        )

    panes = await mgr.get_panes(session_name)

    # Build ttyd_url from the inbound request host so the browser can reach
    # the ttyd instance directly (same hostname, different port).
    host = request.host.split(":")[0]  # strip port from Host header
    for pane in panes:
        port = pane.pop("port", None)
        pane["ttyd_url"] = f"http://{host}:{port}" if port else None

    return web.json_response({"session": session_name, "panes": panes})



async def handle_session_detail(request: web.Request) -> web.Response:
    """Return metadata and ttyd_url for a single session."""
    mgr: SessionManager = request.app["session_manager"]
    session_name = request.match_info["session_name"]

    sess = mgr.sessions.get(session_name)
    if sess is None:
        return web.json_response(
            {"error": f"Session '{session_name}' not found"},
            status=404,
        )

    host = request.host.split(":")[0]
    ttyd_url = f"http://{host}:{sess.port}" if sess.port else None

    return web.json_response({
        "name": sess.name,
        "windows": sess.windows,
        "attached": sess.attached,
        "created_epoch": sess.created_epoch,
        "ttyd_url": ttyd_url,
    })

async def handle_health(request: web.Request) -> web.Response:
    mgr: SessionManager = request.app["session_manager"]
    uptime = time.monotonic() - request.app["start_time"]
    return web.json_response({
        "status": "ok",
        "sessions": len(mgr.sessions),
        "uptime": round(uptime, 1),
    })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _int_param(request: web.Request, name: str, default: int) -> int:
    raw = request.query.get(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


async def on_startup(app: web.Application) -> None:
    mgr = SessionManager()
    app["session_manager"] = mgr
    app["start_time"] = time.monotonic()

    # Run an initial poll immediately so the API has data before the first
    # client connects.
    await mgr.poll_sessions()

    # Start the background polling loop (does not block — creates a task).
    app["poll_task"] = asyncio.create_task(
        mgr.start_polling(_get_client_count),
        name="session-poll-driver",
    )
    log.info(
        "tmux-dash started on http://%s:%d — %d session(s) discovered",
        DASHBOARD_HOST,
        DASHBOARD_PORT,
        len(mgr.sessions),
    )


async def on_cleanup(app: web.Application) -> None:
    mgr: SessionManager = app.get("session_manager")
    if mgr:
        await mgr.cleanup()
    poll_task: asyncio.Task | None = app.get("poll_task")
    if poll_task and not poll_task.done():
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
    log.info("tmux-dash shut down cleanly")


def build_app() -> web.Application:
    app = web.Application(middlewares=[client_tracking_middleware])

    # API routes
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/sessions", handle_sessions)
    app.router.add_get("/api/sessions/{session_name}/panes", handle_panes)
    app.router.add_get("/api/sessions/{session_name}", handle_session_detail)
    app.router.add_get("/api/health", handle_health)

    # Static files
    app.router.add_static("/static", STATIC_DIR, show_index=False)

    # Lifecycle hooks
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    app = build_app()
    web.run_app(app, host=DASHBOARD_HOST, port=DASHBOARD_PORT, print=None)


if __name__ == "__main__":
    main()
