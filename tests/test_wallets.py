"""struck.wallets + the pipeline's sign-and-submit step (no network: a fake
ChainBackend). The mnemonic is the public BIP-39 all-zero-entropy test phrase."""

from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path
from typing import cast

import pytest
import struck_py
from langgraph.types import Command
from nacl.signing import VerifyKey
from pycardano import (
    Address,
    Transaction,
    TransactionBody,
    TransactionInput,
    TransactionOutput,
    TransactionWitnessSet,
)

from struck import secrets as s
from struck import receipt_view, send_cli
from struck import wallets as w
from struck.chain.backends import BackendName, WalletView
from struck.chain.models import ChainUtxo
from struck.network import PROFILES, NetworkProfile
from struck.pipeline import (
    explanation_of,
    signed_summary_of,
    signed_view_lines,
    Pipeline,
    StruckState,
    build_graph,
    plan_of,
)
from struck.plan.models import ApprovedOnchainPlan, CheckReport, PlanDecision, UnsignedTx
from struck.plan.planner import FixedPlanner
from struck.send_cli import _Graph, _ThreadConfig
from tests.conftest import unlock_test_store

PHRASE = " ".join(["abandon"] * 23 + ["art"])  # BIP-39 test vector: 32 zero bytes
PREPROD = PROFILES["preprod"]


def _addr(network: str = "preprod", wtype: w.WalletType = "standard") -> str:
    return w.derive(PHRASE, PROFILES[network], wtype)[1]


def test_derive_is_deterministic_and_network_specific() -> None:
    base, ent = _addr(), _addr(wtype="enterprise")
    assert base == _addr() and base.startswith("addr_test1q") and ent.startswith("addr_test1v")
    main = _addr("mainnet")
    assert main.startswith("addr1q")
    a, b = Address.from_primitive(base), Address.from_primitive(main)
    assert a.payment_part == b.payment_part and a.staking_part == b.staking_part
    key, _ = w.derive(PHRASE, PREPROD, "standard")
    vkey = key.to_verification_key().to_non_extended()
    assert a.payment_part == vkey.hash()
    with pytest.raises(w.WalletError, match="not a valid BIP-39"):
        w.derive(" ".join(["abandon"] * 24), PREPROD, "standard")


def _pipe(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def test_cli_import_from_stdin_pipe_list_delete_round_trip(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    unlock_test_store()
    _pipe(monkeypatch, PHRASE + "\n")
    assert w.main(["import", "alice", "--network", "preprod"]) == 0
    assert w.main(["list"]) == 0
    out = capsys.readouterr()
    assert f"alice\tpreprod\t{_addr()}" in out.out
    rec = w.load_record("alice")
    assert rec.mnemonic.get_secret_value() == PHRASE and PHRASE not in repr(rec)
    assert rec.derivation_path == "m/1852'/1815'/0'/0/0"
    raw = s.SECRETS_PATH.read_bytes()
    assert b"abandon" not in raw and _addr().encode() not in raw  # values encrypted at rest
    _pipe(monkeypatch, PHRASE + "\n")
    assert w.main(["import", "alice", "--network", "preprod"]) == 1
    assert "already exists" in capsys.readouterr().err
    w.delete_wallet("alice")
    assert w.list_wallets() == ()
    with pytest.raises(w.WalletError, match="not in the secret store"):
        w.load_record("alice")


def test_mnemonic_is_never_an_argument_and_bad_names_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unlock_test_store()
    assert w.main(["import", "bob", "--network", "preprod", *PHRASE.split()]) == 2
    assert w.main(["import", "Bad Name", "--network", "preprod"]) == 1
    _pipe(monkeypatch, "\n")
    assert w.main(["import", "bob", "--network", "preprod"]) == 1
    assert "no mnemonic on stdin" in capsys.readouterr().err
    assert w.list_wallets() == ()


def test_locked_store_refuses_and_secrets_file_selects_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _pipe(monkeypatch, PHRASE + "\n")  # not a terminal: the master prompt is refused
    assert w.main(["import", "carol", "--network", "preprod"]) == 1
    assert "real terminal" in capsys.readouterr().err
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    assert w.pop_secrets_file(["--secrets-file", str(other / "s.cfg"), "list"]) == ["list"]
    assert s.SECRETS_PATH == other / "s.cfg" and not s.is_unlocked()


def test_signer_network_mismatch_is_refused() -> None:
    unlock_test_store()
    w.import_mnemonic("pp", PHRASE, "preprod")
    w.import_mnemonic("mn", PHRASE, "mainnet")
    assert w.load_signer("pp", "preprod").address == _addr()
    with pytest.raises(w.WalletError, match="NETWORK MISMATCH"):
        w.load_signer("pp", "mainnet")
    with pytest.raises(w.WalletError, match="NETWORK MISMATCH"):
        w.load_signer("mn", "preprod")
    with pytest.raises(ValueError, match="NETWORK MISMATCH"):
        Pipeline(FixedPlanner("{}"), _FakeBackend(), signer=w.load_signer("mn", "mainnet"))


def test_signature_verifies_over_the_body_hash() -> None:
    unlock_test_store()
    w.import_mnemonic("pp", PHRASE, "preprod")
    signer = w.load_signer("pp", "preprod")
    msg = bytes(range(32))
    vkey, sig = signer.sign(msg)
    VerifyKey(vkey).verify(msg, sig)  # raises BadSignatureError if wrong
    assert "pp" in repr(signer) and PHRASE not in repr(signer)


# ── the pipeline: approve -> build -> congruence -> sign -> re-check -> submit ──

DEST = "addr_test1vq2jvx92wc7sf842u3v9ekvjqva7756w7j0vrd62szp7qscpsfznp"
FUNDING = "a" * 64


class _FakeBackend:
    """A ChainBackend with a fixed UTxO set; builds a plain body; records submits."""

    def __init__(self, tamper: bool = False) -> None:
        self.submitted: list[str] = []
        self.tamper = tamper

    @property
    def name(self) -> BackendName:
        return "koios"

    @property
    def network(self) -> NetworkProfile:
        return PREPROD

    def check_network(self) -> str:
        return "fake preprod"

    def wallet_view(self, address: str, validity_minutes: int) -> WalletView:
        utxo = ChainUtxo(tx_hash=FUNDING, index=0, address=address, lovelace=10_000_000, assets=())
        return WalletView(
            tip_slot=1000,
            utxos=(utxo,),
            valid_until_slot=1000 + validity_minutes * 60,
            ttl_wallclock="test",
        )

    def build_unsigned(
        self,
        plan: ApprovedOnchainPlan,
        refs: list[str],
        checked_utxos: tuple[ChainUtxo, ...],
        out_dir: Path,
    ) -> UnsignedTx:
        fee = 180_000
        sent = sum(o.lovelace for o in plan.outputs)
        body = TransactionBody(
            inputs=[TransactionInput.from_primitive([FUNDING, 0])],
            outputs=[
                *(
                    TransactionOutput(Address.from_primitive(o.address), o.lovelace)
                    for o in plan.outputs
                ),
                TransactionOutput(
                    Address.from_primitive(plan.from_address), 10_000_000 - sent - fee
                ),
            ],
            fee=fee,
            ttl=plan.valid_until_slot,
        )
        tx = Transaction(body, TransactionWitnessSet())
        return UnsignedTx(
            source="pycardano-koios",
            cbor_hex=tx.to_cbor_hex(),
            tx_id=tx.id.payload.hex(),
            build_stdout="fake",
        )

    def validate_unsigned(self, unsigned: UnsignedTx, out_dir: Path) -> dict[str, str]:
        return {}

    def submit(self, signed_cbor_hex: str) -> tuple[str, bytes]:
        self.submitted.append(signed_cbor_hex)
        tx_id = Transaction.from_cbor(signed_cbor_hex).id.payload.hex()
        return tx_id, json.dumps(tx_id).encode()


def _plan(from_address: str) -> str:
    output: dict[str, object] = {"address": DEST, "lovelace": 2_000_000, "assets": []}
    leg: dict[str, object] = {
        "kind": "send",
        "from_address": from_address,
        "outputs": [output],
        "max_fee_lovelace": 300_000,
        "validity_minutes": 120,
    }
    doc: dict[str, object] = {
        "lane": "onchain",
        "intent": "test send",
        "legs": [leg],
        "rationale": "test",
    }
    return json.dumps(doc)


def _run(tmp_path: Path, backend: _FakeBackend, signer: w.WalletSigner, frm: str) -> StruckState:
    pipe = Pipeline(FixedPlanner(_plan(frm)), backend, receipts_root=tmp_path, signer=signer)
    graph = cast("_Graph", build_graph(pipe))
    cfg: _ThreadConfig = {"configurable": {"thread_id": "w"}}
    graph.invoke({"request": "test"}, cfg)
    state = graph.get_state(cfg).values
    if "rejected" in state:
        return state
    d = PlanDecision(
        decision="approve", plan_hash=plan_of(state).plan_hash, approver="unit-test", ts_ms=1
    )
    graph.invoke(Command(resume=cast("dict[str, object]", json.loads(d.model_dump_json()))), cfg)
    return graph.get_state(cfg).values


def test_pipeline_signs_the_checked_tx_and_submits(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    unlock_test_store()
    w.import_mnemonic("pp", PHRASE, "preprod")
    signer = w.load_signer("pp", "preprod")
    backend = _FakeBackend()
    state = _run(tmp_path / "r", backend, signer, signer.address)
    assert "rejected" not in state, state.get("rejected")
    (signed,) = backend.submitted
    tx = Transaction.from_cbor(signed)
    assert (
        state["submitted_tx_id"]
        == tx.id.payload.hex()
        == UnsignedTx.model_validate_json(state["unsigned_json"]).tx_id
    )
    report = CheckReport.model_validate_json(
        struck_py.verify_signed_tx(signed, state["submitted_tx_id"], signer.address)
    )
    assert report.ok is True
    (wit,) = tx.transaction_witness_set.vkey_witnesses or []
    VerifyKey(wit.vkey.payload).verify(tx.transaction_body.hash(), wit.signature)
    receipts = next((tmp_path / "r").iterdir())
    sub = cast("dict[str, str]", json.loads((receipts / "submitted.json").read_text("utf-8")))
    assert sub["tx_id"] == state["submitted_tx_id"] and sub["network"] == "preprod"
    # no seed or key material anywhere: logs, stdout/stderr, receipts, call history
    key_hex = w.derive(PHRASE, PREPROD, "standard")[0].payload.hex()
    seen = caplog.text + "".join(capsys.readouterr())
    for f in tmp_path.rglob("*"):
        if f.is_file() and f.name != "secrets.cfg":
            seen += f.read_text(encoding="utf-8", errors="replace")
    assert "abandon" not in seen and key_hex not in seen


def test_wallet_that_does_not_own_the_inputs_is_refused_before_approval(tmp_path: Path) -> None:
    unlock_test_store()
    w.import_mnemonic("pp", PHRASE, "preprod")
    backend = _FakeBackend()
    other = _addr(wtype="enterprise")
    state = _run(tmp_path, backend, w.load_signer("pp", "preprod"), other)
    assert "does not own" in state["rejected"] and backend.submitted == []


class _WrongSigner(w.WalletSigner):
    def sign(self, message: bytes) -> tuple[bytes, bytes]:
        return super().sign(b"not the body hash")


def test_a_bad_signature_is_never_submitted(tmp_path: Path) -> None:
    unlock_test_store()
    w.import_mnemonic("pp", PHRASE, "preprod")
    good = w.load_signer("pp", "preprod")
    key = w.derive(PHRASE, PREPROD, "standard")[0]
    bad = _WrongSigner(good.name, good.network, good.address, key)
    backend = _FakeBackend()
    state = _run(tmp_path, backend, bad, good.address)
    assert "signed-tx check failed: ['signed.signature']" in state["rejected"]
    assert backend.submitted == []


# ── post-signature views describe the SIGNED tx (never "UNSIGNED") ──────────


def _assert_signed_view(text: str, tx_id: str) -> None:
    assert text.startswith(f"SIGNED transaction {tx_id}")
    m = re.search(r": (\d+) vkey witness\(es\)", text)
    assert m is not None and int(m.group(1)) >= 1
    assert "match yes" in text and "verifies yes" in text and "Signed-tx check: all ok" in text
    assert "UNSIGNED" not in text.upper() and "awaiting" not in text.lower()


def test_every_post_sign_view_describes_the_signed_tx(tmp_path: Path) -> None:
    unlock_test_store()
    w.import_mnemonic("pp", PHRASE, "preprod")
    signer = w.load_signer("pp", "preprod")
    state = _run(tmp_path / "r", _FakeBackend(), signer, signer.address)
    assert "rejected" not in state, state.get("rejected")
    tx_id = state["submitted_tx_id"]
    summary = signed_summary_of(state)
    assert summary.vkey_witnesses >= 1 and summary.other_witnesses == 0
    assert summary.signer_key_hashes == (summary.wallet_key_hash,)
    assert summary.key_hash_matches and summary.signature_verifies and summary.tx_id == tx_id
    view = "\n".join(signed_view_lines(summary, explanation_of(state)))
    _assert_signed_view(view, tx_id)
    receipts = next((tmp_path / "r").iterdir())
    # receipt files: the signed view, and the re-render from the signed CBOR
    _assert_signed_view((receipts / "signed.explanation.txt").read_text("utf-8"), tx_id)
    _assert_signed_view("\n".join(receipt_view.render(receipts)), tx_id)
    # the pre-signature receipt stays, but is labelled as the request awaiting signature
    request = (receipts / "explanation.txt").read_text("utf-8")
    assert request.startswith("SIGNATURE REQUEST (awaiting signature)")


def test_send_cli_submitted_output_is_the_signed_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unlock_test_store()
    w.import_mnemonic("pp", PHRASE, "preprod")
    frm = w.load_signer("pp", "preprod").address
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(_plan(frm), encoding="utf-8")

    def fake_backend(cfg: object, name: object, http: object) -> _FakeBackend:
        return _FakeBackend()

    def pipeline_in_tmp(
        planner: FixedPlanner, backend: _FakeBackend, signer: w.WalletSigner | None
    ) -> Pipeline:
        return Pipeline(planner, backend, receipts_root=tmp_path / "r", signer=signer)

    monkeypatch.setattr(send_cli, "make_backend", fake_backend)
    monkeypatch.setattr(send_cli, "Pipeline", pipeline_in_tmp)
    printed: list[str] = []

    def human() -> str:  # stands in for the person at the terminal (test only)
        printed.append(capsys.readouterr().out)
        m = re.search(r"Type exactly '(approve [0-9a-f]{64})'", printed[-1])
        assert m is not None
        return m.group(1)

    monkeypatch.setattr(send_cli, "read_human_line", human)
    rc = send_cli.main(
        [str(plan_file), "--approver", "t", "--wallet", "pp", "--network", "preprod"]
    )
    out = capsys.readouterr().out
    assert rc == 0, "\n".join([*printed, out])
    body = out.split("=== SUBMITTED on koios (preprod) ===\n", 1)[1]
    found = re.search(r"^tx id: ([0-9a-f]{64})$", out, re.M)
    assert found is not None
    tx_id = found.group(1)
    _assert_signed_view(body.split("\ntx id: ")[0], tx_id)
