"""Read-only view of an on-chain send receipt folder.

    uv run python -m struck.receipt_view <receipts_dir>

If the folder holds ``tx.signed.cbor.hex`` the SIGNED transaction is described
from that signed CBOR (Rust ``describe_signed``: witnesses, signer key hash vs
the wallet's payment key hash, signature over the body hash, tx id) followed by
the body lines; otherwise the pre-signature request is shown, labelled as
awaiting signature. Nothing is written, signed or submitted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import struck_py

from .network import PROFILES
from .pipeline import signed_view_lines
from .plan.models import ApprovedOnchainPlan, Explanation, SignedSummary


def render(folder: Path) -> tuple[str, ...]:
    plan = ApprovedOnchainPlan.model_validate_json(
        (folder / "plan.approved.json").read_text(encoding="utf-8")
    )
    explanation = Explanation.model_validate_json(
        (folder / "explanation.json").read_text(encoding="utf-8")
    )
    signed = folder / "tx.signed.cbor.hex"
    if not signed.is_file():
        return (
            "=== SIGNATURE REQUEST (awaiting signature; no signed tx in this receipt) ===",
            *explanation.lines[1:],
        )
    profile = PROFILES[plan.network]
    summary = SignedSummary.model_validate_json(
        struck_py.describe_signed_tx(
            signed.read_text(encoding="utf-8"),
            explanation.tx_id,
            plan.from_address,
            profile.rust_json(),
        )
    )
    return signed_view_lines(summary, explanation)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m struck.receipt_view <receipts_dir>", file=sys.stderr)
        return 2
    for line in render(Path(argv[0])):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
