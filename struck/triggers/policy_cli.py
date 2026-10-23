"""Approve a D5 pre-approval policy, by a HUMAN typing its hash.

  uv run python -m struck.triggers.policy_cli approve <policy.json> --approver <name>

Prints the policy and its hash, then reads ONE line from stdin: exactly
``approve <policy_hash>`` writes ``data/policies/<id>.json`` and
``<id>.approval.json``; anything else (or EOF) approves nothing.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from ..tty import read_human_line
from ..config import load_config, network_banner, pop_network_flag, network_help, wants_help
from ..plan.models import PlanDecision
from .policy import POLICY_DIR, Policy


def main(argv: list[str]) -> int:
    if wants_help(argv):
        print(
            "usage: python -m struck.triggers.policy_cli approve <policy.json> --approver <name> [--network <name>]\n"
            + network_help(),
            file=sys.stderr,
        )
        return 0
    argv = pop_network_flag(argv)
    if len(argv) != 4 or argv[0] != "approve" or argv[2] != "--approver":
        print(
            "usage: python -m struck.triggers.policy_cli approve <policy.json> --approver <name> [--network <name>]\n"
            + network_help(),
            file=sys.stderr,
        )
        return 2
    policy = Policy.model_validate_json(Path(argv[1]).read_bytes())
    print(policy.model_dump_json(indent=2))
    print(network_banner(load_config()))
    print(f"policy_hash: {policy.policy_hash}")
    print(
        f"Type exactly 'approve {policy.policy_hash}' to approve, anything else aborts:", flush=True
    )
    try:
        # Human-only: a piped/scripted stdin is refused; the answer is read from the TTY.
        answer = read_human_line()  # real terminal only (struck.tty)
    except (EOFError, OSError) as exc:
        print(f"NOT READ: {exc}")
        print("STOPPED: no human answer; nothing approved.")
        return 3
    if answer != f"approve {policy.policy_hash}":
        print("not approved")
        return 1
    POLICY_DIR.mkdir(parents=True, exist_ok=True)
    (POLICY_DIR / f"{policy.policy_id}.json").write_text(
        policy.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    decision = PlanDecision(
        decision="approve",
        plan_hash=policy.policy_hash,
        approver=argv[3],
        ts_ms=time.time_ns() // 1_000_000,
    )
    (POLICY_DIR / f"{policy.policy_id}.approval.json").write_text(
        decision.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    print(f"policy {policy.policy_id} approved by {argv[3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
