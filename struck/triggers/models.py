"""Trigger records as DATA (port of 
``validate_condition`` :214-381, ``validate_plan`` :397-552, ``TriggerStore``
record :596-611, ).

The 
Pydantic model (a wrong shape raises, never repaired). Type changes the spec
§5 requires: money is integer lovelace / token base units or ``DecStr``
(``min_ada`` -> ``min_lovelace``, float thresholds -> ints or decimal
strings, ``amount_ada`` -> integer ``lovelace``).

Ported condition kinds: ``price`` (Strike served mark|index|last, spec §5.1
EXTEND), ``time_window``, ``schedule``, ``interval``, ``composite``
(and|or|xor), ``not``, ``incoming_payment`` (sender filter checked on the
LOCAL Kupo index), ``wallet_balance``. NEW (spec §5.1 extension, flagged in
the report): ``strike_position`` on SERVED ``upnl`` or the Rust ROE from
served values; ``strike_deposit`` (spec NEW: a completed served deposit row);
``indicator`` (spec EXISTS-in-
indicator math is ANALYSIS — the spec explicitly allows floats there, they
never touch money); ``strike_balance`` (spec EXTEND: an exact field selector
on the served ``GET /v2/balances`` row: walletBalance, availableBalance,
marginBalance, maximalWithdrawAmount...); ``strike_realized_pnl`` (spec NEW:
the served ``realized_pnl`` of the newest fill, or the served
``net_realized_pnl`` of the newest position history row, inside a lookback
window in minutes; one SERVED value is compared in Rust — a multi-fill SUM
would need a Rust decimal-sum export, proposed in the E03 report).

``sequence`` is a condition: ordered steps, each with its own condition,
actions and timeout. The store keeps the step index. A fire writes only the
current step's proposal. ``strike_deposit`` as a condition matches a
completed ``GET /v2/history/transaction`` deposit row (strike-history). An
``ACCOUNT_UPDATE`` with ``data.e`` and ``r`` of ``DEPOSIT`` is the stream
credit (strike-userstream); ``DEPOSIT_PENDING`` is not a match.

First-match branches (spec §5.2 EXISTS-in-
:366): a trigger with ``branches`` evaluates each branch condition in order;
the FIRST branch that fires proposes exactly its own actions; when none
matches, ``else_actions`` propose; with neither, the tick simply does not
fire. A branch trigger therefore carries NO top-level ``actions``.

Actions: ``wallet_send`` (fixed integer lovelace / multi-recipient /
multi-asset ``Asset``, the whole ``incoming`` amount, ``pct_of_incoming``, or
``pct_of_balance`` — percent math in Rust) -> on-chain ``PlanProposal``;
``strike_order`` (a full ``StrikeLeg``, including ``twap``, ``grid_bot``,
``cancel``, ``leverage``, ``margin_mode``) -> ``StrikePlanProposal``;
``strike_deposit`` -> a deposit proposal. A fire writes that proposal. It
does not quote, build, sign, or confirm.
"""

from __future__ import annotations

from typing import Annotated, Literal

import struck_py
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from ..chain.models import Asset
from ..strike.market import KLINE_INTERVALS
from ..strike.models import DecStr, StrikeLeg

Op = Literal["gt", "lt", "gte", "lte"]
HHMM = Annotated[StrictStr, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
IsoUtc = Annotated[
    StrictStr, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|\+00:00)$")
]
Hex56 = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{56}$")]
Unit = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{56}\.[0-9a-f]{0,64}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class PriceCondition(_Strict):
    kind: Literal["price"]
    symbol: StrictStr
    price_type: Literal["mark", "index", "last"]
    op: Op
    threshold: DecStr


class TimeWindowCondition(_Strict):
    kind: Literal["time_window"]
    start_utc: HHMM
    end_utc: HHMM
    weekdays: tuple[Annotated[StrictInt, Field(ge=0, le=6)], ...] = Field(min_length=1)


class ScheduleCondition(_Strict):
    kind: Literal["schedule"]
    at_utc: IsoUtc


class IntervalCondition(_Strict):
    kind: Literal["interval"]
    every_minutes: StrictInt = Field(gt=0)
    start_utc: IsoUtc | None


class IncomingPaymentCondition(_Strict):
    kind: Literal["incoming_payment"]
    address: StrictStr
    sender: StrictStr | None = Field(alias="from")
    min_lovelace: StrictInt = Field(ge=0)
    token: Unit | None
    policy: Hex56 | None
    min_token_qty: StrictInt | None = Field(ge=1)

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, populate_by_name=True)


class WalletBalanceCondition(_Strict):
    kind: Literal["wallet_balance"]
    addresses: tuple[StrictStr, ...] = Field(min_length=1)
    aggregate: Literal["sum", "each"]
    symbol: Literal["ada"] | Unit
    op: Op
    threshold: StrictInt


class StrikeDepositCondition(_Strict):
    """A deposit Strike has already credited.

    Matches ``GET /v2/history/transaction`` rows with ``type=deposit`` and
    ``status=completed`` (strike-history). ``min_amount`` compares to the
    served ``amount`` decimal string. The user-stream credit is
    ``ACCOUNT_UPDATE`` with ``data.e`` and ``r`` of ``DEPOSIT``
    (strike-userstream). ``DEPOSIT_PENDING`` is not this condition.
    """

    kind: Literal["strike_deposit"]
    asset: StrictStr
    min_amount: DecStr


class StrikePositionCondition(_Strict):
    kind: Literal["strike_position"]
    symbol: StrictStr
    field: Literal[
        "upnl", "liquidation_price", "bankruptcy_price", "maintenance_margin"
    ]  # /v2/positions, as served
    op: Op
    threshold: DecStr


class IndicatorCondition(_Strict):
    """A price-feed indicator over the served ``/price/v2/klines`` (spec §5.1).

    The indicator VALUE and THRESHOLD are analysis floats (spec: floats inside
    indicator math are display/analysis, not money) — no lovelace, no order
    size ever flows through them. ``candle`` fields are Strike's exact
    ``KLINE_INTERVALS`` request variant; the served close strings are read
    each evaluation, never cached."""

    kind: Literal["indicator"]
    symbol: StrictStr
    interval: StrictStr  # one of strike.market.KLINE_INTERVALS (checked below)
    limit: StrictInt = Field(gt=1, le=1000)
    indicator: Literal["rsi", "ema_cross", "momentum", "volatility"]
    period: StrictInt | None = Field(default=None, ge=2)  # rsi, default 14
    fast: StrictInt | None = Field(default=None, ge=1)  # ema_cross fast, default 9
    slow: StrictInt | None = Field(default=None, ge=1)  # ema_cross slow, default 21
    bars: StrictInt | None = Field(default=None, ge=1)  # momentum/volatility, default 24
    op: Op
    threshold: StrictFloat

    @model_validator(mode="after")
    def _interval_and_params(self) -> "IndicatorCondition":
        if self.interval not in KLINE_INTERVALS:
            raise ValueError(
                f"interval {self.interval!r} is not a served /price/v2/klines variant "
                f"({', '.join(KLINE_INTERVALS)})"
            )
        if self.indicator == "ema_cross":
            if self.period is not None:
                raise ValueError("ema_cross takes fast/slow, not period")
            if self.bars is not None:
                raise ValueError("ema_cross takes fast/slow, not bars")
            if (self.fast is None) != (self.slow is None):
                raise ValueError("ema_cross needs fast AND slow together")
        elif self.indicator in ("momentum", "volatility"):
            if self.period is not None:
                raise ValueError(f"{self.indicator} takes bars, not period")
            if self.fast is not None or self.slow is not None:
                raise ValueError(f"{self.indicator} takes bars, not fast/slow")
        return self


class StrikeBalanceCondition(_Strict):
    """An exact field on the served ``GET /v2/balances`` row of one asset
    (spec §5.1 EXTEND: field selector instead of a guessed field or float).
    The served decimal string is compared in Rust."""

    kind: Literal["strike_balance"]
    asset: StrictStr
    field: Literal[
        "walletBalance",
        "unrealizedPnl",
        "marginBalance",
        "maintMargin",
        "initialMargin",
        "positionInitialMargin",
        "openOrderInitialMargin",
        "crossWalletBalance",
        "crossUnPnl",
        "availableBalance",
        "maxWithdrawAmount",
    ]
    op: Op
    threshold: DecStr


class StrikeRealizedPnlCondition(_Strict):
    """The SERVED PnL of the newest realized event inside a lookback window:
    ``fills`` -> the newest fill's served ``realized_pnl`` within the window;
    ``position_history`` -> the newest position row's served
    ``net_realized_pnl`` (spec §5.1 NEW). One served value, compared in
    Rust; the newest row in the window decides. No row in the window does
    not fire (fail closed with data_ok, like every empty-data kind)."""

    kind: Literal["strike_realized_pnl"]
    source: Literal["fills", "position_history"]
    symbol: StrictStr  # "" = any symbol
    window_minutes: StrictInt = Field(gt=0)
    op: Op
    threshold: DecStr


class CompositeCondition(_Strict):
    kind: Literal["composite"]
    logic: Literal["and", "or", "xor"]
    children: tuple[Condition, ...] = Field(min_length=1)


class NotCondition(_Strict):
    kind: Literal["not"]
    child: Condition


class SequenceStep(_Strict):
    """One step of a sequence: wait for ``condition``, then propose ``actions``."""

    condition: Condition
    actions: tuple[Action, ...] = Field(min_length=1)
    timeout_s: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def _flat(self) -> "SequenceStep":
        if self.condition.kind == "sequence":
            raise ValueError("sequence steps cannot nest another sequence")
        return self


class SequenceCondition(_Strict):
    """Event A, then later event B, then that step's action. Not one shot."""

    kind: Literal["sequence"]
    steps: tuple[SequenceStep, ...] = Field(min_length=2)


Condition = Annotated[
    PriceCondition
    | TimeWindowCondition
    | ScheduleCondition
    | IntervalCondition
    | IncomingPaymentCondition
    | WalletBalanceCondition
    | StrikeDepositCondition
    | StrikePositionCondition
    | IndicatorCondition
    | StrikeBalanceCondition
    | StrikeRealizedPnlCondition
    | CompositeCondition
    | NotCondition
    | SequenceCondition,
    Field(discriminator="kind"),
]


class SendTo(_Strict):
    address: StrictStr
    lovelace: StrictInt | Literal["incoming"] | None = None
    pct_of_incoming: DecStr | None = None
    pct_of_balance: DecStr | None = None
    assets: tuple[Asset, ...] = ()

    @model_validator(mode="after")
    def _one_amount(self) -> "SendTo":
        amounts = (
            self.lovelace is not None,
            self.pct_of_incoming is not None,
            self.pct_of_balance is not None,
        )
        if sum(amounts) != 1:
            raise ValueError(
                "send output needs exactly one of lovelace, pct_of_incoming, pct_of_balance"
            )
        if self.lovelace == "incoming" and self.assets:
            raise ValueError(
                "forwarding the whole incoming payment forwards its lovelace only; "
                "name explicit assets on a fixed-lovelace output"
            )
        return self


class WalletSendAction(_Strict):
    kind: Literal["wallet_send"]
    from_address: StrictStr
    outputs: tuple[SendTo, ...] = Field(min_length=1)
    max_fee_lovelace: StrictInt = Field(gt=0)
    validity_minutes: StrictInt = Field(gt=0)


class StrikeOrderAction(_Strict):
    kind: Literal["strike_order"]
    leg: StrikeLeg


class StrikeDepositAction(_Strict):
    """A deposit the human still has to quote, sign, and confirm.

    Exactly one of ``asset_amount`` (smallest unit, a positive integer
    string) or ``pct_of_incoming``. Rust turns the percent into a whole
    quantity when the payment fires. This action does not call Strike.
    """

    kind: Literal["strike_deposit"]
    blockchain: Literal["cardano", "ethereum", "solana"]
    asset_symbol: StrictStr
    asset_amount: StrictStr | None = None
    pct_of_incoming: DecStr | None = None
    user_address: StrictStr
    mode: Literal["balance", "collateral"] | None = None

    @model_validator(mode="after")
    def _amount(self) -> "StrikeDepositAction":
        if not self.asset_symbol or not self.user_address:
            raise ValueError("strike_deposit: asset_symbol and user_address are required")
        if (self.asset_amount is None) == (self.pct_of_incoming is None):
            raise ValueError("strike_deposit needs asset_amount or pct_of_incoming, not both")
        if self.asset_amount is not None and not struck_py.compare_quantities(
            self.asset_amount, "gt", "0"
        ):
            raise ValueError("strike_deposit: asset_amount must be a positive integer")
        if self.pct_of_incoming is not None and not (
            struck_py.decimal_compare(self.pct_of_incoming, "gt", "0")
            and struck_py.decimal_compare(self.pct_of_incoming, "lte", "100")
        ):
            raise ValueError("pct_of_incoming must be > 0 and <= 100")
        return self


Action = Annotated[
    WalletSendAction | StrikeOrderAction | StrikeDepositAction, Field(discriminator="kind")
]


class TriggerSpec(_Strict):
    """What the operator authors (UI, CLI): a condition tree + ordered actions,
    OR a first-match branch chain (spec §5.2). Exactly one of the two."""

    label: StrictStr = Field(min_length=1)
    condition: Condition
    actions: tuple[Action, ...] = ()
    branches: tuple[Branch, ...] = ()
    else_actions: tuple[Action, ...] = ()
    rearm: StrictBool
    cooldown_s: StrictInt = Field(ge=0)
    policy_id: StrictStr | None

    @model_validator(mode="after")
    def _where_actions_live(self) -> "TriggerSpec":
        sequenced = self.condition.kind == "sequence"
        if sequenced and self.actions:
            raise ValueError("a sequence keeps its actions on each step")
        if not sequenced and not self.actions and not self.branches:
            raise ValueError("trigger needs actions or first-match branches")
        if self.branches and self.actions:
            raise ValueError(
                "a branch trigger is decided by its branches; top-level actions "
                "would also fire — remove them"
            )
        if self.else_actions and not self.branches:
            raise ValueError("else_actions belong to a branch trigger")
        if sequenced and self.branches:
            raise ValueError("branches do not decide a sequence trigger")
        return self


class Branch(_Strict):
    """One if-branch of a first-match chain (spec §5.2): its condition, only
    its actions. Evaluated in order; the FIRST matching branch fires."""

    label: StrictStr = Field(min_length=1)
    condition: Condition
    actions: tuple[Action, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_sequence_inside(self) -> "Branch":
        if self.condition.kind == "sequence":
            raise ValueError("a branch condition cannot be a sequence")
        return self


class FireContext(_Strict):
    """What an ``incoming_payment`` saw ()."""

    address: StrictStr
    tx: StrictStr
    slot: StrictInt
    sender: StrictStr | None
    lovelace: StrictInt
    asset: StrictStr
    qty: StrictInt


class FireRecord(_Strict):
    ts: IsoUtc
    outcome: Literal["proposed", "error", "retracted"]
    detail: StrictStr
    proposals: tuple[StrictStr, ...]
    approval_route: StrictStr


class TriggerRecord(_Strict):
    id: StrictStr
    spec: TriggerSpec
    status: Literal["active", "paused", "fired"]
    created_at: IsoUtc
    updated_at: IsoUtc
    last_evaluated: IsoUtc | None
    last_result: StrictStr | None
    last_payment_slot: StrictInt | None
    sequence_step: StrictInt = 0
    sequence_step_since: IsoUtc | None = None
    fire_context: FireContext | None = None
    fire_history: tuple[FireRecord, ...]


CompositeCondition.model_rebuild()
NotCondition.model_rebuild()
SequenceStep.model_rebuild()
SequenceCondition.model_rebuild()
TriggerSpec.model_rebuild()
