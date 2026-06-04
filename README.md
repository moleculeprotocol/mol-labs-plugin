# molecule-desci — cross-harness plugin

Packages the two DeSci skills + the `molecule` MCP server into one installable plugin that works
under **Claude Code** and **OpenAI Codex** (and any MCP host, via the server alone).

- **`aura-orchestrator`** — POI registration → IP-NFT minting → project creation → file upload
  (public or encrypted) → announcement → transfer. V2 surface, keyed on `ipnftUid`.
- **`molecule-x402`** — client-side-encrypted data-room uploads to an existing project (V2/`ipnftUid`),
  paid per call via x402.
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
├── skills/{aura-orchestrator,molecule-x402,privy-agentic-wallets}/SKILL.md
└── mcp/{server.py,pyproject.toml,requirements.txt,README.md,smoke.py}
```

The canonical, edit-here sources live in `../skills/…` and `../skills/molecule-mcp/`. This directory is
the packaged form — run `./sync-from-source.sh` after editing the sources to refresh it.

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
Then `/molecule-desci:molecule-x402` etc. Use `/reload-plugins` after edits.

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
3. **Service token** → ensure `MOLECULE_SERVICE_TOKEN` is set, or mint one with the MCP `mint_service_token`
   tool. Both skills use it for the direct DEK calls (`labs_generate_dek` / `labs_decrypt_dek`).

### Step 1 — Pick ONE workflow

| You want to… | Run | Needs |
|--------------|-----|-------|
| Register a discovery end-to-end (POI → mint → project → upload → announce → transfer) | **`aura-orchestrator`** | funded wallet + service token; a research PDF |
| Add a private/encrypted file to an **existing** project | **`molecule-x402`** | the project's `ipnftUid` (already minted) |

> `molecule-x402` is essentially `aura-orchestrator`'s Phase 4 *encrypted* variant, extracted as a
> stand-alone skill. Use **aura** to create everything from scratch; use **x402** when the IP-NFT /
> project already exists and you only need to attach a confidential file.

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

### `molecule-x402` — step order (strict E0 → E6)

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

`molecule-x402` and `aura-orchestrator` Phases 3–6 perform **paid x402 mutations — real USDC on Base per
call** — and on-chain transactions (mint/transfer). They need a funded Privy wallet, a valid service
token / API key, and (for `molecule-x402`) an existing V2 project (`ipnftUid`). For a no-spend smoke, use
only the compute/direct tools (`encrypt_file`/`decrypt_file`, `build_access_conditions`, `sha256_file`;
`labs_generate_dek` needs only a service token, no payment).
