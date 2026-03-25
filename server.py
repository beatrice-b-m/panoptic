from __future__ import annotations

"""panoptic server: HTTP API + static file serving + session lifecycle.

When TLS_CERT and TLS_KEY point to valid files (e.g. from ``tailscale cert``),
the server terminates TLS directly.  All ttyd terminal traffic is reverse-
proxied through the dashboard port so only one port needs to be exposed.

Session routes are scoped under ``/api/hosts/{host_id}/sessions/...`` and
the terminal proxy lives at ``/terminal/{host_id}/{session_name}/...``.
"""

import os
import asyncio
import logging
import ssl
import time
from pathlib import Path
from urllib.parse import quote as urlquote

import aiohttp
from aiohttp import web

from config import RuntimeSettings
from host_config import HostConfig
from session_manager import SessionManager
from template_macros import validate_placeholders, extract_variables, render, contains_placeholders
from template_store import TemplateStore

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# ---------------------------------------------------------------------------
# Client activity tracking — timestamp-based instead of request counting.
# SessionManager uses this to decide polling interval / deep idle.
# ---------------------------------------------------------------------------

_last_activity: float = 0.0
_wake_event: asyncio.Event | None = None


def _get_last_activity() -> float:
    return _last_activity


def _get_wake_event() -> asyncio.Event | None:
    return _wake_event


@web.middleware
async def client_tracking_middleware(request: web.Request, handler):
    global _last_activity
    _last_activity = time.monotonic()
    # Wake the polling loop if it is sleeping in deep idle.
    if _wake_event is not None:
        _wake_event.set()
    return await handler(request)


@web.middleware
async def security_headers_middleware(request: web.Request, handler):
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    if request.app.get("_tls_enabled"):
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
    return response

# ---------------------------------------------------------------------------
# API routes — hosts
# ---------------------------------------------------------------------------


async def handle_index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def handle_service_worker(_request: web.Request) -> web.FileResponse:
    """Serve sw.js from the root scope with the permissive scope header."""
    resp = web.FileResponse(STATIC_DIR / "sw.js")
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


async def handle_manifest(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "manifest.json")


async def handle_hosts(request: web.Request) -> web.Response:
    """Return the configured host list with runtime status."""
    mgr: SessionManager = request.app["session_manager"]
    host_config: HostConfig = request.app["host_config"]

    hosts = host_config.list_hosts()
    statuses = mgr.get_host_statuses()

    result = []
    for h in hosts:
        st = statuses.get(h["id"], {})
        result.append({
            **h,
            "status": st.get("status", "unknown"),
            "status_message": st.get("message", ""),
            "default_cwd": os.path.expanduser("~") + "/" if h["type"] == "local" else "~/",
        })

    return web.json_response({"hosts": result})


async def handle_add_host(request: web.Request) -> web.Response:
    """Add a new SSH host."""
    host_config: HostConfig = request.app["host_config"]
    mgr: SessionManager = request.app["session_manager"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    label = body.get("label", "").strip()
    ssh_alias = body.get("ssh_alias", "").strip()

    if not label:
        return web.json_response({"error": "'label' is required"}, status=400)
    if not ssh_alias:
        return web.json_response(
            {"error": "'ssh_alias' is required"}, status=400
        )

    try:
        entry = host_config.add_host(label, ssh_alias)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=409)

    mgr.reload_hosts()
    return web.json_response(entry, status=201)


async def handle_remove_host(request: web.Request) -> web.Response:
    """Remove a configured host."""
    host_config: HostConfig = request.app["host_config"]
    mgr: SessionManager = request.app["session_manager"]
    host_id = request.match_info["host_id"]

    try:
        removed = host_config.remove_host(host_id)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if not removed:
        return web.json_response(
            {"error": f"Host '{host_id}' not found"}, status=404
        )

    # Clean up sessions and ttyd processes for the removed host.
    await mgr.remove_host_sessions(host_id)

    mgr.reload_hosts()
    return web.json_response({"id": host_id, "deleted": True})


# ---------------------------------------------------------------------------
# API routes — sessions (host-scoped)
# ---------------------------------------------------------------------------


async def handle_sessions(request: web.Request) -> web.Response:
    mgr: SessionManager = request.app["session_manager"]
    settings = request.app["settings"]
    host_id = request.match_info["host_id"]

    page = _int_param(request, "page", 1)
    page_size = _int_param(request, "page_size", settings.session_page_size)

    data = mgr.get_sessions(host_id, page=page, page_size=page_size)
    return web.json_response(data)


async def handle_panes(request: web.Request) -> web.Response:
    mgr: SessionManager = request.app["session_manager"]
    host_id = request.match_info["host_id"]
    session_name = request.match_info["session_name"]

    host_sessions = mgr.sessions_for_host(host_id)
    if session_name not in host_sessions:
        return web.json_response(
            {"error": f"Session '{session_name}' not found"},
            status=404,
        )

    panes = await mgr.get_panes(host_id, session_name)

    safe_host = urlquote(host_id, safe="")
    safe_name = urlquote(session_name, safe="")
    for pane in panes:
        port = pane.pop("port", None)
        pane["ttyd_url"] = f"/terminal/{safe_host}/{safe_name}/" if port else None

    return web.json_response({"session": session_name, "panes": panes})


async def handle_session_detail(request: web.Request) -> web.Response:
    """Return metadata and ttyd_url for a single session."""
    mgr: SessionManager = request.app["session_manager"]
    host_id = request.match_info["host_id"]
    session_name = request.match_info["session_name"]

    host_sessions = mgr.sessions_for_host(host_id)
    sess = host_sessions.get(session_name)
    if sess is None:
        return web.json_response(
            {"error": f"Session '{session_name}' not found"},
            status=404,
        )

    safe_host = urlquote(host_id, safe="")
    safe_name = urlquote(session_name, safe="")
    ttyd_url = f"/terminal/{safe_host}/{safe_name}/" if sess.port else None

    return web.json_response({
        "name": sess.name,
        "host_id": sess.host_id,
        "windows": sess.windows,
        "attached": sess.attached,
        "created_epoch": sess.created_epoch,
        "ttyd_url": ttyd_url,
    })


async def handle_thumbnail(request: web.Request) -> web.Response:
    """Return an SVG snapshot thumbnail for a session."""
    mgr: SessionManager = request.app["session_manager"]
    host_id = request.match_info["host_id"]
    session_name = request.match_info["session_name"]

    svg = await mgr.get_thumbnail_svg(host_id, session_name)
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

    total = mgr.total_session_count()

    return web.json_response({
        "status": "ok",
        "sessions": total,
        "uptime": round(uptime, 1),
    })


async def handle_create_session(request: web.Request) -> web.Response:
    """Create a new tmux session on a host."""
    mgr: SessionManager = request.app["session_manager"]
    host_id = request.match_info["host_id"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    name = body.get("name", "").strip()
    if not name:
        return web.json_response({"error": "'name' is required"}, status=400)

    cwd = body.get("cwd") or None
    layout_type = body.get("layout_type") or None
    layout_spec = body.get("layout_spec") or None
    pane_commands = body.get("pane_commands") or None

    result = await mgr.create_session(
        host_id, name, cwd=cwd, layout_type=layout_type,
        layout_spec=layout_spec, pane_commands=pane_commands,
    )

    if "error" in result:
        status = 409 if "already exists" in result["error"] else 400
        return web.json_response(result, status=status)

    return web.json_response(result, status=201)


async def handle_delete_session(request: web.Request) -> web.Response:
    """Delete (kill) an existing tmux session."""
    mgr: SessionManager = request.app["session_manager"]
    host_id = request.match_info["host_id"]
    session_name = request.match_info["session_name"]

    result = await mgr.delete_session(host_id, session_name)

    if "error" in result:
        status = 404 if "not found" in result["error"] else 500
        return web.json_response(result, status=status)

    return web.json_response(result)


async def handle_path_completion(request: web.Request) -> web.Response:
    """Return directory completions for a path prefix (localhost only)."""
    mgr: SessionManager = request.app["session_manager"]
    host_id = request.match_info["host_id"]

    # Path completion only makes sense for localhost.
    host_config: HostConfig = request.app["host_config"]
    host = host_config.get_host(host_id)
    if host is None or host["type"] != "local":
        return web.json_response({"completions": []})

    prefix = request.query.get("prefix", "")
    completions = mgr.list_directories(prefix)
    return web.json_response({"completions": completions})


# ---------------------------------------------------------------------------
# API routes — templates
# ---------------------------------------------------------------------------


async def handle_list_templates(request: web.Request) -> web.Response:
    """Return all templates with extracted macro variable names per template."""
    store: TemplateStore = request.app["template_store"]
    templates = store.list_templates()

    result = []
    for t in templates:
        fields = [t["name"], t["directory"], t["layout_spec"]]
        fields.extend(t.get("pane_commands", []))
        variables = extract_variables(fields)
        result.append({**t, "variables": variables})

    return web.json_response({"templates": result})


async def handle_create_template(request: web.Request) -> web.Response:
    """Save a new template from the current form state."""
    store: TemplateStore = request.app["template_store"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    template_name = body.get("template_name", "").strip()
    if not template_name:
        return web.json_response({"error": "'template_name' is required"}, status=400)

    name = body.get("name", "").strip()
    directory = body.get("directory", "").strip()
    layout_type = body.get("layout_type", "none").strip()
    layout_spec = body.get("layout_spec", "").strip()
    pane_commands = body.get("pane_commands", [])

    if not isinstance(pane_commands, list):
        return web.json_response({"error": "'pane_commands' must be an array"}, status=400)

    # Validate macro placeholders in all template content fields.
    for label, text in [("name", name), ("directory", directory),
                        ("layout_spec", layout_spec)]:
        try:
            validate_placeholders(text)
        except ValueError as exc:
            return web.json_response(
                {"error": f"Invalid placeholder in '{label}': {exc}"}, status=400
            )
    for i, cmd in enumerate(pane_commands):
        try:
            validate_placeholders(cmd)
        except ValueError as exc:
            return web.json_response(
                {"error": f"Invalid placeholder in pane command {i}: {exc}"}, status=400
            )

    try:
        entry = store.add_template(
            template_name, name, directory, layout_type, layout_spec, pane_commands
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=409)

    # Attach extracted variables for immediate frontend use.
    fields = [name, directory, layout_spec] + pane_commands
    entry["variables"] = extract_variables(fields)
    return web.json_response(entry, status=201)


async def handle_update_template(request: web.Request) -> web.Response:
    """Update all content fields of an existing template (keeps template_name)."""
    store: TemplateStore = request.app["template_store"]
    template_name = request.match_info["template_name"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    name = body.get("name", "").strip()
    directory = body.get("directory", "").strip()
    layout_type = body.get("layout_type", "none").strip()
    layout_spec = body.get("layout_spec", "").strip()
    pane_commands = body.get("pane_commands", [])

    if not isinstance(pane_commands, list):
        return web.json_response({"error": "'pane_commands' must be an array"}, status=400)

    # Validate macro placeholders.
    for label, text in [("name", name), ("directory", directory),
                        ("layout_spec", layout_spec)]:
        try:
            validate_placeholders(text)
        except ValueError as exc:
            return web.json_response(
                {"error": f"Invalid placeholder in '{label}': {exc}"}, status=400
            )
    for i, cmd in enumerate(pane_commands):
        try:
            validate_placeholders(cmd)
        except ValueError as exc:
            return web.json_response(
                {"error": f"Invalid placeholder in pane command {i}: {exc}"}, status=400
            )

    try:
        entry = store.update_template(
            template_name, name, directory, layout_type, layout_spec, pane_commands
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=404)

    fields = [name, directory, layout_spec] + pane_commands
    entry["variables"] = extract_variables(fields)
    return web.json_response(entry)


async def handle_rename_template(request: web.Request) -> web.Response:
    """Rename a template (PATCH with {"new_name": "..."})."""
    store: TemplateStore = request.app["template_store"]
    template_name = request.match_info["template_name"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    new_name = body.get("new_name", "").strip()
    if not new_name:
        return web.json_response({"error": "'new_name' is required"}, status=400)

    try:
        entry = store.rename_template(template_name, new_name)
    except ValueError as exc:
        msg = str(exc)
        status = 404 if "not found" in msg else 409
        return web.json_response({"error": msg}, status=status)

    return web.json_response(entry)


async def handle_delete_template(request: web.Request) -> web.Response:
    """Delete a template by name."""
    store: TemplateStore = request.app["template_store"]
    template_name = request.match_info["template_name"]

    deleted = store.delete_template(template_name)
    if not deleted:
        return web.json_response(
            {"error": f"Template '{template_name}' not found"}, status=404
        )

    return web.json_response({"template_name": template_name, "deleted": True})


async def handle_create_from_template(request: web.Request) -> web.Response:
    """Create a new tmux session by rendering a template with variable values."""
    store: TemplateStore = request.app["template_store"]
    mgr: SessionManager = request.app["session_manager"]
    host_id = request.match_info["host_id"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    template_name = body.get("template_name", "").strip()
    if not template_name:
        return web.json_response({"error": "'template_name' is required"}, status=400)

    tpl = store.get_template(template_name)
    if tpl is None:
        return web.json_response(
            {"error": f"Template '{template_name}' not found"}, status=404
        )

    variables: dict[str, str] = body.get("variables", {})
    if not isinstance(variables, dict):
        return web.json_response({"error": "'variables' must be an object"}, status=400)

    # Collect all template content fields for rendering.
    fields = [tpl["name"], tpl["directory"], tpl["layout_spec"]]
    fields.extend(tpl.get("pane_commands", []))

    # Validate that all required variables are provided and non-empty.
    required_vars = extract_variables(fields)
    missing = [v for v in required_vars if not variables.get(v, "").strip()]
    if missing:
        return web.json_response(
            {"error": f"Missing or empty variables: {', '.join(missing)}"}, status=400
        )

    # Render all template fields.
    try:
        rendered_name = render(tpl["name"], variables)
        rendered_dir = render(tpl["directory"], variables)
        rendered_spec = render(tpl["layout_spec"], variables)
        rendered_cmds = [render(c, variables) for c in tpl.get("pane_commands", [])]
    except ValueError as exc:
        return web.json_response({"error": f"Render error: {exc}"}, status=400)

    # Allow optional overlay pane_commands from the request.
    overlay_commands = body.get("pane_commands")
    if overlay_commands is not None and not isinstance(overlay_commands, list):
        return web.json_response({"error": "'pane_commands' must be an array"}, status=400)

    # Merge: explicit overlay > rendered template commands.
    effective_commands = overlay_commands if overlay_commands is not None else rendered_cmds

    # Determine layout.
    layout_type = tpl["layout_type"] if tpl["layout_type"] != "none" else None
    layout_spec = rendered_spec if layout_type else None

    result = await mgr.create_session(
        host_id,
        rendered_name,
        cwd=rendered_dir or None,
        layout_type=layout_type,
        layout_spec=layout_spec,
        pane_commands=effective_commands or None,
        _from_template=True,
    )

    if "error" in result:
        status = 409 if "already exists" in result["error"] else 400
        return web.json_response(result, status=status)

    return web.json_response(result, status=201)



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


def _ttyd_target(request: web.Request) -> tuple[str | None, str | None, int | None]:
    """Resolve (host_id, session_name, port) from the route.

    Returns (None, None, None) when the session doesn't exist or has no port.
    """
    host_id = request.match_info["host_id"]
    session_name = request.match_info["session_name"]
    mgr: SessionManager = request.app["session_manager"]
    host_sessions = mgr.sessions_for_host(host_id)
    sess = host_sessions.get(session_name)
    if sess is None or sess.port is None:
        return None, None, None
    return host_id, session_name, sess.port


async def handle_terminal(request: web.Request) -> web.Response | web.WebSocketResponse:
    """Reverse-proxy HTTP and WebSocket requests to the session's ttyd."""
    host_id, session_name, port = _ttyd_target(request)
    if port is None:
        return web.Response(status=502, text="Terminal not available")

    # Reconstruct the full path ttyd expects (started with --base-path).
    safe_host = urlquote(host_id, safe="")
    safe_name = urlquote(session_name, safe="")
    suffix = request.match_info.get("path", "")
    target = f"http://127.0.0.1:{port}/terminal/{safe_host}/{safe_name}/{suffix}"
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
            headers: dict[str, str] = {}
            for h in (
                "Content-Type", "Content-Encoding", "Cache-Control",
                "ETag", "Last-Modified",
            ):
                if h in resp.headers:
                    headers[h] = resp.headers[h]
            return web.Response(status=resp.status, body=body, headers=headers)
    except Exception as exc:
        log.warning("Terminal HTTP proxy error → %s: %s", target, exc)
        return web.Response(status=502, text="Terminal proxy error")


async def _proxy_ws(request: web.Request, target: str) -> web.WebSocketResponse:
    """Bridge a WebSocket between the browser and ttyd."""
    # Negotiate subprotocol with the browser (ttyd uses 'tty').
    protocols: tuple[str, ...] = ()
    proto_header = request.headers.get("Sec-WebSocket-Protocol", "")
    if proto_header:
        protocols = tuple(
            p.strip() for p in proto_header.split(",") if p.strip()
        )

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
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break

            async def _fwd_server_to_client():
                """browser → ttyd."""
                async for msg in ws_server:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await ws_client.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await ws_client.send_bytes(msg.data)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
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


def _build_ssl_context(settings: RuntimeSettings) -> ssl.SSLContext | None:
    """Create an SSL context from settings.tls_cert/tls_key, or None for plain HTTP."""
    if not settings.tls_cert or not settings.tls_key:
        return None

    cert_path = Path(settings.tls_cert)
    key_path = Path(settings.tls_key)

    if not cert_path.is_file():
        log.warning(
            "TLS_CERT=%s does not exist; falling back to plain HTTP", settings.tls_cert
        )
        return None
    if not key_path.is_file():
        log.warning(
            "TLS_KEY=%s does not exist; falling back to plain HTTP", settings.tls_key
        )
        return None

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert_path), str(key_path))
    log.info("TLS enabled — cert=%s key=%s", cert_path, key_path)
    return ctx


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


async def on_startup(app: web.Application) -> None:
    global _last_activity, _wake_event
    settings: RuntimeSettings = app["settings"]

    host_config = HostConfig(path=settings.hosts_config_path)
    app["host_config"] = host_config

    template_store = TemplateStore(path=settings.templates_config_path)
    app["template_store"] = template_store

    mgr = SessionManager(host_config, settings)
    app["session_manager"] = mgr
    app["start_time"] = time.monotonic()

    # Shared HTTP client for the reverse proxy.
    app["client_session"] = aiohttp.ClientSession(auto_decompress=False)

    # Kill orphaned ttyd processes from a previous server run.
    await mgr.kill_stale_ttyd()

    # Run an initial poll so the API has data before the first client connects.
    await mgr.poll_sessions()
    # Initialise activity tracking so the server starts in active mode.
    _last_activity = time.monotonic()
    _wake_event = asyncio.Event()

    # Start the background polling loop.
    app["poll_task"] = asyncio.create_task(
        mgr.start_polling(_get_last_activity, _get_wake_event),
        name="session-poll-driver",
    )

    scheme = "https" if app.get("_tls_enabled") else "http"
    total = mgr.total_session_count()
    log.info(
        "panoptic started on %s://%s:%d — %d session(s) discovered across %d host(s)",
        scheme,
        settings.host,
        settings.port,
        total,
        len(host_config.list_hosts()),
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
    log.info("panoptic shut down cleanly")


def build_app(settings: RuntimeSettings) -> web.Application:
    app = web.Application(middlewares=[
        client_tracking_middleware,
        security_headers_middleware,
    ])
    app["settings"] = settings

    # Root
    app.router.add_get("/", handle_index)
    app.router.add_get("/sw.js", handle_service_worker)
    app.router.add_get("/manifest.json", handle_manifest)

    # Host management
    app.router.add_get("/api/hosts", handle_hosts)
    app.router.add_post("/api/hosts", handle_add_host)
    app.router.add_delete("/api/hosts/{host_id}", handle_remove_host)

    # Health
    app.router.add_get("/api/health", handle_health)

    # Host-scoped session routes
    app.router.add_get(
        "/api/hosts/{host_id}/sessions", handle_sessions
    )
    app.router.add_post(
        "/api/hosts/{host_id}/sessions", handle_create_session
    )
    app.router.add_get(
        "/api/hosts/{host_id}/sessions/{session_name}", handle_session_detail
    )
    app.router.add_delete(
        "/api/hosts/{host_id}/sessions/{session_name}", handle_delete_session
    )
    app.router.add_get(
        "/api/hosts/{host_id}/sessions/{session_name}/panes", handle_panes
    )
    app.router.add_get(
        "/api/hosts/{host_id}/sessions/{session_name}/thumbnail.svg",
        handle_thumbnail,
    )
    app.router.add_get(
        "/api/hosts/{host_id}/completions/path", handle_path_completion
    )

    # Template management
    app.router.add_get("/api/templates", handle_list_templates)
    app.router.add_post("/api/templates", handle_create_template)
    app.router.add_put("/api/templates/{template_name}", handle_update_template)
    app.router.add_patch("/api/templates/{template_name}", handle_rename_template)
    app.router.add_delete("/api/templates/{template_name}", handle_delete_template)

    # Template-based session creation
    app.router.add_post(
        "/api/hosts/{host_id}/sessions/from-template", handle_create_from_template
    )

    # ttyd reverse proxy — host-scoped catch-all
    app.router.add_route(
        "*",
        "/terminal/{host_id}/{session_name}/{path:.*}",
        handle_terminal,
    )

    # Static files
    app.router.add_static("/static", STATIC_DIR, show_index=False)

    # Lifecycle hooks
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_server(settings: RuntimeSettings) -> None:
    """Configure logging, build the app, and run it.  Called by the CLI."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ssl_ctx = _build_ssl_context(settings)
    app = build_app(settings)
    app["_tls_enabled"] = ssl_ctx is not None

    web.run_app(
        app,
        host=settings.host,
        port=settings.port,
        ssl_context=ssl_ctx,
        print=None,
    )


def main() -> None:
    run_server(RuntimeSettings.from_defaults())


if __name__ == "__main__":
    main()
