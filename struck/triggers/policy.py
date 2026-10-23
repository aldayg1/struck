"""Spec D5: the OPTIONAL opt-in pre-approval policy. Pydantic mirror of Rust
``struck_core::policy::Policy``; the check itself is Rust.

A policy counts only when a HUMAN approved its exact hash with
``python -m struck.triggers.policy_cli approve`` (typed, never scripted). No
policy, an unapproved one, an expired one, or a proposal outside it: the
route is a fresh approval (the default). In this build an inside-policy
verdict is RECORDED on the fire; the pipelines still stop at their approval
interrupt (wiring the skip would change the milestone-1 pipeline).
"""

from __future__ import annotations

from ..network import NetworkName

import hashlib
import hmac
from datetime import datetime
from pathlib import Path
from typing import Literal

import struck_py
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr

from ..config import REPO_ROOT
from ..plan.models import CheckReport, PlanDecision, PlanProposal, SendOutput
from ..strike.deposit import DepositPlanProposal
from ..strike.market import StrikeMarketClient
from ..strike.models import DecStr, StrikePlanProposal

POLICY_DIR = REPO_ROOT / "data" / "policies"


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class OnchainPolicy(_Strict):
    allowed_addresses: tuple[StrictStr, ...]
    max_lovelace_per_output: StrictInt
    max_lovelace_total: StrictInt
    max_fee_lovelace: StrictInt
    allow_native_assets: StrictBool


class StrikePolicy(_Strict):
    allowed_symbols: tuple[StrictStr, ...]
    allowed_kinds: tuple[
        Literal[
            "order",
            "bracket",
            "leverage",
            "cancel",
            "twap",
            "margin_mode",
            "grid_bot",
            "grid_stop",
            "cancel_all",
            "iso_margin",
            "twap_cancel",
            "replace",
            "batch",
            "replace_batch",
            "copy_subscribe",
            "copy_patch",
            "copy_stop",
            "copy_save",
            "copy_unsave",
            "rfq",
            "rfq_close",
            "rfq_response",
            "withdraw_quote",
            "withdraw",
            "vault_deposit",
            "vault_withdraw",
            "vault_withdraw_cancel",
            "vault_create",
            "vault_pause",
            "vault_resume",
        ],
        ...,
    ]
    allowed_order_types: tuple[StrictStr, ...]
    allowed_sides: tuple[Literal["buy", "sell", "BUY", "SELL"], ...]
    max_notional: DecStr
    max_leverage: StrictInt
    reduce_only_required: StrictBool


class Policy(_Strict):
    policy_id: StrictStr
    # On-chain policies name the network (a struck.network.PROFILES key); Strike
    # policies that row's strike_venue. Rust checks it equals the SELECTED
    # network's value for the lane.
    network: StrictStr
    expires_at_unix_ms: StrictInt
    onchain: OnchainPolicy | None
    strike: StrikePolicy | None

    @property
    def policy_hash(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()


class ProposedSend(_Strict):
    """Rust ``policy::ProposedSend``: what a fired send proposes, pre-N2."""

    network: NetworkName
    from_address: StrictStr
    outputs: tuple[SendOutput, ...]
    max_fee_lovelace: StrictInt


class ApprovedPolicies:
    def __init__(self, directory: Path = POLICY_DIR) -> None:
        self._dir = directory

    def load(self, policy_id: str) -> Policy | None:
        path = self._dir / f"{policy_id}.json"
        approval = self._dir / f"{policy_id}.approval.json"
        if not path.exists() or not approval.exists():
            return None
        policy = Policy.model_validate_json(path.read_bytes())
        decision = PlanDecision.model_validate_json(approval.read_bytes())
        if decision.decision != "approve" or not hmac.compare_digest(
            decision.plan_hash, policy.policy_hash
        ):
            return None
        return policy

    def route(
        self,
        policy_id: str | None,
        proposal: PlanProposal | StrikePlanProposal | DepositPlanProposal,
        market: StrikeMarketClient,
        now: datetime,
    ) -> str:
        if policy_id is None:
            return "fresh_approval"
        policy = self.load(policy_id)
        if policy is None:
            return f"fresh_approval (policy {policy_id} missing or not human-approved)"
        now_ms = int(now.timestamp() * 1000)
        if isinstance(proposal, DepositPlanProposal):
            return "fresh_approval (a deposit has no policy section; the human approves the unsigned tx)"
        if isinstance(proposal, PlanProposal):
            leg = proposal.legs[0]
            send = ProposedSend(
                network=market.network.name,  # the selected network (one setting)
                from_address=leg.from_address,
                outputs=leg.outputs,
                max_fee_lovelace=leg.max_fee_lovelace,
            )
            raw = struck_py.policy_check_onchain(
                policy.model_dump_json(), send.model_dump_json(), now_ms
            )
        else:
            legs = "[" + ",".join(leg.model_dump_json() for leg in proposal.legs) + "]"
            markets = market.markets().served.content.decode("utf-8")
            raw = struck_py.policy_check_strike(
                policy.model_dump_json(), market.network.strike_venue, legs, markets, now_ms
            )
        report = CheckReport.model_validate_json(raw)
        if report.ok:
            return f"inside_policy:{policy_id} (recorded; the pipeline still asks in this build)"
        return f"fresh_approval (outside policy {policy_id}: {[c.id for c in report.checks if not c.ok]})"
