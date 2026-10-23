"""The GUARDED account reset (struck.reset): the live audit blocks BY NAME on
any asset/state a logout could lose, and the destructive step runs only on a
PROVEN-CLEAR audit (fail-closed: reset() re-audits and re-checks). Nothing
here touches the network: the per-source readers are replaced with canned
rows; guard, aggregation, report and deletion run for real. Files live in
tmp_path; the deletion list is patched to exactly the paths the reset module
reads, so the test proves which files die and which stay."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from struck import reset as mod
from struck.callhistory import RecordedHttp
from struck import secrets as struck_secrets
from struck.config import StruckConfig, load_config
from struck.reset import AuditReport, AuditRow, ResetRefused, report_text
from struck.ui import registration as REGISTRATION_FILE
from struck.ui.registration import REGISTRATION_PATH

NET = "preprod"


def _strike_reader(rows: tuple[AuditRow, ...]) -> Callable[[RecordedHttp, StruckConfig], tuple[AuditRow, ...]]:
    def read(http: RecordedHttp, cfg: StruckConfig) -> tuple[AuditRow, ...]:
        return rows

    return read


def _chain_reader(rows: tuple[AuditRow, ...]) -> Callable[[RecordedHttp, StruckConfig], tuple[AuditRow, ...]]:
    def read(http: RecordedHttp, cfg: StruckConfig) -> tuple[AuditRow, ...]:
        return rows

    return read


def _trigger_reader() -> Callable[[], tuple[AuditRow, ...]]:
    def read() -> tuple[AuditRow, ...]:
        return ()

    return read


def _row(source: str, clear: bool, detail: str = "0") -> AuditRow:
    return AuditRow(source=source, network=NET, clear=clear, detail=detail)


def _store_paths(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "secrets": tmp_path / "struck-user-config" / "secrets.cfg",
        "llm": tmp_path / "struck-user-config" / "llm.json",
        "strike_env": tmp_path / "struck-user-config" / "strike.env",
        "registered": tmp_path / "struck-user-config" / "registered.json",
        "triggers": tmp_path / "triggers.json",
        "network": tmp_path / "struck-user-config" / "network.json",
    }
    for name, p in paths.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"[]\n" if name == "triggers" else b"here\n")  # triggers must parse as EMPTY records
    return paths


def _patch_deletion_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    """The deletion list reads: secrets.SECRETS_PATH (attr), mod.LLM_CONFIG_PATH
    (name), mod.STRIKE_ENV_PATH (name), ui.registration.REGISTRATION_PATH
    (lazily), mod.TRIGGER_PATH."""
    monkeypatch.setattr(struck_secrets, "SECRETS_PATH", paths["secrets"])
    monkeypatch.setattr(mod, "LLM_CONFIG_PATH", paths["llm"])
    monkeypatch.setattr(mod, "STRIKE_ENV_PATH", paths["strike_env"])
    monkeypatch.setattr(REGISTRATION_FILE, "REGISTRATION_PATH", paths["registered"])
    monkeypatch.setattr(mod, "TRIGGER_PATH", paths["triggers"])


def _isolate(
    monkeypatch: pytest.MonkeyPatch,
    strike: tuple[AuditRow, ...],
    chain: tuple[AuditRow, ...],
    triggers: Callable[[], tuple[AuditRow, ...]],
) -> None:
    monkeypatch.setattr(mod, "_strike_rows", _strike_reader(strike))
    monkeypatch.setattr(mod, "_chain_rows", _chain_reader(chain))
    monkeypatch.setattr(mod, "_trigger_rows", triggers)


def test_a_locked_store_blocks_every_monied_source() -> None:
    cfg = load_config()  # REAL locked secret store; real per-source readers
    report = mod.audit(cfg)
    assert report.clear is False
    sources = {r.source for r in report.rows}
    assert any("strike auth" in s for s in sources)
    assert any("cannot read the wallet index" in r.detail for r in report.rows)


def test_an_unreadable_trigger_store_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    triggers = tmp_path / "triggers.json"
    triggers.write_bytes(b"{not records]\n")
    monkeypatch.setattr(mod, "TRIGGER_PATH", triggers)
    _isolate(monkeypatch, (_row("strike", True),), (_row("wallet a", True),), mod._trigger_rows)
    report = mod.audit(load_config())
    trig = next(r for r in report.rows if r.source == "triggers")
    assert report.clear is False and "unreadable" in trig.detail


def test_assets_present_make_the_reset_refuse_and_touch_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _store_paths(tmp_path)
    _patch_deletion_paths(tmp_path, monkeypatch, paths)
    _isolate(
        monkeypatch,
        (_row("strike balance ADA", False, "walletBalance = 5000000"),),
        (_row("wallet funding", True),),
        _trigger_reader(),
    )
    with pytest.raises(ResetRefused, match="walletBalance = 5000000"):
        mod.reset(receipt_root=tmp_path / "receipts")
    for name, p in paths.items():  # NOTHING was deleted, not even the empty stores
        assert p.read_bytes() == (b"[]\n" if name == "triggers" else b"here\n"), name
    assert not (tmp_path / "receipts").exists()


def test_a_clear_audit_is_the_only_deletion_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _store_paths(tmp_path)
    _patch_deletion_paths(tmp_path, monkeypatch, paths)
    _isolate(
        monkeypatch,
        (_row("strike ADA", True),),
        (_row("wallet funding", True),),
        _trigger_reader(),
    )
    root = tmp_path / "receipts"
    receipt = mod.reset(receipt_root=root)
    for name in ("secrets", "llm", "strike_env", "registered", "triggers"):
        assert not paths[name].exists(), name
    assert paths["network"].exists()  # network.json STAYS (no data lost)
    rows = AuditReport.model_validate_json((receipt / "audit.json").read_text(encoding="utf-8"))
    assert rows.clear is True
    assert str(paths["llm"]) in (receipt / "removed.txt").read_text(encoding="utf-8")
    kept = (receipt / "kept.txt").read_text(encoding="utf-8")
    assert "network.json" in kept and "struck.json" in kept


def test_report_text_names_every_row_and_the_verdicts() -> None:
    blocked = AuditReport(
        rows=(
            _row("strike balance ADA", False, "walletBalance = 5000000"),
            _row("wallet funding", True),
        ),
        clear=False,
    )
    text = report_text(blocked)
    assert "HOLDS ASSETS" in text and "walletBalance = 5000000" in text
    assert "RESET REFUSED" in text and "Nothing was deleted" in text
    assert "SAFE TO LOG OUT" in report_text(AuditReport(rows=(_row("strike", True),), clear=True))


def test_the_registration_marker_is_the_real_registered_json() -> None:
    assert REGISTRATION_PATH.name == "registered.json"
