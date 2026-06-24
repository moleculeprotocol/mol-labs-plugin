# Wallet & signing — how to sign the molecule's prepared payloads

The `molecule` MCP server is **custody-free**: it crafts transactions, EIP-712 typed-data, and sign-in
messages, but it **never holds a key, signs, or broadcasts**. Every wallet-dependent step hands you a
*prepared payload*; **you** sign/send it with your own wallet and feed the `signature` / `txHash` back in.

This file shows how to do that with a **Privy agentic wallet (the recommended first option)** and with
**your own key**. Pick whichever you run; the molecule flow is identical either way.

> The MCP only ever needs your wallet's **public address** (`EVM_WALLET_ADDRESS`, or the `walletAddress`
> argument). **Never** put a private key in an MCP tool argument.

There are exactly three handoff shapes (see §C): a **transaction** (`prepare_transaction` → you send), an
**x402 payment** (`x402_prepare` → you sign EIP-712 → `x402_submit`), and a **service-token sign-in**
(`service_signin_message` → you personal_sign → `service_token_create`).

---

## §A — Privy agentic wallet (recommended)

A Privy *server wallet* signs server-side with no user interaction — ideal for autonomous agents. Provision
one (policy + wallet) with the **`privy-agentic-wallets`** skill, then drive it over the Privy REST API
(`POST https://api.privy.io/v1/wallets/{WALLET_ID}/rpc`, HTTP basic auth `PRIVY_APP_ID:PRIVY_APP_SECRET`,
header `privy-app-id: $PRIVY_APP_ID`). Docs: https://docs.privy.io/guide/server-wallets

**Get the wallet address** (for `EVM_WALLET_ADDRESS` / `walletAddress`):
`GET /v1/wallets/{WALLET_ID}` → `.address`.

**personal_sign** (terms message, service-token sign-in):
```jsonc
// POST /v1/wallets/{WALLET_ID}/rpc
{ "method": "personal_sign", "params": { "message": "<message>", "encoding": "utf-8" } }
// -> data.signature
```

**EIP-712 sign typed data** (x402 `prepared.typedData`). IMPORTANT: Privy's RPC schema is snake_case —
rename the top-level `primaryType` to `primary_type` before sending:
```jsonc
// take prepared.typedData, then:  td.primary_type = td.primaryType; delete td.primaryType
// POST /v1/wallets/{WALLET_ID}/rpc
{ "method": "eth_signTypedData_v4", "params": { "typed_data": { /* ...td with primary_type... */ } } }
// -> data.signature        (pass this to x402_submit)
```

**Send a transaction** (POI anchor, mint — `prepare_transaction.transaction`):
```jsonc
// POST /v1/wallets/{WALLET_ID}/rpc
{ "method": "eth_sendTransaction",
  "caip2": "eip155:<chainId>",
  "params": { "transaction": { "to": "<to>", "data": "<data>", "value": "<valueWei 0x…>" } } }
// -> data.hash   (your txHash)
```

**Sign-only + self-broadcast** (use for the IP-NFT `safeTransferFrom` transfer — see §C phantom-hash note):
```jsonc
// 1) POST /v1/wallets/{WALLET_ID}/rpc
{ "method": "eth_signTransaction",
  "params": { "transaction": { "to": "<to>", "data": "<data>", "value": "0x0",
              "chain_id": <chainId>, "nonce": <pending nonce>, "type": 2,
              "gas_limit": "0x…", "max_fee_per_gas": "0x…", "max_priority_fee_per_gas": "0x…" } } }
// -> data.signed_transaction
// 2) broadcast it yourself: JSON-RPC eth_sendRawTransaction(signed_transaction) against your EVM node
```

Privy policies (spending caps, chain allowlists) still apply to every signature — keep them on. Full REST
details live in the `privy-agentic-wallets` skill (`references/transactions.md`, `references/policies.md`).

---

## §B — Bring your own key (any EOA / signer)

Use any wallet/library you control. The key stays with you; the MCP never sees it. Examples sign the same
three payload shapes.

**viem** (https://viem.sh):
```ts
import { createWalletClient, http } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { baseSepolia } from "viem/chains";

const account = privateKeyToAccount(process.env.WALLET_PRIVATE_KEY as `0x${string}`);
const client = createWalletClient({ account, chain: baseSepolia, transport: http(RPC_URL) });

// account.address  -> EVM_WALLET_ADDRESS / walletAddress
const sig       = await account.signMessage({ message: prepared_message });        // personal_sign
const x402sig   = await account.signTypedData(prepared.typedData);                 // EIP-712 (camelCase OK)
const txHash    = await client.sendTransaction({ to, data, value: BigInt(valueWei) }); // tx
```

**ethers v6** (https://docs.ethers.org):
```ts
import { Wallet, JsonRpcProvider } from "ethers";
const wallet = new Wallet(process.env.WALLET_PRIVATE_KEY!, new JsonRpcProvider(RPC_URL));
const sig     = await wallet.signMessage(prepared_message);                          // personal_sign
const x402sig = await wallet.signTypedData(td.domain, { TransferWithAuthorization: td.types.TransferWithAuthorization }, td.message);
const tx      = await wallet.sendTransaction({ to, data, value: BigInt(valueWei) }); // tx -> tx.hash
```

**Python — eth-account** (https://eth-account.readthedocs.io):
```python
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data

acct = Account.from_key(WALLET_PRIVATE_KEY)             # acct.address -> EVM_WALLET_ADDRESS
sig      = Account.sign_message(encode_defunct(text=message), WALLET_PRIVATE_KEY).signature.hex()
x402sig  = Account.sign_message(encode_typed_data(full_message=prepared["typedData"]), WALLET_PRIVATE_KEY).signature.hex()
signed   = acct.sign_transaction({ "to": to, "data": data, "value": int(valueWei, 16),
                                   "nonce": nonce, "chainId": chain_id, "type": 2,
                                   "gas": gas, "maxFeePerGas": fee, "maxPriorityFeePerGas": tip })
# broadcast signed.raw_transaction via eth_sendRawTransaction
```
(eth-account integer fields must be ints; for the x402 typed-data, coerce the `value`/`validAfter`/
`validBefore`/`nonce`-style string fields to ints as your library requires.)

---

## §C — The handoff contract (per prepared payload)

**1. Transaction** — `prepare_transaction(to, data?, value?, chainId?)` → `{ transaction }`.
Send `transaction` with your wallet (§A `eth_sendTransaction` / §B `sendTransaction`); record the `txHash`.

**2. x402 payment** — `x402_prepare(mutation, query, variables, walletAddress)` → `{ prepared }`.
Sign `prepared.typedData` (EIP-712) with your wallet — **Privy: remap `primaryType`→`primary_type`**; own
key: sign as-is. Then `x402_submit(prepared, signature)` → `{ data, errors, settlement }`. The EIP-3009
`from` inside `prepared.typedData` **must equal the signer** (your `walletAddress`).

**3. Service-token sign-in** — `service_signin_message(walletAddress, serviceName)` → `{ message }`.
`personal_sign` the exact `message` with the wallet named in `walletAddress` (that address becomes the
token's `adminAddress`). Then `service_token_create(walletAddress, messageSignature, …)` → `{ token }`.

**Phantom-hash note (IP-NFT transfer).** For `safeTransferFrom`, **Privy's** managed `eth_sendTransaction`
returns a hash but never broadcasts it — even though mint/POI broadcast fine. So for the Phase 6 transfer,
use **sign-only + self-broadcast** (§A `eth_signTransaction` → `eth_sendRawTransaction`), resolving the live
`pending` nonce so it is re-runnable. With your own key you already sign + `eth_sendRawTransaction`, so this
is a non-issue.
