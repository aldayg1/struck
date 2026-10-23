"""The Midnight Blockfrost indexer client (MID01): the documented
graphql-over-HTTP surface (https://docs.blockfrost.io/midnight/), fed ONLY by
the operator-entered settings file. Auth header ``project_id`` is SENT but —
like every credential — never recorded (RecordedHttp logs headers never) and
scrubbed from any receipt.

A real call looks like:

    POST https://midnight-preprod.blockfrost.io/api/v0
    headers: {project_id: <operator credential>}
    body:    {"query": "{ block { hash height timestamp } }"}

with the documented response shape ``{"data": {"block": {...}}}`` (401/403 on
a bad credential: surfaced, never swallowed).
"""

from __future__ import annotations

import json
from typing import cast

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from ..callhistory import RecordedHttp, Served
from .settings import MidnightSettings, redact

# The minimal documented tip query (docs Quick Start, first example).
TIP_QUERY = "{ block { hash height timestamp } }"


class BlockTip(BaseModel):
    """The served ``data.block`` fields the validate proof reads."""

    model_config = ConfigDict(strict=True, extra="allow", frozen=True)

    hash: StrictStr
    height: StrictInt
    timestamp: StrictInt


class MidnightReport(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    midnight_network: StrictStr
    indexer_url: StrictStr  # redacted of the credential already
    block_hash: StrictStr
    height: StrictInt
    timestamp_ms: StrictInt
    response_sha256: StrictStr


class MidnightClient:
    def __init__(self, http: RecordedHttp, settings: MidnightSettings) -> None:
        self._http = http
        self._settings = settings

    def query(self, query: str) -> tuple[Served, dict[str, object]]:
        """One POST to the indexer base; served bytes verbatim."""
        request_document: dict[str, object] = {"query": query}
        payload = json.dumps(request_document, separators=(",", ":")).encode("utf-8")
        served = self._http.request(
            source="midnight",
            method="POST",
            url=self._settings.indexer_url,
            json_body=payload,
            auth_headers={"project_id": self._settings.project_id},
        )
        raw_response: object = json.loads(served.content)
        if not isinstance(raw_response, dict):
            raise ValueError("midnight indexer: response is not a JSON object (served shape)")
        typed_response: dict[str, object] = {}
        for key, value in raw_response.items():
            typed_response[str(key)] = value
        return served, typed_response

    def tip(self) -> MidnightReport:
        """``{ block { hash height timestamp } }`` — the live validate proof."""
        served, parsed = self.query(TIP_QUERY)
        errors = parsed.get("errors")
        if not isinstance(parsed.get("data"), dict) or errors is not None:
            message = json.dumps(errors)[:400] if errors else "no data served"
            raise ValueError(f"midnight indexer: served an error: {message}")
        data = cast("dict[str, object]", parsed["data"])
        if not isinstance(data.get("block"), dict):
            raise ValueError("midnight indexer: no block served")
        block_data: object = data["block"]
        block = BlockTip.model_validate(block_data)
        # The URL, stored only with the credential scrubbed.
        url = redact(served.url, settings=self._settings)
        return MidnightReport(
            midnight_network=self._settings.midnight_network,
            indexer_url=url,
            block_hash=block.hash,
            height=block.height,
            timestamp_ms=block.timestamp,
            response_sha256=served.sha256,
        )
