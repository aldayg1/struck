"""Read Strike's PUBLIC surface on the selected network; capture every response as served.

  uv run python -m struck.strike.read_cli [--network <name>] [--out <dir>] <operation> [params]

Operations (public reads ONLY — no writes, no auth, no arbitrary URL; every
network-specific value comes from the selected ``struck.network.PROFILES`` row):

  markets                              GET /v2/markets
  server-time                          GET /v2/time
  fee-tiers                            GET /v2/fee-tiers (raw bytes only; the exact
                                       numeric parse lives in Rust, never a Python float)
  ticker <symbol>                      GET /price/v2/ticker/24hr
  depth <symbol> <limit>               GET /price/v2/depth        (limit 1..1000)
  trades <symbol> <limit>              GET /price/v2/trades       (limit 1..1000)
  klines <symbol> <interval> <limit>   GET /price/v2/klines       (interval below,
                                                                 limit 1..1500)

``interval`` is one of the served request values in ``struck.strike.market.KLINE_INTERVALS``.
Symbol operations first fetch ``GET /v2/markets`` and refuse any symbol Strike does
not serve on the selected network (that markets response is captured too). Limit
bounds are Strike's documented ones (``.codex/skills/strike-market-data``).

Each call lands in the call history (``struck.callhistory``, automatic) and its
RAW served body bytes are written to ``<out>/<op>.json`` EXACTLY as served —
never normalized/re-serialized JSON. ``<out>/manifest.json`` maps each
operation name to the final URL, HTTP status, body byte length, body sha256 and
file name. Default ``<out>`` is ``receipts/read-cli-<network>-<timestamp>/``.

Authenticated reads are NOT offered here: they live behind the human unlock in
``struck.strike.account_cli`` (terminal master password; a worker must never
unlock), and this CLI adds no secret-reading path of its own.

Exit 0 = captured; 1 = HTTP failure (loud, partial receipts kept and named);
2 = usage error (unknown operation/params, unserved symbol). No network
selected = the standard loud refusal (``struck.config``).
"""

from __future__ import annotations

import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from ..callhistory import CallFailed, RecordedHttp, Served
from ..config import (
    RECEIPTS_DIR,
    StruckConfig,
    load_config,
    network_banner,
    network_help,
    pop_network_flag,
    wants_help,
)
from .market import KLINE_INTERVALS, StrikeMarketClient


class _UsageError(ValueError):
    """Unknown operation or params: usage is printed and the CLI exits 2."""


class ManifestEntry(BaseModel):
    """One captured call: provenance of a raw body file. No normalized payload."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    url: StrictStr  # the FINAL URL as served (query included)
    status: StrictInt
    response_bytes: StrictInt
    sha256: StrictStr
    file: StrictStr


class Manifest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    network: StrictStr
    generated_utc: StrictStr
    operations: dict[str, ManifestEntry]


# Served symbol shape (e.g. BTC-USD): anything else is a usage error before any
# call. Membership is still checked against the live /v2/markets response.
_SYMBOL = re.compile(r"^[A-Za-z0-9._-]+$")

_SYMBOL_OPS = ("ticker", "depth", "trades", "klines")


def _usage() -> str:
    return (
        "usage: python -m struck.strike.read_cli [--network <name>] [--out <dir>] <operation> [params]\n"
        "operations (public reads only):\n"
        "  markets\n"
        "  server-time\n"
        "  fee-tiers\n"
        "  ticker <symbol>\n"
        "  depth <symbol> <limit>                limit 1..1000\n"
        "  trades <symbol> <limit>               limit 1..1000\n"
        "  klines <symbol> <interval> <limit>    interval one of "
        + ", ".join(KLINE_INTERVALS)
        + "; limit 1..1500\n"
        "  --out <dir>   receipts dir (default receipts/read-cli-<network>-<timestamp>/);\n"
        "                receives each raw served body plus manifest.json\n"
        + network_help()
    )


def _pop_out(argv: list[str]) -> tuple[Path | None, list[str]]:
    """Consume ``--out <dir>`` (or ``--out=<dir>``); loud when the value is missing."""
    out: Path | None = None
    rest: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg == "--out" or arg.startswith("--out="):
            value = next(it, None) if arg == "--out" else arg.split("=", 1)[1]
            if value is None or not value:
                raise _UsageError("--out needs a directory path")
            if out is not None:
                raise _UsageError("--out given more than once")
            out = Path(value)
        else:
            rest.append(arg)
    return out, rest


def _symbol_param(value: str) -> str:
    if not _SYMBOL.fullmatch(value):
        raise _UsageError(f"symbol {value!r}: only letters, digits, '.', '_' or '-'")
    return value


def _limit_param(value: str, op: str, hi: int) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise _UsageError(f"{op}: limit must be an integer, got {value!r}") from exc
    if not 1 <= limit <= hi:
        raise _UsageError(f"{op}: limit must be 1..{hi}, got {limit}")
    return limit


def _parse_op(args: list[str]) -> tuple[str, str | None, str | None, int | None]:
    """(op, symbol, interval, limit) with every param validated BEFORE any call."""
    if not args:
        raise _UsageError("no operation given")
    op, params = args[0], args[1:]
    if op in ("markets", "server-time", "fee-tiers"):
        if params:
            raise _UsageError(f"{op} takes no params, got {params}")
        return op, None, None, None
    if op == "ticker":
        if len(params) != 1:
            raise _UsageError("ticker takes exactly one <symbol>")
        return op, _symbol_param(params[0]), None, None
    if op in ("depth", "trades"):
        if len(params) != 2:
            raise _UsageError(f"{op} takes exactly <symbol> <limit>")
        return op, _symbol_param(params[0]), None, _limit_param(params[1], op, 1000)
    if op == "klines":
        if len(params) != 3:
            raise _UsageError("klines takes exactly <symbol> <interval> <limit>")
        interval = params[1]
        if interval not in KLINE_INTERVALS:
            raise _UsageError(
                f"klines: interval {interval!r} is not a served interval; one of "
                + ", ".join(KLINE_INTERVALS)
            )
        return op, _symbol_param(params[0]), interval, _limit_param(params[2], op, 1500)
    raise _UsageError(f"unknown operation {op!r}")


class _Receipts:
    """Writes each raw served body as it arrives; manifest.json at the end."""

    def __init__(self, out: Path, network: str) -> None:
        self._out = out
        self._network = network
        self._entries: dict[str, ManifestEntry] = {}

    def save(self, name: str, served: Served) -> None:
        self._out.mkdir(parents=True, exist_ok=True)
        file = f"{name}.json"
        (self._out / file).write_bytes(served.content)  # RAW served bytes, verbatim
        self._entries[name] = ManifestEntry(
            url=served.url,
            status=served.status,
            response_bytes=len(served.content),
            sha256=served.sha256,
            file=file,
        )
        print(
            f"{name}: HTTP {served.status}, {len(served.content)} bytes, "
            f"sha256 {served.sha256} -> {(self._out / file)}"
        )

    def write_manifest(self) -> Path:
        self._out.mkdir(parents=True, exist_ok=True)
        manifest = Manifest(
            network=self._network,
            generated_utc=datetime.now(UTC).isoformat(timespec="seconds"),
            operations=self._entries,
        )
        path = self._out / "manifest.json"
        path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path

    @property
    def out(self) -> Path:
        return self._out


def _served_markets(
    client: StrikeMarketClient, receipts: _Receipts
) -> tuple[str, ...]:
    """Fetch /v2/markets (captured), returning the served symbol names."""
    markets = client.markets()
    receipts.save("markets", markets.served)
    return tuple(markets.value.markets)


def _execute(
    op: str,
    symbol: str | None,
    interval: str | None,
    limit: int | None,
    client: StrikeMarketClient,
    receipts: _Receipts,
    served_symbols: tuple[str, ...],
) -> list[str]:
    """Run the operation; every call's raw bytes are already captured. Returns
    summary lines built from the NATIVE parsed models (never re-serialized)."""
    if op == "markets":
        return [f"markets: {len(served_symbols)} served symbols"]
    if op == "server-time":
        server_time = client.server_time()
        receipts.save("server-time", server_time.served)
        return [f"serverTime: {server_time.value.serverTime} (Unix ms, as served)"]
    if op == "fee-tiers":
        fees = client.fee_tiers()
        receipts.save("fee-tiers", fees)
        return [
            "fee-tiers: raw served bytes only "
            f"({len(fees.content)} bytes); the exact numeric parse is Rust's, not Python's"
        ]
    assert symbol is not None  # the symbol ops are the only ones reaching here
    if op == "ticker":
        ticker = client.ticker_24h(symbol)
        receipts.save(f"ticker_{symbol}", ticker.served)
        return [
            f"ticker 24h {symbol}: last={ticker.value.lastPrice} open={ticker.value.openPrice} "
            f"high={ticker.value.highPrice} low={ticker.value.lowPrice} volume={ticker.value.volume}"
        ]
    if op == "depth":
        assert limit is not None
        depth = client.depth(symbol, limit)
        receipts.save(f"depth_{symbol}_{limit}", depth.served)
        return [
            f"depth {symbol} limit={limit}: lastUpdateId={depth.value.lastUpdateId} "
            f"{len(depth.value.bids)} bids / {len(depth.value.asks)} asks"
        ]
    if op == "trades":
        assert limit is not None
        trades = client.trades(symbol, limit)
        receipts.save(f"trades_{symbol}_{limit}", trades.served)
        return [
            f"trades {symbol} limit={limit}: {len(trades.value.root)} trades served (newest first)"
        ]
    assert interval is not None and limit is not None
    klines = client.klines(symbol, interval, limit)
    receipts.save(f"klines_{symbol}_{interval}_{limit}", klines.served)
    rows = klines.value.root
    last = f", last close={rows[-1][4]}" if rows else ""
    return [f"klines {symbol} {interval} limit={limit}: {len(rows)} candles{last}"]


def _run(
    cfg: StruckConfig, http: RecordedHttp, out: Path, args: list[str]
) -> int:
    op, symbol, interval, limit = _parse_op(args)
    receipts = _Receipts(out, cfg.network)
    client = StrikeMarketClient(http, cfg.strike_api_url)
    print(network_banner(cfg))
    print(f"Strike REST: {cfg.strike_api_url}")
    print(f"receipts: {out}")
    served_symbols: tuple[str, ...] = ()
    if op == "markets" or op in _SYMBOL_OPS:
        served_symbols = _served_markets(client, receipts)
    if op in _SYMBOL_OPS:
        assert symbol is not None
        if symbol not in served_symbols:
            manifest = receipts.write_manifest()  # keep the bytes of the call made
            print(
                f"REFUSED: symbol {symbol!r} is not served by /v2/markets on this "
                f"network; served symbols: {sorted(served_symbols)}",
                file=sys.stderr,
            )
            print(f"(the markets response above was still captured: {manifest})")
            return 2
    try:
        lines = _execute(op, symbol, interval, limit, client, receipts, served_symbols)
    except CallFailed:
        manifest = receipts.write_manifest()  # no partial-success masking: keep + name
        print(f"FAILED partway; calls made so far are captured in {manifest}")
        raise
    manifest = receipts.write_manifest()
    for line in lines:
        print(line)
    print(f"manifest: {manifest}")
    return 0


def main(argv: list[str], http: RecordedHttp | None = None) -> int:
    """``http`` is the deliberate L2 test seam: tests inject a ``RecordedHttp``
    subclass replaying CAPTURED Strike bytes; production always builds the real
    one (every call then lands in the call history automatically)."""
    if wants_help(argv):
        print(_usage())
        return 0
    try:
        out, args = _pop_out(argv)
        args = pop_network_flag(args)  # --network + the loud fail-closed config check
    except _UsageError as exc:
        print(f"{exc}\n{_usage()}", file=sys.stderr)
        return 2
    cfg = load_config()
    if out is None:
        out = RECEIPTS_DIR / f"read-cli-{cfg.network}-{time.strftime('%Y%m%dT%H%M%S')}"
    client_http = http if http is not None else RecordedHttp(cfg.timeout_s)
    try:
        return _run(cfg, client_http, out, args)
    except _UsageError as exc:
        print(f"{exc}\n{_usage()}", file=sys.stderr)
        return 2
    except CallFailed as exc:
        # Loud, one line, no traceback: which call failed and why.
        print(f"FAILED (CallFailed): {exc}", file=sys.stderr)
        return 1
    finally:
        client_http.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
