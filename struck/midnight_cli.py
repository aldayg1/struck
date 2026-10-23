"""The Midnight CLI (matrix MID01).

    uv run python -m struck.midnight_cli configure [--network <name>]
    uv run python -m struck.midnight_cli validate [--network <name>]
    uv run python -m struck.midnight_cli network-info [--network <name>]
    uv run python -m struck.midnight_cli install | update | status

``configure`` is the integrator's entry tool for the OPERATOR-supplied
Blockfrost Midnight credentials: prompted at a REAL terminal only (never
argv scanning-depth values, never environment variables), written to the
per-user private file ``midnight.json`` (struck.private_file, 0600, atomic).
Only missing or invalid fields are prompted; the rest keeps its value. The
project_id is read the same way (it is not password-grade, but it never
lands in shell history: no argv, no env).

``validate`` is the LIVE proof against the operator-configured indexer:
loads the settings (fail-closed naming the exact file + fields), POSTs the
documented tip query ``{ block { hash height timestamp } }``, checks the
served shape, and writes the receipt under receipts/midnight/ with the
credential scrubbed from everything stored or echoed.

Endpoints (docs.blockfrost.io/midnight/): indexer
``https://midnight-{net}.blockfrost.io/api/v0`` (GraphQL over HTTP POST,
credential header ``project_id``), WS ``wss://.../api/v0/ws``
(``graphql-transport-ws``), node RPC ``https://rpc.midnight-{net}.blockfrost.io``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from .callhistory import RecordedHttp
from .config import pop_network_flag, network_help, wants_help
from .midnight import installer, updater
from .midnight.client import MidnightClient, TIP_QUERY
from .midnight.settings import (
    MIDNIGHT_SETTINGS_FIELDS,
    MIDNIGHT_SETTINGS_PATH,
    MidnightSettings,
    MidnightSettingsError,
    load_midnight_settings,
    redact,
    save_midnight_settings,
)
from .tty import read_human_line, require_terminal

RECEIPTS_DIR = Path("receipts") / "midnight"

DOCUMENTED_SHAPES = (
    "indexer https://midnight-{net}.blockfrost.io/api/v0 "
    "(GraphQL over POST, credential header project_id)"
    " | WS wss://midnight-{net}.blockfrost.io/api/v0/ws (graphql-transport-ws)"
    " | RPC https://rpc.midnight-{net}.blockfrost.io"
)

_PROMPTS = (
    ("indexer_url", "indexer URL (docs: https://midnight-<net>.blockfrost.io/api/v0)"),
    ("indexer_ws_url", "indexer WebSocket (docs: wss://midnight-<net>.blockfrost.io/api/v0/ws)"),
    ("rpc_url", "node RPC URL (docs: https://rpc.midnight-<net>.blockfrost.io)"),
    ("project_id", "Blockfrost project_id (the Midnight credential; typed, never argv)"),
)


def configure(selected_network: str) -> int:
    """The integrator's entry tool: only missing or invalid fields are prompted."""
    require_terminal()
    values: dict[str, str] = {}
    try:
        existing = load_midnight_settings()
        values = {
            "indexer_url": existing.indexer_url,
            "indexer_ws_url": existing.indexer_ws_url,
            "rpc_url": existing.rpc_url,
            "project_id": existing.project_id,
        }
        print(f"midnight.json already configured ({MIDNIGHT_SETTINGS_PATH}); only invalid/absent entries are prompted")
    except MidnightSettingsError as exc:
        print(f"first configuration ({str(exc).splitlines()[0].split('. ')[-1][:80]}...)")
    for field, prompt in _PROMPTS:
        if values.get(field):
            continue
        print(f"  {prompt}", flush=True)
        try:
            answer = read_human_line()
        except (EOFError, OSError) as exc:
            print(f"NOT READ: {exc}; configure stopped, nothing written.")
            return 3
        values[field] = answer.strip()
    try:
        settings = MidnightSettings.model_validate(values)
    except ValidationError as exc:
        print(
            f"REFUSED: settings are not the strict shape ({exc.error_count()} error(s) across "
            f"{', '.join(MIDNIGHT_SETTINGS_FIELDS)}; URL fields must be https://, the WebSocket "
            f"field wss://, the project_id one [A-Za-z0-9_-] token). Nothing was written.",
            file=sys.stderr,
        )
        return 2
    try:
        network_name = settings.midnight_network
    except MidnightSettingsError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    save_midnight_settings(settings)
    print(f"saved {MIDNIGHT_SETTINGS_PATH} (private 0600, atomic). Midnight network: {network_name}")
    print(f"live proof: uv run python -m struck.midnight_cli validate   (struck network: {selected_network})")
    return 0


def validate() -> int:
    """The LIVE proof, fed by the operator-configured file."""
    try:
        settings = load_midnight_settings()
    except MidnightSettingsError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 2
    http = RecordedHttp(30)
    try:
        client = MidnightClient(http, settings)
        receipt_path = run_validate(client, settings)
    except (ValueError, OSError) as exc:
        print(f"VALIDATE FAILED (served or settings): {exc}", file=sys.stderr)
        return 2
    finally:
        http.close()
    print(f"MIDNIGHT OK: receipt {receipt_path}")
    return 0


def run_validate(client: MidnightClient, settings: MidnightSettings) -> Path:
    """Shared by the CLI and tests: ONE live documented call + write the receipt."""
    served, _parsed = client.query(TIP_QUERY)
    report = client.tip()  # served-shape re-check, raising on any deviation
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    receipt = RECEIPTS_DIR / f"validate-{stamp}.json"
    payload = {
        "ts_utc": stamp,
        "config": {
            "indexer_url": redact(settings.indexer_url, settings=settings),
            "indexer_ws_url": redact(settings.indexer_ws_url, settings=settings),
            "rpc_url": redact(settings.rpc_url, settings=settings),
            "project_id": "<redacted>",
        },
        "midnight_network": report.midnight_network,
        "query": TIP_QUERY,
        "http_status": served.status,
        "response_sha256": served.sha256,
        "served": json.dumps(
            cast("object", json.loads(redact(served.content.decode("utf-8"), settings=settings)))
        ),
    }
    tmp = receipt.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(receipt)
    return receipt


def network_info() -> int:
    try:
        settings = load_midnight_settings()
    except MidnightSettingsError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 2
    print(f"configured midnight network: {settings.midnight_network}")
    print(f"indexer/WS/RPC present: yes (values are in {MIDNIGHT_SETTINGS_PATH}, not echoed)")
    return 0


def main(argv: list[str]) -> int:
    if wants_help(argv):
        print(f"{__doc__}\n{network_help()}", file=sys.stderr)
        return 0
    argv = pop_network_flag(argv)
    match argv:
        case ["configure"]:
            from .config import selected_network

            try:
                net = selected_network(None)
            except Exception as exc:  # noqa: BLE001 - a named StruckConfigError subtype is enough
                print(f"configure still works without a struck network (--network): {type(exc).__name__}")
                net = "<unset>"
            return configure(net)
        case ["validate"]:
            return validate()
        case ["network-info"]:
            return network_info()
        case ["install"]:
            print(installer.check_docker())
            for step in installer.clone_repo(Path("midnight-node-docker")):
                print(step)
            return 0
        case ["update"]:
            print(updater.check_updates(Path("midnight-node-docker")))
            return 0
        case ["status"]:
            print(installer.check_docker())
            try:
                settings = load_midnight_settings()
                print(f"settings: present ({settings.midnight_network}); validate to prove live")
            except MidnightSettingsError:
                print("settings: MISSING (run: uv run python -m struck.midnight_cli configure)")
            return 0
        case _:
            print(f"{__doc__}\n{network_help()}", file=sys.stderr)
            return 2


if __name__ == "__main__":  # pragma: no cover - manual CLI
    raise SystemExit(main(sys.argv[1:]))
