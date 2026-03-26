"""Tests for template_macros: validation, extraction, rendering, type safety."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from template_macros import validate_placeholders, extract_variables, render, contains_placeholders


# ---------------------------------------------------------------------------
# validate_placeholders
# ---------------------------------------------------------------------------

class TestValidatePlaceholders:
    def test_no_placeholders(self):
        validate_placeholders("plain text")  # should not raise

    def test_valid_placeholder(self):
        validate_placeholders("hello {name}")  # should not raise

    def test_multiple_valid(self):
        validate_placeholders("{a} and {b_2}")  # should not raise

    def test_unclosed_brace(self):
        with pytest.raises(ValueError, match="Unclosed"):
            validate_placeholders("hello {oops")

    def test_empty_placeholder(self):
        with pytest.raises(ValueError, match="Empty placeholder"):
            validate_placeholders("hello {}")

    def test_invalid_variable_name(self):
        with pytest.raises(ValueError, match="Invalid variable name"):
            validate_placeholders("hello {123}")

    def test_space_in_name(self):
        with pytest.raises(ValueError, match="Invalid variable name"):
            validate_placeholders("hello {foo bar}")

    def test_non_string_raises(self):
        with pytest.raises(ValueError, match="expects a string"):
            validate_placeholders(42)


# ---------------------------------------------------------------------------
# extract_variables
# ---------------------------------------------------------------------------

class TestExtractVariables:
    def test_basic(self):
        assert extract_variables(["hello {name}"]) == ["name"]

    def test_dedup_preserves_order(self):
        assert extract_variables(["{b} {a} {b}"]) == ["b", "a"]

    def test_across_texts(self):
        assert extract_variables(["{x}", "{y}", "{x}"]) == ["x", "y"]

    def test_no_placeholders(self):
        assert extract_variables(["plain"]) == []

    def test_empty_list(self):
        assert extract_variables([]) == []

    def test_non_string_in_list_raises(self):
        with pytest.raises(ValueError, match="expects strings"):
            extract_variables([42])


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

class TestRender:
    def test_basic_substitution(self):
        assert render("hello {name}", {"name": "world"}) == "hello world"

    def test_multiple_variables(self):
        assert render("{a}-{b}", {"a": "x", "b": "y"}) == "x-y"

    def test_missing_variable_raises(self):
        with pytest.raises(ValueError, match="Unresolved variable"):
            render("hello {oops}", {})

    def test_empty_value_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            render("hello {name}", {"name": ""})

    def test_no_placeholders(self):
        assert render("plain", {}) == "plain"

    def test_non_string_text_raises(self):
        with pytest.raises(ValueError, match="expects a string"):
            render(42, {})

    def test_non_string_variable_value_raises(self):
        with pytest.raises(ValueError, match="must be a string"):
            render("{x}", {"x": 42})

    def test_none_variable_value_raises(self):
        with pytest.raises(ValueError, match="must be a string"):
            render("{x}", {"x": None})


# ---------------------------------------------------------------------------
# contains_placeholders
# ---------------------------------------------------------------------------

class TestContainsPlaceholders:
    def test_has_open_brace(self):
        assert contains_placeholders("hello {") is True

    def test_has_close_brace(self):
        assert contains_placeholders("hello }") is True

    def test_has_both(self):
        assert contains_placeholders("{x}") is True

    def test_no_braces(self):
        assert contains_placeholders("plain text") is False

    def test_empty_string(self):
        assert contains_placeholders("") is False
