"""Read-only LangChain tools over SERVED data (existing clients only). Each
tool returns the served JSON as-is (capped); nothing is derived. No tool can
approve, sign, send or write."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from langchain_core.tools import BaseTool, tool

from ..callhistory import RecordedHttp
from ..chain.backends import make_backend
from ..config import load_config
from ..network import canonical_address
from ..strike.account import StrikeAuthedClient
from ..strike.auth import StrikeCredentialsError
from ..strike.venue_auth import wallet_from_secrets
from ..strike.market import StrikeMarketClient

# The stock @tool decorator's stubs are Any-flavoured: squeezed to the exact
# shapes used here (same boundary treatment as the ).
_tool0 = cast("Callable[[Callable[[], str]], BaseTool]", tool)
_tool1 = cast("Callable[[Callable[[str], str]], BaseTool]", tool)

CAP = 9000  # characters of served JSON per tool result (context budget)


def _cap(text: str) -> str:
    return text if len(text) <= CAP else text[:CAP] + "…[truncated]"


@_tool0
def strike_markets() -> str:
    """Strike perps markets (selected network) exactly as served by GET /v2/markets
    (symbols, status, mark/index/last price, leverage tiers)."""
    cfg = load_config()
    http = RecordedHttp(cfg.timeout_s)
    try:
        return _cap(
            StrikeMarketClient(http, cfg.strike_api_url).markets().served.content.decode("utf-8")
        )
    finally:
        http.close()


@_tool1
def strike_depth(symbol: str) -> str:
    """Order book for one Strike symbol (e.g. "ETH-USD") as served by
    GET /price/v2/depth (top 20 bids/asks)."""
    cfg = load_config()
    http = RecordedHttp(cfg.timeout_s)
    try:
        return _cap(
            StrikeMarketClient(http, cfg.strike_api_url)
            .depth(symbol, 20)
            .served.content.decode("utf-8")
        )
    finally:
        http.close()


@_tool0
def strike_positions() -> str:
    """The user's Strike positions (selected network) as served by the signed read-only
    GET /v2/positions, when the API wallet key is set."""
    try:
        wallet = wallet_from_secrets()
    except StrikeCredentialsError as exc:
        return f"KEY NOT SET: {exc}"
    cfg = load_config()
    http = RecordedHttp(cfg.timeout_s)
    try:
        return _cap(
            StrikeAuthedClient(http, cfg.strike_api_url, wallet)
            .positions()[0]
            .content.decode("utf-8")
        )
    finally:
        http.close()


@_tool1
def wallet_utxos(address: str) -> str:
    """UTxOs at a Cardano address (of the selected network) via the ACTIVE chain backend
    (node: Kupo/Ogmios, koios: Koios), as served; lovelace are integers."""
    cfg = load_config()
    # Only a canonical address of the SELECTED network may enter a URL (stock pycardano).
    canonical_address(address, cfg.profile)
    http = RecordedHttp(cfg.timeout_s)
    try:
        view = make_backend(cfg, cfg.chain_backend, http).wallet_view(address, 10)
    finally:
        http.close()
    rows = [u.model_dump_json() for u in view.utxos]
    return _cap(
        f'{{"backend":"{cfg.chain_backend}","tip_slot":{view.tip_slot},"utxos":[{",".join(rows)}]}}'
    )


READ_ONLY_TOOLS: tuple[BaseTool, ...] = (
    strike_markets,
    strike_depth,
    strike_positions,
    wallet_utxos,
)
