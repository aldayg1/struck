# Golden vectors (real, not synthetic)

`send_2ada.*` come from the milestone-1 proof run on 2026-10-21 10:53 CDMX
(receipts/20261021T165319Z-send-ae0d1b7a4d32):

- `send_2ada.cbor.hex`: the UNSIGNED Conway tx built by stock
  `cardano-cli 11.2.3.0 conway transaction build` against the local preprod
  node (tx id ae9d15e07d94899497c1c5d40ef01c98b5702ac366287c0f88400ae8cf6360dc).
- `send_2ada.plan.json`: the approved plan (plan_hash ae0d1b7a…).
- `send_2ada.inputs.json`: the spent UTxO as resolved by Kupo at that time.

`strike_testnet_*.json` are the raw bodies of the LIVE Strike TESTNET public
reads made 2026-10-21 12:04 CDMX through `struck.callhistory.RecordedHttp`
(source `strike-testnet`, see `logs/call_history.jsonl`; sha256 prefixes
markets `b115de6384b3791b`, fee-tiers `6a93099c18726e63`, time
`c3ae721650207441`). Unsigned GETs only. Both networks are
supported. Mainnet goldens have not
been fetched yet; until they are, spec B1 is golden-tested on testnet only.

`strike_mainnet_*.json` are the raw bodies of the LIVE Strike MAINNET public
reads made 2026-10-21 16:04 CDMX through `struck.callhistory.RecordedHttp`
(source `strike-mainnet`, https://api.strikefinance.org, see
`logs/call_history.jsonl`; sha256 prefixes markets `bf1887893bf92ad8`,
fee-tiers `4fb3af9d04fe14df`, time `6b185873af367f4e`). Unsigned GETs only;
no key, no order. They back `tests/mainnet_golden.rs`, so spec B1 is now
golden-tested on BOTH networks (testnet: `markets_golden.rs`/`strike_golden.rs`).

- `send_2ada_signed.cbor.hex` / `send_2ada_signed.unsigned.cbor.hex`: the signed
  and unsigned CBOR of preprod tx
  `09f6df511e98fcf42657192726672d182e60343e660779d67c56c4c89ba59754`, copied
  byte-for-byte from `receipts/20261022T001259Z-koios-send-b5f4913166bc/`
  (`tx.signed.cbor.hex`, `tx.cbor.hex`). Public data: body + one vkey witness.
