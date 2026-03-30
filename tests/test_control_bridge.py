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