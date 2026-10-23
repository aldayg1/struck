"""Stub for pycardano.utils (new-file addition; the runtime module ships no
annotations and had no stub here)."""

from .backend.base import ChainContext
from .transaction import TransactionOutput

def min_lovelace_post_alonzo(output: TransactionOutput, context: ChainContext) -> int: ...

__all__ = ["min_lovelace_post_alonzo"]
