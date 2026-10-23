from typing import Optional

from .base import ChainContext

__all__ = ["KupoChainContextExtension"]

class KupoChainContextExtension(ChainContext):
    # Runtime signature (pycardano 0.19.2, backend/kupo.py:49-56). The
    # extension serves utxos/datums from Kupo and delegates everything else
    # (tip, params, submit, evaluate) to ``wrapped_backend``.
    def __init__(
        self,
        wrapped_backend: ChainContext,
        kupo_url: Optional[str] = ...,
        refetch_chain_tip_interval: int = ...,
        utxo_cache_size: int = ...,
        datum_cache_size: int = ...,
    ) -> None: ...
