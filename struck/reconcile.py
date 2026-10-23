"""I02 — SCOPED DURABLE UNKNOWN-STATE RECONCILIATION (operator mandate).

The only window where Struck's state can go UNKNOWN is after the signed bytes
are written and the submit call is in flight (or a transport failure or
process death cuts it off): "did the chain take this tx or not?" Everything
BEFORE that point is already durable — every pipeline stage writes its
receipts before moving on, and the SIGNED bytes are written BEFORE submit
(``tx.signed.cbor.hex``), so the exact checked transaction survives any crash
on disk. Reconciling is therefore a pure READ problem, and this module never
writes transactions: it reads the receipt directory, reads Koios for the tx id
once, and returns a verdict with the EXACT next action. No blind retry is
possible through this CLI: a REBUILT tx (possibly different bytes) is refused
without the human's explicit typed confirmation; the only submission that may
ever be retried is the SAME checked bytes (same tx id — the chain dedupes by
id), which is documented in the verdict, not automated.

Verdicts:
* ``resolved_submitted``    — ``submitted.json`` is present and its id matches
  the checked one: the backend ACCEPTED the tx (block inclusion may lag).
* ``confirmed_onchain``     — Koios /tx_info lists the id: it is IN A BLOCK.
* ``unknown_not_confirmed`` — no submit receipt and Koios has no block yet:
  it may be in the mempool or dropped. NOTHING is retried automatically.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from .callhistory import RecordedHttp
from .chain.koios import KoiosClient
from .config import load_config
from .plan.models import SignRequest

SIGN_REQUEST = "sign.request.json"
SIGNED_HEX = "tx.signed.cbor.hex"
SUBMITTED = "submitted.json"


class SubmittedReceipt(BaseModel):
    """The receipt ``sign_and_submit`` wrote AFTER the backend accepted."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    tx_id: StrictStr
    expected_tx_id: StrictStr
    backend: StrictStr
    network: StrictStr
    wallet: StrictStr
    plan_hash: StrictStr
    submitted_utc: StrictStr


class ReconcileReport(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    receipts_dir: StrictStr
    tx_id: StrictStr  # the checked tx id recorded BEFORE anything was submitted
    signed_recorded: StrictBool  # the exact checked bytes survived on disk
    submit_receipt: Literal["present", "absent"]
    chain: Literal["confirmed_onchain", "not_yet_in_a_block", "not_checked"]
    verdict: Literal["resolved_submitted", "confirmed_onchain", "unknown_not_confirmed"]
    detail: StrictStr


class ReconcileRefused(RuntimeError):
    """Receipt dir never reached the submit window — nothing to reconcile."""


def report_text(report: ReconcileReport) -> str:
    lines = [
        f"receipts_dir: {report.receipts_dir}",
        f"checked tx id: {report.tx_id}",
        f"signed bytes recorded BEFORE submit: {'YES' if report.signed_recorded else 'NO'}",
        f"backend submit receipt: {report.submit_receipt}",
    ]
    if report.verdict == "resolved_submitted":
        lines.append("VERDICT: RESOLVED — the backend accepted this tx id (may still confirm).")
    elif report.verdict == "confirmed_onchain":
        lines.append("VERDICT: CONFIRMED IN A BLOCK.")
    else:
        lines.append(
            "VERDICT: UNKNOWN — not yet in a block; it may be in the mempool or dropped. "
            "NO blind retry: the only permitted resubmission is the SAME checked bytes "
            f"({SIGNED_HEX}, same tx id — the chain dedupes by id); a REBUILT tx is "
            "refused unless the human confirms it explicitly at a real terminal."
        )
    lines.append(report.detail)
    return "\n".join(lines)


def _report(
    directory: Path,
    request: SignRequest,
    signed_recorded: bool,
    submit_receipt: Literal["present", "absent"],
    chain: Literal["confirmed_onchain", "not_yet_in_a_block", "not_checked"],
    verdict: Literal["resolved_submitted", "confirmed_onchain", "unknown_not_confirmed"],
    detail: str,
) -> ReconcileReport:
    return ReconcileReport(
        receipts_dir=str(directory),
        tx_id=request.tx_id,
        signed_recorded=signed_recorded,
        submit_receipt=submit_receipt,
        chain=chain,
        verdict=verdict,
        detail=detail,
    )


def reconcile(directory: Path) -> ReconcileReport:
    """Read the receipt directory, then read Koios ONCE. No write anywhere."""
    sign_path = directory / SIGN_REQUEST
    if not sign_path.exists():
        raise ReconcileRefused(
            f"{directory}: no {SIGN_REQUEST} — this directory never reached the submit "
            "window; nothing to reconcile (the failure is earlier and its reason is in "
            "the other receipts)"
        )
    request = SignRequest.model_validate_json(sign_path.read_text(encoding="utf-8"))
    signed_recorded = (directory / SIGNED_HEX).exists()
    submit_path = directory / SUBMITTED
    if submit_path.exists():
        receipt = SubmittedReceipt.model_validate_json(submit_path.read_text(encoding="utf-8"))
        if receipt.expected_tx_id != request.tx_id or receipt.tx_id != request.tx_id:
            return _report(
                directory,
                request,
                signed_recorded,
                "present",
                "not_checked",
                "unknown_not_confirmed",
                f"submit receipt ids ({receipt.tx_id} / expected {receipt.expected_tx_id}) "
                f"differ from the checked {request.tx_id}: STOP — the backend answered a "
                "DIFFERENT tx than the checked one; escalate before any resubmission.",
            )
        return _report(
            directory,
            request,
            signed_recorded,
            "present",
            "not_checked",
            "resolved_submitted",
            f"backend receipt {SUBMITTED} agrees with the checked tx id.",
        )
    # No submit receipt: the submit call may have failed mid-transport or the
    # process died. Read the chain ONCE through the stock Koios client.
    cfg = load_config()
    http = RecordedHttp(cfg.timeout_s)
    try:
        client = KoiosClient(http, cfg.koios_url)
        confirmed, _served = client.get_tx_info(request.tx_id)
    finally:
        http.close()
    if confirmed:
        return _report(
            directory,
            request,
            signed_recorded,
            "absent",
            "confirmed_onchain",
            "confirmed_onchain",
            "Koios /tx_info lists the tx id: it is in a block (full response is in the "
            "call history with its URL and ts).",
        )
    return _report(
        directory,
        request,
        signed_recorded,
        "absent",
        "not_yet_in_a_block",
        "unknown_not_confirmed",
        f"Koios /tx_info served no row for {request.tx_id} on {cfg.network}.",
    )


def report_of(directory: Path) -> str:
    return report_text(reconcile(directory))


def cli(argv: list[str]) -> int:
    """``python -m struck.reconcile_cli <receipts_dir>``: read-only verdict."""
    from .tty import require_terminal

    if len(argv) != 1 or argv[0] in ("", "-h", "--help"):
        print("usage: python -m struck.reconcile_cli <receipts_dir>")
        return 2
    require_terminal()
    print(report_of(Path(argv[0])))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli(sys.argv[1:]))
