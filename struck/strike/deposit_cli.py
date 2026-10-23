"""The Strike deposit lane, end to end, with THREE human gates (handoff step 4).

  uv run python -m struck.strike.deposit_cli <proposal.json> --approver <name>
      --wallet <name> [--backend node|koios] [--network <name>]
      [--max-fee-lovelace <int>] [--secrets-file <path>]

``proposal.json`` is a ``DepositPlanProposal`` (``struck.strike.deposit``):
``blockchain`` (this CLI wires the ``cardano`` lane), ``asset_symbol`` (``ADA``),
``asset_amount`` (lovelace, integer string), ``user_address`` (the stored
wallet's address), ``mode``, ``rationale``.

Gates, each typed by the human at a REAL terminal (``struck.tty`` refuses pipes):

  1. ``quote <plan_hash>``  -> ``POST /v2/deposit/quote`` + ``POST /v2/deposit/build-tx``.
     A quote STARTS a deposit; nothing is asked before this word.
  2. ``sign <plan_hash>``   -> the Rust deposit review must pass first; then
     Struck signs the Strike-built CBOR with the stored wallet (never the API
     wallet), ``verify_signed_tx`` re-checks, and the tx is submitted on the
     selected backend.
  3. ``confirm <plan_hash>``-> ``POST /v2/deposit`` with the on-chain tx hash.

Exit 0 = deposit confirmed as pending; 1 = rejected (reason printed);
3 = stopped at a gate, nothing further happened; 4 = chain/backend/config/
wallet/API failure (the failing call is printed).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from pycardano import Transaction, TransactionWitnessSet, VerificationKey, VerificationKeyWitness

import struck_py
from ..callhistory import CallFailed, RecordedHttp, Served
from ..chain.backends import BackendError, make_backend
from ..chain.cli import CliError
from ..config import (
    ConfigError,
    RECEIPTS_DIR,
    load_config,
    network_banner,
    network_help,
    pop_network_flag,
    wants_help,
)
from ..plan.models import CheckReport
from ..pipeline import utxos_json
from ..secrets import SecretError, unlock_from_terminal
from ..send_cli import parse_backend
from ..tty import read_human_line
from ..wallets import WalletError, load_signer, pop_secrets_file
from .account import StrikeAuthedClient
from .auth import StrikeCredentialsError
from .deposit import (
    DepositClient,
    DepositPlanProposal,
    cip30_utxos,
    review_cardano_deposit,
)
from .venue_auth import wallet_from_secrets

# The outer TTL bound for the review: Strike's build-tx is valid ~30 min
# (strike-deposit skill); the wallet view's bound must cover it.
DEPOSIT_VALIDITY_MINUTES = 45
DEFAULT_MAX_FEE_LOVELACE = 1_000_000  # same default as the UI deposit form


def _usage() -> int:
    print(
        "usage: python -m struck.strike.deposit_cli <proposal.json> --approver <name>"
        " --wallet <name> [--backend node|koios] [--network <name>]"
        " [--max-fee-lovelace <int>]\n" + network_help(),
        file=sys.stderr,
    )
    return 2


def _write(out: Path, name: str, text: str) -> None:
    (out / name).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def _pretty(json_text: str) -> str:
    return json.dumps(cast("object", json.loads(json_text)), indent=2)


def _gate(prompt: str, expect: str, out: Path) -> str:
    """One human word at a real terminal; anything else stops the run."""
    print(f"Type exactly '{expect}' or 'abort':", flush=True)
    try:
        answer = read_human_line()  # real terminal only (struck.tty)
    except (EOFError, OSError) as exc:
        print(f"NOT READ: {exc}")
        print(f"STOPPED at the gate; receipts: {out}")
        raise SystemExit(3) from None
    if answer == expect:
        return answer
    if answer == "abort":
        print("ABORTED by the human; nothing further happened.")
        raise SystemExit(1)
    print(f"REJECTED: unrecognised input {answer!r}")
    raise SystemExit(1)


def main(argv: list[str]) -> int:
    try:
        return _main(argv)
    except SystemExit:
        raise
    except (
        CallFailed,
        BackendError,
        CliError,
        ConfigError,
        WalletError,
        SecretError,
        StrikeCredentialsError,
        ValueError,
    ) as exc:
        print(f"STOPPED ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 4


def _pop_int(argv: list[str], flag: str) -> tuple[int | None, list[str]]:
    if flag not in argv:
        return None, argv
    i = argv.index(flag)
    if i + 1 >= len(argv):
        raise ValueError(f"{flag} needs an integer")
    value = int(argv[i + 1])  # an operator-supplied bound, not money arithmetic
    return value, argv[:i] + argv[i + 2 :]


def _main(argv: list[str]) -> int:
    if wants_help(argv):
        return _usage()
    argv = pop_network_flag(pop_secrets_file(argv))
    if len(argv) < 5 or argv[1] != "--approver" or argv[3] != "--wallet":
        return _usage()
    proposal_path, approver, wallet_name = Path(argv[0]), argv[2], argv[4]
    rest = argv[5:]
    max_fee, rest = _pop_int(rest, "--max-fee-lovelace")
    max_fee_lovelace = max_fee if max_fee is not None else DEFAULT_MAX_FEE_LOVELACE
    cfg = load_config()
    parsed_backend = parse_backend(rest, cfg.chain_backend)
    if parsed_backend is None:
        return _usage()
    backend_name = parsed_backend
    proposal = DepositPlanProposal.model_validate_json(
        proposal_path.read_text(encoding="utf-8")
    )
    if proposal.blockchain != "cardano":
        print(
            f"REFUSED: this CLI wires the Cardano deposit lane; the proposal says "
            f"{proposal.blockchain}. Ethereum/Solana deposits are not wired.",
            file=sys.stderr,
        )
        return 1
    if not proposal.asset_amount.isdigit():
        print("REFUSED: asset_amount must be an integer lovelace string", file=sys.stderr)
        return 1
    deposit_lovelace = int(proposal.asset_amount)

    unlock_from_terminal()  # the master password, at a real terminal only
    signer = load_signer(wallet_name, cfg.network)
    if signer.address != proposal.user_address:
        print(
            f"REFUSED: wallet '{signer.name}' pays from {signer.address}, but the proposal "
            f"names {proposal.user_address}",
            file=sys.stderr,
        )
        return 1
    api_wallet = wallet_from_secrets(unlock=False)  # the store is unlocked above
    http = RecordedHttp(cfg.timeout_s)
    try:
        backend = make_backend(cfg, backend_name, http)
        banner = network_banner(cfg)
        print(f"chain backend: {backend_name}")
        print(banner)
        print(
            f"wallet: {signer.name} ({signer.network}) {signer.address}\n"
            f"fee bound: {struck_py.format_lovelace(max_fee_lovelace)}\n"
            f"deposit: {struck_py.format_lovelace(deposit_lovelace)} {proposal.asset_symbol}"
            f" -> Strike {cfg.profile.strike_venue}"
        )
        print("=== DEPOSIT PROPOSAL (a quote STARTS a deposit; nothing asked yet) ===")
        print(proposal.model_dump_json(indent=2))
        print(f"plan_hash: {proposal.plan_hash}")

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = RECEIPTS_DIR / f"{stamp}-deposit-{proposal.plan_hash[:12]}"
        out.mkdir(parents=True, exist_ok=False)
        _write(out, "proposal.json", _pretty(proposal.model_dump_json()))

        # Gate 1 ─ the human's word that the quote may be asked.
        _gate("quote", f"quote {proposal.plan_hash}", out)
        view = backend.wallet_view(proposal.user_address, DEPOSIT_VALIDITY_MINUTES)
        _write(out, "wallet.utxos.json", _pretty(utxos_json(view.utxos)))
        hexes = cip30_utxos(view.utxos)
        client = DepositClient(StrikeAuthedClient(http, cfg.strike_api_url, api_wallet))
        quote_served: Served
        build_served: Served
        quote_served, quote, build_served, built = client.quote_and_build(proposal, hexes)
        _write(out, "deposit.quote.json", quote_served.content.decode("utf-8", "replace"))
        _write(out, "deposit.build-tx.json", build_served.content.decode("utf-8", "replace"))
        print(
            "=== QUOTE (served) ===\n"
            f"  usd_value {quote.quote.usd_value} (rate {quote.quote.exchange_rate})\n"
            f"  payee {quote.deposit_address} | confirmations {quote.confirmations_required}\n"
            f"  expires_at {quote.quote.expiration_at} | request_id {quote.request_id}"
        )
        if built.format != "cardano_cbor" or not isinstance(built.unsigned_tx, str):
            print(
                f"REFUSED: Strike served format {built.format!r}; this lane signs "
                "cardano_cbor only. Nothing is signed.",
                file=sys.stderr,
            )
            return 1
        unsigned_cbor = built.unsigned_tx
        explanation = review_cardano_deposit(
            unsigned_cbor,
            proposal.user_address,
            deposit_lovelace,
            max_fee_lovelace,
            view.valid_until_slot,
            utxos_json(view.utxos),
            view.ttl_wallclock,
            cfg.profile.rust_json(),
        )
        _write(out, "deposit.review.txt", "\n".join(explanation.lines))
        _write(
            out, "deposit.decoded.json",
            _pretty(struck_py.decode_tx(unsigned_cbor)),
        )
        print("=== STRIKE-BUILT TX (CBOR + decoded JSON + review in receipts) ===")
        for line in explanation.lines:
            print(line)
        failed = [c for c in explanation.congruence.checks if not c.ok]
        if failed:
            print(
                f"REFUSED: deposit review failed {[c.id for c in failed]}; NEVER sign. "
                f"Receipts: {out}",
                file=sys.stderr,
            )
            return 1
        _write(out, "tx.unsigned.cbor.hex", unsigned_cbor)

        # Gate 2 ─ the human's word that the reviewed tx may be signed and sent.
        _gate("sign", f"sign {proposal.plan_hash}", out)
        tx = Transaction.from_cbor(unsigned_cbor)
        body_hash = tx.transaction_body.hash()
        tx_id = body_hash.hex()
        vkey, signature = signer.sign(body_hash)
        tx.transaction_witness_set = TransactionWitnessSet(
            vkey_witnesses=[VerificationKeyWitness(VerificationKey.from_primitive(vkey), signature)]
        )
        signed_hex = tx.to_cbor_hex()
        report = CheckReport.model_validate_json(
            struck_py.verify_signed_tx(signed_hex, tx_id, proposal.user_address)
        )
        _write(out, "signed.check.json", _pretty(report.model_dump_json()))
        _write(out, "tx.signed.cbor.hex", signed_hex)
        _write(out, "tx.id.txt", tx_id)
        if not report.ok:
            bad = [c.id for c in report.checks if not c.ok]
            print(f"REFUSED: signed-tx check failed {bad}; NOT submitted.", file=sys.stderr)
            return 1
        submitted_id, reply = backend.submit(signed_hex)
        _write(out, f"submit.{backend_name}.response.txt", reply.decode("utf-8", "replace"))
        receipt: dict[str, str] = {
            "tx_id": submitted_id,
            "expected_tx_id": tx_id,
            "backend": backend_name,
            "network": cfg.network,
            "wallet": signer.name,
            "approver": approver,
            "request_id": quote.request_id,
            "plan_hash": proposal.plan_hash,
            "submitted_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        _write(out, "submitted.json", json.dumps(receipt, indent=2))
        if submitted_id != tx_id:
            print(
                f"SUBMITTED, but {backend_name} answered {submitted_id}, not the checked "
                f"{tx_id}: see {out}/submitted.json",
                file=sys.stderr,
            )
            return 1
        print(f"=== SUBMITTED on {backend_name} ({cfg.network}) ===")
        print(f"tx id: {tx_id}")
        print(f"explorer: {cfg.profile.explorer_tx_url.format(tx_id=tx_id)}")
        print(f"receipts: {out}")

        # Gate 3 ─ the human's word that Strike may be told the tx hash.
        _gate("confirm", f"confirm {proposal.plan_hash}", out)
        confirm_served, confirmed = client.confirm(quote.request_id, tx_id)
        _write(out, "deposit.confirm.json", confirm_served.content.decode("utf-8", "replace"))
        print(
            f"=== CONFIRMED (served) ===\n  status {confirmed.status}: {confirmed.message}\n"
            f"  credited after {quote.confirmations_required} confirmations; track with "
            "GET /v2/history/transaction or the user stream"
        )
        print(f"receipts: {out}")
        return 0
    finally:
        http.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
