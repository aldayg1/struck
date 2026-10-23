from typing import Optional

from ..network import Network
from .base import ALONZO_COINS_PER_UTXO_WORD, ChainContext, GenesisParameters, ProtocolParameters
from .blockfrost import BlockFrostChainContext
from .cardano_cli import CardanoCliChainContext, CardanoCliNetwork, DockerConfig
from .kupo import KupoChainContextExtension

# Mirrors pycardano/backend/__init__.py (0.19.2): ``from .base/.blockfrost/
# .cardano_cli/.ogmios_v6 import *``. ``KupoChainContextExtension`` is NOT
# re-exported by the real package (kupo.py is reached only through
# ``KupoOgmiosV6ChainContext``'s return value or ``pycardano.backend.kupo``),
# so it is deliberately absent from ``__all__`` here.
__all__ = [
    "ALONZO_COINS_PER_UTXO_WORD",
    "BlockFrostChainContext",
    "CardanoCliChainContext",
    "CardanoCliNetwork",
    "ChainContext",
    "DockerConfig",
    "GenesisParameters",
    "KupoOgmiosV6ChainContext",
    "OgmiosChainContext",
    "OgmiosV6ChainContext",
    "ProtocolParameters",
]

class OgmiosV6ChainContext(ChainContext):
    def __init__(
        self,
        host: str = ...,
        port: int = ...,
        path: str = ...,
        secure: bool = ...,
        refetch_chain_tip_interval: Optional[float] = ...,
        utxo_cache_size: int = ...,
        datum_cache_size: int = ...,
        network: Network = ...,
        additional_headers: Optional[dict[str, str]] = ...,
    ) -> None: ...

OgmiosChainContext = OgmiosV6ChainContext

def KupoOgmiosV6ChainContext(
    host: str,
    port: int,
    path: str,
    secure: bool,
    refetch_chain_tip_interval: Optional[float] = ...,
    utxo_cache_size: int = ...,
    datum_cache_size: int = ...,
    network: Network = ...,
    additional_headers: Optional[dict[str, str]] = ...,
    kupo_url: Optional[str] = ...,
) -> KupoChainContextExtension: ...
