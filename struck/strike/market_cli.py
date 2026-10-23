"""Read Strike public market data (selected network) and save every response as served.

``uv run python -m struck.strike.market_cli BTC-USD``
"""

from __future__ import annotations

import sys

from ..callhistory import RecordedHttp, Served
from ..config import RECEIPTS_DIR, load_config, pop_network_flag, network_help, wants_help
from .market import StrikeMarketClient


def _save(name: str, served: Served) -> str:
    out = RECEIPTS_DIR / "strike" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(served.content)
    return f"{out.relative_to(RECEIPTS_DIR.parent)} sha256={served.sha256}"


def main(argv: list[str]) -> int:
    if wants_help(argv):
        print(
            "usage: python -m struck.strike.market_cli --list [--network <name>]\n"
            "       python -m struck.strike.market_cli <SYMBOL> [--network <name>]\n"
            + network_help(),
            file=sys.stderr,
        )
        return 0
    argv = pop_network_flag(argv)
    if len(argv) != 1:
        print(
            "usage: python -m struck.strike.market_cli --list [--network <name>]\n"
            "       python -m struck.strike.market_cli <SYMBOL> [--network <name>]\n"
            + network_help(),
            file=sys.stderr,
        )
        return 2
    cfg = load_config()
    http = RecordedHttp(cfg.timeout_s)
    try:
        client = StrikeMarketClient(http, cfg.strike_api_url)
        markets = client.markets()
        if argv[0] == "--list":
            save = _save("markets.json", markets.served)
            print(f"markets: {len(markets.value.markets)} served -> {save}")
            for name in markets.value.refused:  # LOUD, never dropped silently
                print(f"REFUSED {name.symbol}: {name.reason}")
            for symbol, market in sorted(markets.value.markets.items()):
                default = "" if market.default_leverage is None else str(market.default_leverage)
                max_lev = str(market.margin_tiers[0].max_leverage) if market.margin_tiers else ""
                print(f"{market.symbol}: status={market.status} default_leverage={default} max_leverage={max_lev}")
            return 0
        symbol = argv[0]
        if symbol not in markets.value.markets:
            raise SystemExit(
                f"{symbol} is not served by /v2/markets: {sorted(markets.value.markets)}"
            )
        market = markets.value.markets[symbol]
        ticker = client.ticker_24h(symbol)
        depth = client.depth(symbol, 5)
        klines = client.klines(symbol, "1h", 24)
    finally:
        http.close()
    print(
        f"markets: {len(markets.value.markets)} served -> {_save('markets.json', markets.served)}"
    )
    default = "" if market.default_leverage is None else str(market.default_leverage)
    max_lev = str(market.margin_tiers[0].max_leverage) if market.margin_tiers else ""
    print(
        f"{symbol}: status={market.status} base_prec={market.base_prec} "
        f"quote_prec={market.quote_prec} default_leverage={default} "
        f"max_leverage={max_lev}"
    )
    print(
        f"ticker 24h: last={ticker.value.lastPrice} open={ticker.value.openPrice} "
        f"high={ticker.value.highPrice} low={ticker.value.lowPrice} vol={ticker.value.volume} "
        f"-> {_save(f'ticker_24h_{symbol}.json', ticker.served)}"
    )
    print(
        f"depth: lastUpdateId={depth.value.lastUpdateId} bids={list(depth.value.bids)} "
        f"asks={list(depth.value.asks)} -> {_save(f'depth_{symbol}.json', depth.served)}"
    )
    rows = klines.value.root
    print(
        f"klines 1h: {len(rows)} candles, last=[open {rows[-1][1]} high {rows[-1][2]} "
        f"low {rows[-1][3]} close {rows[-1][4]}] -> {_save(f'klines_1h_{symbol}.json', klines.served)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
