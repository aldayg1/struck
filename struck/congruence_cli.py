"""Re-run the Rust tx-vs-plan congruence check on a saved receipt, optionally
against a TAMPERED copy of the approved plan (proof that a mismatch blocks).

  uv run python -m struck.congruence_cli <receipts_dir> [--network <name>]
  uv run python -m struck.congruence_cli <receipts_dir> --tamper-lovelace <int> [--network <name>]
  uv run python -m struck.congruence_cli <receipts_dir> --tamper-payee <addr> [--network <name>]

Exit 0 = congruent (a signature may be requested); 1 = NOT congruent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import struck_py

from .config import load_config, pop_network_flag, network_help, wants_help
from .plan.models import ApprovedOnchainPlan, CheckReport, SendOutput


def main(argv: list[str]) -> int:
    if wants_help(argv):
        print(f"{__doc__}\n{network_help()}", file=sys.stderr)
        return 0
    argv = pop_network_flag(argv)
    if len(argv) not in (1, 3):
        print(f"{__doc__}\n{network_help()}", file=sys.stderr)
        return 2
    receipts = Path(argv[0])
    plan = ApprovedOnchainPlan.model_validate_json((receipts / "plan.approved.json").read_bytes())
    if len(argv) == 3:
        first = plan.outputs[0]
        if argv[1] == "--tamper-lovelace":
            changed = SendOutput(address=first.address, lovelace=int(argv[2]), assets=first.assets)
        elif argv[1] == "--tamper-payee":
            changed = SendOutput(address=argv[2], lovelace=first.lovelace, assets=first.assets)
        else:
            print(f"{__doc__}\n{network_help()}", file=sys.stderr)
            return 2
        plan = ApprovedOnchainPlan(
            network=plan.network,
            from_address=plan.from_address,
            outputs=(changed, *plan.outputs[1:]),
            max_fee_lovelace=plan.max_fee_lovelace,
            valid_until_slot=plan.valid_until_slot,
        )
        print(f"TAMPERED plan.outputs[0] -> {changed.model_dump_json()}")
    cbor = (receipts / "tx.cbor.hex").read_text(encoding="utf-8").strip()
    inputs = (receipts / "wallet.utxos.json").read_text(encoding="utf-8")
    report = CheckReport.model_validate_json(
        struck_py.congruence_check(
            cbor, plan.model_dump_json(), inputs, load_config().profile.rust_json()
        )
    )
    for c in report.checks:
        print(f"  [{'ok' if c.ok else 'FAIL'}] {c.id}: expected {c.expected} | actual {c.actual}")
    passed = sum(1 for c in report.checks if c.ok)
    print(f"congruent={report.ok} ({passed}/{len(report.checks)} checks passed)")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
