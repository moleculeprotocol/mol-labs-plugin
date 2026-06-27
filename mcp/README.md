# molecule-mcp

A single **stdio MCP server** that backs the [`aura-orchestrator`](../skills/aura-orchestrator/SKILL.md)
skill (resolve/create On-Chain Lab → createLab → public *or* private/encrypted data-room upload →
announce → grant/transfer; OCL/V3 surface, keyed on `oclId`), including all Privy wallet ops. Every
`curl` / `http_request` / `node -e` step in that skill is now a typed MCP tool, so the agent calls **one
tool per operation**
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
cd mcp
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Register the venv's interpreter (absolute paths, no PATH/shell dependency at spawn time):

```jsonc
{
  "mcpServers": {
    "molecule": {
      "command": "/abs/path/to/molecule-plugin/mcp/.venv/bin/python",
      "args": ["/abs/path/to/molecule-plugin/mcp/server.py"]
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
      "args": ["run", "/abs/path/to/molecule-plugin/mcp/server.py"]
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
`.claude/settings.local.json` secrets). The skill therefore **never passes secrets as tool
arguments** — only file paths, queries, addresses, and the ephemeral `dekHandle`.

| Variable | Where | Used by |
|----------|-------|---------|
| `MOLECULE_CLIENT_URL` | settings.json | (skill body — project URL `/projects/{oclId}`) |
| `MOLECULE_LABS_URL` | settings.json | `labs_graphql`, `labs_generate_dek`, `labs_decrypt_dek`, `issue_service_token` |
| `X402_GATEWAY_URL` | settings.json | `x402_pay` |
| `ACCESS_RESOLVER_ADDRESS` | settings.json | `build_access_conditions`, `ocl_read` (hasRole/TBA), grantRole (skill) |
| `ONCHAIN_LAB_FACTORY_ADDRESS` | settings.json | (skill body — `mintAndCreateAccount`, `ocl_read` oclIdOfToken/accountOfToken) |
| `LABNFT_ADDRESS` | settings.json | (skill body — `ocl_read` mintFeeWei/ownerOf, LabNFT transfer) |
| `CHAIN_ID` | settings.json | `privy_create_policy`, `privy_send_transaction`, `build_access_conditions`, `ocl_read` |
| `EVM_RPC_URL` | settings.json | `ocl_read`, `ocl_tx_identity`, `privy_send_raw_transaction` |
| `EVM_WALLET_ADDRESS` | settings.json | wallet resolution + `x-wallet-address` |
| `PRIVY_APP_ID` | settings.local.json | all Privy tools (basic-auth user) |
| `PRIVY_APP_SECRET` | settings.local.json | all Privy tools (basic-auth pass) |
| `PRIVY_WALLET_ID` | settings.local.json | wallet that signs/sends |
| `MOLECULE_API_KEY` | settings.local.json | `labs_graphql` (auth=`api-key`) |
| `MOLECULE_SERVICE_TOKEN` | settings.local.json | `labs_graphql`/DEK tools (auth=`service-token`) |

If a tool needs a variable that isn't set, it returns a clear error naming the missing
variable(s) — it never guesses an endpoint or address.

### Verify offline

`.venv/bin/python smoke.py` lists all tools and exercises the pure-compute ones — no network or
secrets required. It confirms the legacy IPNFT tools are gone and the OCL primitives present,
regression-checks `abi_encode` against known-good values, confirms it rejects non-`0x` bytes, builds the
OCL access conditions (`hasRole` OR `isAuthorizedSignerForTba`, chain `sepolia-base`), and round-trips
AES-256-GCM encrypt/decrypt.

---

## Tools

### Privy (wallet management, signing, sending)

| Tool | Replaces | Returns |
|------|----------|---------|
| `privy_get_wallet_address` | aura `get_wallet_address`; x402 "resolve wallet" curl | `{ address, walletId }` |
| `privy_list_wallets` | aura Step 0b curl | wallet list |
| `privy_create_policy` | aura Step 0c curl | `{ policyId }` |
| `privy_create_wallet` | aura Step 0d curl | `{ walletId, address }` |
| `privy_send_transaction` | LabNFT mint (`mintAndCreateAccount`), `grantRole` | `{ txHash }` |
| `privy_send_raw_transaction` | sign-only + self-broadcast (LabNFT `safeTransferFrom`) | `{ txHash, nonce, from, gasLimit }` |
| `ocl_read` | read-only `eth_call` view (mintFeeWei, oclIdOfToken, accountOfToken, ownerOf, hasRole, isAuthorizedSignerForTba) | `{ values, raw }` |
| `ocl_tx_identity` | parse a `mintAndCreateAccount` receipt (`OclIdentityCreated`) | `{ tokenId, account, oclId, found }` |

### Molecule HTTP

| Tool | Replaces | Returns |
|------|----------|---------|
| `labs_graphql` | direct GraphQL (`labs(walletAddress)`, `updateLabNftMetadata`, `generateLabImageUploadUrl`, sign-in) | `{ data, errors }` |
| `x402_pay` | the **entire** P1–P7 flow for one mutation | `{ data, errors, settlement }` |
| `s3_upload` | cover-image + file PUT; E3 ciphertext PUT | `{ status, ok }` |

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
`oclId`+`filePath` (data-room file) or `tokenUri`+`agreementUrl` (IPFS agreement) — matching
`encryption.graphql`. Both DEK mutations are x402-whitelisted, but the tools default to
`transport='direct'` (service-token) so the plaintext DEK stays in-process and no payment is needed.

### Crypto / encoding (pure compute)

| Tool | Replaces | Returns |
|------|----------|---------|
| `encrypt_file` | E1 `node -e` encrypt | `{ iv, contentHash, cipherBytes }` |
| `decrypt_file` | E6 `node -e` decrypt | `{ plaintextSha256, bytes }` |
| `sha256_file` | `shasum -a 256` / `wc -c` | `{ sha256, bytes }` |
| `abi_encode` | calldata for `mintAndCreateAccount` / `grantRole` / `safeTransferFrom` | `{ calldata }` |
| `build_access_conditions` | E4 OCL access-condition JSON | `{ conditions, json }` |

`encrypt_file`/`decrypt_file` are byte-for-byte compatible with the Labs client
`encryptFileWithKms`/`decryptFileWithKms`: AES-256-GCM, random 12-byte IV, 16-byte tag
**appended** to the ciphertext, `contentHash` = hex SHA-256 of the **plaintext**.
`abi_encode` rejects non-`0x` `bytes`/`bytesN` arguments (a non-`0x` string would otherwise be
silently misread as UTF-8). `build_access_conditions` builds the OCL gate — an OR of
`hasRole(oclId, :userAddress, CONTRIBUTOR)` and `isAuthorizedSignerForTba(:userAddress, labAccountAddress)`
on AccessResolver V3, keyed on the lab's `oclId` + token-bound account.

### Confidentiality latch (fail-closed privacy guard)

A confidential file must never fall back to a public upload if the encrypted path fails — that would
publish the resource in plaintext. The skill instructs the agent accordingly, but instructions are
not a guarantee, so the server enforces it at the tool boundary, **non-overridably** (no force flag):

- `encrypt_file` records the **plaintext SHA-256** (and resolved path) of the file it encrypts.
  `s3_upload` then **refuses** to PUT any bytes whose SHA-256 (or path) matches — the plaintext of a
  file the agent encrypted can never reach S3, regardless of `accessLevel` or which upload path the
  agent takes. Uploading the `.enc` ciphertext, the cover image, or a genuinely-public file is
  unaffected (different bytes / never encrypted).
- `build_access_conditions` records the lab's **oclId**. `x402_pay` (and `labs_graphql`) then **refuse**
  `finishCreateOrUpdateFile` for that oclId when `accessLevel` is `PUBLIC` or `encryptionMetadata`
  is missing — a file whose access conditions were built can only be finalized non-PUBLIC + encrypted.

The latch is process-local (cleared on subprocess restart, like the DEK store) and keyed on exact
plaintext bytes + oclId, so it has no false positives for legitimate public uploads or for a
different lab handled in the same session.

### Bootstrap

| Tool | Replaces | Returns |
|------|----------|---------|
| `issue_service_token` | issue an off-chain JWT service token bound to the Privy AGENT wallet (3-step flow) | `{ token, tokenId, expiresAt }` |
| `issue_owner_service_token` | issue an off-chain JWT service token bound to the OWNER EOA (signs with `WALLET_PRIVATE_KEY`) | `{ token, tokenId, address, expiresAt }` |
