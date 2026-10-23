"""Append-only call history (``logs/call_history.jsonl``): one JSON line per
external call (HTTP or chain CLI) with timestamp, method, URL, status,
latency and the SHA-256 of the request body and of the response.

Never recorded: request or response headers (they can carry credentials),
and raw bodies (only their hashes; the bodies that matter are saved as
receipts). The file is opened with ``O_APPEND`` and never rewritten.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict

from .config import CALL_HISTORY_PATH


class CallRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    ts_utc: str
    source: str
    method: str
    url: str
    status: int
    latency_ms: int
    request_sha256: str | None
    response_sha256: str
    response_bytes: int
    note: str | None = None  # WebSocket events only: served counts / close reason, never secrets


class CallFailed(RuntimeError):
    """An external call returned a non-success status. Recorded, then raised."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def append_record(record: CallRecord, path: Path = CALL_HISTORY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (record.model_dump_json() + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Served:
    """A response exactly as served: status, final URL and raw body bytes."""

    url: str
    status: int
    content: bytes

    @property
    def sha256(self) -> str:
        return sha256_hex(self.content)


class RecordedHttp:
    """httpx client whose every request lands in the call history."""

    def __init__(self, timeout_s: int, path: Path = CALL_HISTORY_PATH) -> None:
        # trust_env=False: no HTTP(S)_PROXY/ALL_PROXY, no SSL_CERT_FILE/DIR, no .netrc.
        # Struck talks only to its allowlisted hosts, directly, with default TLS verify.
        self._client = httpx.Client(timeout=httpx.Timeout(float(timeout_s)), trust_env=False)
        self._path = path

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        source: str,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json_body: bytes | None = None,
        auth_headers: dict[str, str] | None = None,
        cbor_body: bytes | None = None,
    ) -> Served:
        # auth_headers (Strike API-wallet signature) are SENT, never recorded.
        headers: dict[str, str] = dict(auth_headers or {})
        if json_body is not None and cbor_body is not None:
            raise ValueError("one request body only (json_body or cbor_body)")
        if json_body is not None:
            headers["content-type"] = "application/json"
        if cbor_body is not None:
            headers["content-type"] = "application/cbor"  # Koios /submittx
        body = json_body if json_body is not None else cbor_body
        started = time.monotonic_ns()
        response = self._client.request(method, url, params=params, content=body, headers=headers)
        latency_ms = (time.monotonic_ns() - started) // 1_000_000
        content = response.content
        served = Served(url=str(response.url), status=response.status_code, content=content)
        append_record(
            CallRecord(
                ts_utc=_now(),
                source=source,
                method=method,
                url=served.url,
                status=served.status,
                latency_ms=latency_ms,
                request_sha256=sha256_hex(body) if body is not None else None,
                response_sha256=served.sha256,
                response_bytes=len(content),
            ),
            self._path,
        )
        if not response.is_success:
            raise CallFailed(
                f"{source} {method} {served.url} -> HTTP {served.status}: "
                f"{content[:500].decode('utf-8', 'replace')}"
            )
        return served


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


def record_command(
    source: str,
    argv: Sequence[str],
    result: CommandResult,
    latency_ms: int,
    path: Path = CALL_HISTORY_PATH,
) -> None:
    """Record a chain-CLI invocation (``method`` = ``EXEC``, ``url`` = argv)."""
    append_record(
        CallRecord(
            ts_utc=_now(),
            source=source,
            method="EXEC",
            url=" ".join(argv),
            status=result.returncode,
            latency_ms=latency_ms,
            request_sha256=None,
            response_sha256=sha256_hex(result.stdout + result.stderr),
            response_bytes=len(result.stdout) + len(result.stderr),
        ),
        path,
    )
