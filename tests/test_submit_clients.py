"""Koios /submittx and Ogmios submitTransaction clients (mock transport only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import httpx
import pytest

from struck.callhistory import RecordedHttp
from struck.chain.koios import KoiosClient, KoiosError
from struck.chain.reads import ChainReadError, OgmiosClient

TX = "84a300" + "00" * 8
TX_ID = "ab" * 32


def _http(tmp_path: Path, handler: httpx.MockTransport) -> RecordedHttp:
    http = RecordedHttp(5, tmp_path / "calls.jsonl")
    http._client = httpx.Client(transport=handler)
    return http


def test_koios_submit_posts_raw_cbor(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(202, content=json.dumps(TX_ID).encode())

    client = KoiosClient(
        _http(tmp_path, httpx.MockTransport(handler)), "https://preprod.koios.rest/api/v1"
    )
    tx_id, _served = client.submit_tx(TX)
    assert tx_id == TX_ID
    (req,) = seen
    assert req.url.path.endswith("/submittx") and req.headers["content-type"] == "application/cbor"
    assert req.content == bytes.fromhex(TX)


def test_koios_submit_odd_reply_is_loud(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(202, content=b'"nope"')

    client = KoiosClient(
        _http(tmp_path, httpx.MockTransport(handler)), "https://preprod.koios.rest/api/v1"
    )
    with pytest.raises(KoiosError, match="unexpected reply"):
        client.submit_tx(TX)


def test_ogmios_submit_returns_the_accepted_id_and_refusals_are_loud(tmp_path: Path) -> None:
    replies = [
        {"jsonrpc": "2.0", "method": "submitTransaction", "result": {"transaction": {"id": TX_ID}}},
        {"jsonrpc": "2.0", "method": "submitTransaction", "error": {"code": 3005, "message": "x"}},
    ]
    bodies: list[object] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(cast("object", json.loads(req.content)))
        return httpx.Response(200, content=json.dumps(replies.pop(0)).encode())

    ogmios = OgmiosClient(_http(tmp_path, httpx.MockTransport(handler)), "http://127.0.0.1:1337")
    assert ogmios.submit(TX)[0] == TX_ID
    assert bodies[0] == {
        "jsonrpc": "2.0",
        "method": "submitTransaction",
        "id": 1,
        "params": {"transaction": {"cbor": TX}},
    }
    with pytest.raises(ChainReadError, match="refused"):
        ogmios.submit(TX)
