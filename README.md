# molecule-desci — cross-harness plugin

Packages the two DeSci skills + the `molecule` MCP server into one installable plugin that works
under **Claude Code** and **OpenAI Codex** (and any MCP host, via the server alone).

- **`aura-orchestrator`** — POI registration → IP-NFT minting → project creation → file upload
  (public or encrypted) → announcement → transfer. V2/`ipnftUid` first, with OCL fallback.
- **`molecule-x402`** — client-side-encrypted data-room uploads (OCL/`oclId`), paid per call via x402.
- **`molecule` MCP server** (`mcp/server.py`, Python/FastMCP, stdio) — Privy wallet ops, POI, Labs
  GraphQL, the full x402 payment flow, S3 upload, AES-256-GCM envelope crypto, oclId/ABI/access-conditions.

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
`EVM_WALLET_ADDRESS`, `EXPERIMENT_COST_CENTS`, `OCL_ID`. Secrets: `PRIVY_APP_ID`, `PRIVY_APP_SECRET`,
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

## ⚠️ Running cost

`molecule-x402` and `aura-orchestrator` Phases 3–6 perform **paid x402 mutations — real USDC on Base per
call** — and on-chain transactions (mint/transfer). They need a funded Privy wallet, a valid service
token / API key, and an existing IP-NFT or OCL lab. For a no-spend smoke, use only the compute/direct
tools (`pack_ocl_id`, `encrypt_file`/`decrypt_file`, `build_access_conditions`, `labs_generate_dek`).
