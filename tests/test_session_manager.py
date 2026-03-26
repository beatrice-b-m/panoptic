"""Tests for SessionManager: layout parsing, path boundary, pagination."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from session_manager import SessionManager
from config import RuntimeSettings
from host_config import HostConfig


# ---------------------------------------------------------------------------
# _parse_layout_spec (static method, no instance needed)
# ---------------------------------------------------------------------------

class TestParseLayoutSpec:
    parse = staticmethod(SessionManager._parse_layout_spec)

    def test_single_integer(self):
        result = self.parse("3")
        assert result == ([3], ["", "", ""])

    def test_multiple_integers(self):
        result = self.parse("2:1:3")
        assert result is not None
        counts, cmds = result
        assert counts == [2, 1, 3]
        assert len(cmds) == 6  # 2 + 1 + 3

    def test_command_segment(self):
        result = self.parse("vim,jest")
        assert result is not None
        counts, cmds = result
        assert counts == [2]
        assert cmds == ["vim", "jest"]

    def test_mixed_segment(self):
        result = self.parse("vim,ls:3")
        assert result is not None
        counts, cmds = result
        assert counts == [2, 3]
        assert cmds == ["vim", "ls", "", "", ""]

    def test_empty_string(self):
        assert self.parse("") is None

    def test_whitespace_only(self):
        assert self.parse("   ") is None

    def test_zero_integer(self):
        assert self.parse("0") is None

    def test_negative_integer(self):
        assert self.parse("-1") is None

    def test_empty_segment(self):
        assert self.parse("2::3") is None

    def test_empty_command_in_segment(self):
        # "vim,,ls" has an empty command between commas.
        assert self.parse("vim,,ls") is None

    def test_single_command(self):
        result = self.parse("vim")
        # "vim" is not a valid integer, so it's treated as a 1-command segment.
        assert result is not None
        counts, cmds = result
        assert counts == [1]
        assert cmds == ["vim"]


# ---------------------------------------------------------------------------
# get_sessions pagination guard
# ---------------------------------------------------------------------------

class TestGetSessions:
    @pytest.fixture
    def manager(self, tmp_path):
        """Create a minimal SessionManager with a temp host config."""
        hosts_file = tmp_path / "hosts.json"
        hosts_file.write_text('{"hosts": []}')
        settings = RuntimeSettings(hosts_config_path=str(hosts_file))
        hc = HostConfig(path=str(hosts_file))
        return SessionManager(hc, settings)

    def test_page_size_zero_clamped(self, manager):
        """page_size=0 should not cause ZeroDivisionError."""
        result = manager.get_sessions("localhost", page=1, page_size=0)
        assert result["page_size"] == 1
        assert "sessions" in result

    def test_negative_page_size_clamped(self, manager):
        result = manager.get_sessions("localhost", page=1, page_size=-5)
        assert result["page_size"] == 1

    def test_default_page_size(self, manager):
        result = manager.get_sessions("localhost")
        assert result["page_size"] == 8  # default from RuntimeSettings


# ---------------------------------------------------------------------------
# list_directories boundary enforcement
# ---------------------------------------------------------------------------

class TestListDirectories:
    @pytest.fixture
    def manager(self, tmp_path):
        hosts_file = tmp_path / "hosts.json"
        hosts_file.write_text('{"hosts": []}')
        settings = RuntimeSettings(hosts_config_path=str(hosts_file))
        hc = HostConfig(path=str(hosts_file))
        return SessionManager(hc, settings)

    def test_home_itself_allowed(self, manager):
        home = str(Path.home())
        # Should not return empty (home dir has subdirs on any real system).
        result = manager.list_directories(home + "/")
        # We can't assert specific dirs, but it shouldn't reject the prefix.
        assert isinstance(result, list)

    def test_outside_home_rejected(self, manager):
        result = manager.list_directories("/tmp/")
        assert result == []

    def test_traversal_rejected(self, manager):
        home = str(Path.home())
        # Try to escape via '..'
        result = manager.list_directories(home + "/../../../tmp/")
        assert result == []

    def test_sibling_prefix_rejected(self, manager):
        """The old str.startswith bug: /home/bee should not match /home/beekeeper."""
        home = Path.home()
        # Construct a hypothetical sibling path.  We can't create it on disk
        # easily, but we can test with a path that starts with the same prefix.
        sibling = str(home) + "keeper/foo/"
        result = manager.list_directories(sibling)
        assert result == []

    def test_empty_prefix_defaults_to_home(self, manager):
        result = manager.list_directories("")
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Persistence: host_config schema validation
# ---------------------------------------------------------------------------

class TestHostConfigValidation:
    def test_invalid_entries_filtered(self, tmp_path):
        """Non-dict and entries missing required fields are silently dropped."""
        hosts_file = tmp_path / "hosts.json"
        hosts_file.write_text(
            '{"hosts": ['
            '42, '
            '{"id": "noType"}, '
            '{"id": "badtype", "type": "ftp"}, '
            '{"id": "ssh-no-alias", "type": "ssh"}, '
            '{"id": "valid", "type": "local", "label": "Valid"}'
            ']}'
        )
        hc = HostConfig(path=str(hosts_file))
        hosts = hc.list_hosts()
        ids = [h["id"] for h in hosts]
        assert "valid" in ids
        assert "noType" not in ids
        assert "badtype" not in ids
        assert "ssh-no-alias" not in ids

    def test_malformed_json_starts_empty(self, tmp_path):
        hosts_file = tmp_path / "hosts.json"
        hosts_file.write_text("NOT JSON")
        hc = HostConfig(path=str(hosts_file))
        # Should have at least localhost (ensured by _ensure_localhost).
        hosts = hc.list_hosts()
        assert any(h["id"] == "localhost" for h in hosts)


# ---------------------------------------------------------------------------
# Persistence: template_store schema validation
# ---------------------------------------------------------------------------

class TestTemplateStoreValidation:
    def test_invalid_entries_filtered(self, tmp_path):
        from template_store import TemplateStore

        templates_file = tmp_path / "templates.json"
        templates_file.write_text(
            '{"templates": ['
            '99, '
            '{"template_name": "bad"}, '
            '{"template_name": "ok", "name": "test", "layout_type": "none", '
            '"directory": ".", "layout_spec": "", "pane_commands": []}'
            ']}'
        )
        store = TemplateStore(path=str(templates_file))
        templates = store.list_templates()
        names = [t["template_name"] for t in templates]
        assert "ok" in names
        assert "bad" not in names

    def test_malformed_json_starts_empty(self, tmp_path):
        from template_store import TemplateStore

        templates_file = tmp_path / "templates.json"
        templates_file.write_text("{invalid}")
        store = TemplateStore(path=str(templates_file))
        assert store.list_templates() == []
