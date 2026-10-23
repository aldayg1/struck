"""pycardano ``ChainContext`` served by Koios: a PORT of
``KoiosChainContext`` from 

Source: ``~/
is UNTRACKED there (``??``), so it is ported from the WORKING TREE, sha256
867b0c02659d1e24114aaabe8a4a86ec6daaef80c8f7a4d97bdab8685271b0c4, snapshot
taken 2026-10-21 11:30 CDMX. It is the context the 
with when ``use_remote_api`` is true, and that is the current preprod toggle
(``remote_order`` = koios, blockfrost) as of 2026-10-21 11:28.

Ported verbatim: ``_cost_models``, ``_script_version``, ``_reference_script``,
the ref-script fee constants, and the ``KoiosChainContext`` body.

Struck adaptations (each one is named here):
* The constructor takes the ported ``KoiosClient`` (recorded transport)
  instead of a base URL, so every chain read lands in the call history.
* ``submit_tx_cbor`` raises: the context never submits; the pipeline does (``struck.chain.backends`` ``submit``).
* Not ported: ``FailoverChainContext``, the local cli/Kupo leg, Blockfrost
  (it needs a SecretStore key) and the dated-UTxO helpers. Struck picks
  exactly one context by explicit config, and there is no failover
  (rules 15/16).
"""

from __future__ import annotations

import time
from typing import Literal

from pycardano import (
    ALONZO_COINS_PER_UTXO_WORD,
    Address,
    Asset,
    AssetName,
    ChainContext,
    DatumHash,
    ExecutionUnits,
    GenesisParameters,
    MultiAsset,
    NativeScript,
    Network,
    PlutusScript,
    ProtocolParameters,
    RawCBOR,
    ScriptHash,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
)
from pycardano.backend.blockfrost import _try_fix_script

from .koios import (
    KoiosClient,
    KoiosError,
    KoiosReferenceScript,
    KoiosTip,
    KoiosUtxo,
    field_fraction,
    field_int,
    field_opt_str,
    is_object_list,
    is_str_key_dict,
)

# Conway ledger constants for tiered reference-script fees (cardano-ledger
# Conway spec, ``refScriptCostStride`` / ``refScriptCostMultiplier``). Koios
# exposes only ``min_fee_ref_script_cost_per_byte``; the stride and multiplier
# are protocol constants, not provider data.
_REF_SCRIPT_RANGE = 25_600
_REF_SCRIPT_MULTIPLIER = 1.2
_MAX_REF_SCRIPTS_SIZE = 200_000


def _cost_models(row: dict[str, object], where: str) -> dict[str, dict[str | int, int]]:
    """Koios ``cost_models`` is ``{"PlutusV1": [..], "PlutusV2": [..],
    "PlutusV3": [..]}``. pycardano's ``CostModels`` serialises each model by
    sorted key, so list positions become integer keys — the same mapping the
    stock ``CardanoCliChainContext._parse_cost_models`` applies to cardano-cli's
    list form."""
    raw = row.get("cost_models")
    if not is_str_key_dict(raw):
        raise KoiosError(f"{where}: cost_models missing or not an object")
    models: dict[str, dict[str | int, int]] = {}
    for language, values in raw.items():
        if not is_object_list(values):
            raise KoiosError(f"{where}: cost_models[{language!r}] is not an array")
        model: dict[str | int, int] = {}
        for position, value in enumerate(values):
            if isinstance(value, bool) or not isinstance(value, int):
                raise KoiosError(
                    f"{where}: cost_models[{language!r}][{position}] is not an integer"
                )
            model[position] = value
        models[language] = model
    return models


def _script_version(kind: str) -> Literal[1, 2, 3] | None:
    if kind == "plutusV1":
        return 1
    if kind == "plutusV2":
        return 2
    if kind == "plutusV3":
        return 3
    return None


def _reference_script(ref: KoiosReferenceScript) -> NativeScript | PlutusScript:
    version = _script_version(ref.type)
    if version is not None:
        script = PlutusScript.from_version(version, bytes.fromhex(ref.bytes_hex))
        return _try_fix_script(ref.hash, script)
    if ref.type in ("timelock", "multisig"):
        if not is_str_key_dict(ref.value):
            raise KoiosError(f"Koios reference_script {ref.hash}: native script JSON missing")
        return NativeScript.from_dict(ref.value)
    raise KoiosError(f"Koios reference_script {ref.hash}: unknown script type {ref.type!r}")


class StruckNeverSubmits(RuntimeError):
    """Raised by ``submit_tx_cbor``: submission belongs to the human's wallet."""


class KoiosChainContext(ChainContext):
    """pycardano ``ChainContext`` served by Koios. Nothing is cached across
    calls beyond the epoch-scoped protocol/genesis parameters and a tip read
    no older than ``_TIP_REFRESH_S``.

    The tip window mirrors pycardano's own contexts (``CardanoCliChainContext``
    refetches the tip every ``slot_length / active_slots_coefficient`` = 20 s
    on Cardano): ``TransactionBuilder`` reads ``protocol_param`` dozens of
    times per build, and an uncached ``/tip`` per read made one remote build
    take 18 s (measured 2026-10-21)."""

    _TIP_REFRESH_S = 20.0

    def __init__(self, client: KoiosClient, network: Network) -> None:
        self._client = client
        self._network = network
        self._protocol_param: ProtocolParameters | None = None
        self._protocol_epoch: int | None = None
        self._genesis_param: GenesisParameters | None = None
        self._tip: KoiosTip | None = None
        self._tip_read_at = 0.0

    def _current_tip(self) -> KoiosTip:
        now = time.monotonic()
        if self._tip is None or now - self._tip_read_at >= self._TIP_REFRESH_S:
            self._tip = self._client.get_tip()
            self._tip_read_at = now
        return self._tip

    @property
    def client(self) -> KoiosClient:
        return self._client

    @property
    def tip(self) -> KoiosTip:
        """Struck addition: the cached tip row (slot + block time)."""
        return self._current_tip()

    @property
    def network(self) -> Network:
        return self._network

    @property
    def epoch(self) -> int:
        return self._current_tip().epoch

    @property
    def last_block_slot(self) -> int:
        return self._current_tip().slot

    @property
    def genesis_param(self) -> GenesisParameters:
        if self._genesis_param is None:
            where = "Koios /genesis"
            row = self._client.get_genesis()
            self._genesis_param = GenesisParameters(
                active_slots_coefficient=field_fraction(row, "activeslotcoeff", where),
                update_quorum=field_int(row, "updatequorum", where),
                max_lovelace_supply=field_int(row, "maxlovelacesupply", where),
                network_magic=field_int(row, "networkmagic", where),
                epoch_length=field_int(row, "epochlength", where),
                system_start=field_int(row, "systemstart", where),
                slots_per_kes_period=field_int(row, "slotsperkesperiod", where),
                slot_length=field_int(row, "slotlength", where),
                max_kes_evolutions=field_int(row, "maxkesrevolutions", where),
                security_param=field_int(row, "securityparam", where),
            )
        return self._genesis_param

    @property
    def protocol_param(self) -> ProtocolParameters:
        epoch = self.epoch
        if self._protocol_param is None or self._protocol_epoch != epoch:
            where = f"Koios /epoch_params epoch {epoch}"
            row = self._client.get_epoch_params(epoch)
            extra_entropy = field_opt_str(row, "extra_entropy", where)
            ref_cost = field_int(row, "min_fee_ref_script_cost_per_byte", where)
            self._protocol_param = ProtocolParameters(
                min_fee_constant=field_int(row, "min_fee_b", where),
                min_fee_coefficient=field_int(row, "min_fee_a", where),
                max_block_size=field_int(row, "max_block_size", where),
                max_tx_size=field_int(row, "max_tx_size", where),
                max_block_header_size=field_int(row, "max_bh_size", where),
                key_deposit=field_int(row, "key_deposit", where),
                pool_deposit=field_int(row, "pool_deposit", where),
                pool_influence=field_fraction(row, "influence", where),
                monetary_expansion=field_fraction(row, "monetary_expand_rate", where),
                treasury_expansion=field_fraction(row, "treasury_growth_rate", where),
                decentralization_param=field_fraction(row, "decentralisation", where),
                extra_entropy=extra_entropy if extra_entropy is not None else "",
                protocol_major_version=field_int(row, "protocol_major", where),
                protocol_minor_version=field_int(row, "protocol_minor", where),
                min_utxo=field_int(row, "min_utxo_value", where),
                min_pool_cost=field_int(row, "min_pool_cost", where),
                price_mem=field_fraction(row, "price_mem", where),
                price_step=field_fraction(row, "price_step", where),
                max_tx_ex_mem=field_int(row, "max_tx_ex_mem", where),
                max_tx_ex_steps=field_int(row, "max_tx_ex_steps", where),
                max_block_ex_mem=field_int(row, "max_block_ex_mem", where),
                max_block_ex_steps=field_int(row, "max_block_ex_steps", where),
                max_val_size=field_int(row, "max_val_size", where),
                collateral_percent=field_int(row, "collateral_percent", where),
                max_collateral_inputs=field_int(row, "max_collateral_inputs", where),
                coins_per_utxo_word=ALONZO_COINS_PER_UTXO_WORD,
                coins_per_utxo_byte=field_int(row, "coins_per_utxo_size", where),
                cost_models=_cost_models(row, where),
                maximum_reference_scripts_size={"bytes": _MAX_REF_SCRIPTS_SIZE},
                min_fee_reference_scripts={
                    "base": ref_cost,
                    "range": _REF_SCRIPT_RANGE,
                    "multiplier": _REF_SCRIPT_MULTIPLIER,
                },
            )
            self._protocol_epoch = epoch
        return self._protocol_param

    def _utxos(self, address: str) -> list[UTxO]:
        return [utxo for utxo, _row in self.utxo_rows(address)]

    def utxo_rows(self, address: str) -> list[tuple[UTxO, KoiosUtxo]]:
        """Each UTxO with the Koios row it came from (``block_time`` etc.)."""
        utxos: list[tuple[UTxO, KoiosUtxo]] = []
        for row in self._client.get_utxos(address):
            multi_asset = MultiAsset()
            for unit, quantity in row.assets.items():
                policy_hex, _sep, name_hex = unit.partition(".")
                policy = ScriptHash(bytes.fromhex(policy_hex))
                if policy not in multi_asset:
                    multi_asset[policy] = Asset()
                multi_asset[policy][AssetName(bytes.fromhex(name_hex))] = quantity

            datum: RawCBOR | None = None
            datum_hash: DatumHash | None = None
            if row.inline_datum_hex is not None:
                datum = RawCBOR(bytes.fromhex(row.inline_datum_hex))
            elif row.datum_hash is not None:
                datum_hash = DatumHash.from_primitive(row.datum_hash)

            script: NativeScript | PlutusScript | None = None
            if row.reference_script is not None:
                script = _reference_script(row.reference_script)

            utxos.append(
                (
                    UTxO(
                        TransactionInput.from_primitive([row.tx_hash, row.tx_index]),
                        TransactionOutput(
                            Address.from_primitive(address),
                            amount=Value(row.value, multi_asset),
                            datum_hash=datum_hash,
                            datum=datum,
                            script=script,
                        ),
                    ),
                    row,
                )
            )
        return utxos

    def submit_tx_cbor(self, cbor: bytes | str) -> str:
        raise StruckNeverSubmits(
            "the chain context never submits: Struck submits only from the pipeline's "
            "sign-and-submit step (struck.pipeline), after approval and the Rust checks"
        )

    def evaluate_tx_cbor(self, cbor: bytes | str) -> dict[str, ExecutionUnits]:
        # Upstream evaluates through the Koios /ogmios passthrough. Struck
        # validates on the operator's own Ogmios instead (explicit config,
        # struck.chain.reads.OgmiosClient.evaluate), and plain sends have no
        # redeemers, so the builder never calls this.
        raise NotImplementedError(
            "evaluate_tx_cbor: plain sends carry no scripts; Struck evaluates on Ogmios explicitly"
        )
