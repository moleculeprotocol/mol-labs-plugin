# molecule-mcp

A single **stdio MCP server** that backs the [`aura-orchestrator`](../aura-orchestrator/SKILL.md)
and [`molecule-x402`](../molecule-x402/SKILL.md) skills. Every `curl` / `http_request` / `node -e`
step in those skills is now a typed MCP tool, so the agent calls **one tool per operation**
instead of hand-assembling shell commands, base64 dances, and EIP-712 payloads.

- **Language:** Python (FastMCP) — chosen over Bun/Node so the plugin runs under **any**
  MCP-capable harness (Claude Code, Codex, …) with only a Python interpreter.
- **Transport:** stdio (newline-delimited JSON-RPC)
- **Deps:** `mcp`, `httpx`, `cryptography`, `eth-abi`, `eth-utils`, `eth-hash[pycryptodome]`

Nothing is ever written to **stdout** except the JSON-RPC protocol (FastMCP owns stdout); all
diagnostics go to **stderr**. Secrets (`PRIVY_APP_SECRET`, the plaintext DEK, the service token,
API keys) are never logged or returned to the caller.

---

## Install & register

Pick whichever runner your harness can spawn. All three run the same `server.py`.

### Option A — venv (most portable; only needs `python3`)

```bash
cd skills/molecule-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Register the venv's interpreter (absolute paths, no PATH/shell dependency at spawn time):

```jsonc
{
  "mcpServers": {
    "molecule": {
      "command": "/abs/path/to/skills/molecule-mcp/.venv/bin/python",
      "args": ["/abs/path/to/skills/molecule-mcp/server.py"]
    }
  }
}
```

### Option B — uv (zero manual venv; `server.py` carries PEP 723 inline deps)

```jsonc
{
  "mcpServers": {
    "molecule": {
      "command": "uv",
      "args": ["run", "/abs/path/to/skills/molecule-mcp/server.py"]
    }
  }
}
```

`uv run` reads the `# /// script ... # ///` header in `server.py` and provisions deps into a
managed cache automatically. (Use the absolute path to `uv` if your harness spawns without your
shell PATH.)

### Option C — system / pipx

`pip install -r requirements.txt` (or `pipx`/`pip install .` for the `molecule-mcp` console
script) into whatever interpreter the harness will run, then register that `python` + `server.py`.

After registering, enable it in your harness (for Claude Code: add `"molecule"` to
`enabledMcpjsonServers` in `.claude/settings.local.json`).

### Environment variables

The server reads all configuration from **environment variables**, which the harness injects into
the MCP subprocess (for Claude Code, from `.claude/settings.json` non-secrets and
`.claude/settings.local.json` secrets). The skills therefore **never pass secrets as tool
arguments** — only file paths, queries, addresses, and the ephemeral `dekHandle`.

| Variable | Where | Used by |
|----------|-------|---------|
| `MOLECULE_CLIENT_URL` | settings.json | `poi_register` |
| `MOLECULE_LABS_URL` | settings.json | `labs_graphql`, `labs_generate_dek`, `labs_decrypt_dek`, `mint_service_token` |
| `X402_GATEWAY_URL` | settings.json | `x402_pay` |
| `ACCESS_RESOLVER_ADDRESS` | settings.json | `build_access_conditions` |
| `IPNFT_CONTRACT_ADDRESS` | settings.json | (skill body) |
| `CHAIN_ID` | settings.json | `privy_create_policy`, `privy_send_transaction`, `build_access_conditions` |
| `ENVIRONMENT` | settings.json | `build_access_conditions` (base vs baseSepolia) |
| `EVM_WALLET_ADDRESS` | settings.json | wallet resolution + `x-wallet-address` |
| `EXPERIMENT_COST_CENTS` | settings.json | (skill body) |
| `PRIVY_APP_ID` | settings.local.json | all Privy tools (basic-auth user) |
| `PRIVY_APP_SECRET` | settings.local.json | all Privy tools (basic-auth pass) |
| `PRIVY_WALLET_ID` | settings.local.json | wallet that signs/sends |
| `POI_API_KEY` | settings.local.json | `poi_register` |
| `MOLECULE_API_KEY` | settings.local.json | `labs_graphql` (auth=`api-key`) |
| `MOLECULE_SERVICE_TOKEN` | settings.local.json | `labs_graphql`/DEK tools (auth=`service-token`) |

If a tool needs a variable that isn't set, it returns a clear error naming the missing
variable(s) — it never guesses an endpoint or address.

### Verify offline

`.venv/bin/python smoke.py` lists all tools and exercises the pure-compute ones — no network or
secrets required. It regression-checks `hex_to_uint256` and `abi_encode` against known-good values,
confirms `abi_encode` rejects non-`0x` bytes, builds an `ipnft-signer` access condition, and
round-trips AES-256-GCM encrypt/decrypt.

---

## Tools

### Privy (wallet management, signing, sending)

| Tool | Replaces | Returns |
|------|----------|---------|
| `privy_get_wallet_address` | aura `get_wallet_address`; x402 "resolve wallet" curl | `{ address, walletId }` |
| `privy_list_wallets` | aura Step 0b curl | wallet list |
| `privy_create_policy` | aura Step 0c curl | `{ policyId }` |
| `privy_create_wallet` | aura Step 0d curl | `{ walletId, address }` |
| `privy_sign_message` | aura `sign_message` (terms); service-token sign-in | `{ signature }` |
| `privy_sign_typed_data` | ad-hoc EIP-712 (x402 does this internally) | `{ signature }` |
| `privy_send_transaction` | aura `sign_and_send_transaction` (POI anchor, mint, transfer) | `{ txHash }` |

### Molecule HTTP

| Tool | Replaces | Returns |
|------|----------|---------|
| `poi_register` | aura Phase 1 POI curl/http_request | `{ poiTo, poiData, merkleRoot, response }` |
| `labs_graphql` | aura Steps 2,3,5,6,8 GraphQL; public sign-in queries | `{ data, errors }` |
| `x402_pay` | the **entire** P1–P7 flow for one mutation | `{ data, errors, settlement }` |
| `s3_upload` | aura Step 4/B image+file PUT; x402 E3 ciphertext PUT | `{ status, ok }` |

`x402_pay` sends the unpaid request, decodes the `payment-required` challenge, signs the EIP-712
`TransferWithAuthorization` with the Privy wallet (standard camelCase `primaryType`), builds and
base64-encodes the payment payload, and retries with the `PAYMENT-SIGNATURE` header — all
internally. The single top-level GraphQL field in `query` **must equal** `mutation`.

### DEK-aware (the plaintext DEK never leaves the server)

| Tool | Replaces | Returns |
|------|----------|---------|
| `labs_generate_dek` | E0 `generateDataEncryptionKey` | `{ encryptedDek, encryptionSystem, dekHandle }` |
| `labs_decrypt_dek` | E6 `decryptDataKey` | `{ iv, dekHandle, message }` |

These wrap the DEK mutations and stash the **plaintext DEK in server memory**, returning an opaque
`dekHandle` instead. The agent passes the handle to `encrypt_file` / `decrypt_file`, so the
one-shot secret DEK never enters the conversation, a file, or a log. `labs_decrypt_dek` takes
`ipnftUid`+`filePath` (data-room file, `{contractAddress}_{tokenId}`) or `tokenUri`+`agreementUrl`
(IPFS agreement) — matching `encryption.graphql`. Both DEK mutations are now x402-whitelisted, but
the tools default to `transport='direct'` (service-token) so the plaintext DEK stays in-process and
no payment is needed.

### Crypto / encoding (pure compute)

| Tool | Replaces | Returns |
|------|----------|---------|
| `encrypt_file` | E1 `node -e` encrypt | `{ iv, contentHash, cipherBytes }` |
| `decrypt_file` | E6 `node -e` decrypt | `{ plaintextSha256, bytes }` |
| `sha256_file` | `shasum -a 256` / `wc -c` | `{ sha256, bytes }` |
| `hex_to_uint256` | aura `hex_to_uint256` | `{ decimal, isSmall }` |
| `abi_encode` | aura `abi_encode` | `{ calldata }` |
| `build_access_conditions` | E4 access-condition JSON (`ipnft-signer`) | `{ conditions, json }` |

`encrypt_file`/`decrypt_file` are byte-for-byte compatible with the Labs client
`encryptFileWithKms`/`decryptFileWithKms`: AES-256-GCM, random 12-byte IV, 16-byte tag
**appended** to the ciphertext, `contentHash` = hex SHA-256 of the **plaintext**.
`abi_encode` rejects non-`0x` `bytes`/`bytesN` arguments (a non-`0x` string would otherwise be
silently misread as UTF-8). `build_access_conditions` builds the V2 `isAuthorizedSignerForIpnft`
gate keyed on the IP-NFT tokenId.

### Bootstrap

| Tool | Replaces | Returns |
|------|----------|---------|
| `mint_service_token` | service-token mint (3-step flow) | `{ token, tokenId, expiresAt }` |

---

## Source-of-truth parity

| Behavior | Replicated from |
|----------|-----------------|
| x402 challenge / payment header / `PAYMENT-SIGNATURE` | `desci-infra/lambda/x402-gateway-lambda/index.ts` |
| x402 mutation whitelist | `desci-infra/lambda/x402-gateway-lambda/mutations.ts` |
| AES-256-GCM envelope (12-byte IV, appended tag, plaintext hash) | `desci-ecosystem/packages/storage/src/lib/encryption/kms-envelope.ts` |
| `accessControlConditions` (`isAuthorizedSignerForIpnft`) | `desci-infra/lambda/common/utils/access-control-conditions.ts` + `desci-infra/bruno/desci-labs/v2/25-finishEncryptedFileUploadV2.bru` |
| EIP-712 typed-data `primaryType` | `skills/privy-agentic-wallets/references/transactions.md` |
| GraphQL field shapes / `EncryptionMetadataInput` / `decryptDataKey` args | `desci-infra/graphql/schemas/{ip-hubs,encryption}.graphql` |
| Request shapes & auth headers | `desci-infra/bruno/desci-labs/v2` + `desci-infra/bruno/service-auth` |
