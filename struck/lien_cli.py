"""The LIEN CLI (coordination I04). Money rules are Rust
(``struck_py.lien_transition``, rule ``struck-lien-1``; reputation rule
``struck-reputation-1``); this CLI only orchestrates strict Pydantic models,
the durable store, served chain reads and receipts.

``python -m struck.lien_cli --help`` prints every command. Nothing here
approves, signs or submits: a collateral lock or a repayment IS a payment —
it goes through the human approval pipeline (UI Approve + Send / the send
CLI at a real terminal), and the contract records it only from an observed
tx id that the human hands in. The credit report is a read-only live proof
of the served Strike evidence through the Rust scorer.

    python -m struck.lien_cli --network <name> create <label> <principal> <fee> \
        <collateral> <min_coverage_bps> <due_ms> <borrower> <lender>
    python -m struck.lien_cli list
    python -m struck.lien_cli observe-lock <lien_id> <tx_id> <served_lock_amount>
    python -m struck.lien_cli observe-repay <lien_id> <tx_id> <served_delivered>
    python -m struck.lien_cli audit <lien_id>
    python -m struck.lien_cli credit <kind account|vault|wallet> <identity>
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Final, TypedDict

from pydantic import BaseModel, ConfigDict, ValidationError

from .callhistory import RecordedHttp
from .config import pop_network_flag, network_help, RECEIPTS_DIR
from .lien import (
    LienError,
    LienStore,
    LienTerms,
    audit as lien_audit,
    observe_activate,
    observe_foreclose,
    observe_lock,
    observe_repay,
    summary,
)
from .strike.account import Balances
from .strike.history import PositionLife, StrikePublicReads
from .tty import require_terminal

USAGE: Final = __doc__ or "The LIEN CLI"


class _PublicIdentity(TypedDict):
    """The history module's account-selector shape (verbatim)."""

    account_id: str | None
    vault_id: str | None
    blockchain_address: str | None


def _identity(kind: str, value: str) -> _PublicIdentity:
    match kind:
        case "account":
            return {"account_id": value, "vault_id": None, "blockchain_address": None}
        case "vault":
            return {"account_id": None, "vault_id": value, "blockchain_address": None}
        case "wallet":
            return {"account_id": None, "vault_id": None, "blockchain_address": value}
        case _:
            raise LienError(f"identity kind {kind!r}: use account|vault|wallet")


class ScoreRequest(BaseModel):
    """The reputation rule input (mirrors the UI's strict request; the served
    rows pass through verbatim — Rust reads what it needs)."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    rule: str
    window_start_ms: int
    window_end_ms: int | None
    history_exhausted: bool
    balances: Balances
    positions: tuple[PositionLife, ...]


def credit_report(kind: str, identity: str, receipt_dir: Path | None = None) -> str:
    """LIVE read-only credit evidence: every page of the public served
    position history + balances, then the Rust scorer. Writes its receipt."""
    import struck_py

    from .config import load_config

    cfg = load_config()
    out_dir = receipt_dir or Path(RECEIPTS_DIR) / "credit"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    http = RecordedHttp(cfg.timeout_s)
    lives: list[PositionLife] = []
    last_page_served: bytes | None = None
    try:
        client = StrikePublicReads(http, cfg.strike_api_url)
        selection = _identity(kind, identity)
        _served_account, _account = client.account(**selection)  # identity viability
        _served_balances, balances = client.balances(**selection)
        cursor: str | None = None
        while True:
            served, page = client.position_history(
                **selection, cursor=cursor
            )
            lives.extend(page.positions)
            if not page.has_more:
                last_page_served = served.content
                break
            cursor = page.next_cursor
            if not cursor:
                raise LienError(
                    "served history says has_more but no next_cursor was served: refusing to continue"
                )
    finally:
        http.close()
    request = ScoreRequest(
        rule="struck-reputation-1",
        window_start_ms=0,
        window_end_ms=None,
        history_exhausted=True,
        balances=balances,
        positions=tuple(lives),
    )
    report_raw = struck_py.reputation_score(request.model_dump_json())
    report: dict[str, object] = json.loads(report_raw)
    if not isinstance(report, dict) or not isinstance(report.get("status"), str):
        raise LienError(f"reputation report is not the strict shape: {report_raw[:120]!r}")
    if last_page_served is not None:
        (out_dir / f"credit-{stamp}.lastpage.json").write_bytes(last_page_served)
    receipt_path = out_dir / f"credit-{stamp}.json"
    tmp = receipt_path.with_suffix(".tmp")
    tmp.write_bytes(
        (request.model_dump_json() + "\n-- report --\n" + report_raw + "\n").encode()
    )
    tmp.replace(receipt_path)
    score = report.get("score")
    return (
        f"credit report: rule {report['rule_version']} status {report['status']} score "
        f"{score if score is not None else '-'} (receipt: {receipt_path})"
    )


def main(argv: list[str]) -> int:
    if wants_help(argv):
        print(f"{USAGE}\n{network_help()}", file=sys.stderr)
        return 0
    argv = pop_network_flag(argv)
    store = LienStore()
    match argv:
        case ["create", label, principal, fee, collateral, min_bps_s, due_s, borrower, lender]:
            require_terminal()
            now_ms = int(time.time() * 1000)
            try:
                terms = LienTerms(
                    principal=principal,
                    fee=fee,
                    collateral=collateral,
                    min_coverage_bps=int(min_bps_s),
                    due_ms=int(due_s),
                    borrower=borrower,
                    lender=lender,
                )
            except ValidationError as exc:
                print(f"REFUSED: terms are not the strict shape: {exc.error_count()} error(s)")
                return 2
            record = store.create(label, terms, now_ms)
            print(
                f"created {record.id}: OPEN — principal {principal} + fee {fee}, collateral "
                f"{collateral}. The lock payment goes through the human approval pipeline; "
                f"record it ONLY after it is observed on chain: observe-lock {record.id} <tx_id> <served_amount>"
            )
            return 0
        case ["list"]:
            print(summary(store))
            return 0
        case ["observe-lock", lien_id, tx_id, amount]:
            require_terminal()
            record = observe_lock(
                store, lien_id, tx_id=tx_id, served_amount=amount, now_ms=int(time.time() * 1000)
            )
            print(
                f"{record.id} -> SECURED (lock tx {tx_id}). Audit it live any time: "
                f"python -m struck.lien_cli audit {record.id}"
            )
            return 0
        case ["observe-repay", lien_id, tx_id, delivered]:
            require_terminal()
            record = observe_repay(
                store,
                lien_id,
                served_delivered=delivered,
                tx_id=tx_id,
                now_ms=int(time.time() * 1000),
            )
            print(f"{record.id} -> REPAID (release tx {tx_id}); the lien is closed.")
            return 0
        case ["activate", lien_id]:
            require_terminal()
            record = observe_activate(store, lien_id, now_ms=int(time.time() * 1000))
            print(f"{record.id} -> ACTIVE (principal disbursed).")
            return 0
        case ["foreclose", lien_id]:
            require_terminal()
            record = observe_foreclose(store, lien_id, now_ms=int(time.time() * 1000))
            print(f"{record.id} -> FORECLOSED.")
            return 0
        case ["audit", lien_id]:
            require_terminal()
            print(lien_audit(store, lien_id))
            return 0
        case ["credit", kind, identity]:
            print(credit_report(kind, identity))
            return 0
        case _:
            print(f"{USAGE}\n{network_help()}", file=sys.stderr)
            return 2


def wants_help(argv: list[str]) -> bool:
    return not argv or argv[0] in ("-h", "--help")


if __name__ == "__main__":  # pragma: no cover - manual CLI
    raise SystemExit(main(sys.argv[1:]))
