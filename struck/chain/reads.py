"""Chain reads for the SELECTED network: Kupo (UTxOs), Ogmios HTTP JSON-RPC (tip,
protocol parameters, era summaries, start time, evaluateTransaction) and
Koios public REST (tip, address UTxOs; keyless public tier, as the 
uses it: ``crypto/clients/koios_client.py`` "free, public, needs no API key").

Every call goes through ``RecordedHttp`` (call history). Every response is
shape-checked by a strict Pydantic model; a malformed reply raises.
"""

from __future__ import annotations


import json
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from ..callhistory import RecordedHttp, Served
from .models import Asset, ChainUtxo, Tip
from ..network import NetworkError, NetworkProfile, canonical_address


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class _Loose(BaseModel):
    # Upstream rows carry many fields Struck does not read; they are ignored
    # (not dropped silently from receipts: the raw served bytes are kept).
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)


class ChainReadError(RuntimeError):
    pass


def _utxo_key(u: ChainUtxo) -> str:
    return u.reference


def _asset_key(a: Asset) -> tuple[str, str]:
    return (a.policy_hex, a.name_hex)


def _asset_from_unit(unit: str, qty: int) -> Asset:
    """Kupo unit ``<policy>.<name_hex>``; a dotless unit has an empty name
    (same rule as )."""
    policy, _, name = unit.partition(".")
    if len(policy) != 56:
        raise ChainReadError(f"Kupo asset unit {unit!r} has no 56-hex policy")
    return Asset(policy_hex=policy, name_hex=name, qty=qty)


# ── Kupo ────────────────────────────────────────────────────────────────


class _KupoValue(_Strict):
    coins: StrictInt
    assets: dict[str, StrictInt]


class _KupoMatch(_Loose):
    transaction_id: StrictStr
    output_index: StrictInt
    address: StrictStr
    value: _KupoValue
    datum_hash: StrictStr | None
    script_hash: StrictStr | None


class _KupoMatches(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    rows: tuple[_KupoMatch, ...]


class KupoClient:
    SOURCE = "kupo"

    def __init__(self, http: RecordedHttp, base_url: str, net: NetworkProfile) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._net = net

    def unspent(self, address: str) -> tuple[Served, tuple[ChainUtxo, ...]]:
        # Only a canonical address of the SELECTED network may enter a URL.
        try:
            canonical_address(address, self._net)
        except NetworkError as exc:
            raise ChainReadError(str(exc)) from exc
        served = self._http.request(
            # Kupo takes a bare query flag (`?unspent`), not a key=value pair.
            self.SOURCE,
            "GET",
            f"{self._base}/matches/{address}?unspent",
        )
        rows = _KupoMatches.model_validate_json(b'{"rows":' + served.content + b"}").rows
        utxos = []
        for row in rows:
            if row.address != address:
                raise ChainReadError(f"Kupo returned {row.address} for a query on {address}")
            if row.datum_hash is not None or row.script_hash is not None:
                raise ChainReadError(
                    f"{row.transaction_id}#{row.output_index} carries a datum/script; "
                    "plain sends only spend plain key-locked UTxOs"
                )
            utxos.append(
                ChainUtxo(
                    tx_hash=row.transaction_id,
                    index=row.output_index,
                    address=row.address,
                    lovelace=row.value.coins,
                    assets=tuple(
                        _asset_from_unit(unit, qty)
                        for unit, qty in sorted(row.value.assets.items())
                    ),
                )
            )
        return served, tuple(sorted(utxos, key=_utxo_key))


# ── Ogmios (HTTP JSON-RPC 2.0) ──────────────────────────────────────────


class _TipResult(_Strict):
    jsonrpc: StrictStr
    method: StrictStr
    result: Tip
    id: StrictInt | None


class _StartTime(_Strict):
    jsonrpc: StrictStr
    method: StrictStr
    result: StrictStr
    id: StrictInt | None


class _Seconds(_Strict):
    seconds: StrictInt


class _Bound(_Strict):
    time: _Seconds
    slot: StrictInt
    epoch: StrictInt


class _Millis(_Strict):
    milliseconds: StrictInt


class _EraParams(_Strict):
    epochLength: StrictInt
    slotLength: _Millis
    safeZone: StrictInt | None


class _EraSummary(_Strict):
    start: _Bound
    end: _Bound | None
    parameters: _EraParams


class _EraSummaries(_Strict):
    jsonrpc: StrictStr
    method: StrictStr
    result: tuple[_EraSummary, ...]
    id: StrictInt | None


class SlotClock:
    """Slot <-> wall clock from Ogmios eraSummaries + startTime, integer ms."""

    def __init__(self, system_start: datetime, eras: tuple[_EraSummary, ...]) -> None:
        self._start = system_start
        self._eras = eras

    def _era(self, slot: int) -> _EraSummary:
        for era in self._eras:
            if era.start.slot <= slot and (era.end is None or slot < era.end.slot):
                return era
        raise ChainReadError(f"slot {slot} is outside every known era summary (horizon)")

    def slot_to_utc(self, slot: int) -> datetime:
        era = self._era(slot)
        ms = (
            era.start.time.seconds * 1000
            + (slot - era.start.slot) * era.parameters.slotLength.milliseconds
        )
        return self._start + timedelta(milliseconds=ms)

    def slots_for_minutes(self, slot: int, minutes: int) -> int:
        length_ms = self._era(slot).parameters.slotLength.milliseconds
        span_ms = minutes * 60_000
        if span_ms % length_ms != 0:
            raise ChainReadError(f"{minutes} min is not a whole number of {length_ms} ms slots")
        return span_ms // length_ms


class _ShelleyGenesis(BaseModel):
    model_config = ConfigDict(frozen=True)  # served document: extra fields ignored
    networkMagic: StrictInt
    network: StrictStr  # served; compared to the table's ogmios_genesis_network


class _ShelleyGenesisResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    result: _ShelleyGenesis


class _SubmittedId(_Loose):
    id: StrictStr


class _SubmittedTx(_Loose):
    transaction: _SubmittedId


class _SubmitReply(_Loose):
    result: _SubmittedTx | None = None


class OgmiosClient:
    SOURCE = "ogmios"

    def __init__(self, http: RecordedHttp, base_url: str) -> None:
        self._http = http
        self._url = base_url.rstrip("/")

    def _rpc(self, method: str, params: dict[str, object] | None = None) -> Served:
        body: dict[str, object] = {"jsonrpc": "2.0", "method": method, "id": 1}
        if params is not None:
            body["params"] = params
        return self._http.request(
            self.SOURCE, "POST", self._url, json_body=json.dumps(body).encode("utf-8")
        )

    def tip(self) -> Tip:
        return _TipResult.model_validate_json(self._rpc("queryNetwork/tip").content).result

    def network_magic(self) -> tuple[int, str]:
        """The node's ACTUAL network, as served: Shelley genesis networkMagic +
        network (Ogmios v6 ``queryNetwork/genesisConfiguration``, era shelley)."""
        served = self._rpc("queryNetwork/genesisConfiguration", {"era": "shelley"})
        g = _ShelleyGenesisResult.model_validate_json(served.content).result
        return g.networkMagic, g.network

    def protocol_parameters(self) -> Served:
        return self._rpc("queryLedgerState/protocolParameters")

    def slot_clock(self) -> SlotClock:
        start = _StartTime.model_validate_json(self._rpc("queryNetwork/startTime").content)
        eras = _EraSummaries.model_validate_json(self._rpc("queryLedgerState/eraSummaries").content)
        system_start = datetime.fromisoformat(start.result).astimezone(UTC)
        return SlotClock(system_start, eras.result)

    def submit(self, signed_cbor_hex: str) -> tuple[str, Served]:
        """Ogmios ``submitTransaction``: (the tx id the node accepted, the response
        as served). A JSON-RPC error is raised loudly with its message."""
        served = self._rpc("submitTransaction", {"transaction": {"cbor": signed_cbor_hex}})
        reply = _SubmitReply.model_validate_json(served.content)
        if reply.result is None:
            raise ChainReadError(f"Ogmios submitTransaction refused: {served.content[:800]!r}")
        return reply.result.transaction.id, served

    def evaluate(self, cbor_hex: str) -> Served:
        """Ogmios ``evaluateTransaction``. The JSON-RPC envelope is returned
        as served (a script-less tx is answered without redeemer budgets);
        HTTP errors raise via ``RecordedHttp``."""
        return self._rpc("evaluateTransaction", {"transaction": {"cbor": cbor_hex}})


# ── Koios (public REST, keyless) ────────────────────────────────────────


class _KoiosTip(_Loose):
    hash: StrictStr
    abs_slot: StrictInt
    block_height: StrictInt


class _KoiosTips(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    rows: tuple[_KoiosTip, ...]


class _KoiosAsset(_Loose):
    policy_id: StrictStr
    asset_name: StrictStr | None
    quantity: StrictStr


class _KoiosUtxo(_Loose):
    tx_hash: StrictStr
    tx_index: StrictInt
    address: StrictStr
    value: StrictStr
    asset_list: tuple[_KoiosAsset, ...] | None


class _KoiosUtxos(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    rows: tuple[_KoiosUtxo, ...]


def _koios_int(text: str, where: str) -> int:
    """Koios serialises big integers as strings ()."""
    if not text.isdigit():
        raise ChainReadError(f"Koios {where}: {text!r} is not an unsigned integer")
    return int(text)


class KoiosClient:
    SOURCE = "koios"

    def __init__(self, http: RecordedHttp, base_url: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    def tip(self) -> _KoiosTip:
        served = self._http.request(self.SOURCE, "GET", f"{self._base}/tip")
        rows = _KoiosTips.model_validate_json(b'{"rows":' + served.content + b"}").rows
        if len(rows) != 1:
            raise ChainReadError(f"Koios /tip returned {len(rows)} rows")
        return rows[0]

    def address_utxos(self, address: str) -> tuple[ChainUtxo, ...]:
        query: dict[str, list[str] | bool] = {"_addresses": [address], "_extended": True}
        body = json.dumps(query).encode("utf-8")
        served = self._http.request(
            self.SOURCE, "POST", f"{self._base}/address_utxos", json_body=body
        )
        rows = _KoiosUtxos.model_validate_json(b'{"rows":' + served.content + b"}").rows
        out = []
        for row in rows:
            assets = tuple(
                sorted(
                    (
                        Asset(
                            policy_hex=a.policy_id,
                            name_hex=a.asset_name or "",
                            qty=_koios_int(a.quantity, "asset quantity"),
                        )
                        for a in (row.asset_list or ())
                    ),
                    key=_asset_key,
                )
            )
            out.append(
                ChainUtxo(
                    tx_hash=row.tx_hash,
                    index=row.tx_index,
                    address=row.address,
                    lovelace=_koios_int(row.value, "value"),
                    assets=assets,
                )
            )
        return tuple(sorted(out, key=_utxo_key))
