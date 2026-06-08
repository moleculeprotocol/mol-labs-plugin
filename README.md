# molecule-desci — cross-harness plugin

Packages the DeSci orchestration skill (+ a wallet helper) and the `molecule` MCP server into one
installable plugin that works under **Claude Code** and **OpenAI Codex** (and any MCP host, via the
server alone).

- **`aura-orchestrator`** — the whole molecule in one skill: POI registration → IP-NFT minting → project
  creation → data-room file upload → announcement → transfer. V2 surface, keyed on `ipnftUid`. The file
  upload (Phase 4) is the only branch: choose **public** (plaintext) or **private** (client-side
  AES-256-GCM envelope-encrypted, access-controlled) — **x402 pays per call either way**.
- **`privy-agentic-wallets`** — helper for creating/managing the Privy server wallet (with a policy) that
  signs payments and on-chain transactions. Run once if `PRIVY_WALLET_ID` is unset.
- **`molecule` MCP server** (`mcp/server.py`, Python/FastMCP, stdio) — Privy wallet ops, POI, Labs
  GraphQL, the full x402 payment flow, S3 upload, AES-256-GCM envelope crypto, ABI encoding, on-chain
  access conditions (`isAuthorizedSignerForIpnft`).

> **The MCP server is the portable core** — both harnesses speak MCP. Skills (`SKILL.md`) are a shared
> standard both now read. Only the *plugin manifest* differs per harness, so this package ships both
> `.claude-plugin/` and `.codex-plugin/` manifests pointing at the same `skills/` and `.mcp.json`.

```
molecule-plugin/
├── .claude-plugin/{plugin.json, marketplace.json}   # Claude Code
├── .codex-plugin/plugin.json                         # Codex
├── .mcp.json                                         # shared MCP server config (uv run)
├── skills/{aura-orchestrator,privy-agentic-wallets}/SKILL.md
└── mcp/{server.py,pyproject.toml,requirements.txt,README.md,smoke.py}
```

This plugin directory is the **single source of truth** — it is the only version-controlled copy, so
edit the skills (`skills/<name>/SKILL.md`) and the MCP server (`mcp/`) here directly. (Older unversioned
copies under `molecule_core/skills/` are no longer synced and may be stale — ignore them.)

---

## Prerequisite: `uv`

The MCP server runs via **`uv run mcp/server.py`**, which reads the PEP 723 inline dependency header in
`server.py` and provisions deps automatically — no committed venv, portable across machines. Install uv:
`curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`). First launch resolves deps
(brief one-time lag). If you'd rather use a plain venv, see `mcp/README.md`.

## Environment variables

The server reads all config/secrets from the environment (never from tool args). Provide them however
your harness injects env into MCP subprocesses. Non-secrets: `MOLECULE_CLIENT_URL`, `MOLECULE_LABS_URL`,
`X402_GATEWAY_URL`, `ACCESS_RESOLVER_ADDRESS`, `IPNFT_CONTRACT_ADDRESS`, `CHAIN_ID`, `ENVIRONMENT`,
`EVM_WALLET_ADDRESS`, `EXPERIMENT_COST_CENTS`, `IPNFT_UID`. Secrets: `PRIVY_APP_ID`, `PRIVY_APP_SECRET`,
`PRIVY_WALLET_ID`, `POI_API_KEY`, `MOLECULE_API_KEY`, `MOLECULE_SERVICE_TOKEN`. See `mcp/README.md` for
the per-tool breakdown.

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
MCP server directly and point Codex at the skills:

**Register the MCP server** (`~/.codex/config.toml`):
```toml
[mcp_servers.molecule]
command = "uv"
args = ["run", "/abs/path/to/molecule-plugin/mcp/server.py"]

[mcp_servers.molecule.env]
MOLECULE_LABS_URL = "https://migration.graphql.api.molecule.xyz/graphql"
X402_GATEWAY_URL  = "https://…"
CHAIN_ID          = "84532"
ENVIRONMENT       = "migration"
EVM_WALLET_ADDRESS = "0x…"
ACCESS_RESOLVER_ADDRESS = "0x…"
# secrets:
PRIVY_APP_ID = "…"
PRIVY_APP_SECRET = "…"
PRIVY_WALLET_ID = "…"
POI_API_KEY = "…"
MOLECULE_API_KEY = "…"
MOLECULE_SERVICE_TOKEN = "…"
```
or, equivalently: `codex mcp add molecule --env CHAIN_ID=84532 --env … -- uv run /abs/path/to/molecule-plugin/mcp/server.py`

**Skills:** if your Codex version supports a plugin marketplace, it can also read
`.claude-plugin/marketplace.json` (interop). Otherwise copy `skills/<name>/SKILL.md` into the skills
directory your Codex version scans (`.agents/skills/` or `.codex/skills/` — check `/skills`), or surface
the runbook through `AGENTS.md`.

---

## Verify offline

```bash
cd mcp && uv run smoke.py        # lists tools + exercises compute tools (no network/secrets)
```

---

## Which skill, in what order

This workflow is **sequential — order matters**. Each skill's `SKILL.md` documents its own internal step
order; this is the cross-skill map.

### Step 0 — One-time setup (do once, before any skill)

1. **Env + MCP.** Install `uv`, register the plugin (Claude) or MCP server (Codex), and set the env vars
   above. Pick the surface with `MOLECULE_LABS_URL` / `X402_GATEWAY_URL` / `CHAIN_ID` / `ENVIRONMENT`.
2. **Wallet** → run **`privy-agentic-wallets`** *only if* `PRIVY_WALLET_ID` is unset. It creates a Privy
   server wallet **with a policy** (single-chain + per-tx value cap); set the returned `PRIVY_WALLET_ID`.
   Then **fund** that wallet: USDC on Base (x402 pays per call) + native gas on the mint chain.
3. **Service token** (private uploads only) → ensure `MOLECULE_SERVICE_TOKEN` is set, or issue one with the
   MCP `issue_service_token` tool. This is an **off-chain JWT** (issued by `generateServiceToken` after a
   wallet signature — *not* an on-chain mint). The Phase 4 **private** variant uses it for the direct DEK
   calls (`labs_generate_dek` / `labs_decrypt_dek`). Not needed for public uploads.

### Step 1 — Run `aura-orchestrator`, choosing the upload visibility

There is **one** workflow — `aura-orchestrator` — and it covers everything end-to-end (POI → mint →
project → upload → announce → transfer). The only choice is the **Phase 4 upload visibility**:

| Upload visibility | What Phase 4 does | Needs |
|-------------------|-------------------|-------|
| **Public** (default) | Plaintext file, `accessLevel: PUBLIC`, Steps A–C | funded wallet; a research PDF |
| **Private** (encrypted) | Client-side AES-256-GCM envelope encryption, non-PUBLIC `accessLevel` + on-chain access conditions, Steps E0–E6 | funded wallet + `MOLECULE_SERVICE_TOKEN`; a research PDF |

> **x402 pays per call for both** — `initiateCreateOrUpdateFileV2` / `finishCreateOrUpdateFileV2` are
> billed regardless of visibility. Everything outside Phase 4 (POI, mint, project, announcement, transfer)
> is identical for both. The private variant additionally needs a service token (for the direct, unpaid
> DEK generate/decrypt calls that keep the plaintext key inside the MCP).

### `aura-orchestrator` — phase order (do not reorder or skip)

```
Phase 1  POI registration            → reservationId (= IP-NFT tokenId)
Phase 2  IP-NFT mint                  (sign terms → mint)
Phase 3  createProject               → ipnftUid     [wait ~90s after mint]
Phase 4  Upload file to data room     PUBLIC (Steps A–C)  OR  encrypted (E0–E6)   [wait ~90s after project]
Phase 5  createAnnouncementV2         (attach the datasetId from Phase 4)
Phase 6  Transfer IP-NFT + addProjectOwner   (optional co-owner)
```
Every phase consumes the previous phase's output (`reservationId` → `ipnftUid` → `datasetId`). The two
**90-second waits** are real: on-chain ownership and data-room provisioning are async.

### `aura-orchestrator` Phase 4 — private (encrypted) variant (strict E0 → E6)

Run these **instead of** Phase 4 Steps A–C when the upload visibility is **private**:

```
E0  labs_generate_dek (direct)        → encryptedDek, dekHandle      [no payment]
E1  encrypt_file                      → iv, contentHash, cipherBytes
E2  x402_pay initiateCreateOrUpdateFileV2   → uploadToken, uploadUrl  [PAID]
E3  s3_upload (the .enc ciphertext)                                   [no payment]
E4  build_access_conditions (ipnft-signer, reservationId = tokenId)  → json
E5  x402_pay finishCreateOrUpdateFileV2 (+ encryptionMetadata)        [PAID]
E6  labs_decrypt_dek (ipnftUid+filePath) → decrypt_file → verify SHA-256   [optional, no payment]
```
`contentLength` in E2 is the **ciphertext** size (`cipherBytes` from E1). The plaintext DEK never leaves
the MCP — only the opaque `dekHandle` is passed between E0→E1 and E6.

## ⚠️ Running cost

`aura-orchestrator` Phases 3–6 perform **paid x402 mutations — real USDC on Base per call** — and
on-chain transactions (mint/transfer). They need a funded Privy wallet and a valid service token / API
key (the **private** upload variant also needs `MOLECULE_SERVICE_TOKEN`). For a no-spend smoke, use
only the compute/direct tools (`encrypt_file`/`decrypt_file`, `build_access_conditions`, `sha256_file`;
`labs_generate_dek` needs only a service token, no payment).
