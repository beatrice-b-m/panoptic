"""Tests for control_bridge: protocol parsing, octal unescape, layout parser."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from control_bridge import (
    PaneGeometry,
    parse_control_line,
    parse_layout,
    unescape_output,
)


# ---------------------------------------------------------------------------
# unescape_output
# ---------------------------------------------------------------------------


class TestUnescapeOutput:
    def test_plain_ascii(self):
        assert unescape_output("hello world") == b"hello world"

    def test_newline(self):
        assert unescape_output("abc\\012def") == b"abc\ndef"

    def test_carriage_return(self):
        assert unescape_output("line\\015") == b"line\r"

    def test_escape_character(self):
        # ESC = 0x1b = octal 033
        result = unescape_output("\\033[31m")
        assert result == b"\x1b[31m"

    def test_backslash_itself(self):
        # \134 = backslash
        assert unescape_output("a\\134b") == b"a\\b"

    def test_multiple_escapes(self):
        result = unescape_output("\\033[1m\\033[0m")
        assert result == b"\x1b[1m\x1b[0m"

    def test_tab(self):
        # \011 = tab
        assert unescape_output("col1\\011col2") == b"col1\tcol2"

    def test_null_byte(self):
        assert unescape_output("\\000") == b"\x00"

    def test_mixed_text_and_escapes(self):
        result = unescape_output("hello\\012world\\033[0m")
        assert result == b"hello\nworld\x1b[0m"

    def test_empty_string(self):
        assert unescape_output("") == b""

    def test_utf8_passthrough(self):
        """Non-escaped UTF-8 characters pass through unchanged."""
        result = unescape_output("caf\u00e9")
        assert result == "caf\u00e9".encode("utf-8")

    def test_high_octal(self):
        # \377 = 0xff
        assert unescape_output("\\377") == b"\xff"

    def test_not_octal_digits(self):
        """A backslash followed by non-octal digits is passed through."""
        result = unescape_output("\\89x")
        assert result == b"\\89x"


# ---------------------------------------------------------------------------
# parse_layout
# ---------------------------------------------------------------------------


class TestParseLayout:
    def test_single_pane(self):
        # Typical single-pane layout
        layout = "d203,220x50,0,0,%0"
        panes = parse_layout(layout)
        assert len(panes) == 1
        assert panes[0] == PaneGeometry(pane_id="%0", cols=220, rows=50, x=0, y=0)

    def test_two_pane_vertical_split(self):
        # Vertical split: two panes stacked
        layout = "5f2d,220x50,0,0[220x25,0,0,%0,220x24,0,26,%1]"
        panes = parse_layout(layout)
        assert len(panes) == 2
        assert panes[0] == PaneGeometry(pane_id="%0", cols=220, rows=25, x=0, y=0)
        assert panes[1] == PaneGeometry(pane_id="%1", cols=220, rows=24, x=0, y=26)

    def test_two_pane_horizontal_split(self):
        layout = "abc1,220x50,0,0{110x50,0,0,%0,109x50,111,0,%1}"
        panes = parse_layout(layout)
        assert len(panes) == 2
        assert panes[0].pane_id == "%0"
        assert panes[0].cols == 110
        assert panes[1].pane_id == "%1"
        assert panes[1].x == 111

    def test_nested_layout(self):
        # Top/bottom split, bottom has left/right split
        layout = "e1f3,220x50,0,0[220x25,0,0,%0,220x24,0,26{110x24,0,26,%1,109x24,111,26,%2}]"
        panes = parse_layout(layout)
        assert len(panes) == 3
        ids = [p.pane_id for p in panes]
        assert ids == ["%0", "%1", "%2"]
        # Top pane fills width
        assert panes[0].cols == 220
        assert panes[0].rows == 25
        # Bottom-left
        assert panes[1].cols == 110
        assert panes[1].y == 26
        # Bottom-right
        assert panes[2].x == 111

    def test_four_pane_grid(self):
        # 2x2 grid: top split vertically, then each side split horizontally
        layout = (
            "1234,200x50,0,0["
            "200x24,0,0{100x24,0,0,%0,99x24,101,0,%1},"
            "200x25,0,25{100x25,0,25,%2,99x25,101,25,%3}"
            "]"
        )
        panes = parse_layout(layout)
        assert len(panes) == 4
        assert [p.pane_id for p in panes] == ["%0", "%1", "%2", "%3"]

    def test_checksum_stripped(self):
        """The 4-hex-char checksum prefix is removed before parsing."""
        layout = "abcd,80x24,0,0,%5"
        panes = parse_layout(layout)
        assert panes[0].pane_id == "%5"
        assert panes[0].cols == 80

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_layout("")

    def test_malformed_raises(self):
        with pytest.raises(ValueError):
            parse_layout("not-a-layout")


# ---------------------------------------------------------------------------
# parse_control_line
# ---------------------------------------------------------------------------


class TestParseControlLine:
    def test_output_event(self):
        line = "%output %0 hello\\012world"
        event = parse_control_line(line)
        assert event is not None
        assert event["type"] == "output"
        assert event["pane_id"] == "%0"
        assert event["data"] == b"hello\nworld"

    def test_layout_change_event(self):
        line = "%layout-change @0 5f2d,220x50,0,0[220x25,0,0,%0,220x24,0,26,%1]"
        event = parse_control_line(line)
        assert event is not None
        assert event["type"] == "layout"
        assert event["window_id"] == "@0"
        assert len(event["panes"]) == 2
        assert event["panes"][0]["pane_id"] == "%0"

    def test_window_add(self):
        event = parse_control_line("%window-add @1")
        assert event == {"type": "window_add", "window_id": "@1"}

    def test_window_close(self):
        event = parse_control_line("%window-close @2")
        assert event == {"type": "window_close", "window_id": "@2"}

    def test_window_renamed(self):
        event = parse_control_line("%window-renamed @0 my-new-name")
        assert event is not None
        assert event["type"] == "window_renamed"

    def test_session_window_changed(self):
        event = parse_control_line("%session-window-changed $0 @1")
        assert event is not None
        assert event["type"] == "session_window_changed"
        assert event["window_id"] == "@1"

    def test_begin_ignored(self):
        assert parse_control_line("%begin 1234 1 0") is None

    def test_end_ignored(self):
        assert parse_control_line("%end 1234 1 0") is None

    def test_error_ignored(self):
        assert parse_control_line("%error 1234 1 0") is None

    def test_non_notification_ignored(self):
        assert parse_control_line("some random output") is None

    def test_empty_line(self):
        assert parse_control_line("") is None

    def test_output_with_only_escapes(self):
        """An output line with all-escaped content."""
        line = "%output %3 \\033[H\\033[J"
        event = parse_control_line(line)
        assert event is not None
        assert event["data"] == b"\x1b[H\x1b[J"

    def test_malformed_layout_returns_none(self):
        """A %layout-change with unparseable layout string returns None."""
        line = "%layout-change @0 this-is-not-a-layout"
        assert parse_control_line(line) is None



class TestParseLayoutBareIds:
    """Layout strings from real tmux 3.6+ use bare numeric pane IDs."""

    def test_single_pane_bare_id(self):
        layout = "ab0b,120x40,0,0,104"
        panes = parse_layout(layout)
        assert len(panes) == 1
        assert panes[0] == PaneGeometry(pane_id="%104", cols=120, rows=40, x=0, y=0)

    def test_two_pane_vertical_bare(self):
        layout = "5f2d,220x50,0,0[220x25,0,0,0,220x24,0,26,1]"
        panes = parse_layout(layout)
        assert len(panes) == 2
        assert panes[0].pane_id == "%0"
        assert panes[1].pane_id == "%1"

    def test_nested_bare(self):
        layout = "e1f3,220x50,0,0[220x25,0,0,0,220x24,0,26{110x24,0,26,1,109x24,111,26,2}]"
        panes = parse_layout(layout)
        assert len(panes) == 3
        assert [p.pane_id for p in panes] == ["%0", "%1", "%2"]


class TestLayoutChangeWithTrailingTokens:
    """Real %layout-change lines include visible layout and window flags."""

    def test_layout_with_visible_and_flags(self):
        # Real tmux 3.6 output: LAYOUT VISIBLE_LAYOUT FLAGS
        line = "%layout-change @59 ab0b,120x40,0,0,104 ab0b,120x40,0,0,104 *"
        event = parse_control_line(line)
        assert event is not None
        assert event["type"] == "layout"
        assert event["window_id"] == "@59"
        assert len(event["panes"]) == 1
        assert event["panes"][0]["pane_id"] == "%104"

    def test_layout_visible_only(self):
        line = "%layout-change @0 5f2d,220x50,0,0[220x25,0,0,0,220x24,0,26,1] 5f2d,220x50,0,0[220x25,0,0,0,220x24,0,26,1]"
        event = parse_control_line(line)
        assert event is not None
        assert len(event["panes"]) == 2


# ---------------------------------------------------------------------------
# ControlBridge._read_loop — command-response tracking
# ---------------------------------------------------------------------------


import asyncio
from control_bridge import ControlBridge


def _make_bridge() -> ControlBridge:
    """Create a ControlBridge without starting a subprocess."""
    return ControlBridge("test-session", cols=80, rows=24, tmux_path="tmux")


async def _feed_lines(bridge: ControlBridge, lines: list[str]) -> list[dict]:
    """Feed raw lines into the bridge's reader and collect all queued events.

    Sets up a StreamReader, feeds data, runs the read loop, and drains the
    event queue.  Returns all events except the final ``exit`` sentinel.
    """
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data((line + "\n").encode("utf-8"))
    reader.feed_eof()

    bridge._pty_reader = reader
    await bridge._read_loop()

    events: list[dict] = []
    while not bridge._event_queue.empty():
        ev = bridge._event_queue.get_nowait()
        if ev["type"] != "exit":
            events.append(ev)
    return events


async def _feed_bytes_lines(bridge: ControlBridge, raw_lines: list[bytes]) -> list[dict]:
    """Feed raw byte sequences into the bridge's reader and collect all events.

    Identical to ``_feed_lines`` but accepts bytes directly so callers can feed
    payloads with embedded high bytes (>= 0x80) without UTF-8 encoding them.
    """
    reader = asyncio.StreamReader()
    for line in raw_lines:
        reader.feed_data(line + b"\n")
    reader.feed_eof()

    bridge._pty_reader = reader
    await bridge._read_loop()

    events: list[dict] = []
    while not bridge._event_queue.empty():
        ev = bridge._event_queue.get_nowait()
        if ev["type"] != "exit":
            events.append(ev)
    return events


class TestReadLoopResponseTracking:
    """Tests for %begin/%end handling and capture-pane synthetic output."""

    def test_non_capture_response_is_discarded(self):
        """Commands that aren't capture-pane produce no events from their response."""
        bridge = _make_bridge()
        bridge._cmd_counter = 1  # resize already sent
        lines = [
            "%begin 1000 0 0",
            "%end 1000 0 0",
        ]
        events = asyncio.run(_feed_lines(bridge, lines))
        assert events == []

    def test_capture_pane_emits_output(self):
        """capture-pane response is emitted as a synthetic output event."""
        bridge = _make_bridge()
        bridge._cmd_counter = 2
        bridge._capture_targets[1] = "%0"
        lines = [
            # Response to resize (cmd 0) — empty, should be ignored.
            "%begin 1000 0 0",
            "%end 1000 0 0",
            # Response to capture-pane (cmd 1).
            "%begin 1001 1 0",
            "\x1b[1muser@host\x1b[0m:~$",
            "some output line",
            "%end 1001 1 0",
        ]
        events = asyncio.run(_feed_lines(bridge, lines))
        assert len(events) == 1
        ev = events[0]
        assert ev["type"] == "output"
        assert ev["pane_id"] == "%0"
        expected = "\x1b[1muser@host\x1b[0m:~$\r\nsome output line\r\n"
        assert ev["data"] == expected.encode("utf-8")

    def test_capture_error_produces_no_output(self):
        """If capture-pane fails (%error), no synthetic output is emitted."""
        bridge = _make_bridge()
        bridge._cmd_counter = 1
        bridge._capture_targets[0] = "%99"
        lines = [
            "%begin 1000 0 0",
            "can't find pane %99",
            "%error 1000 0 0",
        ]
        events = asyncio.run(_feed_lines(bridge, lines))
        assert events == []
        assert 0 not in bridge._capture_targets  # cleaned up

    def test_interleaved_notification_during_response(self):
        """Notifications interleaved with a command response are forwarded."""
        bridge = _make_bridge()
        bridge._cmd_counter = 1
        bridge._capture_targets[0] = "%0"
        lines = [
            "%begin 1000 0 0",
            "captured line 1",
            "%output %1 new\\040output",  # notification interleaved
            "captured line 2",
            "%end 1000 0 0",
        ]
        events = asyncio.run(_feed_lines(bridge, lines))
        # Should have: the interleaved %output AND the capture synthetic output.
        assert len(events) == 2
        # Interleaved notification comes first (it's processed inline).
        assert events[0]["type"] == "output"
        assert events[0]["pane_id"] == "%1"
        # Capture synthetic output comes after the %end.
        assert events[1]["type"] == "output"
        assert events[1]["pane_id"] == "%0"
        assert events[1]["data"] == b"captured line 1\r\ncaptured line 2\r\n"

    def test_layout_events_pass_through_normally(self):
        """Layout events are queued normally, not swallowed by response tracking."""
        bridge = _make_bridge()
        lines = [
            "%layout-change @0 d203,80x24,0,0,0",
        ]
        events = asyncio.run(_feed_lines(bridge, lines))
        assert len(events) == 1
        assert events[0]["type"] == "layout"
        assert events[0]["panes"][0]["pane_id"] == "%0"

    def test_dcs_prefix_stripped_on_first_line(self):
        """DCS envelope on the first line is stripped before processing."""
        bridge = _make_bridge()
        lines = [
            "\x1bP1000p%layout-change @0 d203,80x24,0,0,0",
        ]
        events = asyncio.run(_feed_lines(bridge, lines))
        assert len(events) == 1
        assert events[0]["type"] == "layout"

    def test_multiple_capture_panes(self):
        """Multiple capture-pane responses each produce separate output events."""
        bridge = _make_bridge()
        bridge._cmd_counter = 3  # cmd 0 = resize, 1 = capture %0, 2 = capture %1
        bridge._capture_targets[1] = "%0"
        bridge._capture_targets[2] = "%1"
        lines = [
            # cmd 0 response (resize)
            "%begin 1000 0 0",
            "%end 1000 0 0",
            # cmd 1 response (capture %0)
            "%begin 1001 1 0",
            "pane zero content",
            "%end 1001 1 0",
            # cmd 2 response (capture %1)
            "%begin 1002 2 0",
            "pane one content",
            "%end 1002 2 0",
        ]
        events = asyncio.run(_feed_lines(bridge, lines))
        assert len(events) == 2
        assert events[0]["pane_id"] == "%0"
        assert events[0]["data"] == b"pane zero content\r\n"
        assert events[1]["pane_id"] == "%1"
        assert events[1]["data"] == b"pane one content\r\n"

    def test_empty_capture_response_skipped(self):
        """A capture-pane with no output lines (empty pane) emits no event."""
        bridge = _make_bridge()
        bridge._cmd_counter = 1
        bridge._capture_targets[0] = "%0"
        lines = [
            "%begin 1000 0 0",
            "%end 1000 0 0",
        ]
        events = asyncio.run(_feed_lines(bridge, lines))
        assert events == []


    def test_output_raw_utf8_bytes_preserved(self):
        """Raw UTF-8 bytes in %output payloads are forwarded without corruption.

        tmux passes bytes >= 0x80 through %output unescaped.  The fast path
        must forward them as-is; the old decode/encode roundtrip would corrupt
        any byte sequence that tmux splits across PTY read() boundaries.
        """
        bridge = _make_bridge()
        # ─ is U+2500, UTF-8: \xe2\x94\x80
        events = asyncio.run(_feed_bytes_lines(bridge, [b"%output %0 \xe2\x94\x80"]))
        assert len(events) == 1
        assert events[0]["pane_id"] == "%0"
        assert events[0]["data"] == b"\xe2\x94\x80", "raw UTF-8 bytes must not be corrupted"

    def test_output_split_utf8_not_replaced_with_fffd(self):
        """Partial UTF-8 sequences split across %output events forward raw bytes.

        If tmux's PTY read() returns \xe2 in one call and \x94\x80 in the next,
        both %output events must carry the raw bytes — not U+FFFD (\xef\xbf\xbd)
        which the old decode/encode roundtrip would have substituted.
        """
        bridge = _make_bridge()
        events = asyncio.run(_feed_bytes_lines(bridge, [
            b"%output %0 \xe2",
            b"%output %0 \x94\x80",
        ]))
        assert len(events) == 2
        assert events[0]["data"] == b"\xe2", "first fragment must not become U+FFFD"
        assert events[1]["data"] == b"\x94\x80", "continuation bytes must not become U+FFFD"
        # Concatenated they form the correct UTF-8 encoding of ─ (U+2500).
        assert events[0]["data"] + events[1]["data"] == b"\xe2\x94\x80"

    def test_output_mixed_octal_and_raw_utf8(self):
        """Octal-escaped bytes and raw UTF-8 in the same payload are both handled.

        tmux octal-escapes bytes < 0x20 (e.g. ESC as \\033) while passing
        bytes >= 0x80 through as raw UTF-8.  Both must survive the fast path.
        """
        bridge = _make_bridge()
        # \033 is octal-escaped ESC; \xe2\x95\xad is raw UTF-8 for ╭ (U+256D)
        events = asyncio.run(_feed_bytes_lines(bridge, [
            b"%output %0 \\033[32m\xe2\x95\xad\\033[0m",
        ]))
        assert len(events) == 1
        # ESC + [32m + ╭ (raw UTF-8) + ESC + [0m
        assert events[0]["data"] == b"\x1b[32m\xe2\x95\xad\x1b[0m"

class TestTriggerInitialRedraw:
    """Tests for the resize-bounce initial-redraw trigger."""

    def test_sends_two_refresh_client_commands(self):
        """trigger_initial_redraw sends a +1-column bounce then a restore."""
        bridge = _make_bridge()  # cols=80, rows=24
        commands: list[str] = []

        async def _run() -> None:
            original = bridge._send_command

            async def _capture(cmd: str) -> int:
                commands.append(cmd)
                return await original(cmd)

            bridge._send_command = _capture  # type: ignore[method-assign]
            await bridge.trigger_initial_redraw()

        asyncio.run(_run())
        assert commands == ["refresh-client -C 81,24", "refresh-client -C 80,24"]

    def test_second_call_is_no_op(self):
        """trigger_initial_redraw is idempotent: a second call sends nothing."""
        bridge = _make_bridge()
        commands: list[str] = []

        async def _run() -> None:
            original = bridge._send_command

            async def _capture(cmd: str) -> int:
                commands.append(cmd)
                return await original(cmd)

            bridge._send_command = _capture  # type: ignore[method-assign]
            await bridge.trigger_initial_redraw()
            await bridge.trigger_initial_redraw()  # second call — must be no-op

        asyncio.run(_run())
        assert len(commands) == 2  # only the two from the first call

    def test_bridge_dimensions_unchanged_after_call(self):
        """trigger_initial_redraw does not permanently alter bridge.cols/rows."""
        bridge = _make_bridge()  # cols=80, rows=24
        asyncio.run(bridge.trigger_initial_redraw())
        assert bridge.cols == 80
        assert bridge.rows == 24

    def test_cmd_counter_advances_by_two(self):
        """Each refresh-client command occupies one sequence number."""
        bridge = _make_bridge()
        before = bridge._cmd_counter
        asyncio.run(bridge.trigger_initial_redraw())
        assert bridge._cmd_counter == before + 2