"""Midnight settings (matrix MID01): the per-user, operator-entered Blockfrost
Midnight credentials — NEVER source constants, NEVER environment variables.

Same facility as the LLM choice (``struck.config`` LLM_CONFIG_PATH): one
private file, ``paths.user_config_dir() / "midnight.json"``, read through
``struck.private_file.read_private`` (O_NOFOLLOW, owned by you, 0600) and
written through ``struck.private_file.write_private`` (atomic, mkstemp,
fsync). Missing file = fail CLOSED naming the exact file and fields (the
NO_NETWORK style); nothing opens the network on a missing entry.

Endpoint contract: https://docs.blockfrost.io/midnight/ — Indexer API
``https://midnight-{network}.blockfrost.io/api/v0`` (GraphQL over HTTP POST,
auth header ``project_id``), Indexer WebSocket
``wss://midnight-{network}.blockfrost.io/api/v0/ws`` (``graphql-transport-ws``),
Node RPC ``https://rpc.midnight-{network}.blockfrost.io``. The values HERE are
entered by the integrator from the operator; the module validates shapes and
never invents them.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator

from .. import paths
from ..config import checked_url
from ..private_file import read_private, write_private

MIDNIGHT_SETTINGS_PATH = paths.user_config_dir() / "midnight.json"

MIDNIGHT_SETTINGS_FIELDS = (
    "indexer_url",
    "indexer_ws_url",
    "rpc_url",
    "project_id",
)


class MidnightSettingsError(RuntimeError):
    """The Midnight settings are missing or invalid: the rail stops (fail closed)."""


class MidnightSettings(BaseModel):
    """Strict; outliers refuse. ``project_id`` is the Blockfrost Midnight
    credential: it stays OUT of argv, environment, logs, receipts and this
    module's error texts (ACCESS.md)."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    indexer_url: StrictStr
    indexer_ws_url: StrictStr
    rpc_url: StrictStr
    project_id: StrictStr

    @field_validator("indexer_url", "rpc_url")
    @classmethod
    def _https_url(cls, v: str) -> str:
        url = checked_url(v)  # stock shape check first: host/auth/query rules
        if not url.startswith("https://"):
            raise ValueError(
                f"{url!r}: the Midnight endpoints are https:// only (docs.blockfrost.io/midnight/)"
            )
        return url

    @field_validator("indexer_ws_url")
    @classmethod
    def _wss_url(cls, v: str) -> str:
        # checked_url only parses http(s); a WS endpoint needs its own named check.
        if not v.startswith("wss://"):
            raise ValueError(f"{v!r}: indexer_ws_url must be wss:// (docs: graphql-transport-ws)")
        else_part = v[len("wss://"):]
        if not else_part.split("/", 1)[0]:
            raise ValueError(f"{v!r}: indexer_ws_url has no host")
        for banned in ("@", " ", "?", "#", "\\", "//:."):
            if banned in v:
                raise ValueError(f"{v!r}: indexer_ws_url carries a disallowed part")
        return v

    @field_validator("project_id")
    @classmethod
    def _project_id(cls, v: str) -> str:
        if not v:
            raise ValueError("project_id is required")
        if any(not (c.isalnum() or c in "-_") for c in v):
            raise ValueError("project_id: single token, [A-Za-z0-9_-] only (no spaces)")
        if not 16 <= len(v) <= 128:
            raise ValueError("project_id must be 16..128 characters")
        return v

    @property
    def midnight_network(self) -> str:
        """The Midnight network named by the configured indexer host (docs:
        ``midnight-{network}``). Derived, not stored; unknown = loud."""
        host = self.indexer_url.split("//", 1)[1].split("/", 1)[0]
        for name in ("preprod", "mainnet", "preview"):
            if f"midnight-{name}." in host:
                return name
        raise MidnightSettingsError(
            f"indexer_url host {host!r} does not name a Blockfrost Midnight network "
            "(midnight-preprod / midnight-mainnet / midnight-preview)"
        )


def load_midnight_settings(path: Path | None = None) -> MidnightSettings:
    """Fail closed: missing file = the exact command and fields, no network opened."""
    target = MIDNIGHT_SETTINGS_PATH if path is None else path
    data = read_private(target)
    if data is None:
        raise MidnightSettingsError(
            f"{target} is missing. Midnight settings are per-user, operator-entered, never "
            f"hardcoded: run the entry tool "
            f"uv run python -m struck.midnight_cli configure (prompted at a real "
            f"terminal) and fill exactly {', '.join(MIDNIGHT_SETTINGS_FIELDS)}. "
            "Nothing was run."
        )
    try:
        return MidnightSettings.model_validate_json(data)
    except (ValueError, TypeError) as exc:
        # Strip the offending VALUES from the message: only names travel.
        raise MidnightSettingsError(
            f"{target} is not a valid Midnight settings file ({', '.join(MIDNIGHT_SETTINGS_FIELDS)}): "
            f"{type(exc).__name__} at the strict boundary. Fill it with "
            f"uv run python -m struck.midnight_cli configure. Nothing was run."
        ) from exc


def save_midnight_settings(settings: MidnightSettings, path: Path | None = None) -> None:
    """Atomic, private (write_private: mkstemp in the private parent, 0600)."""
    target = MIDNIGHT_SETTINGS_PATH if path is None else path
    write_private(target, (settings.model_dump_json(indent=2) + "\n").encode("utf-8"))


def redact(text: str, settings: MidnightSettings | None = None, project_id: str | None = None) -> str:
    """Scrub the credential from any stored/echoed text (ACCESS.md)."""
    value = project_id if project_id is not None else (settings.project_id if settings else "")
    if value and value in text:
        text = text.replace(value, "<redacted>")
    return text
