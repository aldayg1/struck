"""The LIEN runtime (I04): the Rust contract is the oracle for every value
check; the store is durable; chain evidence is fail-closed. No network: the
Koios audit read is replaced at the client boundary exactly as in
tests/test_reconcile.py, with recorded behavior; the served-amount inputs
follow the contract's terms, never the code's output."""

from __future__ import annotations


import time
from pathlib import Path

import pytest

from struck import lien as mod

NOW = int(time.time() * 1000)
DUE = NOW + 86_400_000

TERMS = mod.LienTerms(
    principal="20",
    fee="1",
    collateral="25",
    min_coverage_bps=10_000,
    due_ms=DUE,
    borrower="addr_test1borrower",
    lender="addr_test1lender",
)


def _store(tmp_path: Path) -> mod.LienStore:
    return mod.LienStore(path=tmp_path / "liens.json")


def _receipts(tmp_path: Path) -> Path:
    return tmp_path / "receipts"


def test_create_persists_and_reloads(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create("demo lien", TERMS, NOW)
    assert record.state.phase == "open"
    # a NEW store object reads the SAME bytes: the durable medium is the file
    again = _store(tmp_path).get(record.id)
    assert again.state.terms == TERMS and again.state.phase == "open"


def test_the_rust_contract_decides_the_lock(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create("demo lien", TERMS, NOW)
    locked = mod.observe_lock(
        store, record.id, tx_id="ab" * 32, served_amount="25", now_ms=NOW + 1,
        receipt_dir=_receipts(tmp_path),
    )
    assert locked.state.phase == "secured" and locked.state.lock_tx_id == "ab" * 32
    receipt_files = list(_receipts(tmp_path).glob("transition-*.json"))
    assert receipt_files and "-- verdict --" in receipt_files[0].read_text()


def test_two_verdicts_in_one_second_never_clobber_each_other(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create("demo lien", TERMS, NOW)
    mod.observe_lock(store, record.id, tx_id="ab" * 32, served_amount="25",
                     now_ms=NOW, receipt_dir=_receipts(tmp_path))
    now_ms = NOW // 1000 * 1000  # same second as the lock receipt
    with pytest.raises(mod.TransitionRefused, match="partial"):
        mod.observe_repay(store, record.id, served_delivered="10", tx_id="cd" * 32,
                          now_ms=now_ms, receipt_dir=_receipts(tmp_path))
    mod.observe_repay(store, record.id, served_delivered="21", tx_id="ef" * 32,
                      now_ms=now_ms, receipt_dir=_receipts(tmp_path))
    names = sorted(p.name for p in _receipts(tmp_path).glob("transition-*.json"))
    repaid = [n for n in names if "repaid" in n]
    bodies = [(_receipts(tmp_path) / n).read_text() for n in repaid]
    assert len(bodies) == 2, names  # the refusal and the release both survive
    assert any('"ok":false' in b for b in bodies), bodies
    assert any('"ok":true' in b for b in bodies), bodies


def test_a_lock_that_moves_the_wrong_amount_is_refused_and_stored_state_unchanged(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    record = store.create("demo lien", TERMS, NOW)
    with pytest.raises(mod.TransitionRefused, match="must move EXACTLY"):
        mod.observe_lock(
            store, record.id, tx_id="ab" * 32, served_amount="24", now_ms=NOW + 1,
            receipt_dir=_receipts(tmp_path),
        )
    assert store.get(record.id).state.phase == "open"  # NO state mutation


def test_partial_repayment_never_releases(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create("demo lien", TERMS, NOW)
    mod.observe_lock(store, record.id, tx_id="ab" * 32, served_amount="25", now_ms=NOW,
                     receipt_dir=_receipts(tmp_path))
    with pytest.raises(mod.TransitionRefused, match="partial repayment NEVER releases"):
        mod.observe_repay(store, record.id, served_delivered="10", tx_id="cd" * 32,
                          now_ms=NOW + 1, receipt_dir=_receipts(tmp_path))
    assert store.get(record.id).state.phase == "secured"


def test_full_repayment_releases(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create("demo lien", TERMS, NOW)
    mod.observe_lock(store, record.id, tx_id="ab" * 32, served_amount="25", now_ms=NOW,
                     receipt_dir=_receipts(tmp_path))
    paid = mod.observe_repay(store, record.id, served_delivered="21", tx_id="cd" * 32,
                             now_ms=NOW + 1, receipt_dir=_receipts(tmp_path))
    assert paid.state.phase == "repaid" and paid.state.release_tx_id == "cd" * 32
    assert paid.state.lock_tx_id == "ab" * 32  # the lock evidence is kept


def test_foreclose_requires_the_passed_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create("demo lien", TERMS, NOW)
    mod.observe_lock(store, record.id, tx_id="ab" * 32, served_amount="25", now_ms=NOW,
                     receipt_dir=_receipts(tmp_path))
    active = store.get(record.id)
    # secured -> active inside the window is the contract path
    moved = mod.transition_receipt(
        active.state, "active", now_ms=NOW + 1, receipt_dir=_receipts(tmp_path)
    )
    store.replace(mod.LienRecord(id=record.id, label=record.label, state=moved))
    with pytest.raises(mod.TransitionRefused, match="before due_ms"):
        mod.transition_receipt(
            store.get(record.id).state, "foreclosed", now_ms=DUE - 1,
            receipt_dir=_receipts(tmp_path),
        )
    executed = mod.transition_receipt(
        store.get(record.id).state, "foreclosed", now_ms=DUE + 1,
        receipt_dir=_receipts(tmp_path),
    )
    assert executed.phase == "foreclosed"


def test_the_live_audit_refuses_when_the_chain_does_not_show_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed chain evidence: the audit reads the selected network and
    refuses by name when the lock tx is not confirmed there."""
    def absent(self: object, tx_id: str) -> tuple[bool, object]:
        return False, object()

    monkeypatch.setattr("struck.chain.koios.KoiosClient.get_tx_info", absent)
    store = _store(tmp_path)
    record = store.create("demo lien", TERMS, NOW)
    mod.observe_lock(store, record.id, tx_id="ab" * 32, served_amount="25", now_ms=NOW,
                     receipt_dir=_receipts(tmp_path))
    with pytest.raises(mod.ChainEvidenceMissing, match="not confirmed"):
        mod.audit(store, record.id)


def test_the_live_audit_passes_from_the_live_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    def present(self: object, tx_id: str) -> tuple[bool, object]:
        calls.append(tx_id)
        return True, object()

    monkeypatch.setattr("struck.chain.koios.KoiosClient.get_tx_info", present)
    store = _store(tmp_path)
    record = store.create("demo lien", TERMS, NOW)
    mod.observe_lock(store, record.id, tx_id="ab" * 32, served_amount="25", now_ms=NOW,
                     receipt_dir=_receipts(tmp_path))
    verdict = mod.audit(store, record.id)
    assert "CONFIRMED" in verdict and calls == ["ab" * 32]


def test_summary_names_every_lien(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create("first", TERMS, NOW)
    second = mod.LienTerms(
        principal="5",
        fee=TERMS.fee,
        collateral=TERMS.collateral,
        min_coverage_bps=TERMS.min_coverage_bps,
        due_ms=TERMS.due_ms,
        borrower=TERMS.borrower,
        lender=TERMS.lender,
    )
    store.create("second", second, NOW)
    report = mod.summary(store)
    assert report.count("principal") == 2 and "first" in report and "second" in report
