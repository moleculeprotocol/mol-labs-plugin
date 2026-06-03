#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.2.0",
#   "httpx>=0.27",
#   "cryptography>=42",
#   "eth-abi>=5",
#   "eth-utils>=4",
#   "eth-hash[pycryptodome]>=0.5",
# ]
# ///
"""molecule-mcp — stdio MCP server for the Molecule DeSci skills.

Replaces every ``curl`` / ``http_request`` / ``node -e`` step in the
``aura-orchestrator`` and ``molecule-x402`` skills with a typed MCP tool, so the
agent calls one tool per operation instead of hand-assembling shell commands.

Written in Python (FastMCP) and run over stdio so it works under any
MCP-capable harness (Claude Code, Codex, …) with only a Python interpreter —
no Bun/Node required.

Source-of-truth parity (these tools faithfully replicate the real backend):
  - x402 payment flow ........ desci-infra/lambda/x402-gateway-lambda/index.ts
  - x402 mutation whitelist .. desci-infra/lambda/x402-gateway-lambda/mutations.ts
  - AES-256-GCM envelope ..... desci-ecosystem/packages/storage/src/lib/encryption/kms-envelope.ts
  - oclId packing ............ desci-infra/lambda/common/utils/ocl-id.ts
  - access conditions ........ desci-infra/lambda/common/utils/access-control-conditions.ts
  - GraphQL field shapes ..... desci-infra/graphql/schemas/{ip-hubs,encryption}.graphql
  - request shapes / auth .... desci-infra/bruno/{desci-labs,service-auth}

Transport: stdio. NOTHING is written to stdout except the JSON-RPC protocol —
FastMCP owns stdout; all diagnostics go to stderr (see ``log``). Secrets
(PRIVY_APP_SECRET, the plaintext DEK, service tokens, API keys) are never
logged or returned to the caller.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from eth_abi import encode as abi_encode_values
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from mcp.server.fastmcp import FastMCP

PRIVY_BASE_URL = "https://api.privy.io"
HTTP_TIMEOUT = 120.0

mcp = FastMCP("molecule")


# --------------------------------------------------------------------------
# diagnostics (stderr only) + errors
# --------------------------------------------------------------------------


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class ToolError(Exception):
    """Tool-level error whose message is safe to surface to the agent.

    FastMCP catches exceptions raised inside a tool and returns them as an
    error tool result (isError=True) with this message as the text.
    """


def dump(obj: Any) -> str:
    """Serialize a tool result as pretty JSON text (the agent-facing payload)."""
    return json.dumps(obj, indent=2, default=str)


# --------------------------------------------------------------------------
# env helpers
# --------------------------------------------------------------------------


def env(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v else None


def require_env(*names: str) -> dict[str, str]:
    out: dict[str, str] = {}
    missing: list[str] = []
    for n in names:
        v = env(n)
        if v:
            out[n] = v
        else:
            missing.append(n)
    if missing:
        raise ToolError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Set them in .claude/settings.json (non-secrets) or "
            ".claude/settings.local.json (secrets), then restart."
        )
    return out


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

_client = httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)


def _json_or_none(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


# --------------------------------------------------------------------------
# Privy
# --------------------------------------------------------------------------


def _privy_auth() -> tuple[tuple[str, str], dict[str, str]]:
    creds = require_env("PRIVY_APP_ID", "PRIVY_APP_SECRET")
    return (
        (creds["PRIVY_APP_ID"], creds["PRIVY_APP_SECRET"]),
        {"privy-app-id": creds["PRIVY_APP_ID"], "Content-Type": "application/json"},
    )


def privy_rpc(wallet_id: str, body: dict[str, Any]) -> Any:
    auth, headers = _privy_auth()
    resp = _client.post(
        f"{PRIVY_BASE_URL}/v1/wallets/{wallet_id}/rpc",
        auth=auth,
        headers=headers,
        content=json.dumps(body),
    )
    if resp.status_code >= 400:
        raise ToolError(f"Privy RPC failed ({resp.status_code}): {resp.text[:500]}")
    return _json_or_none(resp)


def resolve_wallet_id(explicit: str | None) -> str:
    wid = explicit or env("PRIVY_WALLET_ID")
    if not wid:
        raise ToolError(
            "No wallet id available. Pass walletId or set PRIVY_WALLET_ID. "
            "Use privy_list_wallets / privy_create_wallet to obtain one."
        )
    return wid


def get_wallet_address(wallet_id: str | None = None) -> str:
    from_env = env("EVM_WALLET_ADDRESS")
    if from_env:
        return from_env
    wid = resolve_wallet_id(wallet_id)
    auth, headers = _privy_auth()
    resp = _client.get(f"{PRIVY_BASE_URL}/v1/wallets/{wid}", auth=auth, headers=headers)
    j = _json_or_none(resp)
    if resp.status_code >= 400 or not (j and j.get("address")):
        raise ToolError(
            f"Could not resolve wallet address ({resp.status_code}): {resp.text[:300]}"
        )
    return j["address"]


# --------------------------------------------------------------------------
# ephemeral DEK store — the plaintext DEK never leaves this process
# --------------------------------------------------------------------------

_DEK_TTL_S = 60 * 60
_dek_store: dict[str, tuple[str, float]] = {}


def put_dek(plaintext_dek: str) -> str:
    handle = f"dek_{uuid.uuid4()}"
    _dek_store[handle] = (plaintext_dek, time.time())
    return handle


def get_dek(handle: str) -> str:
    entry = _dek_store.get(handle)
    if not entry:
        raise ToolError(
            f'Unknown or expired dekHandle "{handle}". DEK handles are single-process '
            "and live ~1h. Re-run labs_generate_dek / labs_decrypt_dek."
        )
    dek, created = entry
    if time.time() - created > _DEK_TTL_S:
        _dek_store.pop(handle, None)
        raise ToolError(f'dekHandle "{handle}" has expired. Re-generate it.')
    return dek


# --------------------------------------------------------------------------
# Labs GraphQL (direct)
# --------------------------------------------------------------------------

LabsAuth = Literal["service-token", "api-key", "none"]


def _labs_headers(auth: LabsAuth) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if auth == "service-token":
        creds = require_env("MOLECULE_SERVICE_TOKEN", "EVM_WALLET_ADDRESS")
        headers["x-service-token"] = creds["MOLECULE_SERVICE_TOKEN"]
        headers["x-wallet-address"] = creds["EVM_WALLET_ADDRESS"]
    elif auth == "api-key":
        creds = require_env("MOLECULE_API_KEY")
        headers["x-api-key"] = creds["MOLECULE_API_KEY"]
    return headers


def labs_graphql_call(
    query: str,
    variables: dict[str, Any],
    auth: LabsAuth,
    labs_url: str | None = None,
) -> dict[str, Any]:
    url = labs_url or env("MOLECULE_LABS_URL")
    if not url:
        raise ToolError("MOLECULE_LABS_URL is not set (and no labsUrl override given).")
    resp = _client.post(
        url,
        headers=_labs_headers(auth),
        content=json.dumps({"query": query, "variables": variables}),
    )
    j = _json_or_none(resp)
    if j is None:
        raise ToolError(
            f"Labs GraphQL returned non-JSON ({resp.status_code}): {resp.text[:500]}"
        )
    if resp.status_code >= 400 and j.get("data") is None:
        raise ToolError(f"Labs GraphQL HTTP {resp.status_code}: {resp.text[:500]}")
    return {"data": j.get("data"), "errors": j.get("errors")}


# --------------------------------------------------------------------------
# x402 payment flow (P1–P7) in one call. Mirrors x402-gateway-lambda exactly:
#   P1 send -> P2 decode payment-required -> P3 wallet -> P4 nonce/validity ->
#   P5 Privy EIP-712 sign -> P6 build+base64 header -> P7 retry PAYMENT-SIGNATURE
# --------------------------------------------------------------------------


def _decode_x402_challenge(header_value: str | None, body: Any) -> Any:
    if header_value:
        cleaned = header_value.strip().replace("\r", "")
        try:
            return json.loads(base64.b64decode(cleaned).decode("utf-8"))
        except Exception:
            try:
                return json.loads(cleaned)  # some servers send raw JSON
            except Exception:
                pass
    if isinstance(body, dict) and isinstance(body.get("accepts"), list):
        return body
    return None


def _chain_id_from_network(network: str) -> int:
    import re

    m = re.search(r"eip155:(\d+)", network)
    if m:
        return int(m.group(1))
    if network == "base":
        return 8453
    if network == "base-sepolia":
        return 84532
    raise ToolError(f'Cannot derive chainId from network "{network}".')


def run_x402_pay(
    mutation: str,
    query: str,
    variables: dict[str, Any] | None,
    gateway_url: str | None,
    wallet_id: str | None,
) -> dict[str, Any]:
    gateway = gateway_url or env("X402_GATEWAY_URL")
    if not gateway:
        raise ToolError("X402_GATEWAY_URL is not set.")
    wid = resolve_wallet_id(wallet_id)
    endpoint = f"{gateway.rstrip('/')}/x402/labs/{mutation}"
    body_str = json.dumps({"query": query, "variables": variables or {}})

    # P1 — send unpaid request, capture the 402 challenge.
    challenge_res = _client.post(
        endpoint, headers={"Content-Type": "application/json"}, content=body_str
    )
    challenge_body = _json_or_none(challenge_res)

    # P2 — decode payment requirements.
    challenge = _decode_x402_challenge(
        challenge_res.headers.get("payment-required"), challenge_body
    )
    if not challenge or not isinstance(challenge.get("accepts"), list) or not challenge["accepts"]:
        raise ToolError(
            f'No x402 challenge for "{mutation}" (HTTP {challenge_res.status_code}). '
            "The mutation may not be whitelisted on this gateway, or the request "
            f"errored. Body: {challenge_res.text[:400]}"
        )
    accepted = challenge["accepts"][0]
    resource = challenge.get("resource") or accepted.get("resource")
    network = accepted["network"]
    amount = str(accepted.get("amount") or accepted.get("maxAmountRequired") or accepted.get("value") or "")
    asset = accepted.get("asset")
    pay_to = accepted.get("payTo")
    max_timeout = int(accepted.get("maxTimeoutSeconds") or 300)
    extra = accepted.get("extra") or {}
    if not amount or not asset or not pay_to:
        raise ToolError(f'x402 challenge for "{mutation}" is missing amount/asset/payTo.')

    # P3 — wallet address.
    wallet_address = get_wallet_address(wid)

    # P4 — nonce, validAfter, validBefore.
    now = int(time.time())
    nonce = "0x" + secrets.token_hex(32)
    valid_after = str(now - 600)
    valid_before = str(now + max_timeout)
    chain_id = _chain_id_from_network(network)

    # P5 — EIP-712 TransferWithAuthorization signed by the Privy wallet.
    # NOTE: the EIP-712 object uses the standard camelCase `primaryType`
    # (Privy's `typed_data` wrapper is snake_case; the object inside is not).
    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": extra.get("name"),
            "version": extra.get("version"),
            "chainId": chain_id,
            "verifyingContract": asset,
        },
        "message": {
            "from": wallet_address,
            "to": pay_to,
            "value": amount,
            "validAfter": valid_after,
            "validBefore": valid_before,
            "nonce": nonce,
        },
    }
    sign_res = privy_rpc(
        wid, {"method": "eth_signTypedData_v4", "params": {"typed_data": typed_data}}
    )
    signature = (sign_res or {}).get("data", {}).get("signature")
    if not signature:
        raise ToolError(
            f"Privy did not return a signature for the x402 payment. Raw: {json.dumps(sign_res)[:400]}"
        )

    # P6 — build the payment payload and base64-encode it.
    payment_payload = {
        "x402Version": 2,
        "resource": resource,
        "accepted": accepted,
        "payload": {
            "signature": signature,
            "authorization": {
                "from": wallet_address,
                "to": pay_to,
                "value": amount,
                "validAfter": valid_after,
                "validBefore": valid_before,
                "nonce": nonce,
            },
        },
    }
    payment_header = base64.b64encode(json.dumps(payment_payload).encode()).decode()

    # P7 — retry with PAYMENT-SIGNATURE (the only header the gateway reads).
    paid_res = _client.post(
        endpoint,
        headers={
            "Content-Type": "application/json",
            "PAYMENT-SIGNATURE": payment_header,
        },
        content=body_str,
    )
    paid = _json_or_none(paid_res)
    if paid is None:
        raise ToolError(
            f"x402 paid request returned non-JSON ({paid_res.status_code}): {paid_res.text[:500]}"
        )
    settlement = {}
    settle_hdr = paid_res.headers.get("x-payment-response") or paid_res.headers.get(
        "payment-response"
    )
    if settle_hdr:
        settlement["payment-response"] = settle_hdr
    return {"data": paid.get("data"), "errors": paid.get("errors"), "settlement": settlement}


# --------------------------------------------------------------------------
# AES-256-GCM envelope crypto — byte-for-byte compatible with kms-envelope.ts
# (random 12-byte IV, 128-bit tag APPENDED to ciphertext, DEK = base64 raw 32
# bytes, contentHash = hex SHA-256 of the *plaintext*). cryptography's AESGCM
# appends the 16-byte tag to the ciphertext, exactly matching the Web Crypto layout.
# --------------------------------------------------------------------------


def encrypt_file_impl(file_path: str, plaintext_dek: str, out_path: str) -> dict[str, Any]:
    dek = base64.b64decode(plaintext_dek)
    if len(dek) != 32:
        raise ToolError(f"DEK must be 32 bytes (AES-256), got {len(dek)}.")
    plaintext = Path(file_path).read_bytes()
    iv = secrets.token_bytes(12)
    out = AESGCM(dek).encrypt(iv, plaintext, None)  # ciphertext || 16-byte tag
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(out)
    return {
        "iv": base64.b64encode(iv).decode(),
        "contentHash": hashlib.sha256(plaintext).hexdigest(),
        "cipherBytes": len(out),
    }


def decrypt_file_impl(file_path: str, iv: str, plaintext_dek: str, out_path: str) -> dict[str, Any]:
    dek = base64.b64decode(plaintext_dek)
    iv_bytes = base64.b64decode(iv)
    buf = Path(file_path).read_bytes()  # ciphertext || 16-byte tag (tag last)
    plaintext = AESGCM(dek).decrypt(iv_bytes, buf, None)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(plaintext)
    return {
        "plaintextSha256": hashlib.sha256(plaintext).hexdigest(),
        "bytes": len(plaintext),
    }


# --------------------------------------------------------------------------
# oclId packing — replicates ocl-id.ts packOclId / validateOclId.
# --------------------------------------------------------------------------

_MAX_TOKEN_ID = (1 << 80) - 1


def pack_ocl_id_impl(token_id: int, account: str, version: int = 1, namespace: int = 1) -> str:
    if not (0 <= version <= 0xFF):
        raise ToolError("version must be 0–255")
    if not (0 <= namespace <= 0xFF):
        raise ToolError("namespace must be 0–255")
    if not (0 <= token_id <= _MAX_TOKEN_ID):
        raise ToolError("tokenId must be between 0 and 2^80 - 1")
    checksummed = to_checksum_address(account)
    # validateOclId rejects an embedded zero address regardless of tokenId.
    if int(checksummed, 16) == 0:
        raise ToolError("Refusing to pack an oclId with a zero address.")
    packed = (
        (version << 248)
        | (namespace << 240)
        | (token_id << 160)
        | int(checksummed, 16)
    )
    return "0x" + format(packed, "064x")


# --------------------------------------------------------------------------
# access control conditions
# --------------------------------------------------------------------------


def _chain_for_caip_chain_id(chain_id: int) -> str:
    return {1: "ethereum", 11155111: "sepolia", 8453: "base", 84532: "baseSepolia"}.get(
        chain_id
    ) or _raise(ToolError(f"Unmapped chainId {chain_id} for access conditions."))


def _raise(exc: Exception):
    raise exc


def _ocl_has_role_condition(ocl_id: str, role: int, chain: str, resolver: str) -> list[dict]:
    # Mirrors access-control-conditions.ts buildOclAccessCondition.
    return [
        {
            "conditionType": "evmContract",
            "contractAddress": resolver,
            "chain": chain,
            "functionName": "hasRole",
            "functionParams": [ocl_id, ":userAddress", str(role)],
            "functionAbi": {
                "name": "hasRole",
                "inputs": [
                    {"name": "oclId", "type": "bytes32"},
                    {"name": "account", "type": "address"},
                    {"name": "role", "type": "uint8"},
                ],
                "outputs": [{"name": "", "type": "bool"}],
                "stateMutability": "view",
                "type": "function",
            },
            "returnValueTest": {"key": "", "comparator": "=", "value": "true"},
        }
    ]


def _ipnft_signer_condition(reservation_id: str, chain: str, resolver: str) -> list[dict]:
    # Mirrors aura createAuthorizedIpnftSignerCondition.
    return [
        {
            "chain": chain,
            "conditionType": "evmContract",
            "contractAddress": resolver,
            "functionName": "isAuthorizedSignerForIpnft",
            "functionParams": [":userAddress", reservation_id],
            "functionAbi": {
                "name": "isAuthorizedSignerForIpnft",
                "inputs": [
                    {"internalType": "address", "name": "signer", "type": "address"},
                    {"internalType": "uint256", "name": "ipnftId", "type": "uint256"},
                ],
                "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
                "stateMutability": "view",
                "type": "function",
            },
            "returnValueTest": {"comparator": "=", "key": "", "value": "true"},
        }
    ]


# --------------------------------------------------------------------------
# abi_encode — function call -> calldata, via eth-abi / eth-utils.
# --------------------------------------------------------------------------


def _split_signature(function_signature: str) -> tuple[str, list[str]]:
    sig = function_signature.strip()
    if "(" not in sig or not sig.endswith(")"):
        raise ToolError(
            f'functionSignature must look like "name(type,type,...)", got "{function_signature}".'
        )
    name = sig[: sig.index("(")].strip()
    inner = sig[sig.index("(") + 1 : -1].strip()
    if "(" in inner or ")" in inner:
        raise ToolError("Tuple/array argument types are not supported by abi_encode.")
    types = [t.strip() for t in inner.split(",")] if inner else []
    return name, types


def _coerce_abi_arg(t: str, raw: Any, index: int) -> Any:
    if t.startswith("uint") or t.startswith("int"):
        if isinstance(raw, bool):
            raise ToolError(f"Argument {index} for {t} must be an integer, not a bool.")
        return int(raw)
    if t == "bool":
        return raw is True or raw == "true"
    if t == "bytes" or (t.startswith("bytes") and t[5:].isdigit()):
        s = str(raw)
        if not (s.startswith("0x") and all(c in "0123456789abcdefABCDEF" for c in s[2:])):
            raise ToolError(
                f'Argument {index} for {t} must be 0x-prefixed hex, got "{s}". '
                "(A non-0x string would otherwise be misread as UTF-8 bytes.)"
            )
        return bytes.fromhex(s[2:])
    if t == "address":
        s = str(raw)
        if not (s.startswith("0x") and len(s) == 42):
            raise ToolError(f'Argument {index} for address must be 0x + 40 hex, got "{s}".')
        return s
    # string and anything else: pass through as text
    return str(raw)


def abi_encode_impl(function_signature: str, args: list[Any]) -> str:
    name, types = _split_signature(function_signature)
    if len(args) != len(types):
        raise ToolError(f"Expected {len(types)} args for {name}, got {len(args)}.")
    values = [_coerce_abi_arg(t, args[i], i) for i, t in enumerate(types)]
    selector = function_signature_to_4byte_selector(f"{name}({','.join(types)})")
    encoded = abi_encode_values(types, values)
    return "0x" + (selector + encoded).hex()


# ==========================================================================
# TOOLS
# ==========================================================================

# ---- Privy: wallet management + signing + sending -----------------------


@mcp.tool()
def privy_get_wallet_address(walletId: str | None = None) -> str:
    """Resolve the agent wallet address. Returns $EVM_WALLET_ADDRESS if set,
    otherwise looks up the Privy server wallet by id. Replaces aura's
    get_wallet_address and molecule-x402's 'resolve the wallet address' curl."""
    address = get_wallet_address(walletId)
    return dump({"address": address, "walletId": walletId or env("PRIVY_WALLET_ID")})


@mcp.tool()
def privy_list_wallets(chainType: str = "ethereum") -> str:
    """List existing Privy server wallets (GET /v1/wallets). Use during wallet
    setup to reuse an existing wallet."""
    auth, headers = _privy_auth()
    resp = _client.get(
        f"{PRIVY_BASE_URL}/v1/wallets",
        params={"chain_type": chainType},
        auth=auth,
        headers=headers,
    )
    if resp.status_code >= 400:
        raise ToolError(f"Privy list wallets failed ({resp.status_code}): {resp.text[:300]}")
    return dump(_json_or_none(resp))


@mcp.tool()
def privy_create_policy(
    name: str = "DeSci agent policy",
    chainId: str | None = None,
    maxValueWei: str = "10000000000000000",
) -> str:
    """Create a restrictive DeSci agent policy: single-chain (CHAIN_ID) + a
    per-tx value cap. Returns {policyId}."""
    cid = chainId or env("CHAIN_ID")
    if not cid:
        raise ToolError("chainId not provided and CHAIN_ID is not set.")
    body = {
        "version": "1.0",
        "name": name,
        "chain_type": "ethereum",
        "rules": [
            {
                "name": "Single chain only",
                "method": "eth_sendTransaction",
                "conditions": [
                    {"field_source": "ethereum_transaction", "field": "chain_id", "operator": "eq", "value": cid}
                ],
                "action": "ALLOW",
            },
            {
                "name": "Per-tx value cap",
                "method": "eth_sendTransaction",
                "conditions": [
                    {"field_source": "ethereum_transaction", "field": "value", "operator": "lte", "value": maxValueWei}
                ],
                "action": "ALLOW",
            },
        ],
    }
    auth, headers = _privy_auth()
    resp = _client.post(f"{PRIVY_BASE_URL}/v1/policies", auth=auth, headers=headers, content=json.dumps(body))
    if resp.status_code >= 400:
        raise ToolError(f"Privy create policy failed ({resp.status_code}): {resp.text[:300]}")
    j = _json_or_none(resp)
    return dump({"policyId": (j or {}).get("id"), "policy": j})


@mcp.tool()
def privy_create_wallet(policyIds: list[str] | None = None) -> str:
    """Create a Privy server wallet, optionally attaching policy ids. Returns
    {walletId, address}. After this, set PRIVY_WALLET_ID for future runs."""
    body: dict[str, Any] = {"chain_type": "ethereum"}
    if policyIds:
        body["policy_ids"] = policyIds
    auth, headers = _privy_auth()
    resp = _client.post(f"{PRIVY_BASE_URL}/v1/wallets", auth=auth, headers=headers, content=json.dumps(body))
    if resp.status_code >= 400:
        raise ToolError(f"Privy create wallet failed ({resp.status_code}): {resp.text[:300]}")
    j = _json_or_none(resp)
    return dump({"walletId": (j or {}).get("id"), "address": (j or {}).get("address"), "wallet": j})


@mcp.tool()
def privy_sign_message(message: str, walletId: str | None = None, encoding: Literal["utf-8", "hex"] = "utf-8") -> str:
    """EIP-191 personal_sign via the Privy wallet. Used to sign the IP-NFT terms
    message and the service-token sign-in message. Returns {signature}."""
    wid = resolve_wallet_id(walletId)
    res = privy_rpc(wid, {"method": "personal_sign", "params": {"message": message, "encoding": encoding}})
    signature = (res or {}).get("data", {}).get("signature")
    if not signature:
        raise ToolError(f"No signature returned. Raw: {json.dumps(res)[:300]}")
    return dump({"signature": signature})


@mcp.tool()
def privy_sign_typed_data(typedData: dict, walletId: str | None = None) -> str:
    """Generic eth_signTypedData_v4 via the Privy wallet. Pass the full EIP-712
    typed-data object using the standard camelCase `primaryType` key (Privy wraps
    it as params.typed_data). x402_pay does this internally; use this only for
    ad-hoc signing. Returns {signature}."""
    wid = resolve_wallet_id(walletId)
    res = privy_rpc(wid, {"method": "eth_signTypedData_v4", "params": {"typed_data": typedData}})
    signature = (res or {}).get("data", {}).get("signature")
    if not signature:
        raise ToolError(f"No signature returned. Raw: {json.dumps(res)[:300]}")
    return dump({"signature": signature})


@mcp.tool()
def privy_send_transaction(
    to: str,
    data: str | None = None,
    value: str | None = None,
    chainId: str | None = None,
    walletId: str | None = None,
) -> str:
    """Send a transaction from the Privy wallet (eth_sendTransaction with
    caip2 eip155:<chainId>). Replaces aura's sign_and_send_transaction (POI
    anchor, IP-NFT mint, NFT transfer). value is decimal wei (string).
    Returns {txHash}."""
    wid = resolve_wallet_id(walletId)
    cid = chainId or env("CHAIN_ID")
    if not cid:
        raise ToolError("chainId not provided and CHAIN_ID is not set.")
    transaction: dict[str, Any] = {"to": to}
    if data:
        transaction["data"] = data
    if value:
        transaction["value"] = value
    res = privy_rpc(
        wid,
        {"method": "eth_sendTransaction", "caip2": f"eip155:{cid}", "params": {"transaction": transaction}},
    )
    data_obj = (res or {}).get("data", {}) or {}
    tx_hash = data_obj.get("hash") or data_obj.get("transaction_hash") or (res or {}).get("hash")
    if not tx_hash:
        raise ToolError(f"No tx hash returned. Raw: {json.dumps(res)[:400]}")
    return dump({"txHash": tx_hash})


# ---- Molecule HTTP -------------------------------------------------------


@mcp.tool()
def poi_register(filePath: str, clientUrl: str | None = None, contentType: str = "application/pdf") -> str:
    """Register a Proof of Invention: multipart POST to
    $MOLECULE_CLIENT_URL/api/v1/inventions (field name 'files', Bearer
    $POI_API_KEY). Returns the full response plus extracted
    {poiTo, poiData, merkleRoot}."""
    creds = require_env("POI_API_KEY")
    base = clientUrl or env("MOLECULE_CLIENT_URL")
    if not base:
        raise ToolError("MOLECULE_CLIENT_URL is not set (and no clientUrl override given).")
    url = f"{base.rstrip('/')}/api/v1/inventions"
    data = Path(filePath).read_bytes()
    name = Path(filePath).name or "document.pdf"
    resp = _client.post(
        url,
        headers={"Authorization": f"Bearer {creds['POI_API_KEY']}"},
        files={"files": (name, data, contentType)},
    )
    j = _json_or_none(resp)
    if resp.status_code >= 400 or j is None:
        raise ToolError(f"POI registration failed ({resp.status_code}): {resp.text[:500]}")
    tx = (j.get("data") or {}).get("transaction") or {}
    proof = (j.get("data") or {}).get("proof") or {}
    tree = proof.get("tree") or []
    return dump(
        {
            "poiTo": tx.get("to"),
            "poiData": tx.get("data"),
            "merkleRoot": tree[0] if tree else None,
            "response": j,
        }
    )


@mcp.tool()
def labs_graphql(
    query: str,
    variables: dict | None = None,
    auth: LabsAuth = "api-key",
    labsUrl: str | None = None,
) -> str:
    """POST a GraphQL query/mutation to $MOLECULE_LABS_URL. auth='api-key' sends
    x-api-key:$MOLECULE_API_KEY (aura mint flow). auth='service-token' sends
    x-service-token:$MOLECULE_SERVICE_TOKEN + x-wallet-address:$EVM_WALLET_ADDRESS
    (molecule-x402). auth='none' for public sign-in queries. Returns {data, errors}.
    Do NOT use for generateDataEncryptionKey/decryptDataKey — use
    labs_generate_dek/labs_decrypt_dek so the plaintext DEK stays inside the server."""
    return dump(labs_graphql_call(query, variables or {}, auth, labsUrl))


@mcp.tool()
def x402_pay(
    mutation: str,
    query: str,
    variables: dict | None = None,
    gatewayUrl: str | None = None,
    walletId: str | None = None,
) -> str:
    """Run the entire x402 payment flow (P1–P7) for ONE whitelisted mutation in a
    single call: send -> decode the payment-required challenge -> sign the EIP-712
    TransferWithAuthorization with the Privy wallet -> retry with PAYMENT-SIGNATURE.
    The single top-level GraphQL field in `query` MUST equal `mutation` (the
    gateway's validateMutationQuery enforces this). Returns {data, errors, settlement}.
    Whitelisted mutations: initiateCreateOrUpdateFile, finishCreateOrUpdateFile,
    createAnnouncement, createLab (any other mutation 400s with 'not enabled for
    x402 gateway')."""
    return dump(run_x402_pay(mutation, query, variables, gatewayUrl, walletId))


@mcp.tool()
def s3_upload(
    uploadUrl: str,
    filePath: str,
    method: str = "PUT",
    contentType: str = "application/pdf",
    headers: dict | None = None,
) -> str:
    """PUT (or POST) a local file to a presigned S3 URL, applying all headers
    returned by the initiate step plus Content-Type. NO x402 payment. Used for the
    cover image, public file upload (Step B), and the encrypted ciphertext (E3).
    Returns {status, ok}."""
    data = Path(filePath).read_bytes()
    all_headers = {"Content-Type": contentType, **(headers or {})}
    resp = _client.request(method.upper(), uploadUrl, headers=all_headers, content=data)
    if resp.status_code >= 400:
        raise ToolError(f"S3 upload failed ({resp.status_code}): {resp.text[:300]}")
    return dump({"status": resp.status_code, "ok": resp.is_success})


# ---- DEK-aware tools (plaintext DEK never leaves the server) -------------


@mcp.tool()
def labs_generate_dek(
    transport: Literal["direct", "x402"] = "direct",
    auth: LabsAuth = "service-token",
    gatewayUrl: str | None = None,
    labsUrl: str | None = None,
    walletId: str | None = None,
) -> str:
    """Call generateDataEncryptionKey and KEEP the plaintext DEK inside this
    server. Returns {encryptedDek, encryptionSystem, dekHandle} — pass dekHandle to
    encrypt_file. The plaintext DEK is NEVER returned to the agent. transport='direct'
    (default, service-token) is the working path. NOTE: generateDataEncryptionKey is
    NOT in the x402 gateway whitelist (mutations.ts), so transport='x402' will 400
    until it is added there — use 'direct'."""
    query = (
        "mutation GenerateDataEncryptionKey { generateDataEncryptionKey { isSuccess "
        "plaintextDEK encryptedDek encryptionSystem error { message code retryable } } }"
    )
    if transport == "x402":
        r = run_x402_pay("generateDataEncryptionKey", query, {}, gatewayUrl, walletId)
        if r.get("errors"):
            raise ToolError(f"generateDataEncryptionKey errors: {json.dumps(r['errors'])[:400]}")
        result = (r.get("data") or {}).get("generateDataEncryptionKey")
    else:
        r = labs_graphql_call(query, {}, auth, labsUrl)
        if r.get("errors"):
            raise ToolError(f"generateDataEncryptionKey errors: {json.dumps(r['errors'])[:400]}")
        result = (r.get("data") or {}).get("generateDataEncryptionKey")
    if not result or not result.get("isSuccess") or not result.get("plaintextDEK"):
        raise ToolError(
            f"generateDataEncryptionKey did not succeed: {json.dumps((result or {}).get('error') or result)[:400]}"
        )
    handle = put_dek(result["plaintextDEK"])
    return dump(
        {
            "encryptedDek": result.get("encryptedDek"),
            "encryptionSystem": result.get("encryptionSystem"),
            "dekHandle": handle,
        }
    )


@mcp.tool()
def labs_decrypt_dek(
    filePath: str | None = None,
    oclId: str | None = None,
    tokenUri: str | None = None,
    agreementUrl: str | None = None,
    transport: Literal["direct", "x402"] = "direct",
    auth: LabsAuth = "service-token",
    gatewayUrl: str | None = None,
    labsUrl: str | None = None,
    walletId: str | None = None,
) -> str:
    """Call decryptDataKey (the backend evaluates on-chain access conditions for
    the caller) and KEEP the plaintext DEK inside this server. Returns
    {iv, dekHandle, message} — pass dekHandle to decrypt_file.

    The decryptDataKey mutation accepts ONLY oclId/filePath (data-room file) or
    tokenUri/agreementUrl (IPFS agreement) — there is NO ipnftUid argument. For a
    data-room file pass oclId + filePath. ACCESS_DENIED means the caller wallet
    fails the on-chain condition; LEGACY_ENCRYPTION means the file predates the
    envelope flow. NOTE: decryptDataKey is NOT x402-whitelisted, so transport='x402'
    will 400 — use 'direct'."""
    if not oclId and not tokenUri:
        raise ToolError("Provide oclId (data-room file) or tokenUri (IPFS agreement).")
    arg_decls, arg_uses, variables = [], [], {}
    if oclId:
        arg_decls.append("$oclId: String")
        arg_uses.append("oclId: $oclId")
        variables["oclId"] = oclId
    if filePath:
        arg_decls.append("$filePath: String")
        arg_uses.append("filePath: $filePath")
        variables["filePath"] = filePath
    if tokenUri:
        arg_decls.append("$tokenUri: String")
        arg_uses.append("tokenUri: $tokenUri")
        variables["tokenUri"] = tokenUri
    if agreementUrl:
        arg_decls.append("$agreementUrl: String")
        arg_uses.append("agreementUrl: $agreementUrl")
        variables["agreementUrl"] = agreementUrl
    query = (
        f"mutation DecryptDataKey({', '.join(arg_decls)}) {{ decryptDataKey({', '.join(arg_uses)}) "
        "{ isSuccess plaintextDEK iv message error { message code retryable } } }"
    )
    if transport == "x402":
        r = run_x402_pay("decryptDataKey", query, variables, gatewayUrl, walletId)
        result = (r.get("data") or {}).get("decryptDataKey")
    else:
        r = labs_graphql_call(query, variables, auth, labsUrl)
        result = (r.get("data") or {}).get("decryptDataKey")
    if not result or not result.get("isSuccess") or not result.get("plaintextDEK"):
        # Surface backend status verbatim (ACCESS_DENIED / LEGACY_ENCRYPTION).
        return dump(
            {
                "isSuccess": False,
                "message": (result or {}).get("message"),
                "error": (result or {}).get("error"),
            }
        )
    handle = put_dek(result["plaintextDEK"])
    return dump({"iv": result.get("iv"), "dekHandle": handle, "message": result.get("message")})


# ---- Crypto / encoding ---------------------------------------------------


@mcp.tool()
def encrypt_file(filePath: str, dekHandle: str, outPath: str) -> str:
    """AES-256-GCM envelope-encrypt a file exactly like the Labs client
    encryptFileWithKms: random 12-byte IV, 16-byte tag APPENDED to the ciphertext.
    Pass dekHandle from labs_generate_dek (the plaintext DEK never enters the
    conversation). Writes ciphertext to outPath. Returns
    {iv (base64), contentHash (hex SHA-256 of plaintext), cipherBytes}."""
    return dump(encrypt_file_impl(filePath, get_dek(dekHandle), outPath))


@mcp.tool()
def decrypt_file(filePath: str, iv: str, dekHandle: str, outPath: str) -> str:
    """AES-256-GCM decrypt a ciphertext written by encrypt_file (last 16 bytes are
    the auth tag), exactly like decryptFileWithKms. Pass dekHandle from
    labs_decrypt_dek and the iv it returned. Writes plaintext to outPath. Returns
    {plaintextSha256, bytes} — compare plaintextSha256 to the original contentHash
    to confirm round-trip integrity."""
    return dump(decrypt_file_impl(filePath, iv, get_dek(dekHandle), outPath))


@mcp.tool()
def sha256_file(filePath: str) -> str:
    """Return the hex SHA-256 digest of a local file, plus its byte size. (Use the
    byte size wherever the public upload flow needs contentLength.)"""
    data = Path(filePath).read_bytes()
    return dump({"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})


@mcp.tool()
def hex_to_uint256(hex: str) -> str:
    """Convert a 0x-prefixed hex hash (e.g. the POI merkle_root) to its uint256
    decimal string. This decimal IS the reservationId / token_id / ipnftId. A small
    result (isSmall) means POI failed."""
    h = hex if hex.startswith("0x") else f"0x{hex}"
    value = int(h, 16)
    return dump({"decimal": str(value), "isSmall": value < 1000})


@mcp.tool()
def pack_ocl_id(tokenId: str, account: str, version: int = 1, namespace: int = 1) -> str:
    """Pack an oclId from tokenId + ERC-6551 TBA account, replicating ocl-id.ts
    packOclId (version 0x01, namespace 0x01, 80-bit tokenId, 160-bit address).
    Rejects a zero embedded address. Returns {oclId} (66-char 0x-hex)."""
    return dump({"oclId": pack_ocl_id_impl(int(tokenId), account, version, namespace)})


@mcp.tool()
def abi_encode(functionSignature: str, args: list) -> str:
    """ABI-encode a Solidity function call to calldata. functionSignature is e.g.
    'mintReservation(address,uint256,string,string,bytes)' or
    'safeTransferFrom(address,address,uint256)'. Pass args in order: uint*/int* as
    decimal strings or ints; bytes/bytesN as 0x-prefixed hex (a non-0x string is
    rejected, NOT silently UTF-8 encoded); address as 0x + 40 hex; string as text.
    Returns {calldata}."""
    return dump({"calldata": abi_encode_impl(functionSignature, args)})


@mcp.tool()
def build_access_conditions(
    mode: Literal["ocl-hasRole", "ipnft-signer"],
    accessResolverAddress: str | None = None,
    chain: str | None = None,
    oclId: str | None = None,
    role: int = 1,
    reservationId: str | None = None,
    chainId: str | None = None,
    environment: str | None = None,
) -> str:
    """Build the on-chain accessControlConditions array for an encrypted upload, and
    return both the array and its JSON-stringified string (ready for
    encryptionMetadata.accessControlConditions). mode='ocl-hasRole' (molecule-x402):
    hasRole(oclId, :userAddress, role) — role 1=Viewer (default), 2=Contributor; chain
    from ENVIRONMENT (production->base else baseSepolia) unless overridden.
    mode='ipnft-signer' (aura): isAuthorizedSignerForIpnft(:userAddress, reservationId);
    chain from CHAIN_ID. accessResolverAddress defaults to $ACCESS_RESOLVER_ADDRESS."""
    resolver = accessResolverAddress or env("ACCESS_RESOLVER_ADDRESS")
    if not resolver:
        raise ToolError("accessResolverAddress not given and ACCESS_RESOLVER_ADDRESS is not set.")
    if mode == "ocl-hasRole":
        if not oclId:
            raise ToolError("mode 'ocl-hasRole' requires oclId.")
        envv = environment or env("ENVIRONMENT")
        ch = chain or ("base" if envv == "production" else "baseSepolia")
        conditions = _ocl_has_role_condition(oclId, role, ch, resolver)
    else:
        if not reservationId:
            raise ToolError("mode 'ipnft-signer' requires reservationId.")
        cid = int(chainId or env("CHAIN_ID") or 0)
        if not cid:
            raise ToolError("chainId not given and CHAIN_ID is not set.")
        ch = chain or _chain_for_caip_chain_id(cid)
        conditions = _ipnft_signer_condition(reservationId, ch, resolver)
    return dump({"conditions": conditions, "json": json.dumps(conditions, separators=(",", ":"))})


# ---- service token bootstrap --------------------------------------------


@mcp.tool()
def mint_service_token(
    serviceName: str = "data-sync-service",
    expiresIn: str = "720h",
    walletAddress: str | None = None,
    walletId: str | None = None,
    labsUrl: str | None = None,
) -> str:
    """One-time bootstrap: getServiceSignInMessage -> personal_sign (Privy) ->
    generateServiceToken. Returns {token, tokenId, expiresAt} — set token as
    MOLECULE_SERVICE_TOKEN in settings.local.json. The token is a secret; this server
    never logs it. Prefer pre-setting MOLECULE_SERVICE_TOKEN over minting per run."""
    addr = walletAddress or get_wallet_address(walletId)
    msg = labs_graphql_call(
        "query GetServiceSignInMessage($walletAddress: String!, $serviceName: String!) "
        "{ getServiceSignInMessage(walletAddress: $walletAddress, serviceName: $serviceName) { message } }",
        {"walletAddress": addr, "serviceName": serviceName},
        "none",
        labsUrl,
    )
    message = ((msg.get("data") or {}).get("getServiceSignInMessage") or {}).get("message")
    if not message:
        raise ToolError(f"getServiceSignInMessage returned no message: {json.dumps(msg.get('errors') or msg)[:300]}")
    wid = resolve_wallet_id(walletId)
    sig = privy_rpc(wid, {"method": "personal_sign", "params": {"message": message, "encoding": "utf-8"}})
    message_signature = (sig or {}).get("data", {}).get("signature")
    if not message_signature:
        raise ToolError("Privy did not return a signature for the sign-in message.")
    tok = labs_graphql_call(
        "mutation GenerateServiceToken($serviceName: String!, $expiresIn: String!, $walletAddress: String, $messageSignature: String) "
        "{ generateServiceToken(serviceName: $serviceName, expiresIn: $expiresIn, walletAddress: $walletAddress, messageSignature: $messageSignature) "
        "{ token tokenId serviceName expiresAt isSuccess message } }",
        {"serviceName": serviceName, "expiresIn": expiresIn, "walletAddress": addr, "messageSignature": message_signature},
        "none",
        labsUrl,
    )
    result = (tok.get("data") or {}).get("generateServiceToken")
    if not result or not result.get("isSuccess") or not result.get("token"):
        raise ToolError(f"generateServiceToken failed: {json.dumps(result or tok.get('errors'))[:300]}")
    return dump({"token": result.get("token"), "tokenId": result.get("tokenId"), "expiresAt": result.get("expiresAt")})


# --------------------------------------------------------------------------
# boot
# --------------------------------------------------------------------------


def main() -> None:
    log("molecule-mcp ready (stdio)")
    mcp.run()


if __name__ == "__main__":
    main()
