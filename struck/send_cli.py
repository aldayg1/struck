"""Run the on-chain send pipeline to the signature request, then STOP.

  uv run python -m struck.send_cli <plan.json> --approver <name> [--backend node|koios]
      [--wallet <name> [--secrets-file <path>]]

The plan JSON is a ``PlanProposal`` (what an LLM planner would produce).
At the plan-approval interrupt the human types, on stdin, exactly
``approve <plan_hash>`` (or ``reject``). Nothing is built before that.
If stdin closes without an answer, the run STOPS at the approval interrupt
(exit 3): the plan, its check and the approval request are in receipts/.
Without ``--wallet`` the run ends at the signature request (an external wallet
signs). With ``--wallet <name>`` (a wallet in Struck's encrypted store, see
``struck.wallets``; master password at the terminal, before anything runs) the
approved, congruence-checked tx is signed, re-checked in Rust and submitted on
the selected backend; the tx id and explorer link are printed and saved in
receipts/. The wallet must be on the selected network.
``--backend`` overrides ``chain_backend`` in config/struck.json for this run;
``--network <name>`` (a struck.network.PROFILES row) selects the network for this run (struck.network).
Exit 0 = signature request written; 1 = rejected (reason printed);
3 = stopped at the approval interrupt, nothing built;
4 = stopped by a chain/backend/config/wallet failure (the failing call is printed).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast

from langgraph.types import Command

from .tty import read_human_line
from .callhistory import CallFailed, RecordedHttp
from .chain.backends import BackendError, BackendName, make_backend
from .chain.cli import CliError
from .config import (
    ConfigError,
    load_config,
    network_banner,
    pop_network_flag,
    network_help,
    wants_help,
)
from .pipeline import (
    Pipeline,
    StruckState,
    build_graph,
    explanation_of,
    signed_summary_of,
    signed_view_lines,
    plan_check_of,
    plan_of,
    sign_request_of,
)
from .plan.models import ApprovalRequest, PlanDecision
from .plan.planner import FixedPlanner
from .secrets import SecretError, unlock_from_terminal
from .wallets import WalletError, load_signer, pop_secrets_file


class _Snapshot(Protocol):
    """The two ``StateSnapshot`` fields the driver reads, exactly typed."""

    @property
    def values(self) -> StruckState: ...

    @property
    def next(self) -> tuple[str, ...]: ...


class _ThreadConfig(TypedDict):
    configurable: dict[str, str]


class _Graph(Protocol):
    """Exact-type contract for the compiled graph at this one boundary
    (langgraph's ``RunnableConfig`` and ``StateSnapshot`` are Any-typed)."""

    def invoke(self, input: StruckState | Command[str], config: _ThreadConfig) -> object: ...

    def get_state(self, config: _ThreadConfig) -> _Snapshot: ...


def _usage() -> int:
    print(
        "usage: python -m struck.send_cli <plan.json> --approver <name> [--backend node|koios]"
        " [--network <name>]\n" + network_help(),
        file=sys.stderr,
    )
    return 2


def parse_backend(argv: list[str], default: BackendName) -> BackendName | None:
    """``[]`` -> the config value; ``["--backend", "node"|"koios"]`` -> that."""
    if not argv:
        return default
    if len(argv) == 2 and argv[0] == "--backend":
        if argv[1] == "node":
            return "node"
        if argv[1] == "koios":
            return "koios"
    return None


def main(argv: list[str]) -> int:
    try:
        return _main(argv)
    except (CallFailed, BackendError, CliError, ConfigError, WalletError, SecretError) as exc:
        # Loud, one line, no traceback: which call failed and why. Nothing signed.
        print(f"STOPPED ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 4


def _main(argv: list[str]) -> int:
    if wants_help(argv):
        _usage()
        return 0
    argv = pop_network_flag(argv)
    if len(argv) < 3 or argv[1] != "--approver":
        return _usage()
    plan_path, approver = Path(argv[0]), argv[2]
    rest = pop_secrets_file(argv[3:])
    wallet_name: str | None = None
    if "--wallet" in rest:
        i = rest.index("--wallet")
        if i + 1 >= len(rest):
            return _usage()
        wallet_name, rest = rest[i + 1], rest[:i] + rest[i + 2 :]
    cfg = load_config()
    backend_name = parse_backend(rest, cfg.chain_backend)
    if backend_name is None:
        return _usage()
    signer = None
    if wallet_name is not None:
        unlock_from_terminal()  # the master password, at a real terminal only
        signer = load_signer(wallet_name, cfg.network)
        print(f"wallet: {signer.name} ({signer.network}) {signer.address}")
    http = RecordedHttp(cfg.timeout_s)
    try:
        pipeline = Pipeline(
            planner=FixedPlanner(plan_path.read_text(encoding="utf-8")),
            backend=make_backend(cfg, backend_name, http),
            signer=signer,
        )
        print(f"chain backend: {backend_name}")
        graph = cast("_Graph", build_graph(pipeline))
        config: _ThreadConfig = {"configurable": {"thread_id": f"send-{plan_path.stem}"}}
        initial: StruckState = {"request": f"plan file {plan_path}"}
        graph.invoke(initial, config)
        state = graph.get_state(config).values
        if "rejected" in state:
            print(f"REJECTED before approval: {state['rejected']}")
            if "plan_check_json" in state:
                for c in plan_check_of(state).checks:
                    print(
                        f"  [{'ok' if c.ok else 'FAIL'}] {c.id}: expected {c.expected} | actual {c.actual}"
                    )
            return 1
        plan, plan_check = plan_of(state), plan_check_of(state)
        request = ApprovalRequest(
            plan_hash=plan.plan_hash, plan=plan, plan_check=plan_check, summary=()
        )
        print("=== PLAN APPROVAL REQUEST (nothing is built yet) ===")
        print(network_banner(cfg))
        print(plan.model_dump_json(indent=2))
        for c in plan_check.checks:
            print(
                f"  [{'ok' if c.ok else 'FAIL'}] {c.id}: expected {c.expected} | actual {c.actual}"
            )
        print(f"plan_hash: {request.plan_hash}")
        print(f"Type exactly 'approve {request.plan_hash}' or 'reject':", flush=True)
        try:
            # Human-only: a piped/scripted stdin is refused; the answer is read from the TTY.
            answer = read_human_line()  # real terminal only (struck.tty)
        except (EOFError, OSError) as exc:
            print(f"NOT READ: {exc}")
            print("STOPPED at the approval interrupt: no human answer on stdin; nothing built.")
            print(f"pending approval receipts: {state['receipts_dir']}")
            return 3
        verdict: Literal["approve", "reject"]
        if answer == f"approve {request.plan_hash}":
            verdict = "approve"
        elif answer == "reject":
            verdict = "reject"
        else:
            print(f"REJECTED: unrecognised approval input {answer!r}")
            return 1
        decision = PlanDecision(
            decision=verdict,
            plan_hash=request.plan_hash,
            approver=approver,
            ts_ms=time.time_ns() // 1_000_000,
        )
        # Resume with plain JSON data; the interrupt's response_schema
        # (PlanDecision) re-validates it strictly inside the graph.
        resume: Command[str] = Command(
            resume=cast("dict[str, object]", json.loads(decision.model_dump_json()))
        )
        try:
            graph.invoke(resume, config)
        except OSError as exc:
            # Transport failure INSIDE the submit node (node died, network cut,
            # backend unreachable): state is UNKNOWN — the signed bytes and the
            # checked tx id are already durable in the receipts dir. NO blind
            # retry here or anywhere: reconcile first (read-only).
            state = graph.get_state(config).values
            print(
                f"TRANSPORT FAILURE during submit ({type(exc).__name__}: {exc}) — the "
                "submit result is UNKNOWN; nothing was retried. Reconcile read-only:\n"
                f"  uv run python -m struck.reconcile_cli {state['receipts_dir']}"
            )
            return 5
        state = graph.get_state(config).values
        if "rejected" in state:
            print(f"REJECTED: {state['rejected']}")
            if "signed_summary_json" in state:
                for line in signed_view_lines(signed_summary_of(state), explanation_of(state)):
                    print(line)
            if "explanation_json" in state:
                for c in explanation_of(state).congruence.checks:
                    print(
                        f"  [{'ok' if c.ok else 'FAIL'}] {c.id}: expected {c.expected} | actual {c.actual}"
                    )
            return 1
        if "submitted_tx_id" in state:
            tx_id = state["submitted_tx_id"]
            print(f"=== SUBMITTED on {backend_name} ({cfg.network}) ===")
            for line in signed_view_lines(signed_summary_of(state), explanation_of(state)):
                print(line)
            print(f"tx id: {tx_id}")
            print(f"explorer: {cfg.profile.explorer_tx_url.format(tx_id=tx_id)}")
            print(f"receipts: {state['receipts_dir']}")
            return 0
        pending = graph.get_state(config).next
        print(
            f"=== SIGNATURE REQUEST (graph paused at {pending}; no --wallet: an external wallet signs) ==="
        )
        for line in explanation_of(state).lines:
            print(line)
        sign = sign_request_of(state)
        print(f"expected tx id: {sign.tx_id}")
        print(f"receipts: {sign.receipts_dir}")
        return 0
    finally:
        http.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
