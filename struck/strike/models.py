"""Pydantic boundary models of the Strike lane (spec §3.2, §4.1(b)).

Strict, frozen, ``extra="forbid"``: a wrong shape is rejected, never
repaired. Decimals are ``DecStr`` (the exact strings sent to Strike and
parsed exactly in Rust); no floats anywhere.

``StrikeLeg`` is the spec's flat leg. Kinds built: ``order``, ``bracket``,
``leverage``, ``cancel``, ``twap``. ``twap`` is ``POST /v2/algo/twap``
(strike-trading): ``side`` is ``BUY`` or ``SELL``, ``size`` is ``total_size``,
``price`` is the optional ``limit_price``, ``duration_sec`` is 300..=86400.
``margin_mode`` is ``cross`` or ``isolated`` (``POST /v2/marginMode``).
``client_order_id`` is an addition for ``cancel`` only (its body needs it).
The on-chain ``PlanProposal`` (milestone 1) is untouched; the Strike lane has
its own ``StrikePlanProposal`` with ``lane: Literal["strike"]``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from ..plan.models import CheckReport

DecStr = Annotated[StrictStr, Field(pattern=r"^-?\d+(\.\d+)?$")]
# A served grid bot id: it is interpolated into DELETE /v2/grid/bot/{bot_id},
# so it may contain no path metacharacters (mirrors struck_core
# grid_bot_id_ok). Vault bots serve `grid-vault_<vault_id>-…`, so `_` is a
# served character (captured live on mainnet 2026-10-22).
_GRID_BOT_ID = re.compile(r"^grid-[A-Za-z0-9_-]{1,195}$")
OrderType = Literal[
    "limit",
    "market",
    "stop",
    "stop_limit",
    "take_profit",
    "take_profit_limit",
    "trailing_stop_market",
]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class ExitLeg(_Strict):
    """A bracket TP or SL leg (CreateStrategyOrderRequest ``tp_order``/``sl_order``)."""

    type: Literal["take_profit", "take_profit_limit", "stop", "stop_limit"]
    stop_price: DecStr
    price: DecStr | None = None
    time_in_force: Literal["GTC", "IOC", "FOK"] | None = None
    working_type: Literal["mark_price", "contract_price"] | None = None
    post_only: StrictBool = False
    price_protect: StrictBool = False


class OrderBody(_Strict):
    """One CreateOrderRequest inside ``POST /v2/orders/batch`` or a replace batch."""

    symbol: StrictStr
    side: Literal["buy", "sell"]
    type: OrderType
    size: DecStr
    price: DecStr | None = None
    stop_price: DecStr | None = None
    callback_rate: DecStr | None = None
    activation_price: DecStr | None = None
    time_in_force: Literal["GTC", "IOC", "FOK"] | None = None
    post_only: StrictBool = False
    reduce_only: StrictBool = False
    leverage: StrictInt | None = None
    working_type: Literal["mark_price", "contract_price"] | None = None
    close_position: StrictBool = False
    price_protect: StrictBool = False
    slippage: DecStr | None = None


class SymbolSetting(_Strict):
    """One market inside copy ``margin_settings.symbol_settings``."""

    symbol: StrictStr
    margin_mode: Literal["cross", "isolated"]
    leverage: StrictInt


class Replacement(_Strict):
    """``POST /v2/order/replace-batch`` item: cancel this served order, create ``new_order``."""

    order_id: StrictStr
    symbol: StrictStr
    new_order: OrderBody


_VENUE_KINDS = frozenset(
    {
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
    }
)


class StrikeLeg(_Strict):
    kind: Literal[
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
    ]
    symbol: StrictStr
    side: Literal["buy", "sell", "BUY", "SELL"] | None
    type: OrderType | None
    size: DecStr | None
    price: DecStr | None
    stop_price: DecStr | None
    callback_rate: DecStr | None
    activation_price: DecStr | None
    time_in_force: Literal["GTC", "IOC", "FOK"] | None
    post_only: StrictBool
    reduce_only: StrictBool
    tp: ExitLeg | None
    sl: ExitLeg | None
    leverage: StrictInt | None
    client_order_id: StrictStr | None
    duration_sec: StrictInt | None = None
    randomize: StrictBool = False
    margin_mode: Literal["cross", "isolated"] | None = None
    grid_mode: Literal["LONG", "SHORT", "NEUTRAL"] | None = None
    initialization_mode: Literal["LIMIT_ONLY", "MARKET_SEEDED"] | None = None
    lower_price: DecStr | None = None
    upper_price: DecStr | None = None
    grid_count: StrictInt | None = None
    collateral: DecStr | None = None
    sl_price: DecStr | None = None
    long_sl_price: DecStr | None = None
    short_sl_price: DecStr | None = None
    bot_id: StrictStr | None = None
    cancel_orders: StrictBool = False
    working_type: Literal["mark_price", "contract_price"] | None = None
    close_position: StrictBool = False
    price_protect: StrictBool = False
    slippage: DecStr | None = None
    vault_id: StrictStr | None = None
    amount: DecStr | None = None
    modify_type: Literal["add", "remove"] | None = None
    strategy_id: StrictStr | None = None
    order_id: StrictStr | None = None
    orders: tuple[OrderBody, ...] | None = None
    replacements: tuple[Replacement, ...] | None = None
    lead_account_id: StrictStr | None = None
    platform: Literal["strike", "hyperliquid"] | None = None
    copy_mode: Literal["fixed_ratio", "fixed_amount"] | None = None
    copy_amount: DecStr | None = None
    margin_per_entry_order: DecStr | None = None
    ratio_multiplier: DecStr | None = None
    minimum_entry: DecStr | None = None
    max_margin_per_symbol: DecStr | None = None
    max_margin_total: DecStr | None = None
    excluded_symbols: tuple[StrictStr, ...] = ()
    stop_loss_amount: DecStr | None = None
    subscription_id: StrictStr | None = None
    sub_account_id: StrictStr | None = None
    copy_existing_positions: StrictBool = False
    auto_create_sub_account: StrictBool = False
    request_id: StrictStr | None = None
    source_account_id: StrictStr | None = None
    funding_amount: DecStr | None = None
    margin_mode_source: Literal["account", "lead"] | None = None
    leverage_source: Literal["account", "lead"] | None = None
    max_leverage: StrictInt | None = None
    settings_margin_mode: Literal["cross", "isolated"] | None = None
    settings_leverage: StrictInt | None = None
    symbol_settings: tuple[SymbolSetting, ...] = ()
    client_rfq_id: StrictStr | None = None
    requested_size: DecStr | None = None
    rfq_id: StrictStr | None = None
    response_status: Literal["acked", "quoted", "uninterested"] | None = None
    bid_size: DecStr | None = None
    ask_size: DecStr | None = None
    blockchain: Literal["ethereum", "solana", "cardano"] | None = None
    usd_amount: DecStr | None = None
    asset: StrictStr | None = None
    withdraw_id: StrictStr | None = None
    wallet_signature: StrictStr | None = None
    shares: DecStr | None = None
    withdraw_all: StrictBool = False
    vault_name: StrictStr | None = None
    description: StrictStr | None = None
    initial_deposit: DecStr | None = None
    leader_commission_bps: StrictInt | None = None
    lockup_days: StrictInt | None = None
    min_deposit: DecStr | None = None
    allow_proportional_close: StrictBool = False
    withdrawal_id: StrictStr | None = None

    @model_validator(mode="after")
    def _kind_fields(self) -> "StrikeLeg":
        kind = self.kind
        if self.orders is not None and kind != "batch":
            raise ValueError(f"{kind}: orders belong to kind batch")
        if self.replacements is not None and kind != "replace_batch":
            raise ValueError(f"{kind}: replacements belong to kind replace_batch")
        if self.strategy_id is not None and kind != "twap_cancel":
            raise ValueError(f"{kind}: strategy_id belongs to kind twap_cancel")
        if self.order_id is not None and kind != "replace":
            raise ValueError(f"{kind}: order_id belongs to kind replace")
        if (self.amount is not None or self.modify_type is not None) and kind != "iso_margin":
            raise ValueError(f"{kind}: amount and modify_type belong to kind iso_margin")
        order_extras = (
            self.working_type is not None
            or self.close_position
            or self.price_protect
            or self.slippage is not None
        )
        if order_extras and kind not in ("order", "bracket", "replace"):
            raise ValueError(
                f"{kind}: working_type, close_position, price_protect and slippage belong to an order"
            )
        if self.vault_id is not None and kind not in (
            "order",
            "bracket",
            "replace",
            "cancel",
            "cancel_all",
            "leverage",
            "margin_mode",
            "iso_margin",
            "batch",
            "replace_batch",
            "rfq",
            "rfq_close",
            "vault_deposit",
            "vault_withdraw",
            "vault_withdraw_cancel",
            "vault_pause",
            "vault_resume",
        ):
            raise ValueError(f"{kind}: vault_id is not a field of this route")
        if self.kind == "twap":
            if self.side not in ("BUY", "SELL"):
                raise ValueError("twap: side is BUY or SELL")
            if self.size is None:
                raise ValueError("twap: size is required")
            if self.duration_sec is None or not 300 <= self.duration_sec <= 86_400:
                raise ValueError("twap: duration_sec must be 300..=86400")
            if (
                self.type is not None
                or self.stop_price is not None
                or self.callback_rate is not None
                or self.activation_price is not None
                or self.time_in_force is not None
                or self.post_only
                or self.tp is not None
                or self.sl is not None
                or self.leverage is not None
                or self.client_order_id is not None
            ):
                raise ValueError(
                    "twap: only symbol, side, size, duration_sec, price, reduce_only, randomize"
                )
        elif self.kind == "margin_mode":
            if self.margin_mode not in ("cross", "isolated"):
                raise ValueError("margin_mode: marginMode is cross or isolated")
            if (
                self.side is not None
                or self.type is not None
                or self.size is not None
                or self.price is not None
                or self.leverage is not None
                or self.client_order_id is not None
                or self.duration_sec is not None
                or self.randomize
                or self.post_only
                or self.reduce_only
            ):
                raise ValueError("margin_mode: exactly symbol + margin_mode")
        elif self.kind == "grid_bot":
            if self.grid_mode is None:
                raise ValueError("grid_bot: grid_mode is required")
            if self.initialization_mode == "MARKET_SEEDED" and self.grid_mode == "NEUTRAL":
                raise ValueError(
                    "grid_bot: initialization_mode MARKET_SEEDED is only supported for LONG or SHORT"
                )
            if self.grid_mode == "NEUTRAL" and self.sl_price is not None:
                raise ValueError(
                    "grid_bot: sl_price is not supported for NEUTRAL mode; "
                    "use long_sl_price and/or short_sl_price"
                )
            if self.grid_mode != "NEUTRAL" and (
                self.long_sl_price is not None or self.short_sl_price is not None
            ):
                raise ValueError(
                    "grid_bot: long_sl_price and short_sl_price are only supported for NEUTRAL mode"
                )
            if (
                self.lower_price is None
                or self.upper_price is None
                or self.grid_count is None
                or not 2 <= self.grid_count <= 200
                or self.collateral is None
            ):
                raise ValueError(
                    "grid_bot: lower_price, upper_price, grid_count (2..=200) and collateral are required"
                )
            if (
                self.side is not None
                or self.type is not None
                or self.size is not None
                or self.price is not None
                or self.stop_price is not None
                or self.callback_rate is not None
                or self.activation_price is not None
                or self.time_in_force is not None
                or self.post_only
                or self.reduce_only
                or self.tp is not None
                or self.sl is not None
                or self.leverage is not None
                or self.client_order_id is not None
                or self.duration_sec is not None
                or self.randomize
                or self.margin_mode is not None
                or self.bot_id is not None
                or self.cancel_orders
            ):
                raise ValueError("grid_bot: only the grid fields (see strike-grid-bot skill)")
        elif self.kind == "grid_stop":
            if self.bot_id is None or not _GRID_BOT_ID.fullmatch(self.bot_id):
                raise ValueError(
                    "grid_stop: bot_id must be a served id (grid-..., letters, digits, '-' and '_')"
                )
            if (
                self.side is not None
                or self.type is not None
                or self.size is not None
                or self.price is not None
                or self.stop_price is not None
                or self.callback_rate is not None
                or self.activation_price is not None
                or self.time_in_force is not None
                or self.post_only
                or self.reduce_only
                or self.tp is not None
                or self.sl is not None
                or self.leverage is not None
                or self.client_order_id is not None
                or self.duration_sec is not None
                or self.randomize
                or self.margin_mode is not None
                or self.grid_mode is not None
                or self.lower_price is not None
                or self.upper_price is not None
                or self.grid_count is not None
                or self.collateral is not None
                or self.sl_price is not None
                or self.long_sl_price is not None
                or self.short_sl_price is not None
            ):
                raise ValueError("grid_stop: exactly bot_id + cancel_orders")
        elif self.kind == "cancel_all":
            if self.side is not None or self.type is not None or self.size is not None or self.leverage is not None:
                raise ValueError("cancel_all: exactly symbol, optional vault_id")
            if not self.symbol:
                raise ValueError("cancel_all: symbol is required")
        elif self.kind == "iso_margin":
            if self.amount is None or self.modify_type not in ("add", "remove"):
                raise ValueError("iso_margin: amount and modify_type add|remove are required")
            if self.side is not None or self.type is not None or self.size is not None:
                raise ValueError("iso_margin: exactly symbol, amount, modify_type, optional vault_id")
        elif self.kind == "twap_cancel":
            if not self.strategy_id or self.symbol:
                raise ValueError("twap_cancel: exactly strategy_id (symbol is empty)")
        elif self.kind == "batch":
            if not self.orders:
                raise ValueError("batch: orders must contain at least one order")
            if self.side is not None or self.type is not None or self.size is not None:
                raise ValueError("batch: exactly orders, optional vault_id")
        elif self.kind == "replace_batch":
            if not self.replacements:
                raise ValueError("replace_batch: replacements must contain at least one item")
            for item in self.replacements:
                if item.symbol != item.new_order.symbol:
                    raise ValueError("replace_batch: cancel symbol must equal new_order.symbol")
            if self.side is not None or self.type is not None or self.size is not None:
                raise ValueError("replace_batch: exactly replacements, optional vault_id")
        elif self.kind == "replace":
            if not self.order_id:
                raise ValueError("replace: order_id is required")
            if self.side not in ("buy", "sell") or self.type is None or self.size is None:
                raise ValueError("replace: order_id plus the new order (side, type, size)")
            if self.tp is not None or self.sl is not None:
                raise ValueError("replace: tp/sl belong to kind bracket")
        elif self.kind in _VENUE_KINDS:
            if self.kind == "rfq":
                if not self.symbol:
                    raise ValueError("rfq: symbol is required")
            elif self.symbol:
                raise ValueError(f"{self.kind}: symbol is empty; this route has no market symbol")
            if (self.kind != "rfq" and self.side is not None) or self.type is not None or self.size is not None:
                raise ValueError(f"{self.kind}: order fields do not belong on this route")
            if (
                self.price is not None
                or self.stop_price is not None
                or self.leverage is not None
                or self.duration_sec is not None
                or self.orders is not None
                or self.replacements is not None
                or self.margin_mode is not None
                or self.grid_mode is not None
                or self.bot_id is not None
            ):
                raise ValueError(f"{self.kind}: order fields do not belong on this route")
        elif (
            self.margin_mode is not None
            or self.grid_mode is not None
            or self.initialization_mode is not None
            or self.lower_price is not None
            or self.upper_price is not None
            or self.grid_count is not None
            or self.collateral is not None
            or self.sl_price is not None
            or self.long_sl_price is not None
            or self.short_sl_price is not None
            or self.bot_id is not None
            or self.cancel_orders
        ):
            raise ValueError(f"{self.kind}: grid fields belong to kinds grid_bot/grid_stop")
        elif self.margin_mode is not None:
            raise ValueError(f"{self.kind}: margin_mode belongs to kind margin_mode")
        elif self.duration_sec is not None or self.randomize:
            raise ValueError(f"{self.kind}: duration_sec and randomize belong to twap")
        elif self.side in ("BUY", "SELL"):
            raise ValueError(f"{self.kind}: side is buy or sell")
        return self

class StrikePlanProposal(_Strict):
    lane: Literal["strike"]
    intent: StrictStr
    legs: tuple[StrikeLeg, ...] = Field(min_length=1)
    rationale: StrictStr


class ApprovedStrikePlan(_Strict):
    """The exact plan the human approves; its hash seeds every client id."""

    network: StrictStr  # the SELECTED network's strike_venue (struck.network.PROFILES); checked before send
    legs: tuple[StrikeLeg, ...]
    markets_snapshot_sha256: StrictStr

    @property
    def canonical_json(self) -> str:
        return self.model_dump_json()

    @property
    def plan_hash(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()

    def client_order_id(self, index: int) -> str:
        """Deterministic, <=256 chars, no reserved prefix (strike-trading :98)."""
        return f"struck-{self.plan_hash[:24]}-{index}"


class LegValidation(_Strict):
    report: CheckReport
    not_evaluated: tuple[StrictStr, ...]


class StrikeApprovalRequest(_Strict):
    plan_hash: StrictStr
    plan: ApprovedStrikePlan
    validations: tuple[LegValidation, ...]
    summary: tuple[StrictStr, ...]


class ApiRequest(_Strict):
    """N4s -> N5s: the exact method, path and body bytes the signature would cover."""

    method: Literal["POST", "DELETE", "PATCH", "PUT"]
    path: StrictStr
    body: StrictStr
    client_order_id: StrictStr | None


class RequestCongruence(_Strict):
    report: CheckReport
    fresh_validation: LegValidation


class RequestExplanation(_Strict):
    lines: tuple[StrictStr, ...]


class PreparedRequest(_Strict):
    request: ApiRequest
    congruence: RequestCongruence
    explanation: RequestExplanation


class StrikeSignRequest(_Strict):
    """N7s: what the human confirms before the API wallet sends (N8s). Strike
    authorizes orders by the API-wallet request signature alone (no signData)."""

    plan_hash: StrictStr
    requests: tuple[ApiRequest, ...]
    explanation_sha256: StrictStr
    receipts_dir: StrictStr


class StrikeConfirm(_Strict):
    """Resume schema of the N7s interrupt: the HUMAN's send/abort for one plan hash."""

    decision: Literal["send", "abort"]
    plan_hash: StrictStr
    approver: StrictStr
    ts_ms: StrictInt


class SentLeg(_Strict):
    """N8s: one leg as sent and what Strike answered (status + body hash).
    ``outcome`` "unknown" = the transport failed after the request may have
    left (timeout / reset): no status, no body; the client order id is put on
    the unresolved ledger and refused until a human resyncs."""

    leg: StrictInt
    method: StrictStr
    path: StrictStr
    client_order_id: StrictStr | None  # None: leverage / cancel requests carry none
    outcome: Literal["served", "unknown"]
    status: StrictInt | None
    response_sha256: StrictStr | None
    error: StrictStr | None
