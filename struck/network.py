"""The ONE network setting: mainnet and testnet run the same code against
different networks.

``PROFILES`` below is the ONLY place network-specific values live (Strike
REST/WS URLs, Cardano magic, Ogmios genesis network, Koios URL, address HRPs,
tx-body network_id, cardano-cli args, labels): one frozen constant table.
Every other module, and Rust (which receives the selected row as JSON via
``NetworkProfile.rust_json``), reads it from here. No fallback: an unknown name
fails loudly. There is NO code default: the network is chosen by ``--network``,
else ``<user config dir>/network.json``, else the ``network`` field of the local
``config/struck.json``; with none of them set, Struck refuses to start.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Final
from urllib.parse import urlsplit

from pycardano import Network as PycNetwork
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, ValidationError

from .paths import user_config_dir
from .private_file import read_private, write_private

# A network name is a key of PROFILES; the table decides which exist.
NetworkName = str


class NetworkProfile(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    name: StrictStr  # what --network / Settings select
    label: StrictStr  # title bar + every approval screen
    strike_label: StrictStr
    cardano_label: StrictStr
    strike_venue: StrictStr
    strike_api_url: StrictStr
    strike_price_ws: StrictStr
    strike_user_ws: StrictStr
    cardano_magic: StrictInt
    ogmios_genesis_network: StrictStr
    koios_url: StrictStr
    network_id: StrictInt  # tx body network_id == address header network (CIP-19)
    address_hrp: StrictStr
    stake_hrp: StrictStr
    cardano_cli_network_args: tuple[StrictStr, ...]
    explorer_tx_url: StrictStr  # "{tx_id}" is replaced; shown after a submit
    strike_api_keys_url: StrictStr  # Strike's API-keys page (register an API wallet)

    @property
    def address_prefix(self) -> str:
        """bech32 HRP + the bech32 separator '1' (BIP-173)."""
        return f"{self.address_hrp}1"

    @property
    def pycardano_network(self) -> PycNetwork:
        """pycardano's Network enum value IS the CIP-19 header network id."""
        return PycNetwork(self.network_id)

    @property
    def koios_host(self) -> str:
        return urlsplit(self.koios_url).hostname or ""

    def rust_json(self) -> str:
        """What struck_py's checks take: the selected row of PROFILES."""
        return self.model_dump_json(
            include={"name", "network_id", "address_hrp", "label", "strike_label"}
        )


# Sources:
# * Strike REST/WS hosts: Strike's public API docs (testnet and mainnet
#   deployments); exact-host allowlist.
# * cardano_magic / ogmios_genesis_network: the cardano-node Shelley genesis
#   ``networkMagic`` / ``network`` of each network.
# * koios_url: the koios.rest base URL of each network (public tier).
# * network_id / address_hrp / stake_hrp: CIP-19 (header network id and bech32
#   HRPs) and the Conway tx body ``network_id``.
# * cardano_cli_network_args: ``cardano-cli --mainnet | --testnet-magic N``.
_PREPROD: Final = NetworkProfile(
    name="preprod",
    label="preprod (Cardano preprod + Strike TESTNET)",
    strike_label="Strike TESTNET",
    cardano_label="Cardano preprod",
    strike_venue="testnet",
    strike_api_url="https://api-v2-testnet.strikefinance.org",
    strike_price_ws="wss://api-v2-testnet.strikefinance.org/ws/price",
    strike_user_ws="wss://api-v2-testnet.strikefinance.org/ws/user-api",
    cardano_magic=1,
    ogmios_genesis_network="testnet",
    koios_url="https://preprod.koios.rest/api/v1",
    network_id=0,
    address_hrp="addr_test",
    stake_hrp="stake_test",
    cardano_cli_network_args=("--testnet-magic", "1"),
    explorer_tx_url="https://preprod.cardanoscan.io/transaction/{tx_id}",
    strike_api_keys_url="https://testnet.strikefinance.org/api-keys",
)
# Operator decree 2026-10-22: mainnet is mainnet and preprod is preprod — plain
# names, no danger styling, no "real funds" editorializing anywhere.
_MAINNET: Final = NetworkProfile(
    name="mainnet",
    label="mainnet (Cardano mainnet + Strike MAINNET)",
    strike_label="Strike MAINNET",
    cardano_label="Cardano mainnet",
    strike_venue="mainnet",
    strike_api_url="https://api.strikefinance.org",
    strike_price_ws="wss://api.strikefinance.org/ws/price",
    strike_user_ws="wss://api.strikefinance.org/ws/user-api",
    cardano_magic=764824073,
    ogmios_genesis_network="mainnet",
    koios_url="https://api.koios.rest/api/v1",
    network_id=1,
    address_hrp="addr",
    stake_hrp="stake",
    cardano_cli_network_args=("--mainnet",),
    explorer_tx_url="https://cardanoscan.io/transaction/{tx_id}",
    strike_api_keys_url="https://app.strikefinance.org/api-keys",
)


def validate_table(rows: tuple[NetworkProfile, ...]) -> MappingProxyType[str, NetworkProfile]:
    """Every identifying value must differ between rows; else loud at import."""
    if not rows:
        raise ValueError("network table is empty")
    keys: tuple[tuple[str, tuple[object, ...]], ...] = (
        ("name", tuple(p.name for p in rows)),
        ("strike_venue", tuple(p.strike_venue for p in rows)),
        ("strike_api_url", tuple(p.strike_api_url for p in rows)),
        ("koios_url", tuple(p.koios_url for p in rows)),
        ("cardano_magic", tuple(p.cardano_magic for p in rows)),
        ("network_id", tuple(p.network_id for p in rows)),
        ("address_hrp", tuple(p.address_hrp for p in rows)),
        ("explorer_tx_url", tuple(p.explorer_tx_url for p in rows)),
        ("strike_api_keys_url", tuple(p.strike_api_keys_url for p in rows)),
    )
    for field, values in keys:
        if len(set(values)) != len(values):
            raise ValueError(f"network table: {field} must differ between networks")
    return MappingProxyType({p.name: p for p in rows})


PROFILES: Final[MappingProxyType[str, NetworkProfile]] = validate_table((_PREPROD, _MAINNET))
NETWORKS: Final[tuple[NetworkName, ...]] = tuple(PROFILES)

# Exact-host allowlists, built from the table only.
STRIKE_HOSTS: Final[frozenset[str]] = frozenset(
    urlsplit(p.strike_api_url).hostname or "" for p in PROFILES.values()
)
KOIOS_HOSTS: Final[frozenset[str]] = frozenset(p.koios_host for p in PROFILES.values())


class NetworkError(ValueError):
    pass


def profile(name: str) -> NetworkProfile:
    for p in PROFILES.values():
        if p.name == name:
            return p
    raise NetworkError(f"unknown network {name!r}: choose one of {', '.join(NETWORKS)}")


def checked_strike_url(url: str) -> str:
    """https:// or wss:// to one of exactly the two Strike hosts; loud otherwise."""
    parts = urlsplit(url)
    if parts.scheme not in ("https", "wss") or parts.hostname not in STRIKE_HOSTS:
        raise NetworkError(
            f"{url!r}: Strike URLs must be https/wss to exactly one of {sorted(STRIKE_HOSTS)}"
        )
    if parts.port is not None or parts.username or parts.password or parts.query or parts.fragment:
        raise NetworkError(f"{url!r}: no port, credentials, query or fragment on a Strike URL")
    return url


class _Setting(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    network: StrictStr


# Local, per-user selection (NOT the git-tracked config): <user config dir>/network.json
NETWORK_SETTING_PATH = user_config_dir() / "network.json"


def load_network_setting(path: Path = NETWORK_SETTING_PATH) -> NetworkName | None:
    """The user's Settings-tab choice, or None. Same rules as llm.json
    (``struck.private_file``): O_NOFOLLOW + fstat, owned by you, mode exactly
    0600, private parent directory."""
    try:
        data = read_private(path)
    except ValueError as exc:
        raise NetworkError(str(exc)) from exc
    if data is None:
        return None
    try:
        return profile(_Setting.model_validate_json(data).network).name
    except ValidationError as exc:
        raise NetworkError(
            f'{path} must be exactly {{"network": one of {list(NETWORKS)}}}: {exc}'
        ) from exc


def save_network_setting(name: NetworkName, path: Path = NETWORK_SETTING_PATH) -> None:
    """Atomic 0600 write (``struck.private_file``); unknown names are refused first."""
    chosen = profile(name).name
    try:
        write_private(path, (_Setting(network=chosen).model_dump_json() + "\n").encode("utf-8"))
    except ValueError as exc:
        raise NetworkError(str(exc)) from exc


def canonical_address(address: str, net: NetworkProfile) -> str:
    """Only a canonical bech32 address of the SELECTED network may enter a URL or
    a plan (stock pycardano decoder; HRP and network id from the table)."""
    from pycardano import Address
    from pycardano.exception import PyCardanoException

    try:
        parsed = Address.from_primitive(address)
    except PyCardanoException as exc:
        raise NetworkError(f"not a valid bech32 address: {address!r}: {exc}") from exc
    if (
        not address.startswith(net.address_prefix)
        or parsed.network != net.pycardano_network
        or parsed.encode() != address
    ):
        raise NetworkError(
            f"not a canonical {net.name} {net.address_prefix} bech32 address: {address!r}"
        )
    return address


def profile_for_strike_url(url: str) -> NetworkProfile:
    """The profile whose Strike REST base is EXACTLY ``url`` (allowlist); loud otherwise."""
    base = checked_strike_url(url.rstrip("/"))
    for p in PROFILES.values():
        if p.strike_api_url == base:
            return p
    raise NetworkError(f"{url!r} is not a Strike REST base URL ({sorted(STRIKE_HOSTS)})")


def profile_for_strike_venue(venue: str) -> NetworkProfile:
    for p in PROFILES.values():
        if p.strike_venue == venue:
            return p
    raise NetworkError(
        f"unknown Strike venue {venue!r}: one of {[p.strike_venue for p in PROFILES.values()]}"
    )
