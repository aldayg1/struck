"""Stock cardano-cli against the local node of the selected network: build an UNSIGNED,
balanced Conway transaction (the node supplies the UTxO set and protocol
parameters, computes the fee and refuses an unbalanced or under-min-UTxO
transaction), read its tx id, and print its stock JSON view.

This module never signs or submits: ``transaction build`` writes an unwitnessed
tx envelope (signing is ``struck.pipeline.sign_and_submit``, with a stored wallet). Every invocation is
recorded in the call history (source ``cardano-cli``).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, StrictStr

from ..callhistory import CommandResult, record_command
from .models import Asset
from ..config import StruckConfig

SOURCE = "cardano-cli"


class CliError(RuntimeError):
    pass


class TextEnvelope(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    type: StrictStr
    description: StrictStr
    cborHex: StrictStr


class _TxId(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    txhash: StrictStr


def _tx_out_value(lovelace: int, assets: Sequence[Asset]) -> str:
    parts = [str(lovelace)]
    for a in assets:
        unit = f"{a.policy_hex}.{a.name_hex}" if a.name_hex else a.policy_hex
        parts.append(f"{a.qty} {unit}")
    return "+".join(parts)


# The ONLY variables cardano-cli inherits (no keys, no proxies). PATH/HOME on
# POSIX; SYSTEMROOT/USERPROFILE/TEMP/TMP are what a Windows process needs to
# start (Winsock/crypto DLL lookup and its temp dir). Untested on Windows.
CLI_ENV_ALLOWLIST = ("PATH", "HOME", "SYSTEMROOT", "USERPROFILE", "TEMP", "TMP")


class CardanoCli:
    def __init__(self, cfg: StruckConfig) -> None:
        self._cfg = cfg
        cfg.checked_cardano_cli  # noqa: B018  (raises ConfigError before any node-backend work)

    def _run(self, args: Sequence[str]) -> CommandResult:
        argv = (self._cfg.checked_cardano_cli, *args)  # re-checked at every spawn
        # Allowlisted env only: no provider keys or other secrets reach cardano-cli.
        env = {k: os.environ[k] for k in CLI_ENV_ALLOWLIST if k in os.environ}
        env["CARDANO_NODE_SOCKET_PATH"] = self._cfg.node_socket_path
        started = time.monotonic_ns()
        proc = subprocess.run(argv, capture_output=True, env=env, check=False)
        latency_ms = (time.monotonic_ns() - started) // 1_000_000
        result = CommandResult(argv, proc.returncode, proc.stdout, proc.stderr)
        record_command(SOURCE, argv, result, latency_ms)
        if proc.returncode != 0:
            raise CliError(
                f"cardano-cli {' '.join(args[:3])} exited {proc.returncode}: "
                f"{proc.stderr.decode('utf-8', 'replace').strip()}"
            )
        return result

    def socket_tip_slot(self) -> int:
        """``cardano-cli query tip`` on the configured socket with the SELECTED
        network's flag; a magic mismatch fails the handshake (non-zero exit = loud)."""
        out = self._run(
            [
                "conway",
                "query",
                "tip",
                *self.network_args(),
                "--socket-path",
                self._cfg.node_socket_path,
            ]
        ).stdout
        try:
            tip = cast("dict[str, object]", json.loads(out))
            slot = tip["slot"]
        except (ValueError, KeyError, TypeError) as exc:
            raise CliError(f"cardano-cli query tip: unexpected output {out[:200]!r}") from exc
        if not isinstance(slot, int):
            raise CliError(f"cardano-cli query tip: slot is not an integer: {slot!r}")
        return slot

    def network_args(self) -> list[str]:
        """cardano-cli's network flag for the SELECTED network (stock flags)."""
        return list(self._cfg.profile.cardano_cli_network_args)  # struck.network.PROFILES

    def build_unsigned_send(
        self,
        *,
        tx_ins: Sequence[str],
        outputs: Sequence[tuple[str, int, Sequence[Asset]]],
        change_address: str,
        invalid_hereafter: int,
        out_file: Path,
    ) -> tuple[TextEnvelope, str]:
        """Returns the unwitnessed envelope and cardano-cli's own build stdout
        (it reports the estimated fee)."""
        args: list[str] = [
            "conway",
            "transaction",
            "build",
            *self.network_args(),
            "--socket-path",
            self._cfg.node_socket_path,
        ]
        for ref in tx_ins:
            args += ["--tx-in", ref]
        for address, lovelace, assets in outputs:
            args += ["--tx-out", f"{address}+{_tx_out_value(lovelace, assets)}"]
        args += [
            "--change-address",
            change_address,
            "--invalid-hereafter",
            str(invalid_hereafter),
            "--out-file",
            str(out_file),
        ]
        result = self._run(args)
        envelope = TextEnvelope.model_validate_json(out_file.read_bytes())
        # cardano-cli 11.2 labels a built (still unwitnessed) tx "Tx ConwayEra";
        # "unsigned" is proven by the Rust decode (0 witnesses), not by this label.
        if envelope.type != "Tx ConwayEra":
            raise CliError(f"expected a Conway tx envelope, got {envelope.type!r}")
        return envelope, result.stdout.decode("utf-8", "replace").strip()

    def txid(self, tx_file: Path) -> str:
        out = self._run(
            ["conway", "transaction", "txid", "--tx-file", str(tx_file), "--output-json"]
        )
        return _TxId.model_validate_json(out.stdout).txhash

    def view(self, tx_file: Path) -> bytes:
        return self._run(
            ["debug", "transaction", "view", "--output-json", "--tx-file", str(tx_file)]
        ).stdout
