from __future__ import annotations

"""tmux-dash server: HTTP API + static file serving + session lifecycle.

When TLS_CERT and TLS_KEY point to valid files (e.g. from `tailscale cert`),
the server terminates TLS directly.  All ttyd terminal traffic is reverse-
proxied through the dashboard port so only one port needs to be exposed.
"""

import asyncio
import logging
import math
import ssl
import time
from pathlib import Path
from urllib.parse import quote as urlquote

import aiohttp
from aiohttp import web

from config import (
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    LOG_LEVEL,
    SESSION_PAGE_SIZE,
    TLS_CERT,
    TLS_KEY,
)
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
# API routes
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
    """Return metadata and ttyd_url for a single session.

    ttyd_url is a same-origin path through the reverse proxy so it works
    transparently over both HTTP and HTTPS.
    """
    mgr: SessionManager = request.app["session_manager"]
    session_name = request.match_info["session_name"]

    sess = mgr.sessions.get(session_name)
    if sess is None:
        return web.json_response(
            {"error": f"Session '{session_name}' not found"},
            status=404,
        )

    # Proxy-based relative URL — the browser inherits scheme + host.
    safe_name = urlquote(session_name, safe="")
    ttyd_url = f"/terminal/{safe_name}/" if sess.port else None

    return web.json_response({
        "name": sess.name,
        "windows": sess.windows,
        "attached": sess.attached,
        "created_epoch": sess.created_epoch,
        "ttyd_url": ttyd_url,
    })


async def handle_thumbnail(request: web.Request) -> web.Response:
    """Return an SVG snapshot thumbnail for a session."""
    mgr: SessionManager = request.app["session_manager"]
    session_name = request.match_info["session_name"]

    svg = await mgr.get_thumbnail_svg(session_name)
    if svg is None:
        return web.json_response(
            {"error": f"Session '{session_name}' not found"},
            status=404,
        )

    return web.Response(
        text=svg,
        content_type="image/svg+xml",
        headers={"Cache-Control": "no-cache"},
    )


async def handle_health(request: web.Request) -> web.Response:
    mgr: SessionManager = request.app["session_manager"]
    uptime = time.monotonic() - request.app["start_time"]
    return web.json_response({
        "status": "ok",
        "sessions": len(mgr.sessions),
        "uptime": round(uptime, 1),
    })


# ---------------------------------------------------------------------------
# ttyd reverse proxy — forwards HTTP and WebSocket to the per-session ttyd
# ---------------------------------------------------------------------------

# Headers that must not be forwarded between proxy hops.
_HOP_HEADERS = frozenset({
    "host", "connection", "upgrade", "keep-alive",
    "transfer-encoding", "te", "trailer",
    "sec-websocket-key", "sec-websocket-version",
    "sec-websocket-extensions", "sec-websocket-accept",
})


def _proxy_request_headers(request: web.Request) -> dict[str, str]:
    """Filter inbound request headers for proxying."""
    return {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_HEADERS
    }


def _ttyd_target(request: web.Request) -> tuple[str | None, int | None]:
    """Resolve the session name from the route and return (session_name, port).

    Returns (None, None) when the session doesn't exist or has no port.
    """
    session_name = request.match_info["session_name"]
    mgr: SessionManager = request.app["session_manager"]
    sess = mgr.sessions.get(session_name)
    if sess is None or sess.port is None:
        return None, None
    return session_name, sess.port


async def handle_terminal(request: web.Request) -> web.Response | web.WebSocketResponse:
    """Reverse-proxy HTTP and WebSocket requests to the session's ttyd."""
    session_name, port = _ttyd_target(request)
    if port is None:
        return web.Response(status=502, text="Terminal not available")

    # Reconstruct the full path ttyd expects (it was started with --base-path).
    safe_name = urlquote(session_name, safe="")
    suffix = request.match_info.get("path", "")
    target = f"http://127.0.0.1:{port}/terminal/{safe_name}/{suffix}"
    if request.query_string:
        target += f"?{request.query_string}"

    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await _proxy_ws(request, target)

    return await _proxy_http(request, target)


async def _proxy_http(request: web.Request, target: str) -> web.Response:
    """Forward a plain HTTP request to ttyd and relay the response."""
    cs: aiohttp.ClientSession = request.app["client_session"]
    try:
        async with cs.request(
            request.method,
            target,
            headers=_proxy_request_headers(request),
            data=await request.read() if request.can_read_body else None,
            allow_redirects=False,
        ) as resp:
            body = await resp.read()
            # Relay content headers only; drop hop-by-hop from response too.
            headers: dict[str, str] = {}
            for h in ("Content-Type", "Cache-Control", "ETag", "Last-Modified"):
                if h in resp.headers:
                    headers[h] = resp.headers[h]
            return web.Response(status=resp.status, body=body, headers=headers)
    except Exception as exc:
        log.warning("Terminal HTTP proxy error → %s: %s", target, exc)
        return web.Response(status=502, text="Terminal proxy error")


async def _proxy_ws(request: web.Request, target: str) -> web.WebSocketResponse:
    """Bridge a WebSocket between the browser and ttyd.

    Runs two concurrent forwarding loops; when either side closes or errors
    the other is torn down promptly.
    """
    # Negotiate subprotocol with the browser (ttyd uses 'tty').
    protocols: tuple[str, ...] = ()
    proto_header = request.headers.get("Sec-WebSocket-Protocol", "")
    if proto_header:
        protocols = tuple(p.strip() for p in proto_header.split(",") if p.strip())

    ws_server = web.WebSocketResponse(protocols=protocols)
    await ws_server.prepare(request)

    ws_url = target.replace("http://", "ws://", 1)
    cs: aiohttp.ClientSession = request.app["client_session"]

    try:
        async with cs.ws_connect(ws_url, protocols=protocols) as ws_client:
            async def _fwd_client_to_server():
                """ttyd → browser."""
                async for msg in ws_client:
                    if ws_server.closed:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await ws_server.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await ws_server.send_bytes(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

            async def _fwd_server_to_client():
                """browser → ttyd."""
                async for msg in ws_server:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await ws_client.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await ws_client.send_bytes(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

            done, pending = await asyncio.wait(
                [
                    asyncio.ensure_future(_fwd_client_to_server()),
                    asyncio.ensure_future(_fwd_server_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    except Exception as exc:
        log.warning("WebSocket proxy error: %s", exc)

    if not ws_server.closed:
        await ws_server.close()
    return ws_server


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


def _build_ssl_context() -> ssl.SSLContext | None:
    """Create an SSL context from TLS_CERT/TLS_KEY, or None for plain HTTP."""
    if not TLS_CERT or not TLS_KEY:
        return None

    cert_path = Path(TLS_CERT)
    key_path = Path(TLS_KEY)

    if not cert_path.is_file():
        log.warning("TLS_CERT=%s does not exist; falling back to plain HTTP", TLS_CERT)
        return None
    if not key_path.is_file():
        log.warning("TLS_KEY=%s does not exist; falling back to plain HTTP", TLS_KEY)
        return None

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    log.info("TLS enabled — cert=%s key=%s", cert_path, key_path)
    return ctx


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


async def on_startup(app: web.Application) -> None:
    mgr = SessionManager()
    app["session_manager"] = mgr
    app["start_time"] = time.monotonic()

    # Shared HTTP client for the reverse proxy.
    app["client_session"] = aiohttp.ClientSession()

    # Kill orphaned ttyd processes from a previous server run so their
    # ports are freed before we start spawning new ones.
    await mgr._kill_stale_ttyd()


    # Run an initial poll immediately so the API has data before the first
    # client connects.
    await mgr.poll_sessions()

    # Start the background polling loop (does not block — creates a task).
    app["poll_task"] = asyncio.create_task(
        mgr.start_polling(_get_client_count),
        name="session-poll-driver",
    )

    scheme = "https" if app.get("_tls_enabled") else "http"
    log.info(
        "tmux-dash started on %s://%s:%d — %d session(s) discovered",
        scheme,
        DASHBOARD_HOST,
        DASHBOARD_PORT,
        len(mgr.sessions),
    )


async def on_cleanup(app: web.Application) -> None:
    cs: aiohttp.ClientSession | None = app.get("client_session")
    if cs:
        await cs.close()

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
    app.router.add_get("/api/sessions/{session_name}/thumbnail.svg", handle_thumbnail)
    app.router.add_get("/api/sessions/{session_name}", handle_session_detail)
    app.router.add_get("/api/health", handle_health)

    # ttyd reverse proxy — catch-all under /terminal/{session_name}/
    app.router.add_route("*", "/terminal/{session_name}/{path:.*}", handle_terminal)

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

    ssl_ctx = _build_ssl_context()
    app = build_app()
    # Stash flag so on_startup can log the correct scheme.
    app["_tls_enabled"] = ssl_ctx is not None

    web.run_app(
        app,
        host=DASHBOARD_HOST,
        port=DASHBOARD_PORT,
        ssl_context=ssl_ctx,
        print=None,
    )


if __name__ == "__main__":
    main()
