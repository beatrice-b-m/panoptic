from __future__ import annotations

"""panoptic server: HTTP API + static file serving + session lifecycle.

When TLS_CERT and TLS_KEY point to valid files (e.g. from ``tailscale cert``),
the server terminates TLS directly. Terminal sessions use a WebSocket bridge
to tmux control mode through the dashboard port so only one port needs to be
exposed.

Session routes are scoped under ``/api/hosts/{host_id}/sessions/...`` and
terminal WebSocket bridge lives at ``/ws/hosts/{host_id}/sessions/{session_name}``.
"""

import os
import json
import asyncio
import logging
import ssl
import time
import socket
from pathlib import Path
from urllib.parse import quote as urlquote, urlparse

import aiohttp
from aiohttp import web

from config import RuntimeSettings
from control_bridge import ControlBridge
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
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # CSP: allow self + inline styles + ws/wss for terminal connections
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' ws: wss:; img-src 'self' data:; font-src 'self' data:;",
    )
    if request.app.get("_tls_enabled"):
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
    return response



@web.middleware
async def origin_validation_middleware(request: web.Request, handler):
    """Reject cross-origin state-changing requests.

    Validates that Origin (or Referer) matches the Host header for
    POST/PUT/PATCH/DELETE and WebSocket upgrades.  Requests with no
    Origin header are allowed (same-origin browser requests may omit it).
    """
    is_upgrade = request.headers.get("Upgrade", "").lower() == "websocket"
    is_mutating = request.method in ("POST", "PUT", "PATCH", "DELETE")

    if not is_upgrade and not is_mutating:
        return await handler(request)

    origin = request.headers.get("Origin")
    if origin is None:
        # Fall back to Referer.
        referer = request.headers.get("Referer")
        if referer:
            parsed = urlparse(referer)
            origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else None

    if origin is None:
        # No origin info at all — likely a same-origin request or a non-browser client.
        return await handler(request)

    # Extract the host portion from Origin and compare to the Host header.
    origin_parsed = urlparse(origin)
    origin_host = origin_parsed.hostname or ""
    origin_port = origin_parsed.port

    request_host_header = request.host  # "host:port" or just "host"
    # Parse the Host header.
    if ":" in request_host_header:
        req_host, req_port_s = request_host_header.rsplit(":", 1)
        try:
            req_port = int(req_port_s)
        except ValueError:
            req_host = request_host_header
            req_port = None
    else:
        req_host = request_host_header
        req_port = None

    # Normalize localhost variants.
    _LOCALHOST = frozenset({"localhost", "127.0.0.1", "::1"})
    o_host = origin_host.lower()
    r_host = req_host.lower()
    hosts_match = (o_host == r_host) or (o_host in _LOCALHOST and r_host in _LOCALHOST)

    # Port comparison: if origin port is None, use scheme default.
    if origin_port is None:
        origin_port = 443 if origin_parsed.scheme == "https" else 80

    ports_match = (origin_port == req_port) or (req_port is None)

    if hosts_match and ports_match:
        return await handler(request)

    log.warning(
        "Rejected cross-origin %s request: Origin=%s Host=%s",
        request.method, origin, request_host_header,
    )
    return web.json_response(
        {"error": "Cross-origin request rejected"}, status=403
    )

# ---------------------------------------------------------------------------
# Request body validation helpers
# ---------------------------------------------------------------------------


class _ValidationError(Exception):
    """Raised by validation helpers to produce a clean 4xx response."""
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _require_str(body: dict, field: str, *, strip: bool = True) -> str:
    """Extract a required non-empty string field from a JSON body.

    Raises _ValidationError on type mismatch or empty value.
    """
    value = body.get(field)
    if value is None or not isinstance(value, str):
        raise _ValidationError(f"'{field}' must be a non-empty string", 400)
    if strip:
        value = value.strip()
    if not value:
        raise _ValidationError(f"'{field}' is required", 400)
    return value


def _optional_str(body: dict, field: str, default: str = "", *, strip: bool = True) -> str:
    """Extract an optional string field; returns *default* when absent or empty."""
    value = body.get(field)
    if value is None:
        return default
    if not isinstance(value, str):
        raise _ValidationError(f"'{field}' must be a string", 400)
    return value.strip() if strip else value


def _optional_str_or_none(body: dict, field: str, *, strip: bool = True) -> str | None:
    """Extract an optional string field; returns None when absent or falsy."""
    value = body.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _ValidationError(f"'{field}' must be a string", 400)
    cleaned = value.strip() if strip else value
    return cleaned or None


def _require_str_list(body: dict, field: str) -> list[str]:
    """Extract a field that must be a list of strings.

    Returns [] when the field is absent.
    """
    value = body.get(field)
    if value is None:
        return []
    if not isinstance(value, list):
        raise _ValidationError(f"'{field}' must be an array", 400)
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise _ValidationError(f"'{field}[{i}]' must be a string", 400)
    return value


def _require_str_dict(body: dict, field: str) -> dict[str, str]:
    """Extract a field that must be a dict with string keys and string values.

    Returns {} when the field is absent.
    """
    value = body.get(field, {})
    if not isinstance(value, dict):
        raise _ValidationError(f"'{field}' must be an object", 400)
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise _ValidationError(
                f"'{field}' must have string keys and string values", 400
            )
    return value


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

    try:
        label = _require_str(body, "label")
        ssh_alias = _require_str(body, "ssh_alias")
    except _ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=exc.status)

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

    # Clean up sessions for the removed host.
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

    return web.json_response({"session": session_name, "panes": panes})


async def handle_session_detail(request: web.Request) -> web.Response:
    """Return metadata and ws_url for a single session."""
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
    ws_url = f"/ws/hosts/{safe_host}/sessions/{safe_name}"

    return web.json_response({
        "name": sess.name,
        "host_id": sess.host_id,
        "windows": sess.windows,
        "attached": sess.attached,
        "created_epoch": sess.created_epoch,
        "ws_url": ws_url,
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

    try:
        name = _require_str(body, "name")
        cwd = _optional_str_or_none(body, "cwd")
        layout_type = _optional_str_or_none(body, "layout_type")
        layout_spec = _optional_str_or_none(body, "layout_spec")
        pane_commands = _require_str_list(body, "pane_commands") or None
    except _ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=exc.status)

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

    try:
        template_name = _require_str(body, "template_name")
        name = _optional_str(body, "name")
        directory = _optional_str(body, "directory")
        layout_type = _optional_str(body, "layout_type", "none")
        layout_spec = _optional_str(body, "layout_spec")
        pane_commands = _require_str_list(body, "pane_commands")
    except _ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=exc.status)

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

    try:
        name = _optional_str(body, "name")
        directory = _optional_str(body, "directory")
        layout_type = _optional_str(body, "layout_type", "none")
        layout_spec = _optional_str(body, "layout_spec")
        pane_commands = _require_str_list(body, "pane_commands")
    except _ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=exc.status)

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

    try:
        new_name = _require_str(body, "new_name")
    except _ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=exc.status)

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

    try:
        template_name = _require_str(body, "template_name")
    except _ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=exc.status)

    tpl = store.get_template(template_name)
    if tpl is None:
        return web.json_response(
            {"error": f"Template '{template_name}' not found"}, status=404
        )

    try:
        variables = _require_str_dict(body, "variables")
    except _ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=exc.status)

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
    if "pane_commands" in body:
        try:
            overlay_commands = _require_str_list(body, "pane_commands") or None
        except _ValidationError as exc:
            return web.json_response({"error": str(exc)}, status=exc.status)
    else:
        overlay_commands = None

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
# Terminal WebSocket bridge — connects browser to tmux control mode
# ---------------------------------------------------------------------------


async def handle_terminal_ws(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint bridging the browser to a tmux control mode client."""
    host_id = request.match_info["host_id"]
    session_name = request.match_info["session_name"]
    mgr: SessionManager = request.app["session_manager"]
    settings: RuntimeSettings = request.app["settings"]

    # Verify session exists
    host_sessions = mgr.sessions_for_host(host_id)
    if session_name not in host_sessions:
        return web.Response(status=404, text="Session not found")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Determine cols/rows from query params or config defaults
    try:
        cols = int(request.query.get("cols", settings.control_bridge_cols))
        rows = int(request.query.get("rows", settings.control_bridge_rows))
    except ValueError:
        cols, rows = settings.control_bridge_cols, settings.control_bridge_rows

    host = request.app["host_config"].get_host(host_id)
    ssh_alias = host.get("ssh_alias") if host and host["type"] == "ssh" else None

    bridge = ControlBridge(
        session_name=session_name,
        cols=cols,
        rows=rows,
        tmux_path=mgr._tmux_path,
        ssh_alias=ssh_alias,
        ssh_connect_timeout=settings.ssh_connect_timeout,
    )

    bridges: dict = request.app["active_bridges"]
    bridge_key = id(ws)  # unique per connection
    bridges[bridge_key] = bridge

    relay_task: asyncio.Task | None = None
    try:
        await bridge.start()

        async def relay_events():
            """Push bridge events to the browser."""
            captured_panes: set[str] = set()
            async for event in bridge.events():
                if ws.closed:
                    break
                if event["type"] == "output":
                    pane_id_bytes = event["pane_id"].encode()
                    frame = (
                        b"\x01"
                        + len(pane_id_bytes).to_bytes(2, "big")
                        + pane_id_bytes
                        + event["data"]
                    )
                    await ws.send_bytes(frame)
                elif event["type"] == "exit":
                    if not ws.closed:
                        await ws.send_str(json.dumps({"type": "exit"}))
                    break
                else:
                    # layout, window_add, window_close, etc. — JSON text frame
                    await ws.send_str(json.dumps(event))
                    # After forwarding a layout event, capture initial content
                    # for any panes the browser hasn't seen yet.  The captured
                    # text arrives as synthetic output events on subsequent
                    # iterations, filling the freshly-created xterm.js instances.
                    if event["type"] == "layout":
                        new_ids = [
                            p["pane_id"] for p in event["panes"]
                            if p["pane_id"] not in captured_panes
                        ]
                        if new_ids:
                            captured_panes.update(new_ids)
                            await bridge.capture_panes(new_ids)

        relay_task = asyncio.create_task(relay_events())

        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                msg_type = data.get("type")
                if msg_type == "input":
                    pane_id = data.get("pane_id", "")
                    try:
                        raw = bytes.fromhex(data.get("data", ""))
                    except ValueError:
                        continue
                    await bridge.send_keys(pane_id, raw)
                elif msg_type == "select_pane":
                    await bridge.select_pane(data.get("pane_id", ""))
                elif msg_type == "resize":
                    try:
                        next_cols = int(data.get("cols", cols))
                        next_rows = int(data.get("rows", rows))
                    except (TypeError, ValueError):
                        continue
                    cols, rows = next_cols, next_rows
                    await bridge.resize(next_cols, next_rows)
                elif msg_type == "resize_pane":
                    pane_id = data.get("pane_id", "")
                    try:
                        p_cols = int(data["cols"])
                        p_rows = int(data["rows"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    await bridge.resize_pane(pane_id, p_cols, p_rows)
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

    except Exception:
        log.exception("WebSocket bridge error for %s/%s", host_id, session_name)
    finally:
        if relay_task is not None:
            relay_task.cancel()
            try:
                await relay_task
            except asyncio.CancelledError:
                pass
        await bridge.stop()
        bridges.pop(bridge_key, None)

    return ws

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

    app["active_bridges"] = {}

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
    bridges = app.get("active_bridges", {})
    for bridge in list(bridges.values()):
        await bridge.stop()
    bridges.clear()
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
        origin_validation_middleware,
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

    app.router.add_get(
        "/ws/hosts/{host_id}/sessions/{session_name}",
        handle_terminal_ws,
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


    # Fail fast if the HTTP port is already in use — avoid conflicts with
    # a running instance.
    _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        _sock.bind((settings.host, settings.port))
    except OSError:
        log.error(
            "Port %d already in use; aborting startup to avoid conflicts "
            "with the running instance",
            settings.port,
        )
        raise SystemExit(1)
    finally:
        _sock.close()

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
