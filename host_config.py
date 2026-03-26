from __future__ import annotations

"""Host configuration: JSON-backed persistence for the multi-host model.

Every host entry has:
    id         – slug-safe unique identifier ([a-z0-9_-]+)
    label      – human-readable display name
    type       – "local" or "ssh"
    ssh_alias  – (ssh only) the alias from ~/.ssh/config
    enabled    – whether the host participates in polling

localhost always exists and cannot be removed.
"""

import json
import logging
import re
from pathlib import Path

from config import HOSTS_CONFIG_PATH


HOST_ID_RE = re.compile(r"^[a-z0-9_-]+$")
log = logging.getLogger(__name__)
_SAFE_SSH_ALIAS_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._@:/-]*$')

_LOCALHOST_ENTRY: dict = {
    "id": "localhost",
    "label": "localhost",
    "type": "local",
    "enabled": True,
}


class HostConfig:
    """Manages the on-disk JSON host list."""

    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path or HOSTS_CONFIG_PATH)
        self._hosts: list[dict] = []
        self._load()

    # -------------------------------------------------------------- public API

    def list_hosts(self) -> list[dict]:
        """Return a shallow copy of every host entry."""
        return [dict(h) for h in self._hosts]

    def get_host(self, host_id: str) -> dict | None:
        for h in self._hosts:
            if h["id"] == host_id:
                return dict(h)
        return None

    def add_host(self, label: str, ssh_alias: str) -> dict:
        """Add an SSH host. Returns the new entry. Raises ValueError on conflict."""
        label = label.strip()
        ssh_alias = ssh_alias.strip()
        if not label:
            raise ValueError("Label must not be empty")
        if not ssh_alias:
            raise ValueError("SSH alias must not be empty")
        if not _SAFE_SSH_ALIAS_RE.match(ssh_alias):
            raise ValueError(
                f"Invalid SSH alias {ssh_alias!r}: must start with an alphanumeric "
                "character and contain only [A-Za-z0-9._@:/-]"
            )

        host_id = self._derive_id(label)

        entry = {
            "id": host_id,
            "label": label,
            "type": "ssh",
            "ssh_alias": ssh_alias,
            "enabled": True,
        }
        self._hosts.append(entry)
        self._save()
        return dict(entry)

    def remove_host(self, host_id: str) -> bool:
        """Remove a host by id. Returns True if removed. Raises on localhost."""
        if host_id == "localhost":
            raise ValueError("Cannot remove localhost")
        for i, h in enumerate(self._hosts):
            if h["id"] == host_id:
                del self._hosts[i]
                self._save()
                return True
        return False

    # ----------------------------------------------------------- persistence

    def _load(self) -> None:
        if self._path.is_file():
            try:
                data = json.loads(self._path.read_text())
                hosts = data.get("hosts")
                if isinstance(hosts, list):
                    self._hosts = [h for h in hosts if self._validate_host_entry(h)]
                else:
                    self._hosts = []
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                log.warning("Failed to parse %s: %s; starting with empty host list", self._path, exc)
                self._hosts = []
        self._ensure_localhost()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps({"hosts": self._hosts}, indent=2) + "\n"
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(content)
        tmp.replace(self._path)

    @staticmethod
    def _validate_host_entry(entry: object) -> bool:
        """Return True if *entry* has the minimum required shape for a host."""
        if not isinstance(entry, dict):
            log.warning("Skipping non-dict host entry: %r", entry)
            return False
        required = {"id", "type"}
        for field in required:
            if field not in entry or not isinstance(entry[field], str):
                log.warning("Skipping host entry missing or invalid '%s': %r", field, entry)
                return False
        if entry["type"] not in ("local", "ssh"):
            log.warning("Skipping host entry with invalid type %r: %r", entry["type"], entry)
            return False
        if entry["type"] == "ssh" and not entry.get("ssh_alias"):
            log.warning("Skipping SSH host entry without ssh_alias: %r", entry)
            return False
        return True

    def _ensure_localhost(self) -> None:
        for h in self._hosts:
            if h.get("id") == "localhost":
                return
        self._hosts.insert(0, dict(_LOCALHOST_ENTRY))
        self._save()

    # ----------------------------------------------------------- id derivation

    def _derive_id(self, label: str) -> str:
        """Turn a human label into a unique slug-safe id."""
        slug = re.sub(r"[^a-z0-9_-]", "-", label.lower().strip())
        slug = re.sub(r"-+", "-", slug).strip("-")
        if not slug:
            slug = "host"

        base = slug
        counter = 2
        while self.get_host(slug):
            slug = f"{base}-{counter}"
            counter += 1
        return slug
