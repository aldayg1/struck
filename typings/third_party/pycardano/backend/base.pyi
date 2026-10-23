from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, List, Optional

from ..network import Network
from ..plutus import ExecutionUnits
from ..transaction import Transaction, UTxO

ALONZO_COINS_PER_UTXO_WORD: int

@dataclass
class ProtocolParameters:
    min_fee_constant: int
    min_fee_coefficient: int
    max_block_size: int
    max_tx_size: int
    max_block_header_size: int
    key_deposit: int
    pool_deposit: int
    pool_influence: Fraction
    monetary_expansion: Fraction
    treasury_expansion: Fraction
    decentralization_param: Fraction
    extra_entropy: str
    protocol_major_version: int
    protocol_minor_version: int
    min_utxo: int
    min_pool_cost: int
    price_mem: Fraction
    price_step: Fraction
    max_tx_ex_mem: int
    max_tx_ex_steps: int
    max_block_ex_mem: int
    max_block_ex_steps: int
    max_val_size: int
    collateral_percent: int
    max_collateral_inputs: int
    coins_per_utxo_word: int
    coins_per_utxo_byte: int
    # Upstream annotates Dict[str, Dict[str, int]]. Its cardano-cli backend
    # fills integer keys (list position) and CostModels sorts keys before
    # CBOR-encoding, so list-form models (cardano-cli ≥ 8.x, Koios
    # /epoch_params) are int-keyed. Dict is invariant, so the field accepts
    # either keying.
    cost_models: (
        Dict[str, Dict[str, int]] | Dict[str, Dict[int, int]] | Dict[str, Dict[str | int, int]]
    )
    # THE v7 BLEED POINT. pycardano fills these from
    # `ogmios.datatypes.ProtocolParameters`, whose __init__ takes an EXPLICIT
    # keyword list with no **kwargs. The bare-metal server at
    # /mnt/cardano-node/bin/ogmios is v7.0.0 and emits
    # `maxReferenceScriptsSizePerTransaction` (+ a new
    # `maxReferenceScriptsSizePerBlock`); stock ogmios 1.4.3 still declares
    # `maxReferenceScriptsSize`. So `ProtocolParameters(**result)` raises
    # TypeError before either field below is ever populated — the failure is a
    # runtime wire-shape mismatch inside the client, which NO stub can catch
    # statically. Optional[...] here is the real pycardano signature, not a
    # concession: `tiered_reference_script_fee` (pycardano/utils.py:43-46)
    # returns 0 when either is None, so if the client is ever made to tolerate
    # the rename WITHOUT mapping it, reference-script fees silently become 0
    # and transactions are rejected for too-small fees. Map it or fail; never
    # let it reach None quietly.
    maximum_reference_scripts_size: Optional[Dict[str, int]] = ...
    min_fee_reference_scripts: Optional[Dict[str, float]] = ...

@dataclass
class GenesisParameters:
    active_slots_coefficient: Fraction
    update_quorum: int
    max_lovelace_supply: int
    network_magic: int
    epoch_length: int
    system_start: int
    slots_per_kes_period: int
    slot_length: int
    max_kes_evolutions: int
    security_param: int

class ChainContext:
    @property
    def network(self) -> Network: ...
    @property
    def epoch(self) -> int: ...
    @property
    def last_block_slot(self) -> int: ...
    @property
    def genesis_param(self) -> GenesisParameters: ...
    @property
    def protocol_param(self) -> ProtocolParameters: ...
    def utxos(self, address: str) -> List[UTxO]: ...
    def _utxos(self, address: str) -> List[UTxO]: ...
    def submit_tx(self, tx: Transaction) -> str: ...
    def submit_tx_cbor(self, cbor: bytes | str) -> str: ...
    def evaluate_tx(self, tx: Transaction) -> Dict[str, ExecutionUnits]: ...
    def evaluate_tx_cbor(self, cbor: bytes | str) -> Dict[str, ExecutionUnits]: ...
