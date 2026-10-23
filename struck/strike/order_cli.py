"""Run the Strike lane: plan -> checks -> HUMAN approval -> exact requests +
congruence + explanation -> HUMAN send confirm -> signed send (selected network's
Strike host, signed by its API wallet only).

  uv run python -m struck.strike.venue_auth --network <name> generate|import ...
  uv run python -m struck.strike.order_cli <strike-plan.json> --approver <name>

No API wallet for the selected network in the secret store = exit 2 before anything runs. At the plan
interrupt the human types exactly ``approve <plan_hash>`` (or ``reject``);
nothing is built before that. At the send interrupt the human types exactly
``send <plan_hash>`` (or ``abort``); nothing is sent before that. Closed stdin
at either prompt = STOP (exit 3). Exit 0 = every leg sent (HTTP 2xx means
"sequenced", not filled); 1 = rejected or a leg failed.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast

from langgraph.types import Command

from ..tty import read_human_line
from ..callhistory import CallFailed, RecordedHttp
from ..config import load_config, network_banner, pop_network_flag, network_help, wants_help
from ..plan.models import PlanDecision
from .account import StrikeAuthedClient
from .auth import StrikeCredentialsError
from .venue_auth import wallet_from_secrets
from .market import StrikeMarketClient
from .models import StrikeConfirm
from .pipeline import (
    SENT,
    FixedStrikePlanner,
    StrikePipeline,
    StrikeState,
    build_strike_graph,
    confirm_request_of,
    plan_of,
    prepared_of,
    summarize,
    validations_of,
)


class _Snapshot(Protocol):
    @property
    def values(self) -> StrikeState: ...

    @property
    def next(self) -> tuple[str, ...]: ...


class _ThreadConfig(TypedDict):
    configurable: dict[str, str]


class _Graph(Protocol):
    def invoke(self, input: StrikeState | Command[str], config: _ThreadConfig) -> object: ...

    def get_state(self, config: _ThreadConfig) -> _Snapshot: ...


def print_validations(state: StrikeState) -> None:
    for i, v in enumerate(validations_of(state)):
        for c in v.report.checks:
            print(
                f"  leg {i} [{'ok' if c.ok else 'FAIL'}] {c.id}: expected {c.expected} | actual {c.actual}"
            )
        for s in v.not_evaluated:
            print(f"  leg {i} [not evaluated] {s}")


def main(argv: list[str]) -> int:
    if wants_help(argv):
        print(
            "usage: python -m struck.strike.order_cli <strike-plan.json> --approver <name> [--network <name>]\n"
            + network_help(),
            file=sys.stderr,
        )
        return 0
    argv = pop_network_flag(argv)
    if len(argv) != 3 or argv[1] != "--approver":
        print(
            "usage: python -m struck.strike.order_cli <strike-plan.json> --approver <name> [--network <name>]\n"
            + network_help(),
            file=sys.stderr,
        )
        return 2
    plan_path, approver = Path(argv[0]), argv[2]
    try:
        wallet = wallet_from_secrets(unlock=True)
    except StrikeCredentialsError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 2
    cfg = load_config()
    http = RecordedHttp(cfg.timeout_s)
    try:
        pipeline = StrikePipeline(
            FixedStrikePlanner(plan_path.read_text(encoding="utf-8")),
            StrikeMarketClient(http, cfg.strike_api_url),
            sender=StrikeAuthedClient(http, cfg.strike_api_url, wallet),
        )
        graph = cast("_Graph", build_strike_graph(pipeline))
        config: _ThreadConfig = {"configurable": {"thread_id": f"strike-{plan_path.stem}"}}
        graph.invoke({"request": f"plan file {plan_path}"}, config)
        state = graph.get_state(config).values
        if "validations_json" in state:
            print_validations(state)
        if "rejected" in state:
            print(f"REJECTED before approval: {state['rejected']}")
            return 1
        plan = plan_of(state)
        print("=== STRIKE PLAN APPROVAL REQUEST (nothing is built or sent) ===")
        print(network_banner(cfg))
        print(plan.model_dump_json(indent=2))
        for line in summarize(plan):
            print(line)
        print(f"plan_hash: {plan.plan_hash}")
        print(f"Type exactly 'approve {plan.plan_hash}' or 'reject':", flush=True)
        try:
            # Human-only: a piped/scripted stdin is refused; the answer is read from the TTY.
            answer = read_human_line()  # real terminal only (struck.tty)
        except (EOFError, OSError) as exc:
            print(f"NOT READ: {exc}")
            print("STOPPED at the approval interrupt: no human answer on stdin; nothing built.")
            print(f"pending approval receipts: {state['receipts_dir']}")
            return 3
        verdict: Literal["approve", "reject"]
        if answer == f"approve {plan.plan_hash}":
            verdict = "approve"
        elif answer == "reject":
            verdict = "reject"
        else:
            print(f"REJECTED: unrecognised approval input {answer!r}")
            return 1
        decision = PlanDecision(
            decision=verdict,
            plan_hash=plan.plan_hash,
            approver=approver,
            ts_ms=time.time_ns() // 1_000_000,
        )
        graph.invoke(
            Command(resume=cast("dict[str, object]", json.loads(decision.model_dump_json()))),
            config,
        )
        state = graph.get_state(config).values
        if "rejected" in state:
            print(f"REJECTED: {state['rejected']}")
            if "prepared_json" in state:
                for i, p in enumerate(prepared_of(state)):
                    for c in p.congruence.report.checks:
                        print(
                            f"  leg {i} [{'ok' if c.ok else 'FAIL'}] {c.id}: {c.expected} | {c.actual}"
                        )
            return 1
        print(
            f"=== SEND CONFIRM (graph paused at {graph.get_state(config).next}; nothing sent yet) ==="
        )
        print(network_banner(cfg))
        for p in prepared_of(state):
            for line in p.explanation.lines:
                print(line)
        print(f"receipts: {confirm_request_of(state).receipts_dir}")
        print(
            f"Type exactly 'send {plan.plan_hash}' to send these exact requests to {cfg.profile.strike_label}, or 'abort':",
            flush=True,
        )
        try:
            # Human-only: a piped/scripted stdin is refused; the answer is read from the TTY.
            answer = read_human_line()  # real terminal only (struck.tty)
        except (EOFError, OSError) as exc:
            print(f"NOT READ: {exc}")
            print("STOPPED at the send confirm: no human answer on stdin; nothing sent.")
            return 3
        send: Literal["send", "abort"]
        if answer == f"send {plan.plan_hash}":
            send = "send"
        elif answer == "abort":
            send = "abort"
        else:
            print(f"ABORTED: unrecognised confirm input {answer!r}; nothing sent")
            return 1
        selected = load_config().profile
        if send == "send" and plan.network != selected.strike_venue:
            print(
                f"NOT SENT: NETWORK MISMATCH: the approved plan is for Strike {plan.network}, the "
                f"selected network is {selected.label}. Nothing was sent."
            )
            return 5
        confirm = StrikeConfirm(
            decision=send,
            plan_hash=plan.plan_hash,
            approver=approver,
            ts_ms=time.time_ns() // 1_000_000,
        )
        graph.invoke(
            Command(resume=cast("dict[str, object]", json.loads(confirm.model_dump_json()))), config
        )
        state = graph.get_state(config).values
        if "executed_json" in state:
            for row in SENT.validate_json(state["executed_json"]):
                print(
                    f"SENT leg {row.leg}: {row.method} {row.path} -> HTTP {row.status} (sequenced, not filled)"
                )
        if "rejected" in state:
            print(f"NOT SENT / STOPPED: {state['rejected']}")
            return 1
        print(f"receipts: {confirm_request_of(state).receipts_dir}")
        return 0
    except CallFailed as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        http.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
