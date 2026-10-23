# K02 read_cli test fixtures — REAL Strike TESTNET bytes (no invented JSON)

Every `*.json` body in this directory is a verbatim RAW response body captured
from the live Strike TESTNET deployment (`https://api-v2-testnet.strikefinance.org`,
Struck network `preprod`) on 2026-10-22 (~17:02–17:05 UTC) by the K02 worker,
using the production CLI `struck/strike/read_cli.py` exactly as shipped.
`capture-manifest.json` was merged mechanically from the seven runs' own
`manifest.json` files; each entry's `sha256`/`response_bytes` was re-verified
against the copied bytes at build time.

Original run receipts (kept as proof artifacts): `receipts/coordination-20261022-k02/`
(`live`, `live2`, `live3`, `capture/*`). The call history of every capture call is in
`logs/call_history.jsonl` (source `strike-testnet`).

## Refresh (re-capture from the live service, deliberately)

```bash
uv run python -m struck.strike.read_cli --network preprod markets      --out receipts/capture/markets
uv run python -m struck.strike.read_cli --network preprod server-time  --out receipts/capture/server-time
uv run python -m struck.strike.read_cli --network preprod fee-tiers    --out receipts/capture/fee-tiers
uv run python -m struck.strike.read_cli --network preprod ticker BTC-USD      --out receipts/capture/ticker
uv run python -m struck.strike.read_cli --network preprod depth BTC-USD 5     --out receipts/capture/depth
uv run python -m struck.strike.read_cli --network preprod trades BTC-USD 5    --out receipts/capture/trades
uv run python -m struck.strike.read_cli --network preprod klines BTC-USD 1h 5 --out receipts/capture/klines
```

Then copy each `<run>/<op>.json` body over the same-named file here and update
`capture-manifest.json` from the new runs' manifests (the merges are mechanical:
url, status, response_bytes, sha256, file). Volatile fields (serverTime, prices,
depth ids, trade ids) WILL differ after a refresh; the tests assert structure,
exact-bytes persistence and manifest truthfulness, never these values.
