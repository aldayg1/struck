"""Stub for pycardano.certificate (new-file addition; the runtime module ships
no annotations and had no stub here).  Signatures mirror the runtime
dataclasses.  ``PoolKeyHash`` and ``Certificate`` are re-exported names of
``pycardano.certificate`` at runtime, so declaring them here is faithful."""

from typing import Union

from .hash import ScriptHash, VerificationKeyHash
from .serialization import CBORSerializable

# Re-exported by pycardano.certificate at runtime (imported from .hash).
PoolKeyHash = VerificationKeyHash

class StakeCredential(CBORSerializable):
    def __init__(self, credential: Union[VerificationKeyHash, ScriptHash]) -> None: ...
    credential: Union[VerificationKeyHash, ScriptHash]

class StakeRegistration(CBORSerializable):
    def __init__(self, stake_credential: StakeCredential) -> None: ...
    stake_credential: StakeCredential

class StakeDeregistration(CBORSerializable):
    def __init__(self, stake_credential: StakeCredential) -> None: ...
    stake_credential: StakeCredential

class StakeDelegation(CBORSerializable):
    def __init__(self, stake_credential: StakeCredential, pool_keyhash: PoolKeyHash) -> None: ...
    stake_credential: StakeCredential
    pool_keyhash: PoolKeyHash

Certificate = Union[StakeRegistration, StakeDeregistration, StakeDelegation]

__all__ = [
    "Certificate",
    "PoolKeyHash",
    "StakeCredential",
    "StakeDelegation",
    "StakeDeregistration",
    "StakeRegistration",
]
