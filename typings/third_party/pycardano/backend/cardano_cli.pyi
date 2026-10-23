from enum import Enum
from pathlib import Path
from typing import Optional

from .base import ChainContext

__all__ = ["CardanoCliChainContext", "CardanoCliNetwork", "DockerConfig"]

class CardanoCliNetwork(Enum):
    MAINNET = ...
    TESTNET = ...
    PREVIEW = ...
    PREPROD = ...
    GUILDNET = ...
    CUSTOM = ...

class DockerConfig:
    container_name: str
    host_socket: Optional[Path]
    def __init__(self, container_name: str, host_socket: Optional[Path] = ...) -> None: ...

class CardanoCliChainContext(ChainContext):
    # Runtime signature (pycardano 0.19.2, backend/cardano_cli.py:115-126).
    # Stock gaps, verified against the source: ``__init__`` sets
    # ``os.environ["CARDANO_NODE_SOCKET_PATH"]`` process-wide; the protocol
    # parameters it builds never fill ``maximum_reference_scripts_size`` /
    # ``min_fee_reference_scripts`` and drop PlutusV3 cost models;
    # ``evaluate_tx_cbor`` raises ``NotImplementedError``.
    def __init__(
        self,
        binary: Path,
        socket: Path,
        config_file: Path,
        network: CardanoCliNetwork,
        refetch_chain_tip_interval: Optional[float] = ...,
        utxo_cache_size: int = ...,
        datum_cache_size: int = ...,
        docker_config: Optional[DockerConfig] = ...,
        network_magic_number: Optional[int] = ...,
    ) -> None: ...
