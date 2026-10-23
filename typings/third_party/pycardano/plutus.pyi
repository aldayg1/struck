from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Mapping, Optional, Union

from .hash import DatumHash, ScriptHash
from .nativescript import NativeScript
from .serialization import CBORSerializable, IndefiniteList, RawCBOR

class PlutusScript(bytes, CBORSerializable):
    # plutus.py:1098 — resolves PlutusV{version}Script; ValueError otherwise.
    @classmethod
    def from_version(cls, version: int, script_data: bytes) -> PlutusScript: ...

class PlutusV1Script(PlutusScript): ...
class PlutusV2Script(PlutusScript): ...
class PlutusV3Script(PlutusScript): ...
class RawPlutusData(CBORSerializable): ...

@dataclass
class ExecutionUnits:
    mem: int
    steps: int

class RedeemerTag(CBORSerializable, Enum):
    SPEND = 0
    MINT = 1
    CERTIFICATE = 2
    WITHDRAWAL = 3
    VOTE = 4
    PROPOSE = 5

@dataclass
class Redeemer(CBORSerializable):
    tag: RedeemerTag
    index: int
    data: object
    ex_units: Optional[ExecutionUnits]
    def __init__(self, data: object, ex_units: Optional[ExecutionUnits] = ...) -> None: ...

Datum = Union[
    PlutusData,
    Mapping[object, object],
    int,
    bytes,
    IndefiniteList,
    RawCBOR,
    RawPlutusData,
]

class PlutusData(CBORSerializable):
    CONSTR_ID: int
    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "PlutusData": ...
    def to_dict(self) -> Dict[str, object]: ...
    def hash(self) -> DatumHash: ...
    @classmethod
    def from_cbor(cls, payload: str | bytes) -> "PlutusData": ...

def plutus_script_hash(script: Union[NativeScript, PlutusScript]) -> ScriptHash: ...

Redeemers = List[Redeemer]
