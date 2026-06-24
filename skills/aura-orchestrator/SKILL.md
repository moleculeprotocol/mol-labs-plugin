---
name: aura-orchestrator
description: End-to-end DeSci molecule — POI registration, IP-NFT minting, Molecule authentication, project creation, file upload (public or private/encrypted), and announcement. Single-agent sequential execution. The `molecule` MCP server CRAFTS every request/payload (it is custody-free — it never holds a key, signs, or broadcasts); YOUR wallet (we recommend Privy agentic wallets or any key & rpc server you can use to interact with an EVM blockchain) does all signing/sending.
platforms: [macos, linux]
metadata:
  # Claude Code: these are injected into the MCP subprocess via settings.json / settings.local.json
  env_vars:
    - MOLECULE_CLIENT_URL
    - MOLECULE_LABS_URL
    - IPNFT_CONTRACT_ADDRESS
    - ACCESS_RESOLVER_ADDRESS
    - X402_GATEWAY_URL
    - EVM_WALLET_ADDRESS
    - CHAIN_ID
    - EXPERIMENT_COST_CENTS
    - POI_API_KEY
    - MOLECULE_API_KEY
    - MOLECULE_SERVICE_TOKEN
  hermes:
    tags: [desci, blockchain, ip-nft, molecule, x402, encryption, privy, web3]
    category: web3
    requires_toolsets: [terminal]
    config:
      - key: MOLECULE_CLIENT_URL
        description: "Molecule Labs client base URL"
        default: "https://testnet.molecule.xyz/"
        prompt: "Enter your Molecule Labs client URL (staging default: https://testnet.molecule.xyz/)"
      - key: MOLECULE_LABS_URL
        description: "Molecule Labs GraphQL API URL"
        default: "https://staging.graphql.api.molecule.xyz/graphql"
        prompt: "Enter the Molecule Labs API URL (staging default: https://staging.graphql.api.molecule.xyz/graphql)"
      - key: IPNFT_CONTRACT_ADDRESS
        description: "IP-NFT smart contract address on-chain"
        default: "0x152B444e60C526fe4434C721561a077269FcF61a"
        prompt: "Enter the IPNFT contract address (Sepolia staging: 0x152B444e60C526fe4434C721561a077269FcF61a)"
      - key: ACCESS_RESOLVER_ADDRESS
        description: "AccessResolver contract address used for encrypted file access conditions"
        default: "0xd9b492fd34b1579C052b2EA25970178B3011Ce6B"
        prompt: "Enter the AccessResolver contract address (Sepolia staging: 0xd9b492fd34b1579C052b2EA25970178B3011Ce6B)"
      - key: X402_GATEWAY_URL
        description: "x402 payment gateway URL"
        default: "https://zgnyn6izbk.execute-api.eu-central-2.amazonaws.com/prod"
        prompt: "Enter the x402 gateway URL (staging default: https://zgnyn6izbk.execute-api.eu-central-2.amazonaws.com/prod)"
      - key: EVM_WALLET_ADDRESS
        description: "Your operating wallet PUBLIC address — this is an address, NOT a private key"
        prompt: "Enter your EVM wallet public address (0x...)"
      - key: CHAIN_ID
        description: "EVM chain ID (1=Ethereum mainnet, 8453=Base, 11155111=Sepolia, 84532=Base Sepolia)"
        default: "11155111"
        prompt: "Enter the chain ID (staging uses Sepolia: 11155111)"
      - key: ENVIRONMENT
        description: "Deployment environment tag read by build_access_conditions"
        default: "staging"
        prompt: "Enter the environment (staging)"
      - key: EXPERIMENT_COST_CENTS
        description: "Default experiment / funding cost in USD cents (used in non-interactive runs)"
        default: "1"
        prompt: "Enter the default experiment cost in cents (staging default: 1)"
      - key: POI_API_KEY
        description: "API key for Proof of Invention registration endpoint"
        prompt: "Enter your POI API key"
      - key: MOLECULE_API_KEY
        description: "Molecule Labs API key for GraphQL mutations"
        prompt: "Enter your Molecule API key"
---

# Aura Orchestrator

## Prerequisites

This skill requires the **`molecule` MCP server** to be running as a local stdio process before you
execute any step. The server is included in this repository at `mcp/server.py` and requires Python + `uv`.

**Start the MCP server** (set `MOLECULE_PLUGIN_ROOT` to wherever you cloned / installed this repo):

```bash
cd $MOLECULE_PLUGIN_ROOT && uv run mcp/server.py
```

Or configure it in your harness's MCP server block:

```json
{
  "mcpServers": {
    "molecule": {
      "command": "uv",
      "args": ["run", "/path/to/mol-labs-plugin/mcp/server.py"],
      "env": {
        "MOLECULE_CLIENT_URL": "https://testnet.molecule.xyz/",
        "MOLECULE_LABS_URL": "https://staging.graphql.api.molecule.xyz/graphql",
        "IPNFT_CONTRACT_ADDRESS": "0x152B444e60C526fe4434C721561a077269FcF61a",
        "ACCESS_RESOLVER_ADDRESS": "0xd9b492fd34b1579C052b2EA25970178B3011Ce6B",
        "X402_GATEWAY_URL": "https://zgnyn6izbk.execute-api.eu-central-2.amazonaws.com/prod",
        "CHAIN_ID": "11155111",
        "ENVIRONMENT": "staging",
        "EXPERIMENT_COST_CENTS": "1",
        "EVM_WALLET_ADDRESS": "<your-wallet-address>",
        "POI_API_KEY": "<secret>",
        "MOLECULE_API_KEY": "<secret>",
        "MOLECULE_SERVICE_TOKEN": "<secret — private uploads only>"
      }
    }
  }
}
```

All `mcp__molecule__*` tool calls below are dispatched to this server. In Claude Code the tool prefix is
`mcp__molecule__<tool>`; in other harnesses the naming convention may differ — consult your harness docs
for how stdio MCP tools are addressed, and substitute accordingly.

**`shared_cache`** is used throughout this skill to persist values across steps (reservation IDs, hashes,
wallet address, etc.). In Claude Code this is a built-in tool. In other harnesses, use whichever
key-value store your runtime provides (an in-memory dict, a scratch file, or your harness's equivalent)
— the important thing is that values written in one step are readable in later steps within the same run.

**`read_file`** is used for PDF text extraction. In Claude Code this is a built-in tool. In Hermes or
other harnesses, use your runtime's file-reading or document-parsing tool.

**Wallet / signing** either provision a Privy agentic wallet (recommended, check the bundled
`privy-agentic-wallets` skill), or reuse your own wallet skill / EOA / rpc logic. If you do, your harness must be able to call the wallet's
signing RPC (`eth_signTypedData_v4`, `personal_sign`, `eth_sendTransaction`). See
[references/wallet-signing.md](references/wallet-signing.md).

**`MOLECULE_SERVICE_TOKEN`** (private uploads only) is a secret JWT — do NOT expose it via a config
prompt. Store it in your harness's secrets store and inject it as an env var into the MCP server process.

---

Complete DeSci molecule executed as one continuous sequence of tool calls.
Do NOT stop, report progress, or output text between steps — execute ALL steps as one uninterrupted flow.
(The one unavoidable interruption is signing: wherever the flow says **sign with your wallet**, hand
the prepared payload to your wallet, get the signature/txHash, and continue — see *Wallet & signing*.)

The **`molecule` MCP server** (`mcp/`) is **custody-free**: it *crafts* the transactions, EIP-712
typed-data, and HTTP payloads this molecule needs and runs only the non-signing HTTP around them — it
**never holds a private key, signs, or broadcasts**. Every wallet-dependent step is handed back to
**your wallet** to sign/send (a **Privy agentic wallet — the recommended first option** — or any key
you control), and you feed the signature / transaction hash back in to continue. See
[references/wallet-signing.md](references/wallet-signing.md) for ready-to-use signing snippets.
The only non-MCP tools used are `read_file` (PDF text extraction), `shared_cache` (cross-step state),
your wallet's signer, and `Bash` (waits/timestamps only — never curl).

**SUPER IMPORTANT RULES:**
- **The MCP never signs or sends.** It is custody-free: it returns *prepared* payloads and you sign/broadcast them with **your wallet** (we recomment Privy agentic wallets, or a keypair that you control with a dedicated wallet / signing skill). Wherever a step says "sign with your wallet" / "sign & send with your wallet", do exactly that and feed the resulting `signature` / `txHash` into the next step. NEVER ask the MCP to sign or send, and NEVER put a private key in a tool argument — the MCP only ever needs a **public address**. See [references/wallet-signing.md](references/wallet-signing.md).
- POI registration is an **HTTP API call** (`mcp__molecule__poi_register`), NOT a smart contract call. Do NOT use `abi_encode` or `prepare_transaction` for POI.
- Use `read_file` for PDFs — it has built-in PDF text extraction. NEVER use python, pip, pdftotext, or any shell tools for PDF reading.
- Do NOT `read_file` on image/binary attachments (PNG, JPG, etc.). The upload flow only needs the `file_path` — pass the path directly to `mcp__molecule__s3_upload`.
- Use `shared_cache` to persist all critical molecule values (IDs, hashes, tokens, the `dekHandle`). If you need a value from an earlier step, retrieve it from cache.
- Follow every URL, contract address, and function signature in this document EXACTLY. Do NOT guess or fabricate alternatives. URLs and contract addresses come from `.env`, read by the MCP — never hardcode.
- Use the **x402 prepare → sign → submit** flow for ALL Molecule paid mutations (project creation, file uploads, announcements, ownership): `mcp__molecule__x402_prepare` builds the EIP-712 the **caller** signs, then `mcp__molecule__x402_submit` posts that signature. The MCP signs nothing — see the *x402 Payment Flow* section.
- For **private / confidential** files, use the Private / Encrypted Upload variant in Phase 4 (Steps E0–E5) **instead of** the public Steps A–C: generate a one-shot DEK (kept inside the MCP), AES-256-GCM encrypt locally, upload the ciphertext, and finish with `encryptionMetadata` + a non-PUBLIC `accessLevel`. NEVER upload a confidential file as plaintext or with `accessLevel: PUBLIC`.
- **FAIL CLOSED — no public fallback for confidential files.** Once a file is chosen for the Private / Encrypted variant, if **any** step (E0 DEK generation, E1 encryption, E2/E3 ciphertext upload, E4 access conditions, E5 finalize, E6 verify) fails and you cannot fix it in-path, **ABORT the entire molecule and report the error.** Do **NOT** "recover" by running the public Steps A–C, do **NOT** re-upload with `accessLevel: PUBLIC`, and do **NOT** `s3_upload` the plaintext PDF — ever. A confidential file leaking to public is a far worse outcome than a failed run. This is also enforced in code: `encrypt_file` arms a non-overridable MCP guard that refuses to S3-upload that file's plaintext, and `build_access_conditions` arms a guard that refuses to finalize that IP-NFT as `PUBLIC` / without `encryptionMetadata`. Do not attempt to work around these guards — they are the safety net, not the plan.
- The plaintext DEK is single-use and secret. It **never leaves the MCP** — `labs_generate_dek` returns only an opaque `dekHandle`. NEVER attempt to obtain, cache, or log the plaintext DEK. Only the wrapped `encryptedDek` and the ciphertext are persisted.
- AES-256-GCM encrypt/decrypt is handled by `mcp__molecule__encrypt_file`/`decrypt_file` (it replicates the Labs Web Crypto `encryptFileWithKms`). PDF reading still uses `read_file` — never python/pip/pdftotext.
- Phases executed sequentially without stopping or reporting intermediate progress.

## Environment Variables — the MCP reads these for non-signing HTTP, payload crafting, and your wallet's **public** address. The MCP holds **no private key and no wallet secret** — your wallet (a Privy agentic wallet, recommended, or your own key) keeps the key and does all signing. A required var, if missing, makes the relevant MCP tool terminate with an error naming it.

| Variable | Description |
|----------|-------------|
| `EVM_WALLET_ADDRESS` | **Public address of your operating wallet** — the wallet that signs the terms, authorizes x402 payments, mints, and (until any transfer) owns the IP-NFT. The MCP uses it as the default signer identity (x402 `from`, service-token `x-wallet-address`) when you do not pass `walletAddress` explicitly. This is an address, **NOT a private key**. |
| `MOLECULE_SERVICE_TOKEN` | **Private uploads only.** Off-chain JWT for the direct (non-x402) DEK generate/decrypt calls (`x-service-token`), bound to one wallet's address as its `adminAddress`. Not needed for public uploads. If missing/expired, mint a fresh one bound to your operating wallet — `service_signin_message` → **sign with your wallet** → `service_token_create` (see **Service Token** below). Secret — keep in `settings.local.json`. |

**Your wallet's private key NEVER goes to the MCP.** All signing happens in your wallet (Privy agentic
wallet — recommended first option — or any key you control); see *Wallet & signing* below and
[references/wallet-signing.md](references/wallet-signing.md). The MCP also reads URLs / contract
addresses / API keys from the environment (`MOLECULE_CLIENT_URL`, `MOLECULE_LABS_URL`,
`X402_GATEWAY_URL`, `ACCESS_RESOLVER_ADDRESS`, `IPNFT_CONTRACT_ADDRESS`, `CHAIN_ID`, `ENVIRONMENT`,
`POI_API_KEY`, `MOLECULE_API_KEY`) — see `mcp/README.md` for the per-tool breakdown.

**Note:** Non-secrets live in `.claude/settings.json`, secrets in `.claude/settings.local.json`; Claude
Code injects both into the MCP subprocess. The skill passes only file paths, addresses, queries, and
non-secret values as tool arguments — **never a private key**. Switching between staging and production is
a `.env` edit only — never modify the skill body for environment changes.

## Input

- A research PDF file in the workspace (e.g. `.tengu-attachments/document.pdf`)
- An optional cover image (PNG/JPG) in `.tengu-attachments/`
- Title, description, symbol, topic — draft these from the research document (surface them so the user can override).
- **Research lead (name + email)** — REQUIRED and **user-supplied**. Do **NOT** draft, guess, or infer the research lead from the document text, author list, or cover image. You **MUST ask the user to paste the research lead's name and email up front** (see *Collect run inputs* below); if the document happens to name an author you may offer it as a pre-filled suggestion, but the user must confirm or replace it — never use an un-confirmed value.
- **Organization** and **experiment / funding cost** — REQUIRED, run-specific, and **NOT derivable from the document and NOT static config**. You **MUST ask the user for both up front** (see *Collect run inputs* below). Never guess `organization` from the document text or the cover-image label, and never silently use the `EXPERIMENT_COST_CENTS` env var as the cost.
- **Upload visibility** — REQUIRED and **user-supplied**; it is the one knob that changes Phase 4, so you **MUST ask the user up front** (see *Collect run inputs* below) rather than silently assuming. Pick ONE:
  1. **Public file upload** — the file is stored as plaintext with `accessLevel: PUBLIC`. Run Phase 4 Steps A–C.
  2. **Private file upload** (confidential / encrypted) — the file is AES-256-GCM envelope-encrypted client-side, stored as ciphertext with a non-PUBLIC `accessLevel` and on-chain access conditions. Run Phase 4 Private variant Steps E0–E6 **instead of** A–C. This path additionally needs `MOLECULE_SERVICE_TOKEN` (see below).

  Everything else (Phases 0–3, 5, 6) is identical for both options, and **x402 payment is used for both** (`initiateCreateOrUpdateFileV2` / `finishCreateOrUpdateFileV2` are paid per call regardless of visibility). Present **public** as the pre-selected default in the question, but only proceed once the user has confirmed the choice — do not skip the ask.

## Collect run inputs (do this BEFORE the uninterrupted flow)

The "do not stop or report between steps" rule governs the **execution** flow (Phases 0–6). Gathering inputs happens *first*, before that flow begins — it is not an interruption. Before Phase 1, ask the user — in a single prompt (e.g. `AskUserQuestion` in Claude Code, or the equivalent in your harness) — for the values that are neither derivable nor static. **Never hallucinate or silently default any of these** — each must come from the user:

1. **Research lead — name + email** — the person recorded as `research_lead` on the IP-NFT (Phase 2 Steps 2 & 5). This is **user-supplied; do NOT invent it from the document, author list, or cover image**. Ask the user to paste the lead's name and email. If the document plausibly names an author you MAY pre-fill it as a suggestion, but require the user to confirm or replace it before using it.
2. **Upload visibility — public or private/encrypted** — the Phase 4 path selector. Ask explicitly; present **public** as the pre-selected default but require the user to confirm. `public` → Phase 4 Steps A–C; `private/encrypted` → Phase 4 Private variant Steps E0–E6 (also needs `MOLECULE_SERVICE_TOKEN`).
3. **Organization** — the organization / lab name recorded on the IP-NFT (the `organization` field in Phase 2 Steps 2 & 5). There is **no default**; if the user is unsure, have them confirm an explicit value rather than inventing one from the document or cover image.
4. **Experiment / funding cost (USD)** — the funding amount in US dollars (e.g. `5000` → $5,000.00). Convert to integer cents for `funding_amount.value`: `experiment_cost_cents = round(USD × 100)`, kept with `"decimals": 2` (so $0.01 → `1`, $5,000 → `500000`). Only fall back to the `EXPERIMENT_COST_CENTS` env var when the run is fully non-interactive; in an interactive session always use the user's answer.
5. **Transfer recipient (OPTIONAL)** — the long-term owner wallet address to transfer the finished IP-NFT to in Phase 6. **Default: none** (the IP-NFT stays with the operating wallet and Phase 6 is skipped). Only set it if the user explicitly wants the IP-NFT handed off to a different wallet; this is **not** `EVM_WALLET_ADDRESS` (which is the operating/signer address).

You MAY confirm the auto-drafted title, symbol, and topic in the same prompt. Once these inputs are gathered, run Phases 0–6 as one uninterrupted sequence. Use the collected research lead, organization, and `experiment_cost_cents` wherever Phase 2 references `research_lead`, `<organization>`, and the funding amount, the collected visibility to choose the Phase 4 path, and the optional transfer recipient as `owner_wallet` in Phase 6.

## Phase 0: Wallet & signing setup

This molecule needs a wallet you can **sign with** — the MCP is custody-free and never signs, so you
bring the signer. The MCP only ever needs the wallet's **public address**; the private key stays with you.

**Recommended first option: a Privy agentic wallet** — a server-side, policy-guarded wallet purpose-built
for autonomous agents. You may instead **bring your own key** (any EOA / signer you control). Either way,
you sign every prepared payload yourself; see [references/wallet-signing.md](references/wallet-signing.md).

### Step 0a — Pick / provision your signer

- **Privy agentic wallet (recommended).** If you don't have one yet, provision it with the
  **`privy-agentic-wallets`** skill (create a policy + server wallet, e.g. single-chain + a per-tx value
  cap) and note its address. Signing how-to: [references/wallet-signing.md](references/wallet-signing.md) §A.
- **Bring your own key.** Use any wallet/library you control (viem, ethers, eth-account, a hardware
  signer, …). Signing how-to: [references/wallet-signing.md](references/wallet-signing.md) §B.

### Step 0b — Record the operating address

Obtain your operating wallet's **public address** from your signer (Privy: fetch the wallet address; own
key: derive it), cache it, and make it the MCP's default identity:

```
shared_cache: { "operation": "put", "namespace": "molecule", "key": "wallet_address", "value": "<operating address>" }
```

Ensure `EVM_WALLET_ADDRESS` equals this address (set it in `.claude/settings.json` for future runs).
Throughout this skill, pass this `wallet_address` as `walletAddress` to `x402_prepare`,
`service_signin_message`, and the service-token DEK calls so the MCP crafts payloads under your signer's
identity (the EIP-3009 `from`, the IP-NFT minter, and the service-token `adminAddress` must all be this
address).

Save wallet details (address + which signer) to `mint/wallet_info.json`. **NEVER write a private key to disk.**

## Phase 1: POI Registration

Register the research PDF as a Proof of Invention.

**CRITICAL**: `mcp__molecule__poi_register` posts to the EXACT endpoint `$MOLECULE_CLIENT_URL/api/v1/inventions` (field name `files`, Bearer `$POI_API_KEY`). Do NOT guess, modify, or construct alternative POI URLs — there is no other POI endpoint.

```
mcp__molecule__poi_register:
  filePath: <path-to-pdf>
```

If it fails, stop and report the error.

The tool extracts from the response:
- `poiTo` ← `data.transaction.to`
- `poiData` ← `data.transaction.data`
- `merkleRoot` ← `data.proof.tree[0]` (a 0x-prefixed hex hash, e.g. `0x35554760...`)

Save the full `response` to `mint/metadata/poi_result.json`.

Immediately cache POI outputs:

```
shared_cache: { "operation": "put", "namespace": "molecule", "key": "poi_to", "value": "<poiTo>" }
shared_cache: { "operation": "put", "namespace": "molecule", "key": "poi_data", "value": "<poiData>" }
shared_cache: { "operation": "put", "namespace": "molecule", "key": "merkle_root", "value": "<merkleRoot>" }
```

## ID Chain (critical — read before Phase 2)

The `merkle_root` from POI drives ALL subsequent IDs:

1. `reservationId` = `hex_to_uint256(merkle_root)` — a large decimal number (NOT 1, NOT a small number)
2. `reservationId` IS the `token_id` / `ipnftId` / `ipnftTokenId` — these are ALL the same value
3. `ipnft_uid` = `$IPNFT_CONTRACT_ADDRESS_{reservationId}` (contract address + underscore + decimal token ID)
4. The Molecule project URL = `$MOLECULE_CLIENT_URL/ipnfts/{reservationId}`

Derive it now:

```
mcp__molecule__hex_to_uint256:
  hex: <merkleRoot>
```

If the returned `decimal` is a small number (`isSmall: true`, e.g. 0 or 1), something went wrong in Phase 1. Stop and report the error.

Cache it immediately and use it for ALL subsequent steps:
```
shared_cache: { "operation": "put", "namespace": "molecule", "key": "reservation_id", "value": "<decimal>" }
```

Proceed immediately to Phase 2 — the merkle root is already in the POI response, no waiting needed.

## Phase 2: IP-NFT Minting (10 steps)

### Step 1 — Anchor POI on-chain

Prepare the anchor transaction, then **sign & send it with your wallet**:

```
mcp__molecule__prepare_transaction:
  to: <poi_to>
  data: <poi_data>
  chainId: $CHAIN_ID
```

Hand the returned `transaction` to your wallet to broadcast (Privy: `eth_sendTransaction`; own key: sign
+ `eth_sendRawTransaction` — see [references/wallet-signing.md](references/wallet-signing.md)). Save the
resulting `txHash` as `poi_tx_hash`. (The `reservationId` was already derived from the `merkle_root` in the ID Chain section — it MUST be a large number, typically 50+ digits. Use it as `ipnftId` in ALL subsequent steps.)

Cache critical IDs immediately:

```
shared_cache: { "operation": "put", "namespace": "molecule", "key": "poi_tx_hash", "value": "<poi_tx_hash>" }
shared_cache: { "operation": "put", "namespace": "molecule", "key": "wallet_address", "value": "<wallet_address>" }
```

If you lose context of the reservationId at any point, retrieve it:

```
shared_cache: { "operation": "get", "namespace": "molecule", "key": "reservation_id" }
```

### Step 2 — Generate assignment agreement

```
mcp__molecule__labs_graphql:
  auth: api-key
  query: "mutation GenerateAssignmentAgreement($projectData: AWSJSON!) { generateAssignmentAgreement(projectData: $projectData) { agreementCid agreementContentHash isSuccess error { message code retryable } } }"
  variables: { "projectData": "<JSON-encoded string, see below>" }
```

`projectData` is a **JSON-encoded string** containing:
```json
{
  "project": {
    "name": "<title>",
    "description": "<description>",
    "initialSymbol": "<symbol>",
    "funding_amount": {"value": <experiment_cost_cents>, "currency": "USD", "currency_type": "ISO4217", "decimals": 2},
    "organization": "<organization>",
    "research_lead": {"name": "<lead_name>", "email": "<lead_email>"},
    "topic": "<topic>"
  },
  "connectedWalletAddress": "<wallet_address>",
  "agreementType": "POI_ASSIGNMENT",
  "chainId": $CHAIN_ID,
  "ipnftId": "<reservationId as decimal string>",
  "poiLocation": {"chainId": $CHAIN_ID, "transactionHash": "<poi_tx_hash>"},
  "merkleRootHash": "<merkle_root>"
}
```

Save `agreementCid` and `agreementContentHash` from `data.generateAssignmentAgreement`.

### Step 3 — Get image upload URL

```
mcp__molecule__labs_graphql:
  auth: api-key
  query: "mutation GenerateImageUploadUrl($filename: String!, $contentType: String!, $ipnftId: String!) { generateImageUploadUrl(filename: $filename, contentType: $contentType, ipnftId: $ipnftId) { uploadUrl key isSuccess error { message code retryable } } }"
  variables: { "filename": "cover.png", "contentType": "image/png", "ipnftId": "<reservationId>" }
```

Save `uploadUrl` and `key` (image key) from `data.generateImageUploadUrl`.

### Step 4 — Upload cover image

If a cover image exists in `.tengu-attachments/`, upload it. Otherwise skip.

```
mcp__molecule__s3_upload:
  uploadUrl: <uploadUrl from step 3>
  filePath: <path to image>
  method: PUT
  contentType: image/png
```

### Step 5 — Upload metadata

```
mcp__molecule__labs_graphql:
  auth: api-key
  query: "mutation UploadMetadataWithImageKey($metadata: AWSJSON!, $imageKey: String!, $ipnftId: String!) { uploadMetadataWithImageKey(metadata: $metadata, imageKey: $imageKey, ipnftId: $ipnftId) { metadataCid metadataUrl isSuccess error { message code retryable } } }"
  variables: { "metadata": "<JSON-encoded string, see below>", "imageKey": "<key from step 3>", "ipnftId": "<reservationId>" }
```

`metadata` is a **JSON-encoded string**:
```json
{
  "name": "<title>",
  "description": "<description>",
  "external_url": "$MOLECULE_CLIENT_URL",
  "terms_signature": "placeholder",
  "properties": {
    "agreements": [{"content_hash": "<agreementContentHash>", "mime_type": "application/json", "type": "POI_ASSIGNMENT", "url": "ipfs://<agreementCid>"}],
    "initial_symbol": "<symbol>",
    "project_details": {
      "funding_amount": {"value": <experiment_cost_cents>, "currency": "USD", "currency_type": "ISO4217", "decimals": 2},
      "organization": "<organization>",
      "research_lead": {"name": "<lead_name>", "email": "<lead_email>"},
      "topic": "<topic>"
    }
  }
}
```

Save `metadataCid` from `data.uploadMetadataWithImageKey`.

### Step 6 — Get terms message

```
mcp__molecule__labs_graphql:
  auth: api-key
  query: "query GetTermsMessage($metadataCid: String!, $minter: String!, $chainId: Int!) { getTermsMessage(metadataCid: $metadataCid, minter: $minter, chainId: $chainId) { message digest isSuccess error { message code retryable } } }"
  variables: { "metadataCid": "<metadataCid from step 5>", "minter": "<wallet_address>", "chainId": $CHAIN_ID }
```

Save `message` from `data.getTermsMessage`.

### Step 7 — Sign terms (with your wallet)

Personal-sign (EIP-191) the **exact** `message` from Step 6 with **your wallet** (Privy: `personal_sign`;
own key: sign the message — see [references/wallet-signing.md](references/wallet-signing.md)). The MCP
does not sign this — the signature must come from the wallet whose address is the `minter`
(your `wallet_address`). Save the returned `signature`.

### Step 8 — Sign off metadata (get authorization)

```
mcp__molecule__labs_graphql:
  auth: api-key
  query: "mutation SignoffMetadata($ipnftId: String!, $tokenURI: String!, $chainId: Int!, $minter: String!, $to: String!, $termsSignature: String!) { signoffMetadata(ipnftId: $ipnftId, tokenURI: $tokenURI, chainId: $chainId, minter: $minter, to: $to, termsSignature: $termsSignature) { authorization isSuccess error { message code retryable } } }"
  variables: { "ipnftId": "<reservationId>", "tokenURI": "ipfs://<metadataCid>", "chainId": $CHAIN_ID, "minter": "<wallet_address>", "to": "<wallet_address>", "termsSignature": "<signature from step 7>" }
```

Save `authorization` from `data.signoffMetadata`.

### Step 9 — ABI-encode the mint call

```
mcp__molecule__abi_encode:
  functionSignature: "mintReservation(address,uint256,string,string,bytes)"
  args:
    - <wallet_address>
    - <reservationId as decimal string>
    - "ipfs://<metadataCid>"
    - <symbol>
    - <authorization from step 8>
```

Save `calldata`.

### Step 10 — Mint IP-NFT on-chain

Prepare the mint transaction, then **sign & send it with your wallet**:

```
mcp__molecule__prepare_transaction:
  to: $IPNFT_CONTRACT_ADDRESS
  data: <calldata from step 9>
  value: "1000000000000000"
  chainId: $CHAIN_ID
```

The mint fee is 0.001 ETH (1000000000000000 wei). Hand the returned `transaction` to your wallet to
broadcast (Privy: `eth_sendTransaction`; own key: sign + `eth_sendRawTransaction`). Save the resulting
`txHash` as `mint_tx_hash`.

Save to `mint/metadata/mint_result.json`:
- `reservation_id` (the large decimal — this IS the token_id)
- `poi_tx_hash`
- `mint_tx_hash`
- `metadata_cid`
- `ipnft_symbol`
- `contract_address`: `$IPNFT_CONTRACT_ADDRESS`
- `ipnft_uid`: `$IPNFT_CONTRACT_ADDRESS_{reservation_id}`

Cache mint results:

```
shared_cache: { "operation": "put", "namespace": "molecule", "key": "mint_tx_hash", "value": "<mint_tx_hash>" }
shared_cache: { "operation": "put", "namespace": "molecule", "key": "ipnft_uid", "value": "<ipnft_uid>" }
shared_cache: { "operation": "put", "namespace": "molecule", "key": "ipnft_symbol", "value": "<symbol>" }
shared_cache: { "operation": "put", "namespace": "molecule", "key": "metadata_cid", "value": "<metadataCid>" }
```

## x402 Payment Flow (used by ALL paid mutations in Phases 3–6)

Every Molecule mutation below is paid per call in USDC on Base — no API key or service token. The MCP
runs the non-signing parts (fetch the `payment-required` challenge → build the EIP-712
`TransferWithAuthorization` → post the paid request) but **does NOT sign** — **your wallet signs the
EIP-712**. Three steps per paid call — this is the **"x402 paid call"** macro referenced throughout
Phases 3–6:

**1) Prepare** — the MCP fetches the challenge and builds the typed-data:

```
mcp__molecule__x402_prepare:
  mutation: <mutation_name>
  query: "<the GraphQL mutation — its single top-level field MUST equal `mutation`>"
  variables: { ... }
  walletAddress: <wallet_address>     # your operating address = the EIP-3009 `from`
```

Returns `{ prepared }`, where `prepared.typedData` is the EIP-712 to sign.

**2) Sign** — sign `prepared.typedData` with **your wallet** (EIP-712 / `eth_signTypedData_v4`).
**Privy:** remap the top-level `primaryType` → `primary_type` before calling Privy's wallet RPC (its
schema is snake_case). **Own key:** sign the standard typed-data as-is. See
[references/wallet-signing.md](references/wallet-signing.md). Keep the returned `signature`.

**3) Submit** — the MCP posts the signed payment (it does not sign):

```
mcp__molecule__x402_submit:
  prepared: <the prepared object from step 1, verbatim>
  signature: <the EIP-712 signature from step 2>
```

Returns `{ data, errors, settlement }`; read `data.<mutation_name>` and check `isSuccess` / `error`.

> **Shorthand:** where a phase below says *"x402 paid call: `<mutation>`"* with a `query` + `variables`,
> run all three steps (prepare → sign → submit) with those exact `mutation` / `query` / `variables`, and
> `walletAddress: <wallet_address>`.

**Required env vars (read by the MCP):** `X402_GATEWAY_URL`. Step 2's signature is produced by **your**
wallet (Privy or your own key); the MCP holds no wallet credentials.

### GraphQL surface (V2, keyed on `ipnftUid`)

Every paid step in Phases 3–6 uses the **V2** mutations keyed on `ipnftUid` (`{contractAddress}_{tokenId}`):
`createProject`, `initiateCreateOrUpdateFileV2`, `finishCreateOrUpdateFileV2`, `createAnnouncementV2`,
`addProjectOwner`. These are exactly the mutations whitelisted on the x402 gateway
(`desci-infra/lambda/x402-gateway-lambda/mutations.ts`), on both staging and production. The retired OCL
surface (`oclId`, `initiateCreateOrUpdateFile`/`finishCreateOrUpdateFile`/`createAnnouncement`/`createLab`)
is **not** used — it is not on production.

If `x402_prepare` reports a mutation is not enabled / not whitelisted (HTTP 400 "not enabled for x402
gateway", "No x402 challenge"), the gateway is misconfigured for this environment — surface the error and
stop; do **not** improvise a different surface. A response that arrives but reports `isSuccess: false` is
a real business error — surface it.

---

## Service Token (off-chain JWT — bind it to the wallet that owns IP-NFT access)

`MOLECULE_SERVICE_TOKEN` is an **off-chain JWT** (issued by Labs `generateServiceToken`; *never* minted
on-chain). It authenticates the **direct, non-x402** DEK calls — `labs_generate_dek` / `labs_decrypt_dek`
with `auth: service-token` — via the `x-service-token` header. **It is used only by the Phase 4 Private /
Encrypted variant and Phase 6 owner-decrypt; public uploads never touch it.**

**What the token is bound to (why the wallet matters).** The JWT payload carries an `adminAddress` — the
single wallet the token represents (`token-manager-service.ts` `generateServiceToken`). On `decryptDataKey`
the backend substitutes that `adminAddress` for the `:userAddress` placeholder in the on-chain access
condition `isAuthorizedSignerForIpnft(:userAddress, <reservationId>)` (see Phase 4 Step E6). So a service
token only unlocks a file if **the wallet it is bound to owns — or is a recursive Safe / Ownable /
ERC-6551 signer of — that IP-NFT.** Bind the token to whichever wallet is the IP-NFT's authorized signer
at the moment of the call; a token bound to the wrong wallet authenticates fine but fails `ACCESS_DENIED`
on decrypt.

**How issuance works — the MCP prepares, YOUR wallet signs, the MCP exchanges (no x402, no on-chain tx):**

1. **Prepare the sign-in message** (the MCP runs `getServiceSignInMessage`):
   ```
   mcp__molecule__service_signin_message:
     walletAddress: <the wallet to bind the token to>   # e.g. your wallet_address
     serviceName: data-sync-service
   ```
   Returns `{ message }`.
2. **Sign it with that wallet** — EIP-191 `personal_sign` of the **exact** `message`. **Privy**
   (recommended): `personal_sign` via the Privy wallet RPC. **Own key:** sign the message. See
   [references/wallet-signing.md](references/wallet-signing.md). The signing wallet MUST be the one named
   in `walletAddress` — that address becomes the token's `adminAddress`.
3. **Exchange the signature for the token** (the MCP runs `generateServiceToken`, which `verifyMessage`s
   the signature against `walletAddress` and sets `adminAddress = walletAddress`):
   ```
   mcp__molecule__service_token_create:
     walletAddress: <same address as step 1>
     messageSignature: <signature from step 2>
     serviceName: data-sync-service
     expiresIn: "720h"
   ```
   Returns `{ token, tokenId, expiresAt }`.

Because step 2 is a standard ECDSA message signature, **a Privy agentic wallet and a raw key are
interchangeable to the backend** — the only difference is which key signs. Set the returned `token` as
`MOLECULE_SERVICE_TOKEN` in `.claude/settings.local.json` (it is a secret — the MCP never logs it, and
neither should you), or pass it per-call via the `serviceToken` override on `labs_decrypt_dek` when you
need a token bound to a *different* wallet than the env default.

**Selection rule (apply this everywhere a service token is needed):**
1. Identify the wallet that must be the IP-NFT's authorized signer for this call (the minter/owner, or a
   recursive Safe/Ownable/TBA signer of it).
2. Run `service_signin_message` → **sign with that wallet** (Privy agentic wallet first option; or its own
   key) → `service_token_create`, all using that wallet's `walletAddress`.
3. Prefer a pre-set `MOLECULE_SERVICE_TOKEN` already bound to the right wallet over minting per run; only
   mint when it is missing/expired, or when you need a token bound to a different wallet than the env one.

---

## Phase 3: Create Molecule Project (via x402)

**Wait 90 seconds** after minting — on-chain ownership needs time to propagate to the AccessResolver:
```
Bash: sleep 90
```

Retrieve `reservationId` from cache if not in context:
```
shared_cache: { "operation": "get", "namespace": "molecule", "key": "reservation_id" }
```

**x402 paid call** — run prepare → **sign with your wallet** → submit (see *x402 Payment Flow*):
```
mcp__molecule__x402_prepare:
  mutation: createProject
  query: "mutation CreateProject($input: CreateProjectInput!) { createProject(input: $input) { isSuccess message error { message code retryable } project { ipnftUid ipnftSymbol ipnftAddress ipnftTokenId } } }"
  variables: { "input": { "ipnftSymbol": "<symbol>", "ipnftTokenId": "<reservationId as decimal string>" } }
  walletAddress: <wallet_address>
```
→ sign `prepared.typedData` with your wallet → `mcp__molecule__x402_submit: { prepared: <prepared>, signature: <signature> }`.

From `data.createProject.project` extract `ipnftUid` — every subsequent data-room call is keyed on it.

Extract project URL: `$MOLECULE_CLIENT_URL/ipnfts/{reservationId}`. Cache:
```
shared_cache: { "operation": "put", "namespace": "molecule", "key": "project_url", "value": "<project_url>" }
```

## Phase 4: Upload File to Data Room

By default a file is uploaded **PUBLIC** via Steps A–C. If the file must be **private / confidential** (encrypted at rest, access-controlled), use the **Private / Encrypted Upload** variant (Steps E0–E6) at the end of this phase *instead of* Steps A–C. Choose ONE path per file; do not run both.

**The path choice is irreversible mid-flight.** If you started the Private / Encrypted variant for this file, you may NEVER switch to Steps A–C for it. A failure anywhere in E0–E6 means **abort and report** — see the FAIL CLOSED rule above. The public path is only valid for files that were public from the start, never as a fallback for a failed confidential upload.

**Wait 90 seconds** after project creation — data room provisioning is async:
```
Bash: sleep 90
```

Get the file size (use the `bytes` field — this replaces `wc -c`):
```
mcp__molecule__sha256_file:
  filePath: <path-to-pdf>
```

### Step A — Initiate upload (x402 paid)

**x402 paid call** — run prepare → **sign with your wallet** → submit (see *x402 Payment Flow*):
```
mcp__molecule__x402_prepare:
  mutation: initiateCreateOrUpdateFileV2
  query: "mutation InitiateCreateOrUpdateFileV2($ipnftUid: String!, $contentType: String!, $contentLength: Int!) { initiateCreateOrUpdateFileV2(ipnftUid: $ipnftUid, contentType: $contentType, contentLength: $contentLength) { uploadToken uploadUrl uploadUrlExpiry method headers { key value } useMultipart isSuccess error { message code retryable } } }"
  variables: { "ipnftUid": "<ipnft_uid>", "contentType": "application/pdf", "contentLength": <bytes from sha256_file> }
  walletAddress: <wallet_address>
```
→ sign `prepared.typedData` with your wallet → `mcp__molecule__x402_submit: { prepared: <prepared>, signature: <signature> }`.

From `data.initiateCreateOrUpdateFileV2` extract: `uploadToken`, `uploadUrl`, `method`, `headers`.

### Step B — Upload to S3 (direct, NO x402 payment)

Convert the `headers` array (`[{key,value}, …]`) to a `{ key: value }` map. Use the EXACT `uploadUrl` and ALL `headers` from Step A:
```
mcp__molecule__s3_upload:
  uploadUrl: <uploadUrl from step A>
  filePath: <path-to-file>
  method: <method from step A, usually PUT>
  contentType: application/pdf
  headers: { <all key:value pairs from step A headers> }
```

### Step C — Finalize upload (x402 paid)

**Categories and tags** (REQUIRED — pick exactly one category and one or more correlated tags from the lists below; do NOT invent values). **Casing matters:** send the **category in lowercase** and each **tag in Title-Case** (e.g. `science` + `Discovery`). A wrong-cased finalize is rejected by the backend *after* the x402 payment has already settled, so a casing mistake makes you pay 2–3× for one upload — get it right on the first call.

Allowed categories (send lowercase):
```
['science', 'business', 'governance', 'media']
```

Correlated tags (each tag belongs to exactly one category — only pick tags whose category matches the chosen category):
```
business:
  'Ecosystem Partnership',
  'Funding',
  'University Partnership',
  'Important Meeting',
  'Market Opportunity',
  'Regulatory filing',
  'Biotech Partnership'
governance:
  'Proposal Failed',
  'Proposal Approved',
  'Proposal Open for Feedback'
media:
  'Promotional material',
  'Blog',
  'News coverage',
  'Academic article',
  'Pitch deck'
science:
  'Discovery',
  'Clinical Trial',
  'Provisional Patent Application',
  'Validation',
  'Milestone Achieved',
  'Manufacturing',
  'Lab Life',
  'In vivo data',
  'Patent licensed',
  'Non-Provisional Patent Application',
  'Optimization',
  'Patent granted'
```

Derive the category and tags from the research document content. For a typical research-PDF upload, default to category `science` (lowercase) with tag(s) like `Discovery` or `Validation` unless the document clearly fits another category.

**x402 paid call** — run prepare → **sign with your wallet** → submit (see *x402 Payment Flow*):
```
mcp__molecule__x402_prepare:
  mutation: finishCreateOrUpdateFileV2
  query: "mutation FinishCreateOrUpdateFileV2($ipnftUid: String!, $uploadToken: String!, $path: String, $accessLevel: String!, $changeBy: String!, $description: String, $tags: [String!], $categories: [String!]) { finishCreateOrUpdateFileV2(ipnftUid: $ipnftUid, uploadToken: $uploadToken, path: $path, accessLevel: $accessLevel, changeBy: $changeBy, description: $description, tags: $tags, categories: $categories) { datasetId contentHash version newHead isSuccess message error { message code retryable } } }"
  variables: { "ipnftUid": "<ipnft_uid>", "uploadToken": "<from step A>", "path": "<filename>", "accessLevel": "PUBLIC", "changeBy": "<wallet_address>", "description": "<file description>", "categories": ["<one of: science | business | governance | media>"], "tags": ["<one or more correlated tags from the list above>"] }
  walletAddress: <wallet_address>
```
→ sign `prepared.typedData` with your wallet → `mcp__molecule__x402_submit: { prepared: <prepared>, signature: <signature> }`.

From `data.finishCreateOrUpdateFileV2` extract: `datasetId` (format: `did:odf:...`), `contentHash`. Cache:
```
shared_cache: { "operation": "put", "namespace": "molecule", "key": "dataset_id", "value": "<datasetId>" }
```

---

## Phase 4 (Private variant): Encrypted Upload to Data Room (Steps E0–E6)

Use this **instead of** Steps A–C when the file must be confidential. It is a faithful client-side replication of Labs **Onchain-Verified Envelope Encryption** (`encryptFileWithKms`) — same algorithm, IV size, tag handling, and `contentHash` rule (handled by `mcp__molecule__encrypt_file`). The backend never sees plaintext or the unwrapped key; it only stores the ciphertext, the KMS-wrapped DEK, and the on-chain access conditions.

**Preconditions & invariants:**
- The DEK is generated by `mcp__molecule__labs_generate_dek` with `auth: service-token` (needs `MOLECULE_SERVICE_TOKEN` + the operating wallet's address). This is a **direct service-token call — no wallet signature, no payment** — so the plaintext DEK stays in-process. The service token here must be **bound to the operating wallet** — the wallet that minted and (until any Phase 6 transfer) owns this IP-NFT, i.e. its authorized signer. If it is missing/expired, mint a fresh one bound to that wallet via the **Service Token** flow above — `service_signin_message` → **sign with your wallet** (Privy agentic wallet first option, or its own key) → `service_token_create`:
    ```
    mcp__molecule__service_signin_message:
      walletAddress: <wallet_address>
      serviceName: data-sync-service
    ```
    → sign the returned `message` with your wallet → `mcp__molecule__service_token_create: { walletAddress: <wallet_address>, messageSignature: <signature>, serviceName: data-sync-service, expiresIn: "720h" }`. Set the returned `token` as `MOLECULE_SERVICE_TOKEN` in `.claude/settings.local.json` (secret — the MCP never logs it, and neither should you).
- `accessLevel` MUST be `ADMIN` (or `HOLDERS`) — valid values are `PUBLIC | HOLDERS | ADMIN`. Never `PUBLIC` for a confidential file.
- **Production guard:** the backend verifies the caller is an authorized signer for the IP-NFT (`isAuthorizedSignerForIpnft`) on the configured `AccessResolver` chain before it will finalize an encrypted file. If the resolver is unreachable / not deployed on that chain, Step E5 fails with a clear error — surface that message verbatim and stop.
- The plaintext DEK is **one-shot and secret** and **never leaves the MCP** — `labs_generate_dek` hands back only a `dekHandle`. Only `encryptedDek` (wrapped) and the ciphertext are persisted.
- Crypto matches the Labs client exactly via the MCP: **AES-256-GCM**, **random 12-byte IV**, **128-bit (16-byte) auth tag appended to the ciphertext**, `contentHash` = **hex SHA-256 of the _plaintext_**, DEK = base64 raw 32 bytes (AES-256), `iv` reported base64.

### Step E0 — Generate the data encryption key (direct, service-token)

```
mcp__molecule__labs_generate_dek:
  auth: service-token
```
(Uses `MOLECULE_SERVICE_TOKEN` + `EVM_WALLET_ADDRESS`; no wallet signature, no payment.) Returns `encryptedDek` (base64), `encryptionSystem` (e.g. `"kms"` — echo it verbatim, never hardcode), and `dekHandle`. **The plaintext DEK is not returned** — it stays in the MCP, addressed by `dekHandle`. Cache the `dekHandle` if you need it later in this run.

### Step E1 — Encrypt the file (replicates `encryptFileWithKms`)

```
mcp__molecule__encrypt_file:
  filePath: <path-to-pdf>
  dekHandle: <from E0>
  outPath: mint/encrypted/<filename>.enc
```

From the result save `iv` (base64), `contentHash` (hex), and `cipherBytes`. The output file `mint/encrypted/<filename>.enc` is `ciphertext‖tag` — exactly the byte layout the Labs reader (`decryptFileWithKms`) expects, so it is what you upload.

### Step E2 — Initiate upload with the **ciphertext** size (x402 paid)

Identical to public Step A, except `contentLength` MUST be the ciphertext size (`cipherBytes` from E1). **x402 paid call** — prepare → **sign with your wallet** → submit:
```
mcp__molecule__x402_prepare:
  mutation: initiateCreateOrUpdateFileV2
  query: "mutation InitiateCreateOrUpdateFileV2($ipnftUid: String!, $contentType: String!, $contentLength: Int!) { initiateCreateOrUpdateFileV2(ipnftUid: $ipnftUid, contentType: $contentType, contentLength: $contentLength) { uploadToken uploadUrl uploadUrlExpiry method headers { key value } useMultipart isSuccess error { message code retryable } } }"
  variables: { "ipnftUid": "<ipnft_uid>", "contentType": "application/pdf", "contentLength": <cipherBytes> }
  walletAddress: <wallet_address>
```
→ sign `prepared.typedData` with your wallet → `mcp__molecule__x402_submit: { prepared: <prepared>, signature: <signature> }`. Extract `uploadToken`, `uploadUrl`, `method`, `headers`.

### Step E3 — PUT the ciphertext to S3 (direct, NO x402)

Upload the **encrypted** file, not the original:
```
mcp__molecule__s3_upload:
  uploadUrl: <uploadUrl from E2>
  filePath: mint/encrypted/<filename>.enc
  method: <method from E2, usually PUT>
  contentType: application/pdf
  headers: { <all key:value pairs from E2 headers> }
```

### Step E4 — Build `accessControlConditions` (authorized IP-NFT signer)

Replicates `createAuthorizedIpnftSignerCondition` — gates decryption on `AccessResolver.isAuthorizedSignerForIpnft(:userAddress, <reservationId>)` so the IP-NFT owner and any recursive (Safe / Ownable / ERC-6551 TBA) signer can decrypt. The chain string is derived from `$CHAIN_ID` (`1`→`ethereum`, `11155111`→`sepolia`, `8453`→`base`, `84532`→`baseSepolia`).

```
mcp__molecule__build_access_conditions:
  mode: ipnft-signer
  reservationId: "<reservationId>"
```
`:userAddress` is a literal placeholder the backend evaluator substitutes — the tool keeps it verbatim. Use the returned **`json`** string as `encryptionMetadata.accessControlConditions` in E5.

### Step E5 — Finalize the encrypted upload (x402 paid)

Same category/tag rules as the public Step C (pick exactly one category + correlated tag(s) — lowercase category, Title-Case tag — default `science` / `Discovery`). The new piece is `encryptionMetadata` (`EncryptionMetadataInput`) and the non-PUBLIC `accessLevel`. `encryptionMetadata.accessControlConditions` is the E4 **`json`** string. Generate `encryptedAt` as an ISO-8601 UTC timestamp:
```
Bash: date -u +%Y-%m-%dT%H:%M:%SZ
```

| Field | Value |
|-------|-------|
| `encryptionSystem` | echo from E0 (e.g. `kms`) — never hardcode |
| `accessControlConditions` | the E4 `json` string |
| `encryptedBy` | `<wallet_address>` |
| `encryptedAt` | ISO-8601 UTC timestamp |
| `encryptedDek` | `encryptedDek` from E0 (base64, wrapped) |
| `iv` | `iv` from E1 (base64) |
| `contentHash` | `contentHash` from E1 (hex SHA-256 of plaintext) |

**x402 paid call** — prepare → **sign with your wallet** → submit:
```
mcp__molecule__x402_prepare:
  mutation: finishCreateOrUpdateFileV2
  query: "mutation FinishCreateOrUpdateFileV2($ipnftUid: String!, $uploadToken: String!, $path: String, $accessLevel: String!, $changeBy: String!, $description: String, $tags: [String!], $categories: [String!], $encryptionMetadata: EncryptionMetadataInput) { finishCreateOrUpdateFileV2(ipnftUid: $ipnftUid, uploadToken: $uploadToken, path: $path, accessLevel: $accessLevel, changeBy: $changeBy, description: $description, tags: $tags, categories: $categories, encryptionMetadata: $encryptionMetadata) { datasetId contentHash version newHead isSuccess message error { message code retryable } } }"
  variables: { "ipnftUid": "<ipnft_uid>", "uploadToken": "<from E2>", "path": "<filename>", "accessLevel": "ADMIN", "changeBy": "<wallet_address>", "description": "<file description>", "categories": ["<one of: science | business | governance | media>"], "tags": ["<one or more correlated tags>"], "encryptionMetadata": { "encryptionSystem": "<from E0>", "accessControlConditions": "<E4 json string>", "encryptedBy": "<wallet_address>", "encryptedAt": "<ISO-8601 UTC>", "encryptedDek": "<from E0>", "iv": "<from E1>", "contentHash": "<from E1>" } }
  walletAddress: <wallet_address>
```
→ sign `prepared.typedData` with your wallet → `mcp__molecule__x402_submit: { prepared: <prepared>, signature: <signature> }`.

From `data.finishCreateOrUpdateFileV2` extract `datasetId` (`did:odf:...`) and `contentHash`, then cache:
```
shared_cache: { "operation": "put", "namespace": "molecule", "key": "dataset_id", "value": "<datasetId>" }
```

### Step E6 (optional) — Verify decryption (replicates `decryptFileWithKms`)

The `decryptDataKey` mutation (`encryption.graphql`) accepts `ipnftUid`+`filePath` (a data-room file) or `tokenUri`+`agreementUrl` (an IPFS agreement). For the V2 data-room file uploaded above, pass the `ipnftUid` + the stored data-room `path`.

To confirm an authorized caller can recover the file, fetch the DEK (the plaintext stays in the MCP) and decrypt locally:
```
mcp__molecule__labs_decrypt_dek:
  ipnftUid: "<ipnft_uid>"
  filePath: "<filename / data-room path from E5>"
  auth: service-token
```
Returns `iv` and a fresh `dekHandle` on success. A `LEGACY_ENCRYPTION` message means the file predates the envelope flow; `ACCESS_DENIED` means the decrypt caller does not satisfy the on-chain `isAuthorizedSignerForIpnft` condition.

**IMPORTANT — the decrypt caller is NOT the `x-wallet-address` header.** When a service token is present (it always is here), the backend substitutes the **service token's `adminAddress`** for `:userAddress` (`appsync-resolver-labs-lambda/index.ts` `case "decryptDataKey"` → `serviceContext.adminAddress`; evaluated by `services/condition-evaluator.ts`). So to decrypt *as* a given wallet you must present a `MOLECULE_SERVICE_TOKEN` **bound to that wallet** — mint one via `service_signin_message` → **sign with that wallet** (Privy agentic wallet first option, or its own key) → `service_token_create`, all using that wallet's `walletAddress` (see **Service Token** for the selection rule). Pass it via the per-call `serviceToken` override:
```
mcp__molecule__labs_decrypt_dek:
  ipnftUid: "<ipnft_uid>"
  filePath: "<filename / data-room path from E5>"
  serviceToken: "<token bound to the wallet you want to decrypt as>"
```
That wallet must be the IP-NFT owner or an authorized signer on the configured resolver.

```
mcp__molecule__decrypt_file:
  filePath: mint/encrypted/<filename>.enc
  iv: <iv from labs_decrypt_dek>
  dekHandle: <from labs_decrypt_dek>
  outPath: mint/decrypted-check.bin
```
The returned `plaintextSha256` MUST equal the `contentHash` from E1 — that confirms the round trip.

## Phase 5: Create Announcement (via x402)

**x402 paid call** — run prepare → **sign with your wallet** → submit (see *x402 Payment Flow*):
```
mcp__molecule__x402_prepare:
  mutation: createAnnouncementV2
  query: "mutation CreateAnnouncementV2($ipnftUid: String!, $headline: String!, $body: String!, $attachments: [String!]) { createAnnouncementV2(ipnftUid: $ipnftUid, headline: $headline, body: $body, attachments: $attachments) { isSuccess message error { message code retryable } } }"
  variables: { "ipnftUid": "<ipnft_uid>", "headline": "<title>", "body": "<markdown body>", "attachments": ["<datasetId from upload>"] }
  walletAddress: <wallet_address>
```
→ sign `prepared.typedData` with your wallet → `mcp__molecule__x402_submit: { prepared: <prepared>, signature: <signature> }`.

### External Posting Copy Rules (Phase 5 body + any Beach.science post)

When composing any user-facing markdown that describes the registration (the `body` field above, or a Beach.science post body), obey the rules below. The active chain id for this run is **$CHAIN_ID** (resolved from env); use it directly wherever a chain id is needed.

- **Project URL:** use `$MOLECULE_CLIENT_URL/ipnfts/{reservationId}` verbatim — never substitute `testnet.molecule.xyz`, `staging.molecule.xyz`, or any other domain.
- **Chain name:** if the active chain id is `1`, call it "Ethereum mainnet". If it is `11155111`, call it "Sepolia". For any other chain id, name it explicitly (e.g. "Base mainnet (8453)"). Do NOT label the registration as "Sepolia staging", "testnet", or "staging" when the active chain id is `1`.
- **TX explorer links:** chain id `1` → `https://etherscan.io/tx/<hash>`; chain id `11155111` → `https://sepolia.etherscan.io/tx/<hash>`; chain id `8453` → `https://basescan.org/tx/<hash>`.
- **Update slugs:** any `/updates/<slug>` link MUST be lowercase, hyphen-separated, and have NO file extension. Example: `/updates/kiss1r-pipeline-update-gen2` — NOT `/updates/KISS1R_Pipeline_Update_Gen2.md`, `/updates/KISS1R_Pipeline_Update_Gen2`, or `/updates/kiss1r-pipeline-update-gen2.md`. Lowercase the title, replace spaces and underscores with hyphens, and drop any trailing `.md`/`.html`.
- Do not invent URLs, symbols, or transaction hashes — use the values actually saved to `shared_cache` during this run.

## Phase 6: NFT Transfer and Co-Ownership

Optionally transfer the minted IP-NFT to a different long-term owner wallet and add them as a project
co-owner.

**Skip this phase entirely** unless the user wants the IP-NFT transferred to a *different* wallet than the
operating wallet that minted it. (`EVM_WALLET_ADDRESS` is now the **operating/signer** address, not the
recipient — do NOT treat it as the transfer target.)

### Step A — Determine the recipient

Use the recipient ("long-term owner") wallet the user supplied for this run as `owner_wallet`. If none was
provided, or it equals `wallet_address` (the operating wallet), **skip to Output** — no transfer needed.

### Step B — ABI-encode ERC-721 transfer

```
mcp__molecule__abi_encode:
  functionSignature: "safeTransferFrom(address,address,uint256)"
  args:
    - <wallet_address>
    - <owner_wallet>
    - <token_id as decimal string>
```

Save `calldata`.

### Step C — Transfer IP-NFT on-chain (sign & send with your wallet)

Prepare the transfer transaction, then sign + broadcast it with **your wallet**:

```
mcp__molecule__prepare_transaction:
  to: $IPNFT_CONTRACT_ADDRESS
  data: <calldata from step B>
  chainId: $CHAIN_ID
```

> **Send it sign-only + self-broadcast (raw), not via a managed send.** For `safeTransferFrom`, **Privy's**
> managed `eth_sendTransaction` returns a hash but never broadcasts it (the "phantom hash") — even though
> mint and POI broadcast fine. So with a **Privy** wallet, use `eth_signTransaction` (sign only) then
> broadcast the raw tx yourself via `eth_sendRawTransaction`, resolving the live `pending` nonce (re-runnable
> after a stuck attempt). With your **own key**, you already sign + `eth_sendRawTransaction`. See
> [references/wallet-signing.md](references/wallet-signing.md) §C.

Save the resulting `txHash` as `transfer_tx_hash`.

### Step D — Add owner as project co-owner (via x402)

`addProjectOwner` takes `ipnftUid` + `ownerAddress` (per `graphql/schemas/ip-hubs.graphql` and `bruno/desci-labs/v2/2-addProjectOwner.bru`). **x402 paid call** — prepare → **sign with your wallet** → submit:

```
mcp__molecule__x402_prepare:
  mutation: addProjectOwner
  query: "mutation AddProjectOwner($ipnftUid: String!, $ownerAddress: String!) { addProjectOwner(ipnftUid: $ipnftUid, ownerAddress: $ownerAddress) { isSuccess message error { message code retryable } } }"
  variables: { "ipnftUid": "<ipnft_uid>", "ownerAddress": "<owner_wallet>" }
  walletAddress: <wallet_address>
```
→ sign `prepared.typedData` with your wallet → `mcp__molecule__x402_submit: { prepared: <prepared>, signature: <signature> }`.

Note the Step C `safeTransferFrom` already moved the IP-NFT (and thus owner role) on-chain; `addProjectOwner` additionally whitelists `<owner_wallet>` in the project's off-chain owner list. (The x402 payment in this step is still authorized by your **operating** wallet — `walletAddress: <wallet_address>` — even though the IP-NFT now belongs to `owner_wallet`.)

### Step E — Owner decrypt access (private / encrypted uploads only)

Skip for public uploads. For a **private** upload (Phase 4 Private variant), the owner must be able to decrypt — and **project membership alone does NOT grant decryption**: the off-chain owner list from Step D is **not** consulted by the decrypt condition-evaluator. Decryption is gated by `isAuthorizedSignerForIpnft(:userAddress, <reservationId>)` evaluated against the caller's **service-token `adminAddress`** (see Phase 4 Step E6). So ensure the owner satisfies that condition by either:

- the Step C `safeTransferFrom` above — once the owner holds the IP-NFT they ARE the authorized signer (the common path); **or**
- if the IP-NFT was not transferred to them, make the file's `encryptionMetadata.accessControlConditions` an **OR** that also authorizes the owner (Lit unified format `[cond, {"operator":"or"}, cond]`, e.g. OR a second `isAuthorizedSignerForIpnft(:userAddress, <a tokenId the owner owns>)`). The evaluator supports boolean operators but only contract-call conditions (no bare address-equality), so the owner must be an authorized signer of *some* IP-NFT.

The owner then decrypts by presenting a service token **bound to the owner's wallet** (no env swap needed — use the per-call `serviceToken` override on `labs_decrypt_dek`, as in Step E6). Mint that token with the owner's wallet doing the signing: `service_signin_message: { walletAddress: <owner_wallet> }` → **owner signs the message** with their wallet (Privy agentic wallet first option, or its own key) → `service_token_create: { walletAddress: <owner_wallet>, messageSignature: <signature> }`.

Either way the token's `adminAddress` must be the owner's address — that is what the decrypt evaluator substitutes into `isAuthorizedSignerForIpnft`.

## Output

Final results to report:
- `ipnft_uid`: `{contract_address}_{token_id}`
- `poi_tx_hash`
- `mint_tx_hash`
- `project_url`: `$MOLECULE_CLIENT_URL/ipnfts/{ipnftTokenId}`
- `datasetId` from upload
- Announcement success status
- `transfer_tx_hash` (if transfer was performed)
- Co-owner addition status (if transfer was performed)
