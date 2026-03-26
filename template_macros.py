from __future__ import annotations

import re

# Matches a valid macro variable name: letter/underscore start, alphanumeric/underscore body.
_VAR_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

# Matches any {…} group, including empty and invalid-name forms.
_PLACEHOLDER_RE = re.compile(r'\{([^}]*)\}')

# Matches a lone `{` that is never closed (no matching `}` follows on the same scan).
_UNCLOSED_RE = re.compile(r'\{(?![^}]*\})')


def validate_placeholders(text: str) -> None:
    """Validate all macro placeholders in *text*.

    Raises ValueError for:
    - unclosed `{` (no matching `}`)
    - empty placeholder `{}`
    - placeholder whose name fails _VAR_NAME_RE (e.g. `{123}`, `{foo bar}`)

    Returns None when the text is valid (including texts with no placeholders).
    """
    if not isinstance(text, str):
        raise ValueError(f"validate_placeholders() expects a string, got {type(text).__name__}")
    if _UNCLOSED_RE.search(text):
        raise ValueError(f"Unclosed '{{' in: {text!r}")

    for m in _PLACEHOLDER_RE.finditer(text):
        name = m.group(1)
        if name == "":
            raise ValueError(f"Empty placeholder '{{}}' in: {text!r}")
        if not _VAR_NAME_RE.match(name):
            raise ValueError(f"Invalid variable name {name!r} in placeholder")


def extract_variables(texts: list[str]) -> list[str]:
    """Return unique variable names found across *texts* in first-occurrence order.

    Does not validate — callers should call validate_placeholders on each text first.
    """
    seen: set[str] = set()
    order: list[str] = []
    for text in texts:
        if not isinstance(text, str):
            raise ValueError(f"extract_variables() expects strings, got {type(text).__name__}")
        for m in _PLACEHOLDER_RE.finditer(text):
            name = m.group(1)
            if name and name not in seen:
                seen.add(name)
                order.append(name)
    return order


def render(text: str, variables: dict[str, str]) -> str:
    """Substitute every `{var}` placeholder in *text* with the corresponding value.

    Raises ValueError if:
    - a placeholder's variable is absent from *variables*
    - a variable's value is an empty string (macros must expand to something meaningful)
    """
    if not isinstance(text, str):
        raise ValueError(f"render() expects a string, got {type(text).__name__}")
    for name, value in variables.items():
        if not isinstance(value, str):
            raise ValueError(
                f"Variable {name!r} must be a string, got {type(value).__name__}"
            )
    def _replace(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in variables:
            raise ValueError(f"Unresolved variable {name!r}")
        value = variables[name]
        if value == "":
            raise ValueError(f"Variable {name!r} must not be empty")
        return value

    return _PLACEHOLDER_RE.sub(_replace, text)


def contains_placeholders(text: str) -> bool:
    """Return True if *text* contains any `{` or `}` character.

    Intentionally coarse — used as a fast guard before attempting to create a
    session directly (anything brace-shaped must go through the macro path).
    """
    return "{" in text or "}" in text
