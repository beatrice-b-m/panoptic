#!/usr/bin/env python3
"""tmux-dash CLI — the canonical way to start the dashboard server.

Usage::

    python3 tmux_dash_cli.py serve                # default settings
    python3 tmux_dash_cli.py serve --headless      # loopback-only + SSH instructions
    python3 tmux_dash_cli.py serve --port 8080     # custom port

See ``python3 tmux_dash_cli.py serve --help`` for all flags.
"""

from __future__ import annotations

import argparse
import getpass
import socket
import sys
import textwrap

from config import RuntimeSettings


def _build_serve_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``serve`` subcommand and all its flags."""
    defaults = RuntimeSettings.from_defaults()

    p = subparsers.add_parser(
        "serve",
        help="Start the tmux-dash server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""\
            Start the tmux-dash dashboard server.

            By default the server binds to {host}:{port} and serves the
            dashboard over HTTP (or HTTPS when TLS is configured).

            Use --headless for remote/headless servers: it forces all
            listeners to 127.0.0.1 and prints SSH port-forward instructions
            so you can reach the UI from a local browser.
        """.format(host=defaults.host, port=defaults.port)),
    )

    # -- network
    # Use None as default sentinel; detect explicit user override vs. fallback.
    p.add_argument(
        "--host",
        default=None,
        help=f"Dashboard bind address (default: {defaults.host})",
    )
    p.add_argument(
        "--port",
        type=int,
        default=defaults.port,
        help=f"Dashboard HTTP port (default: {defaults.port})",
    )
    p.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="Force loopback binding and print SSH tunnel instructions",
    )

    # -- ttyd
    p.add_argument(
        "--ttyd-bind-host",
        default=None,
        help=f"Interface each ttyd process binds on (default: {defaults.ttyd_bind_host})",
    )
    p.add_argument(
        "--ttyd-port-start",
        type=int,
        default=defaults.ttyd_port_start,
        help=f"First port in the ttyd pool (default: {defaults.ttyd_port_start})",
    )
    p.add_argument(
        "--ttyd-port-end",
        type=int,
        default=defaults.ttyd_port_end,
        help=f"Last port in the ttyd pool (default: {defaults.ttyd_port_end})",
    )
    p.add_argument(
        "--ttyd-binary",
        default=defaults.ttyd_binary,
        help=f"Path to ttyd executable (default: {defaults.ttyd_binary})",
    )

    # -- tmux / beamux
    p.add_argument(
        "--tmux-binary",
        default=defaults.tmux_binary,
        help=f"Path to tmux executable (default: {defaults.tmux_binary})",
    )
    p.add_argument(
        "--beamux-binary",
        default=defaults.beamux_binary,
        help="Path to beamux script",
    )

    # -- polling
    p.add_argument(
        "--poll-interval-active",
        type=int,
        default=defaults.poll_interval_active,
        help=f"Poll interval (seconds) when clients are connected (default: {defaults.poll_interval_active})",
    )
    p.add_argument(
        "--poll-interval-idle",
        type=int,
        default=defaults.poll_interval_idle,
        help=f"Poll interval (seconds) when idle (default: {defaults.poll_interval_idle})",
    )

    # -- display
    p.add_argument(
        "--session-page-size",
        type=int,
        default=defaults.session_page_size,
        help=f"Sessions per page in the dashboard (default: {defaults.session_page_size})",
    )

    # -- TLS
    p.add_argument(
        "--tls-cert",
        default=defaults.tls_cert,
        help="Path to TLS certificate (PEM)",
    )
    p.add_argument(
        "--tls-key",
        default=defaults.tls_key,
        help="Path to TLS private key (PEM)",
    )

    # -- hosts / SSH
    p.add_argument(
        "--hosts-config-path",
        default=defaults.hosts_config_path,
        help="Path to hosts.json configuration file",
    )
    p.add_argument(
        "--ssh-connect-timeout",
        type=int,
        default=defaults.ssh_connect_timeout,
        help=f"SSH connect timeout in seconds (default: {defaults.ssh_connect_timeout})",
    )

    # -- logging
    p.add_argument(
        "--log-level",
        default=defaults.log_level,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help=f"Logging level (default: {defaults.log_level})",
    )



def _validate_serve_args(args: argparse.Namespace) -> None:
    """Validate flag combinations; exit with a clear message on conflict."""
    if args.headless:
        # None means user didn't pass the flag — we silently override to loopback.
        # Any explicit non-loopback value is a conflict.
        if args.host is not None and args.host != "127.0.0.1":
            print(
                f"error: --headless requires dashboard to bind on 127.0.0.1, "
                f"but --host {args.host!r} was specified.\n"
                f"Either drop --host or use --host 127.0.0.1.",
                file=sys.stderr,
            )
            sys.exit(1)

        if args.ttyd_bind_host is not None and args.ttyd_bind_host != "127.0.0.1":
            print(
                f"error: --headless requires ttyd to bind on 127.0.0.1, "
                f"but --ttyd-bind-host {args.ttyd_bind_host!r} was specified.\n"
                f"Either drop --ttyd-bind-host or use --ttyd-bind-host 127.0.0.1.",
                file=sys.stderr,
            )
            sys.exit(1)

    if args.ttyd_port_start > args.ttyd_port_end:
        print(
            f"error: --ttyd-port-start ({args.ttyd_port_start}) must be <= "
            f"--ttyd-port-end ({args.ttyd_port_end})",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.port < 1 or args.port > 65535:
        print(f"error: --port must be 1-65535, got {args.port}", file=sys.stderr)
        sys.exit(1)

def _build_settings(args: argparse.Namespace) -> RuntimeSettings:
    """Construct RuntimeSettings from parsed CLI arguments."""
    defaults = RuntimeSettings.from_defaults()

    if args.headless:
        host = "127.0.0.1"
        ttyd_bind_host = "127.0.0.1"
    else:
        host = args.host if args.host is not None else defaults.host
        ttyd_bind_host = (
            args.ttyd_bind_host
            if args.ttyd_bind_host is not None
            else defaults.ttyd_bind_host
        )

    return RuntimeSettings(
        host=host,
        port=args.port,
        ttyd_port_start=args.ttyd_port_start,
        ttyd_port_end=args.ttyd_port_end,
        ttyd_bind_host=ttyd_bind_host,
        ttyd_binary=args.ttyd_binary,
        tmux_binary=args.tmux_binary,
        beamux_binary=args.beamux_binary,
        ttyd_font_family=defaults.ttyd_font_family,
        poll_interval_active=args.poll_interval_active,
        poll_interval_idle=args.poll_interval_idle,
        session_page_size=args.session_page_size,
        log_level=args.log_level,
        tls_cert=args.tls_cert,
        tls_key=args.tls_key,
        hosts_config_path=args.hosts_config_path,
        ssh_connect_timeout=args.ssh_connect_timeout,
        headless=args.headless,
    )


def _print_headless_instructions(settings: RuntimeSettings) -> None:
    """Print SSH port-forwarding instructions for headless mode."""
    hostname = socket.gethostname()
    user = getpass.getuser()
    scheme = "https" if settings.tls_cert else "http"

    print()
    print("=" * 60)
    print("  tmux-dash running in HEADLESS mode")
    print("=" * 60)
    print()
    print(f"  Dashboard bound to 127.0.0.1:{settings.port} (loopback only)")
    print(f"  ttyd processes bound to 127.0.0.1 (loopback only)")
    print()
    print("  To access the dashboard from your local machine,")
    print("  open an SSH tunnel:")
    print()
    print(f"    ssh -N -L {settings.port}:127.0.0.1:{settings.port} {user}@{hostname}")
    print()
    print(f"  Then browse: {scheme}://127.0.0.1:{settings.port}")
    print()
    print("  (All terminal traffic is reverse-proxied through the")
    print("  dashboard port — no additional port forwards needed.)")
    print("=" * 60)
    print()


def _cmd_serve(args: argparse.Namespace) -> None:
    """Execute the ``serve`` subcommand."""
    _validate_serve_args(args)
    settings = _build_settings(args)

    if settings.headless:
        _print_headless_instructions(settings)

    from server import run_server
    run_server(settings)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tmux-dash",
        description="tmux-dash — browser-based tmux session monitor and terminal",
    )
    subparsers = parser.add_subparsers(dest="command")
    _build_serve_parser(subparsers)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "serve":
        _cmd_serve(args)


if __name__ == "__main__":
    main()
