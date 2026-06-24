# molecule-mcp

A single **stdio MCP server** that backs the [`aura-orchestrator`](../aura-orchestrator/SKILL.md)
skill (POI → mint → project → public *or* private/encrypted data-room upload → announce → transfer).
Every `curl` / `http_request` / `node -e` *non-signing* step in that skill is a typed MCP tool, so the
agent calls **one tool per operation** instead of hand-assembling shell commands and base64 dances.

**Custody-free by design.** This server **never holds a private key, signs a message, or broadcasts a
transaction.** It *crafts* the requests/payloads the molecule needs (transactions, EIP-712 typed-data,
GraphQL, x402 challenges, encryption) and runs only the non-signing HTTP around them. Every
wallet-dependent step is handed back to the **caller's wallet** — a Privy agentic wallet (the recommended
first option) or any key the caller controls — to sign/send; see
[`../aura-orchestrator/references/wallet-signing.md`](../aura-orchestrator/references/wallet-signing.md).

- **Language:** Python (FastMCP) — chosen over Bun/Node so the plugin runs under **any**
  MCP-capable harness (Claude Code, Codex, …) with only a Python interpreter.
- **Transport:** stdio (newline-delimited JSON-RPC)
- **Deps:** `mcp`, `httpx`, `cryptography`, `eth-abi`, `eth-utils`, `eth-hash[pycryptodome]`

Nothing is ever written to **stdout** except the JSON-RPC protocol (FastMCP owns stdout); all
diagnostics go to **stderr**. The server holds **no wallet credentials**; the secrets it does read (the
service token, API keys) and the in-process plaintext DEK are never logged or returned to the caller.

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
`.claude/settings.local.json` secrets). It reads **no wallet credentials** (no private key, no Privy
secret) — those stay with the caller's wallet. The skill **never passes secrets as tool arguments** —
only file paths, queries, public addresses, signatures the caller produced, and the ephemeral `dekHandle`.

| Variable | Where | Used by |
|----------|-------|---------|
| `MOLECULE_CLIENT_URL` | settings.json | `poi_register` |
| `MOLECULE_LABS_URL` | settings.json | `labs_graphql`, `labs_generate_dek`, `labs_decrypt_dek`, `service_signin_message`, `service_token_create` |
| `X402_GATEWAY_URL` | settings.json | `x402_prepare`, `x402_submit` |
| `ACCESS_RESOLVER_ADDRESS` | settings.json | `build_access_conditions` |
| `IPNFT_CONTRACT_ADDRESS` | settings.json | (skill body) |
| `CHAIN_ID` | settings.json | `prepare_transaction`, `build_access_conditions` |
| `ENVIRONMENT` | settings.json | `build_access_conditions` (base vs baseSepolia) |
| `EVM_WALLET_ADDRESS` | settings.json | default signer **public address** (x402 `from`, `x-wallet-address`) |
| `EXPERIMENT_COST_CENTS` | settings.json | (skill body) |
| `POI_API_KEY` | settings.local.json | `poi_register` |
| `MOLECULE_API_KEY` | settings.local.json | `labs_graphql` (auth=`api-key`) |
| `MOLECULE_SERVICE_TOKEN` | settings.local.json | `labs_graphql`/DEK tools (auth=`service-token`) |

**Not read by this server:** your wallet credentials. A Privy `PRIVY_APP_ID` / `PRIVY_APP_SECRET` /
`PRIVY_WALLET_ID` (recommended) or your own private key live with your **signer**, not here — the MCP
only ever needs the **public** `EVM_WALLET_ADDRESS`.

If a tool needs a variable that isn't set, it returns a clear error naming the missing
variable(s) — it never guesses an endpoint or address.

### Verify offline

`.venv/bin/python smoke.py` lists all tools and exercises the pure-compute ones — no network or
secrets required. It regression-checks `hex_to_uint256` and `abi_encode` against known-good values,
confirms `abi_encode` rejects non-`0x` bytes, builds an `ipnft-signer` access condition, validates
`prepare_transaction` normalization, and round-trips AES-256-GCM encrypt/decrypt.

---

## Tools

### Wallet handoff (the MCP prepares; the caller's wallet signs/sends)

The server signs nothing — these tools craft what the caller signs/sends and accept the result back.

| Tool | Crafts / does | Returns |
|------|---------------|---------|
| `prepare_transaction` | normalize a tx request (POI anchor, mint, transfer) for the caller to sign + broadcast | `{ transaction:{to,data,value,valueWei,chainId,caip2}, note }` |
| `x402_prepare` | fetch the 402 challenge + build the EIP-712 the caller signs | `{ prepared:{endpoint,query,variables,accepted,resource,authorization,typedData} }` |
| `x402_submit` | post the caller's signed x402 payment (does NOT sign) | `{ data, errors, settlement }` |

`x402_prepare` sends the unpaid request, decodes the `payment-required` challenge, and builds the EIP-712
`TransferWithAuthorization` (standard camelCase `primaryType`) with `from = walletAddress` — then stops.
**The caller signs `prepared.typedData` with their own wallet** (Privy: remap `primaryType`→`primary_type`;
own key: sign as-is) and calls `x402_submit(prepared, signature)`, which base64-encodes the payment payload
and retries with the `PAYMENT-SIGNATURE` header. The single top-level GraphQL field in `query` **must
equal** `mutation`.

### Molecule HTTP (non-signing)

| Tool | Replaces | Returns |
|------|----------|---------|
| `poi_register` | aura Phase 1 POI curl/http_request | `{ poiTo, poiData, merkleRoot, response }` |
| `labs_graphql` | aura Steps 2,3,5,6,8 GraphQL; public queries | `{ data, errors }` |
| `s3_upload` | aura Step 4/B image+file PUT; x402 E3 ciphertext PUT | `{ status, ok }` |

### DEK-aware (the plaintext DEK never leaves the server)

| Tool | Replaces | Returns |
|------|----------|---------|
| `labs_generate_dek` | E0 `generateDataEncryptionKey` | `{ encryptedDek, encryptionSystem, dekHandle }` |
| `labs_decrypt_dek` | E6 `decryptDataKey` | `{ iv, dekHandle, message }` |

These wrap the DEK mutations and stash the **plaintext DEK in server memory**, returning an opaque
`dekHandle` instead. The agent passes the handle to `encrypt_file` / `decrypt_file`, so the
one-shot secret DEK never enters the conversation, a file, or a log. `labs_decrypt_dek` takes
`ipnftUid`+`filePath` (data-room file, `{contractAddress}_{tokenId}`) or `tokenUri`+`agreementUrl`
(IPFS agreement) — matching `encryption.graphql`. Both use `auth='service-token'` (a JWT, not a wallet
signature), so the plaintext DEK stays in-process and no x402 payment is needed.

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

### Confidentiality latch (fail-closed privacy guard)

A confidential file must never fall back to a public upload if the encrypted path fails — that would
publish the resource in plaintext. The skill instructs the agent accordingly, but instructions are
not a guarantee, so the server enforces it at the tool boundary, **non-overridably** (no force flag):

- `encrypt_file` records the **plaintext SHA-256** (and resolved path) of the file it encrypts.
  `s3_upload` then **refuses** to PUT any bytes whose SHA-256 (or path) matches — the plaintext of a
  file the agent encrypted can never reach S3, regardless of `accessLevel` or which upload path the
  agent takes. Uploading the `.enc` ciphertext, the cover image, or a genuinely-public file is
  unaffected (different bytes / never encrypted).
- `build_access_conditions` records the IP-NFT **tokenId**. `x402_prepare` / `x402_submit` (and the direct
  `labs_graphql` path) then **refuse** `finishCreateOrUpdateFileV2` for that tokenId when `accessLevel` is
  `PUBLIC` or `encryptionMetadata` is missing — a molecule whose access conditions were built can only be
  finalized non-PUBLIC + encrypted.

The latch is process-local (cleared on subprocess restart, like the DEK store) and keyed on exact
plaintext bytes + tokenId, so it has no false positives for legitimate public uploads or for a
different molecule handled in the same session.

### Service token (off-chain JWT — the MCP prepares + exchanges; the caller signs)

| Tool | Crafts / does | Returns |
|------|---------------|---------|
| `service_signin_message` | fetch `getServiceSignInMessage` for a wallet (its address → the token's `adminAddress`) | `{ message, walletAddress, serviceName }` |
| `service_token_create` | exchange the caller's `personal_sign` of that message for the JWT via `generateServiceToken` | `{ token, tokenId, expiresAt }` |

Between the two, **the caller `personal_sign`s the `message` with their own wallet** (eg Privy agent wallet, or their own key) — the MCP server itself never signs. Bind the token to whatever wallet is the IP-NFT's authorized signer.

---

## Source-of-truth parity

| Behavior | Replicated from |
|----------|-----------------|
| x402 challenge decode / payment header / `PAYMENT-SIGNATURE` (caller signs) | `desci-infra/lambda/x402-gateway-lambda/index.ts` |
| x402 mutation whitelist | `desci-infra/lambda/x402-gateway-lambda/mutations.ts` |
| AES-256-GCM envelope (12-byte IV, appended tag, plaintext hash) | `desci-ecosystem/packages/storage/src/lib/encryption/kms-envelope.ts` |
| `accessControlConditions` (`isAuthorizedSignerForIpnft`) | `desci-infra/lambda/common/utils/access-control-conditions.ts` + `desci-infra/bruno/desci-labs/v2/25-finishEncryptedFileUploadV2.bru` |
| EIP-712 `TransferWithAuthorization` typed-data (built by `x402_prepare`, **signed by the caller's wallet**) | `skills/aura-orchestrator/references/wallet-signing.md` |
| GraphQL field shapes / `EncryptionMetadataInput` / `decryptDataKey` args | `desci-infra/graphql/schemas/{ip-hubs,encryption}.graphql` |
| Request shapes & auth headers | `desci-infra/bruno/desci-labs/v2` + `desci-infra/bruno/service-auth` |
