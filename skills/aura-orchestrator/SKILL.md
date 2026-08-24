---
name: aura-orchestrator
description: End-to-end DeSci molecule on the OCL (On-Chain Labs) surface — resolve-or-create an on-chain lab (LabNFT + token-bound account), register it, upload files (public or private/encrypted), and announce. Single-agent sequential execution, driven entirely through the `molecule` MCP server (no raw curl).
metadata:
  env_vars:
    - ENVIRONMENT
    - MOLECULE_CLIENT_URL
    - MOLECULE_LABS_URL
    - ONCHAIN_LAB_FACTORY_ADDRESS
    - LABNFT_ADDRESS
    - ACCESS_RESOLVER_ADDRESS
    - X402_GATEWAY_URL
    - EVM_WALLET_ADDRESS
    - EVM_RPC_URL
    - CHAIN_ID
    - WALLET_BACKEND
    - PRIVY_APP_ID
    - PRIVY_APP_SECRET
    - PRIVY_WALLET_ID
    - WALLET_PRIVATE_KEY
    - MOLECULE_CONSUMER_CREDENTIAL
    - MOLECULE_API_KEY
    - MOLECULE_SERVICE_TOKEN
---

# Aura Orchestrator (OCL)

Complete DeSci molecule executed as one continuous sequence of tool calls.
Do NOT stop, report progress, or output text between steps — execute ALL steps as one uninterrupted flow.

This skill targets the **OCL / V3 surface**: a lab is an **On-Chain Lab** — a **LabNFT** plus its
**token-bound account (TBA)** on Base / Base Sepolia, identified by a 32-byte **`oclId`**. There is no
IP-NFT and no Proof-of-Invention here; per-file provenance comes from the Kamu data room (`contentHash` +
immutable versioning).

Every network, on-chain, and crypto operation runs through the **`molecule` MCP server** (`mcp/`). The only
non-MCP tools used are `read_file` (PDF text extraction), `shared_cache` (cross-step state), and `Bash`
(waits/timestamps only — never curl).

**SUPER IMPORTANT RULES:**
- Use `read_file` for PDFs — it has built-in PDF text extraction. NEVER use python, pip, pdftotext, or any shell tools for PDF reading.
- Do NOT `read_file` on image/binary attachments (PNG, JPG, etc.). The upload flow only needs the `file_path` — pass the path directly to `mcp__molecule__s3_upload`.
- Use `shared_cache` to persist all critical values (`oclId`, `labAccountAddress`, `labNftTokenId`, tx hashes, tokens, the `dekHandle`). If you need a value from an earlier step, retrieve it from cache.
- Follow every URL, contract address, and function signature in this document EXACTLY. Do NOT guess or fabricate alternatives. URLs and contract addresses come from `.env`, read by the MCP — never hardcode.
- **WALLET BACKEND — the user is in control; you NEVER assume one.** There are two operating-wallet backends and **all** their env vars are optional until the user picks: (1) a **Privy agentic wallet** (signs server-side via Privy — `PRIVY_APP_ID` + `PRIVY_APP_SECRET` + `PRIVY_WALLET_ID`), or (2) a **raw EOA** (signs locally in the MCP with `WALLET_PRIVATE_KEY` — Privy never exposes a key, so the two are NOT interchangeable). **Ask the user which wallet to use** (Phase 0), then pin it for the whole run via `WALLET_BACKEND=privy|eoa` (or the per-call `backend` arg). If exactly one backend is configured the MCP auto-selects it; if BOTH are configured you MUST pass the user's choice — the MCP refuses to guess. Every signing/sending tool follows this choice: **Privy →** `privy_send_transaction` / `privy_send_raw_transaction` / `issue_service_token`; **EOA →** `eoa_send_transaction` / `issue_owner_service_token`. `x402_pay` and `wallet_address` work for either.
- Use the x402 payment flow for ALL Molecule mutations (createLab, file uploads, announcements) via `mcp__molecule__x402_pay` — one call runs the whole P1–P7 handshake, signing with the **selected wallet backend**. Send the **top-level AppSync** mutations (not the nested `molecule.v3.project(oclId)` documents).
- The lab is keyed on **`oclId`** (a 32-byte `0x` + 64-hex string), threaded through every backend call. Its **`labAccountAddress`** (the TBA) is threaded into the access-conditions step.
- For **private / confidential** files, use the Private / Encrypted Upload variant in Phase 3 (Steps E0–E6) **instead of** the public Steps A–C: generate a one-shot DEK (kept inside the MCP), AES-256-GCM encrypt locally, upload the ciphertext, and finish with `encryptionMetadata` + a non-PUBLIC `accessLevel`. NEVER upload a confidential file as plaintext or with `accessLevel: PUBLIC`.
- **FAIL CLOSED — no public fallback for confidential files.** Once a file is chosen for the Private / Encrypted variant, if **any** step (E0 DEK, E1 encrypt, E2/E3 ciphertext upload, E4 access conditions, E5 finalize, E6 verify) fails and you cannot fix it in-path, **ABORT the entire molecule and report the error.** Do **NOT** "recover" by running the public Steps A–C, re-upload with `accessLevel: PUBLIC`, or `s3_upload` the plaintext PDF — ever. This is also enforced in code: `encrypt_file` arms a non-overridable MCP guard that refuses to S3-upload that file's plaintext, and `build_access_conditions` arms a guard that refuses to finalize a file for that `oclId` as `PUBLIC` / without `encryptionMetadata`. Do not attempt to work around these guards.
- The plaintext DEK is single-use and secret. It **never leaves the MCP** — `labs_generate_dek` returns only an opaque `dekHandle`. NEVER attempt to obtain, cache, or log the plaintext DEK.
- AES-256-GCM encrypt/decrypt is handled by `mcp__molecule__encrypt_file`/`decrypt_file` (it replicates the Labs `encryptFileWithKms`). PDF reading still uses `read_file`.
- Phases executed sequentially without stopping or reporting intermediate progress.

## Environment Variables — the lab/endpoint vars are required; the **wallet vars are all optional until the user picks a backend** (see the WALLET BACKEND rule above). A required var, if missing, makes the relevant MCP tool terminate with an error naming it. The MCP supports **two operating-wallet backends — the user chooses one:**

- **Privy agentic wallet** (`PRIVY_APP_ID` + `PRIVY_APP_SECRET` + `PRIVY_WALLET_ID`): Privy signs server-side; no raw key exists. Service token via `mcp__molecule__issue_service_token`.
- **Raw EOA** (`WALLET_PRIVATE_KEY`): the MCP signs locally (the key never leaves the process). Service token via `mcp__molecule__issue_owner_service_token`.

Set `WALLET_BACKEND=privy|eoa` to pin the choice (auto-selected when only one is configured; **required** when both are). Run `mcp__molecule__config_doctor` to see the active backend and what each one still needs.

| Variable | Description |
|----------|-------------|
| `ENVIRONMENT` | Deployment profile: `staging` (Base Sepolia) or `production` (Base mainnet). |
| `MOLECULE_LABS_URL` | Labs GraphQL endpoint (OCL/V3 surface). |
| `MOLECULE_CLIENT_URL` | Client app base URL — used only to build the project URL (`/projects/{shortname}`) for announcements. |
| `ONCHAIN_LAB_FACTORY_ADDRESS` | `OnChainLabFactory` — `mintAndCreateAccount` / `createAccount` / `oclIdOfToken` / `accountOfToken` (Base / Base Sepolia). |
| `LABNFT_ADDRESS` | **Optional override.** `LabNFT` ERC-721 — `mintFeeWei()`, `ownerOf`, `safeTransferFrom`. Auto-discovered from the factory (`getDerivationConfigDetails().labNft`, Step 1·0); set only to pin/override it. |
| `ACCESS_RESOLVER_ADDRESS` | **Required** — AccessResolver V3 (`hasRole` / `isAuthorizedSignerForTba` / `grantRole`). Not derivable on-chain (the resolver points to LabNFT, not vice-versa), so set it explicitly. |
| `X402_GATEWAY_URL` | x402 paid-mutation gateway. |
| `CHAIN_ID` | OCL canonical chain (Base Sepolia `84532` / Base `8453`). |
| `EVM_RPC_URL` | **Optional, non-secret** (→ `settings.json`). Base / Base Sepolia RPC for `ocl_read` + raw broadcast (the EOA backend signs+broadcasts through it). Falls back to a public node for known chains (`https://sepolia.base.org` / `https://mainnet.base.org`). Sensitive only if the URL embeds an API key. |
| `WALLET_BACKEND` | **Optional wallet selector — `privy` or `eoa`.** Pins which backend signs for the whole run. Auto-selected when exactly one backend's env is set; **required when both are** (the MCP refuses to guess). |
| `EVM_WALLET_ADDRESS` | EOA address for **watch-only reads** and the **Phase-5 owner / hand-off target** (optional — skip Phase 5 if not set or equal to the operating wallet). Under the **privy** backend this is NOT the operating signer. |
| `PRIVY_APP_ID` / `PRIVY_APP_SECRET` / `PRIVY_WALLET_ID` | **Privy backend (optional).** Privy agentic wallet — `mcp__molecule__privy_*` tools + x402 + `issue_service_token`. |
| `WALLET_PRIVATE_KEY` | **EOA backend (optional, secret).** Raw EOA private key — the MCP's **local** signer for `eoa_send_transaction`, x402 payments, and `issue_owner_service_token`. The key never leaves the MCP process. Needed only when `WALLET_BACKEND=eoa` (or the EOA is the only configured wallet). |
| `MOLECULE_CONSUMER_CREDENTIAL` | **Preferred Labs consumer auth (secret).** `mol_<consumerId>_<secret>` credential, sent verbatim as `Authorization` (no `Bearer`) on every Labs call — direct `labs_graphql` reads (e.g. the `labs(walletAddress)` resolve query), sign-in queries, DEK calls. |
| `MOLECULE_API_KEY` | **Legacy fallback (secret).** The shared `x-api-key` for the same calls, until the `mol_` credential migration completes. If both are set, the MCP sends both headers. |
| `MOLECULE_SERVICE_TOKEN` | **Private uploads only.** Off-chain JWT for the direct (non-x402) DEK generate/decrypt calls, bound to one wallet's `adminAddress`. If missing/expired, issue one bound to the operating wallet — Privy via `issue_service_token`, EOA via `issue_owner_service_token` (see **Service Token**). Secret — keep in `settings.local.json`. |

**Note:** The MCP server reads all URLs, contract addresses, API keys, and secrets from the environment
(`.claude/settings.json` for non-secrets, `.claude/settings.local.json` for secrets). The skill passes only
file paths, addresses, queries, and non-secret values as tool arguments — never secrets. Switching between
staging and production is a `.env` edit only — never modify the skill body for environment changes.

Only these two environment/chain profiles are supported. The contract values are synchronized with
`desci-infra/lambda/common/utils/chain.ts`. `X402_GATEWAY_URL` is the deployed API Gateway **base URL**
from the matching stack's `X402GatewayEndpoint_*` output, with `/x402/labs/{mutation}` removed (the MCP
appends that path). The staging client URL comes from the current Labs staging deployment; production is
`https://labs.molecule.xyz`.

| `ENVIRONMENT` | Labs GraphQL | Chain | Factory | LabNFT | AccessResolver |
|---|---|---:|---|---|---|
| `staging` | `https://staging.graphql.api.molecule.xyz/graphql` | Base Sepolia (`84532`) | `0xd629FE2310b4309a212495F10A47f8436dcEfD90` | `0x13Ff210695fdb54A7F928ECcc28BC3486c05BB28` | `0x5493F472602C87318EA5Eff753cDD593bf9bF559` |
| `production` | `https://production.graphql.api.molecule.xyz/graphql` | Base (`8453`) | `0xECdF4f05384056507485C90aeAb0a83268760D6E` | `0x9F96027eeAFb9ad5F2b5d7043B36Ee96B2EeBE92` | `0x89a14Be8f7824d4775053Edad0f2fA2d6767b72B` |

Run `mcp__molecule__config_doctor` before Phase 0 and stop on any `configurationIssues`; this catches
staging/production endpoint, chain, or contract-address mismatches before any spend.

## Input

- A research PDF file in the workspace (e.g. `.tengu-attachments/document.pdf`)
- An optional cover image (PNG/JPG) in `.tengu-attachments/`
- **Lab name and description** — draft these from the research document (surface them so the user can override). The backend derives the human-readable `shortname` from the lab name; there is no user-supplied symbol.
- **Upload visibility** — REQUIRED and **user-supplied**; it is the one knob that changes Phase 3, so you **MUST ask the user up front** rather than silently assuming. Pick ONE:
  1. **Public file upload** — stored as plaintext with `accessLevel: PUBLIC`. Run Phase 3 Steps A–C.
  2. **Private file upload** (confidential / encrypted) — AES-256-GCM envelope-encrypted client-side, stored as ciphertext with a non-PUBLIC `accessLevel` and on-chain access conditions. Run Phase 3 Private variant Steps E0–E6 **instead of** A–C. This path additionally needs `MOLECULE_SERVICE_TOKEN`.

## Collect run inputs (do this BEFORE the uninterrupted flow)

Before Phase 0, ask the user — in a single prompt (e.g. `AskUserQuestion`) — for:

1. **Wallet backend — Privy agentic wallet or raw EOA** — the operating wallet that signs every on-chain tx and x402 payment. **You MUST ask; never assume.** If `config_doctor` shows exactly one backend configured, present that as the pre-selected default but still confirm; if both are configured, the user MUST pick (the MCP will not guess). Record the choice and use it for the whole run (pass it as `backend` where a tool accepts it, and treat `WALLET_BACKEND` as the source of truth). `privy` → Phase 0 Privy variant (Steps 0a–0d); `eoa` → Phase 0 EOA variant (Step 0e).
2. **Upload visibility — public or private/encrypted** — the Phase 3 path selector. Present **public** as the pre-selected default but require the user to confirm. `public` → Phase 3 Steps A–C; `private/encrypted` → Phase 3 Private variant Steps E0–E6 (also needs `MOLECULE_SERVICE_TOKEN`).
3. **Confirm the lab name and description** you auto-drafted from the document (the user may override). The backend derives `shortname` from `name`; the editable lab metadata fields are `name` / `description` / `image` / `externalUrl` — there is no symbol, research-lead, organization, or funding metadata on an OCL lab.

Once gathered, run Phases 0–5 as one uninterrupted sequence.

## Phase 0: Wallet Setup

Set up the **operating wallet for the backend the user chose** (Collect-run-inputs Q1). Run **the Privy variant (Steps 0a–0d) OR the EOA variant (Step 0e) — not both.** Either way, end Phase 0 with a saved `wallet_address` (the operating signer) and a known backend. A quick `mcp__molecule__config_doctor` first confirms which backends are configured and which is selected.

> Whichever backend is active, get the operating address with the backend-agnostic `mcp__molecule__wallet_address` (it returns `{address, backend}`) and `shared_cache` both `wallet_address` and `wallet_backend`. Pass `backend: <wallet_backend>` to any tool that accepts it so the run never drifts to the wrong wallet.

### Privy variant (Steps 0a–0d) — `WALLET_BACKEND=privy`

Verify a Privy agentic wallet is available. If available, save its address. If not, create one with a restrictive policy and report the new wallet id.

### Step 0a — Check for existing wallet
```
mcp__molecule__privy_get_wallet_address: {}
```
If this succeeds, save the returned `address` as `wallet_address` and proceed to Phase 1. If it fails (missing `PRIVY_WALLET_ID`), check for existing wallets.

### Step 0b — List existing wallets
```
mcp__molecule__privy_list_wallets:
  chainType: ethereum
```
If wallets exist, use the first: save its `id` as `wallet_id` and `address` as `wallet_address`, and report `Set PRIVY_WALLET_ID=<wallet_id>`. If none exist, create one.

### Step 0c — Create a policy
```
mcp__molecule__privy_create_policy:
  name: "DeSci agent policy"
  maxValueWei: "10000000000000000"
```
(Single-chain, pinned to `$CHAIN_ID`, 0.01 ETH per-tx cap.) Save the returned `policyId`.

### Step 0d — Create a wallet
```
mcp__molecule__privy_create_wallet:
  policyIds: ["<policyId>"]
```
Save `walletId` as `wallet_id` and `address` as `wallet_address`. Report that the user must set `PRIVY_WALLET_ID=<wallet_id>` for the Privy MCP tools to function. Save wallet details to `lab/wallet_info.json`.

### EOA variant (Step 0e) — `WALLET_BACKEND=eoa`

The user is bringing their **own EOA** (an external/personal key). There is **no wallet to create or policy to attach** — the MCP signs locally with `WALLET_PRIVATE_KEY` (the key never leaves the MCP process). Just confirm the wallet resolves:
```
mcp__molecule__wallet_address:
  backend: eoa
```
Save the returned `address` as `wallet_address`. If this errors with "WALLET_PRIVATE_KEY is not set", the user has chosen the EOA backend without providing the key — stop and ask them to set `WALLET_PRIVATE_KEY` (secret → `settings.local.json`) and, if desired, `WALLET_BACKEND=eoa`, then reload the MCP. **Fund this EOA** before the paid phases: USDC on the x402 settlement chain (x402 pays per call) + native gas on the mint chain. Then proceed to Phase 1.

> **Combining a wallet later.** Because the backend is purely an env choice, a user can run today on whichever wallet they have and switch later with **no skill change** — e.g. start on an EOA, then later create/fund a Privy agentic wallet (Steps 0a–0d) and set `WALLET_BACKEND=privy`, or vice-versa. The same lab, files, and access grants keep working; only the signer changes. To give a *second* wallet access to an existing lab, use Phase 5 (grantRole / transfer).

## Phase 1: Resolve-or-create the On-Chain Lab

Reuse an existing lab the operating wallet already controls, or mint a new one. **Never mint a duplicate** if the wallet already admins a lab you intend to use.

### Step 1·0 — Resolve the LabNFT address from the factory
The `LabNFT` ERC-721 is an implementation detail of the factory, so discover it instead of requiring a second configured address. `$ONCHAIN_LAB_FACTORY_ADDRESS` is the single source of truth.
```
mcp__molecule__ocl_read:
  functionSignature: "getDerivationConfigDetails()"
  to: $ONCHAIN_LAB_FACTORY_ADDRESS
  returns: ["address", "address", "address", "uint256"]
```
`values` is `[registry, router, labNft, canonicalChainId]`. Save `values[2]` as `lab_nft_address` — used below for `mintFeeWei()` (Step 1b), the optional `ownerOf` check (Step 1a), and the Phase 5 hand-off `safeTransferFrom`. **If `$LABNFT_ADDRESS` is set in the environment, it overrides this discovered value;** otherwise always use `lab_nft_address`. Cache it:
```
shared_cache: { "operation": "put", "namespace": "molecule", "key": "lab_nft_address", "value": "<lab_nft_address>" }
```

### Step 1a — Look for an existing lab the wallet owns
`labs(walletAddress)` returns labs for every active membership role, so the owner filter is mandatory for
this owner/admin workflow.
```
mcp__molecule__labs_graphql:
  auth: api-key
  query: "query Labs($walletAddress: String!) { labs(walletAddress: $walletAddress, role: OWNER) { totalCount nodes { oclId shortname labAccountAddress labNftTokenId } } }"
  variables: { "walletAddress": "<wallet_address>" }
```
- If `data.labs.totalCount > 0`: **REUSE**. Pick the intended lab (if more than one, ask the user which `shortname`/`oclId`). Save its `oclId`, `shortname`, `labAccountAddress`, `labNftTokenId`. Skip to **Phase 2** (createLab is idempotent for an already-registered lab — re-running returns the existing lab; if it errors `already exists`, treat as success).
- If `totalCount == 0`: **MINT** a new lab (Step 1b).

### Step 1b — Mint a new LabNFT + token-bound account
Read the mint fee, then mint via the factory (mint + TBA creation in one tx).
```
mcp__molecule__ocl_read:
  functionSignature: "mintFeeWei()"
  to: <lab_nft_address>
  returns: ["uint256"]
```
Save `values[0]` as `mint_fee_wei` (decimal string; `"0"` if no fee).
```
mcp__molecule__abi_encode:
  functionSignature: "mintAndCreateAccount(address)"
  args: ["<wallet_address>"]
```
Save `calldata`. Now send the mint **with the selected wallet backend**:

- **Privy** (`backend=privy`) — `privy_send_transaction` (Privy broadcasts the mint and estimates gas):
```
mcp__molecule__privy_send_transaction:
  to: $ONCHAIN_LAB_FACTORY_ADDRESS
  data: <calldata>
  value: "<mint_fee_wei>"
  chainId: $CHAIN_ID
```
- **EOA** (`backend=eoa`) — `eoa_send_transaction` (the MCP signs locally with `WALLET_PRIVATE_KEY` and self-broadcasts; same args):
```
mcp__molecule__eoa_send_transaction:
  to: $ONCHAIN_LAB_FACTORY_ADDRESS
  data: <calldata>
  value: "<mint_fee_wei>"
  chainId: $CHAIN_ID
```
Save `txHash` as `mint_tx_hash`.

### Step 1c — Read the new lab identity from the mint receipt
```
mcp__molecule__ocl_tx_identity:
  txHash: <mint_tx_hash>
```
On `found: true`, save `tokenId` → `labNftTokenId`, `account` → `labAccountAddress`, `oclId` → `oclId`.
If `found: false`, recover `labNftTokenId` from the receipt logs (the ERC-721 `Transfer` event's third topic) and resolve the rest:
```
mcp__molecule__ocl_read: { functionSignature: "oclIdOfToken(uint256)", to: $ONCHAIN_LAB_FACTORY_ADDRESS, args: ["<labNftTokenId>"], returns: ["bytes32"] }
mcp__molecule__ocl_read: { functionSignature: "accountOfToken(uint256)", to: $ONCHAIN_LAB_FACTORY_ADDRESS, args: ["<labNftTokenId>"], returns: ["address"] }
```

### Step 1d — (Optional) set lab display metadata + cover image
Direct `labs_graphql` (service-token or api-key — NOT x402). Cover image first if one exists:
```
mcp__molecule__labs_graphql:
  auth: service-token
  query: "mutation GenLabImg($oclId: String!, $contentType: String!) { generateLabImageUploadUrl(oclId: $oclId, contentType: $contentType) { uploadUrl key isSuccess error { message code retryable } } }"
  variables: { "oclId": "<oclId>", "contentType": "image/png" }
```
```
mcp__molecule__s3_upload: { uploadUrl: <uploadUrl>, filePath: <path to image>, method: PUT, contentType: image/png }
```
Then set name/description (the processor patches `image` async after the upload lands):
```
mcp__molecule__labs_graphql:
  auth: service-token
  query: "mutation UpdLab($oclId: String!, $input: UpdateLabNftMetadataInput!) { updateLabNftMetadata(oclId: $oclId, input: $input) { isSuccess oclId message error { message code retryable } } }"
  variables: { "oclId": "<oclId>", "input": { "name": "<lab name>", "description": "<description>" } }
```

Cache the identity now and use it for ALL subsequent steps:
```
shared_cache: { "operation": "put", "namespace": "molecule", "key": "ocl_id", "value": "<oclId>" }
shared_cache: { "operation": "put", "namespace": "molecule", "key": "lab_account_address", "value": "<labAccountAddress>" }
shared_cache: { "operation": "put", "namespace": "molecule", "key": "lab_nft_token_id", "value": "<labNftTokenId>" }
shared_cache: { "operation": "put", "namespace": "molecule", "key": "wallet_address", "value": "<wallet_address>" }
```

## x402 Payment Flow (used by the paid mutations in Phases 2–4)

Each paid Molecule mutation is settled per call in USDC on Base. The entire P1–P7 handshake (send → decode
the `payment-required` challenge → sign the EIP-712 `TransferWithAuthorization` with the **selected wallet
backend** (Privy wallet or local EOA) → retry with `PAYMENT-SIGNATURE`) runs **inside one
`mcp__molecule__x402_pay` call**:
```
mcp__molecule__x402_pay:
  mutation: <mutation_name>
  query: "<the GraphQL mutation — its single top-level field MUST equal `mutation`>"
  variables: { ... }
```
It returns `{ data, errors, settlement }`; read `data.<mutation_name>` and check `isSuccess` / `error`. `x402_pay` signs with the **selected wallet backend** — pass `backend: <wallet_backend>` if both backends are configured.

**Required env vars (read by the MCP):** `X402_GATEWAY_URL`, `CHAIN_ID`, **plus the chosen wallet backend** — either Privy (`PRIVY_APP_ID` + `PRIVY_APP_SECRET` + `PRIVY_WALLET_ID`) or EOA (`WALLET_PRIVATE_KEY`). The paying wallet must hold USDC on the settlement chain regardless of backend.

### Whitelisted mutations (OCL surface, keyed on `oclId`)
The x402 gateway whitelist is exactly:
`createLab`, `initiateCreateOrUpdateFile`, `finishCreateOrUpdateFile`, `createAnnouncement`,
`generateDataEncryptionKey`, `decryptDataKey`. Send the **top-level AppSync** mutations above — NOT the
nested `molecule.v3.project(oclId)` Kamu documents (those are internal). If `x402_pay` reports a mutation is
not enabled (HTTP 400 "not enabled for x402 gateway"), the gateway is misconfigured for this environment —
surface the error and stop; do not improvise a different surface. A response that arrives but reports
`isSuccess: false` is a real business error — surface it.

---

## Service Token (off-chain JWT — bind it to the operating wallet, Privy *or* EOA)

`MOLECULE_SERVICE_TOKEN` is an **off-chain JWT** (issued by Labs `generateServiceToken`; never minted
on-chain). It authenticates the **direct, non-x402** DEK calls — `labs_generate_dek` / `labs_decrypt_dek`
with `transport: direct`, `auth: service-token`. **It is used only by the Phase 3 Private / Encrypted variant
and Phase 5 owner-decrypt; public uploads never touch it.**

**What the token is bound to (why the wallet matters).** The JWT payload carries an `adminAddress` — the
single wallet the token represents. `decryptDataKey` access is **two-gate**: (a) that `adminAddress` must
hold ≥Viewer role on the lab (DB `authorizeViewer(adminAddress, oclId)`); and (b) the stored on-chain
`accessControlConditions` are evaluated against it — passing via `hasRole(oclId, addr, CONTRIBUTOR)` **OR**
`isAuthorizedSignerForTba(addr, labAccountAddress)` (the LabNFT/TBA owner). So a token only unlocks a file if
the wallet it is bound to is the lab's TBA owner or holds a role on the lab. A token bound to the wrong
wallet authenticates fine but fails `ACCESS_DENIED` on decrypt.

**How issuance works (identical for both wallet types — no x402, no on-chain tx).** The MCP runs three
off-chain steps: `getServiceSignInMessage(walletAddress, serviceName)` → the wallet `personal_sign`s the
exact message → `generateServiceToken(...)` verifies it and sets `adminAddress = walletAddress`. Because
step 2 is a standard ECDSA signature, a Privy agentic wallet and a raw EOA are interchangeable to the
backend — only the signing key differs:

| Operating wallet | Tool | Signs with | Binds token to | Needs |
|---|---|---|---|---|
| **Privy agentic wallet** | `mcp__molecule__issue_service_token` | Privy `personal_sign` | the Privy wallet (pass `walletId`/`walletAddress` for a non-default one) | `PRIVY_APP_ID`, `PRIVY_APP_SECRET`, `PRIVY_WALLET_ID` |
| **EOA (raw key)** | `mcp__molecule__issue_owner_service_token` | local `eth_account` sign with `WALLET_PRIVATE_KEY` | the EOA | `WALLET_PRIVATE_KEY` (or `ownerPrivateKey` arg) |

Set the returned `token` as `MOLECULE_SERVICE_TOKEN` in `.claude/settings.local.json` (secret), or pass it
per-call via the `serviceToken` override when you need a token bound to a *different* wallet.

**Selection rule:** identify the wallet that must satisfy the lab's decrypt gate (the lab's TBA owner, i.e.
the LabNFT holder, or a wallet you `grantRole`'d on the lab); issue/select a token bound to that wallet
(Privy → `issue_service_token`; EOA → `issue_owner_service_token`); prefer a pre-set token already bound to
the right wallet over issuing per run.

---

## Phase 2: Register the lab (createLab, via x402)

Register the Kamu-backed lab for the on-chain `oclId`. The operating wallet that minted the LabNFT is its
owner, so it passes the createLab admin check.
```
mcp__molecule__x402_pay:
  mutation: createLab
  query: "mutation CreateLab($input: CreateLabInput!) { createLab(input: $input) { isSuccess message error { message code retryable } lab { oclId shortname labAccountAddress labNftTokenId } } }"
  variables: { "input": { "oclId": "<oclId>" } }
```
If `isSuccess` is false because the admin role isn't indexed yet (just-minted lab), poll `labs(walletAddress)`
(Step 1a, including `role: OWNER`) until the new `oclId` appears, then retry — do NOT use a fixed long
sleep. Use the `shortname` saved in Step 1a or returned as `lab.shortname`; if it is null, poll the same
owner-only query until the lab's derived `shortname` is present. Cache it, then build the project URL
`$MOLECULE_CLIENT_URL/projects/{shortname}` and cache that:
```
shared_cache: { "operation": "put", "namespace": "molecule", "key": "shortname", "value": "<shortname>" }
shared_cache: { "operation": "put", "namespace": "molecule", "key": "project_url", "value": "<project_url>" }
```

## Phase 3: Upload File to Data Room

By default a file is uploaded **PUBLIC** via Steps A–C. If the file must be **private / confidential**, use
the **Private / Encrypted Upload** variant (Steps E0–E6) *instead of* A–C. Choose ONE path per file.

**The path choice is irreversible mid-flight.** If you started the Private / Encrypted variant, you may NEVER
switch to Steps A–C for that file. A failure anywhere in E0–E6 means **abort and report** (FAIL CLOSED).

Get the file size (use the `bytes` field):
```
mcp__molecule__sha256_file:
  filePath: <path-to-pdf>
```

### Step A — Initiate upload (x402 paid)
```
mcp__molecule__x402_pay:
  mutation: initiateCreateOrUpdateFile
  query: "mutation InitiateCreateOrUpdateFile($oclId: String!, $contentType: String!, $contentLength: Int!) { initiateCreateOrUpdateFile(oclId: $oclId, contentType: $contentType, contentLength: $contentLength) { uploadToken uploadUrl uploadUrlExpiry method headers { key value } useMultipart isSuccess error { message code retryable } } }"
  variables: { "oclId": "<oclId>", "contentType": "application/pdf", "contentLength": <bytes from sha256_file> }
```
From `data.initiateCreateOrUpdateFile` extract `uploadToken`, `uploadUrl`, `method`, `headers`.

### Step B — Upload to S3 (direct, NO x402 payment)
Convert the `headers` array (`[{key,value}, …]`) to a `{ key: value }` map. Use the EXACT `uploadUrl` + ALL `headers` from Step A:
```
mcp__molecule__s3_upload:
  uploadUrl: <uploadUrl from step A>
  filePath: <path-to-file>
  method: <method from step A, usually PUT>
  contentType: application/pdf
  headers: { <all key:value pairs from step A headers> }
```

### Step C — Finalize upload (x402 paid)

**Categories and tags** (REQUIRED — pick exactly one category and one or more correlated tags from the lists below; do NOT invent values). **Casing matters:** send the **category in lowercase** and each **tag in Title-Case** (e.g. `science` + `Discovery`). A wrong-cased finalize is rejected by the backend *after* the x402 payment has settled, so a casing mistake makes you pay 2–3× — get it right the first time.

Allowed categories (send lowercase): `['science', 'business', 'governance', 'media']`

Correlated tags (each tag belongs to exactly one category — only pick tags whose category matches):
```
business: 'Ecosystem Partnership', 'Funding', 'University Partnership', 'Important Meeting', 'Market Opportunity', 'Regulatory filing', 'Biotech Partnership'
governance: 'Proposal Failed', 'Proposal Approved', 'Proposal Open for Feedback'
media: 'Promotional material', 'Blog', 'News coverage', 'Academic article', 'Pitch deck'
science: 'Discovery', 'Clinical Trial', 'Provisional Patent Application', 'Validation', 'Milestone Achieved', 'Manufacturing', 'Lab Life', 'In vivo data', 'Patent licensed', 'Non-Provisional Patent Application', 'Optimization', 'Patent granted'
```
For a typical research-PDF upload, default to category `science` with tag(s) like `Discovery` or `Validation` unless the document clearly fits another category.
```
mcp__molecule__x402_pay:
  mutation: finishCreateOrUpdateFile
  query: "mutation FinishCreateOrUpdateFile($oclId: String!, $uploadToken: String!, $path: String, $accessLevel: String!, $changeBy: String!, $description: String, $tags: [String!], $categories: [String!]) { finishCreateOrUpdateFile(oclId: $oclId, uploadToken: $uploadToken, path: $path, accessLevel: $accessLevel, changeBy: $changeBy, description: $description, tags: $tags, categories: $categories) { datasetId contentHash version newHead isSuccess message error { message code retryable } } }"
  variables: { "oclId": "<oclId>", "uploadToken": "<from step A>", "path": "<filename>", "accessLevel": "PUBLIC", "changeBy": "<wallet_address>", "description": "<file description>", "categories": ["<one of: science | business | governance | media>"], "tags": ["<one or more correlated tags>"] }
```
From `data.finishCreateOrUpdateFile` extract `datasetId` (`did:odf:...`) and `contentHash`. Cache `datasetId`.

---

## Phase 3 (Private variant): Encrypted Upload to Data Room (Steps E0–E6)

Use this **instead of** Steps A–C for a confidential file. It is a faithful client-side replication of Labs
envelope encryption (`encryptFileWithKms`) — same algorithm, IV size, tag handling, and `contentHash` rule
(handled by `mcp__molecule__encrypt_file`). The backend never sees plaintext or the unwrapped key.

**Preconditions & invariants:**
- The DEK is generated with **`transport: direct`** + `auth: service-token` (needs `MOLECULE_SERVICE_TOKEN` bound to the operating wallet — the LabNFT/TBA owner). Keep it **direct** so the plaintext DEK stays in-process. If missing/expired, issue one (see **Service Token**): `mcp__molecule__issue_service_token: { serviceName: data-sync-service, expiresIn: "720h" }` (Privy) or `mcp__molecule__issue_owner_service_token: { ... }` (EOA).
- `accessLevel` MUST be `ADMIN` or `HOLDERS` (valid: `PUBLIC | HOLDERS | ADMIN`). Never `PUBLIC`.
- **Decrypt gate:** the finalized file is decryptable only by a wallet that passes the lab's access conditions (E4) — the lab's TBA owner, or a wallet you `grantRole`'d. If the AccessResolver is unreachable on the OCL chain, Step E5 surfaces a clear error — report it verbatim and stop.
- Crypto matches the Labs client exactly: **AES-256-GCM**, **random 12-byte IV**, **16-byte tag appended**, `contentHash` = **hex SHA-256 of the _plaintext_**, DEK = base64 raw 32 bytes, `iv` reported base64.

### Step E0 — Generate the data encryption key (direct, service-token)
```
mcp__molecule__labs_generate_dek:
  transport: direct
  auth: service-token
```
Returns `encryptedDek` (base64), `encryptionSystem` (echo verbatim), and `dekHandle`. The plaintext DEK stays in the MCP.

### Step E1 — Encrypt the file
```
mcp__molecule__encrypt_file:
  filePath: <path-to-pdf>
  dekHandle: <from E0>
  outPath: lab/encrypted/<filename>.enc
```
Save `iv` (base64), `contentHash` (hex), `cipherBytes`. The `.enc` file is `ciphertext‖tag` — that is what you upload.

### Step E2 — Initiate upload with the **ciphertext** size (x402 paid)
Identical to Step A, except `contentLength` = `cipherBytes` from E1:
```
mcp__molecule__x402_pay:
  mutation: initiateCreateOrUpdateFile
  query: "mutation InitiateCreateOrUpdateFile($oclId: String!, $contentType: String!, $contentLength: Int!) { initiateCreateOrUpdateFile(oclId: $oclId, contentType: $contentType, contentLength: $contentLength) { uploadToken uploadUrl uploadUrlExpiry method headers { key value } useMultipart isSuccess error { message code retryable } } }"
  variables: { "oclId": "<oclId>", "contentType": "application/pdf", "contentLength": <cipherBytes> }
```
Extract `uploadToken`, `uploadUrl`, `method`, `headers`.

### Step E3 — PUT the ciphertext to S3 (direct, NO x402)
```
mcp__molecule__s3_upload:
  uploadUrl: <uploadUrl from E2>
  filePath: lab/encrypted/<filename>.enc
  method: <method from E2, usually PUT>
  contentType: application/pdf
  headers: { <all key:value pairs from E2 headers> }
```

### Step E4 — Build `accessControlConditions` (OCL role OR TBA owner)
Builds the OCL access conditions — an OR of `hasRole(oclId, :userAddress, CONTRIBUTOR)` and
`isAuthorizedSignerForTba(:userAddress, labAccountAddress)` (the LabNFT/TBA owner). The chain string is
derived from `$CHAIN_ID` (`8453`→`base`, `84532`→`sepolia-base`).
```
mcp__molecule__build_access_conditions:
  oclId: "<oclId>"
  labAccountAddress: "<labAccountAddress>"
  role: contributor
```
`:userAddress` is a literal placeholder the backend evaluator substitutes (the decrypt-time service-token `adminAddress`). Use the returned **`json`** string as `encryptionMetadata.accessControlConditions` in E5.

### Step E5 — Finalize the encrypted upload (x402 paid)
Same category/tag rules as Step C. The new piece is `encryptionMetadata` (`EncryptionMetadataInput`) and the non-PUBLIC `accessLevel`. Generate `encryptedAt`:
```
Bash: date -u +%Y-%m-%dT%H:%M:%SZ
```
| Field | Value |
|-------|-------|
| `encryptionSystem` | echo from E0 (e.g. `kms`) |
| `accessControlConditions` | the E4 `json` string |
| `encryptedBy` | `<wallet_address>` |
| `encryptedAt` | ISO-8601 UTC timestamp |
| `encryptedDek` | `encryptedDek` from E0 (base64, wrapped) |
| `iv` | `iv` from E1 (base64) |
| `contentHash` | `contentHash` from E1 (hex SHA-256 of plaintext) |
```
mcp__molecule__x402_pay:
  mutation: finishCreateOrUpdateFile
  query: "mutation FinishCreateOrUpdateFile($oclId: String!, $uploadToken: String!, $path: String, $accessLevel: String!, $changeBy: String!, $description: String, $tags: [String!], $categories: [String!], $encryptionMetadata: EncryptionMetadataInput) { finishCreateOrUpdateFile(oclId: $oclId, uploadToken: $uploadToken, path: $path, accessLevel: $accessLevel, changeBy: $changeBy, description: $description, tags: $tags, categories: $categories, encryptionMetadata: $encryptionMetadata) { datasetId contentHash version newHead isSuccess message error { message code retryable } } }"
  variables: { "oclId": "<oclId>", "uploadToken": "<from E2>", "path": "<filename>", "accessLevel": "ADMIN", "changeBy": "<wallet_address>", "description": "<file description>", "categories": ["<category>"], "tags": ["<tags>"], "encryptionMetadata": { "encryptionSystem": "<from E0>", "accessControlConditions": "<E4 json string>", "encryptedBy": "<wallet_address>", "encryptedAt": "<ISO-8601 UTC>", "encryptedDek": "<from E0>", "iv": "<from E1>", "contentHash": "<from E1>" } }
```
From `data.finishCreateOrUpdateFile` extract `datasetId` (`did:odf:...`) and `contentHash`; cache `datasetId`.

### Step E6 (optional) — Verify decryption
`decryptDataKey` accepts `oclId`+`filePath` (a data-room file) or `tokenUri`+`agreementUrl` (an IPFS agreement). For the file above, pass `oclId` + the stored data-room `path`:
```
mcp__molecule__labs_decrypt_dek:
  oclId: "<oclId>"
  filePath: "<filename / data-room path from E5>"
  transport: direct
  auth: service-token
```
Returns `iv` + a fresh `dekHandle` on success. `ACCESS_DENIED` means the decrypt caller fails the two-gate check (role + on-chain conditions); `LEGACY_ENCRYPTION` means the file predates the envelope flow.

**The decrypt caller is the service token's `adminAddress`, NOT the `x-wallet-address` header.** To decrypt *as* a given wallet, present a `MOLECULE_SERVICE_TOKEN` bound to that wallet (it must be the lab's TBA owner or hold a role on the lab). Pass it via the per-call `serviceToken` override if it differs from the env default:
```
mcp__molecule__labs_decrypt_dek:
  oclId: "<oclId>"
  filePath: "<data-room path>"
  serviceToken: "<token bound to the wallet you want to decrypt as>"
```
```
mcp__molecule__decrypt_file:
  filePath: lab/encrypted/<filename>.enc
  iv: <iv from labs_decrypt_dek>
  dekHandle: <from labs_decrypt_dek>
  outPath: lab/decrypted-check.bin
```
The returned `plaintextSha256` MUST equal the `contentHash` from E1 — that confirms the round trip.

## Phase 4: Create Announcement (via x402)
```
mcp__molecule__x402_pay:
  mutation: createAnnouncement
  query: "mutation CreateAnnouncement($oclId: String!, $headline: String!, $body: String!, $attachments: [String!]) { createAnnouncement(oclId: $oclId, headline: $headline, body: $body, attachments: $attachments) { isSuccess message error { message code retryable } } }"
  variables: { "oclId": "<oclId>", "headline": "<lab name>", "body": "<markdown body>", "attachments": ["<datasetId from upload>"] }
```

### External Posting Copy Rules (Phase 4 body + any Beach.science post)
The active chain id for this run is **$CHAIN_ID** (resolved from env).
- **Project URL:** use `$MOLECULE_CLIENT_URL/projects/{shortname}` verbatim — never substitute the `oclId`, another domain, or the legacy `/ipnfts/` route.
- **Chain name:** chain id `8453` → "Base mainnet (8453)"; `84532` → "Base Sepolia (84532)". Name any other chain id explicitly. Do NOT mislabel the active chain.
- **TX explorer links:** chain id `8453` → `https://basescan.org/tx/<hash>`; `84532` → `https://sepolia.basescan.org/tx/<hash>`.
- **Update slugs:** any `/updates/<slug>` link MUST be lowercase, hyphen-separated, NO file extension (e.g. `/updates/kiss1r-pipeline-update-gen2`).
- Do not invent URLs, shortnames, or transaction hashes — use the values saved to `shared_cache` during this run.

## Phase 5: Co-ownership / hand-off

**Skip entirely** if `EVM_WALLET_ADDRESS` is not set or equals the agent's `wallet_address`. Otherwise the owner wallet is `$EVM_WALLET_ADDRESS` (save as `owner_wallet`).

There are two ways to give the owner access; **default to grantRole** (additive — both the agent and the owner keep access; needed if the agent must keep operating the lab):

### Option A (default) — Grant the owner a role on the lab
A role grant satisfies BOTH decrypt gates (the DB `authorizeViewer` and the on-chain `hasRole` OR-branch). `grantRole(oclId, account, role, expiry, isAgent)` — role `2` = Contributor; `expiry` `0` = no expiry; `isAgent` `false`.
```
mcp__molecule__abi_encode:
  functionSignature: "grantRole(bytes32,address,uint8,uint64,bool)"
  args: ["<oclId>", "<owner_wallet>", 2, 0, false]
```
Send with the active backend — **Privy →** `privy_send_transaction`, **EOA →** `eoa_send_transaction` (same args):
```
mcp__molecule__privy_send_transaction:   # OR mcp__molecule__eoa_send_transaction (backend=eoa)
  to: $ACCESS_RESOLVER_ADDRESS
  data: <calldata>
  chainId: $CHAIN_ID
```
Save `txHash` as `grant_tx_hash`.

**Verify + indexing caveat (important).** The grant is effective **on-chain** immediately — confirm with `mcp__molecule__ocl_read: { functionSignature: "hasRole(bytes32,address,uint8)", to: $ACCESS_RESOLVER_ADDRESS, args: ["<oclId>", "<owner_wallet>", 2], returns: ["bool"] }` → `true`. But decryption ALSO needs the DB `authorizeViewer` gate, which reads an **indexed** `ocl_user` table populated by the AccessResolver `RoleGranted` replay (`ocl-processor`), NOT the chain directly. That indexer can lag behind the chain, especially on staging. So the grantee can temporarily get `AUTH_FAILED` ("not authorized as viewer") from `decryptDataKey` even though `hasRole` is already `true`. To check readiness, test `labs_decrypt_dek` as the grantee (a service token bound to that wallet). If it keeps returning `AUTH_FAILED` long after the grant, the AccessResolver indexer is behind for this environment — use **Option B (transfer)** for prompt decrypt access, or flag the backend; do NOT re-grant (the on-chain state is already correct).

### Option B — Transfer the LabNFT (true hand-off; the agent LOSES control)
Only when the owner should fully take over. After transfer the new holder is the TBA owner and the agent can no longer admin or decrypt. **Never transfer to the lab's own TBA (`labAccountAddress`) — the contract reverts.**
```
mcp__molecule__abi_encode:
  functionSignature: "safeTransferFrom(address,address,uint256)"
  args: ["<wallet_address>", "<owner_wallet>", "<labNftTokenId>"]
```
Send with the active backend:
- **Privy** (`backend=privy`) — use `privy_send_raw_transaction` (Privy's `eth_sendTransaction` returns a *phantom hash* for `safeTransferFrom`, so the sign-only + self-broadcast path is required):
```
mcp__molecule__privy_send_raw_transaction:
  to: <lab_nft_address>
  data: <calldata>
  chainId: $CHAIN_ID
```
- **EOA** (`backend=eoa`) — use `eoa_send_transaction` (an EOA already signs locally and self-broadcasts, so there is no phantom-hash workaround; same args):
```
mcp__molecule__eoa_send_transaction:
  to: <lab_nft_address>
  data: <calldata>
  chainId: $CHAIN_ID
```
Save `txHash` as `transfer_tx_hash`. **Verify** with `mcp__molecule__ocl_read: { functionSignature: "ownerOf(uint256)", to: <lab_nft_address>, args: ["<labNftTokenId>"], returns: ["address"] }` → `owner_wallet`. Unlike a role grant (Option A), the new owner's membership indexes via the **fast `onchain_event` path** (the LabNFT `Transfer` event → ~minutes), so decrypt works promptly. This is the reliable way to give a wallet decrypt access when the AccessResolver-event indexer is lagging — at the cost of handing over control.

### Owner decrypt access (private / encrypted uploads only)
Skip for public uploads. After Option A (grantRole) or Option B (transfer), the owner satisfies the lab's
access conditions. The owner decrypts by presenting a service token **bound to the owner's wallet** (per-call
`serviceToken` override on `labs_decrypt_dek`): Privy owner → `issue_service_token: { walletId: "<owner's Privy wallet id>" }`; EOA owner → `issue_owner_service_token: {}` (signs with `WALLET_PRIVATE_KEY`). The token's `adminAddress` must be the owner's address.

## Output

Final results to report:
- `oclId` (the lab's 32-byte id)
- `labAccountAddress` (the token-bound account)
- `labNftTokenId`
- `mint_tx_hash` (if a new lab was minted; omit if an existing lab was reused)
- `project_url`: `$MOLECULE_CLIENT_URL/projects/{shortname}`
- `datasetId` from upload
- Announcement success status
- `grant_tx_hash` or `transfer_tx_hash` (if Phase 5 ran)
