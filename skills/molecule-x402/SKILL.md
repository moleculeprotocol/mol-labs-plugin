---
name: molecule-x402
description: End-to-end (client-side) encrypted file uploads to a Molecule Labs data room, paid per call over the x402 gateway. Generate a KMS-wrapped DEK, AES-256-GCM encrypt the file, upload the ciphertext, finish with on-chain access conditions, and verify decryption — all driven through the `molecule` MCP server (no raw curl/node), with a Privy agentic wallet. V2 GraphQL surface (production), keyed on ipnftUid.
---

# Molecule x402 — Client-Side Encrypted Data-Room Uploads

This skill does **one thing**: perform the **end-to-end encryption (E2EE) feature** for a Molecule Labs
data room. Every step that used to be a hand-written `curl` / `node -e` / `openssl` command is now a
single typed tool on the **`molecule` MCP server** (`skills/molecule-mcp/`). It is a faithful
replication of the Labs **Onchain-Verified Envelope Encryption** client
(`desci-ecosystem/packages/storage/src/lib/encryption/kms-envelope.ts`): the backend never sees
plaintext or the unwrapped key — only the ciphertext, the KMS-wrapped DEK, and the on-chain access
conditions are persisted.

The two **billable data-room writes** — `initiateCreateOrUpdateFileV2` and `finishCreateOrUpdateFileV2` —
are paid per call in USDC on Base through the **x402 gateway**. The `mcp__molecule__x402_pay` tool runs
the **entire** payment handshake (challenge → EIP-712 sign via the Privy wallet → retry with payment) in
one call — no API key, no manual base64.

The two **crypto-helper mutations** — `generateDataEncryptionKey` and `decryptDataKey` — are now also
x402-whitelisted, but this skill calls them **directly** against the Labs GraphQL endpoint with a
**service token** (`x-service-token: $MOLECULE_SERVICE_TOKEN` + `x-wallet-address: $EVM_WALLET_ADDRESS`)
— exactly the auth the `desci-infra/bruno/desci-labs/v2` integration tests use. Calling them direct needs
no payment and lets the `mcp__molecule__labs_generate_dek` / `mcp__molecule__labs_decrypt_dek` tools
**keep the plaintext DEK inside the MCP process**, handing back only an opaque `dekHandle`. So this skill
is **hybrid**: x402 for the uploads (E2/E5), direct service-token GraphQL for DEK generation (E0) and
decryption (E6).

This targets the **V2 GraphQL surface** (the one live on production), keyed on the
**`ipnftUid`** (`{contractAddress}_{tokenId}`). The retired OCL surface (`oclId`,
`initiateCreateOrUpdateFile`/`finishCreateOrUpdateFile`) is **not** used here — it is not on production.

---

## Environment variables (set these first)

The MCP server reads all of these from the environment; Claude Code injects them into the MCP
subprocess from your settings. **Non-secrets** → `.claude/settings.json`; **secrets** →
`.claude/settings.local.json` (gitignored). The skill never passes secrets as tool arguments — only the
MCP reads them.

| Variable | Where | Description |
|----------|-------|-------------|
| `X402_GATEWAY_URL` | settings.json | Base URL of the x402 gateway. `mcp__molecule__x402_pay` reaches `$X402_GATEWAY_URL/x402/labs/<mutation>`. |
| `MOLECULE_LABS_URL` | settings.json | Labs **GraphQL endpoint** (e.g. `https://staging.graphql.api.molecule.xyz/graphql`). The direct DEK calls (E0/E6) POST here. |
| `MOLECULE_SERVICE_TOKEN` | settings.local.json | JWT service token for the direct GraphQL calls (`x-service-token`). Mint via `mcp__molecule__mint_service_token` if you don't have one. Secret. |
| `ACCESS_RESOLVER_ADDRESS` | settings.json | IPNFT `AccessResolver` (L1) contract used in `accessControlConditions` for `isAuthorizedSignerForIpnft`. Staging (Sepolia): `0xd9b492fd34b1579C052b2EA25970178B3011Ce6B`. |
| `CHAIN_ID` | settings.json | Chain id of the IPNFT `AccessResolver` (L1) — selects the access-condition chain string (`1`→`ethereum`, `11155111`→`sepolia`, `8453`→`base`, `84532`→`baseSepolia`). Staging: `11155111`. |
| `ENVIRONMENT` | settings.json | `production` \| `staging` \| `local`. Informational / gates the production encryption precondition. |
| `IPNFT_UID` | settings.json | _(optional)_ Default target data room `ipnftUid` (`{contractAddress}_{tokenId}`). May be passed per run instead. |
| `EVM_WALLET_ADDRESS` | settings.json | **Required.** Caller wallet — sent as `x-wallet-address` on the direct calls and used for `changeBy`/`encryptedBy`. For E6 decrypt this is the `:userAddress` evaluated against `isAuthorizedSignerForIpnft`. |
| `PRIVY_APP_ID` | settings.local.json | Privy app id (basic-auth user for the wallet RPC). |
| `PRIVY_APP_SECRET` | settings.local.json | Privy app secret (basic-auth password). |
| `PRIVY_WALLET_ID` | settings.local.json | Privy server-wallet id that signs the x402 payment authorization. |

Ready-to-paste skeleton:

```jsonc
// .claude/settings.json   (non-secret, safe to commit)
{ "env": {
  "X402_GATEWAY_URL": "",
  "MOLECULE_LABS_URL": "",
  "ACCESS_RESOLVER_ADDRESS": "",
  "CHAIN_ID": "",
  "ENVIRONMENT": "staging",
  "IPNFT_UID": "",
  "EVM_WALLET_ADDRESS": ""
}}
```
```jsonc
// .claude/settings.local.json   (secrets, gitignored)
{ "env": {
  "MOLECULE_SERVICE_TOKEN": "",
  "PRIVY_APP_ID": "",
  "PRIVY_APP_SECRET": "",
  "PRIVY_WALLET_ID": ""
}}
```

If a required variable is empty at run time, the relevant MCP tool **stops and reports which one** —
it never guesses endpoints or addresses.

---

## How this runs in Claude Code (toolset)

Everything goes through the **`molecule` MCP server**. The only non-MCP tool is **Read**, for inspecting
the PDF you intend to describe.

| Need | Tool |
|------|------|
| Generate the DEK (direct GraphQL, service token) | `mcp__molecule__labs_generate_dek` |
| AES-256-GCM encrypt / decrypt | `mcp__molecule__encrypt_file` / `mcp__molecule__decrypt_file` |
| Build `accessControlConditions` (ipnft-signer) | `mcp__molecule__build_access_conditions` |
| x402 paid upload calls (E2, E5) | `mcp__molecule__x402_pay` |
| PUT the ciphertext to S3 (no payment) | `mcp__molecule__s3_upload` |
| Resolve the wallet address | `mcp__molecule__privy_get_wallet_address` |
| Mint a service token (bootstrap) | `mcp__molecule__mint_service_token` |
| Round-trip integrity (SHA-256) | built into `mcp__molecule__decrypt_file` (`plaintextSha256`) |
| Inspect a PDF's text (to write a description/tags) | **Read** tool (native PDF extraction — never python/pip/pdftotext) |

Track step outputs (the `dekHandle`, `encryptedDek`, `iv`, `uploadToken`, `datasetId`) in your working
context across the run. The **plaintext DEK is never exposed** — it lives only inside the MCP, addressed
by `dekHandle`.

---

## SUPER IMPORTANT RULES

- **Crypto fidelity is the MCP's job.** `mcp__molecule__encrypt_file`/`decrypt_file` already match the
  Labs client exactly (AES-256-GCM, random 12-byte IV, 16-byte tag **appended** to the ciphertext,
  `contentHash` = hex SHA-256 of the **plaintext**, DEK = base64 raw 32-byte key). Do not re-implement
  crypto in the shell.
- **The plaintext DEK never leaves the MCP.** `labs_generate_dek` / `labs_decrypt_dek` return only an
  opaque `dekHandle`; pass it to `encrypt_file` / `decrypt_file`. Never ask for, print, cache, or write
  the plaintext DEK — there is no path to it from the skill, and that is intentional.
- **Confidential files are never PUBLIC.** `accessLevel` MUST be `HOLDERS` or `ADMIN` (valid values:
  `PUBLIC | HOLDERS | ADMIN`). Uploading a confidential file as plaintext or `PUBLIC` defeats the feature.
- **Two transport modes — do not mix them up.**
  - **x402 (E2, E5):** `mcp__molecule__x402_pay` with `mutation = initiateCreateOrUpdateFileV2` /
    `finishCreateOrUpdateFileV2`. The single top-level GraphQL field in `query` must **equal** `mutation`
    (`validateMutationQuery`). These are x402-whitelisted in
    `desci-infra/lambda/x402-gateway-lambda/mutations.ts`.
  - **Direct (E0, E6):** `mcp__molecule__labs_generate_dek` / `labs_decrypt_dek` (transport `direct`,
    auth `service-token`). `generateDataEncryptionKey` / `decryptDataKey` are also x402-whitelisted, but
    keep them **direct** so the plaintext DEK stays in-process and no payment is spent on a key fetch.
- **All data-room args take `ipnftUid`** (`{contractAddress}_{tokenId}`) — never `oclId`.
- **Production guard.** The backend verifies the caller is an authorized signer for the IP-NFT
  (`isAuthorizedSignerForIpnft`) on the configured `AccessResolver` chain before it will finalize an
  encrypted file. If the resolver is unreachable / not deployed on that chain, the finish step (E5)
  fails with a clear error — surface it verbatim and stop.
- **Payment is the Privy wallet's job.** `x402_pay` signs the EIP-712 `TransferWithAuthorization` through
  the Privy server-wallet RPC. Never sign with a raw private key.

---

## Prerequisites

1. **Privy agentic wallet** configured (`PRIVY_APP_ID`/`PRIVY_APP_SECRET`/`PRIVY_WALLET_ID`). If
   `PRIVY_WALLET_ID` is unset, follow the **`privy-agentic-wallets`** skill
   (`skills/privy-agentic-wallets/SKILL.md`) to create a wallet **with a policy** (single-chain + per-tx
   value cap), then set `PRIVY_WALLET_ID`.
2. **An existing V2 Lab project** → its `ipnftUid` (`{contractAddress}_{tokenId}`). This skill uploads to
   an existing data room; it does **not** create projects. The project must already exist (created via
   `createProject` / the `aura-orchestrator` flow) and the IP-NFT must exist on-chain so the access
   resolver can verify the signer.
3. **A file to encrypt** in the workspace.
4. **A service token** (`MOLECULE_SERVICE_TOKEN`) for the direct E0/E6 calls — see "Obtaining a service
   token" below.

### Resolve the wallet address

```
mcp__molecule__privy_get_wallet_address: {}
```
Save the returned `address` as `wallet_address`. It must equal `$EVM_WALLET_ADDRESS` (the
`x-wallet-address` / `:userAddress` caller for the direct calls), and that wallet must be an authorized
signer for the target IP-NFT (the owner, or a Safe / ERC-4337 / ERC-6551 signer of it) — otherwise E6
decryption is denied.

### Obtaining a service token

`MOLECULE_SERVICE_TOKEN` is a JWT issued by `generateServiceToken` via wallet-signature auth (no Privy
session needed). If you already have one, just set `MOLECULE_SERVICE_TOKEN`. To mint a fresh one (it runs
`getServiceSignInMessage` → `personal_sign` via the Privy wallet → `generateServiceToken`):

```
mcp__molecule__mint_service_token:
  serviceName: data-sync-service
  expiresIn: "720h"
```
Set the returned `token` as `MOLECULE_SERVICE_TOKEN` in `.claude/settings.local.json` and restart. If E0
later returns an auth error, the token is missing/expired — re-mint and retry. The token is a secret; the
MCP never logs it, and neither should you.

### Derive the IP-NFT `tokenId` from the `ipnftUid`

The access condition (E4) needs the IP-NFT **tokenId**, which is the part of the `ipnftUid` after the
underscore: for `ipnftUid = 0x152B…F61a_280`, the `tokenId` is `280`. No packing/derivation tool is
needed on the V2 surface.

---

## x402 payment, in one tool call

The old P1–P7 dance (send → decode `payment-required` → nonce → EIP-712 sign via Privy → base64 payment
header → retry with `PAYMENT-SIGNATURE`) is now fully inside `mcp__molecule__x402_pay`. You give it the
mutation name, the GraphQL `query`, and `variables`; it returns `{ data, errors, settlement }`. No API
key — USDC on Base pays per call. If the mutation isn't whitelisted (no challenge), the tool stops and
reports it.

```
mcp__molecule__x402_pay:
  mutation: <initiateCreateOrUpdateFileV2 | finishCreateOrUpdateFileV2>
  query: "<the GraphQL mutation — single top-level field must equal `mutation`>"
  variables: { ... }
```

Read the GraphQL `data.<mutation>` result (check `isSuccess` / `error`).

---

## The E2EE upload flow (E0 → E5), then verify (E6)

Run E0–E5 in order for one file. This is the whole feature.

### E0 — Generate the data encryption key (direct, **not** x402)

```
mcp__molecule__labs_generate_dek:
  transport: direct
  auth: service-token
```
Returns `encryptedDek` (base64, wrapped), `encryptionSystem` (`"kms"` — echo verbatim, never hardcode),
and `dekHandle`. **The plaintext DEK is not returned** — it stays in the MCP, addressed by `dekHandle`.
If you get an auth error, the service token is missing/expired (re-mint and retry) or the GraphQL surface
isn't reachable — **stop and report**.

### E1 — Encrypt the file (replicates `encryptFileWithKms`)

```
mcp__molecule__encrypt_file:
  filePath: <path-to-file>
  dekHandle: <from E0>
  outPath: <out>.enc
```
Returns `iv` (base64), `contentHash` (hex SHA-256 of the **plaintext**), and `cipherBytes`. The output
`<out>.enc` (`ciphertext‖tag`, tag last) is exactly what the Labs reader (`decryptFileWithKms`) expects —
it is the upload payload.

### E2 — Initiate the upload with the **ciphertext** size (x402 paid)

`contentLength` MUST be the ciphertext size (`cipherBytes` from E1).
```
mcp__molecule__x402_pay:
  mutation: initiateCreateOrUpdateFileV2
  query: "mutation InitiateCreateOrUpdateFileV2($ipnftUid: String!, $contentType: String!, $contentLength: Int!) { initiateCreateOrUpdateFileV2(ipnftUid: $ipnftUid, contentType: $contentType, contentLength: $contentLength) { uploadToken uploadUrl uploadUrlExpiry method headers { key value } useMultipart isSuccess error { message code retryable } } }"
  variables: { "ipnftUid": "<ipnftUid>", "contentType": "application/pdf", "contentLength": <cipherBytes> }
```
From `data.initiateCreateOrUpdateFileV2` extract `uploadToken`, `uploadUrl`, `method`, and `headers`.

### E3 — PUT the ciphertext to S3 (direct, **no x402**)

Convert the returned `headers` array (`[{key,value}, …]`) into a `{ key: value }` map and pass it as
`headers`. Upload the **encrypted** file:
```
mcp__molecule__s3_upload:
  uploadUrl: <uploadUrl from E2>
  filePath: <out>.enc
  method: <method from E2, usually PUT>
  contentType: application/pdf
  headers: { <each key:value from E2 headers> }
```

### E4 — Build `accessControlConditions` (ipnft-signer gate)

Replicates the bruno v2 encrypted-upload condition — gates decryption on
`AccessResolver.isAuthorizedSignerForIpnft(:userAddress, <tokenId>)`, so the IP-NFT owner and any
recursive (Safe / Ownable / ERC-4337 / ERC-6551 TBA) signer can decrypt. `reservationId` is the IP-NFT
**tokenId** (the part of `ipnftUid` after the underscore). The chain string is derived from `CHAIN_ID`.
```
mcp__molecule__build_access_conditions:
  mode: ipnft-signer
  reservationId: "<tokenId from ipnftUid>"
```
`:userAddress` is a literal placeholder the backend evaluator substitutes with the authenticated caller —
the tool keeps it verbatim. Use the returned **`json`** string as `encryptionMetadata.accessControlConditions`
in E5.

### E5 — Finalize the encrypted upload (x402 paid)

`accessLevel` MUST be `HOLDERS` or `ADMIN`. `encryptionMetadata` is an `EncryptionMetadataInput`;
`accessControlConditions` is the E4 **`json`** string; `encryptedAt` is an ISO-8601 UTC timestamp.
`categories`/`tags` are optional — default `Science` / `Discovery`.

| Field | Value |
|-------|-------|
| `encryptionSystem` | from E0 (e.g. `kms`) — never hardcode |
| `accessControlConditions` | E4 `json` string |
| `encryptedBy` | `<wallet_address>` |
| `encryptedAt` | ISO-8601 UTC (e.g. `2026-06-03T12:00:00Z`) |
| `encryptedDek` | from E0 (base64, wrapped) |
| `iv` | from E1 (base64) |
| `contentHash` | from E1 (hex SHA-256 of plaintext) |

```
mcp__molecule__x402_pay:
  mutation: finishCreateOrUpdateFileV2
  query: "mutation FinishCreateOrUpdateFileV2($ipnftUid: String!, $uploadToken: String!, $path: String, $accessLevel: String!, $changeBy: String!, $description: String, $tags: [String!], $categories: [String!], $encryptionMetadata: EncryptionMetadataInput) { finishCreateOrUpdateFileV2(ipnftUid: $ipnftUid, uploadToken: $uploadToken, path: $path, accessLevel: $accessLevel, changeBy: $changeBy, description: $description, tags: $tags, categories: $categories, encryptionMetadata: $encryptionMetadata) { datasetId contentHash version newHead isSuccess message error { message code retryable } } }"
  variables: { "ipnftUid": "<ipnftUid>", "uploadToken": "<from E2>", "path": "<filename>", "accessLevel": "HOLDERS", "changeBy": "<wallet_address>", "description": "<desc>", "categories": ["Science"], "tags": ["Discovery"], "encryptionMetadata": { "encryptionSystem": "<from E0>", "accessControlConditions": "<E4 json string>", "encryptedBy": "<wallet_address>", "encryptedAt": "<ISO-8601 UTC>", "encryptedDek": "<from E0>", "iv": "<from E1>", "contentHash": "<from E1>" } }
```
From `data.finishCreateOrUpdateFileV2` extract `datasetId` (`did:odf:…`) and `contentHash`. Note the
stored data-room `path` (e.g. `/v2-kms-encrypted-….pdf`) — E6 needs it.

### E6 — Verify decryption (optional, direct **not** x402, replicates `decryptFileWithKms`)

Confirms an authorized caller can recover the file. Fetch the DEK with a **direct** authenticated call
(the plaintext stays in the MCP), then decrypt locally.

```
mcp__molecule__labs_decrypt_dek:
  ipnftUid: "<ipnftUid>"
  filePath: "<data-room path from E5>"
  transport: direct
  auth: service-token
```
- On success: returns `iv` and a fresh `dekHandle`.
- `isSuccess: false` with `ACCESS_DENIED`: the caller does not satisfy the on-chain
  `isAuthorizedSignerForIpnft` condition. The caller is the `x-wallet-address` (`$EVM_WALLET_ADDRESS`)
  substituted for `:userAddress` — so that wallet must be the IP-NFT owner or an authorized signer.
  Verify you passed the same wallet you encrypted with.
- `LEGACY_ENCRYPTION`: the file predates the envelope flow and must be decrypted with the legacy client.

Then decrypt and check round-trip integrity:
```
mcp__molecule__decrypt_file:
  filePath: <out>.enc
  iv: <iv from labs_decrypt_dek>
  dekHandle: <from labs_decrypt_dek>
  outPath: <out>.dec
```
The returned `plaintextSha256` **must equal** the `contentHash` from E1 — that confirms the round trip.

---

## Mutation reference (V2 surface)

| Step | Mutation | Tool | Transport | Args (key) | Result fields used |
|------|----------|------|-----------|------------|--------------------|
| E0 | `generateDataEncryptionKey` | `labs_generate_dek` | **direct** (`x-service-token`) | _(none)_ | `encryptedDek`, `encryptionSystem`, `dekHandle` |
| E2 | `initiateCreateOrUpdateFileV2` | `x402_pay` | **x402** | `ipnftUid, contentType, contentLength` | `uploadToken, uploadUrl, method, headers` |
| E5 | `finishCreateOrUpdateFileV2` | `x402_pay` | **x402** | `ipnftUid, uploadToken, path, accessLevel, changeBy, …, encryptionMetadata` | `datasetId, contentHash` |
| E6 | `decryptDataKey` | `labs_decrypt_dek` | **direct** (`x-service-token`) | `ipnftUid, filePath` | `iv`, `dekHandle` |

The x402 rows must appear in the gateway whitelist; the direct rows are reached over
`$MOLECULE_LABS_URL` with `x-service-token` + `x-wallet-address` (handled by the MCP). All four mutations
are in fact x402-whitelisted — E0/E6 simply stay direct so the plaintext DEK never leaves the MCP.

**Source of truth:**
- working request shapes & auth — `desci-infra/bruno/desci-labs/v2` (tests 23–27: the encrypted-upload flow) + `desci-infra/bruno/service-auth` (service-token mint) + `desci-infra/bruno/environments/staging.bru`
- x402 whitelist — `desci-infra/lambda/x402-gateway-lambda/mutations.ts`
- field signatures — `desci-infra/graphql/schemas/ip-hubs.graphql` (`initiateCreateOrUpdateFileV2`/`finishCreateOrUpdateFileV2`/`EncryptionMetadataInput`) + `encryption.graphql` (`generateDataEncryptionKey`/`decryptDataKey`)
- encryption-metadata validation (KMS required fields) — `desci-infra/lambda/appsync-resolver-labs-lambda/utils/encryption-validator.ts`
- crypto — `desci-ecosystem/packages/storage/src/lib/encryption/kms-envelope.ts` (12-byte IV, SHA-256 of plaintext — the Bruno tests use placeholder crypto; the MCP uses the real client algorithm)
- access conditions (`isAuthorizedSignerForIpnft`) — `desci-infra/bruno/desci-labs/v2/25-finishEncryptedFileUploadV2.bru` + `desci-infra/lambda/appsync-resolver-labs-lambda/services/access-resolver-client.ts`
- gateway routing/headers — `desci-infra/lambda/x402-gateway-lambda/index.ts` (`payment-signature` header; path-field equality)
- MCP tool reference — `skills/molecule-mcp/README.md`
