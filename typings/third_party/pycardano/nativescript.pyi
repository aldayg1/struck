from typing import List, Union

from .hash import ScriptHash, VerificationKeyHash
from .serialization import CBORSerializable

_SubScript = Union[
    "ScriptPubkey",
    "ScriptAll",
    "ScriptAny",
    "ScriptNofK",
    "InvalidBefore",
    "InvalidHereAfter",
]

class NativeScript(CBORSerializable):
    def hash(self) -> ScriptHash: ...
    # nativescript.py:67 — parses the cardano-cli/Koios native-script JSON.
    @classmethod
    def from_dict(cls, script_json: dict[str, object]) -> _SubScript: ...

class ScriptPubkey(NativeScript):
    def __init__(self, key_hash: VerificationKeyHash) -> None: ...
    key_hash: VerificationKeyHash

class ScriptAll(NativeScript):
    def __init__(self, native_scripts: List[_SubScript]) -> None: ...
    native_scripts: List[_SubScript]

class ScriptAny(NativeScript):
    def __init__(self, native_scripts: List[_SubScript]) -> None: ...
    native_scripts: List[_SubScript]

class ScriptNofK(NativeScript):
    def __init__(self, n: int, native_scripts: List[_SubScript]) -> None: ...
    n: int
    native_scripts: List[_SubScript]

class InvalidBefore(NativeScript):
    def __init__(self, before: int) -> None: ...
    before: int

class InvalidHereAfter(NativeScript):
    def __init__(self, after: int) -> None: ...
    after: int
