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



# ---------------------------------------------------------------------------
# Thumbnail SVG cache — 3-tuple (text, ts, svg) contract
# ---------------------------------------------------------------------------


class TestThumbnailCache:
    @pytest.fixture
    def manager(self, tmp_path):
        hosts_file = tmp_path / "hosts.json"
        hosts_file.write_text('{"hosts": []}')
        settings = RuntimeSettings(hosts_config_path=str(hosts_file))
        hc = HostConfig(path=str(hosts_file))
        return SessionManager(hc, settings)

    def _inject_session(self, manager, host_id="localhost", name="test"):
        """Inject a live session directly into the registry."""
        from session_manager import SessionInfo
        manager._host_sessions.setdefault(host_id, {})[name] = SessionInfo(
            host_id=host_id, name=name, windows=1,
            attached=False, created_epoch=0,
        )

    def test_fresh_cache_hit_returns_cached_svg_without_rerender(self, manager):
        """A fresh cache entry must return the stored svg without calling _render_svg.

        Verified by pre-loading the cache with a sentinel SVG string that _render_svg
        could never produce (it always returns a valid SVG document), then asserting
        the exact sentinel is returned unchanged.
        """
        import asyncio, time
        self._inject_session(manager)
        host_cache = manager._snapshot_cache.setdefault("localhost", {})

        sentinel_svg = "<svg>CACHED_SENTINEL</svg>"
        host_cache["test"] = ("some text", time.monotonic(), sentinel_svg)

        result = asyncio.run(manager.get_thumbnail_svg("localhost", "test"))

        assert result == sentinel_svg, "Should return cached SVG unmodified"

    def test_stale_cache_triggers_capture_and_rerender(self, manager):
        """An expired cache entry must trigger re-capture and produce a new SVG."""
        import asyncio, time, unittest.mock as mock
        self._inject_session(manager)
        host_cache = manager._snapshot_cache.setdefault("localhost", {})

        old_ts = time.monotonic() - 9999  # definitely expired
        host_cache["test"] = ("old text", old_ts, "<svg>OLD</svg>")

        new_text = "new content"
        with mock.patch.object(manager, "_capture_pane", return_value=new_text):
            result = asyncio.run(manager.get_thumbnail_svg("localhost", "test"))

        # Cache must be updated.
        cached = host_cache["test"]
        assert cached[0] == new_text
        assert cached[2] == result, "Stored svg must match returned svg"
        assert result != "<svg>OLD</svg>"

    def test_capture_failure_returns_stale_svg(self, manager):
        """When capture fails and a stale entry exists, return the stale SVG."""
        import asyncio, time, unittest.mock as mock
        self._inject_session(manager)
        host_cache = manager._snapshot_cache.setdefault("localhost", {})

        stale_svg = "<svg>STALE</svg>"
        old_ts = time.monotonic() - 9999
        host_cache["test"] = ("stale text", old_ts, stale_svg)

        with mock.patch.object(manager, "_capture_pane", return_value=None):
            result = asyncio.run(manager.get_thumbnail_svg("localhost", "test"))

        assert result == stale_svg, "Must return stale SVG when capture fails"

    def test_cache_entry_shape_is_three_tuple(self, manager):
        """After a successful render, the cache must hold (text, ts, svg)."""
        import asyncio, unittest.mock as mock
        self._inject_session(manager)
        captured_text = "hello world"
        with mock.patch.object(manager, "_capture_pane", return_value=captured_text):
            asyncio.run(manager.get_thumbnail_svg("localhost", "test"))

        entry = manager._snapshot_cache["localhost"]["test"]
        assert len(entry) == 3, "Cache entry must be a 3-tuple (text, ts, svg)"
        text, ts, svg = entry
        assert text == captured_text
        assert isinstance(ts, float)
        assert svg.startswith("<svg "), "svg field must be a rendered SVG string"


# ---------------------------------------------------------------------------
# Sorted session cache invalidation
# ---------------------------------------------------------------------------


class TestSortedSessionsCache:
    @pytest.fixture
    def manager(self, tmp_path):
        hosts_file = tmp_path / "hosts.json"
        hosts_file.write_text('{"hosts": []}')
        settings = RuntimeSettings(hosts_config_path=str(hosts_file))
        hc = HostConfig(path=str(hosts_file))
        return SessionManager(hc, settings)

    def _add_session(self, manager, host_id, name):
        from session_manager import SessionInfo
        manager._host_sessions.setdefault(host_id, {})[name] = SessionInfo(
            host_id=host_id, name=name, windows=1,
            attached=False, created_epoch=0,
        )
        manager._sorted_sessions_cache[host_id] = None

    def test_initial_sorted_cache_is_none(self, manager):
        """Sorted cache starts as None (dirty) for every host."""
        # 'localhost' was seeded by _sync_host_structures.
        assert manager._sorted_sessions_cache.get("localhost") is None

    def test_first_get_sessions_populates_cache(self, manager):
        self._add_session(manager, "localhost", "alpha")
        manager.get_sessions("localhost")
        assert manager._sorted_sessions_cache["localhost"] is not None

    def test_sorted_cache_is_reused_on_second_call(self, manager):
        """A second get_sessions call must reuse the cached list object."""
        self._add_session(manager, "localhost", "beta")
        manager.get_sessions("localhost")
        cached_list = manager._sorted_sessions_cache["localhost"]
        manager.get_sessions("localhost")
        assert manager._sorted_sessions_cache["localhost"] is cached_list, \
            "Cache list must be the same object (no re-sort)"

    def test_delete_session_invalidates_cache(self, manager):
        """delete_session must mark sorted cache dirty."""
        import asyncio, unittest.mock as mock
        self._add_session(manager, "localhost", "gamma")
        manager.get_sessions("localhost")  # populate cache
        assert manager._sorted_sessions_cache["localhost"] is not None

        with mock.patch.object(manager, "_run_tmux", return_value=(0, "", "")):
            asyncio.run(manager.delete_session("localhost", "gamma"))

        assert manager._sorted_sessions_cache.get("localhost") is None, \
            "Sorted cache must be invalidated after session deletion"

    def test_sorted_order_is_alphabetical(self, manager):
        """Sessions must be returned in alphabetical order by name."""
        for name in ["zebra", "apple", "mango"]:
            self._add_session(manager, "localhost", name)
        result = manager.get_sessions("localhost")
        names = [s["name"] for s in result["sessions"]]
        assert names == sorted(names), "Sessions must be in alphabetical order"


# ---------------------------------------------------------------------------
# list_directories — os.scandir-based optimization parity
# ---------------------------------------------------------------------------


class TestListDirectoriesOptimized:
    @pytest.fixture
    def manager(self, tmp_path):
        hosts_file = tmp_path / "hosts.json"
        hosts_file.write_text('{"hosts": []}')
        settings = RuntimeSettings(hosts_config_path=str(hosts_file))
        hc = HostConfig(path=str(hosts_file))
        return SessionManager(hc, settings)

    def test_returns_sorted_results(self, tmp_path, manager, monkeypatch):
        """Results must always be sorted alphabetically."""
        monkeypatch.setenv("HOME", str(tmp_path))
        import importlib, session_manager as sm
        monkeypatch.setattr(sm.Path, "home", staticmethod(lambda: tmp_path))

        (tmp_path / "beta").mkdir()
        (tmp_path / "alpha").mkdir()
        (tmp_path / "gamma").mkdir()

        result = manager.list_directories(str(tmp_path) + "/")
        names = [r.rstrip("/").split("/")[-1] for r in result]
        assert names == sorted(names), "Results must be sorted"

    def test_limit_is_respected(self, tmp_path, manager, monkeypatch):
        """Result count must not exceed the limit."""
        import session_manager as sm
        monkeypatch.setattr(sm.Path, "home", staticmethod(lambda: tmp_path))

        for i in range(20):
            (tmp_path / f"dir{i:02d}").mkdir()

        result = manager.list_directories(str(tmp_path) + "/", limit=5)
        assert len(result) <= 5, "Must not return more entries than limit"

    def test_symlinks_excluded(self, tmp_path, manager, monkeypatch):
        """Symlinks must not appear in results."""
        import session_manager as sm
        monkeypatch.setattr(sm.Path, "home", staticmethod(lambda: tmp_path))

        real_dir = tmp_path / "realdir"
        real_dir.mkdir()
        link = tmp_path / "symlink"
        link.symlink_to(real_dir)

        result = manager.list_directories(str(tmp_path) + "/")
        names = [r.rstrip("/").split("/")[-1] for r in result]
        assert "symlink" not in names, "Symlinks must be excluded"
        assert "realdir" in names

    def test_partial_prefix_filter(self, tmp_path, manager, monkeypatch):
        """Partial name prefix must filter results correctly."""
        import session_manager as sm
        monkeypatch.setattr(sm.Path, "home", staticmethod(lambda: tmp_path))

        (tmp_path / "foo_a").mkdir()
        (tmp_path / "foo_b").mkdir()
        (tmp_path / "bar").mkdir()

        result = manager.list_directories(str(tmp_path / "foo"))
        names = [r.rstrip("/").split("/")[-1] for r in result]
        assert all(n.startswith("foo") for n in names), "Only foo_* dirs should match"
        assert "bar" not in names