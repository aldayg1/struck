"""TEST-ONLY: build and check the unsigned tx for a plan WITHOUT approval.

  uv run python -m struck.testonly.preview_cli <plan.json> [--backend node|koios]

This exists so the deterministic tx-vs-plan check can be proven without
anyone typing an approval. It runs the pipeline's own
nodes (propose_plan, check_plan, build_tx, congruence) but skips the
approval nodes. Its receipts go to ``receipts/test-only/``, contain a
TEST-ONLY.md marker, and NEVER contain plan.approved.json,
plan.decision.json or sign.request.json. A test-only tx is not a request
to sign.
Exit 0 = congruent; 1 = rejected (reason printed).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

import struck_py

from ..callhistory import RecordedHttp
from ..chain.backends import make_backend
from ..config import RECEIPTS_DIR, load_config, pop_network_flag, network_help, wants_help
from ..pipeline import (
    Pipeline,
    StruckState,
    backend_receipts_of,
    explanation_of,
    plan_check_of,
    unsigned_of,
)
from ..plan.planner import FixedPlanner
from ..send_cli import parse_backend

TEST_ONLY_ROOT = RECEIPTS_DIR / "test-only"
_MARKER = (
    "# TEST-ONLY — NOT APPROVED — DO NOT SIGN\n\n"
    "Built by `struck.testonly.preview_cli` WITHOUT a human approval, to prove\n"
    "the deterministic tx-vs-plan (congruence) check. No plan.approved.json,\n"
    "plan.decision.json or sign.request.json exist here by design.\n"
)


def _write(directory: Path, name: str, text: str) -> None:
    (directory / name).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def _pretty(text: str) -> str:
    return json.dumps(cast("object", json.loads(text)), indent=2)


def main(argv: list[str]) -> int:
    if wants_help(argv):
        print(
            "usage: python -m struck.testonly.preview_cli <plan.json> [--backend node|koios] [--network <name>]\n"
            + network_help(),
            file=sys.stderr,
        )
        return 0
    argv = pop_network_flag(argv)
    if not argv:
        print(
            "usage: python -m struck.testonly.preview_cli <plan.json> [--backend node|koios] [--network <name>]\n"
            + network_help(),
            file=sys.stderr,
        )
        return 2
    plan_path = Path(argv[0])
    cfg = load_config()
    backend_name = parse_backend(argv[1:], cfg.chain_backend)
    if backend_name is None:
        return 2
    http = RecordedHttp(cfg.timeout_s)
    try:
        pipeline = Pipeline(
            planner=FixedPlanner(plan_path.read_text(encoding="utf-8")),
            backend=make_backend(cfg, backend_name, http),
            receipts_root=TEST_ONLY_ROOT,
        )
        print(f"TEST-ONLY preview (no approval), chain backend: {backend_name}")
        state: StruckState = {"request": f"plan file {plan_path}"}
        state.update(pipeline.propose_plan(state))
        state.update(pipeline.check_plan(state))
        out = Path(state["receipts_dir"])
        _write(out, "TEST-ONLY.md", _MARKER)
        if "rejected" in state:
            for c in plan_check_of(state).checks:
                print(
                    f"  [{'ok' if c.ok else 'FAIL'}] {c.id}: expected {c.expected} | actual {c.actual}"
                )
            print(f"REJECTED: {state['rejected']}")
            return 1
        state.update(pipeline.build_tx(state))
        state.update(pipeline.congruence(state))
        unsigned = unsigned_of(state)
        explanation = explanation_of(state)
        _write(out, "tx.cbor.hex", unsigned.cbor_hex)
        _write(out, "tx.decoded.pallas.json", _pretty(struck_py.decode_tx(unsigned.cbor_hex)))
        _write(out, "tx.build.stdout.txt", unsigned.build_stdout)
        for name, text in backend_receipts_of(state).items():
            _write(out, name, text)
        _write(out, "congruence.json", _pretty(explanation.congruence.model_dump_json()))
        _write(out, "explanation.json", _pretty(explanation.model_dump_json()))
        _write(out, "explanation.txt", "\n".join(explanation.lines))
        for line in explanation.lines:
            print(line)
        print(f"{unsigned.source} tx id: {unsigned.tx_id}")
        print(f"TEST-ONLY receipts: {out}")
        if "rejected" in state:
            print(f"REJECTED: {state['rejected']}")
            return 1
        return 0
    finally:
        http.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
