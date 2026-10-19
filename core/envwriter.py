"""Atomic .env file writes.

Used by the onboarding wizard. Secrets never pass through stdout; the file
is written to a temp path, chmod 600'd, then atomically renamed so a crash
can never leave a half-configured secrets file.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

ENV_HEADER = (
    "# Okkults Quant Execution — configuration\n"
    "# Written by the terminal onboarding wizard.\n"
    "# Broker keys and license stay on this machine. Never commit this file.\n"
)


# (no other header constants)


PLACEHOLDER_VALUES = ("", "PASTE_KEY_HERE", "your-key-here", "change-me")


def read_env_file(path: str | Path = ".env") -> dict[str, str]:
    """Parse KEY=VALUE lines (comments/blank lines ignored)."""
    env: dict[str, str] = {}
    path_ = Path(path)
    if not path_.exists():
        return env
    for line in path_.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def write_env_file(
    values: dict[str, str],
    path: str | Path = ".env",
    header: str = ENV_HEADER,
) -> None:
    """Atomically persist KEY=VALUE pairs with 0600 permissions."""
    path_ = Path(path)
    lines = [header]
    for key in sorted(values):
        value = values[key]
        lines.append(f"{key}={value}")
    content = "\n".join(lines) + "\n"

    fd, temp_path = tempfile.mkstemp(
        prefix=f".{path_.name}.", dir=str(path_.parent or ".")
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        os.replace(temp_path, path_)
    finally:
        if os.path.exists(temp_path):  # pragma: no cover - replace success path
            os.unlink(temp_path)


def merge_env_updates(
    current: dict[str, str], updates: dict[str, str], keep_placeholders: bool = True
) -> dict[str, str]:
    """Overlay wizard answers onto existing values; empty answers keep old."""
    merged = dict(current)
    for key, value in updates.items():
        if value == "" and keep_placeholders and key in merged:
            continue  # operator pressed Enter to keep the existing value
        merged[key] = value
    return merged


def mask_secret(value: str) -> str:
    """Display-safe mask: last 3 chars only."""
    if not value:
        return "<not set>"
    if len(value) <= 6:
        return "***"
    return f"***{value[-3:]}"


__all__ = [
    "ENV_HEADER",
    "PLACEHOLDER_VALUES",
    "mask_secret",
    "merge_env_updates",
    "read_env_file",
    "write_env_file",
]
