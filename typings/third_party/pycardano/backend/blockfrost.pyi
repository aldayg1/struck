from typing import Optional

from ..network import Network
from ..plutus import PlutusScript
from .base import ChainContext

__all__ = ["BlockFrostChainContext"]

# blockfrost.py:39 — module-level helper pycardano's own kupo backend imports:
# returns ``script`` when its hash matches ``scripth``, else the CBOR-unwrapped
# script when THAT matches; ValueError otherwise.
def _try_fix_script(scripth: str, script: PlutusScript) -> PlutusScript: ...

class BlockFrostChainContext(ChainContext):
    # Runtime signature (pycardano 0.19.2, backend/blockfrost.py:65-70).
    # ``network`` is deprecated upstream; pass ``base_url`` (blockfrost-python
    # ``ApiUrls``: https://cardano-<net>.blockfrost.io/api).
    def __init__(
        self,
        project_id: str,
        network: Optional[Network] = ...,
        base_url: Optional[str] = ...,
    ) -> None: ...
