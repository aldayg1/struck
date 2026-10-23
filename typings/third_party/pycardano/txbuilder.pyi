from typing import Dict, List, MutableSequence, Optional, Set, Union

from .address import Address
from .hash import DatumHash, ScriptDataHash, VerificationKeyHash
from .key import ExtendedSigningKey, SigningKey
from .nativescript import NativeScript
from .plutus import Datum, PlutusScript, Redeemer
from .transaction import (
    AuxiliaryData,
    MultiAsset,
    Transaction,
    TransactionBody,
    TransactionOutput,
    TransactionWitnessSet,
    UTxO,
)

class TransactionBuilder:
    collaterals: MutableSequence[UTxO]
    auxiliary_data: Optional[AuxiliaryData]
    native_scripts: Optional[List[NativeScript]]
    mint: Optional[MultiAsset]
    ttl: Optional[int]
    validity_start: Optional[int]
    required_signers: Optional[List[VerificationKeyHash]]
    @property
    def scripts(self) -> List[object]: ...
    def __init__(
        self,
        context: object = ...,
        execution_engine_buffer: float = ...,
        ttl: Optional[int] = ...,
        validity_start: Optional[int] = ...,
        auxiliary_data: Optional[object] = ...,
        mint: Optional[object] = ...,
        required_signers: Optional[Set[VerificationKeyHash]] = ...,
        collateral_inputs: Optional[List[UTxO]] = ...,
        collateral_return: Optional[TransactionOutput] = ...,
        total_collateral: Optional[int] = ...,
        withdrawals: Optional[Dict[Address, int]] = ...,
    ) -> None: ...
    def add_input(self, utxo: UTxO) -> TransactionBuilder: ...
    def add_input_address(self, address: Union[Address, str]) -> TransactionBuilder: ...
    def add_output(
        self,
        tx_out: TransactionOutput,
        datum: Optional[Datum] = ...,
        add_datum_to_witness: bool = ...,
    ) -> TransactionBuilder: ...
    def add_script_input(
        self,
        utxo: UTxO,
        script: Optional[Union[UTxO, NativeScript, PlutusScript]] = ...,
        datum: Optional[Datum] = ...,
        redeemer: Optional[Redeemer] = ...,
    ) -> TransactionBuilder: ...
    def add_minting_script(
        self,
        script: Union[UTxO, NativeScript, PlutusScript],
        redeemer: Optional[Redeemer] = ...,
    ) -> TransactionBuilder: ...
    def add_withdrawal_script(
        self,
        script: Union[UTxO, NativeScript, PlutusScript],
        redeemer: Optional[Redeemer] = ...,
    ) -> TransactionBuilder: ...
    def build(
        self,
        change_address: Optional[Address] = ...,
        merge_change: Optional[bool] = ...,
        collateral_change_address: Optional[Address] = ...,
        auto_validity_start_offset: Optional[int] = ...,
        auto_ttl_offset: Optional[int] = ...,
        auto_required_signers: Optional[bool] = ...,
    ) -> TransactionBody: ...
    def build_and_sign(
        self,
        signing_keys: List[Union[SigningKey, ExtendedSigningKey]],
        change_address: Optional[Address] = ...,
        merge_change: Optional[bool] = ...,
        collateral_change_address: Optional[Address] = ...,
        auto_validity_start_offset: Optional[int] = ...,
        auto_ttl_offset: Optional[int] = ...,
        auto_required_signers: Optional[bool] = ...,
        force_skeys: Optional[bool] = ...,
    ) -> Transaction: ...
    def build_witness_set(self, remove_dup_script: bool = ...) -> TransactionWitnessSet: ...
    @property
    def inputs(self) -> List[UTxO]: ...
    @property
    def potential_inputs(self) -> List[UTxO]: ...
    @property
    def excluded_inputs(self) -> List[UTxO]: ...
    @excluded_inputs.setter
    def excluded_inputs(self, excluded_inputs: List[UTxO]) -> None: ...
    @property
    def input_addresses(self) -> List[Union[Address, str]]: ...
    @property
    def outputs(self) -> List[TransactionOutput]: ...
    @property
    def fee(self) -> int: ...
    @fee.setter
    def fee(self, fee: int) -> None: ...
    @property
    def datums(self) -> Dict[DatumHash, Datum]: ...
    @property
    def script_data_hash(self) -> Optional[ScriptDataHash]: ...
