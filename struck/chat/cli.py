"""Run the chat harness end to end in a terminal, the SAME harness the UI
Chat panel uses: prompt -> tool calls -> structured proposal -> the existing
deterministic check -> STOP at the human approval interrupt (never approves).

  uv run python -m struck.chat.cli --provider <p> --model <m> [--base-url <url>] [--network <name>] "<request>"

The provider/model/endpoint are explicit arguments (nothing is guessed);
keys come from Struck's encrypted secret store (unlocked at a real terminal). Routing:
  strike        -> the Strike pipeline (validate -> STOP at the N3 approval interrupt)
  onchain_send  -> python -m struck.send_cli <saved plan> (its own STOP at the approval prompt)
  trigger       -> the strict TriggerSpec + `triggers.cli add` command to store it (no auto-store)
  question      -> printed; nothing runs
"""

from __future__ import annotations

import json
import sys
import time
from typing import cast

from ..callhistory import CallFailed, RecordedHttp
from ..chain.backends import BackendError
from .assistant import ChatAssistant, ChatRejected
from ..config import (
    RECEIPTS_DIR,
    LlmConfig,
    load_config,
    pop_network_flag,
    network_help,
    wants_help,
)
from ..plan.providers import PROVIDERS, LlmConfigError, chat_model
from ..secrets import SecretError, unlock_from_terminal
from ..strike.market import StrikeMarketClient
from ..strike.models import StrikePlanProposal
from ..strike.order_cli import _Graph, _ThreadConfig, print_validations
from ..strike.pipeline import FixedStrikePlanner, StrikePipeline, build_strike_graph, plan_of


def _strike_to_approval(plan: StrikePlanProposal) -> int:
    cfg = load_config()
    http = RecordedHttp(cfg.timeout_s)
    try:
        pipeline = StrikePipeline(
            FixedStrikePlanner(plan.model_dump_json()),
            StrikeMarketClient(http, cfg.strike_api_url),
            sender=None,
        )
        graph = cast("_Graph", build_strike_graph(pipeline))
        config: _ThreadConfig = {"configurable": {"thread_id": f"chat-cli-{time.time_ns()}"}}
        graph.invoke({"request": "chat proposal"}, config)
        state = graph.get_state(config).values
        if "validations_json" in state:
            print_validations(state)
        if "rejected" in state:
            print(f"REJECTED before approval: {state['rejected']}")
            return 1
        approved = plan_of(state)
        print(f"plan_hash: {approved.plan_hash}")
        print(
            f"STOPPED at the approval interrupt (next={graph.get_state(config).next}): "
            "no approval is typed or scripted; nothing is built or sent."
        )
        print(f"pending approval receipts: {state['receipts_dir']}")
        return 0
    finally:
        http.close()


def main(argv: list[str]) -> int:
    if wants_help(argv):
        print(f"{__doc__}\n{network_help()}", file=sys.stderr)
        return 0
    argv = pop_network_flag(argv)
    args = dict(zip(argv[0:-1:2], argv[1:-1:2], strict=False))
    if len(argv) < 5 or "--provider" not in args or "--model" not in args:
        print(f"{__doc__}\n{network_help()}", file=sys.stderr)
        return 2
    request = argv[-1]
    spec: dict[str, str | None] = {
        "provider": args["--provider"],
        "model": args["--model"],
        "base_url": args.get("--base-url"),
    }
    try:
        llm = LlmConfig.model_validate_json(json.dumps(spec))
        if PROVIDERS[llm.provider].keyed:
            unlock_from_terminal()  # master password at a real terminal only
        model = chat_model(llm)
    except (LlmConfigError, SecretError, ValueError) as exc:
        print(f"LLM NOT CONFIGURED: {exc}")
        return 2
    print(f"provider={llm.provider} model={llm.model} base_url={llm.base_url}")
    t0 = time.monotonic()
    try:
        turn = ChatAssistant(model).ask(request)
    except ChatRejected as exc:
        print(f"llm turn: {time.monotonic() - t0:.1f}s")
        for tool_name, tool_args in exc.tool_calls:
            print(f"tool call: {tool_name} {tool_args}")
        print(f"REJECTED: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001  provider SDKs raise their own types; reported loudly, exit 1
        print(
            f"FAILED: {llm.provider} at {llm.base_url or 'stock endpoint'}: {type(exc).__name__}: {exc}"
        )
        return 1
    print(f"llm turn: {time.monotonic() - t0:.1f}s")
    for name, call_args in turn.tool_calls:
        print(f"tool call: {name} {call_args}")
    for u in turn.usage:
        print(f"usage: {u}")
    reply = turn.reply
    print(f"reply kind: {reply.kind}")
    if reply.question is not None:
        print(f"QUESTION from the LLM: {reply.question}")
        return 0
    if reply.strike is not None:
        print(reply.strike.model_dump_json(indent=2))
        return _strike_to_approval(reply.strike)
    out = RECEIPTS_DIR / "chat" / f"{time.strftime('%Y%m%dT%H%M%S')}-{reply.kind}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    if reply.onchain is not None:
        out.write_text(reply.onchain.model_dump_json(indent=2), encoding="utf-8")
        print(out.read_text(encoding="utf-8"))
        # Deterministic check only, run to the approval interrupt in-process with
        # NO approval input of any kind; the human approves in send_cli at a TTY.
        from ..chain.backends import make_backend
        from ..pipeline import Pipeline, build_graph, plan_check_of, plan_of
        from ..plan.planner import FixedPlanner
        from ..send_cli import _Graph as _SendGraph
        from ..send_cli import _ThreadConfig as _SendConfig

        cfg = load_config()
        pipeline = Pipeline(
            planner=FixedPlanner(reply.onchain.model_dump_json()),
            backend=make_backend(cfg, cfg.chain_backend, RecordedHttp(cfg.timeout_s)),
        )
        graph = cast("_SendGraph", build_graph(pipeline))
        config: _SendConfig = {"configurable": {"thread_id": f"chat-{out.stem}"}}
        try:
            graph.invoke({"request": f"chat plan {out}"}, config)
        except (CallFailed, BackendError) as exc:
            # Loud, one line: the chain read that failed. Nothing built or sent.
            print(f"STOPPED before approval ({type(exc).__name__}): {exc}")
            return 4
        state = graph.get_state(config).values
        if "plan_check_json" in state:
            for c in plan_check_of(state).checks:
                print(
                    f"  [{'ok' if c.ok else 'FAIL'}] {c.id}: expected {c.expected} | actual {c.actual}"
                )
        if "rejected" in state:
            print(f"REJECTED before approval: {state['rejected']}")
            return 1
        print(f"plan_hash: {plan_of(state).plan_hash}")
        print(
            "STOPPED at the approval interrupt: nothing built or sent. Approve as the human at a "
            f"terminal: uv run python -m struck.send_cli {out} --approver <you>"
        )
        return 0
    assert reply.trigger is not None
    out.write_text(reply.trigger.model_dump_json(indent=2), encoding="utf-8")
    print(out.read_text(encoding="utf-8"))
    print(
        f"TriggerSpec is valid (strict model). Store it yourself: uv run python -m struck.triggers.cli add {out}"
    )
    print("A fired trigger only creates a proposal that needs fresh human approval.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
