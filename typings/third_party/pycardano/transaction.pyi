from typing import List, Mapping, Optional, Union

from .address import Address
from .hash import (
    AuxiliaryDataHash,
    DatumHash,
    ScriptDataHash,
    ScriptHash,
    TransactionId,
    VerificationKeyHash,
)
from .metadata import AlonzoMetadata, Metadata, ShelleyMarryMetadata
from .nativescript import NativeScript
from .network import Network
from .plutus import Datum, PlutusScript, PlutusV1Script, PlutusV2Script, PlutusV3Script
from .serialization import CBORSerializable
from .witness import VerificationKeyWitness

class TransactionInput(CBORSerializable):
    transaction_id: TransactionId
    index: int
    def __init__(self, transaction_id: TransactionId, index: int) -> None: ...
    def __eq__(self, other: object) -> bool: ...
    def __hash__(self) -> int: ...

class AssetName(CBORSerializable):
    MAX_SIZE: int
    MIN_SIZE: int
    payload: bytes
    def __init__(self, payload: bytes) -> None: ...
    @classmethod
    def from_primitive(cls, value: object) -> AssetName: ...
    def to_primitive(self) -> bytes: ...
    def __eq__(self, other: object) -> bool: ...
    def __hash__(self) -> int: ...

class Asset(dict[AssetName, int]):
    @classmethod
    def from_primitive(cls, value: Mapping[object, object]) -> Asset: ...
    def union(self, other: Asset) -> Asset: ...
    def normalize(self) -> Asset: ...
    def __add__(self, other: Asset) -> Asset: ...
    def __iadd__(self, other: Asset) -> Asset: ...
    def __sub__(self, other: Asset) -> Asset: ...
    def __le__(self, other: Asset) -> bool: ...
    def __lt__(self, other: Asset) -> bool: ...
    def __ge__(self, other: Asset) -> bool: ...
    def __gt__(self, other: Asset) -> bool: ...
    def __eq__(self, other: object) -> bool: ...

class MultiAsset(dict[ScriptHash, Asset]):
    @classmethod
    def from_primitive(cls, value: Mapping[object, object]) -> MultiAsset: ...
    def union(self, other: MultiAsset) -> MultiAsset: ...
    def normalize(self) -> MultiAsset: ...
    def __add__(self, other: MultiAsset) -> MultiAsset: ...
    def __iadd__(self, other: MultiAsset) -> MultiAsset: ...
    def __sub__(self, other: MultiAsset) -> MultiAsset: ...
    def filter(self, criteria: object) -> MultiAsset: ...
    def count(self, criteria: object) -> int: ...
    def __eq__(self, other: object) -> bool: ...

class Value(CBORSerializable):
    coin: int
    multi_asset: MultiAsset
    def __init__(self, coin: int = ..., multi_asset: MultiAsset = ...) -> None: ...
    def union(self, other: Union[Value, int]) -> Value: ...
    def __add__(self, other: Union[Value, int]) -> Value: ...
    def __iadd__(self, other: Union[Value, int]) -> Value: ...
    def __sub__(self, other: Union[Value, int]) -> Value: ...
    def __le__(self, other: Union[Value, int]) -> bool: ...
    def __lt__(self, other: Union[Value, int]) -> bool: ...
    def __ge__(self, other: Union[Value, int]) -> bool: ...
    def __gt__(self, other: Union[Value, int]) -> bool: ...

class TransactionOutput(CBORSerializable):
    address: Address
    amount: Value
    datum_hash: Optional[DatumHash]
    datum: Optional[Datum]
    script: Optional[Union[NativeScript, PlutusScript]]
    post_alonzo: Optional[bool]
    def __init__(
        self,
        # Runtime truth: __post_init__ coerces a bech32 str to Address and a
        # bare lovelace int to Value.
        address: Union[Address, str],
        amount: Union[Value, int],
        datum_hash: Optional[DatumHash] = ...,
        datum: Optional[Datum] = ...,
        script: Optional[Union[NativeScript, PlutusScript]] = ...,
        post_alonzo: Optional[bool] = ...,
    ) -> None: ...
    @property
    def lovelace(self) -> int: ...

class UTxO(CBORSerializable):
    input: TransactionInput
    output: TransactionOutput
    def __init__(self, input: TransactionInput, output: TransactionOutput) -> None: ...

class TransactionBody(CBORSerializable):
    inputs: List[TransactionInput]
    outputs: List[TransactionOutput]
    fee: int
    ttl: Optional[int]
    mint: Optional[MultiAsset]
    auxiliary_data_hash: Optional[AuxiliaryDataHash]
    script_data_hash: Optional[ScriptDataHash]
    required_signers: Optional[List[VerificationKeyHash]]
    network_id: Optional[Network]
    collateral: Optional[List[TransactionInput]]
    collateral_return: Optional[TransactionOutput]
    total_collateral: Optional[int]
    reference_inputs: Optional[List[TransactionInput]]
    validity_start: Optional[int]
    def __init__(
        self,
        inputs: List[TransactionInput] = ...,
        outputs: List[TransactionOutput] = ...,
        fee: int = ...,
        ttl: Optional[int] = ...,
        auxiliary_data_hash: Optional[AuxiliaryDataHash] = ...,
        validity_start: Optional[int] = ...,
        mint: Optional[MultiAsset] = ...,
        script_data_hash: Optional[ScriptDataHash] = ...,
        collateral: Optional[List[TransactionInput]] = ...,
        required_signers: Optional[List[VerificationKeyHash]] = ...,
        network_id: Optional[Network] = ...,
        collateral_return: Optional[TransactionOutput] = ...,
        total_collateral: Optional[int] = ...,
        reference_inputs: Optional[List[TransactionInput]] = ...,
    ) -> None: ...
    def hash(self) -> bytes: ...
    @property
    def id(self) -> TransactionId: ...

class TransactionWitnessSet(CBORSerializable):
    vkey_witnesses: Optional[List[VerificationKeyWitness]]
    native_scripts: Optional[List[NativeScript]]
    plutus_v1_script: Optional[List[PlutusV1Script]]
    plutus_v2_script: Optional[List[PlutusV2Script]]
    plutus_v3_script: Optional[List[PlutusV3Script]]
    def __init__(
        self,
        vkey_witnesses: Optional[List[VerificationKeyWitness]] = ...,
        native_scripts: Optional[List[NativeScript]] = ...,
        plutus_v1_script: Optional[List[PlutusV1Script]] = ...,
        plutus_v2_script: Optional[List[PlutusV2Script]] = ...,
        plutus_v3_script: Optional[List[PlutusV3Script]] = ...,
    ) -> None: ...

class Transaction(CBORSerializable):
    transaction_body: TransactionBody
    transaction_witness_set: TransactionWitnessSet
    valid: Optional[bool]
    auxiliary_data: Optional["AuxiliaryData"]
    def __init__(
        self,
        transaction_body: TransactionBody,
        transaction_witness_set: TransactionWitnessSet,
        valid: Optional[bool] = ...,
        auxiliary_data: Optional["AuxiliaryData"] = ...,
    ) -> None: ...
    @property
    def id(self) -> TransactionId: ...

class AuxiliaryData(CBORSerializable):
    data: Union[Metadata, ShelleyMarryMetadata, AlonzoMetadata]
    def __init__(self, data: Union[Metadata, ShelleyMarryMetadata, AlonzoMetadata]) -> None: ...
