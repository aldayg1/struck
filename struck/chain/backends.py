"""The chain-backend toggle: ``node`` | ``koios``.

Each backend is a complete, self-sufficient path for the three things the
pipeline needs from a chain:

* ``wallet_view``: the tip slot, the wallet's UTxO set, the TTL slot for the
  plan's validity window, and that slot's wall-clock time;
* ``build_unsigned``: the UNSIGNED, balanced tx for the approved plan;
* ``validate_unsigned``: the backend's own extra receipts for that tx;
* ``submit``: the signed tx, after Struck's signed-tx checks (node: Ogmios
  ``submitTransaction``; koios: ``POST /submittx``).

``NodeBackend`` is the existing milestone-1 path. It calls the unchanged
``KupoClient``/``OgmiosClient``/``CardanoCli`` with the same arguments
(cardano-cli build, Ogmios tip/eraSummaries/evaluate, Kupo UTxOs).
``KoiosBackend`` uses the ported 
pycardano ``TransactionBuilder``. Neither backend calls the other, there are
no cross-backend checks, and there is no fallback: an unreachable backend
raises.
"""

from __future__ import annotations

import time

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

import pycardano
from pycardano import (
    Address,
    AssetName,
    MultiAsset,
    ScriptHash,
    Transaction,
    TransactionBuilder,
    TransactionOutput,
    TransactionWitnessSet,
    UTxO,
    Value,
)

from ..callhistory import RecordedHttp
from .cli import CardanoCli
from .koios import KoiosClient, KoiosUtxo
from .koios_context import KoiosChainContext
from .models import Asset, ChainUtxo
from .reads import KupoClient, OgmiosClient
from ..config import StruckConfig
from ..network import NetworkProfile
from ..plan.models import ApprovedOnchainPlan, UnsignedTx

CDMX = ZoneInfo("America/Mexico_City")
BackendName = Literal["node", "koios"]


class BackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class WalletView:
    tip_slot: int
    utxos: tuple[ChainUtxo, ...]
    valid_until_slot: int
    ttl_wallclock: str


class ChainBackend(Protocol):
    @property
    def name(self) -> BackendName: ...

    @property
    def network(self) -> NetworkProfile:
        """The SELECTED network this backend was built for (config ``network``)."""
        ...

    def check_network(self) -> str:
        """The backend's ACTUAL network vs the selected one; raises on mismatch."""
        ...

    def wallet_view(self, address: str, validity_minutes: int) -> WalletView: ...

    def build_unsigned(
        self,
        plan: ApprovedOnchainPlan,
        refs: list[str],
        checked_utxos: tuple[ChainUtxo, ...],
        out_dir: Path,
    ) -> UnsignedTx: ...

    def validate_unsigned(self, unsigned: UnsignedTx, out_dir: Path) -> dict[str, str]: ...

    def submit(self, signed_cbor_hex: str) -> tuple[str, bytes]:
        """Submit a signed, verified tx: (the tx id the backend accepted, its raw reply)."""
        ...


def _wallclock(when: datetime) -> str:
    return when.astimezone(CDMX).strftime("%Y-%m-%d %H:%M:%S CDMX (UTC-6)")


# ── node: cardano-cli + Ogmios + Kupo (milestone-1 path) ───────────────


def _mismatch(source: str, magic: int, net: NetworkProfile) -> BackendError:
    return BackendError(
        f"NETWORK MISMATCH: {source} serves network magic {magic}, but the selected network "
        f"is {net.name} (magic {net.cardano_magic}). Nothing is read or built; fix the "
        "node/socket or select the matching network."
    )


class NodeBackend:
    def __init__(
        self, kupo: KupoClient, ogmios: OgmiosClient, cli: CardanoCli, net: NetworkProfile
    ) -> None:
        self._kupo = kupo
        self._ogmios = ogmios
        self._cli = cli
        self._net = net

    @property
    def name(self) -> BackendName:
        return "node"

    @property
    def network(self) -> NetworkProfile:
        return self._net

    def check_network(self) -> str:
        magic, kind = self._ogmios.network_magic()
        if magic != self._net.cardano_magic or kind != self._net.ogmios_genesis_network:
            raise _mismatch(f"the node (Ogmios genesis: {kind})", magic, self._net)
        # The SOCKET cardano-cli builds against is checked too (it may be another node
        # than Ogmios'): the node-to-client handshake carries the network magic and
        # refuses a mismatch, so a served tip proves the socket is on this network.
        slot = self._cli.socket_tip_slot()
        return (
            f"node genesis networkMagic {magic} ({kind}) == selected {self._net.name}; "
            f"cardano-cli socket handshake ok with {' '.join(self._net.cardano_cli_network_args)} "
            f"(tip slot {slot})"
        )

    def wallet_view(self, address: str, validity_minutes: int) -> WalletView:
        self.check_network()
        tip = self._ogmios.tip()
        _served, utxos = self._kupo.unspent(address)
        clock = self._ogmios.slot_clock()
        valid_until = tip.slot + clock.slots_for_minutes(tip.slot, validity_minutes)
        return WalletView(
            tip_slot=tip.slot,
            utxos=utxos,
            valid_until_slot=valid_until,
            ttl_wallclock=_wallclock(clock.slot_to_utc(valid_until)),
        )

    def build_unsigned(
        self,
        plan: ApprovedOnchainPlan,
        refs: list[str],
        checked_utxos: tuple[ChainUtxo, ...],
        out_dir: Path,
    ) -> UnsignedTx:
        (out_dir / "network.check.txt").write_text(self.check_network() + "\n", encoding="utf-8")
        envelope_path = out_dir / "unsigned.tx.json"
        envelope, stdout = self._cli.build_unsigned_send(
            tx_ins=refs,
            outputs=[(o.address, o.lovelace, o.assets) for o in plan.outputs],
            change_address=plan.from_address,
            invalid_hereafter=plan.valid_until_slot,
            out_file=envelope_path,
        )
        return UnsignedTx(
            source="cardano-cli",
            cbor_hex=envelope.cborHex,
            tx_id=self._cli.txid(envelope_path),
            build_stdout=stdout,
        )

    def submit(self, signed_cbor_hex: str) -> tuple[str, bytes]:
        tx_id, served = self._ogmios.submit(signed_cbor_hex)  # the node's own mempool
        return tx_id, served.content

    def validate_unsigned(self, unsigned: UnsignedTx, out_dir: Path) -> dict[str, str]:
        return {
            "ogmios.evaluate.json": self._ogmios.evaluate(unsigned.cbor_hex).content.decode(
                "utf-8"
            ),
            "tx.view.cardano-cli.json": self._cli.view(out_dir / "unsigned.tx.json").decode(
                "utf-8"
            ),
        }


# ── koios: ported 


def _asset_of(unit: str, qty: int) -> Asset:
    policy, _sep, name = unit.partition(".")
    return Asset(policy_hex=policy, name_hex=name, qty=qty)


def _asset_key(a: Asset) -> tuple[str, str]:
    return (a.policy_hex, a.name_hex)


def _utxo_key(u: ChainUtxo) -> str:
    return u.reference


def _chain_utxo(row: KoiosUtxo, address: str) -> ChainUtxo:
    ref = f"{row.tx_hash}#{row.tx_index}"
    if row.address != address:
        raise BackendError(f"Koios returned {ref} at {row.address} for a query on {address}")
    if (
        row.datum_hash is not None
        or row.inline_datum_hex is not None
        or row.reference_script is not None
    ):
        raise BackendError(
            f"{ref} carries a datum/script; plain sends only spend plain key-locked UTxOs"
        )
    assets = tuple(sorted((_asset_of(u, q) for u, q in row.assets.items()), key=_asset_key))
    return ChainUtxo(
        tx_hash=row.tx_hash, index=row.tx_index, address=address, lovelace=row.value, assets=assets
    )


def _value(lovelace: int, assets: tuple[Asset, ...]) -> Value:
    if not assets:
        return Value(lovelace)
    multi = MultiAsset()
    for a in assets:
        policy = ScriptHash(bytes.fromhex(a.policy_hex))
        if policy not in multi:
            multi[policy] = pycardano.Asset()
        multi[policy][AssetName(bytes.fromhex(a.name_hex))] = a.qty
    return Value(lovelace, multi)


# Tip-vs-clock bounds (seconds). Policy constants, not chain values: a block is
# minted every ~20 s on average, so a healthy index is well inside 10 minutes;
# 120 s ahead tolerates ordinary NTP skew.
KOIOS_TIP_MAX_LAG_S = 600
KOIOS_TIP_MAX_AHEAD_S = 120


class KoiosBackend:
    def __init__(self, client: KoiosClient, net: NetworkProfile) -> None:
        self._client = client
        self._ctx = KoiosChainContext(client, net.pycardano_network)
        self._net = net

    def submit(self, signed_cbor_hex: str) -> tuple[str, bytes]:
        tx_id, served = self._client.submit_tx(signed_cbor_hex)
        return tx_id, served.content

    @property
    def name(self) -> BackendName:
        return "koios"

    @property
    def network(self) -> NetworkProfile:
        return self._net

    def check_network(self) -> str:
        magic = self._ctx.genesis_param.network_magic
        if magic != self._net.cardano_magic:
            raise _mismatch("Koios /genesis", magic, self._net)
        return f"Koios genesis networkmagic {magic} == selected {self._net.name}"

    def _rows(self, address: str) -> list[tuple[UTxO, ChainUtxo]]:
        return [(u, _chain_utxo(row, address)) for u, row in self._ctx.utxo_rows(address)]

    def wallet_view(self, address: str, validity_minutes: int) -> WalletView:
        self.check_network()
        tip = self._ctx.tip
        # Tip sanity vs this machine's clock (fail loudly, nothing is guessed): a
        # tip from the future or an index lagging by minutes would put the TTL
        # wall-clock and the validity window on the wrong side of reality.
        lag_s = time.time() - tip.block_time
        if lag_s < -KOIOS_TIP_MAX_AHEAD_S or lag_s > KOIOS_TIP_MAX_LAG_S:
            raise BackendError(
                f"Koios tip (slot {tip.slot}, block_time {tip.block_time}) is {lag_s:.0f} s from "
                f"local time; allowed -{KOIOS_TIP_MAX_AHEAD_S}..{KOIOS_TIP_MAX_LAG_S} s. "
                "Check this machine's clock or Koios sync; nothing was built."
            )
        if validity_minutes <= 0:
            raise BackendError(f"validity_minutes must be > 0, got {validity_minutes}")
        slot_length_s = self._ctx.genesis_param.slot_length
        span_s = validity_minutes * 60
        if slot_length_s <= 0 or span_s % slot_length_s != 0:
            raise BackendError(
                f"{validity_minutes} min is not a whole number of {slot_length_s} s slots"
            )
        valid_until = tip.slot + span_s // slot_length_s
        if not tip.slot < valid_until:
            raise BackendError(f"TTL slot {valid_until} is not after the tip slot {tip.slot}")
        # Koios /tip carries the tip block's time; the current era's slot
        # length (genesis ``slotlength``) maps the TTL slot to wall clock.
        ttl_at = datetime.fromtimestamp(tip.block_time, tz=UTC) + timedelta(
            seconds=(valid_until - tip.slot) * slot_length_s
        )
        utxos = tuple(sorted((c for _u, c in self._rows(address)), key=_utxo_key))
        return WalletView(
            tip_slot=tip.slot,
            utxos=utxos,
            valid_until_slot=valid_until,
            ttl_wallclock=_wallclock(ttl_at),
        )

    def build_unsigned(
        self,
        plan: ApprovedOnchainPlan,
        refs: list[str],
        checked_utxos: tuple[ChainUtxo, ...],
        out_dir: Path,
    ) -> UnsignedTx:
        (out_dir / "network.check.txt").write_text(self.check_network() + "\n", encoding="utf-8")
        checked = {u.reference: u for u in checked_utxos}
        live = {c.reference: (u, c) for u, c in self._rows(plan.from_address)}
        builder = TransactionBuilder(self._ctx)
        for ref in refs:
            if ref not in live:
                raise BackendError(
                    f"selected input {ref} is no longer unspent at {plan.from_address}"
                )
            utxo, now = live[ref]
            if checked.get(ref) != now:
                raise BackendError(f"selected input {ref} changed since the plan check")
            builder.add_input(utxo)
        for o in plan.outputs:
            builder.add_output(
                TransactionOutput(Address.from_primitive(o.address), _value(o.lovelace, o.assets))
            )
        builder.ttl = plan.valid_until_slot
        body = builder.build(change_address=Address.from_primitive(plan.from_address))
        tx = Transaction(body, TransactionWitnessSet())
        cbor_hex = tx.to_cbor_hex()
        (out_dir / "unsigned.tx.json").write_text(
            '{\n    "type": "Tx ConwayEra",\n    "description": "Ledger Cddl Format",\n'
            f'    "cborHex": "{cbor_hex}"\n}}\n',
            encoding="utf-8",
        )
        return UnsignedTx(
            source="pycardano-koios",
            cbor_hex=cbor_hex,
            tx_id=tx.id.payload.hex(),
            build_stdout=(
                f"pycardano TransactionBuilder over KoiosChainContext: fee {body.fee} lovelace, "
                f"{len(body.inputs)} input(s), {len(body.outputs)} output(s), ttl {body.ttl}"
            ),
        )

    def validate_unsigned(self, unsigned: UnsignedTx, out_dir: Path) -> dict[str, str]:
        # Koios mode has no node; the deterministic Rust congruence check is
        # the validation (no cross-backend check).
        return {}


def make_backend(cfg: StruckConfig, name: BackendName, http: RecordedHttp) -> ChainBackend:
    if name == "node":
        cfg.node_socket_path  # noqa: B018  (raises ConfigError when this network has no node)
        return NodeBackend(
            KupoClient(http, cfg.kupo_url, cfg.profile),
            OgmiosClient(http, cfg.ogmios_url),
            CardanoCli(cfg),
            cfg.profile,
        )
    return KoiosBackend(KoiosClient(http, cfg.koios_url), cfg.profile)
