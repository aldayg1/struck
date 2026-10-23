"""The LLM chat: the user's interface for expressing plans and automations
(spec §7.3 chat / LLM-as-interface). The LLM ONLY PROPOSES.

* Harness = a stock LangGraph agent graph:
  ``langchain.agents.create_agent`` (a LangGraph graph) with LangChain tools,
  ``ToolCallLimitMiddleware(exit_behavior="end")`` and LangGraph's
  ``recursion_limit`` backstop. Struck adds the stock ``response_format``
  (the ChatReply JSON schema; create_agent's stock auto strategy) for the final proposal
  and the stock ``InMemorySaver`` checkpointer for multi-turn history.
  ONE harness: only the chat model object changes per provider.
* Tools: read-only, served data only (struck/chat/tools.py).
* Output: The reply carries exactly ONE of the EXISTING strict
  models (on-chain ``PlanProposal``, Strike ``StrikePlanProposal``, trigger
  ``TriggerSpec``) or a clarifying question. The provider returns a dict; it is
  parsed with ``ChatReply.model_validate_json`` (strict JSON mode). Anything
  else is REJECTED; nothing is repaired.
* Context: ONLY served data handed in by the caller (``context_fn``).
* Nothing here approves, signs or sends: a proposal is only handed to the
  existing Approval / Bots / send_cli paths by an explicit human Review click.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, Self, TypedDict, cast

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError, model_validator

from ..config import load_config
from ..network import NetworkProfile
from ..plan.models import PlanProposal
from ..strike.models import StrikePlanProposal
from .tools import READ_ONLY_TOOLS
from ..triggers.models import TriggerSpec

ReplyKind = Literal["onchain_send", "strike", "trigger", "question"]


class ChatReply(BaseModel):
    """Envelope only: one existing proposal model, or a question."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: ReplyKind
    onchain: PlanProposal | None
    strike: StrikePlanProposal | None
    trigger: TriggerSpec | None
    question: StrictStr | None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        filled = {
            "onchain_send": self.onchain is not None,
            "strike": self.strike is not None,
            "trigger": self.trigger is not None,
            "question": self.question is not None,
        }
        if [k for k, v in filled.items() if v] != [self.kind]:
            raise ValueError(
                f"kind={self.kind!r} must carry exactly its own field (filled: {[k for k, v in filled.items() if v]})"
            )
        return self


# The stock JSON schema of the envelope (dict schema -> the provider returns a dict).
REPLY_SCHEMA: dict[str, object] = cast("dict[str, object]", ChatReply.model_json_schema())


class ChatRejected(RuntimeError):
    """The LLM output is not a valid ChatReply. Shown as REJECTED."""

    tool_calls: tuple[tuple[str, str], ...] = ()


def chat_prompt(net: NetworkProfile) -> str:
    """The system prompt names the SELECTED network (struck.network)."""
    return CHAT_PROMPT.format(cardano=net.cardano_label, strike=net.strike_label)


CHAT_PROMPT = """You are Struck's planning assistant (Refractic Labs). The human describes what they want;
you reply with exactly ONE structured ChatReply:
- kind "onchain_send": a {cardano} send as PlanProposal (lane "onchain"); lovelace are integers.
  The onchain field is a PLAN object with lane="onchain", intent (string), legs (array),
  rationale (string). Each leg is a send object: kind="send", from_address, outputs (an
  array of output objects, each exactly {{"address": ..., "lovelace": <integer lovelace>,
  "assets": []}}), max_fee_lovelace (integer), validity_minutes (integer > 0).
- kind "strike": {strike} perps as StrikePlanProposal (lane "strike"); decimals are strings.
  The strike field is a PLAN object with lane="strike", intent (string), legs (array),
  and rationale (string). Put each order or strategy inside legs, with its kind field.
  EXACT top-level shape for a strike reply (fill values; NEVER drop keys, NEVER nest
  a "plan" object, NEVER invent other keys):
  {{"kind": "strike", "strike": {{"lane": "strike", "intent": "...", "rationale": "...",
  "legs": [{{"kind": "order", "symbol": "BTC-USD", "side": "buy", "type": "market",
  "size": "0.01", "price": null, "stop_price": null, "callback_rate": null,
  "activation_price": null, "time_in_force": "GTC", "post_only": false,
  "reduce_only": false, "tp": null, "sl": null, "leverage": 2, "client_order_id": null}}]}}}}
  Every leg includes kind, symbol, side, type, size, price, stop_price, callback_rate,
  activation_price, time_in_force (exactly "GTC", "IOC" or "FOK" — never
  "good_till_cancelled"), post_only, reduce_only, tp, sl, leverage, client_order_id.
  Include required nullable fields as null when unused; post_only and reduce_only are booleans.
  size and all prices are decimal strings; leverage is an integer. Ordinary orders use
  side="buy" or "sell"; only TWAP uses "BUY" or "SELL". Use the full ChatReply tool schema.
  Leg kinds are Strike's own routes: order (POST /v2/order; type limit|market|stop|stop_limit|take_profit|take_profit_limit|trailing_stop_market — trailing_stop_market needs callback_rate "0.1".."5" and reduce_only true, activation_price optional), bracket (POST /v2/order/strategy; tp take_profit|take_profit_limit, sl stop|stop_limit), twap (POST /v2/algo/twap; side BUY|SELL, size is total_size, duration_sec 300..86400, price is optional limit_price), twap_cancel (strategy_id, symbol ""), cancel, cancel_all, replace (order_id plus the new order), batch (orders list), replace_batch (replacements list), leverage, margin_mode (cross|isolated), iso_margin (amount string, modify_type add|remove), grid_bot, grid_stop, copy_subscribe (POST /v2/copy/subscribe), copy_patch, copy_stop, copy_save (POST /v2/copy/saved), copy_unsave, rfq (POST /v2/rfq), rfq_close, rfq_response, withdraw_quote (POST /v2/withdraw/quote, does not withdraw), withdraw (wallet_signature is the human's signature of the quote message), vault_deposit, vault_withdraw, vault_withdraw_cancel, vault_create, vault_pause, vault_resume. Kinds with no market symbol use symbol "". Optional order fields only when asked: working_type mark_price|contract_price, slippage, close_position, price_protect, vault_id.
- kind "trigger": an automation/bot as TriggerSpec (condition tree + actions). A fired trigger only creates a NEW proposal that needs fresh human approval.
  The trigger field is an object: label (string), condition (ONE condition object; EVERY condition object MUST carry its "kind" discriminator field, e.g. "kind": "price"),
  rearm (true = fire again on later events, false = fire once at most),
  cooldown_s (integer >= 0), policy_id null, actions (an ordered array of action
  objects; REQUIRED unless the condition is a sequence, which keeps its actions on
  each step and forbids top-level actions). Condition objects, exactly one of:
  price {{"symbol", "price_type": mark|index|last, "op", "threshold": "<decimal string>"}},
  time_window {{"start_utc": "HH:MM", "end_utc": "HH:MM", "weekdays": [0..6 Mon=0]}},
  schedule {{"at_utc": "<ISO-8601 UTC instant>"}},
  interval {{"every_minutes": <integer >> 0>, "start_utc": null | instant}},
  incoming_payment {{"address", "from": <sender address or null>, "min_lovelace": <int>,
    "token": "<policy-id>.<asset-name-hex>" or null, "policy": <56-hex> or null,
    "min_token_qty": <int or null>}},
  wallet_balance {{"addresses": [<addr>...], "aggregate": sum|each, "symbol": "ada" | <unit>,
    "op", "threshold": <integer>}},
  strike_deposit {{"asset", "min_amount": "<decimal string>"}},
  strike_position {{"symbol", "field": upnl|liquidation_price|bankruptcy_price|maintenance_margin,
    "op", "threshold": "<decimal string>"}},
  composite {{"logic": and|or|xor, "children": [<condition>...]}},
  not {{"child": <condition>}},
  sequence {{"steps": [<one object per step, each with "condition", "actions" (1+), "timeout_s" (integer >> 0)>]}}.
  op is exactly one of "gt", "lt", "gte", "lte". Action objects, exactly one of:
  wallet_send {{"from_address", "outputs": [{{"address", "lovelace": <integer lovelace> |
    "incoming" (the literal string "incoming": forward the WHOLE incoming payment; never a
    boolean) }}, ...], "max_fee_lovelace": <int>, "validity_minutes": <int>}}
  (a percentage of a payment is instead the field {{"pct_of_incoming": "<decimal string>"}}:
  an output object carries its address and EXACTLY ONE of lovelace, the string "incoming",
  or pct_of_incoming — no other fields),
  strike_order {{"leg": <exactly the StrikeLeg object described under kind strike>}},
  strike_deposit {{"blockchain": cardano|ethereum|solana, "asset_symbol", "user_address",
    and exactly one of "asset_amount": "<positive integer string>" or
    "pct_of_incoming": "<decimal string >0 <= 100>", "mode": balance|collateral or null}}.
- kind "question": ask ONE clarifying question when anything needed is missing or ambiguous (address, size, price, symbol...). Never guess values.
Fill only the field of your kind; the others are null. You only PROPOSE: you never approve, sign or send.
Use the read-only tools (strike_markets, strike_depth, strike_positions, wallet_utxos) for facts;
only served data is fact. Do not invent prices, balances, addresses or symbols."""


# Ported constants ().
AGENT_TOOL_CALL_LIMIT = 5
AGENT_RECURSION_LIMIT = 25


class _ThreadConfig(TypedDict):
    configurable: dict[str, str]
    recursion_limit: int


class _Snapshot(Protocol):
    @property
    def values(self) -> Mapping[str, object]: ...


class _Graph(Protocol):
    def invoke(
        self, input: Mapping[str, object], config: _ThreadConfig
    ) -> Mapping[str, object]: ...

    def get_state(self, config: _ThreadConfig) -> _Snapshot: ...


def summary(reply: ChatReply) -> str:
    """Plain-language one-liner of a reply."""
    if reply.onchain is not None:
        return f"[proposed onchain_send] {reply.onchain.intent}"
    if reply.strike is not None:
        return f"[proposed strike, {len(reply.strike.legs)} leg(s)] {reply.strike.intent}"
    if reply.trigger is not None:
        return f"[proposed trigger] {reply.trigger.label}"
    return f"[question] {reply.question}"


@dataclass(frozen=True)
class Turn:
    """One chat turn as it happened: the tool calls the model made, served
    token usage, and the validated reply."""

    reply: ChatReply
    tool_calls: tuple[tuple[str, str], ...]  # (name, args JSON)
    usage: tuple[str, ...]


class ChatAssistant:
    def __init__(
        self,
        model: BaseChatModel,
        context_fn: Callable[[], str] = lambda: "",
        tools: Sequence[BaseTool] = READ_ONLY_TOOLS,
        thread_id: str = "chat",
    ) -> None:
        self._context_fn = context_fn
        self._config: _ThreadConfig = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": AGENT_RECURSION_LIMIT,
        }
        middleware = [
            cast(
                "AgentMiddleware[AgentState[object], None, object]",
                ToolCallLimitMiddleware(run_limit=AGENT_TOOL_CALL_LIMIT, exit_behavior="end"),
            )
        ]
        tool_list: list[BaseTool] = list(tools)
        self._graph = cast(
            "_Graph",
            create_agent(
                model=model,
                tools=tool_list,
                system_prompt=chat_prompt(load_config().profile),
                middleware=middleware,
                # Stock auto strategy: create_agent picks the provider's native
                # structured output when the model profile supports it, else
                # its ToolStrategy. Same code for every provider.
                response_format=REPLY_SCHEMA,
                checkpointer=InMemorySaver(),
            ),
        )
        self._seen = 0

    def ask(self, text: str) -> Turn:
        if not text.strip():
            raise ChatRejected("empty message")
        context = self._context_fn()
        content = f"{text}\n\n[served context]\n{context}" if context else text
        try:
            out = self._graph.invoke({"messages": [HumanMessage(content)]}, self._config)
        except (ValueError, TypeError, KeyError) as exc:  # stock structured-output errors included
            raise ChatRejected(
                f"LLM output is not a valid ChatReply: {type(exc).__name__}: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001  SDK exceptions must reach the UI failure signal
            raise ChatRejected(f"Planner failed: {type(exc).__name__}: {exc}") from exc
        messages = cast("list[BaseMessage]", out.get("messages", []))
        new = messages[self._seen :]
        self._seen = len(messages)
        calls: list[tuple[str, str]] = []
        usage: list[str] = []
        for m in new:
            if m.type == "ai":
                ai = cast("AIMessage", m)
                for c in cast("list[dict[str, object]]", ai.tool_calls):
                    calls.append((str(c["name"]), json.dumps(c["args"], sort_keys=True)))
                if ai.usage_metadata:
                    u = ai.usage_metadata
                    usage.append(
                        f"in={u['input_tokens']} out={u['output_tokens']} total={u['total_tokens']}"
                    )
        raw = out.get("structured_response")
        try:
            if raw is None:
                raise ValueError(
                    "the model ended without a structured ChatReply (tool-call limit or plain text)"
                )
            reply = ChatReply.model_validate_json(json.dumps(raw))
        except (ValidationError, TypeError, ValueError) as exc:
            rejected = ChatRejected(f"LLM output is not a valid ChatReply: {exc}")
            rejected.tool_calls = tuple(calls)
            raise rejected from exc
        return Turn(reply, tuple(calls), tuple(usage))

    def history(self) -> list[tuple[str, str]]:
        values = self._graph.get_state(self._config).values
        messages = cast("list[BaseMessage]", values.get("messages", []))
        return [(m.type, m.text) for m in messages]
