"""TEST-ONLY: build and check the exact Strike request bodies for a plan
WITHOUT approval, to prove N4s/N5s/N6s with no one typing an approval.

  uv run python -m struck.testonly.strike_preview_cli <strike-plan.json>

Runs the Strike pipeline's own nodes (propose_plan, validate_plan,
build_requests) and skips approval. Receipts go to ``receipts/test-only/``
with a TEST-ONLY.md marker and NEVER contain plan.approved.json,
plan.decision.json or confirm.request.json. Nothing is sent anywhere; only
Strike PUBLIC reads (selected network) are made. Exit 0 = congruent; 1 = rejected.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..callhistory import RecordedHttp
from ..config import RECEIPTS_DIR, load_config, pop_network_flag, network_help, wants_help
from ..strike.market import StrikeMarketClient
from ..strike.order_cli import print_validations
from ..strike.pipeline import FixedStrikePlanner, StrikePipeline, StrikeState, prepared_of

TEST_ONLY_ROOT = RECEIPTS_DIR / "test-only"
_MARKER = (
    "# TEST-ONLY — NOT APPROVED — DO NOT SEND\n\n"
    "Built by `struck.testonly.strike_preview_cli` WITHOUT a human approval, to\n"
    "prove the exact Strike request bodies and the body-vs-plan congruence.\n"
    "No plan.approved.json, plan.decision.json or confirm.request.json exist here.\n"
)


def _write(directory: Path, name: str, text: str) -> None:
    (directory / name).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    if wants_help(argv):
        print(
            "usage: python -m struck.testonly.strike_preview_cli <strike-plan.json> [--network <name>]\n"
            + network_help(),
            file=sys.stderr,
        )
        return 0
    argv = pop_network_flag(argv)
    if len(argv) != 1:
        print(
            "usage: python -m struck.testonly.strike_preview_cli <strike-plan.json> [--network <name>]\n"
            + network_help(),
            file=sys.stderr,
        )
        return 2
    plan_path = Path(argv[0])
    cfg = load_config()
    http = RecordedHttp(cfg.timeout_s)
    try:
        pipeline = StrikePipeline(
            FixedStrikePlanner(plan_path.read_text(encoding="utf-8")),
            StrikeMarketClient(http, cfg.strike_api_url),
            receipts_root=TEST_ONLY_ROOT,
        )
        print("TEST-ONLY Strike preview (no approval, nothing sent)")
        state: StrikeState = {"request": f"plan file {plan_path}"}
        state.update(pipeline.propose_plan(state))
        state.update(pipeline.validate_plan(state))
        out = Path(state["receipts_dir"])
        _write(out, "TEST-ONLY.md", _MARKER)
        print_validations(state)
        if "rejected" in state:
            print(f"REJECTED: {state['rejected']}")
            return 1
        state.update(pipeline.build_requests(state))
        for i, p in enumerate(prepared_of(state)):
            _write(out, f"leg{i}.request.json", p.request.model_dump_json(indent=2))
            _write(out, f"leg{i}.request.body.json", p.request.body)
            _write(out, f"leg{i}.congruence.json", p.congruence.model_dump_json(indent=2))
            _write(out, f"leg{i}.explanation.json", p.explanation.model_dump_json(indent=2))
            ok = sum(c.ok for c in p.congruence.report.checks)
            print(f"leg {i}: {p.request.method} {p.request.path} body {p.request.body}")
            print(
                f"leg {i}: congruence {ok}/{len(p.congruence.report.checks)} ok={p.congruence.report.ok}"
            )
            for line in p.explanation.lines:
                print(f"  {line}")
        print(f"receipts: {out}")
        if "rejected" in state:
            print(f"REJECTED: {state['rejected']}")
            return 1
        return 0
    finally:
        http.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
