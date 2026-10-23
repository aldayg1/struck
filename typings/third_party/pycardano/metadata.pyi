from dataclasses import dataclass
from typing import List, Mapping, Optional

from .nativescript import NativeScript
from .plutus import PlutusV1Script, PlutusV2Script, PlutusV3Script
from .serialization import CBORSerializable

class Metadata(dict[int, object]):
    """CIP-10 transaction metadata: first-level int labels, nested CBOR values."""

    MAX_ITEM_SIZE: int
    def __init__(self, data: Mapping[int, object] = ..., **kwargs: object) -> None: ...

@dataclass
class ShelleyMarryMetadata(CBORSerializable):
    metadata: Metadata
    native_scripts: Optional[List[NativeScript]] = ...

@dataclass
class AlonzoMetadata(CBORSerializable):
    metadata: Optional[Metadata] = ...
    native_scripts: Optional[List[NativeScript]] = ...
    plutus_v1_scripts: Optional[List[PlutusV1Script]] = ...
    plutus_v2_scripts: Optional[List[PlutusV2Script]] = ...
    plutus_v3_scripts: Optional[List[PlutusV3Script]] = ...

__all__ = [
    "AlonzoMetadata",
    "Metadata",
    "ShelleyMarryMetadata",
]
