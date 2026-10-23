"""Koios REST client: a PORT of 

Source: ``~/
the file is ported from the WORKING TREE (it has uncommitted changes), sha256
2da0ed68b3550a8f0c019ca574b8bfcfcddd07c112a16b1be2d74c3c5e984119, snapshot
taken 2026-10-21 11:30 CDMX. Ported: the JSON narrowing helpers, the typed
rows and ``get_utxos``/``get_tip``/``get_genesis``/``get_epoch_params``,
verbatim except for the adaptations below.

Struck adaptations (each one is named here):
* Transport. Every request goes through ``RecordedHttp``, so it lands in the
  append-only call history. A non-2xx status raises ``CallFailed`` there
  (upstream raised ``KoiosError``; the message still carries the server text).
* ``KoiosTip`` also carries ``block_time``, the POSIX time of the tip block,
  served by the same ``/tip`` row. Struck needs it to turn the TTL slot into
  wall-clock time without a local node.
* ``KoiosUtxo`` also carries the served ``address``, so callers can check
  that a row really belongs to the queried wallet.
* ``submit_tx`` (``POST /submittx``) is Struck's own: it is called only by the
  pipeline's sign-and-submit step, after approval and every Rust check. Not
  ported: asset/tx-info lookups, the ``/ogmios`` evaluate passthrough and
  ``health``, because Struck does not call them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import TypeGuard, cast

from ..callhistory import RecordedHttp, Served

_PAGE = 1000  # Koios/PostgREST default page size; paged with ``offset``.
SOURCE = "koios"


class KoiosError(RuntimeError):
    """Koios returned a row that does not match the documented shape. The
    message carries the endpoint and the offending value."""


# ── JSON narrowing (one boundary, then exact types) ───────────────────


def is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def is_str_key_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(
        isinstance(key, str) for key in cast("dict[object, object]", value)
    )


def _json(served: Served) -> object:
    """``json.loads`` is Any-typed; squeeze at this one line."""
    return cast("object", json.loads(served.content))


def field_int(row: dict[str, object], key: str, where: str) -> int:
    """Koios serialises big integers as strings (``value``, ``key_deposit``)
    and small ones as ints; both are accepted, anything else raises."""
    value = row.get(key)
    if isinstance(value, bool):
        raise KoiosError(f"{where}: field {key!r} is a bool, expected an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise KoiosError(f"{where}: field {key!r}={value!r} is not an integer") from exc
    raise KoiosError(f"{where}: field {key!r} missing or not an integer ({value!r})")


def field_fraction(row: dict[str, object], key: str, where: str) -> Fraction:
    """Decimal-exact: ``Fraction(str(0.0577)) == Fraction(577, 10000)``,
    which is the on-chain rational; ``Fraction(0.0577)`` would be the binary
    float's exact expansion."""
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise KoiosError(f"{where}: field {key!r} missing or not numeric ({value!r})")
    try:
        return Fraction(str(value))
    except ValueError as exc:
        raise KoiosError(f"{where}: field {key!r}={value!r} is not a number") from exc


def field_str(row: dict[str, object], key: str, where: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise KoiosError(f"{where}: field {key!r} missing or not a string ({value!r})")
    return value


def field_opt_str(row: dict[str, object], key: str, where: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise KoiosError(f"{where}: field {key!r} is not a string ({value!r})")
    return value


def _rows(served: Served, where: str) -> list[dict[str, object]]:
    """Koios list endpoints return a JSON array of objects."""
    raw = _json(served)
    if not is_object_list(raw):
        raise KoiosError(f"{where}: expected a JSON array, got {type(raw).__name__}")
    rows: list[dict[str, object]] = []
    for entry in raw:
        if not is_str_key_dict(entry):
            raise KoiosError(f"{where}: array entry is not an object ({type(entry).__name__})")
        rows.append(entry)
    return rows


def _single_row(served: Served, where: str) -> dict[str, object]:
    rows = _rows(served, where)
    if len(rows) != 1:
        raise KoiosError(f"{where}: expected exactly one row, got {len(rows)}")
    return rows[0]


# ── Typed rows ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KoiosReferenceScript:
    """``reference_script`` of an extended ``/address_utxos`` row."""

    hash: str
    type: str  # "plutusV1" | "plutusV2" | "plutusV3" | "timelock" | "multisig"
    bytes_hex: str
    value: object  # native-script JSON for timelock/multisig, else None


@dataclass(frozen=True)
class KoiosUtxo:
    """One UTxO from ``POST /address_utxos`` with ``_extended: true``."""

    tx_hash: str
    tx_index: int
    address: str  # Struck adaptation: the served owner address
    value: int  # lovelace
    assets: dict[str, int]  # policy_id.asset_name_hex -> quantity
    datum_hash: str | None
    inline_datum_hex: str | None
    reference_script: KoiosReferenceScript | None
    block_height: int
    block_time: int  # POSIX seconds of the block that created this output


@dataclass(frozen=True)
class KoiosTip:
    """Chain tip from ``GET /tip``."""

    slot: int
    height: int
    hash: str
    epoch: int
    block_time: int  # Struck adaptation: POSIX seconds of the tip block


class KoiosClient:
    """Synchronous Koios REST client over the recorded transport."""

    def __init__(self, http: RecordedHttp, base_url: str) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"Koios base URL must be http(s), got {base_url!r}")
        self._base = base_url.rstrip("/")
        self._http = http

    @property
    def base_url(self) -> str:
        return self._base

    # ── UTxO queries ────────────────────────────────────────────────

    def get_utxos(self, address: str) -> list[KoiosUtxo]:
        """All unspent UTxOs at an address via ``POST /address_utxos``.

        ``_extended: true`` is REQUIRED — without it Koios omits
        ``asset_list``, ``inline_datum`` and ``reference_script``; a tx built
        from such rows fails ``ValueNotConservedUTxO`` on submit (proven
        2026-10-13). Pages with ``offset`` until a short page arrives.
        """
        where = "Koios /address_utxos"
        result: list[KoiosUtxo] = []
        offset = 0
        while True:
            payload: dict[str, list[str] | bool] = {"_addresses": [address], "_extended": True}
            served = self._http.request(
                SOURCE,
                "POST",
                f"{self._base}/address_utxos",
                params={"offset": str(offset), "limit": str(_PAGE)},
                json_body=json.dumps(payload).encode("utf-8"),
            )
            rows = _rows(served, where)
            for row in rows:
                if row.get("is_spent") is True:
                    continue
                result.append(self._utxo_row(row, where))
            if len(rows) < _PAGE:
                return result
            offset += _PAGE

    @staticmethod
    def _utxo_row(row: dict[str, object], where: str) -> KoiosUtxo:
        assets: dict[str, int] = {}
        asset_entries = row.get("asset_list")
        if asset_entries is not None:
            if not is_object_list(asset_entries):
                raise KoiosError(f"{where}: asset_list is not an array")
            for entry in asset_entries:
                if not is_str_key_dict(entry):
                    raise KoiosError(f"{where}: asset_list entry is not an object")
                policy = field_str(entry, "policy_id", where)
                name = field_str(entry, "asset_name", where)
                assets[f"{policy}.{name}"] = field_int(entry, "quantity", where)

        inline_hex: str | None = None
        inline = row.get("inline_datum")
        if inline is not None:
            if not is_str_key_dict(inline):
                raise KoiosError(f"{where}: inline_datum is not an object")
            inline_hex = field_str(inline, "bytes", where)

        script: KoiosReferenceScript | None = None
        ref = row.get("reference_script")
        if ref is not None:
            if not is_str_key_dict(ref):
                raise KoiosError(f"{where}: reference_script is not an object")
            script = KoiosReferenceScript(
                hash=field_str(ref, "hash", where),
                type=field_str(ref, "type", where),
                bytes_hex=field_str(ref, "bytes", where),
                value=ref.get("value"),
            )

        return KoiosUtxo(
            tx_hash=field_str(row, "tx_hash", where),
            tx_index=field_int(row, "tx_index", where),
            address=field_str(row, "address", where),
            value=field_int(row, "value", where),
            assets=assets,
            datum_hash=field_opt_str(row, "datum_hash", where),
            inline_datum_hex=inline_hex,
            reference_script=script,
            block_height=field_int(row, "block_height", where),
            block_time=field_int(row, "block_time", where),
        )

    # ── Chain queries ───────────────────────────────────────────────

    def get_tip(self) -> KoiosTip:
        """Current chain tip via ``GET /tip``."""
        where = "Koios /tip"
        row = _single_row(self._http.request(SOURCE, "GET", f"{self._base}/tip"), where)
        return KoiosTip(
            slot=field_int(row, "abs_slot", where),
            height=field_int(row, "block_no", where),
            hash=field_str(row, "hash", where),
            epoch=field_int(row, "epoch_no", where),
            block_time=field_int(row, "block_time", where),
        )

    def submit_tx(self, signed_cbor_hex: str) -> tuple[str, Served]:
        """``POST /submittx`` (raw CBOR, ``application/cbor``): (the tx id Koios
        returned, the response as served). Anything but a 64-hex id is loud."""
        served = self._http.request(
            SOURCE, "POST", f"{self._base}/submittx", cbor_body=bytes.fromhex(signed_cbor_hex)
        )
        tx_id = _json(served)
        if not isinstance(tx_id, str) or not re.fullmatch(r"[0-9a-f]{64}", tx_id):
            raise KoiosError(f"Koios /submittx: unexpected reply {served.content[:300]!r}")
        return tx_id, served

    def get_genesis(self) -> dict[str, object]:
        """Genesis row via ``GET /genesis`` (every numeric field is a string)."""
        served = self._http.request(SOURCE, "GET", f"{self._base}/genesis")
        return _single_row(served, "Koios /genesis")

    def get_tx_info(self, tx_id: str) -> tuple[bool, Served]:
        """``GET /tx_info?_tx_hashes=``: (the id is CONFIRMED on-chain, the
        served response). Koios takes the argument as a Postgres array
        literal (``">{hash}"``); Koios lists confirmed transactions only, so
        an empty row set means not yet in a block (mempool or dropped), NOT
        absent forever."""
        where = "Koios /tx_info"
        if not re.fullmatch(r"[0-9a-f]{64}", tx_id):
            raise KoiosError(f"Koios /tx_info: {tx_id!r} is not a 64-hex tx id")
        served = self._http.request(
            SOURCE, "GET", f"{self._base}/tx_info", params={"_tx_hashes": '{"' + tx_id + '"}'}
        )
        return bool(_rows(served, where)), served

    def get_epoch_params(self, epoch_no: int) -> dict[str, object]:
        """Protocol parameters of one epoch via ``GET /epoch_params?_epoch_no=``."""
        where = f"Koios /epoch_params epoch {epoch_no}"
        served = self._http.request(
            SOURCE, "GET", f"{self._base}/epoch_params", params={"_epoch_no": str(epoch_no)}
        )
        return _single_row(served, where)
