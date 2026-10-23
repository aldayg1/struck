"""The rest of Strike's read API, parsed as served.

Public GETs go through ``StrikeMarketClient.get``. Signed GETs go through
``StrikeAuthedClient.request``. Nothing here quotes, deposits, withdraws, or
places an order. Money fields stay the served strings.

Paths (skills under ``.cursor/skills/strike-*``, checked against Strike
TESTNET on 2026-10-22):

* ``GET /price/v2/exchangeInfo``
* ``GET /price/v2/ticker/bookTicker``
* ``GET /price/v2/markPrice``
* ``GET /price/v2/openInterest``
* ``GET /v2/vaults``
* ``GET /v2/copy/lead-traders``
* ``GET /v2/copy/discover/filters``
* ``GET /v2/openOrders``
* ``GET /v2/closedPositions``
* ``GET /v2/symbols``
* ``GET /v2/algo/twap?status=all``
* ``GET /v2/grid/bot``
* ``GET /v2/copy/subscriptions``
* ``GET /v2/copy/orders``
* ``GET /v2/copy/positions``
* ``GET /v2/copy/lead/stats``
* ``GET /v2/copy/saved``
* ``GET /v2/vault/positions``
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, StrictInt, StrictStr, TypeAdapter

from ..callhistory import Served
from .account import Portfolio, PositionsResponse, StrikeAuthedClient
from .market import Read, StrikeMarketClient
from .stream_models import MarkPriceUpdate


class _Served(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", frozen=True)


class ServedRow(_Served):
    """Fields the liquidity tables show. Absent fields stay unset. Extras are kept."""

    id: StrictStr | StrictInt | None = None
    symbol: StrictStr | None = None
    side: StrictStr | None = None
    type: StrictStr | None = None
    size: StrictStr | None = None
    price: StrictStr | None = None
    status: StrictStr | None = None
    mode: StrictStr | None = None
    margin: StrictStr | None = None
    strategy_id: StrictStr | None = None
    filled_size: StrictStr | None = None
    total_size: StrictStr | None = None


def _none_is_empty(value: object) -> object:
    """Closed positions and vault positions serve ``null`` for an empty list."""
    if value is None:
        return []
    return value


class RateLimit(_Served):
    rateLimitType: StrictStr
    interval: StrictStr
    intervalNum: StrictInt
    limit: StrictInt


class ExchangeSymbol(_Served):
    symbol: StrictStr
    status: StrictStr
    baseAsset: StrictStr
    quoteAsset: StrictStr


class ExchangeInfo(_Served):
    timezone: StrictStr
    serverTime: StrictInt
    rateLimits: tuple[RateLimit, ...]
    symbols: tuple[ExchangeSymbol, ...]


class BookTicker(_Served):
    symbol: StrictStr
    bidPrice: StrictStr
    bidQty: StrictStr
    askPrice: StrictStr
    askQty: StrictStr
    time: StrictInt


class BookTickers(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    rows: tuple[BookTicker, ...]


class OpenInterest(_Served):
    symbol: StrictStr
    openInterest: StrictStr
    time: StrictInt


class VaultSummary(_Served):
    vault_id: StrictStr
    type: StrictStr
    name: StrictStr


class VaultPage(_Served):
    count: StrictInt
    limit: StrictInt
    offset: StrictInt
    vaults: tuple[VaultSummary, ...]


class LeadTrader(_Served):
    account_id: StrictStr
    platform: StrictStr
    nickname: StrictStr | None = None


class LeadTraders(_Served):
    auto_create_sub_account_enabled: bool
    lead_traders: tuple[LeadTrader, ...]


class DiscoverFilters(_Served):
    enabled: bool
    platforms: tuple[StrictStr, ...]
    styles: tuple[StrictStr, ...]


class SymbolSetting(_Served):
    leverage: StrictInt
    margin_mode: StrictStr


class SymbolSettings(_Served):
    default_margin_mode: StrictStr
    default_leverage: StrictInt
    symbol_settings: dict[str, SymbolSetting]


class OpenOrder(_Served):
    """Strike Order fields retain their documented PascalCase names."""

    Symbol: StrictStr
    Side: StrictStr
    Type: StrictStr
    Size: StrictStr
    Price: StrictStr
    Status: StrictStr


class TwapStrategy(_Served):
    """Native TWAP state; Strike runs the slices and serves these values."""

    strategy_id: StrictStr
    market: StrictStr
    side: StrictStr
    status: StrictStr
    filled_size: StrictStr
    total_size: StrictStr


class OpenOrders(_Served):
    count: StrictInt
    orders: Annotated[list[OpenOrder], BeforeValidator(_none_is_empty)]


class ClosedPositions(_Served):
    count: StrictInt
    positions: Annotated[list[ServedRow], BeforeValidator(_none_is_empty)]


class TwapList(_Served):
    strategies: list[TwapStrategy]


class GridBots(_Served):
    count: StrictInt
    bots: list[ServedRow]


class CopySubscriptions(_Served):
    subscriptions: list[ServedRow]


class CopyOrders(_Served):
    orders: list[ServedRow]
    traders: list[ServedRow]


class CopyPositions(_Served):
    positions: list[ServedRow]
    traders: list[ServedRow]
    total_margin: StrictStr
    total_worth: StrictStr
    realized_pnl: StrictStr
    total_pnl: StrictStr


class CopyLeadStats(_Served):
    lead_account_id: StrictStr
    profit_share_rate: StrictStr


class CopySaved(_Served):
    saved_traders: list[ServedRow]


class VaultPositions(_Served):
    positions: Annotated[list[ServedRow], BeforeValidator(_none_is_empty)]


class Leaderboard(_Served):
    """``GET /v2/copy/leaderboard`` as served. Row fields stay extras."""

    entries: list[ServedRow]


class Copiers(_Served):
    """``GET /v2/copy/copiers``. Row fields stay extras."""

    lead_account_id: StrictStr
    summary: ServedRow
    copiers: list[ServedRow]


class PublicSurface(_Served):
    exchange: ExchangeInfo
    book: tuple[BookTicker, ...]
    marks: tuple[MarkPriceUpdate, ...]
    interest: tuple[OpenInterest, ...]
    vaults: VaultPage
    leads: LeadTraders
    filters: DiscoverFilters


class VaultPortfolio(_Served):
    """``GET /v2/vault/{id}/portfolio`` (strike-vaults). Fields the Skill names
    are omitted until the data exists; all money values stay served strings."""

    vault_id: StrictStr
    total_value_locked: StrictStr
    apr: StrictStr | None = None
    period_start_share_price: StrictStr | None = None
    period_start_timestamp: StrictInt | None = None
    all_time_deposits: StrictStr
    all_time_withdrawals: StrictStr
    all_time_pnl: StrictStr
    return_24h: StrictStr
    period_return: StrictStr
    volume: StrictStr
    all_time_volume: StrictStr
    depositors: StrictInt
    fee: StrictStr
    commission_bps: StrictInt
    fee_earned: StrictStr
    pending_fees_owed: StrictStr
    max_drawdown: StrictStr | None = None
    sharpe_ratio: StrictStr | None = None
    created_at: StrictInt


class VaultHistoryRow(_Served):
    """One deposit/withdrawal row of ``GET /v2/vault/{id}/history``. A withdrawal
    row's ``id`` IS the withdrawal_id. Conditional fields stay unset."""

    id: StrictStr
    type: StrictStr
    status: StrictStr
    timestamp: StrictInt
    usd_amount: StrictStr | None = None
    shares_minted: StrictStr | None = None
    share_price: StrictStr | None = None
    shares_burned: StrictStr | None = None
    estimated_usd: StrictStr | None = None
    actual_usd: StrictStr | None = None


class VaultHistory(_Served):
    vault_id: StrictStr
    history: Annotated[list[VaultHistoryRow], BeforeValidator(_none_is_empty)]
    limit: StrictInt
    next_cursor: StrictStr | None = None


class Depositor(_Served):
    """``GET /v2/vault/{id}/depositors`` row; ``pnl`` is served, not computed."""

    address: StrictStr
    nickname: StrictStr | None = None
    deposited: StrictStr
    total_deposited: StrictStr
    total_withdrawn: StrictStr
    current_equity: StrictStr
    share_percentage: StrictStr
    pnl: StrictStr
    deposited_since: StrictInt
    avatar_url: StrictStr | None = None


class VaultDepositors(_Served):
    depositors: Annotated[list[Depositor], BeforeValidator(_none_is_empty)]
    count: StrictInt
    limit: StrictInt
    next_cursor: StrictStr | None = None


class LiquidationRow(_Served):
    """``GET /v2/vault/{id}/liquidations`` row: one liquidation-fee receipt."""

    id: StrictInt
    timestamp: StrictInt
    symbol: StrictStr | None = None
    fill_id: StrictInt | None = None
    order_id: StrictInt | None = None
    side: StrictStr | None = None
    position_side: StrictStr | None = None
    price: StrictStr | None = None
    size: StrictStr | None = None
    notional: StrictStr | None = None
    liquidation_fee_earned: StrictStr


class VaultLiquidations(_Served):
    vault_id: StrictStr
    liquidations: Annotated[list[LiquidationRow], BeforeValidator(_none_is_empty)]
    limit: StrictInt
    next_cursor: StrictStr | None = None


class Announcement(_Served):
    """``GET /v2/vault/{id}/announcements`` row."""

    announcement_id: StrictStr
    vault_id: StrictStr
    title: StrictStr
    body: StrictStr
    created_at: StrictInt
    updated_at: StrictInt | None = None


class VaultAnnouncements(_Served):
    vault_id: StrictStr
    announcements: Annotated[list[Announcement], BeforeValidator(_none_is_empty)]
    limit: StrictInt
    next_cursor: StrictStr | None = None


class DiscoverItem(_Served):
    """One ``GET /v2/copy/discover`` row (strike-copy-trading). ``address`` is a
    Hyperliquid wallet or a Strike account id; score fields are extras."""

    address: StrictStr
    platform: StrictStr
    enrichment_state: StrictStr


class DiscoverPage(_Served):
    items: Annotated[list[DiscoverItem], BeforeValidator(_none_is_empty)]
    total: StrictInt
    data_as_of_ms: StrictInt
    next_cursor: StrictStr | None = None


class AuthedSurface(_Served):
    portfolio: Portfolio
    symbols: SymbolSettings
    open_orders: OpenOrders
    closed_positions: ClosedPositions
    twap: TwapList
    grids: GridBots
    subscriptions: CopySubscriptions
    copy_orders: CopyOrders
    copy_positions: CopyPositions
    lead_stats: CopyLeadStats
    saved: CopySaved
    vault_positions: VaultPositions


def _read[T: BaseModel](served: Served, model: type[T]) -> Read[T]:
    return Read(served, model.model_validate_json(served.content))


_BOOKS: TypeAdapter[tuple[BookTicker, ...]] = TypeAdapter(tuple[BookTicker, ...])
_MARKS: TypeAdapter[tuple[MarkPriceUpdate, ...]] = TypeAdapter(tuple[MarkPriceUpdate, ...])


def exchange_info(market: StrikeMarketClient) -> Read[ExchangeInfo]:
    return _read(market.get("/price/v2/exchangeInfo"), ExchangeInfo)


def book_tickers(market: StrikeMarketClient) -> Read[BookTickers]:
    served = market.get("/price/v2/ticker/bookTicker")
    return Read(served, BookTickers(rows=_BOOKS.validate_json(served.content)))


def mark_prices(market: StrikeMarketClient) -> Read[tuple[MarkPriceUpdate, ...]]:
    served = market.get("/price/v2/markPrice")
    return Read(served, _MARKS.validate_json(served.content))


def open_interest(market: StrikeMarketClient, symbol: str) -> Read[OpenInterest]:
    if not symbol:
        raise ValueError("open interest requires a symbol")
    return _read(market.get("/price/v2/openInterest", {"symbol": symbol}), OpenInterest)


def vaults(market: StrikeMarketClient, limit: int) -> Read[VaultPage]:
    if limit < 1:
        raise ValueError("vault list limit must be >= 1")
    return _read(market.get("/v2/vaults", {"limit": str(limit)}), VaultPage)


def _vault_segment(what: str, value: str) -> str:
    if not value or any(c in value for c in "/?#%\\ @"):
        raise ValueError(f"{what} must be a single unescaped path segment")
    return value


def vault_detail(market: StrikeMarketClient, vault_id: str) -> Read[VaultSummary]:
    """``GET /v2/vault/{id}`` (strike-vaults): the list item with its live
    fields filled in; unknown id = Strike's own 404."""
    return _read(market.get(f"/v2/vault/{_vault_segment('vault_id', vault_id)}"), VaultSummary)


VAULT_PERIODS: tuple[str, ...] = ("24h", "7d", "30d", "6m", "1y", "all")


def vault_portfolio(
    market: StrikeMarketClient, vault_id: str, period: str = "30d"
) -> Read[VaultPortfolio]:
    """``GET /v2/vault/{id}/portfolio``. An invalid period is Strike's 404."""
    if period not in VAULT_PERIODS:
        raise ValueError(f"vault portfolio period is one of {', '.join(VAULT_PERIODS)}")
    served = market.get(
        f"/v2/vault/{_vault_segment('vault_id', vault_id)}/portfolio", {"period": period}
    )
    return Read(served, VaultPortfolio.model_validate_json(served.content))


def vault_history(
    market: StrikeMarketClient,
    vault_id: str,
    *,
    tx_type: str | None = None,
    status: str | None = None,
    limit: int | None = None,
    pagination: str | None = None,
) -> Read[VaultHistory]:
    """``GET /v2/vault/{id}/history``: the vault's deposits and withdrawals."""
    if tx_type is not None and tx_type not in ("deposit", "withdrawal"):
        raise ValueError("vault history type is deposit or withdrawal")
    if status is not None and status not in ("completed", "pending", "cancelled", "failed"):
        raise ValueError("vault history status is completed, pending, cancelled or failed")
    params = _vault_page(tx_type=tx_type, status=status, limit=limit, pagination=pagination)
    served = market.get(
        f"/v2/vault/{_vault_segment('vault_id', vault_id)}/history", params or None
    )
    return Read(served, VaultHistory.model_validate_json(served.content))


def _vault_page(
    *,
    tx_type: str | None = None,
    status: str | None = None,
    limit: int | None = None,
    pagination: str | None = None,
) -> dict[str, str]:
    params: dict[str, str] = {}
    if tx_type is not None:
        params["type"] = tx_type
    if status is not None:
        params["status"] = status
    if limit is not None:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("vault limit must be an integer 1..500")
        params["limit"] = str(limit)
    if pagination is not None:
        if not pagination:
            raise ValueError("pagination cursor must be a non-empty served string")
        params["pagination"] = pagination
    return params


def vault_depositors(
    market: StrikeMarketClient, vault_id: str, *, limit: int | None = None, pagination: str | None = None
) -> Read[VaultDepositors]:
    """``GET /v2/vault/{id}/depositors`` (sorted by shares, desc)."""
    params = _vault_page(limit=limit, pagination=pagination)
    served = market.get(
        f"/v2/vault/{_vault_segment('vault_id', vault_id)}/depositors", params or None
    )
    return Read(served, VaultDepositors.model_validate_json(served.content))


def vault_liquidations(
    market: StrikeMarketClient,
    vault_id: str,
    *,
    limit: int | None = None,
    symbol: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    pagination: str | None = None,
) -> Read[VaultLiquidations]:
    """``GET /v2/vault/{id}/liquidations``: liquidation-fee income rows."""
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
        raise ValueError("vault liquidations limit must be an integer 1..100")
    params = _vault_page(limit=limit, pagination=pagination)
    if symbol:
        if len(symbol) > 100:
            raise ValueError("vault liquidations symbol must be at most 100 chars")
        params["symbol"] = symbol
    if start_ms is not None:
        if type(start_ms) is not int or start_ms < 0:
            raise ValueError("startTime must be a non-negative integer")
        params["startTime"] = str(start_ms)
    if end_ms is not None:
        if type(end_ms) is not int or end_ms < 0:
            raise ValueError("endTime must be a non-negative integer")
        params["endTime"] = str(end_ms)
    served = market.get(
        f"/v2/vault/{_vault_segment('vault_id', vault_id)}/liquidations", params or None
    )
    return Read(served, VaultLiquidations.model_validate_json(served.content))


def vault_announcements(
    market: StrikeMarketClient, vault_id: str, *, limit: int | None = None, pagination: str | None = None
) -> Read[VaultAnnouncements]:
    """``GET /v2/vault/{id}/announcements``. Posting/deleting is JWT-only; Struck
    reads the announcements it can see."""
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
        raise ValueError("vault announcements limit must be an integer 1..100")
    params = _vault_page(limit=limit, pagination=pagination)
    served = market.get(
        f"/v2/vault/{_vault_segment('vault_id', vault_id)}/announcements", params or None
    )
    return Read(served, VaultAnnouncements.model_validate_json(served.content))


COPY_PLATFORMS: tuple[str, ...] = ("hyperliquid", "strike", "all")


def copy_discover(market: StrikeMarketClient, *, platform: str = "hyperliquid") -> Read[DiscoverPage]:
    """``GET /v2/copy/discover`` (strike-copy-trading): one page, default sorted
    by ``pnl_30d``. Strike's finer query filters are served by this same route;
    this reads the served default page. An invalid platform is refused here.
    """
    if platform not in COPY_PLATFORMS:
        raise ValueError(f"copy discover platform is one of {', '.join(COPY_PLATFORMS)}")
    served = market.get("/v2/copy/discover", {"platform": platform, "limit": "50"})
    return Read(served, DiscoverPage.model_validate_json(served.content))


def copy_discover_one(market: StrikeMarketClient, address: str) -> Read[DiscoverItem]:
    """``GET /v2/copy/discover/{address}``: one trader. 404 = not found (served)."""
    return _read(
        market.get(f"/v2/copy/discover/{_vault_segment('address', address)}"), DiscoverItem
    )


def lead_traders(market: StrikeMarketClient) -> Read[LeadTraders]:
    return _read(market.get("/v2/copy/lead-traders"), LeadTraders)


def discover_filters(market: StrikeMarketClient) -> Read[DiscoverFilters]:
    return _read(market.get("/v2/copy/discover/filters"), DiscoverFilters)


def leaderboard(market: StrikeMarketClient, status: str = "active") -> Read[Leaderboard]:
    """``GET /v2/copy/leaderboard``. ``status`` is ``active``, ``stopped`` or ``liquidated``."""
    if status not in ("active", "stopped", "liquidated"):
        raise ValueError("leaderboard status is active, stopped or liquidated")
    return _read(market.get("/v2/copy/leaderboard", {"status": status}), Leaderboard)


def copiers(market: StrikeMarketClient, lead_account_id: str) -> Read[Copiers]:
    """``GET /v2/copy/copiers``. ``lead_account_id`` is required by Strike."""
    if not lead_account_id:
        raise ValueError("copiers requires lead_account_id")
    return _read(market.get("/v2/copy/copiers", {"lead_account_id": lead_account_id}), Copiers)


def public_surface(market: StrikeMarketClient) -> PublicSurface:
    book = book_tickers(market).value.rows
    interest = tuple(open_interest(market, row.symbol).value for row in book)
    return PublicSurface(
        exchange=exchange_info(market).value,
        book=book,
        marks=mark_prices(market).value,
        interest=interest,
        vaults=vaults(market, 20).value,
        leads=lead_traders(market).value,
        filters=discover_filters(market).value,
    )


def _signed[T: BaseModel](client: StrikeAuthedClient, path: str, model: type[T]) -> T:
    return model.model_validate_json(client.request("GET", path).content)


def _public_selector(
    *,
    account_id: str | None = None,
    vault_id: str | None = None,
    blockchain_address: str | None = None,
) -> dict[str, str]:
    """One public account selector (strike-trading: no auth when account_id,
    vault_id or blockchain_address is given). Exactly one is required."""
    given = [v for v in (account_id, vault_id, blockchain_address) if v is not None]
    if len(given) != 1:
        raise ValueError(
            "public trading read needs exactly one of account_id, vault_id, or blockchain_address"
        )
    if vault_id is not None:
        return {"vault_id": vault_id}
    if account_id is not None:
        return {"account_id": account_id}
    assert blockchain_address is not None  # exactly one selector is non-None above
    if not blockchain_address:
        raise ValueError("blockchain_address must be a non-empty served address")
    return {"blockchain_address": blockchain_address}


def public_open_orders(
    market: StrikeMarketClient,
    *,
    account_id: str | None = None,
    vault_id: str | None = None,
    blockchain_address: str | None = None,
) -> Read[OpenOrders]:
    """``GET /v2/openOrders`` with a public selector (unsigned)."""
    params: dict[str, str] = dict(_public_selector(account_id=account_id, vault_id=vault_id, blockchain_address=blockchain_address))
    served = market.get("/v2/openOrders", params)
    return Read(served, OpenOrders.model_validate_json(served.content))


def public_order(
    market: StrikeMarketClient,
    symbol: str,
    *,
    order_id: int | None = None,
    client_order_id: str | None = None,
    account_id: str | None = None,
    vault_id: str | None = None,
    blockchain_address: str | None = None,
) -> Read[ServedRow]:
    """``GET /v2/order`` with a public selector; exactly one of order_id /
    client_order_id as strike-trading documents."""
    if (order_id is None) == (client_order_id is None):
        raise ValueError("GET /v2/order needs exactly one of order_id, client_order_id")
    params: dict[str, str] = {"symbol": symbol}
    if order_id is not None:
        if type(order_id) is not int or order_id < 1:
            raise ValueError("order_id must be a positive integer")
        params["order_id"] = str(order_id)
    else:
        assert client_order_id is not None
        if not client_order_id:
            raise ValueError("client_order_id must be a non-empty served string")
        params["client_order_id"] = client_order_id
    params.update(_public_selector(account_id=account_id, vault_id=vault_id, blockchain_address=blockchain_address))
    served = market.get("/v2/order", params)
    return Read(served, ServedRow.model_validate_json(served.content))


def public_twaps(
    market: StrikeMarketClient,
    *,
    account_id: str | None = None,
    vault_id: str | None = None,
    blockchain_address: str | None = None,
) -> Read[TwapList]:
    """``GET /v2/algo/twap`` with a public selector, every status as served."""
    params: dict[str, str] = {"status": "all"}
    params.update(_public_selector(account_id=account_id, vault_id=vault_id, blockchain_address=blockchain_address))
    served = market.get("/v2/algo/twap", params)
    return Read(served, TwapList.model_validate_json(served.content))


def public_grid_bots(
    market: StrikeMarketClient,
    *,
    account_id: str | None = None,
    vault_id: str | None = None,
    blockchain_address: str | None = None,
) -> Read[GridBots]:
    """``GET /v2/grid/bot`` with a public selector."""
    params: dict[str, str] = dict(_public_selector(account_id=account_id, vault_id=vault_id, blockchain_address=blockchain_address))
    served = market.get("/v2/grid/bot", params)
    return Read(served, GridBots.model_validate_json(served.content))


def public_grid_bot(
    market: StrikeMarketClient,
    bot_id: str,
    *,
    account_id: str | None = None,
    vault_id: str | None = None,
    blockchain_address: str | None = None,
) -> Read[ServedRow]:
    """``GET /v2/grid/bot/{id}`` with a public selector."""
    if not bot_id or any(c in bot_id for c in "/?#%\\ @"):
        raise ValueError("grid bot id must be a single unescaped path segment")
    params: dict[str, str] = dict(_public_selector(account_id=account_id, vault_id=vault_id, blockchain_address=blockchain_address))
    served = market.get(f"/v2/grid/bot/{bot_id}", params)
    return Read(served, ServedRow.model_validate_json(served.content))


def public_grid_bot_orders(
    market: StrikeMarketClient,
    bot_id: str,
    *,
    account_id: str | None = None,
    vault_id: str | None = None,
    blockchain_address: str | None = None,
) -> Read[ServedRow]:
    """``GET /v2/grid/bot/{id}/orders` with a public selector. Shape stays as
    served (no invented field list)."""
    if not bot_id or any(c in bot_id for c in "/?#%\\ @"):
        raise ValueError("grid bot id must be a single unescaped path segment")
    params: dict[str, str] = dict(_public_selector(account_id=account_id, vault_id=vault_id, blockchain_address=blockchain_address))
    served = market.get(f"/v2/grid/bot/{bot_id}/orders", params)
    return Read(served, ServedRow.model_validate_json(served.content))


def public_positions(
    market: StrikeMarketClient,
    *,
    account_id: str | None = None,
    vault_id: str | None = None,
    blockchain_address: str | None = None,
) -> Read[PositionsResponse]:
    """``GET /v2/positions`` with a public selector (from .account: the served
    Position / PositionsResponse models)."""
    params: dict[str, str] = dict(_public_selector(account_id=account_id, vault_id=vault_id, blockchain_address=blockchain_address))
    served = market.get("/v2/positions", params)
    return Read(served, PositionsResponse.model_validate_json(served.content))


def authed_surface(client: StrikeAuthedClient) -> AuthedSurface:
    return AuthedSurface(
        portfolio=client.portfolio()[1],
        symbols=_signed(client, "/v2/symbols", SymbolSettings),
        open_orders=_signed(client, "/v2/openOrders", OpenOrders),
        closed_positions=_signed(client, "/v2/closedPositions?limit=50", ClosedPositions),
        twap=_signed(client, "/v2/algo/twap?status=all", TwapList),
        grids=_signed(client, "/v2/grid/bot", GridBots),
        subscriptions=_signed(client, "/v2/copy/subscriptions?status=all", CopySubscriptions),
        copy_orders=_signed(client, "/v2/copy/orders?limit=50", CopyOrders),
        copy_positions=_signed(client, "/v2/copy/positions", CopyPositions),
        lead_stats=_signed(client, "/v2/copy/lead/stats", CopyLeadStats),
        saved=_signed(client, "/v2/copy/saved", CopySaved),
        vault_positions=_signed(client, "/v2/vault/positions", VaultPositions),
    )
