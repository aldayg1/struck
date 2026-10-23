"""Strike Finance PUBLIC market data (selected network), read-only, used AS SERVED (D2).

Endpoints (surface map ``plans/struck-strike-api-surface-2026-10-21.md``):
``GET /v2/markets`` (rules), ``/price/v2/ticker/24hr``, ``/price/v2/depth``,
``/price/v2/klines``. No key, no signing, no writes. Each call is recorded in
the call history; the raw served bytes are returned with the parsed view so a
report can always point at exactly what Strike served. Decimal fields stay
the served strings (no float conversion anywhere).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    RootModel,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
)

from ..callhistory import RecordedHttp, Served
from ..network import NetworkProfile, profile_for_strike_url

PriceType = Literal["last", "mark", "index"]


def source_for(net: NetworkProfile) -> str:
    """Call-history source tag: names the Strike venue actually called."""
    return f"strike-{net.strike_venue}"


class _Served(BaseModel):
    # extra="allow": fields Strike adds are kept as served, never dropped.
    model_config = ConfigDict(strict=True, extra="allow", frozen=True)


class MarginTier(_Served):
    max_notional: StrictStr
    max_leverage: StrictInt
    maintenance_margin_rate: StrictStr
    maintenance_amount: StrictStr


class Market(_Served):
    symbol: StrictStr
    name: StrictStr
    base_asset: StrictStr
    status: StrictStr
    base_prec: StrictInt
    quote_prec: StrictInt
    # Perp-only fields, modeled AS SERVED: a served market shape without them
    # parses (the fields are None) instead of rejecting the whole page.
    default_leverage: StrictInt | None = None
    margin_tiers: tuple[MarginTier, ...] | None = None
    mark_price: StrictStr | None = None
    index_price: StrictStr | None = None
    last_price: StrictStr | None = None


@dataclass(frozen=True)
class RefusedMarket:
    """One served /v2/markets entry whose strict parse failed: reported LOUDLY
    by name, never dropped, never guessed."""

    symbol: str
    reason: str


@dataclass(frozen=True)
class MarketsView:
    """The served /v2/markets page, parsed entry-by-entry.

    ``markets`` holds every entry that parsed; ``refused`` names every entry
    that did not, with the exact pydantic error — the caller displays it
    loudly. Neither list is ever filtered silently. Live evidence (receipts
    "markets-diff-20261022/DIAGNOSIS.md"): the endpoint does NOT paginate and
    ``GET /price/v2/exchangeInfo`` served exactly the same symbol set on both
    networks, so there is nothing to fetch beyond this one body."""

    markets: dict[str, Market]
    refused: tuple[RefusedMarket, ...]


def parse_markets(content: bytes) -> MarketsView:
    """Parse the served /v2/markets body entry-by-entry (the SAME strict JSON
    parse as the previous whole-body parse, applied per name so one bad entry
    is refused BY NAME while every parseable entry survives)."""
    raw: object = json.loads(content)
    if not isinstance(raw, dict) or not isinstance(raw.get("markets"), dict):
        raise ValueError('/v2/markets: served body has no "markets" object')
    rows: dict[str, object] = raw["markets"]
    parsed: dict[str, Market] = {}
    refused: list[RefusedMarket] = []
    for name, row in rows.items():
        try:
            parsed[name] = Market.model_validate_json(json.dumps(row))
        except ValidationError as exc:
            errors: list[dict[str, object]] = cast("list[dict[str, object]]", exc.errors())
            reasons: list[str] = []
            for col in errors:
                what = col.get("type", "validation_error")
                message = col.get("msg", str(exc))
                reasons.append(
                    f"{what if isinstance(what, str) else repr(what)}"
                    f": {message if isinstance(message, str) else repr(message)}"
                )
            refused.append(RefusedMarket(symbol=name, reason=" ; ".join(reasons) or str(exc)))
    return MarketsView(markets=parsed, refused=tuple(refused))


class ServerTime(_Served):
    serverTime: StrictInt


class Ticker24h(_Served):
    symbol: StrictStr
    lastPrice: StrictStr
    openPrice: StrictStr
    highPrice: StrictStr
    lowPrice: StrictStr
    volume: StrictStr
    quoteVolume: StrictStr
    openTime: StrictInt
    closeTime: StrictInt


class Depth(_Served):
    lastUpdateId: StrictInt
    E: StrictInt
    T: StrictInt
    bids: tuple[tuple[StrictStr, StrictStr], ...]
    asks: tuple[tuple[StrictStr, StrictStr], ...]


class PriceTicker(_Served):
    """``GET /price/v2/ticker/price`` row (market-api.yaml:412). With a symbol
    Strike serves one object; without it, an array."""

    symbol: StrictStr
    price: StrictStr
    time: StrictInt


class IndexPriceUpper(_Served):
    """``GET /price/v2/indexPrice`` row (market-api.yaml:313)."""

    e: StrictStr
    E: StrictInt
    s: StrictStr
    p: StrictStr


class PremiumIndex(_Served):
    """``GET /price/v2/premiumIndex`` row (market-api.yaml:210).

    ``latestPremiumIndex`` is "0" until the active funding window has a sample.
    """

    symbol: StrictStr
    markPrice: StrictStr
    indexPrice: StrictStr
    latestPremiumIndex: StrictStr
    averagePremiumIndex: StrictStr
    premiumIndexCount: StrictInt
    fundingRate: StrictStr
    nextFundingTime: StrictInt
    interestRate: StrictStr
    interestRateDampener: StrictStr
    fundingMultiplier: StrictStr
    time: StrictInt


class MarketStats(_Served):
    """One ``GET /price/v2/marketStats`` row's live stats (market-api.yaml:494).

    ``nextFundingRate`` is the NEXT FUNDING TIME (Unix ms) despite the name:
    Strike keeps the field name, the served meaning is documented.
    """

    volume24h: StrictStr
    volumeBase24h: StrictStr
    priceChange24h: StrictStr
    priceChangePercentage24h: StrictStr
    low24h: StrictStr
    high24h: StrictStr
    lastPrice: StrictStr
    markPrice: StrictStr
    indexPrice: StrictStr
    fundingRate: StrictStr
    nextFundingRate: StrictInt
    openInterest: StrictStr
    openInterestBase: StrictStr


class MarketStatsRow(_Served):
    symbol: StrictStr
    type: StrictStr
    status: StrictStr
    marketStats: MarketStats
    visibleOnUI: bool


class MarketStatsResponse(_Served):
    status: StrictStr
    data: tuple[MarketStatsRow, ...]


class FearGreedComponents(_Served):
    momentum: float
    breadth: float
    volatility: float


class FearGreedData(_Served):
    value: StrictInt
    classification: StrictStr
    timestamp: StrictInt
    components: FearGreedComponents
    # market-api.yaml:631 the count is a data field, not a sibling of data
    marketsCounted: StrictInt


class FearGreed(_Served):
    status: StrictStr
    data: FearGreedData


class FxRates(_Served):
    """``GET /price/v2/fxRates`` (market-api.yaml:987) as served. ``rates`` keys
    are whatever Strike serves (never hard-coded)."""

    status: StrictStr
    base: StrictStr
    date: StrictStr
    updatedAt: StrictInt
    rates: dict[str, StrictStr]


class Sparklines(_Served):
    """``GET /price/v2/sparklines`` (market-api.yaml:820). ``data`` maps each
    symbol to its close series as served."""

    status: StrictStr
    interval: StrictStr
    priceType: StrictStr
    points: StrictInt
    data: dict[str, object]


class StablecoinRates(_Served):
    """``GET /price/v2/stablecoin/rates`` root map (market-api.yaml:964):
    symbol -> USD decimal string; the served key set is never hard-coded."""

    model_config = ConfigDict(strict=True, extra="allow", frozen=True)


class L3Order(_Served):
    """One resting L3 order (market-api.yaml:1045)."""

    order_id: StrictStr
    size: StrictStr


class L3Level(_Served):
    price: StrictStr
    orders: tuple[L3Order, ...]


class L3Snapshot(_Served):
    """``GET /v2/l3/snapshot``. Served only for allowlisted symbols (BTC-USD)."""

    type: StrictStr
    symbol: StrictStr
    last_sequence: StrictInt
    timestamp: StrictInt
    bids: tuple[L3Level, ...]
    asks: tuple[L3Level, ...]


class Trade(_Served):
    """``GET /price/v2/trades`` item (market-api.yaml ``TradeResponse``), newest first."""

    id: StrictInt
    price: StrictStr
    qty: StrictStr
    quoteQty: StrictStr
    time: StrictInt
    isBuyerMaker: bool


class Trades(RootModel[tuple[Trade, ...]]):
    model_config = ConfigDict(strict=True, frozen=True)


# With ``symbol`` these routes serve ONE object; without it an ARRAY
# (market-api.yaml: "With symbol returns one object; without it returns an
# array"). The union adapters accept exactly the served shape; anything else
# fails the strict parse loudly.
_TICKERS: TypeAdapter[PriceTicker | tuple[PriceTicker, ...]] = TypeAdapter(
    PriceTicker | tuple[PriceTicker, ...]
)
_INDEX_PRICES: TypeAdapter[IndexPriceUpper | tuple[IndexPriceUpper, ...]] = TypeAdapter(
    IndexPriceUpper | tuple[IndexPriceUpper, ...]
)
_PREMIUM: TypeAdapter[PremiumIndex | tuple[PremiumIndex, ...]] = TypeAdapter(
    PremiumIndex | tuple[PremiumIndex, ...]
)
_STABLECOINS: TypeAdapter[StablecoinRates] = TypeAdapter(StablecoinRates)


# One kline row as served: [openTime, open, high, low, close, volume, closeTime,
# quoteVolume, trades, takerBuyBase, takerBuyQuote, ignore].
KlineRow = tuple[
    StrictInt,
    StrictStr,
    StrictStr,
    StrictStr,
    StrictStr,
    StrictStr,
    StrictInt,
    StrictStr,
    StrictInt,
    StrictStr,
    StrictStr,
    StrictStr,
]


class Klines(RootModel[tuple[KlineRow, ...]]):
    model_config = ConfigDict(strict=True, frozen=True)


@dataclass(frozen=True)
class Read[T]:
    served: Served
    value: T


# strike-klines / strike-price-feeds: the request enum, not a computed series.
KLINE_INTERVALS: tuple[str, ...] = (
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
    "1M",
)

# strike-market-api.sql:820 (sparklines): the served interval enum and bounds.
SPARK_INTERVALS: tuple[str, ...] = ("5m", "15m", "30m", "1h", "2h", "4h")
SPARK_PRICE_TYPES: tuple[str, ...] = ("last", "mark", "index")
SPARK_POINTS_MAX = 288  # closes per market; 2..288 (values below 2 refused)


class StrikeMarketClient:
    def __init__(self, http: RecordedHttp, base_url: str) -> None:
        self._http = http
        # Exact-host allowlist: only the two Strike deployments (struck.network).
        self.network = profile_for_strike_url(base_url)
        self._base = self.network.strike_api_url

    def _get(self, path: str, params: dict[str, str] | None = None) -> Served:
        return self._http.request(
            source_for(self.network), "GET", f"{self._base}{path}", params=params
        )

    def get(self, path: str, params: dict[str, str] | None = None) -> Served:
        """Public GET on the pinned Strike host. ``path`` is a path, not a URL."""
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "://" in path
            or "\\" in path
            or "@" in path.split("?", 1)[0]
        ):
            raise ValueError(
                f"public request path {path!r} refused: must start with a single '/' "
                "on the pinned Strike host"
            )
        return self._get(path, params)

    def markets(self) -> Read[MarketsView]:
        served = self._get("/v2/markets")
        return Read(served, parse_markets(served.content))

    def fee_tiers(self) -> Served:
        """``GET /v2/fee-tiers``: PascalCase JSON NUMBERS (``-5e-05`` on
        mainnet). Returned as the raw served bytes only; the exact parse is in
        Rust (serde_json arbitrary_precision), never through a Python float."""
        return self._get("/v2/fee-tiers")

    def server_time(self) -> Read[ServerTime]:
        served = self._get("/v2/time")
        return Read(served, ServerTime.model_validate_json(served.content))

    def ticker_24h(self, symbol: str) -> Read[Ticker24h]:
        served = self._get("/price/v2/ticker/24hr", {"symbol": symbol})
        return Read(served, Ticker24h.model_validate_json(served.content))

    def depth(self, symbol: str, limit: int) -> Read[Depth]:
        served = self._get("/price/v2/depth", {"symbol": symbol, "limit": str(limit)})
        return Read(served, Depth.model_validate_json(served.content))

    def trades(self, symbol: str, limit: int) -> Read[Trades]:
        served = self._get("/price/v2/trades", {"symbol": symbol, "limit": str(limit)})
        return Read(served, Trades.model_validate_json(served.content))

    def klines(
        self, symbol: str, interval: str, limit: int, price_type: PriceType = "last"
    ) -> Read[Klines]:
        params = {"symbol": symbol, "interval": interval, "limit": str(limit)}
        if price_type != "last":
            params["priceType"] = price_type
        served = self._get(
            "/price/v2/klines", params
        )
        return Read(served, Klines.model_validate_json(served.content))

    def _optional(self, name: str, value: str | None) -> dict[str, str]:
        return {name: value} if value is not None else {}

    def ticker_price(self, symbol: str | None = None) -> Read[PriceTicker | tuple[PriceTicker, ...]]:
        """``GET /price/v2/ticker/price``: last traded price; one object with a
        symbol, an array without it."""
        served = self._get("/price/v2/ticker/price", self._optional("symbol", symbol))
        return Read(served, _TICKERS.validate_json(served.content))

    def index_price(self, symbol: str | None = None) -> Read[IndexPriceUpper | tuple[IndexPriceUpper, ...]]:
        """``GET /price/v2/indexPrice``: one object with a symbol, an array without it."""
        served = self._get("/price/v2/indexPrice", self._optional("symbol", symbol))
        return Read(served, _INDEX_PRICES.validate_json(served.content))

    def premium_index(self, symbol: str | None = None) -> Read[PremiumIndex | tuple[PremiumIndex, ...]]:
        """``GET /price/v2/premiumIndex``: the active funding window as served."""
        served = self._get("/price/v2/premiumIndex", self._optional("symbol", symbol))
        return Read(served, _PREMIUM.validate_json(served.content))

    def market_stats(self, symbol: str | None = None) -> Read[MarketStatsResponse]:
        """``GET /price/v2/marketStats``: metadata + live stats for every market
        (or one with ``symbol``). Not cached, newest read."""
        served = self._get("/price/v2/marketStats", self._optional("symbol", symbol))
        return Read(served, MarketStatsResponse.model_validate_json(served.content))

    def fear_greed(self) -> Read[FearGreed]:
        """``GET /price/v2/fearGreed``: display-only sentiment index (0..100).
        Per its doc it is NOT a trading or risk input."""
        served = self._get("/price/v2/fearGreed")
        return Read(served, FearGreed.model_validate_json(served.content))

    def fx_rates(self, symbols: str | None = None) -> Read[FxRates]:
        """``GET /price/v2/fxRates``: display-only USD->fiat reference rates.
        ``symbols`` is the documented comma-separated 3-letter filter."""
        served = self._get("/price/v2/fxRates", self._optional("symbols", symbols))
        return Read(served, FxRates.model_validate_json(served.content))

    def sparklines(
        self, interval: str, points: int, price_type: str
    ) -> Read[Sparklines]:
        """``GET /price/v2/sparklines``. ``price_type`` defaults to Strike's
        'mark' but only 'last' returns data today (their doc); the caller
        chooses explicitly. ``points`` is 2..288 (Strike clamps above)."""
        if interval not in SPARK_INTERVALS:
            raise ValueError(f"sparklines interval must be one of {', '.join(SPARK_INTERVALS)}")
        if price_type not in SPARK_PRICE_TYPES:
            raise ValueError(f"sparklines priceType must be one of {', '.join(SPARK_PRICE_TYPES)}")
        if type(points) is not int or not 2 <= points <= SPARK_POINTS_MAX:
            raise ValueError(f"sparklines points must be an integer 2..{SPARK_POINTS_MAX}")
        params = {"interval": interval, "points": str(points), "priceType": price_type}
        served = self._get("/price/v2/sparklines", params)
        return Read(served, Sparklines.model_validate_json(served.content))

    def stablecoin_rates(self) -> dict[str, object]:
        """``GET /price/v2/stablecoin/rates``: symbol -> USD price as served.
        Returns the LOOSE object (the served key set is never hard-coded);
        strict parsing belongs to the caller's exact use."""
        served = self._get("/price/v2/stablecoin/rates")
        return cast("dict[str, object]", json.loads(served.content))

    def l3_snapshot(self, symbol: str) -> Read[L3Snapshot]:
        """``GET /v2/l3/snapshot``: every visible resting order for ONE symbol
        at one coherent sequence. Only allowlisted symbols are served."""
        if not symbol:
            raise ValueError("L3 snapshot requires a symbol")
        served = self._get("/v2/l3/snapshot", {"symbol": symbol})
        return Read(served, L3Snapshot.model_validate_json(served.content))

    def _one_segment(self, what: str, value: str) -> str:
        if not value or any(c in value for c in "/?#%\\ \t@"):
            raise ValueError(f"{what} must be a single unescaped path segment")
        return value

    def ping(self) -> Served:
        """``GET /v2/ping`` (discovered live; unknown served shape): raw bytes."""
        return self._get("/v2/ping")

    def market_detail(self, symbol: str) -> Served:
        """``GET /v2/markets/{symbol}`` (discovered live): raw served bytes."""
        return self._get(f"/v2/markets/{self._one_segment('symbol', symbol)}")

    def market_fundamentals(self, symbol: str) -> Served:
        """``GET /v2/markets/{symbol}/fundamentals`` (discovered live): raw bytes."""
        return self._get(f"/v2/markets/{self._one_segment('symbol', symbol)}/fundamentals")

    def account_by_nickname(self, nickname: str) -> Served:
        """``GET /v2/account/by-nickname`` (discovered live): raw served bytes."""
        if not nickname or any(c in nickname for c in "/?#%\\@&"):
            raise ValueError("nickname must be a query value, not a path or URL chunk")
        return self._get("/v2/account/by-nickname", {"nickname": nickname})
