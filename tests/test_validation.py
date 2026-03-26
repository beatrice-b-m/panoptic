"""Tests for request-body validation helpers in server.py."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from server import (
    _ValidationError,
    _require_str,
    _optional_str,
    _optional_str_or_none,
    _require_str_list,
    _require_str_dict,
)


# ---------------------------------------------------------------------------
# _require_str
# ---------------------------------------------------------------------------

class TestRequireStr:
    def test_valid(self):
        assert _require_str({"name": "hello"}, "name") == "hello"

    def test_strips_whitespace(self):
        assert _require_str({"name": "  hello  "}, "name") == "hello"

    def test_missing_field_raises(self):
        with pytest.raises(_ValidationError, match="must be a non-empty string"):
            _require_str({}, "name")

    def test_none_value_raises(self):
        with pytest.raises(_ValidationError, match="must be a non-empty string"):
            _require_str({"name": None}, "name")

    def test_int_value_raises(self):
        with pytest.raises(_ValidationError, match="must be a non-empty string"):
            _require_str({"name": 42}, "name")

    def test_list_value_raises(self):
        with pytest.raises(_ValidationError, match="must be a non-empty string"):
            _require_str({"name": ["a", "b"]}, "name")

    def test_empty_string_raises(self):
        with pytest.raises(_ValidationError, match="is required"):
            _require_str({"name": ""}, "name")

    def test_whitespace_only_raises(self):
        with pytest.raises(_ValidationError, match="is required"):
            _require_str({"name": "   "}, "name")

    def test_no_strip(self):
        assert _require_str({"name": "  x  "}, "name", strip=False) == "  x  "


# ---------------------------------------------------------------------------
# _optional_str
# ---------------------------------------------------------------------------

class TestOptionalStr:
    def test_present(self):
        assert _optional_str({"x": "val"}, "x") == "val"

    def test_absent_returns_default(self):
        assert _optional_str({}, "x") == ""

    def test_custom_default(self):
        assert _optional_str({}, "x", default="none") == "none"

    def test_int_raises(self):
        with pytest.raises(_ValidationError, match="must be a string"):
            _optional_str({"x": 123}, "x")

    def test_strips(self):
        assert _optional_str({"x": "  hi  "}, "x") == "hi"


# ---------------------------------------------------------------------------
# _optional_str_or_none
# ---------------------------------------------------------------------------

class TestOptionalStrOrNone:
    def test_present(self):
        assert _optional_str_or_none({"x": "val"}, "x") == "val"

    def test_absent_returns_none(self):
        assert _optional_str_or_none({}, "x") is None

    def test_empty_returns_none(self):
        assert _optional_str_or_none({"x": ""}, "x") is None

    def test_whitespace_returns_none(self):
        assert _optional_str_or_none({"x": "   "}, "x") is None

    def test_int_raises(self):
        with pytest.raises(_ValidationError, match="must be a string"):
            _optional_str_or_none({"x": 5}, "x")


# ---------------------------------------------------------------------------
# _require_str_list
# ---------------------------------------------------------------------------

class TestRequireStrList:
    def test_valid(self):
        assert _require_str_list({"cmds": ["a", "b"]}, "cmds") == ["a", "b"]

    def test_absent_returns_empty(self):
        assert _require_str_list({}, "cmds") == []

    def test_not_a_list_raises(self):
        with pytest.raises(_ValidationError, match="must be an array"):
            _require_str_list({"cmds": "oops"}, "cmds")

    def test_non_string_element_raises(self):
        with pytest.raises(_ValidationError, match=r"cmds\[1\].*must be a string"):
            _require_str_list({"cmds": ["ok", 42]}, "cmds")

    def test_empty_list_is_valid(self):
        assert _require_str_list({"cmds": []}, "cmds") == []


# ---------------------------------------------------------------------------
# _require_str_dict
# ---------------------------------------------------------------------------

class TestRequireStrDict:
    def test_valid(self):
        assert _require_str_dict({"v": {"a": "1"}}, "v") == {"a": "1"}

    def test_absent_returns_empty(self):
        assert _require_str_dict({}, "v") == {}

    def test_not_a_dict_raises(self):
        with pytest.raises(_ValidationError, match="must be an object"):
            _require_str_dict({"v": [1, 2]}, "v")

    def test_non_string_value_raises(self):
        with pytest.raises(_ValidationError, match="string keys and string values"):
            _require_str_dict({"v": {"a": 42}}, "v")

    def test_empty_dict_is_valid(self):
        assert _require_str_dict({"v": {}}, "v") == {}
