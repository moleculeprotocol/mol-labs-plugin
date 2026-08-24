# molecule-desci — cross-harness plugin

Packages the DeSci orchestration skill and the `molecule` MCP server into one
installable plugin that works under **Claude Code** and **OpenAI Codex** (and any MCP host, via the
server alone).

- **`aura-orchestrator`** — the whole molecule in one skill: resolve-or-create an **On-Chain Lab** (mint a
  LabNFT + token-bound account, or reuse one the wallet owns) → register it (`createLab`) → data-room
  file upload → announcement → role-grant/hand-off. OCL/V3 surface, keyed on `oclId`. The file upload
  (Phase 3) is the only branch: choose **public** (plaintext) or **private** (client-side AES-256-GCM
  envelope-encrypted, access-controlled) — **x402 pays per call either way**.
- **Two wallet backends — the user chooses (all creds optional).** Every signing/spending step works with
  either (1) a **Privy agentic wallet** (signs server-side via the Privy API — `PRIVY_APP_ID` +
  `PRIVY_APP_SECRET` + `PRIVY_WALLET_ID`), or (2) a **raw EOA** (the MCP signs locally with
  `WALLET_PRIVATE_KEY`; the key never leaves the process). Privy never exposes a private key, so the two are
  distinct backends rather than one key — hence each has its own optional env set. Select with
  `WALLET_BACKEND=privy|eoa` (auto when only one is configured; required when both are). Create a Privy
  wallet with the `privy_*` tools (aura Phase 0) — for the official standalone Privy skill see
  [privy-io/privy-agentic-wallets-skill](https://github.com/privy-io/privy-agentic-wallets-skill) — or bring
  your own EOA and combine it later; nothing in the skill changes when you switch.
- **`molecule` MCP server** (`mcp/server.py`, Python/FastMCP, stdio) — dual-backend wallet ops (Privy
  agentic wallet **or** local-EOA signing), on-chain OCL reads (`ocl_read` / `ocl_tx_identity`), Labs
  GraphQL, the full x402 payment flow (signed by either backend), S3 upload, AES-256-GCM envelope crypto,
  ABI encoding, on-chain access conditions (`hasRole(oclId,…)` OR `isAuthorizedSignerForTba`).

> **The MCP server is the portable core** — both harnesses speak MCP. Skills (`SKILL.md`) are a shared
> standard both now read. Only the *plugin manifest* differs per harness, so this package ships both
> `.claude-plugin/` and `.codex-plugin/` manifests pointing at the same `skills/` and `.mcp.json`.

```
molecule-plugin/
├── .claude-plugin/{plugin.json, marketplace.json}   # Claude Code
├── .codex-plugin/plugin.json                         # Codex
├── .mcp.json                                         # shared MCP server config (uv run)
├── skills/aura-orchestrator/SKILL.md
└── mcp/{server.py,pyproject.toml,requirements.txt,README.md,smoke.py}
```

This plugin directory is the **single source of truth** for the `aura-orchestrator` skill and the MCP
server — edit `skills/aura-orchestrator/SKILL.md` and `mcp/` here directly.

---

## Prerequisite: `uv`

The MCP server runs via **`uv run mcp/server.py`**, which reads the PEP 723 inline dependency header in
`server.py` and provisions deps automatically — no committed venv, portable across machines. Install uv:
`curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`). First launch resolves deps
(brief one-time lag). If you'd rather use a plain venv, see `mcp/README.md`.

## Environment variables

The server reads all config/secrets from the environment (never from tool args). Provide them however
your harness injects env into MCP subprocesses. Non-secrets: `ENVIRONMENT`, `MOLECULE_CLIENT_URL`, `MOLECULE_LABS_URL`,
`X402_GATEWAY_URL`, `ACCESS_RESOLVER_ADDRESS`, `ONCHAIN_LAB_FACTORY_ADDRESS`, `LABNFT_ADDRESS`, `CHAIN_ID`,
`EVM_WALLET_ADDRESS`, `EVM_RPC_URL`, `WALLET_BACKEND`. Secrets: `MOLECULE_CONSUMER_CREDENTIAL` (or the
legacy `MOLECULE_API_KEY`), `MOLECULE_SERVICE_TOKEN`.

**Labs consumer auth — `mol_` credentials preferred.** The Labs API is migrating from one shared
`x-api-key` to per-consumer credentials: a single `mol_<consumerId>_<secret>` string sent verbatim as the
`Authorization` header (no `Bearer` prefix — that is reserved for Privy user tokens). Set it as
`MOLECULE_CONSUMER_CREDENTIAL`; the legacy shared key still works via `MOLECULE_API_KEY` until the
migration completes. If both are set the MCP sends both headers, so one config works against either
authorizer mode.

**Wallet secrets are optional — pick ONE backend** (all wallet env is optional until you choose, and
`config_doctor` reports which backend is active and what it still needs):

- **Privy agentic wallet:** `PRIVY_APP_ID`, `PRIVY_APP_SECRET`, `PRIVY_WALLET_ID` (signs via the Privy API).
- **Raw EOA:** `WALLET_PRIVATE_KEY` (the MCP signs locally; the key never leaves the process).

Set `WALLET_BACKEND=privy|eoa` to pin the choice (required only when both are configured). See
`mcp/README.md` for the per-tool breakdown.

---

## Install in Claude Code

**Local (dev):**
```bash
claude --plugin-dir /abs/path/to/molecule-plugin
```
Then `/molecule-desci:aura-orchestrator` etc. Use `/reload-plugins` after edits.

**Via marketplace (distribution):** push this dir to a git repo, then in Claude Code:
```
/plugin marketplace add <owner>/<repo>
/plugin install molecule-desci@molecule-desci-marketplace
```
The MCP server (`molecule`) loads automatically from `.mcp.json` using `${CLAUDE_PLUGIN_ROOT}`.

## Install in Codex

Codex reads `SKILL.md` skills and supports plugins, but its plugin/skills paths are version-dependent —
verify with `codex --version` and `/skills`. The reliable, version-independent route is to register the
MCP server directly and point Codex at the skill:

**Register the MCP server** (`~/.codex/config.toml`):
```toml
[mcp_servers.molecule]
command = "uv"
args = ["run", "/molecule-plugin/mcp/server.py"]

[mcp_servers.molecule.env]
ENVIRONMENT       = "staging"
MOLECULE_CLIENT_URL = "https://…" # current Labs staging deployment base URL
MOLECULE_LABS_URL = "https://staging.graphql.api.molecule.xyz/graphql"
X402_GATEWAY_URL  = "https://…" # staging API Gateway base; no /x402/labs suffix
CHAIN_ID          = "84532"
EVM_WALLET_ADDRESS = "0x…"
ACCESS_RESOLVER_ADDRESS = "0x5493F472602C87318EA5Eff753cDD593bf9bF559"
ONCHAIN_LAB_FACTORY_ADDRESS = "0xd629FE2310b4309a212495F10A47f8436dcEfD90"
LABNFT_ADDRESS = "0x13Ff210695fdb54A7F928ECcc28BC3486c05BB28"
WALLET_BACKEND = "privy"          # or "eoa"
# secrets — configure ONE wallet backend:
#   Privy agentic wallet:
PRIVY_APP_ID = "…"
PRIVY_APP_SECRET = "…"
PRIVY_WALLET_ID = "…"
#   …or a raw EOA (instead of the three Privy vars):
# WALLET_PRIVATE_KEY = "0x…"
MOLECULE_CONSUMER_CREDENTIAL = "…"  # mol_<consumerId>_<secret> — preferred Labs consumer auth
# MOLECULE_API_KEY = "…"            # …or the legacy shared x-api-key (fallback during migration)
MOLECULE_SERVICE_TOKEN = "…"
```
or, equivalently: `codex mcp add molecule --env CHAIN_ID=84532 --env … -- uv run /abs/path/to/molecule-plugin/mcp/server.py`

| Environment | Labs GraphQL | Chain | Factory | LabNFT | AccessResolver |
|---|---|---:|---|---|---|
| `staging` | `https://staging.graphql.api.molecule.xyz/graphql` | Base Sepolia (`84532`) | `0xd629FE2310b4309a212495F10A47f8436dcEfD90` | `0x13Ff210695fdb54A7F928ECcc28BC3486c05BB28` | `0x5493F472602C87318EA5Eff753cDD593bf9bF559` |
| `production` | `https://production.graphql.api.molecule.xyz/graphql` | Base (`8453`) | `0xECdF4f05384056507485C90aeAb0a83268760D6E` | `0x9F96027eeAFb9ad5F2b5d7043B36Ee96B2EeBE92` | `0x89a14Be8f7824d4775053Edad0f2fA2d6767b72B` |

For `X402_GATEWAY_URL`, use the matching stack's `X402GatewayEndpoint_staging` or
`X402GatewayEndpoint_production` output, removing `/x402/labs/{mutation}` so the value is the API Gateway
base URL. `MOLECULE_CLIENT_URL` is likewise the environment's Labs app base URL (production:
`https://labs.molecule.xyz`). The workflow appends `/projects/{shortname}`. Run `config_doctor` after every
environment switch; it reports cross-profile endpoints, chains, and addresses under `configurationIssues`.

**Skills:** if your Codex version supports a plugin marketplace, it can also read
`.claude-plugin/marketplace.json` (interop). Otherwise copy `skills/aura-orchestrator/SKILL.md` into the
skills directory your Codex version scans (`.agents/skills/` or `.codex/skills/` — check `/skills`), or
surface the runbook through `AGENTS.md`.

---

## Verify offline

```bash
cd mcp && uv run smoke.py        # lists tools + exercises compute tools (no network/secrets)
```

---

## Setup & run order

This workflow is **sequential — order matters**. `aura-orchestrator`'s `SKILL.md` documents the internal
phase order; below is the one-time setup that precedes it.

### Step 0 — One-time setup (do once, before running the skill)

1. **Env + MCP.** Install `uv`, register the plugin (Claude) or MCP server (Codex), and set the env vars
   above. Choose `ENVIRONMENT=staging` for Base Sepolia or `ENVIRONMENT=production` for Base mainnet, then
   use only the matching endpoints, chain id, and contracts from the table above.
2. **Wallet — pick a backend (you're in control).** Either:
   - **Privy agentic wallet:** if `PRIVY_WALLET_ID` is unset, **aura-orchestrator Phase 0** creates a Privy
     server wallet **with a policy** (single-chain + per-tx value cap) via the MCP `privy_*` tools; set the
     returned `PRIVY_WALLET_ID`. Or
   - **Raw EOA:** set `WALLET_PRIVATE_KEY` (and optionally `WALLET_BACKEND=eoa`); the MCP signs locally —
     nothing to create.

   Set `WALLET_BACKEND=privy|eoa` when both are configured. Then **fund** the operating wallet: USDC on Base
   (x402 pays per call) + native gas on the mint chain. You can start on one backend and combine/switch to the
   other later — it's just an env change; the lab and its files keep working.
3. **Service token** (private uploads only) → ensure `MOLECULE_SERVICE_TOKEN` is set, or issue one with the
   MCP `issue_service_token` tool. This is an **off-chain JWT** (issued by `generateServiceToken` after a
   wallet signature — *not* an on-chain mint). The Phase 4 **private** variant uses it for the direct DEK
   calls (`labs_generate_dek` / `labs_decrypt_dek`). Not needed for public uploads.

### Step 1 — Run `aura-orchestrator`, choosing the upload visibility

There is **one** workflow — `aura-orchestrator` — and it covers everything end-to-end (resolve/create lab →
createLab → upload → announce → grant/transfer). The only choice is the **Phase 3 upload visibility**:

| Upload visibility | What Phase 3 does | Needs |
|-------------------|-------------------|-------|
| **Public** (default) | Plaintext file, `accessLevel: PUBLIC`, Steps A–C | funded wallet; a research PDF |
| **Private** (encrypted) | Client-side AES-256-GCM envelope encryption, non-PUBLIC `accessLevel` + on-chain access conditions, Steps E0–E6 | funded wallet + `MOLECULE_SERVICE_TOKEN`; a research PDF |

> **x402 pays per call for both** — `initiateCreateOrUpdateFile` / `finishCreateOrUpdateFile` are billed
> regardless of visibility. Everything outside Phase 3 (lab resolve/create, createLab, announcement,
> grant/transfer) is identical for both. The private variant additionally needs a service token (for the
> direct, unpaid DEK generate/decrypt calls that keep the plaintext key inside the MCP).

### `aura-orchestrator` — phase order (do not reorder or skip)

```
Phase 0  Wallet setup                 (Privy agentic wallet OR raw EOA — user's choice)
Phase 1  Resolve-or-create OCL lab    → oclId, shortname, labAccountAddress, labNftTokenId   (owner-only reuse, else mint LabNFT)
Phase 2  createLab (x402)             → registers the lab for oclId   (poll owner-only labs query for indexer)
Phase 3  Upload file to data room     PUBLIC (Steps A–C)  OR  encrypted (E0–E6)
Phase 4  createAnnouncement (x402)    (attach the datasetId from Phase 3)
Phase 5  grantRole / LabNFT hand-off  (optional co-owner)
```
Every phase consumes the previous phase's output (`oclId` + `labAccountAddress` → `datasetId`).

### `aura-orchestrator` Phase 3 — private (encrypted) variant (strict E0 → E6)

Run these **instead of** Phase 3 Steps A–C when the upload visibility is **private**:

```
E0  labs_generate_dek (direct)        → encryptedDek, dekHandle      [no payment]
E1  encrypt_file                      → iv, contentHash, cipherBytes
E2  x402_pay initiateCreateOrUpdateFile     → uploadToken, uploadUrl  [PAID]
E3  s3_upload (the .enc ciphertext)                                   [no payment]
E4  build_access_conditions (oclId + labAccountAddress)              → json   (hasRole OR isAuthorizedSignerForTba)
E5  x402_pay finishCreateOrUpdateFile (+ encryptionMetadata)         [PAID]
E6  labs_decrypt_dek (oclId+filePath) → decrypt_file → verify SHA-256   [optional, no payment]
```
`contentLength` in E2 is the **ciphertext** size (`cipherBytes` from E1). The plaintext DEK never leaves
the MCP — only the opaque `dekHandle` is passed between E0→E1 and E6.

## ⚠️ Running cost

`aura-orchestrator` Phases 2–5 perform **paid x402 mutations — real USDC on Base per call** — and
on-chain transactions (LabNFT mint / grantRole / transfer, plus the mint fee). They need a funded operating
wallet (**Privy agentic wallet or raw EOA — your choice**) and a valid service token / API key (the
**private** upload variant also needs `MOLECULE_SERVICE_TOKEN`). For a no-spend smoke, use only the
compute/read tools (`encrypt_file`/
`decrypt_file`, `build_access_conditions`, `sha256_file`, `ocl_read`; `labs_generate_dek` needs only a
service token, no payment).
