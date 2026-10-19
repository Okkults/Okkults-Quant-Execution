"""Test isolation: never read the developer's real .env file."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
