"""Chain-side boundary models. ``ChainUtxo`` serializes to exactly the JSON
shape Rust ``struck_core::plan::Utxo`` deserializes (deny_unknown_fields)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Asset(_Strict):
    policy_hex: StrictStr
    name_hex: StrictStr
    qty: StrictInt


class ChainUtxo(_Strict):
    tx_hash: StrictStr
    index: StrictInt
    address: StrictStr
    lovelace: StrictInt
    assets: tuple[Asset, ...]

    @property
    def reference(self) -> str:
        return f"{self.tx_hash}#{self.index}"


class Tip(_Strict):
    slot: StrictInt
    id: StrictStr
