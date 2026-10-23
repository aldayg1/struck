"""The ONE Struck chain/venue config reader (``config/struck.json``).

Values were copied from 
(``chains.cardano.networks.preprod``) on 2026-10-21; Ogmios is addressed over
its HTTP JSON-RPC interface on the same port as its websocket.

``network`` is ONE setting naming a row of the frozen table
``struck.network.PROFILES`` (same code, different endpoints); Strike REST/WS URLs, the network magic, the Koios URL and the
address network all come from that row. It is selected by the ``--network``
CLI flag, else the Settings tab's per-user ``<user config dir>/network.json``,
else the ``network`` field of the local (gitignored) ``config/struck.json``.
There is NO code default: with none of the three set, every entry point fails
loudly naming all three. There is no silent switch: a real-funds network
happens only when the user selects it.

``node_socket`` maps EVERY network name to its node socket or ``null``; the
node backend on a network whose socket is ``null`` fails loudly.

``chain_backend`` is the explicit toggle. Each value
is a complete, self-sufficient path: ``node`` uses cardano-cli + Ogmios +
Kupo, ``koios`` uses the ported KoiosChainContext + pycardano. A backend
never calls the other one, and nothing falls back. The CLI ``--backend``
flag overrides the file for one run.
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

from . import paths
from .private_file import (
    not_group_writable,
    checked_executable,
    checked_node_socket,
    read_private,
    write_private,
)
from .network import (
    KOIOS_HOSTS,
    NETWORK_SETTING_PATH,
    NETWORKS,
    NetworkError,
    NetworkName,
    NetworkProfile,
    checked_strike_url,
    load_network_setting,
    profile,
)
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, field_validator

REPO_ROOT = paths.REPO_ROOT # package-relative, never the cwd
CONFIG_PATH = REPO_ROOT / "config" / "struck.json"
CALL_HISTORY_PATH = REPO_ROOT / "logs" / "call_history.jsonl"
RECEIPTS_DIR = REPO_ROOT / "receipts"
# LLM choice is per-user, NOT in the git-tracked chain/venue config.
LLM_CONFIG_PATH = paths.user_config_dir() / "llm.json"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_host(url: str, host: str) -> None:
    """Exact-name hosts only: no trailing dot, no IDN/punycode, no numeric
    host spellings that are not a canonical IP (2130706433, 127.1, 0x7f.1)."""
    if host.endswith("."):
        raise ValueError(f"{url!r}: trailing-dot host {host!r} is not allowed")
    if not host.isascii() or any(label.startswith("xn--") for label in host.split(".")):
        raise ValueError(f"{url!r}: internationalized (IDN/punycode) host {host!r} is not allowed")
    try:
        ipaddress.ip_address(host)
        return # a canonical IPv4/IPv6 literal
    except ValueError:
        pass
    last = host.rsplit(".", 1)[-1]
    if last.isdigit() or last.startswith("0x") or all(c in "0123456789." for c in host):
        raise ValueError(f"{url!r}: all-numeric host {host!r} is not a canonical IP address")


def checked_url(url: str, *, loopback_only: bool = False, https_only: bool = False) -> str:
    """Stock urllib parse; loud ValueError naming the rule that failed.
    https:// anywhere (except link-local metadata hosts), or http:// to loopback."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{url!r}: credentials (user:pass@) are not allowed in a URL")
    if parts.query or parts.fragment or url.rstrip().endswith(("?", "#")):
        raise ValueError(f"{url!r}: query strings and fragments are not allowed")
    if not host:
        raise ValueError(f"{url!r}: no host")
    _check_host(url, host)
    try:
        if ipaddress.ip_address(host).is_link_local:
            raise ValueError(f"{url!r}: link-local hosts (e.g. 169.254.169.254) are not allowed")
    except ValueError as exc:
        if "link-local" in str(exc):
            raise
    if loopback_only and not _is_loopback(host):
        raise ValueError(f"{url!r}: must be a loopback host (localhost / 127.x / ::1)")
    if parts.scheme == "https":
        return url
    if parts.scheme == "http" and not https_only and _is_loopback(host):
        return url
    if parts.scheme == "http":
        raise ValueError(
            f"{url!r}: plain http:// is allowed only to loopback"
            + ("" if not https_only else "; this URL must be https://")
        )
    raise ValueError(
        f"{url!r}: scheme {parts.scheme!r} is not allowed (https://, or http:// to loopback)"
    )


LlmProvider = Literal[
    "openai",
    "anthropic",
    "google_genai",
    "google_vertexai",
    "bedrock_converse",
    "azure_openai",
    "mistralai",
    "groq",
    "xai",
    "deepseek",
    "fireworks",
    "together",
    "cohere",
    "perplexity",
    "openrouter",
    "ollama",
    "vllm",
]


class LlmConfig(BaseModel):
    """LLM provider (struck/plan/providers.py). Keys are NEVER here: they live
    only in Struck's encrypted secret store (``llm_<provider>``, struck.secrets)."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    provider: LlmProvider
    model: StrictStr
    base_url: StrictStr | None # required for vllm / ollama / azure_openai; no default
    # A hosted provider at a NON-official host needs this explicit opt-in, and then
    # is reachable only over https.
    allow_custom_host: StrictBool = False

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, v: str | None) -> str | None:
        # Every path (Settings, chat/cli --base-url, a hand-edited config) builds an
        # LlmConfig, so a provider key can only ever go to a URL that passed this.
        return None if v is None else checked_url(v)


class ConfigError(ValueError):
    """The local config is missing or invalid: Struck stops (fail closed)."""


class StruckConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    network: NetworkName
    chain_backend: Literal["node", "koios"]
    node_socket: dict[NetworkName, StrictStr | None] # EVERY network name; null = no node
    cardano_cli_path: StrictStr
    kupo_url: StrictStr
    ogmios_url: StrictStr
    timeout_s: StrictInt

    @field_validator("network")
    @classmethod
    def _network(cls, v: str) -> str:
        return profile(v).name # a row of struck.network.PROFILES, else loud

    @field_validator("node_socket")
    @classmethod
    def _node_socket(cls, v: dict[str, str | None]) -> dict[str, str | None]:
        if set(v) != set(NETWORKS):
            raise ValueError(
                f"node_socket must name every network exactly ({', '.join(NETWORKS)}), each a "
                f"socket path or null; got {sorted(v)}"
            )
        for name, sock in v.items():
            if sock is not None:
                try:
                    checked_node_socket(sock)
                except ValueError as exc:
                    raise ValueError(f"node_socket.{name}: {exc}") from exc
        return v

    @property
    def node_socket_path(self) -> str:
        """The SELECTED network's node socket; null = the node backend is refused."""
        sock = self.node_socket[self.network]
        if sock is None:
            raise ConfigError(
                f"node_socket.{self.network} is null in config/struck.json: no node is configured "
                f"for {self.profile.label}. Set its socket path, or use --backend koios."
            )
        return sock

    @property
    def checked_cardano_cli(self) -> str:
        """cardano-cli, permission-checked at the point of use (node backend only).

        Configured absolute path only, never PATH. The koios backend never runs
        cardano-cli, so a koios run is not refused over this binary; every
        node-backend run checks it (and its folder) before the first spawn.
        """
        try:
            return checked_executable(self.cardano_cli_path)
        except ValueError as exc:
            raise ConfigError(f"cardano_cli_path: {exc}") from exc

    @field_validator("kupo_url", "ogmios_url")
    @classmethod
    def _local_url(cls, v: str) -> str:
        return checked_url(v, loopback_only=True)

    @property
    def profile(self) -> NetworkProfile:
        return profile(self.network)

    @property
    def strike_api_url(self) -> str:
        return checked_strike_url(self.profile.strike_api_url)

    @property
    def koios_url(self) -> str:
        url = checked_url(self.profile.koios_url, https_only=True)
        if urlsplit(url).hostname not in KOIOS_HOSTS:
            raise ValueError(f"{url!r}: Koios host must be one of {sorted(KOIOS_HOSTS)}")
        return url

    @property
    def cardano_magic(self) -> int:
        return self.profile.cardano_magic


_SELECTED: list[NetworkName] = [] # pinned once per process (--network, or first resolution)


def select_network(name: str) -> NetworkName:
    """The CLI ``--network`` flag: overrides the Settings choice for this process."""
    chosen = profile(name).name
    _SELECTED[:] = [chosen]
    return chosen


NO_NETWORK = (
    "NO NETWORK SELECTED: Struck has no default network. Set it one of three ways: "
    "(1) --network <name> on the command line; (2) the Settings tab (saves "
    '{setting}); (3) "network": "<name>" in config/struck.json. '
    "Names: {names}. Nothing was run."
)


def selected_network(file_network: str | None) -> NetworkName:
    """--network flag, else <user config dir>/network.json, else the local config's
    ``network`` field; none = loud (no code default). Resolved ONCE per process
    and pinned: a Settings change applies on the next start, never under a
    running session or a pending approval."""
    if not _SELECTED:
        chosen = load_network_setting()
        if chosen is None and file_network is not None:
            chosen = profile(file_network).name
        if chosen is None:
            raise ConfigError(
                NO_NETWORK.format(names=", ".join(NETWORKS), setting=NETWORK_SETTING_PATH)
            )
        _SELECTED[:] = [chosen]
    return _SELECTED[0]


CONFIG_KEYS = tuple(StruckConfig.model_fields)


def load_config(path: Path = CONFIG_PATH) -> StruckConfig:
    """``config/struck.json`` (local, untracked, endpoints only). Missing = loud;
    there is no default, example or template config."""
    try:
        data_bytes = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(
            f"{path} is missing. Struck needs this local config (not tracked in git; no "
            f"default): a JSON object with {', '.join(CONFIG_KEYS)}. Nothing was run."
        ) from exc
    try:
        not_group_writable(path) # the file selects the network and the binaries
        not_group_writable(path.parent)
    except ValueError as exc:
        raise ConfigError(f"{exc}. Nothing was run.") from exc
    raw = cast("object", json.loads(data_bytes))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: not a JSON object")
    data = cast("dict[str, object]", raw)
    file_network = data.get("network")
    if file_network is not None and not isinstance(file_network, str):
        raise ConfigError(f"{path}: network must be a string naming one of {', '.join(NETWORKS)}")
    try:
        data["network"] = selected_network(file_network)
    except NetworkError as exc:
        raise ConfigError(f"{path}: network: {exc}") from exc
    return StruckConfig.model_validate_json(json.dumps(data))


def load_llm_config(path: Path = LLM_CONFIG_PATH) -> LlmConfig | None:
    """The user's LLM choice (None = not configured). ``struck.private_file``:
    O_NOFOLLOW + fstat, owned by you, mode exactly 0600, private parent."""
    data = read_private(path)
    return None if data is None else LlmConfig.model_validate_json(data)


def save_llm_config(llm: LlmConfig, path: Path = LLM_CONFIG_PATH) -> None:
    """Atomic: mkstemp in the (private) parent, fchmod 0600, write, fsync, os.replace."""
    write_private(path, (llm.model_dump_json(indent=2) + "\n").encode("utf-8"))


def pop_network_flag(argv: list[str], *, check_config: bool = True) -> list[str]:
    """Consume ``--network <name>`` (a ``struck.network.PROFILES`` row; or ``--network=...``) from argv,
    select it for this process, and return the remaining args. Unknown = loud.

    ``check_config=False`` (the UI only): skip the load_config refusal so the
    first-run REGISTRATION DIALOGUE can open instead of a stderr exit; every
    CLI keeps the loud fail-closed check."""
    rest: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg == "--network" or arg.startswith("--network="):
            value = next(it, None) if arg == "--network" else arg.split("=", 1)[1]
            if value is None:
                raise SystemExit(f"--network needs a value: one of {', '.join(NETWORKS)}")
            try:
                select_network(value)
            except NetworkError as exc:
                raise SystemExit(f"--network: {exc}") from exc
        else:
            rest.append(arg)
    if check_config:
        try:
            load_config() # every entry point fails closed on a missing/invalid local config
        except (ValueError, OSError) as exc:
            raise SystemExit(f"STRUCK CONFIG REFUSED: {exc}") from exc
    return rest


def network_banner(cfg: StruckConfig) -> str:
    """The line every approval prompt prints first: the selected network, named
    plainly (no editorializing — ."""
    return f"NETWORK: {cfg.profile.label}"


def network_help() -> str:
    """The ``--network`` line every CLI usage/help prints (names from the table)."""
    return (
        f" --network <name> one of: {', '.join(NETWORKS)} (rows of struck.network.PROFILES); "
        f"default: the Settings choice ({NETWORK_SETTING_PATH}), else config/struck.json"
    )


def wants_help(argv: list[str]) -> bool:
    return "-h" in argv or "--help" in argv
