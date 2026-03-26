from __future__ import annotations

"""Template store: JSON-backed persistence for session templates.

Each template entry has:
    template_name  – slug-safe unique identifier ([A-Za-z0-9_-]+)
    name           – human-readable session name (macro-expanded at apply time)
    directory      – starting directory (macro-expanded at apply time)
    layout_type    – "none", "row", or "col"
    layout_spec    – layout string passed to tmux (may be empty)
    pane_commands  – list of shell commands, one per pane
"""

import json
import logging
import re
from pathlib import Path

from config import TEMPLATES_CONFIG_PATH


TEMPLATE_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')

_VALID_LAYOUT_TYPES = frozenset({"none", "row", "col"})
log = logging.getLogger(__name__)


class TemplateStore:
    """Manages the on-disk JSON template list."""

    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path or TEMPLATES_CONFIG_PATH)
        self._templates: list[dict] = []
        self._load()

    # -------------------------------------------------------------- public API

    def list_templates(self) -> list[dict]:
        """Return a shallow copy of every template entry."""
        return [dict(t) for t in self._templates]

    def get_template(self, template_name: str) -> dict | None:
        """Return a shallow copy of the named template, or None."""
        for t in self._templates:
            if t["template_name"] == template_name:
                return dict(t)
        return None

    def add_template(
        self,
        template_name: str,
        name: str,
        directory: str,
        layout_type: str,
        layout_spec: str,
        pane_commands: list[str] | None = None,
    ) -> dict:
        """Add a new template. Returns the new entry. Raises ValueError on
        invalid name format, duplicate template_name, or invalid layout_type."""
        self._validate_template_name(template_name)
        self._validate_layout_type(layout_type)
        if self.get_template(template_name) is not None:
            raise ValueError(f"Template {template_name!r} already exists")

        entry = self._build_entry(
            template_name, name, directory, layout_type, layout_spec, pane_commands
        )
        self._templates.append(entry)
        self._save()
        return dict(entry)

    def rename_template(self, old_name: str, new_name: str) -> dict:
        """Rename a template. Returns the updated entry. Raises ValueError if
        old_name is not found, new_name already exists, or new_name is invalid."""
        self._validate_template_name(new_name)
        if self.get_template(old_name) is None:
            raise ValueError(f"Template {old_name!r} not found")
        if old_name != new_name and self.get_template(new_name) is not None:
            raise ValueError(f"Template {new_name!r} already exists")

        for t in self._templates:
            if t["template_name"] == old_name:
                t["template_name"] = new_name
                self._save()
                return dict(t)

        # Should be unreachable given the get_template check above.
        raise ValueError(f"Template {old_name!r} not found")  # pragma: no cover

    def delete_template(self, template_name: str) -> bool:
        """Remove a template by name. Returns True if deleted, False if not found."""
        for i, t in enumerate(self._templates):
            if t["template_name"] == template_name:
                del self._templates[i]
                self._save()
                return True
        return False

    def update_template(
        self,
        template_name: str,
        name: str,
        directory: str,
        layout_type: str,
        layout_spec: str,
        pane_commands: list[str] | None = None,
    ) -> dict:
        """Update all fields of an existing template (keeps template_name).
        Raises ValueError if not found or layout_type is invalid. Returns
        the updated entry."""
        self._validate_layout_type(layout_type)
        for i, t in enumerate(self._templates):
            if t["template_name"] == template_name:
                self._templates[i] = self._build_entry(
                    template_name, name, directory, layout_type, layout_spec, pane_commands
                )
                self._save()
                return dict(self._templates[i])
        raise ValueError(f"Template {template_name!r} not found")

    # ----------------------------------------------------------- persistence

    def _load(self) -> None:
        if self._path.is_file():
            try:
                data = json.loads(self._path.read_text())
                templates = data.get("templates")
                if isinstance(templates, list):
                    self._templates = [t for t in templates if self._validate_template_entry(t)]
                else:
                    self._templates = []
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                log.warning("Failed to parse %s: %s; starting with empty template list", self._path, exc)
                self._templates = []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps({"templates": self._templates}, indent=2) + "\n"
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(content)
        tmp.replace(self._path)

    @staticmethod
    def _validate_template_entry(entry: object) -> bool:
        """Return True if *entry* has the minimum required shape for a template."""
        if not isinstance(entry, dict):
            log.warning("Skipping non-dict template entry: %r", entry)
            return False
        required_str = {"template_name", "name", "layout_type"}
        for field in required_str:
            if field not in entry or not isinstance(entry[field], str):
                log.warning("Skipping template entry missing or invalid '%s': %r", field, entry)
                return False
        if entry["layout_type"] not in ("none", "row", "col"):
            log.warning("Skipping template with invalid layout_type %r: %r", entry["layout_type"], entry)
            return False
        cmds = entry.get("pane_commands")
        if cmds is not None and not isinstance(cmds, list):
            log.warning("Skipping template with non-list pane_commands: %r", entry)
            return False
        return True

    # ----------------------------------------------------------- helpers

    @staticmethod
    def _build_entry(
        template_name: str,
        name: str,
        directory: str,
        layout_type: str,
        layout_spec: str,
        pane_commands: list[str] | None,
    ) -> dict:
        return {
            "template_name": template_name,
            "name": name,
            "directory": directory,
            "layout_type": layout_type,
            "layout_spec": layout_spec,
            "pane_commands": pane_commands if pane_commands is not None else [],
        }

    @staticmethod
    def _validate_template_name(template_name: str) -> None:
        if not TEMPLATE_NAME_RE.match(template_name):
            raise ValueError(
                f"Invalid template name {template_name!r}: must match "
                r"[A-Za-z0-9_-]+ (non-empty)"
            )

    @staticmethod
    def _validate_layout_type(layout_type: str) -> None:
        if layout_type not in _VALID_LAYOUT_TYPES:
            raise ValueError(
                f"Invalid layout_type {layout_type!r}: must be one of "
                f"{sorted(_VALID_LAYOUT_TYPES)}"
            )
