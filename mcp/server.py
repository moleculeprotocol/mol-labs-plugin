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

CUSTODY-FREE BY DESIGN. This server NEVER holds a private key, signs a message,
directly authorizes a payment, or broadcasts an on-chain transaction. It only *crafts*
the requests, EIP-712 typed-data, calldata, and payloads that the Molecule protocol needs, and
performs the non-signing HTTP requests that surround them (POI registration, Labs
GraphQL, the x402 challenge fetch + paid submission, S3 uploads). Every step that
requires a wallet is handed back to the **caller** to sign/send with their own
signer — a Privy agentic wallet (the recommended first option) or any key they
control — and the signature / transaction hash is passed back in to continue.

The split, concretely:
  - prepare_transaction ....... returns {to, data, value, chainId} for the caller
                                to sign + broadcast with their wallet (POI anchor,
                                IP-NFT mint, NFT transfer).
  - x402_prepare / x402_submit  prepare returns the EIP-712 TransferWithAuthorization
                                the caller signs; submit takes that signature and
                                posts the paid request. The server signs nothing.
  - service_signin_message /    prepare returns the sign-in message the caller
    service_token_create        personal_signs; create exchanges that signature for
                                the Labs JWT. The server signs nothing.
The rest (abi_encode, encrypt_file/decrypt_file, build_access_conditions, the DEK
tools, sha256_file, hex_to_uint256) is pure compute or service-token HTTP.

Written in Python (FastMCP) and run over stdio so it works under any MCP-capable
harness (Claude Code, Codex, …) with only a Python interpreter — no Bun/Node required.

This server targets the **V2 GraphQL surface** (the one live on production), keyed on
``ipnftUid`` (``{contractAddress}_{tokenId}``). The retired OCL surface (``oclId``,
``initiateCreateOrUpdateFile``/``finishCreateOrUpdateFile``/``createAnnouncement``/``createLab``)
is intentionally NOT supported here.

Source-of-truth parity (these tools faithfully replicate the real backend):
  - x402 challenge / payment header .. desci-infra/lambda/x402-gateway-lambda/index.ts
  - x402 mutation whitelist ........... desci-infra/lambda/x402-gateway-lambda/mutations.ts
  - AES-256-GCM envelope .............. desci-ecosystem/packages/storage/src/lib/encryption/kms-envelope.ts
  - access conditions ................. desci-infra/lambda/common/utils/access-control-conditions.ts
  - GraphQL field shapes .............. desci-infra/graphql/schemas/{ip-hubs,encryption}.graphql
  - request shapes / auth ............. desci-infra/bruno/desci-labs/v2 + desci-infra/bruno/service-auth

Transport: stdio. NOTHING is written to stdout except the JSON-RPC protocol —
FastMCP owns stdout; all diagnostics go to stderr (see ``log``). Secrets
(service tokens, API keys) are never logged or returned to the caller; the server
holds NO wallet credentials at all.
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
from eth_utils import function_signature_to_4byte_selector

from mcp.server.fastmcp import FastMCP

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
# wallet address (NEVER a key) — the address whose wallet the caller will use to
# sign. The server only ever needs the public address (for the EIP-3009 `from`,
# the IP-NFT minter/terms signer, and the service-token adminAddress). The key
# stays entirely with the caller.
# --------------------------------------------------------------------------


def resolve_address(explicit: str | None = None) -> str:
    addr = explicit or env("EVM_WALLET_ADDRESS")
    if not addr:
        raise ToolError(
            "No wallet address available. Pass walletAddress, or set EVM_WALLET_ADDRESS "
            "to the public address of the wallet that will sign (a Privy agentic wallet — "
            "the recommended first option — or any key you control). The server never "
            "needs the private key."
        )
    return addr


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
# Confidentiality latch — fail-closed privacy guard.
#
# The aura skill instructs the agent never to fall back to a public upload when
# the private/encrypted path fails. Instructions are not a guarantee, so this
# latch makes the breach *physically impossible* at the tool boundary: the
# moment the agent declares a file confidential (by encrypting it, or by
# building on-chain access conditions for its IP-NFT) we refuse, for the rest
# of this process, to (a) S3-upload that file's plaintext bytes or (b) finalize
# that IP-NFT as PUBLIC / without encryptionMetadata. Process-local and
# NON-overridable — there is deliberately no force flag, because the whole
# point is that an agent under "just finish the upload" pressure cannot opt out.
#
# Known limits (disclosed, not bugs): the latch lives in memory, so an MCP
# subprocess restart clears it — but a restart also wipes the DEK store and
# breaks the run, forcing a re-run from a clean state. It is keyed on the exact
# plaintext bytes, so re-encoding the plaintext to different bytes before upload
# would evade the hash check — the per-IP-NFT finalize guard still covers that
# molecule.
# --------------------------------------------------------------------------

_confidential_plaintext_hashes: set[str] = set()  # hex sha256 of plaintext the agent encrypted
_confidential_plaintext_paths: set[str] = set()  # resolved abs paths passed to encrypt_file
_confidential_token_ids: set[str] = set()  # IP-NFT tokenIds that got on-chain access conditions


def mark_confidential_plaintext(file_path: str, plaintext_sha256_hex: str) -> None:
    """Latch a file as confidential once the agent encrypts it. Its plaintext
    may never again be S3-uploaded by this process."""
    _confidential_plaintext_hashes.add(plaintext_sha256_hex.lower())
    try:
        _confidential_plaintext_paths.add(str(Path(file_path).resolve()))
    except OSError:
        pass


def mark_confidential_token_id(token_id: str | None) -> None:
    """Latch an IP-NFT tokenId as confidential once on-chain access conditions
    are built for it. It may never be finalized PUBLIC / without encryption."""
    if token_id and str(token_id).strip():
        _confidential_token_ids.add(str(token_id).strip())


def _token_id_from_ipnft_uid(ipnft_uid: str | None) -> str | None:
    """An ipnftUid is `{contractAddress}_{tokenId}`; return the tokenId part."""
    if not ipnft_uid or "_" not in ipnft_uid:
        return None
    return ipnft_uid.rsplit("_", 1)[1].strip()


def assert_not_confidential_plaintext(file_path: str, data: bytes) -> None:
    """Fail-closed gate for S3 uploads: refuse to PUT the plaintext of a file
    the agent encrypted for a confidential upload (catches both a path reuse and
    a byte-identical copy at a different path)."""
    upload_sha = hashlib.sha256(data).hexdigest().lower()
    try:
        resolved = str(Path(file_path).resolve())
    except OSError:
        resolved = None
    if upload_sha in _confidential_plaintext_hashes or (
        resolved is not None and resolved in _confidential_plaintext_paths
    ):
        raise ToolError(
            "PRIVACY GUARD (non-overridable): refusing to S3-upload the plaintext of a "
            "file that was encrypted for a confidential upload. The private/encrypted "
            "upload MUST NOT fall back to a public upload. Upload the '.enc' ciphertext "
            "instead, or abort the run and report the failure — never publish this "
            "file's plaintext."
        )


def assert_confidential_finalize_ok(variables: dict[str, Any] | None) -> None:
    """Fail-closed gate for finishCreateOrUpdateFileV2: if on-chain access
    conditions were built for this IP-NFT, refuse a PUBLIC / unencrypted
    finalize."""
    v = variables or {}
    token_id = _token_id_from_ipnft_uid(v.get("ipnftUid"))
    if not token_id or token_id not in _confidential_token_ids:
        return
    access_level = str(v.get("accessLevel") or "").upper()
    has_enc_meta = bool(v.get("encryptionMetadata"))
    if access_level == "PUBLIC" or not has_enc_meta:
        raise ToolError(
            f"PRIVACY GUARD (non-overridable): refusing to finalize IP-NFT tokenId "
            f"{token_id} with accessLevel={access_level or 'MISSING'} / "
            f"encryptionMetadata={'present' if has_enc_meta else 'MISSING'}. On-chain "
            "access conditions were built for this IP-NFT (a confidential upload), so it "
            "must be finalized with a non-PUBLIC accessLevel AND encryptionMetadata. Do "
            "NOT fall back to the public path — abort and report the failure."
        )


# --------------------------------------------------------------------------
# Labs GraphQL (direct, non-signing HTTP)
# --------------------------------------------------------------------------

LabsAuth = Literal["service-token", "api-key", "none"]


def _labs_headers(
    auth: LabsAuth,
    service_token: str | None = None,
    wallet_address: str | None = None,
) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if auth == "service-token":
        # Per-call overrides let the agent act as any authorized wallet (e.g.
        # decrypt as the OWNER wallet) without swapping env / reloading.
        token = service_token or env("MOLECULE_SERVICE_TOKEN")
        addr = wallet_address or env("EVM_WALLET_ADDRESS")
        if not token or not addr:
            raise ToolError(
                "service-token auth needs MOLECULE_SERVICE_TOKEN + EVM_WALLET_ADDRESS "
                "(or serviceToken / walletAddress overrides)."
            )
        headers["x-service-token"] = token
        headers["x-wallet-address"] = addr
    elif auth == "api-key":
        creds = require_env("MOLECULE_API_KEY")
        headers["x-api-key"] = creds["MOLECULE_API_KEY"]
    return headers


def labs_graphql_call(
    query: str,
    variables: dict[str, Any],
    auth: LabsAuth,
    labs_url: str | None = None,
    service_token: str | None = None,
    wallet_address: str | None = None,
) -> dict[str, Any]:
    url = labs_url or env("MOLECULE_LABS_URL")
    if not url:
        raise ToolError("MOLECULE_LABS_URL is not set (and no labsUrl override given).")
    resp = _client.post(
        url,
        headers=_labs_headers(auth, service_token, wallet_address),
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
# x402 payment — PREPARE (build the EIP-712 the caller signs) and SUBMIT (post
# the caller's signature). The server signs NOTHING. Mirrors x402-gateway-lambda:
#   prepare: P1 send -> P2 decode payment-required -> P3 wallet -> P4 nonce/validity
#            -> build TransferWithAuthorization typed-data
#   << caller signs typedData with their wallet (eth_signTypedData_v4) >>
#   submit:  P6 build+base64 PAYMENT-SIGNATURE header -> P7 retry
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


def _x402_prepare(
    mutation: str,
    query: str,
    variables: dict[str, Any] | None,
    wallet_address: str,
    gateway_url: str | None,
) -> dict[str, Any]:
    # Fail-closed: never let a confidential IP-NFT be set up for a public /
    # plaintext finalize, even at the prepare step.
    if mutation == "finishCreateOrUpdateFileV2":
        assert_confidential_finalize_ok(variables)
    gateway = gateway_url or env("X402_GATEWAY_URL")
    if not gateway:
        raise ToolError("X402_GATEWAY_URL is not set.")
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

    # P3/P4 — `from` is the caller's wallet (EIP-3009 requires from == signer);
    # fresh nonce + validity window.
    now = int(time.time())
    nonce = "0x" + secrets.token_hex(32)
    valid_after = str(now - 600)
    valid_before = str(now + max_timeout)
    chain_id = _chain_id_from_network(network)

    # Standard EIP-712 TransferWithAuthorization (camelCase primaryType). The
    # CALLER signs this with their wallet (Privy: remap primaryType->primary_type
    # for Privy's wallet-RPC; a raw key signs it directly). The server does NOT.
    authorization = {
        "from": wallet_address,
        "to": pay_to,
        "value": amount,
        "validAfter": valid_after,
        "validBefore": valid_before,
        "nonce": nonce,
    }
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
        "message": authorization,
    }
    return {
        "endpoint": endpoint,
        "query": query,
        "variables": variables or {},
        "accepted": accepted,
        "resource": resource,
        "network": network,
        "chainId": chain_id,
        "authorization": authorization,
        "typedData": typed_data,
    }


def _x402_submit(prepared: dict[str, Any], signature: str) -> dict[str, Any]:
    if not isinstance(prepared, dict):
        raise ToolError("`prepared` must be the object returned by x402_prepare.")
    for k in ("endpoint", "query", "accepted", "authorization"):
        if k not in prepared:
            raise ToolError(f"`prepared` is missing '{k}' — pass the x402_prepare result verbatim.")
    if not signature:
        raise ToolError("`signature` (the caller's EIP-712 signature of prepared.typedData) is required.")
    variables = prepared.get("variables") or {}
    # Re-apply the fail-closed finalize guard at submit time.
    if "finishCreateOrUpdateFileV2" in (prepared.get("query") or ""):
        assert_confidential_finalize_ok(variables)

    # P6 — build the payment payload and base64-encode it.
    payment_payload = {
        "x402Version": 2,
        "resource": prepared.get("resource"),
        "accepted": prepared["accepted"],
        "payload": {"signature": signature, "authorization": prepared["authorization"]},
    }
    payment_header = base64.b64encode(json.dumps(payment_payload).encode()).decode()
    body_str = json.dumps({"query": prepared["query"], "variables": variables})

    # P7 — retry with PAYMENT-SIGNATURE (the only header the gateway reads).
    paid_res = _client.post(
        prepared["endpoint"],
        headers={"Content-Type": "application/json", "PAYMENT-SIGNATURE": payment_header},
        content=body_str,
    )
    paid = _json_or_none(paid_res)
    if paid is None:
        raise ToolError(
            f"x402 paid request returned non-JSON ({paid_res.status_code}): {paid_res.text[:500]}"
        )
    settlement = {}
    settle_hdr = paid_res.headers.get("x-payment-response") or paid_res.headers.get("payment-response")
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
# access control conditions
# --------------------------------------------------------------------------


def _chain_for_caip_chain_id(chain_id: int) -> str:
    return {1: "ethereum", 11155111: "sepolia", 8453: "base", 84532: "baseSepolia"}.get(
        chain_id
    ) or _raise(ToolError(f"Unmapped chainId {chain_id} for access conditions."))


def _raise(exc: Exception):
    raise exc


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

# ---- Wallet handoff: the server prepares, the CALLER's wallet signs/sends ----
# None of these tools sign or broadcast. They craft the exact request/typed-data
# the caller then signs and sends with their own wallet — a Privy agentic wallet
# (the recommended first option) or any key the caller controls.


@mcp.tool()
def prepare_transaction(
    to: str,
    data: str | None = None,
    value: str | None = None,
    chainId: str | None = None,
) -> str:
    """Craft an Ethereum transaction request for the CALLER to sign + broadcast
    with their own wallet (POI anchor, IP-NFT mint, NFT transfer). This server
    does NOT sign or send — it only assembles and normalizes the fields. Build
    `data` with abi_encode. value is decimal wei (or 0x hex); it is returned in
    both decimal and hex. Returns {transaction:{to,data,value,valueWei,chainId,
    caip2}, note}. Hand `transaction` to your wallet's send-transaction call
    (Privy: eth_sendTransaction; raw key: sign + eth_sendRawTransaction) and feed
    the resulting txHash back into the next step."""
    cid = str(chainId or env("CHAIN_ID") or "")
    if not cid:
        raise ToolError("chainId not provided and CHAIN_ID is not set.")
    tx: dict[str, Any] = {"to": to, "chainId": int(cid), "caip2": f"eip155:{cid}"}
    if data:
        tx["data"] = data
    if value is not None and str(value).strip() != "":
        v = str(value).strip()
        wei = int(v, 16) if v.lower().startswith("0x") else int(v)
        tx["value"] = str(wei)  # decimal wei
        tx["valueWei"] = hex(wei)  # 0x hex wei (what most wallet RPCs want)
    return dump(
        {
            "transaction": tx,
            "note": (
                "Sign and broadcast this with YOUR wallet (Privy agentic wallet recommended; "
                "or any key you control). This server never signs or sends. Then pass the "
                "returned txHash to the next step."
            ),
        }
    )


@mcp.tool()
def x402_prepare(
    mutation: str,
    query: str,
    variables: dict | None = None,
    walletAddress: str | None = None,
    gatewayUrl: str | None = None,
) -> str:
    """Prepare a paid x402 mutation WITHOUT signing: fetch the gateway's 402
    challenge and build the EIP-712 `TransferWithAuthorization` (EIP-3009 USDC)
    that the CALLER must sign with their wallet. The single top-level GraphQL field
    in `query` MUST equal `mutation`. `walletAddress` (or EVM_WALLET_ADDRESS) is the
    EIP-3009 `from` and MUST equal the address that will sign. Returns a `prepared`
    object — sign `prepared.typedData` with your wallet (Privy: eth_signTypedData_v4,
    remapping primaryType->primary_type per Privy's RPC; raw key: sign_typed_data),
    then call x402_submit(prepared, signature). Whitelisted mutations (V2 surface):
    initiateCreateOrUpdateFileV2, finishCreateOrUpdateFileV2, createAnnouncementV2,
    createProject, addProjectOwner, generateDataEncryptionKey, decryptDataKey. All
    data-room args are keyed on ipnftUid ({contractAddress}_{tokenId})."""
    addr = resolve_address(walletAddress)
    prepared = _x402_prepare(mutation, query, variables, addr, gatewayUrl)
    return dump(
        {
            "prepared": prepared,
            "next": (
                "Sign prepared.typedData with YOUR wallet (Privy agentic wallet recommended), "
                "then call x402_submit with {prepared, signature}. This server signs nothing."
            ),
        }
    )


@mcp.tool()
def x402_submit(prepared: dict, signature: str) -> str:
    """Submit a prepared x402 mutation using the CALLER's signature. Pass the
    `prepared` object returned by x402_prepare verbatim plus the EIP-712
    `signature` your wallet produced over prepared.typedData. This builds the
    base64 PAYMENT-SIGNATURE header and posts the paid request — it does NOT sign.
    Returns {data, errors, settlement}; read data.<mutation> and check
    isSuccess / error. (A response that reports isSuccess:false is a real business
    error — surface it.)"""
    return dump(_x402_submit(prepared, signature))


# ---- Molecule HTTP (non-signing) -----------------------------------------


@mcp.tool()
def poi_register(filePath: str, clientUrl: str | None = None, contentType: str = "application/pdf") -> str:
    """Register a Proof of Invention: multipart POST to
    $MOLECULE_CLIENT_URL/api/v1/inventions (field name 'files', Bearer
    $POI_API_KEY). Returns the full response plus extracted
    {poiTo, poiData, merkleRoot}. (No wallet involved; the returned poiTo/poiData
    is the on-chain anchor tx you then prepare_transaction + sign/send yourself.)"""
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
    (private/encrypted upload). auth='none' for public queries. Returns {data, errors}.
    This is non-signing HTTP only — it does not sign or send transactions. Do NOT use
    for generateDataEncryptionKey/decryptDataKey — use labs_generate_dek/labs_decrypt_dek
    so the plaintext DEK stays inside the server. For PAID mutations use
    x402_prepare/x402_submit (the caller signs)."""
    # Same fail-closed finalize guard as x402, in case a finalize is ever routed
    # through the direct Labs endpoint instead of the x402 gateway.
    if "finishCreateOrUpdateFileV2" in query:
        assert_confidential_finalize_ok(variables)
    return dump(labs_graphql_call(query, variables or {}, auth, labsUrl))


@mcp.tool()
def s3_upload(
    uploadUrl: str,
    filePath: str,
    method: str = "PUT",
    contentType: str = "application/pdf",
    headers: dict | None = None,
) -> str:
    """PUT (or POST) a local file to a presigned S3 URL, applying all headers
    returned by the initiate step plus Content-Type. No wallet / no payment. Used for
    the cover image, public file upload (Step B), and the encrypted ciphertext (E3).
    Returns {status, ok}."""
    data = Path(filePath).read_bytes()
    assert_not_confidential_plaintext(filePath, data)
    all_headers = {"Content-Type": contentType, **(headers or {})}
    resp = _client.request(method.upper(), uploadUrl, headers=all_headers, content=data)
    if resp.status_code >= 400:
        raise ToolError(f"S3 upload failed ({resp.status_code}): {resp.text[:300]}")
    return dump({"status": resp.status_code, "ok": resp.is_success})


# ---- DEK-aware tools (plaintext DEK never leaves the server) -------------


@mcp.tool()
def labs_generate_dek(
    auth: LabsAuth = "service-token",
    labsUrl: str | None = None,
) -> str:
    """Call generateDataEncryptionKey (direct, service-token) and KEEP the plaintext
    DEK inside this server. Returns {encryptedDek, encryptionSystem, dekHandle} — pass
    dekHandle to encrypt_file. The plaintext DEK is NEVER returned to the agent. This
    is a service-token HTTP call (no wallet signature, no payment); the DEK stays
    in-process. (generateDataEncryptionKey is also x402-whitelisted, but keep it direct
    here so no payment is spent on a key fetch.)"""
    query = (
        "mutation GenerateDataEncryptionKey { generateDataEncryptionKey { isSuccess "
        "plaintextDEK encryptedDek encryptionSystem error { message code retryable } } }"
    )
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
    ipnftUid: str | None = None,
    tokenUri: str | None = None,
    agreementUrl: str | None = None,
    auth: LabsAuth = "service-token",
    labsUrl: str | None = None,
    serviceToken: str | None = None,
    walletAddress: str | None = None,
) -> str:
    """Call decryptDataKey (direct, service-token — the backend evaluates the
    on-chain access conditions for the caller) and KEEP the plaintext DEK inside this
    server. Returns {iv, dekHandle, message} — pass dekHandle to decrypt_file.

    The decryptDataKey mutation (encryption.graphql) accepts ipnftUid + filePath
    (a data-room file, format {contractAddress}_{tokenId}) or tokenUri + agreementUrl
    (an IPFS agreement). For a data-room file pass ipnftUid + filePath. ACCESS_DENIED
    means the caller (the service token's adminAddress) fails the on-chain condition;
    LEGACY_ENCRYPTION means the file predates the envelope flow. Pass serviceToken /
    walletAddress to act as a specific authorized wallet without swapping env. This is
    a service-token HTTP call — no wallet signature, no payment."""
    if not ipnftUid and not tokenUri:
        raise ToolError("Provide ipnftUid (data-room file) or tokenUri (IPFS agreement).")
    arg_decls, arg_uses, variables = [], [], {}
    if ipnftUid:
        arg_decls.append("$ipnftUid: String")
        arg_uses.append("ipnftUid: $ipnftUid")
        variables["ipnftUid"] = ipnftUid
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
    r = labs_graphql_call(
        query, variables, auth, labsUrl, service_token=serviceToken, wallet_address=walletAddress
    )
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


# ---- Crypto / encoding (pure compute) ------------------------------------


@mcp.tool()
def encrypt_file(filePath: str, dekHandle: str, outPath: str) -> str:
    """AES-256-GCM envelope-encrypt a file exactly like the Labs client
    encryptFileWithKms: random 12-byte IV, 16-byte tag APPENDED to the ciphertext.
    Pass dekHandle from labs_generate_dek (the plaintext DEK never enters the
    conversation). Writes ciphertext to outPath. Returns
    {iv (base64), contentHash (hex SHA-256 of plaintext), cipherBytes}."""
    result = encrypt_file_impl(filePath, get_dek(dekHandle), outPath)
    # Arm the fail-closed latch: this file is now confidential, so its plaintext
    # can never be S3-uploaded by this process (no public-upload fallback).
    mark_confidential_plaintext(filePath, result["contentHash"])
    return dump(result)


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
def abi_encode(functionSignature: str, args: list) -> str:
    """ABI-encode a Solidity function call to calldata (pure compute — no signing).
    functionSignature is e.g. 'mintReservation(address,uint256,string,string,bytes)'
    or 'safeTransferFrom(address,address,uint256)'. Pass args in order: uint*/int* as
    decimal strings or ints; bytes/bytesN as 0x-prefixed hex (a non-0x string is
    rejected, NOT silently UTF-8 encoded); address as 0x + 40 hex; string as text.
    Returns {calldata} — pass it as `data` to prepare_transaction, then sign/send with
    your wallet."""
    return dump({"calldata": abi_encode_impl(functionSignature, args)})


@mcp.tool()
def build_access_conditions(
    reservationId: str,
    mode: Literal["ipnft-signer"] = "ipnft-signer",
    accessResolverAddress: str | None = None,
    chain: str | None = None,
    chainId: str | None = None,
) -> str:
    """Build the on-chain accessControlConditions array for an encrypted V2 upload, and
    return both the array and its JSON-stringified string (ready for
    encryptionMetadata.accessControlConditions). mode='ipnft-signer' (the only V2 gate):
    isAuthorizedSignerForIpnft(:userAddress, reservationId), where reservationId is the
    IP-NFT tokenId (the {tokenId} part of an ipnftUid). Chain is derived from CHAIN_ID
    (1->ethereum, 11155111->sepolia, 8453->base, 84532->baseSepolia) unless overridden;
    accessResolverAddress defaults to $ACCESS_RESOLVER_ADDRESS. This replicates the
    bruno v2 encrypted-upload condition and aura's createAuthorizedIpnftSignerCondition."""
    resolver = accessResolverAddress or env("ACCESS_RESOLVER_ADDRESS")
    if not resolver:
        raise ToolError("accessResolverAddress not given and ACCESS_RESOLVER_ADDRESS is not set.")
    if not reservationId:
        raise ToolError("mode 'ipnft-signer' requires reservationId (the IP-NFT tokenId).")
    cid = int(chainId or env("CHAIN_ID") or 0)
    if not cid:
        raise ToolError("chainId not given and CHAIN_ID is not set.")
    ch = chain or _chain_for_caip_chain_id(cid)
    conditions = _ipnft_signer_condition(reservationId, ch, resolver)
    # Arm the fail-closed latch: this IP-NFT is now confidential, so it can never
    # be finalized PUBLIC / without encryptionMetadata by this process.
    mark_confidential_token_id(reservationId)
    return dump({"conditions": conditions, "json": json.dumps(conditions, separators=(",", ":"))})


# ---- Service token: PREPARE the sign-in message, then EXCHANGE the caller's
#      signature for the JWT. The server signs nothing. ---------------------


@mcp.tool()
def service_signin_message(
    walletAddress: str | None = None,
    serviceName: str = "data-sync-service",
    labsUrl: str | None = None,
) -> str:
    """Step 1/2 of issuing a Labs JWT service token (off-chain credential — NOT an
    on-chain mint). Fetch getServiceSignInMessage for the wallet that will own the
    token's access (its address becomes the token's adminAddress, which the decrypt
    evaluator substitutes into isAuthorizedSignerForIpnft). Returns {message,
    walletAddress, serviceName}. Have YOUR wallet personal_sign (EIP-191) the exact
    `message` (Privy agentic wallet recommended; or any key bound to walletAddress),
    then call service_token_create with that signature. This server does NOT sign."""
    addr = resolve_address(walletAddress)
    msg = labs_graphql_call(
        "query GetServiceSignInMessage($walletAddress: String!, $serviceName: String!) "
        "{ getServiceSignInMessage(walletAddress: $walletAddress, serviceName: $serviceName) { message } }",
        {"walletAddress": addr, "serviceName": serviceName},
        "none",
        labsUrl,
    )
    message = ((msg.get("data") or {}).get("getServiceSignInMessage") or {}).get("message")
    if not message:
        raise ToolError(
            f"getServiceSignInMessage returned no message: {json.dumps(msg.get('errors') or msg)[:300]}"
        )
    return dump(
        {
            "message": message,
            "walletAddress": addr,
            "serviceName": serviceName,
            "next": (
                "personal_sign this exact message with YOUR wallet (the one bound to "
                f"{addr}), then call service_token_create(walletAddress, messageSignature)."
            ),
        }
    )


@mcp.tool()
def service_token_create(
    walletAddress: str,
    messageSignature: str,
    serviceName: str = "data-sync-service",
    expiresIn: str = "720h",
    labsUrl: str | None = None,
) -> str:
    """Step 2/2 of issuing a Labs JWT service token: exchange the caller's
    personal_sign signature of the service_signin_message for the token via
    generateServiceToken. The backend verifies the signature against walletAddress and
    binds the token's adminAddress to it. Returns {token, tokenId, expiresAt} — set
    `token` as MOLECULE_SERVICE_TOKEN (secret; this server never logs it), or pass it
    per-call via labs_decrypt_dek(serviceToken=...) to act as that wallet. This server
    does NOT sign — `messageSignature` must come from the caller's wallet."""
    tok = labs_graphql_call(
        "mutation GenerateServiceToken($serviceName: String!, $expiresIn: String!, $walletAddress: String, $messageSignature: String) "
        "{ generateServiceToken(serviceName: $serviceName, expiresIn: $expiresIn, walletAddress: $walletAddress, messageSignature: $messageSignature) "
        "{ token tokenId serviceName expiresAt isSuccess message } }",
        {
            "serviceName": serviceName,
            "expiresIn": expiresIn,
            "walletAddress": walletAddress,
            "messageSignature": messageSignature,
        },
        "none",
        labsUrl,
    )
    result = (tok.get("data") or {}).get("generateServiceToken")
    if not result or not result.get("isSuccess") or not result.get("token"):
        raise ToolError(f"generateServiceToken failed: {json.dumps(result or tok.get('errors'))[:300]}")
    return dump(
        {
            "token": result.get("token"),
            "tokenId": result.get("tokenId"),
            "expiresAt": result.get("expiresAt"),
        }
    )


# --------------------------------------------------------------------------
# boot
# --------------------------------------------------------------------------


def main() -> None:
    log("molecule-mcp ready (stdio) — custody-free: crafts payloads, never signs")
    mcp.run()


if __name__ == "__main__":
    main()
