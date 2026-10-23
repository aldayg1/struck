"""The registration gate: ONLY the private completion marker counts as done —
a saved network alone must NOT open the main window (the 2026-10-22
half-registered-machine bug)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

from struck.private_file import write_private
from struck.ui import registration as reg


def test_missing_marker_is_not_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reg, "REGISTRATION_PATH", tmp_path / "registered.json")
    assert reg.registration_complete() is False


def test_valid_marker_is_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "registered.json"
    monkeypatch.setattr(reg, "REGISTRATION_PATH", marker)
    reg._mark_registered("preprod")
    assert marker.is_file() and marker.stat().st_mode & 0o777 == 0o600
    assert reg.registration_complete() is True


def test_corrupt_marker_is_not_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "registered.json"
    monkeypatch.setattr(reg, "REGISTRATION_PATH", marker)
    write_private(marker, b"not json at all\n")
    assert reg.registration_complete() is False
