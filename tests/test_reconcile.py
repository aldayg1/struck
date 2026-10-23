"""I02 reconciliation gate: receipts-only durability, the captured transport
failure, and no-retry-by-construction. No network: the reconcile unit's live
Koios read is replaced per test with recorded behavior and the receipts are
real files written exactly as the production pipeline writes them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from langgraph.types import Command

from struck import reconcile as mod
from struck import wallets as w
from struck.plan.models import PlanDecision, SignRequest
from struck.pipeline import Pipeline, build_graph, plan_of
from struck.plan.planner import FixedPlanner
from struck.send_cli import _Graph, _ThreadConfig
from tests.conftest import unlock_test_store
from tests.test_wallets import PHRASE

TX = "ab" * 32

REQUEST = SignRequest(
    tx_id=TX,
    cbor_hex="84a400" + "00" * 4,  # shape only: never parsed by the reconcile unit
    plan_hash="cd" * 32,
    explanation_sha256="ef" * 32,
    receipts_dir="/does-not-matter",
)

SIGN_REQUEST = "sign.request.json"
SIGNED_HEX = "tx.signed.cbor.hex"

SUBMITTED_OK: dict[str, object] = {
    "tx_id": TX,
    "expected_tx_id": TX,
    "backend": "koios",
    "network": "preprod",
    "wallet": "your-wallet",
    "plan_hash": "cd" * 32,
    "submitted_utc": "2026-10-22T21:00:00+00:00",
}


def _receipt_dir(tmp_path: Path, *, sign_request: bool, signed: bool, submitted: dict[str, object] | None) -> Path:
    out = tmp_path / "receipts" / "20261022T210000Z-koios-send-abcd"
    out.mkdir(parents=True)
    if sign_request:
        (out / "sign.request.json").write_text(REQUEST.model_dump_json(), encoding="utf-8")
    if signed:
        (out / "tx.signed.cbor.hex").write_text("84a4" + "00" * 8 + "\n", encoding="utf-8")
    if submitted is not None:
        (out / "submitted.json").write_text(json.dumps(submitted, indent=2), encoding="utf-8")
    return out


def _koios(monkeypatch: pytest.MonkeyPatch, confirmed: bool, calls: list[str]) -> None:
    def get_tx_info(self: object, tx_id: str) -> tuple[bool, object]:
        calls.append(f"/tx_info?_tx_hashes={tx_id}")
        return confirmed, object()

    monkeypatch.setattr("struck.reconcile.KoiosClient.get_tx_info", get_tx_info)


def test_a_directory_that_never_reached_the_submit_window_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "receipts" / "early"
    out.mkdir(parents=True)
    with pytest.raises(mod.ReconcileRefused, match="never reached the submit window"):
        mod.reconcile(out)


def test_backend_receipt_agrees_means_resolved_without_any_chain_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _koios(monkeypatch, True, calls)  # would LOUDLY fail if a chain read happened
    out = _receipt_dir(tmp_path, sign_request=True, signed=True, submitted=SUBMITTED_OK)
    report = mod.reconcile(out)
    assert report.verdict == "resolved_submitted"
    assert report.chain == "not_checked" and calls == []
    assert report.tx_id == REQUEST.tx_id and report.signed_recorded is True


def test_a_mismatching_submit_receipt_is_a_stop_not_a_resolution(tmp_path: Path) -> None:
    submitted = {**SUBMITTED_OK, "tx_id": "ff" * 32}
    out = _receipt_dir(tmp_path, sign_request=True, signed=True, submitted=submitted)
    report = mod.reconcile(out)
    assert report.verdict == "unknown_not_confirmed"
    assert "differ from the checked" in report.detail


def test_transport_failure_then_absent_block_is_unknown_and_names_the_only_permitted_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No submit receipt, Koios not yet listing the id: UNKNOWN — with the exact
    same-bytes-only guidance. No retry happens anywhere in production code."""
    calls: list[str] = []
    _koios(monkeypatch, False, calls)
    out = _receipt_dir(tmp_path, sign_request=True, signed=True, submitted=None)
    report = mod.reconcile(out)
    assert report.verdict == "unknown_not_confirmed"
    assert report.chain == "not_yet_in_a_block"
    assert "SAME checked bytes" in mod.report_text(report)


def test_confirmed_onchain_is_reported_from_the_live_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _koios(monkeypatch, True, calls)
    out = _receipt_dir(tmp_path, sign_request=True, signed=True, submitted=None)
    report = mod.reconcile(out)
    assert report.verdict == "confirmed_onchain"
    assert calls == [f"/tx_info?_tx_hashes={TX}"]


def test_signed_bytes_outlive_the_crash_because_they_are_written_before_submit(
    tmp_path: Path,
) -> None:
    """The durability anchor: pipeline source order proves it, and the mined
    receipts prove the CLI path works from files alone."""
    out = _receipt_dir(tmp_path, sign_request=True, signed=False, submitted=None)
    calls: list[str] = []
    _koios(pytest.MonkeyPatch(), True, calls)
    report = mod.reconcile(out)
    assert report.signed_recorded is False and report.verdict == "confirmed_onchain"
    source = Path("struck/pipeline.py").read_text(encoding="utf-8")
    write_at = source.index('"tx.signed.cbor.hex"')
    submit_at = source.index("self._backend.submit(signed_hex)")
    assert write_at < submit_at  # bytes hit disk BEFORE the submit call: crash-proof


def test_transport_failure_inside_submit_is_captured_and_points_at_reconcile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The REAL captured transport failure: the actual pipeline graph runs to
    approval; the submit node's transport raises OSError; the signed bytes and
    sign.request.json survive on disk (written BEFORE the submit); reconcile
    reports UNKNOWN from them. No retry ran."""
    from tests.test_wallets import _FakeBackend, _addr, _plan

    unlock_test_store()
    w.import_mnemonic("i02", PHRASE, "preprod")
    signer = w.load_signer("i02", "preprod")
    frm = _addr()

    class _DeadTransport(_FakeBackend):
        """The submit transport is cut: the exact captured failure window."""

        def submit(self, signed_cbor_hex: str) -> tuple[str, bytes]:
            raise OSError("connection reset by peer before the reply")

    pipe = Pipeline(FixedPlanner(_plan(frm)), _DeadTransport(), receipts_root=tmp_path, signer=signer)
    graph = cast("_Graph", build_graph(pipe))
    cfg: _ThreadConfig = {"configurable": {"thread_id": "i02"}}
    graph.invoke({"request": "test"}, cfg)
    state = graph.get_state(cfg).values
    assert "rejected" not in state
    decision = PlanDecision(
        decision="approve", plan_hash=plan_of(state).plan_hash, approver="unit-test", ts_ms=1
    )
    receipts_dir = Path(state["receipts_dir"])
    with pytest.raises(OSError, match="connection reset by peer"):
        graph.invoke(
            Command(resume=cast("dict[str, object]", json.loads(decision.model_dump_json()))), cfg
        )
    # SENT bytes + expected id are already durable (no retry, no new state):
    assert (receipts_dir / SIGN_REQUEST).exists()
    assert (receipts_dir / "tx.signed.cbor.hex").exists()
    after = graph.get_state(cfg).values
    assert "submitted_tx_id" not in after  # the submit result was NEVER fabricated
    calls: list[str] = []
    _koios(monkeypatch, False, calls)
    report = mod.reconcile(receipts_dir)
    assert report.verdict == "unknown_not_confirmed"
    assert report.signed_recorded is True and report.submit_receipt == "absent"
    assert "SAME checked bytes" in mod.report_text(report)
