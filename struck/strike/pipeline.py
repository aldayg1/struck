"""The Strike lane as a LangGraph ``StateGraph`` (spec §3.1, §4.1(b)):

  propose_plan (N1) -> validate_plan (N2, Rust vs FRESH /v2/markets) ->
  approve_plan (N3 interrupt: HUMAN) -> verify_approval ->
  build_requests (N4s, Rust exact bodies) -> congruence (N5s, Rust: body ==
  approved leg, re-validated vs a SECOND fresh /v2/markets) + explain (N6s) ->
  write_receipts -> confirm_request (N7s interrupt: HUMAN confirm)

-> execute (N8s: the EXACT approved bytes, Ed25519 API-wallet signed, to the
selected network's Strike base URL; ``struck.strike.auth``). N9s (user-stream fill confirm)
is not built: the HTTP 201/200 only means "sequenced". Same rules as the on-chain lane: a failed check
routes to END with ``rejected``; each ``interrupt()`` node has no side
effects; all money checks are Rust (``struck_py``).
"""

from __future__ import annotations

import hmac
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast

import struck_py
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
import httpx
from pydantic import TypeAdapter

from ..callhistory import CallFailed, Served, sha256_hex
from ..config import RECEIPTS_DIR
from ..network import NetworkProfile, profile_for_strike_venue
from ..plan.models import PlanDecision
from .market import StrikeMarketClient
from .models import (
    ApiRequest,
    ApprovedStrikePlan,
    LegValidation,
    PreparedRequest,
    SentLeg,
    RequestCongruence,
    RequestExplanation,
    StrikeApprovalRequest,
    StrikeConfirm,
    StrikeLeg,
    StrikePlanProposal,
    StrikeSignRequest,
)

# Tier index passed into the Rust rule check. Tier 0 is the estimate.
# The account's served tier is GET /v2/portfolio (Liquidity tab), not this constant.
FEE_TIER_ESTIMATE = 0
_VALIDATIONS = TypeAdapter(tuple[LegValidation, ...])
_PREPARED = TypeAdapter(tuple[PreparedRequest, ...])
SENT = TypeAdapter(tuple[SentLeg, ...])

# Client order ids whose send outcome is UNKNOWN (transport error after the
# request may have left). Persistent across runs; any plan reusing one is
# refused until a human resyncs with Strike and removes the line.
UNRESOLVED_LEDGER = "UNRESOLVED_SENDS.jsonl"


def unresolved_legs(root: Path) -> tuple[SentLeg, ...]:
    path = root / UNRESOLVED_LEDGER
    if not path.exists():
        return ()
    lines = path.read_text(encoding="utf-8").splitlines()
    return tuple(SentLeg.model_validate_json(line) for line in lines if line.strip())


def unresolved_client_ids(root: Path) -> set[str]:
    return {
        leg.client_order_id
        for leg in unresolved_legs(root)
        if leg.client_order_id is not None
    }


def _record_unresolved(root: Path, leg: SentLeg) -> None:
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / UNRESOLVED_LEDGER, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (leg.model_dump_json() + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


# Order authorization is the Ed25519 API-wallet request signature only (Strike
# OpenAPI ``POST /v2/order``: security = the four X-API-Wallet-* headers or a
# JWT; ``CreateOrderRequest`` has no wallet-signature field), exactly as the
# ). The human
# gates stay: ``approve <plan_hash>``, then ``send <plan_hash>``.


class StrikeSender(Protocol):
    """The API-wallet signed client (``struck.strike.account.StrikeAuthedClient``)."""

    @property
    def network(self) -> NetworkProfile: ...

    @property
    def base_url(self) -> str: ...

    def request(self, method: str, path_with_query: str, body: bytes | None = None) -> Served: ...


# Exactly the routes ``strike_request.rs`` builds. ``execute()`` sends those
# bytes through ``StrikeAuthedClient.request`` after the human confirm.
# Anything else is refused. ``DELETE /v2/grid/bot/{bot_id}`` is the one
# parameterised route: allowed only with a served bot id shape (the Rust
# builder + congruence check it before it can ever reach here).
SENDABLE = frozenset(
    {
        ("POST", "/v2/order"),
        ("POST", "/v2/order/strategy"),
        ("POST", "/v2/leverage"),
        ("POST", "/v2/algo/twap"),
        ("POST", "/v2/marginMode"),
        ("POST", "/v2/grid/bot"),
        ("DELETE", "/v2/order/cancel"),
        ("DELETE", "/v2/order/cancel-all"),
        ("POST", "/v2/order/replace"),
        ("POST", "/v2/order/replace-batch"),
        ("POST", "/v2/orders/batch"),
        ("POST", "/v2/isoMargin"),
        ("POST", "/v2/copy/subscribe"),
        ("POST", "/v2/copy/saved"),
        ("POST", "/v2/rfq"),
        ("POST", "/v2/withdraw/quote"),
        ("POST", "/v2/withdraw"),
        ("POST", "/v2/vault/deposit"),
        ("POST", "/v2/vault/withdraw"),
        ("POST", "/v2/vault/create"),
    }
)
_COPY_SUB = re.compile(r"^/v2/copy/subscribe/[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_COPY_SAVED = re.compile(r"^/v2/copy/saved/[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_COPY_SUB_Q = re.compile(
    r"^/v2/copy/subscribe/[A-Za-z0-9][A-Za-z0-9_-]{0,127}\?sub_account_id=[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
)
_RFQ_ID = re.compile(r"^/v2/rfq/[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_RFQ_Q = re.compile(
    r"^/v2/rfq/[A-Za-z0-9][A-Za-z0-9_-]{0,127}\?(?:vault_id|sub_account_id)=[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
)
_RFQ_RESPONSE = re.compile(r"^/v2/rfq/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/response$")
_VAULT_CANCEL = re.compile(
    r"^/v2/vault/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/withdrawals/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/cancel$"
)
_VAULT_PAUSE = re.compile(r"^/v2/vault/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/(?:pause|resume)$")
_GRID_STOP_PATH = re.compile(r"^/v2/grid/bot/grid-[A-Za-z0-9-]{1,195}$")
_TWAP_CANCEL_PATH = re.compile(r"^/v2/algo/twap/[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def route_sendable(method: str, path: str) -> bool:
    """(method, path) is a route Struck itself builds, with an exact body."""
    if (method, path) in SENDABLE:
        return True
    if method == "DELETE" and (
        _GRID_STOP_PATH.fullmatch(path) is not None
        or _TWAP_CANCEL_PATH.fullmatch(path) is not None
        or _COPY_SUB.fullmatch(path) is not None
        or _COPY_SAVED.fullmatch(path) is not None
        or _COPY_SUB_Q.fullmatch(path) is not None
        or _RFQ_ID.fullmatch(path) is not None
        or _RFQ_Q.fullmatch(path) is not None
    ):
        return True
    if method == "PATCH" and _COPY_SUB.fullmatch(path) is not None:
        return True
    if method == "PUT" and _RFQ_RESPONSE.fullmatch(path) is not None:
        return True
    return method == "POST" and (
        _VAULT_CANCEL.fullmatch(path) is not None or _VAULT_PAUSE.fullmatch(path) is not None
    )


class StrikePlanner(Protocol):
    def propose(self, request: str) -> StrikePlanProposal: ...


class FixedStrikePlanner:
    """A fixed plan document instead of a live LLM (proof runs, tests)."""

    def __init__(self, plan_json: str) -> None:
        self._plan_json = plan_json

    def propose(self, request: str) -> StrikePlanProposal:
        return StrikePlanProposal.model_validate_json(self._plan_json)


class StrikeState(TypedDict, total=False):
    request: str
    proposal_json: str
    plan_json: str
    markets_json: str
    fee_tiers_json: str
    validations_json: str
    decision_json: str
    receipts_dir: str
    prepared_json: str
    confirm_request_json: str
    confirm_json: str
    executed_json: str
    rejected: str


def plan_of(state: StrikeState) -> ApprovedStrikePlan:
    return ApprovedStrikePlan.model_validate_json(state["plan_json"])


def validations_of(state: StrikeState) -> tuple[LegValidation, ...]:
    return _VALIDATIONS.validate_json(state["validations_json"])


def prepared_of(state: StrikeState) -> tuple[PreparedRequest, ...]:
    return _PREPARED.validate_json(state["prepared_json"])


def confirm_request_of(state: StrikeState) -> StrikeSignRequest:
    return StrikeSignRequest.model_validate_json(state["confirm_request_json"])


def leg_json(leg: StrikeLeg) -> str:
    return leg.model_dump_json()


def _write(directory: Path, name: str, text: str) -> None:
    (directory / name).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def _pretty(json_text: str) -> str:
    return json.dumps(cast("object", json.loads(json_text)), indent=2)


def summarize(plan: ApprovedStrikePlan) -> tuple[str, ...]:
    lines: list[str] = []
    for i, leg in enumerate(plan.legs):
        if leg.kind in ("order", "bracket"):
            px = f" @ {leg.price}" if leg.price is not None else ""
            stop = f" stop {leg.stop_price}" if leg.stop_price is not None else ""
            lines.append(
                f"leg {i}: {leg.kind} {leg.side} {leg.size} {leg.symbol} {leg.type}{px}{stop}"
            )
            lines.append(
                f"leg {i}: time_in_force {leg.time_in_force} reduce_only {str(leg.reduce_only).lower()}"
                f" post_only {str(leg.post_only).lower()} callback_rate {leg.callback_rate}"
                f" activation_price {leg.activation_price} leverage {leg.leverage}"
                f" client_order_id {leg.client_order_id}"
            )
            if leg.tp is not None:
                lines.append(
                    f"leg {i}: take-profit {leg.tp.type} at {leg.tp.stop_price} price {leg.tp.price}"
                )
            if leg.sl is not None:
                lines.append(
                    f"leg {i}: stop-loss {leg.sl.type} at {leg.sl.stop_price} price {leg.sl.price}"
                )
        elif leg.kind == "leverage":
            lines.append(f"leg {i}: set {leg.symbol} leverage {leg.leverage}x")
        elif leg.kind == "margin_mode":
            lines.append(f"leg {i}: set {leg.symbol} margin mode {leg.margin_mode}")
        elif leg.kind == "twap":
            limit = f" limit {leg.price}" if leg.price is not None else ""
            lines.append(
                f"leg {i}: twap {leg.side} {leg.size} {leg.symbol} over {leg.duration_sec}s{limit}"
                f" reduce_only {str(leg.reduce_only).lower()} randomize {str(leg.randomize).lower()}"
            )
        elif leg.kind == "grid_bot":
            init = leg.initialization_mode or "LIMIT_ONLY"
            lines.append(
                f"leg {i}: grid bot {leg.grid_mode} {leg.symbol} {leg.grid_count} levels "
                f"{leg.lower_price}..={leg.upper_price} collateral {leg.collateral} USD {init}"
            )
        elif leg.kind == "grid_stop":
            lines.append(
                f"leg {i}: stop grid bot {leg.bot_id} "
                f"cancel_orders {str(leg.cancel_orders).lower()}"
            )
        elif leg.kind == "cancel":
            lines.append(f"leg {i}: cancel {leg.symbol} {leg.client_order_id}")
        elif leg.kind == "cancel_all":
            lines.append(f"leg {i}: cancel every open order on {leg.symbol}")
        elif leg.kind == "iso_margin":
            lines.append(f"leg {i}: isolated margin {leg.modify_type} {leg.amount} on {leg.symbol}")
        elif leg.kind == "twap_cancel":
            lines.append(f"leg {i}: cancel TWAP {leg.strategy_id}")
        elif leg.kind == "replace":
            lines.append(
                f"leg {i}: replace order {leg.order_id} with {leg.side} {leg.size} {leg.symbol} {leg.type}"
            )
        elif leg.kind == "batch":
            n = 0 if leg.orders is None else len(leg.orders)
            lines.append(f"leg {i}: batch of {n} order(s)")
        elif leg.kind == "replace_batch":
            n = 0 if leg.replacements is None else len(leg.replacements)
            lines.append(f"leg {i}: replace batch of {n} order(s)")
        elif leg.kind == "copy_subscribe":
            lines.append(
                f"leg {i}: copy {leg.platform} lead {leg.lead_account_id} mode {leg.copy_mode}"
                f" amount {leg.copy_amount or leg.margin_per_entry_order}"
            )
        elif leg.kind == "copy_patch":
            lines.append(f"leg {i}: patch copy subscription {leg.subscription_id}")
        elif leg.kind == "copy_stop":
            lines.append(f"leg {i}: stop copy subscription {leg.subscription_id}")
        elif leg.kind == "copy_save":
            lines.append(f"leg {i}: save lead {leg.lead_account_id} (bookmark, no copy)")
        elif leg.kind == "copy_unsave":
            lines.append(f"leg {i}: remove saved lead {leg.lead_account_id}")
        elif leg.kind == "rfq":
            lines.append(
                f"leg {i}: rfq {leg.side or 'both'} {leg.requested_size} {leg.symbol}"
                f" client {leg.client_rfq_id}"
            )
        elif leg.kind == "rfq_close":
            lines.append(f"leg {i}: close rfq {leg.rfq_id}")
        elif leg.kind == "rfq_response":
            lines.append(f"leg {i}: rfq response {leg.response_status} on {leg.rfq_id}")
        elif leg.kind == "withdraw_quote":
            lines.append(
                f"leg {i}: withdraw quote {leg.usd_amount} USD on {leg.blockchain} (no withdrawal yet)"
            )
        elif leg.kind == "withdraw":
            lines.append(f"leg {i}: confirm withdraw {leg.withdraw_id} with the human wallet signature")
        elif leg.kind == "vault_deposit":
            lines.append(f"leg {i}: vault deposit {leg.usd_amount} USD into {leg.vault_id}")
        elif leg.kind == "vault_withdraw":
            which = "all shares" if leg.withdraw_all else f"{leg.shares} shares"
            lines.append(f"leg {i}: vault withdraw {which} from {leg.vault_id}")
        elif leg.kind == "vault_withdraw_cancel":
            lines.append(f"leg {i}: cancel vault withdrawal {leg.withdrawal_id} on {leg.vault_id}")
        elif leg.kind == "vault_create":
            lines.append(
                f"leg {i}: create vault {leg.vault_name} deposit {leg.initial_deposit} USD"
                f" commission {leg.leader_commission_bps} bps lockup {leg.lockup_days}d"
            )
        elif leg.kind == "vault_pause":
            lines.append(f"leg {i}: pause vault {leg.vault_id}")
        elif leg.kind == "vault_resume":
            lines.append(f"leg {i}: resume vault {leg.vault_id}")
        else:
            lines.append(f"leg {i}: {leg.kind} {leg.symbol}")
    net = profile_for_strike_venue(plan.network)
    lines.append(f"{net.strike_label}; nothing is sent before this approval and the later confirm")
    return tuple(lines)


class StrikePipeline:
    def __init__(
        self,
        planner: StrikePlanner,
        market: StrikeMarketClient,
        receipts_root: Path = RECEIPTS_DIR,
        sender: StrikeSender | None = None,
    ) -> None:
        self._planner = planner
        self._sender = sender
        self._market = market
        self._receipts_root = receipts_root

    # N1
    def propose_plan(self, state: StrikeState) -> StrikeState:
        return {"proposal_json": self._planner.propose(state["request"]).model_dump_json()}

    # N2 ─ Rust rule check of every leg against a FRESH /v2/markets.
    def validate_plan(self, state: StrikeState) -> StrikeState:
        proposal = StrikePlanProposal.model_validate_json(state["proposal_json"])
        markets = self._market.markets().served
        fees = self._market.fee_tiers()
        markets_text = markets.content.decode("utf-8")
        plan = ApprovedStrikePlan(
            network=self._market.network.strike_venue,
            legs=proposal.legs,
            markets_snapshot_sha256=markets.sha256,
        )
        checked: list[LegValidation] = []
        for i, leg in enumerate(plan.legs):
            try:
                report = struck_py.strike_validate_leg(
                    leg_json(leg), markets_text, fees.content.decode("utf-8"), FEE_TIER_ESTIMATE
                )
            except ValueError as exc:
                # The Rust check refuses a malformed leg outright: a loud rejection
                # before approval, never a crash and never a partial plan.
                return {"rejected": f"plan validation refused leg {i}: {exc}"}
            checked.append(LegValidation.model_validate_json(report))
        validations = tuple(checked)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = self._receipts_root / f"{stamp}-strike-{plan.plan_hash[:12]}"
        out.mkdir(parents=True, exist_ok=False)
        _write(out, "plan.proposal.json", _pretty(state["proposal_json"]))
        _write(out, "plan.pending.json", _pretty(plan.canonical_json))
        _write(out, "plan.validation.json", _pretty(_VALIDATIONS.dump_json(validations).decode()))
        _write(out, "strike.markets.n2.json", markets_text)
        _write(out, "strike.fee_tiers.json", fees.content.decode("utf-8"))
        update: StrikeState = {
            "plan_json": plan.canonical_json,
            "markets_json": markets_text,
            "fee_tiers_json": fees.content.decode("utf-8"),
            "validations_json": _VALIDATIONS.dump_json(validations).decode(),
            "receipts_dir": str(out),
        }
        failed = [
            f"leg {i}: {c.id}"
            for i, v in enumerate(validations)
            for c in v.report.checks
            if not c.ok
        ]
        if failed:
            update["rejected"] = f"plan validation failed: {failed}"
        return update

    # N3 ─ HUMAN approval; interrupt() is the only effect.
    def approve_plan(self, state: StrikeState) -> StrikeState:
        plan = plan_of(state)
        request = StrikeApprovalRequest(
            plan_hash=plan.plan_hash,
            plan=plan,
            validations=validations_of(state),
            summary=summarize(plan),
        )
        decision = interrupt(request.model_dump_json(), response_schema=PlanDecision)
        return {"decision_json": decision.model_dump_json()}

    def verify_approval(self, state: StrikeState) -> StrikeState:
        decision = PlanDecision.model_validate_json(state["decision_json"])
        if decision.decision != "approve":
            return {"rejected": f"plan rejected by {decision.approver}"}
        if not hmac.compare_digest(decision.plan_hash, plan_of(state).plan_hash):
            return {"rejected": "approval names a different plan hash; nothing is built"}
        return {}

    # N4s + N5s + N6s ─ exact bodies, congruence vs a SECOND fresh read, explanation.
    def build_requests(self, state: StrikeState) -> StrikeState:
        plan = plan_of(state)
        fresh = self._market.markets().served.content.decode("utf-8")
        _write(Path(state["receipts_dir"]), "strike.markets.n5s.json", fresh)
        prepared: list[PreparedRequest] = []
        for i, leg in enumerate(plan.legs):
            coid = plan.client_order_id(i)
            raw = struck_py.strike_build_request(leg_json(leg), coid)
            request = ApiRequest.model_validate_json(raw)
            congruence = RequestCongruence.model_validate_json(
                struck_py.strike_request_congruence(
                    raw, leg_json(leg), coid, fresh, state["fee_tiers_json"], FEE_TIER_ESTIMATE
                )
            )
            explanation = RequestExplanation.model_validate_json(
                struck_py.strike_explain(
                    raw,
                    leg_json(leg),
                    fresh,
                    state["fee_tiers_json"],
                    FEE_TIER_ESTIMATE,
                    profile_for_strike_venue(plan.network).rust_json(),
                )
            )
            prepared.append(
                PreparedRequest(request=request, congruence=congruence, explanation=explanation)
            )
        update: StrikeState = {"prepared_json": _PREPARED.dump_json(tuple(prepared)).decode()}
        failed = [
            f"leg {i}: {c.id}"
            for i, p in enumerate(prepared)
            for c in p.congruence.report.checks
            if not c.ok
        ]
        if failed:
            update["rejected"] = f"request congruence failed: {failed}"
        return update

    def write_receipts(self, state: StrikeState) -> StrikeState:
        out = Path(state["receipts_dir"])
        plan = plan_of(state)
        prepared = prepared_of(state)
        _write(out, "plan.approved.json", _pretty(plan.canonical_json))
        _write(out, "plan.decision.json", _pretty(state["decision_json"]))
        words: list[str] = []
        for i, p in enumerate(prepared):
            _write(out, f"leg{i}.request.json", _pretty(p.request.model_dump_json()))
            _write(out, f"leg{i}.request.body.json", p.request.body)
            _write(out, f"leg{i}.congruence.json", _pretty(p.congruence.model_dump_json()))
            _write(out, f"leg{i}.explanation.json", _pretty(p.explanation.model_dump_json()))
            words.extend(p.explanation.lines)
        text = "\n".join(words)
        _write(out, "explanation.txt", text)
        confirm = StrikeSignRequest(
            plan_hash=plan.plan_hash,
            requests=tuple(p.request for p in prepared),
            explanation_sha256=sha256_hex(text.encode("utf-8")),
            receipts_dir=str(out),
        )
        _write(out, "confirm.request.json", _pretty(confirm.model_dump_json()))
        return {"confirm_request_json": confirm.model_dump_json()}

    # N7s ─ HUMAN confirm before any API-wallet send; interrupt() is the only effect.
    def confirm_request(self, state: StrikeState) -> StrikeState:
        confirm = interrupt(state["confirm_request_json"], response_schema=StrikeConfirm)
        return {"confirm_json": confirm.model_dump_json()}

    # N8s ─ send the EXACT approved request bytes, signed by the API wallet.
    # Preconditions re-checked here (fail closed): human "send" naming this
    # plan hash, every leg congruent, every route one Struck builds. Legs go
    # in plan order; the first HTTP failure stops the rest. Each served
    # response is saved as a receipt (headers are never written anywhere).
    def execute(self, state: StrikeState) -> StrikeState:
        plan = plan_of(state)
        confirm = StrikeConfirm.model_validate_json(state["confirm_json"])
        if confirm.decision != "send":
            return {"rejected": f"send aborted by {confirm.approver}; nothing sent"}
        if not hmac.compare_digest(confirm.plan_hash, plan.plan_hash):
            return {"rejected": "confirm names a different plan hash; nothing sent"}
        prepared = prepared_of(state)
        bad = [
            f"leg {i}: {c.id}"
            for i, p in enumerate(prepared)
            for c in p.congruence.report.checks
            if not c.ok
        ]
        if bad:
            return {"rejected": f"request congruence failed: {bad}; nothing sent"}
        routes = [(p.request.method, p.request.path) for p in prepared]
        if any(not route_sendable(m, path) for m, path in routes):
            return {"rejected": f"unsendable route in {routes}; nothing sent"}
        if self._sender is None:
            return {"rejected": "no Strike API wallet client; nothing sent"}
        if plan.network != self._market.network.strike_venue:
            return {
                "rejected": f"NETWORK MISMATCH: plan is for Strike {plan.network}, the client "
                f"is on Strike {self._market.network.strike_venue}; nothing sent"
            }
        approved_net = profile_for_strike_venue(plan.network)
        if (
            self._sender.network.name != approved_net.name
            or self._sender.base_url != approved_net.strike_api_url
        ):
            return {
                "rejected": f"NETWORK MISMATCH: plan is for Strike {plan.network} "
                f"({approved_net.strike_api_url}), the SIGNING client is on "
                f"{self._sender.network.strike_venue} ({self._sender.base_url}); nothing sent"
            }
        pending = unresolved_legs(self._receipts_root)
        unidentified = [
            f"{leg.method} {leg.path}"
            for leg in pending
            if leg.client_order_id is None
        ]
        if unidentified:
            return {
                "rejected": f"REFUSED: prior write outcome UNKNOWN without a client order id: "
                f"{unidentified}. Reconcile the saved response and Strike's native state before "
                f"clearing {self._receipts_root / UNRESOLVED_LEDGER}; nothing sent. "
                "The ledger has no account/network scope, so this blocks all sends using it."
            }
        unresolved = {leg.client_order_id for leg in pending if leg.client_order_id is not None} & {
            p.request.client_order_id for p in prepared if p.request.client_order_id is not None
        }
        if unresolved:
            return {
                "rejected": f"REFUSED: client order id(s) {sorted(unresolved)} have an UNKNOWN "
                "outcome from an earlier send (transport error). Resync first: check Strike "
                "(open orders / trades / positions) for those ids, then remove their lines from "
                f"{self._receipts_root / UNRESOLVED_LEDGER}; nothing sent"
            }
        out = Path(state["receipts_dir"])
        sent: list[SentLeg] = []
        for i, p in enumerate(prepared):
            try:
                served = self._sender.request(
                    p.request.method, p.request.path, p.request.body.encode("utf-8")
                )
            except httpx.HTTPError as e:
                unknown = SentLeg(
                    leg=i,
                    method=p.request.method,
                    path=p.request.path,
                    client_order_id=p.request.client_order_id,
                    outcome="unknown",
                    status=None,
                    response_sha256=None,
                    error=f"{type(e).__name__}: {e}",
                )
                sent.append(unknown)
                _record_unresolved(self._receipts_root, unknown)
                _write(out, f"leg{i}.response.error.txt", unknown.error or "")
                _write(out, "executed.json", _pretty(SENT.dump_json(tuple(sent)).decode()))
                return {
                    "executed_json": SENT.dump_json(tuple(sent)).decode(),
                    "rejected": f"leg {i} outcome UNKNOWN ({unknown.error}): it may or may not "
                    f"have reached Strike; later legs NOT sent. Reconcile the native "
                    f"{p.request.path} state before retrying; unresolved ledger: "
                    f"{self._receipts_root / UNRESOLVED_LEDGER}",
                }
            except CallFailed as e:
                _write(out, f"leg{i}.response.error.txt", str(e))
                _write(out, "executed.json", _pretty(SENT.dump_json(tuple(sent)).decode()))
                return {
                    "executed_json": SENT.dump_json(tuple(sent)).decode(),
                    "rejected": f"leg {i} failed, later legs NOT sent: {e}",
                }
            (out / f"leg{i}.response.json").write_bytes(served.content)  # as served, byte for byte
            sent.append(
                SentLeg(
                    leg=i,
                    method=p.request.method,
                    path=p.request.path,
                    client_order_id=p.request.client_order_id,
                    outcome="served",
                    status=served.status,
                    response_sha256=served.sha256,
                    error=None,
                )
            )
        _write(out, "executed.json", _pretty(SENT.dump_json(tuple(sent)).decode()))
        return {"executed_json": SENT.dump_json(tuple(sent)).decode()}


def _after_validate(state: StrikeState) -> Literal["approve_plan", "__end__"]:
    return "__end__" if "rejected" in state else "approve_plan"


def _after_approval(state: StrikeState) -> Literal["build_requests", "__end__"]:
    return "__end__" if "rejected" in state else "build_requests"


def _after_build(state: StrikeState) -> Literal["write_receipts", "__end__"]:
    return "__end__" if "rejected" in state else "write_receipts"


def build_strike_graph(
    pipeline: StrikePipeline,
) -> CompiledStateGraph[StrikeState, None, StrikeState, StrikeState]:
    graph = StateGraph(StrikeState)
    graph.add_node("propose_plan", pipeline.propose_plan)
    graph.add_node("validate_plan", pipeline.validate_plan)
    graph.add_node("approve_plan", pipeline.approve_plan)
    graph.add_node("verify_approval", pipeline.verify_approval)
    graph.add_node("build_requests", pipeline.build_requests)
    graph.add_node("write_receipts", pipeline.write_receipts)
    graph.add_node("confirm_request", pipeline.confirm_request)
    graph.add_edge(START, "propose_plan")
    graph.add_edge("propose_plan", "validate_plan")
    graph.add_conditional_edges("validate_plan", _after_validate)
    graph.add_edge("approve_plan", "verify_approval")
    graph.add_conditional_edges("verify_approval", _after_approval)
    graph.add_conditional_edges("build_requests", _after_build)
    graph.add_edge("write_receipts", "confirm_request")
    graph.add_node("execute", pipeline.execute)
    graph.add_edge("confirm_request", "execute")
    graph.add_edge("execute", END)
    return graph.compile(checkpointer=InMemorySaver())
