# Struck, by Refractic Labs

Struck is a local trading framework for **Strike Finance** (Cardano perps) and
the **Cardano chain**. An LLM or a human writes a plan, deterministic Rust
checks it, a **human approves it**, and Struck shows the exact raw request or
unsigned transaction with a plain explanation and a congruence check. A Cardano
send is signed only with a wallet you imported into Struck's encrypted store
(`--wallet`), only after your approval and the Rust checks, and is re-checked
before it is submitted; without `--wallet` Struck stops at the signature
request. Strike orders require human Approve and Send, then API-wallet signing.
Nothing is autonomous.

```
plan (LLM or human, strict Pydantic)
  -> Rust check against live, served state
  -> HUMAN approval (LangGraph interrupt; TTY-only in the CLIs)
  -> exact raw request / unsigned tx + plain explanation
  -> Rust congruence (request/tx == approved plan)
  -> stop (signature request / send confirm)
```

## One network setting

`network` is one value: a row of the frozen per-network table `struck.network.PROFILES`
(today `preprod` = Cardano preprod + Strike TESTNET, and `mainnet` = Cardano
mainnet + Strike MAINNET). Every network-specific value comes from
that row: Strike REST/WS URLs (exact-host allowlist), Cardano network magic,
the Ogmios genesis network, the Koios URL, address HRPs, the tx-body
`network_id`, the cardano-cli network arguments and the labels. Rust receives
the row as JSON and holds no network literal.

Selection order: `--network <name>` on any CLI, then the Settings tab's
per-user `network.json` (in `struck.paths.user_config_dir()`, private; applies
on the next start), then the `network` field of the local `config/struck.json`.
There is NO code default: with none of the three set, every CLI and the UI
refuse and name all three ways.
The choice is resolved once and pinned for the process. A plan approved on one
network is refused on any other (the network is inside the approved hash). There
is no silent switch and no fallback. An unknown name fails loudly.

* The title bar and the Approval tab show the selected network, and every CLI
  approval prompt prints a `NETWORK:` banner first. Plain names only: mainnet
  is mainnet, preprod is preprod — no danger styling, no editorializing.
* The node backend reads the node's actual magic from Ogmios
  (`queryNetwork/genesisConfiguration`) before any read or build. The Koios
  backend checks Koios `/genesis`. A mismatch fails with `NETWORK MISMATCH`
  and nothing is built.
* Addresses must be canonical bech32 for the selected network (HRP and header
  network id), both in Python (pycardano) and in Rust (pallas).

## Install

Prerequisites:

* [uv](https://docs.astral.sh/uv/) — manages Python 3.14 and the venv.
* A Rust toolchain (`cargo`): the money core (`rust/struck_py`) is a PyO3
  extension built from this repo on first sync.
* Only the `node` chain backend needs a local stack (cardano-node socket,
  cardano-cli, Ogmios, Kupo). The default `koios` backend needs none.

```bash
git clone https://github.com/aldayg1/struck && cd struck
uv sync --all-extras     # or: uv sync --extra llm-openai (pick your providers)
```

`uv sync` builds the Rust core, creates `.venv`, installs everything
exact-pinned. Then write the one required config file (gitignored, no
template — Struck prints the exact path and refuses to start without it):

```jsonc
// <repo>/config/struck.json
{
  "network": "preprod",          // or "mainnet"; or omit and pass --network
  "chain_backend": "koios",      // "koios" (keyless) or "node"
  "node_socket": {"preprod": null, "mainnet": null},  // node IPC paths; null = node backend refuses
  "cardano_cli_path": "/usr/local/bin/cardano-cli",   // absolute; never looked up on PATH
  "kupo_url": "http://127.0.0.1:1442",
  "ogmios_url": "http://127.0.0.1:1337",
  "timeout_s": 30
}
```

Secrets — all prompted with input hidden, at a real terminal or in the
Settings tab; nothing is ever read from the environment:

```bash
uv run python -m struck.keys list                      # entry names, present/missing only
uv run python -m struck.keys set llm_openai            # your LLM provider key
uv run python -m struck.strike.venue_auth generate <label>   # new Strike API key pair, or:
uv run python -m struck.strike.venue_auth import <label> <file.json>  # bind a pair you made on Strike's site
uv run python -m struck.strike.venue_auth connect <label> --code <builder-code>  # builder connect
uv run python -m struck.wallets import <name> --network <name>  # Cardano wallet (mnemonic, hidden prompt)
```

Every command takes `--network preprod|mainnet`. With no network set anywhere,
every CLI and the UI refuse and name the three ways to set it.

## Run it

```bash
uv run python -m struck.ui.app [--network <name>]          # desktop app (PySide6)
uv run python -m struck.strike.market_cli BTC-USD           # public market data, as served
uv run python -m struck.send_cli plans/send-2ada-preprod.json --approver <you> [--backend node|koios]
uv run python -m struck.strike.order_cli plans/strike-eth-limit-testnet.json --approver <you>
uv run python -m struck.congruence_cli receipts/<run> [--tamper-lovelace N]
uv run python -m struck.triggers.cli add plans/triggers/eth-mark-band-strike-order.json
uv run python -m struck.triggers.cli tick
uv run python -m struck.chat.cli --provider <p> --model <m> "limit buy 0.01 ETH-USD at 2000 GTC"
```

Every CLI accepts `--network <name>`. Approval prompts read only from a TTY.
Piped or scripted answers are refused with exit 3, and nothing is built or sent.

Chain backend (`chain_backend` in `config/struck.json`, or `--backend`):
`node` uses the local cardano-node socket + cardano-cli build, Ogmios and
Kupo; `koios` uses the ported KoiosChainContext + pycardano. Each backend is
self-sufficient: no cross-backend checks and no fallback.

## Strike API wallet (you add it; Struck ships no key)

Keys are read only from Struck's encrypted secret store (service `struck`, entries
`strike_api_wallet_public_key` and `strike_api_wallet_private_key`; see
"Secrets" below). No environment variable is read. They are never logged or
written, and the call history records no headers. Signing follows Strike's spec: Ed25519
over `{METHOD}:{PATH}:{TIMESTAMP}:{NONCE}:{BODY_HASH}`, with the timestamp on
Strike's served clock (`GET /v2/time`). The user-stream `session.logon` uses
the same served-clock offset.

```bash
uv run python -m struck.strike.account_cli --network <name> [--no-values]   # READ-ONLY
uv run python -m struck.strike.logon_cli --network <name>                  # READ-ONLY logon proof
```

`--no-values` prints HTTP status, byte counts, sha256 and field names only,
and writes only hashes.

**Order sending requires human Approve and Send.** The exact approved request is
signed by the API wallet; CIP-30 `signData` is not required for Strike orders. Acceptance by the API is not proof
of execution; confirm the outcome through Strike's stream or status reads.

If a send ever hits a transport error (timeout or reset after the request may
have left), the leg is recorded as `outcome: "unknown"` in `executed.json`, and
its client order id goes on `receipts/UNRESOLVED_SENDS.jsonl` (0600). Any
re-run with that id is refused until a human resyncs with Strike and removes
the line.

## Chat and LLM providers

The Chat dock proposes ONE strict model per reply: a Strike plan, an on-chain
send, a trigger, or a clarifying question. It has no approve, sign or send
control. "Review" hands the proposal to the normal path. Providers go through
the stock `init_chat_model`.

* Keys come only from Struck's encrypted secret store (`llm_<provider>`; see "Secrets").
  A provider's standard env var (e.g. `OPENAI_API_KEY`) is ignored even when set.
* A hosted provider always gets its official URL pinned (`OFFICIAL_URLS`).
  Struck refuses to start if that SDK's base-URL env var is set (for example
  `OPENAI_BASE_URL` or `ANTHROPIC_BASE_URL`).
* A custom host needs `allow_custom_host: true`, https, and the secret entry
  `llm_custom`. It never receives the provider's own key.
* vertex and bedrock are UNSUPPORTED (refused loudly): their SDK credential
  chains read env vars and credential files, which Struck does not allow.
* perplexity is refused (its client takes no base URL to pin).
* `vllm` and `ollama` use your endpoint.
* LLM settings live in `~/.config/struck/llm.json` (exactly 0600; private
  parent dir; no symlinks; atomic write).

## Gates

```bash
uv run mypy struck tests                  # strict, every disallow_any_*; zero
uv run ruff check .                       # zero
uv run pytest -q
(cd rust && cargo test)
```

Every external call (HTTP, WS open/close, cardano-cli) is appended to
`logs/call_history.jsonl` with hashes, never headers.

## Limits (stated plainly)

* Strike execution remains unproven until a human-approved request has a
  correlated engine outcome.
* `FEE_TIER` is a constant (tier 0). The account's served tier is not read yet.
* Hosted LLM providers were not live-validated under the encrypted-store key rule.
  Cohere's `tool_choice` is incompatible with the structured-output path.
  Ollama `qwen3-30b` was rejected or timed out in our runs.
* Some SDK base-URL env var names (together, fireworks) are partly inferred
  from upstream sources, not from installed SDK code.

## Local config (`config/struck.json`, gitignored, no template)

Struck refuses to start without it and names the path. Keys:
`network` (optional; selection rung 3), `chain_backend` (`node`|`koios`),
`node_socket` (EVERY network name -> its node IPC endpoint or `null`; the node
backend on a network whose entry is `null` refuses), `cardano_cli_path`
(absolute path to the binary; never looked up on PATH), `kupo_url`,
`ogmios_url`, `timeout_s`. The node backend also checks the node itself:
Ogmios genesis magic, and a `cardano-cli query tip` handshake on the
configured socket with the selected network's flag.

## Secrets (one encrypted file, the same on Linux, macOS and Windows)

Every secret comes from Struck's own encrypted store: the stock `keyring`
package with the stock `keyrings.cryptfile` backend (both exact-pinned; Argon2id
KDF + AES-GCM), set explicitly in `struck/secrets.py` (never auto-detected; no
OS or desktop keyring is ever consulted). Service `struck`. The file is
`<per-user Struck config dir>/secrets.cfg`, private-file checked before every
use (POSIX 0600 and a private folder; Windows owner-only DACL by SID, no
reparse point). Entry NAMES are visible in the file; values are encrypted.

The master password is typed at a real terminal (`getpass`, no echo) or in the
Settings tab's masked field; never read from env, arguments or files, never
logged. A wrong password or a missing entry fails loudly with the exact command
to fix it. No environment variable is read for a secret, even when set.

| entry | used for |
|---|---|
| `strike_api_wallet_public_key` | Strike API wallet, 64 hex Ed25519 public key as registered |
| `strike_api_wallet_private_key` | Strike API wallet, 64 hex Ed25519 seed |
| `llm_<provider>` | that provider's key, sent only to its official host: `llm_openai`, `llm_anthropic`, `llm_google_genai`, `llm_azure_openai`, `llm_mistralai`, `llm_groq`, `llm_xai`, `llm_deepseek`, `llm_fireworks`, `llm_together`, `llm_cohere`, `llm_perplexity`, `llm_openrouter`, `llm_vllm` |
| `llm_custom` | the ONLY key an opted-in custom LLM host receives |
| `wallet_<name>`, `wallets_index` | Cardano wallets (`struck.wallets`, below) |

Manage entries (at a real terminal; values are prompted with input hidden):

```
uv run python -m struck.keys list            # names + present/missing only
uv run python -m struck.keys set llm_openai  # prompts, input hidden
uv run python -m struck.keys delete llm_openai
```

The Settings tab has the same: a masked field (never pre-filled, cleared
after each store) and a present/missing list. `llm_vllm` is required for vLLM
too: for a server started WITHOUT `--api-key`, store any explicit value, e.g.
`none`. Azure also needs `OPENAI_API_VERSION` (an api-version string, not a
secret). Proxy/CA variables (`HTTP(S)_PROXY`, `ALL_PROXY`, lower
case too, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`,
`CURL_CA_BUNDLE`) are ignored by Struck's own clients (`trust_env=False`, Qt
`NoProxy`); integrations that cannot take such a client refuse to start while
one is set.

## Cardano wallets (`struck.wallets`)

A wallet is a BIP-39 mnemonic stored only in the encrypted store. Keys are
derived with stock pycardano (CIP-1852 over CIP-3 Icarus): payment
`m/1852'/1815'/0'/0/0`, stake `m/1852'/1815'/0'/2/0` (standard base address)
or no stake part (`--enterprise`). The network is explicit; nothing defaults.

```
# mnemonic: hidden prompt at a terminal, or ONE line piped on stdin; never an argument
uv run python -m struck.wallets import <name> --network preprod [--enterprise]
uv run python -m struck.wallets list          # name, network, address only
uv run python -m struck.wallets delete <name> # asks y/N at the terminal
```

`--secrets-file <path>` (before the command) selects another store file for one
run; it is a path, never a secret. Sending with a stored wallet:

```
uv run python -m struck.send_cli <plan.json> --approver <you> --wallet <name> [--backend node|koios] [--network <name>]
```

Order: master password -> plan check (Rust) -> you type `approve <plan_hash>`
-> build -> tx-matches-plan check (Rust) -> sign the exact checked CBOR ->
signed-tx check (Rust: tx id unchanged, one vkey witness, the from-address key
hash, signature over the tx id) -> submit on the selected backend -> tx id,
explorer link and receipts. A wallet on another network than the selected one,
or one that does not own the plan's from-address, is refused before approval.

Views: before signing, `explanation.txt` is the request, labelled
`SIGNATURE REQUEST (awaiting signature)`. After signing, every view (the
`SUBMITTED` output, `signed.explanation.txt` / `.json` in the receipt) describes
the SIGNED tx from its signed CBOR: vkey witness count, signer key hash vs the
wallet's payment key hash, signature over the body hash (tx id), and the tx id.
Any receipt can be re-rendered read-only:

```
uv run python -m struck.receipt_view receipts/<run>
```

## PORTABILITY (Linux, macOS, Windows)

Code paths exist for all three, using only the stdlib, Qt and stock tools:
package-relative repo paths (`struck/paths.py`, never the cwd), a per-user
config dir per OS (Linux `~/.config/struck`, macOS
`~/Library/Application Support/Struck`, Windows `%USERPROFILE%\AppData\Roaming\Struck`),
relative intra-package imports, `encoding="utf-8"` on every text open, list-arg
subprocesses without a shell, and executables only from configured absolute
paths (no shelled-out ACL tools: Windows ACLs are read and set through
advapi32/kernel32 via ctypes, `struck/win_acl.py`).

| Area | Linux | macOS | Windows |
|---|---|---|---|
| Private files | POSIX 0600 + O_NOFOLLOW + parent checks, RUN | same POSIX code, type-checked + unit-tested | CreateFileW with OPEN_REPARSE_POINT, handle checks, owner-only DACL by SID (owner from the process token); type-checked + unit-tested with a ctypes fake |
| Approval TTY | isatty + `/dev/tty`, RUN | same, unit-tested | isatty + `GetConsoleMode` + `CONIN$`, unit-tested with fakes |
| Node IPC | Unix socket, RUN (preprod) | Unix socket, unit-tested | local named pipe `\\.\pipe\<name>` only (UNC/remote refused), unit-tested; never run against a node |
| cardano-cli check (node backend, at use) | not group/other-writable, RUN | same, unit-tested | file and folder write ACEs only for owner, SYSTEM, Administrators, TrustedInstaller (by SID; inherit-only ACEs skipped); unit-tested with a ctypes fake |
| Proxies | trust_env=False, NoProxy, RUN | same code | same code |

Verified: `uv run mypy --platform linux|darwin|win32 struck shared tests` (all
clean) and the pytest suite on Linux. `cargo test` was run on Linux only (no
`std::os::unix` in the crates). NOT run on Windows or macOS: no GUI, CLI, node
or Strike session has been exercised there. Not used: file locking.

