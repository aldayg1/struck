"""Strike user history and public account reads, as served.

Docs: https://docs.strikefinance.org/api/user/rest-api/history
and https://docs.strikefinance.org/api/user/rest-api/account.

Signed reads go through ``StrikeAuthedClient.request`` (the query is part of
the signed path). A public selector (``account_id``, ``vault_id``, or
``blockchain_address``) is unsigned. Amounts stay the served strings.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal, Protocol

import httpx
from pydantic import BaseModel, BeforeValidator, ConfigDict, StrictInt, StrictStr

import struck_py

from ..callhistory import RecordedHttp, Served
from .account import Account, Balances
from .market import StrikeMarketClient, source_for

Chain = Literal["cardano", "ethereum", "solana"]
_CHAINS: tuple[Chain, ...] = ("cardano", "ethereum", "solana")


class _Signed(Protocol):
    def request(self, method: str, path_with_query: str, body: bytes | None = None) -> Served: ...


class _Served(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", frozen=True)


def _none_is_empty(value: object) -> object:
    """Skill strike-history: an empty history list is JSON null, not []."""
    if value is None:
        return []
    return value


def _nonneg(name: str, value: int | None) -> str | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return str(value)


def _limit(value: int | None, cap: int) -> str | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= cap:
        raise ValueError(f"limit must be an integer 1..{cap}")
    return str(value)


def path_with_query(path: str, params: Mapping[str, str]) -> str:
    if not params:
        return path
    return f"{path}?{httpx.QueryParams(params)}"


def account_selector(
    *,
    account_id: str | None = None,
    vault_id: str | None = None,
    blockchain: str | None = None,
    blockchain_address: str | None = None,
    sub_account_id: str | None = None,
    public: bool,
) -> dict[str, str]:
    """One account selector. Public reads cannot use ``sub_account_id``."""
    if blockchain is not None and blockchain not in _CHAINS:
        raise ValueError(f"blockchain must be one of {', '.join(_CHAINS)}")
    if blockchain is not None and not blockchain_address:
        raise ValueError("blockchain requires blockchain_address")
    params: dict[str, str] = {}
    if account_id:
        params["account_id"] = account_id
    if vault_id:
        params["vault_id"] = vault_id
    if blockchain_address:
        params["blockchain_address"] = blockchain_address
    if blockchain:
        params["blockchain"] = blockchain
    families = [name for name in ("account_id", "vault_id", "blockchain_address") if name in params]
    if len(families) > 1:
        raise ValueError("pass one of account_id, vault_id, or blockchain_address")
    if public and sub_account_id:
        raise ValueError("sub_account_id requires the API wallet")
    if sub_account_id:
        params["sub_account_id"] = sub_account_id
    if public and not families:
        raise ValueError(
            "public Strike read needs account_id, vault_id, or blockchain_address"
        )
    return params


class OrderRow(_Served):
    id: StrictInt
    client_order_id: StrictStr
    account_id: StrictStr
    symbol: StrictStr
    strategy_id: StrictStr | None
    is_primary: bool | None
    close_reason: StrictStr | None
    side: Literal["buy", "sell"]
    status: Literal["pending", "open", "filled", "canceled", "untriggered", "rejected", "expired"]
    type: Literal[
        "market",
        "limit",
        "stop",
        "stop_limit",
        "take_profit",
        "take_profit_limit",
        "trailing_stop_market",
    ]
    origin_type: Literal[
        "",
        "market",
        "limit",
        "stop",
        "stop_limit",
        "take_profit",
        "take_profit_limit",
        "trailing_stop_market",
    ] | None
    auto_close_type: Literal[
        "",
        "liquidation",
        "adl",
        "bankrupt",
        "if_transfer",
        "vault_withdrawal",
        "slp_transfer",
    ] | None
    time_in_force: Literal["GTC", "FOK", "IOC"] | None
    working_type: Literal["none", "contract_price", "mark_price"] | None
    size: StrictStr
    filled: StrictStr
    price: StrictStr
    stop_price: StrictStr
    post_only: bool
    reduce_only: bool
    close_position: bool
    price_protect: bool
    create_timestamp: StrictInt
    entry_timestamp: StrictInt
    event_timestamp: StrictInt


class OrderHistory(_Served):
    orders: Annotated[list[OrderRow], BeforeValidator(_none_is_empty)]
    count: StrictInt


class FillRow(_Served):
    id: StrictInt
    trade_id: StrictInt
    order_id: StrictInt
    account_id: StrictStr
    symbol: StrictStr
    side: Literal["buy", "sell"]
    role: Literal["maker", "taker", "mixed"]
    price: StrictStr
    size: StrictStr
    realized_pnl: StrictStr
    fee: StrictStr
    fee_type: Literal["none", "commission", "liquidation", "funding", "mixed"]
    timestamp: StrictInt
    auto_close_type: Literal[
        "",
        "liquidation",
        "adl",
        "bankrupt",
        "if_transfer",
        "vault_withdrawal",
        "slp_transfer",
    ] | None


class FillHistory(_Served):
    fills: Annotated[list[FillRow], BeforeValidator(_none_is_empty)]
    count: StrictInt


class PositionLife(_Served):
    """Strike's directional position history, including its completeness signal.

    PnL and ROI are served strings, never computed from fills here. Checkpoints
    may be open; history rows have at least one reduction. Official contract:
    strike-finance-skills@30ba679, user-api.yaml PositionHistory.
    """

    id: StrictStr
    position_id: StrictStr
    account_id: StrictStr
    symbol: StrictStr
    side: Literal["long", "short"]
    status: Literal["open", "partially_closed", "closed"]
    margin_mode: StrictStr
    leverage: StrictInt
    entry_price: StrictStr
    avg_close_price: StrictStr
    realized_pnl: StrictStr
    trading_fees: StrictStr
    funding_pnl: StrictStr
    net_realized_pnl: StrictStr
    roi_pct: StrictStr
    max_held: StrictStr
    closed_size: StrictStr
    remaining_size: StrictStr
    open_timestamp: StrictInt
    close_timestamp: StrictInt | None
    last_close_timestamp: StrictInt
    update_timestamp: StrictInt
    last_fill_id: StrictStr
    last_fill_seq: StrictInt
    revision: StrictInt
    data_complete: bool
    opened_size: StrictStr
    entry_notional: StrictStr
    close_notional: StrictStr
    closing_margin: StrictStr
    funding_baseline: StrictStr


class PositionHistory(_Served):
    positions: Annotated[list[PositionLife], BeforeValidator(_none_is_empty)]
    position_checkpoints: Annotated[list[PositionLife], BeforeValidator(_none_is_empty)]
    count: StrictInt
    has_more: bool
    next_cursor: StrictStr
    account_ids: list[StrictStr]


def _position_params(
    *,
    symbol: str | None,
    side: Literal["long", "short"] | None,
    status: Literal["closed", "partially_closed"] | None,
    start_ms: int | None,
    end_ms: int | None,
    limit: int | None,
    cursor: str | None,
) -> dict[str, str]:
    if start_ms is not None and end_ms is not None and start_ms > end_ms:
        raise ValueError("startTime must not exceed endTime")
    params = _time_params(start_ms, end_ms)
    if symbol is not None:
        params["symbol"] = symbol
    if side is not None:
        params["side"] = side
    if status is not None:
        params["status"] = status
    rendered_limit = _limit(limit, 200)
    if rendered_limit is not None:
        params["limit"] = rendered_limit
    if cursor is not None:
        params["cursor"] = cursor
    return params


def position_history(
    client: _Signed,
    *,
    symbol: str | None = None,
    side: Literal["long", "short"] | None = None,
    status: Literal["closed", "partially_closed"] | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    account_id: str | None = None,
    vault_id: str | None = None,
    blockchain: str | None = None,
    blockchain_address: str | None = None,
    sub_account_id: str | None = None,
) -> tuple[Served, PositionHistory]:
    """Read one page; pass the served opaque next_cursor for subsequent pages."""
    params = account_selector(
        account_id=account_id, vault_id=vault_id, blockchain=blockchain,
        blockchain_address=blockchain_address, sub_account_id=sub_account_id, public=False,
    )
    params.update(_position_params(
        symbol=symbol, side=side, status=status, start_ms=start_ms, end_ms=end_ms,
        limit=limit, cursor=cursor,
    ))
    served = client.request("GET", path_with_query("/v2/history/position", params))
    return served, PositionHistory.model_validate_json(served.content)


class FundingRow(_Served):
    id: StrictInt
    transaction_id: StrictInt
    account_id: StrictStr
    position_id: StrictInt
    symbol: StrictStr
    position_size: StrictStr
    position_side: Literal["Long", "Short"]
    funding_rate: StrictStr
    amount: StrictStr
    timestamp: StrictInt


class FundingHistory(_Served):
    funding: Annotated[list[FundingRow], BeforeValidator(_none_is_empty)]
    count: StrictInt


class TransactionRow(_Served):
    id: StrictInt
    request_id: StrictStr
    account_id: StrictStr
    type: Literal[
        "deposit",
        "withdraw",
        "fee",
        "realized_pnl",
        "liquidation",
        "funding",
        "reward_grant",
        "reward_reclaim",
        "credit",
        "transfer",
        "liquidation_income",
    ]
    amount: StrictStr
    blockchain: StrictStr
    blockchain_address: StrictStr
    tx_hash: StrictStr | None
    recipient_address: StrictStr | None
    status: Literal["pending", "completed", "pending_settlement", "settled", "failed", "cancelled"]
    timestamp: StrictInt
    asset_symbol: StrictStr | None
    asset_amount: StrictStr | None


class TransactionHistory(_Served):
    transactions: Annotated[list[TransactionRow], BeforeValidator(_none_is_empty)]
    count: StrictInt


def completed_deposit(row: TransactionRow, asset: str, min_amount: str) -> bool:
    """A landed deposit from ``GET /v2/history/transaction``.

    ``type`` ``deposit`` and ``status`` ``completed`` (query status ``2``).
    ``asset_symbol`` must be the asset asked for. ``amount`` is the served
    decimal string and is compared in Rust. A null symbol does not match.
    """
    if row.type != "deposit" or row.status != "completed" or row.asset_symbol != asset:
        return False
    return struck_py.decimal_compare(row.amount, "gte", min_amount)


def _time_params(start_ms: int | None, end_ms: int | None) -> dict[str, str]:
    params: dict[str, str] = {}
    start = _nonneg("startTime", start_ms)
    end = _nonneg("endTime", end_ms)
    if start is not None:
        params["startTime"] = start
    if end is not None:
        params["endTime"] = end
    return params


def orders(
    client: _Signed,
    *,
    symbol: str | None = None,
    side: Literal["buy", "sell"] | None = None,
    order_type: str | None = None,
    status: int | None = None,
    order_id: int | None = None,
    source: Literal["twap", "grid", "copy"] | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int | None = None,
    from_order_id: int | None = None,
    account_id: str | None = None,
    vault_id: str | None = None,
    blockchain: str | None = None,
    blockchain_address: str | None = None,
    sub_account_id: str | None = None,
) -> tuple[Served, OrderHistory]:
    params = account_selector(
        account_id=account_id,
        vault_id=vault_id,
        blockchain=blockchain,
        blockchain_address=blockchain_address,
        sub_account_id=sub_account_id,
        public=False,
    )
    if symbol:
        params["symbol"] = symbol
    if side:
        params["side"] = side
    if order_type:
        params["type"] = order_type
    if status is not None:
        rendered = _nonneg("status", status)
        if rendered is None or not 1 <= status <= 7:
            raise ValueError("order status must be an integer 1..7")
        params["status"] = rendered
    rendered_id = _nonneg("order_id", order_id)
    if rendered_id is not None:
        params["order_id"] = rendered_id
    if source:
        params["source"] = source
    params.update(_time_params(start_ms, end_ms))
    rendered_limit = _limit(limit, 1000)
    if rendered_limit is not None:
        params["limit"] = rendered_limit
    cursor = _nonneg("fromOrderID", from_order_id)
    if cursor is not None:
        params["fromOrderID"] = cursor
    served = client.request("GET", path_with_query("/v2/history/order", params))
    return served, OrderHistory.model_validate_json(served.content)


def fills(
    client: _Signed,
    *,
    symbol: str | None = None,
    side: Literal["buy", "sell"] | None = None,
    role: Literal["maker", "taker"] | None = None,
    auto_close_type: str | None = None,
    order_id: int | None = None,
    source: Literal["twap", "grid", "copy"] | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int | None = None,
    from_id: int | None = None,
    since_trade_id: int | None = None,
    account_id: str | None = None,
    vault_id: str | None = None,
    blockchain: str | None = None,
    blockchain_address: str | None = None,
    sub_account_id: str | None = None,
) -> tuple[Served, FillHistory]:
    if from_id is not None and since_trade_id is not None:
        raise ValueError("fromId and since_trade_id cannot be combined")
    params = account_selector(
        account_id=account_id,
        vault_id=vault_id,
        blockchain=blockchain,
        blockchain_address=blockchain_address,
        sub_account_id=sub_account_id,
        public=False,
    )
    if symbol:
        params["symbol"] = symbol
    if side:
        params["side"] = side
    if role:
        params["role"] = role
    if auto_close_type is not None:
        params["auto_close_type"] = auto_close_type
    rendered_id = _nonneg("order_id", order_id)
    if rendered_id is not None:
        params["order_id"] = rendered_id
    if source:
        params["source"] = source
    params.update(_time_params(start_ms, end_ms))
    rendered_limit = _limit(limit, 1000)
    if rendered_limit is not None:
        params["limit"] = rendered_limit
    cursor = _nonneg("fromId", from_id)
    if cursor is not None:
        params["fromId"] = cursor
    forward = _nonneg("since_trade_id", since_trade_id)
    if forward is not None:
        params["since_trade_id"] = forward
    served = client.request("GET", path_with_query("/v2/history/fill", params))
    return served, FillHistory.model_validate_json(served.content)


def funding(
    client: _Signed,
    *,
    symbol: str | None = None,
    position_side: Literal["Long", "Short"] | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int | None = None,
    from_id: int | None = None,
    account_id: str | None = None,
    vault_id: str | None = None,
    blockchain: str | None = None,
    blockchain_address: str | None = None,
    sub_account_id: str | None = None,
) -> tuple[Served, FundingHistory]:
    params = account_selector(
        account_id=account_id,
        vault_id=vault_id,
        blockchain=blockchain,
        blockchain_address=blockchain_address,
        sub_account_id=sub_account_id,
        public=False,
    )
    if symbol:
        params["symbol"] = symbol
    if position_side:
        params["position_side"] = position_side
    params.update(_time_params(start_ms, end_ms))
    rendered_limit = _limit(limit, 1000)
    if rendered_limit is not None:
        params["limit"] = rendered_limit
    cursor = _nonneg("fromId", from_id)
    if cursor is not None:
        params["fromId"] = cursor
    served = client.request("GET", path_with_query("/v2/history/funding", params))
    return served, FundingHistory.model_validate_json(served.content)


def transactions(
    client: _Signed,
    *,
    tx_type: str | None = None,
    status: int | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int | None = None,
    from_id: int | None = None,
    account_id: str | None = None,
    vault_id: str | None = None,
    blockchain: str | None = None,
    blockchain_address: str | None = None,
    sub_account_id: str | None = None,
) -> tuple[Served, TransactionHistory]:
    """``tx_type`` is the documented ``type`` query (names or integers, comma-separated)."""
    params = account_selector(
        account_id=account_id,
        vault_id=vault_id,
        blockchain=blockchain,
        blockchain_address=blockchain_address,
        sub_account_id=sub_account_id,
        public=False,
    )
    if tx_type:
        params["type"] = tx_type
    if status is not None:
        rendered = _nonneg("status", status)
        if rendered is None or not 1 <= status <= 6:
            raise ValueError("transaction status must be an integer 1..6")
        params["status"] = rendered
    params.update(_time_params(start_ms, end_ms))
    rendered_limit = _limit(limit, 1000)
    if rendered_limit is not None:
        params["limit"] = rendered_limit
    cursor = _nonneg("fromId", from_id)
    if cursor is not None:
        params["fromId"] = cursor
    served = client.request("GET", path_with_query("/v2/history/transaction", params))
    return served, TransactionHistory.model_validate_json(served.content)


class StrikePublicReads:
    """Unsigned GETs. A public selector is required (docs: no selector needs auth)."""

    def __init__(self, http: RecordedHttp, base_url: str) -> None:
        market = StrikeMarketClient(http, base_url)
        self.network = market.network
        self._http = http
        self._base = self.network.strike_api_url

    def _get(self, path: str, params: Mapping[str, str]) -> Served:
        if not any(key in params for key in ("account_id", "vault_id", "blockchain_address")):
            raise ValueError(
                "public Strike read needs account_id, vault_id, or blockchain_address"
            )
        return self._http.request(
            source_for(self.network),
            "GET",
            f"{self._base}{path}",
            params=dict(params),
        )

    def position_history(
        self,
        *,
        symbol: str | None = None,
        side: Literal["long", "short"] | None = None,
        status: Literal["closed", "partially_closed"] | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        account_id: str | None = None,
        vault_id: str | None = None,
        blockchain: str | None = None,
        blockchain_address: str | None = None,
    ) -> tuple[Served, PositionHistory]:
        """Public position lives and checkpoints, with no API wallet involved."""
        params = account_selector(
            account_id=account_id, vault_id=vault_id, blockchain=blockchain,
            blockchain_address=blockchain_address, public=True,
        )
        params.update(_position_params(
            symbol=symbol, side=side, status=status, start_ms=start_ms, end_ms=end_ms,
            limit=limit, cursor=cursor,
        ))
        served = self._get("/v2/history/position", params)
        return served, PositionHistory.model_validate_json(served.content)

    def account(
        self,
        *,
        account_id: str | None = None,
        vault_id: str | None = None,
        blockchain: str | None = None,
        blockchain_address: str | None = None,
    ) -> tuple[Served, Account]:
        params = account_selector(
            account_id=account_id,
            vault_id=vault_id,
            blockchain=blockchain,
            blockchain_address=blockchain_address,
            public=True,
        )
        served = self._get("/v2/account", params)
        return served, Account.model_validate_json(served.content)

    def balances(
        self,
        *,
        account_id: str | None = None,
        vault_id: str | None = None,
        blockchain: str | None = None,
        blockchain_address: str | None = None,
    ) -> tuple[Served, Balances]:
        params = account_selector(
            account_id=account_id,
            vault_id=vault_id,
            blockchain=blockchain,
            blockchain_address=blockchain_address,
            public=True,
        )
        served = self._get("/v2/balances", params)
        return served, Balances.model_validate_json(served.content)

    def transactions(
        self,
        *,
        tx_type: str | None = None,
        limit: int | None = None,
        account_id: str | None = None,
        vault_id: str | None = None,
        blockchain: str | None = None,
        blockchain_address: str | None = None,
    ) -> tuple[Served, TransactionHistory]:
        params = account_selector(
            account_id=account_id,
            vault_id=vault_id,
            blockchain=blockchain,
            blockchain_address=blockchain_address,
            public=True,
        )
        if tx_type:
            params["type"] = tx_type
        rendered_limit = _limit(limit, 1000)
        if rendered_limit is not None:
            params["limit"] = rendered_limit
        served = self._get("/v2/history/transaction", params)
        return served, TransactionHistory.model_validate_json(served.content)
