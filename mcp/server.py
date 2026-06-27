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
#   "eth-account>=0.13",
# ]
# ///
"""molecule-mcp — stdio MCP server for the Molecule DeSci skills.

Replaces every ``curl`` / ``http_request`` / ``node -e`` step in the
``aura-orchestrator`` skill (public and private/encrypted data-room uploads)
with a typed MCP tool, so the agent calls one tool per operation instead of
hand-assembling shell commands.

Written in Python (FastMCP) and run over stdio so it works under any
MCP-capable harness (Claude Code, Codex, …) with only a Python interpreter —
no Bun/Node required.

This server targets the **OCL / V3 GraphQL surface** (the current production model),
keyed on ``oclId`` (a bytes32 ``0x`` + 64 hex), on the OCL chain (Base / Base Sepolia).
A lab is an On-Chain Lab: a LabNFT + its token-bound account (TBA). The legacy IP-NFT
surface (``ipnftUid``, ``mintReservation``, ``isAuthorizedSignerForIpnft``, Proof-of-
Invention) is intentionally NOT supported here — on mainnet there is no IP-NFT.

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
from eth_abi import decode as abi_decode_values
from eth_abi import encode as abi_encode_values
from eth_utils import function_signature_to_4byte_selector, keccak

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
            + ". Set them in the project-base .claude/settings.json (non-secrets) or "
            ".claude/settings.local.json (secrets), then reload the MCP (/mcp). "
            "Run the config_doctor tool to see what is set and which file it loaded from."
        )
    return out


# --------------------------------------------------------------------------
# config bootstrap — load env from the PROJECT-BASE .claude, never global.
#
# A stdio MCP server only sees os.environ as injected by the launching harness.
# When the active project root is a SUBDIRECTORY of where the workspace's
# .claude/settings*.json live (here: launched from molecule-plugin/ while the
# config — secrets included — sits one level up in molecule_core/.claude), the
# harness injects nothing and every tool dies on "Missing required environment
# variable(s)". So at import time we resolve ONE project base — the nearest
# ancestor .claude dir (of CLAUDE_PLUGIN_ROOT / this file / the CWD) that
# actually carries an `env` block — and load its settings.json + settings.local.json.
# The user's global ~/.claude is deliberately EXCLUDED: config comes from the
# project base you run in, not from global settings. Precedence:
# real process env > base settings.local.json (secrets) > base settings.json.
# We never overwrite a var already in the environment, and never log values.
# --------------------------------------------------------------------------

_PREEXISTING_ENV_KEYS: set[str] = set()  # keys present before bootstrap (harness-injected)
_ENV_SOURCES: dict[str, str] = {}  # var -> file it was loaded from (bootstrapped vars only)
_CONFIG_BASE: str | None = None  # the resolved project-base .claude dir
_CONFIG_FILES_LOADED: list[str] = []  # settings files actually loaded from the base


def _read_settings_env(path: Path) -> dict[str, str]:
    """Return the non-empty `env` block of a .claude/settings*.json ({} if absent/empty/bad)."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except Exception as e:  # malformed JSON, permissions, …
        log(f"[config] skipping unreadable {path}: {e}")
        return {}
    block = data.get("env") if isinstance(data, dict) else None
    if isinstance(block, dict):
        return {k: str(v) for k, v in block.items() if v is not None and v != ""}
    return {}


def _candidate_claude_dirs() -> list[Path]:
    """Ancestor .claude dirs (nearest first) of the run dir / this file / CWD,
    EXCLUDING the global ~/.claude — config must come from the project base."""
    try:
        home_claude: Path | None = (Path.home() / ".claude").resolve()
    except Exception:
        home_claude = None
    starts: list[Path] = []
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        starts.append(Path(plugin_root))
    for getter in (lambda: Path(__file__).resolve().parent, lambda: Path.cwd()):
        try:
            starts.append(getter())
        except Exception:
            pass
    out: list[Path] = []
    seen: set[Path] = set()
    for start in starts:
        try:
            chain = [start.resolve(), *start.resolve().parents]
        except Exception:
            continue
        for d in chain:
            cd = d / ".claude"
            try:
                rcd = cd.resolve()
            except Exception:
                continue
            if rcd in seen:
                continue
            if home_claude is not None and rcd == home_claude:
                continue  # never the global config
            try:
                if cd.is_dir():
                    seen.add(rcd)
                    out.append(cd)
            except OSError:
                pass
    out.sort(key=lambda p: len(p.resolve().parts), reverse=True)  # nearest (deepest) first
    return out


def _bootstrap_env() -> None:
    """Resolve the project base and fill os.environ from its .claude settings."""
    global _CONFIG_BASE
    _PREEXISTING_ENV_KEYS.update(os.environ.keys())
    for cd in _candidate_claude_dirs():
        local_env = _read_settings_env(cd / "settings.local.json")
        shared_env = _read_settings_env(cd / "settings.json")
        if not local_env and not shared_env:
            continue  # not a config-bearing base; keep walking up
        _CONFIG_BASE = str(cd)
        filled: list[str] = []
        # settings.local.json (secrets) wins over settings.json within the base;
        # neither ever overwrites a var already set by the harness/process.
        for fname, block in (("settings.local.json", local_env), ("settings.json", shared_env)):
            if not block:
                continue
            _CONFIG_FILES_LOADED.append(str(cd / fname))
            for k, v in block.items():
                if k in os.environ:
                    continue
                os.environ[k] = v
                _ENV_SOURCES[k] = str(cd / fname)
                filled.append(k)
        log(
            f"[config] project base {cd}: loaded {len(filled)} env var(s)"
            + (f" ({', '.join(sorted(filled))})" if filled else "")
        )
        return  # only the nearest config-bearing base is used
    log("[config] no project-base .claude with an env block found (global ~/.claude excluded)")


_bootstrap_env()


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


# Cache the Privy wallet's on-chain address (the operating signer) per wallet id —
# resolved via a Privy GET, so cache it to avoid an API round-trip on every call.
_operating_address_cache: dict[str, str] = {}


def _privy_wallet_address(wallet_id: str) -> str:
    cached = _operating_address_cache.get(wallet_id)
    if cached:
        return cached
    auth, headers = _privy_auth()
    resp = _client.get(f"{PRIVY_BASE_URL}/v1/wallets/{wallet_id}", auth=auth, headers=headers)
    j = _json_or_none(resp)
    if resp.status_code >= 400 or not (j and j.get("address")):
        raise ToolError(
            f"Could not resolve wallet address ({resp.status_code}): {resp.text[:300]}"
        )
    _operating_address_cache[wallet_id] = j["address"]
    return j["address"]


def get_wallet_address(wallet_id: str | None = None) -> str:
    # Operating identity = the Privy wallet that actually SIGNS (mint, x402, uploads,
    # the agent's service-token calls). Resolve its REAL on-chain address first, so the
    # agent can differ from the owner/recipient EOA (EVM_WALLET_ADDRESS) — required for a
    # genuine hand-off where the LabNFT is transferred to a distinct EOA that then
    # decrypts. EVM_WALLET_ADDRESS is the owner/recipient EOA (the Phase-5 target), NOT
    # the operating signer, so it must NOT shadow the Privy address here (doing so pins
    # the x402 `from` and mint recipient to the wrong wallet). Fall back to
    # EVM_WALLET_ADDRESS only when no Privy wallet is configured (raw-EOA signing flows).
    wid = wallet_id or env("PRIVY_WALLET_ID")
    if wid:
        return _privy_wallet_address(wid)
    from_env = env("EVM_WALLET_ADDRESS")
    if from_env:
        return from_env
    raise ToolError(
        "No operating wallet available: set PRIVY_WALLET_ID (Privy agent) or "
        "EVM_WALLET_ADDRESS (raw EOA)."
    )


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
# building on-chain access conditions for its OCL lab) we refuse, for the rest
# of this process, to (a) S3-upload that file's plaintext bytes or (b) finalize
# that lab's file as PUBLIC / without encryptionMetadata. Process-local and
# NON-overridable — there is deliberately no force flag, because the whole
# point is that an agent under "just finish the upload" pressure cannot opt out.
#
# Known limits (disclosed, not bugs): the latch lives in memory, so an MCP
# subprocess restart clears it — but a restart also wipes the DEK store and
# breaks the run, forcing a re-run from a clean state. It is keyed on the exact
# plaintext bytes, so re-encoding the plaintext to different bytes before upload
# would evade the hash check — the per-oclId finalize guard still covers that
# molecule.
# --------------------------------------------------------------------------

_confidential_plaintext_hashes: set[str] = set()  # hex sha256 of plaintext the agent encrypted
_confidential_plaintext_paths: set[str] = set()  # resolved abs paths passed to encrypt_file
_confidential_ocl_ids: set[str] = set()  # OCL oclIds that got on-chain access conditions


def mark_confidential_plaintext(file_path: str, plaintext_sha256_hex: str) -> None:
    """Latch a file as confidential once the agent encrypts it. Its plaintext
    may never again be S3-uploaded by this process."""
    _confidential_plaintext_hashes.add(plaintext_sha256_hex.lower())
    try:
        _confidential_plaintext_paths.add(str(Path(file_path).resolve()))
    except OSError:
        pass


def mark_confidential_ocl_id(ocl_id: str | None) -> None:
    """Latch an OCL oclId as confidential once on-chain access conditions are
    built for it. A file under it may never be finalized PUBLIC / unencrypted."""
    if ocl_id and str(ocl_id).strip():
        _confidential_ocl_ids.add(str(ocl_id).strip().lower())


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
    """Fail-closed gate for finishCreateOrUpdateFile: if on-chain access
    conditions were built for this lab's oclId, refuse a PUBLIC / unencrypted
    finalize."""
    v = variables or {}
    ocl_id = str(v.get("oclId") or "").strip().lower()
    if not ocl_id or ocl_id not in _confidential_ocl_ids:
        return
    access_level = str(v.get("accessLevel") or "").upper()
    has_enc_meta = bool(v.get("encryptionMetadata"))
    if access_level == "PUBLIC" or not has_enc_meta:
        raise ToolError(
            f"PRIVACY GUARD (non-overridable): refusing to finalize a file for OCL lab "
            f"oclId {ocl_id} with accessLevel={access_level or 'MISSING'} / "
            f"encryptionMetadata={'present' if has_enc_meta else 'MISSING'}. On-chain "
            "access conditions were built for this lab (a confidential upload), so it "
            "must be finalized with a non-PUBLIC accessLevel AND encryptionMetadata. Do "
            "NOT fall back to the public path — abort and report the failure."
        )


# --------------------------------------------------------------------------
# Labs GraphQL (direct)
# --------------------------------------------------------------------------

LabsAuth = Literal["service-token", "api-key", "none"]


def _labs_headers(
    auth: LabsAuth,
    service_token: str | None = None,
    wallet_address: str | None = None,
) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    # The Labs endpoint is AppSync in API_KEY auth mode: the x-api-key transport
    # gate applies to EVERY request, regardless of the logical auth layer above it
    # — service-token identity calls AND the unauthenticated 'none' sign-in queries
    # that issue_service_token uses (getServiceSignInMessage / generateServiceToken).
    # Without x-api-key the gateway 401s before the resolver runs. So attach it
    # whenever it is configured; the identity headers below layer on top.
    api_key = env("MOLECULE_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    if auth == "service-token":
        # Per-call overrides let the agent act as any authorized wallet (e.g.
        # decrypt as the OWNER wallet) without swapping env / reloading. The
        # resolver reads these for identity; the x-api-key above passes the gate.
        token = service_token or env("MOLECULE_SERVICE_TOKEN")
        # Default x-wallet-address to the OPERATING (Privy agent) wallet so it matches
        # MOLECULE_SERVICE_TOKEN's adminAddress. To act as a DIFFERENT wallet — e.g.
        # decrypt as the owner EOA after the hand-off — pass walletAddress + serviceToken
        # overrides bound to that wallet. (Previously this defaulted to EVM_WALLET_ADDRESS,
        # which broke once EVM_WALLET_ADDRESS became the distinct owner EOA.)
        addr = wallet_address or get_wallet_address()
        if not token or not addr:
            raise ToolError(
                "service-token auth needs MOLECULE_SERVICE_TOKEN + an operating wallet "
                "(PRIVY_WALLET_ID / EVM_WALLET_ADDRESS), or serviceToken / walletAddress overrides."
            )
        headers["x-service-token"] = token
        headers["x-wallet-address"] = addr
    elif auth == "api-key" and not api_key:
        require_env("MOLECULE_API_KEY")  # raise the standard missing-env error
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
# x402 payment flow (P1–P7) in one call:
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
    # Fail-closed: never let a confidential lab's file be finalized as a public /
    # plaintext file, even if the agent reaches this with the wrong variables.
    if mutation == "finishCreateOrUpdateFile":
        assert_confidential_finalize_ok(variables)
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

    # P3 — wallet address. EIP-3009 requires the authorization `from` to be the
    # address whose key signs. Privy always signs with the wallet's own key, so a
    # stale EVM_WALLET_ADDRESS that differs from the Privy wallet produces a
    # signature the facilitator recovers to a different signer and rejects with a
    # generic "Payment verification failed". Catch that here with a clear message.
    wallet_address = get_wallet_address(wid)
    _auth, _phdr = _privy_auth()
    _wj = _json_or_none(_client.get(f"{PRIVY_BASE_URL}/v1/wallets/{wid}", auth=_auth, headers=_phdr))
    signer_address = (_wj or {}).get("address")
    if signer_address and wallet_address.lower() != signer_address.lower():
        raise ToolError(
            f"x402 payment would be rejected: the EIP-3009 `from` ({wallet_address}) "
            f"does not match the Privy signing wallet {wid} ({signer_address}). The "
            f"facilitator recovers the signer from the signature and fails verification "
            f"when signer != from. Set EVM_WALLET_ADDRESS to {signer_address}, or point "
            f"PRIVY_WALLET_ID at the {wallet_address} wallet."
        )

    # P4 — nonce, validAfter, validBefore.
    now = int(time.time())
    nonce = "0x" + secrets.token_hex(32)
    valid_after = str(now - 600)
    valid_before = str(now + max_timeout)
    chain_id = _chain_id_from_network(network)

    # P5 — EIP-712 TransferWithAuthorization signed by the Privy wallet.
    # NOTE: Privy's wallet-RPC typed_data schema is snake_case all the way down —
    # the primary type field is `primary_type`, not the EIP-712 `primaryType`
    # (see EthereumSignTypedDataRpcInput.Params.TypedData in @privy-io/node). The
    # API rejects camelCase `primaryType` with a 400.
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
        "primary_type": "TransferWithAuthorization",
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
# AES-256-GCM envelope crypto — the data-room envelope format
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


# AccessResolver chain identifiers the backend evaluator expects — kebab-case,
# network first for testnets ('sepolia-base'), NOT Lit's camelCase.
def _chain_for_caip_chain_id(chain_id: int) -> str:
    return {1: "ethereum", 11155111: "sepolia", 8453: "base", 84532: "sepolia-base"}.get(
        chain_id
    ) or _raise(ToolError(f"Unmapped chainId {chain_id} for access conditions."))


def _raise(exc: Exception):
    raise exc


# OCL access roles (uint8) — AccessResolver role constants.
_OCL_ROLES = {"viewer": 1, "contributor": 2}


def _lab_signer_condition(ocl_id: str, role: int, chain: str, resolver: str) -> dict:
    # Mirrors createAuthorizedLabSignerCondition: hasRole(oclId, account, role).
    return {
        "chain": chain,
        "conditionType": "evmContract",
        "contractAddress": resolver,
        "functionName": "hasRole",
        "functionParams": [ocl_id, ":userAddress", str(role)],
        "functionAbi": {
            "name": "hasRole",
            "inputs": [
                {"internalType": "bytes32", "name": "oclId", "type": "bytes32"},
                {"internalType": "address", "name": "account", "type": "address"},
                {"internalType": "uint8", "name": "role", "type": "uint8"},
            ],
            "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
            "stateMutability": "view",
            "type": "function",
        },
        "returnValueTest": {"comparator": "=", "key": "", "value": "true"},
    }


def _tba_owner_condition(lab_account: str, chain: str, resolver: str) -> dict:
    # Mirrors createAuthorizedTbaOwnerCondition: isAuthorizedSignerForTba(signer, account).
    return {
        "chain": chain,
        "conditionType": "evmContract",
        "contractAddress": resolver,
        "functionName": "isAuthorizedSignerForTba",
        "functionParams": [":userAddress", lab_account],
        "functionAbi": {
            "name": "isAuthorizedSignerForTba",
            "inputs": [
                {"internalType": "address", "name": "signer", "type": "address"},
                {"internalType": "address", "name": "account", "type": "address"},
            ],
            "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
            "stateMutability": "view",
            "type": "function",
        },
        "returnValueTest": {"comparator": "=", "key": "", "value": "true"},
    }


def _lab_access_conditions(
    ocl_id: str, lab_account: str, role: int, chain: str, resolver: str
) -> list[dict]:
    # A CONTRIBUTOR-or-better role OR the LabNFT-backed TBA owner.
    # The backend AccessResolver evaluates left-to-right.
    return [
        _lab_signer_condition(ocl_id, role, chain, resolver),
        {"operator": "or"},
        _tba_owner_condition(lab_account, chain, resolver),
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
    get_wallet_address / 'resolve the wallet address' curl."""
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
def privy_send_transaction(
    to: str,
    data: str | None = None,
    value: str | None = None,
    chainId: str | None = None,
    walletId: str | None = None,
) -> str:
    """Send a transaction from the Privy wallet (eth_sendTransaction with
    caip2 eip155:<chainId>). Used for the LabNFT mint (mintAndCreateAccount, with
    value=mintFeeWei) and AccessResolver grantRole. value is decimal wei (string).
    Returns {txHash}."""
    wid = resolve_wallet_id(walletId)
    cid = chainId or env("CHAIN_ID")
    if not cid:
        raise ToolError("chainId not provided and CHAIN_ID is not set.")
    transaction: dict[str, Any] = {"to": to}
    if data:
        transaction["data"] = data
    if value:
        # Privy's transaction.value must be hex-encoded wei ("0x…"); the tool's
        # documented input is decimal wei, so convert (and pass hex through).
        v = str(value).strip()
        transaction["value"] = v if v.startswith("0x") else hex(int(v))
    res = privy_rpc(
        wid,
        {"method": "eth_sendTransaction", "caip2": f"eip155:{cid}", "params": {"transaction": transaction}},
    )
    data_obj = (res or {}).get("data", {}) or {}
    tx_hash = data_obj.get("hash") or data_obj.get("transaction_hash") or (res or {}).get("hash")
    if not tx_hash:
        raise ToolError(f"No tx hash returned. Raw: {json.dumps(res)[:400]}")
    return dump({"txHash": tx_hash})


# Public fallback RPC endpoints by chain id, used by privy_send_raw_transaction
# when neither rpcUrl nor EVM_RPC_URL is provided.
_DEFAULT_RPC_BY_CHAIN: dict[str, str] = {
    "84532": "https://sepolia.base.org",  # Base Sepolia — the OCL canonical chain
    "8453": "https://mainnet.base.org",  # Base mainnet
    "11155111": "https://ethereum-sepolia-rpc.publicnode.com",  # Sepolia L1 (legacy)
}


def _chain_rpc(rpc_url: str, method: str, params: list[Any]) -> Any:
    """Minimal JSON-RPC call against an EVM node (live nonce + raw broadcast)."""
    resp = _client.post(
        rpc_url,
        headers={"content-type": "application/json"},
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}),
    )
    j = _json_or_none(resp)
    if resp.status_code >= 400 or not isinstance(j, dict) or j.get("error") or "result" not in j:
        err = j.get("error") if isinstance(j, dict) else None
        raise ToolError(
            f"EVM RPC {method} failed ({resp.status_code}): "
            + (json.dumps(err) if err else resp.text[:300])
        )
    return j["result"]


@mcp.tool()
def privy_send_raw_transaction(
    to: str,
    data: str | None = None,
    value: str | None = None,
    chainId: str | None = None,
    walletId: str | None = None,
    rpcUrl: str | None = None,
    gasLimit: str | None = None,
    maxFeePerGas: str | None = None,
    maxPriorityFeePerGas: str | None = None,
) -> str:
    """Sign with Privy (eth_signTransaction, SIGN-ONLY) then broadcast the raw tx
    yourself via eth_sendRawTransaction against an EVM RPC. Use this for the LabNFT
    `safeTransferFrom` (hand-off to a new owner): Privy's eth_sendTransaction returns a
    hash but never broadcasts safeTransferFrom for the agent wallet — the "phantom
    hash" — even though mint/grantRole broadcast fine, so keep using privy_send_transaction
    for those. (Never transfer a LabNFT to its own bound TBA — the contract reverts.)
    Resolves the live `pending` nonce so the call is re-runnable. rpcUrl falls back to
    EVM_RPC_URL, then a public node for known chains. value is decimal wei (or 0x hex).
    Gas is auto-estimated (eth_estimateGas ×1.2) unless gasLimit is passed; EIP-1559 fees
    default to 5/2 gwei. Returns {txHash, nonce, from, gasLimit}."""
    wid = resolve_wallet_id(walletId)
    cid = str(chainId or env("CHAIN_ID") or "")
    if not cid:
        raise ToolError("chainId not provided and CHAIN_ID is not set.")
    rpc = rpcUrl or env("EVM_RPC_URL") or _DEFAULT_RPC_BY_CHAIN.get(cid)
    if not rpc:
        raise ToolError(
            f"No EVM RPC endpoint for chainId {cid}. Pass rpcUrl or set EVM_RPC_URL."
        )

    # Resolve the SENDER (signer) address from the Privy wallet record directly.
    # Do NOT use get_wallet_address(): it prefers EVM_WALLET_ADDRESS, which in the
    # aura flow is the transfer RECIPIENT, not the sender — that would query the
    # wrong account's nonce.
    auth, headers = _privy_auth()
    wj = _json_or_none(_client.get(f"{PRIVY_BASE_URL}/v1/wallets/{wid}", auth=auth, headers=headers))
    sender = (wj or {}).get("address")
    if not sender:
        raise ToolError(f"Could not resolve signer address for Privy wallet {wid}.")

    # Live pending nonce keeps the tool re-runnable after a stuck/failed attempt.
    nonce = int(_chain_rpc(rpc, "eth_getTransactionCount", [sender, "pending"]), 16)

    val = "0x0"
    if value:
        v = str(value).strip()
        val = v if v.startswith("0x") else hex(int(v))

    # Gas limit: an explicit override wins; otherwise estimate with a 20% buffer.
    # A flat default is unsafe — a transfer is ~51k but mintAndCreateAccount (mint +
    # TBA provisioning) is much heavier, so a fixed cap silently reverts OUT-OF-GAS.
    # Note that an eth_call simulation can still PASS in that window (it assumes a
    # high gas cap), so it is a misleading signal — eth_estimateGas is the real check.
    if gasLimit:
        gas_limit_hex = gasLimit
    else:
        est_call: dict[str, Any] = {"from": sender, "to": to, "value": val}
        if data:
            est_call["data"] = data
        try:
            est = int(_chain_rpc(rpc, "eth_estimateGas", [est_call]), 16)
            gas_limit_hex = hex(est * 12 // 10)  # ×1.2 buffer
        except ToolError:
            gas_limit_hex = "0x61a80"  # 400000 fallback (covers mint ~176k + transfers)

    transaction: dict[str, Any] = {
        "to": to,
        "value": val,
        "chain_id": int(cid),
        "nonce": nonce,
        # EIP-1559 fee defaults from privy_transfer_v5.sh (5 gwei / 2 gwei);
        # override per chain congestion via the params above.
        "max_fee_per_gas": maxFeePerGas or "0x12a05f200",
        "max_priority_fee_per_gas": maxPriorityFeePerGas or "0x77359400",
        "gas_limit": gas_limit_hex,
        "type": 2,
    }
    if data:
        transaction["data"] = data

    res = privy_rpc(
        wid,
        {"chain_type": "ethereum", "method": "eth_signTransaction", "params": {"transaction": transaction}},
    )
    signed = ((res or {}).get("data") or {}).get("signed_transaction")
    if not signed:
        raise ToolError(f"Privy returned no signed_transaction. Raw: {json.dumps(res)[:400]}")

    tx_hash = _chain_rpc(rpc, "eth_sendRawTransaction", [signed])
    return dump({"txHash": tx_hash, "nonce": nonce, "from": sender, "gasLimit": gas_limit_hex})


# ---- Molecule HTTP -------------------------------------------------------


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
    (private/encrypted upload). auth='none' for public sign-in queries. Returns {data, errors}.
    Do NOT use for generateDataEncryptionKey/decryptDataKey — use
    labs_generate_dek/labs_decrypt_dek so the plaintext DEK stays inside the server."""
    # Same fail-closed finalize guard as x402_pay, in case the finalize is ever
    # routed through the direct Labs endpoint instead of the x402 gateway.
    if "finishCreateOrUpdateFile" in query:
        assert_confidential_finalize_ok(variables)
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
    Whitelisted mutations: initiateCreateOrUpdateFile,
    finishCreateOrUpdateFile, createAnnouncement, createLab, generateDataEncryptionKey,
    decryptDataKey (any other mutation 400s with 'not enabled for x402 gateway'). Send the
    TOP-LEVEL AppSync mutations (not the nested molecule.v3.project(oclId) Kamu documents).
    All data-room args are keyed on oclId (the lab's bytes32 id)."""
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
    assert_not_confidential_plaintext(filePath, data)
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
    (default, service-token) is the recommended path — it needs no payment and keeps
    the DEK in-process. generateDataEncryptionKey IS now x402-whitelisted (mutations.ts),
    so transport='x402' also works, but prefer 'direct' for DEK generation."""
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
    serviceToken: str | None = None,
    walletAddress: str | None = None,
) -> str:
    """Call decryptDataKey (the backend evaluates access for the caller) and KEEP the
    plaintext DEK inside this server. Returns {iv, dekHandle, message} — pass dekHandle
    to decrypt_file.

    decryptDataKey (encryption.graphql) accepts oclId + filePath (a data-room file) or
    tokenUri + agreementUrl (an IPFS agreement). For a data-room file pass oclId + filePath.
    Access is TWO-GATE: (a) the caller's service-token adminAddress must hold >=Viewer role
    on the lab (DB authorizeViewer(adminAddress, oclId)); (b) the stored on-chain
    accessControlConditions are evaluated against that adminAddress — passing via
    hasRole(oclId, addr, CONTRIBUTOR) OR isAuthorizedSignerForTba(addr, labAccountAddress)
    (the LabNFT/TBA owner). ACCESS_DENIED means one of these failed; LEGACY_ENCRYPTION means
    the file predates the envelope flow. decryptDataKey IS x402-whitelisted, but
    transport='direct' (service-token) is recommended so the plaintext DEK stays
    in-process and no payment is needed."""
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
        r = labs_graphql_call(
            query, variables, auth, labsUrl,
            service_token=serviceToken, wallet_address=walletAddress,
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


# ---- Crypto / encoding ---------------------------------------------------


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
def abi_encode(functionSignature: str, args: list) -> str:
    """ABI-encode a Solidity function call to calldata. functionSignature is e.g.
    'mintAndCreateAccount(address)' (OnChainLabFactory — mint a LabNFT + create its TBA;
    send with value=mintFeeWei), 'createAccount(uint256)' (idempotent re-create for an
    existing tokenId), 'safeTransferFrom(address,address,uint256)' (transfer a LabNFT —
    never to the bound TBA), or 'grantRole(bytes32,address,uint8,uint64,bool)' (AccessResolver
    per-lab role grant: oclId, account, role(1=viewer,2=contributor), expiry, isAgent).
    Pass args in order: uint*/int* as decimal strings or ints; bytes/bytesN as 0x-prefixed
    hex (a non-0x string is rejected, NOT silently UTF-8 encoded); address as 0x + 40 hex;
    bool as true/false; string as text. Returns {calldata}."""
    return dump({"calldata": abi_encode_impl(functionSignature, args)})


# OclIdentityCreated(address indexed account, bytes32 indexed oclId,
#                    uint256 indexed tokenId, bytes32 salt, uint256 canonicalChainId)
_OCL_IDENTITY_TOPIC0 = "0x" + keccak(
    text="OclIdentityCreated(address,bytes32,uint256,bytes32,uint256)"
).hex()


def _abi_value_to_json(v: Any) -> Any:
    if isinstance(v, bytes):
        return "0x" + v.hex()
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return str(v)  # avoid JSON bigint precision loss
    return v


def _resolve_rpc(rpc_url: str | None, chain_id: str | None) -> str:
    cid = str(chain_id or env("CHAIN_ID") or "")
    rpc = rpc_url or env("EVM_RPC_URL") or (_DEFAULT_RPC_BY_CHAIN.get(cid) if cid else None)
    if not rpc:
        raise ToolError(
            f"No EVM RPC endpoint (chainId {cid or '?'}). Pass rpcUrl or set EVM_RPC_URL."
        )
    return rpc


@mcp.tool()
def ocl_read(
    functionSignature: str,
    to: str,
    args: list | None = None,
    returns: list[str] | None = None,
    rpcUrl: str | None = None,
    chainId: str | None = None,
) -> str:
    """Read-only on-chain view call (eth_call) — no signing, no spend. ABI-encodes
    `functionSignature` + `args`, calls contract `to`, and decodes the result using the
    Solidity types in `returns`. Use for the OCL resolve/identity reads:
    `mintFeeWei()` -> ['uint256'] (the LabNFT mint value); `oclIdOfToken(uint256)` ->
    ['bytes32']; `accountOfToken(uint256)` -> ['address'] (the TBA) on $ONCHAIN_LAB_FACTORY_ADDRESS;
    `ownerOf(uint256)` -> ['address'] on $LABNFT_ADDRESS; `hasRole(bytes32,address,uint8)` ->
    ['bool'] and `isAuthorizedSignerForTba(address,address)` -> ['bool'] on
    $ACCESS_RESOLVER_ADDRESS. `to` is the contract address; rpcUrl falls back to EVM_RPC_URL
    then a known public node. Returns {values: [...], raw}. uint values are decimal strings."""
    calldata = abi_encode_impl(functionSignature, args or [])
    rpc = _resolve_rpc(rpcUrl, chainId)
    raw = _chain_rpc(rpc, "eth_call", [{"to": to, "data": calldata}, "latest"])
    if not isinstance(raw, str) or not raw.startswith("0x"):
        raise ToolError(f"eth_call returned no data (reverted?): {raw!r}")
    ret_types = list(returns or [])
    values: list[Any] = []
    if ret_types and len(raw) > 2:
        decoded = abi_decode_values(ret_types, bytes.fromhex(raw[2:]))
        values = [_abi_value_to_json(v) for v in decoded]
    return dump({"values": values, "raw": raw})


@mcp.tool()
def ocl_tx_identity(txHash: str, rpcUrl: str | None = None, chainId: str | None = None) -> str:
    """Extract the OCL identity from a `mintAndCreateAccount` receipt: fetch the tx receipt
    and scan logs for `OclIdentityCreated(address account, bytes32 oclId, uint256 tokenId, …)`
    (all three indexed). Returns {tokenId, account, oclId, found}. `account` is the lab's
    token-bound account (labAccountAddress). If the event isn't present (found=False), recover
    the tokenId from the receipt and use `ocl_read oclIdOfToken/accountOfToken` as a fallback.
    Read-only; rpcUrl falls back to EVM_RPC_URL then a public node."""
    rpc = _resolve_rpc(rpcUrl, chainId)
    receipt = _chain_rpc(rpc, "eth_getTransactionReceipt", [txHash])
    if not isinstance(receipt, dict):
        raise ToolError(f"No receipt for tx {txHash} (not mined yet?).")
    for lg in receipt.get("logs") or []:
        topics = lg.get("topics") or []
        if len(topics) >= 4 and str(topics[0]).lower() == _OCL_IDENTITY_TOPIC0:
            account = "0x" + str(topics[1])[-40:]
            ocl_id = str(topics[2])
            token_id = int(str(topics[3]), 16)
            return dump(
                {"tokenId": str(token_id), "account": account, "oclId": ocl_id, "found": True}
            )
    return dump(
        {
            "found": False,
            "message": (
                "OclIdentityCreated not in receipt logs — recover tokenId from the receipt "
                "and use ocl_read oclIdOfToken(tokenId)/accountOfToken(tokenId)."
            ),
            "status": receipt.get("status"),
        }
    )


@mcp.tool()
def build_access_conditions(
    oclId: str,
    labAccountAddress: str,
    role: Literal["contributor", "viewer"] = "contributor",
    accessResolverAddress: str | None = None,
    chain: str | None = None,
    chainId: str | None = None,
) -> str:
    """Build the OCL on-chain accessControlConditions array for an encrypted upload, and
    return both the array and its JSON-stringified string (ready for
    encryptionMetadata.accessControlConditions). The array is an OR of two AccessResolver
    checks:
      1. hasRole(oclId, :userAddress, <role>)  — role-based access (default CONTRIBUTOR=2, VIEWER=1)
      2. isAuthorizedSignerForTba(:userAddress, labAccountAddress)  — the LabNFT/TBA owner
    `oclId` is the lab's bytes32 id (0x + 64 hex); `labAccountAddress` is its token-bound
    account (TBA). Chain is derived from CHAIN_ID (1->ethereum, 11155111->sepolia, 8453->base,
    84532->sepolia-base) unless overridden; accessResolverAddress defaults to
    $ACCESS_RESOLVER_ADDRESS (the V3 resolver on Base / Base Sepolia)."""
    resolver = accessResolverAddress or env("ACCESS_RESOLVER_ADDRESS")
    if not resolver:
        raise ToolError("accessResolverAddress not given and ACCESS_RESOLVER_ADDRESS is not set.")
    if not oclId:
        raise ToolError("oclId is required (the lab's bytes32 id, 0x + 64 hex).")
    if not labAccountAddress:
        raise ToolError("labAccountAddress is required (the lab's token-bound account / TBA).")
    role_int = _OCL_ROLES.get(role)
    if role_int is None:
        raise ToolError(f"role must be 'contributor' or 'viewer', got {role!r}.")
    cid = int(chainId or env("CHAIN_ID") or 0)
    if not cid:
        raise ToolError("chainId not given and CHAIN_ID is not set.")
    ch = chain or _chain_for_caip_chain_id(cid)
    conditions = _lab_access_conditions(oclId, labAccountAddress, role_int, ch, resolver)
    # Arm the fail-closed latch: this lab is now confidential, so a file under it
    # can never be finalized PUBLIC / without encryptionMetadata by this process.
    mark_confidential_ocl_id(oclId)
    return dump({"conditions": conditions, "json": json.dumps(conditions, separators=(",", ":"))})


# ---- service token bootstrap --------------------------------------------


@mcp.tool()
def issue_service_token(
    serviceName: str = "data-sync-service",
    expiresIn: str = "720h",
    walletAddress: str | None = None,
    walletId: str | None = None,
    labsUrl: str | None = None,
) -> str:
    """Issue a Labs JWT service token (off-chain credential — NOT an on-chain mint):
    getServiceSignInMessage -> personal_sign (Privy) -> generateServiceToken. Returns
    {token, tokenId, expiresAt} — set token as MOLECULE_SERVICE_TOKEN in
    settings.local.json. The token is a secret; this server never logs it. Prefer
    pre-setting MOLECULE_SERVICE_TOKEN over issuing per run."""
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


@mcp.tool()
def issue_owner_service_token(
    ownerPrivateKey: str | None = None,
    serviceName: str = "owner-data-access",
    expiresIn: str = "720h",
    labsUrl: str | None = None,
) -> str:
    """Issue a Labs JWT service token (off-chain credential — NOT an on-chain mint)
    bound to the OWNER (user's personal) wallet by signing getServiceSignInMessage
    with the owner's raw private key (WALLET_PRIVATE_KEY by default). Unlike
    issue_service_token (which signs via the Privy AGENT wallet), this binds the
    token to the owner EOA — required because
    decryptDataKey gates on the service token's adminAddress. Pass the returned
    `token` to labs_decrypt_dek(serviceToken=...) to decrypt as the owner. Returns
    {token, tokenId, address, expiresAt}. The private key never leaves this process."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except ImportError as e:  # pragma: no cover
        raise ToolError(f"eth-account is required for owner-key signing: {e}")
    pk = ownerPrivateKey or env("WALLET_PRIVATE_KEY")
    if not pk:
        raise ToolError("Provide ownerPrivateKey or set WALLET_PRIVATE_KEY.")
    acct = Account.from_key(pk)
    msg_q = ("query GetServiceSignInMessage($w: String!, $s: String!) { "
             "getServiceSignInMessage(walletAddress: $w, serviceName: $s) { message } }")
    m = labs_graphql_call(msg_q, {"w": acct.address, "s": serviceName}, "api-key", labsUrl)
    message = (((m.get("data") or {}).get("getServiceSignInMessage")) or {}).get("message")
    if not message:
        raise ToolError(f"getServiceSignInMessage failed: {json.dumps(m.get('errors') or m)[:300]}")
    signature = Account.sign_message(encode_defunct(text=message), pk).signature.hex()
    signature = signature if signature.startswith("0x") else "0x" + signature
    tok_q = ("mutation GenerateServiceToken($s: String!, $e: String, $w: String, $m: String) { "
             "generateServiceToken(serviceName: $s, expiresIn: $e, walletAddress: $w, messageSignature: $m) "
             "{ token tokenId expiresAt isSuccess message } }")
    t = labs_graphql_call(
        tok_q, {"s": serviceName, "e": expiresIn, "w": acct.address, "m": signature}, "api-key", labsUrl
    )
    res = ((t.get("data") or {}).get("generateServiceToken")) or {}
    if not res.get("isSuccess") or not res.get("token"):
        raise ToolError(f"generateServiceToken failed: {json.dumps(res or t.get('errors'))[:400]}")
    return dump({
        "token": res.get("token"),
        "tokenId": res.get("tokenId"),
        "address": acct.address,
        "expiresAt": res.get("expiresAt"),
    })


# ---- configuration diagnostics ------------------------------------------

# (name, is_secret, purpose) — the env vars the molecule flows read.
_ENV_CATALOG: list[tuple[str, bool, str]] = [
    ("MOLECULE_LABS_URL", False, "Labs GraphQL endpoint (OCL/V3 surface)"),
    ("MOLECULE_CLIENT_URL", False, "Client base URL for project links"),
    ("ONCHAIN_LAB_FACTORY_ADDRESS", False, "OnChainLabFactory (mint + TBA, oclId reads)"),
    ("LABNFT_ADDRESS", False, "LabNFT ERC-721 (optional override; auto-discovered otherwise)"),
    ("ACCESS_RESOLVER_ADDRESS", False, "AccessResolver V3 (hasRole / TBA owner / grantRole)"),
    ("X402_GATEWAY_URL", False, "x402 paid-mutation gateway"),
    ("CHAIN_ID", False, "OCL chain id (84532 Base Sepolia / 8453 Base)"),
    ("EVM_RPC_URL", False, "Base RPC for ocl_read + raw broadcast (optional)"),
    ("EVM_WALLET_ADDRESS", False, "Owner / hand-off wallet (optional)"),
    ("PRIVY_APP_ID", True, "Privy app id — wallet ops + x402"),
    ("PRIVY_APP_SECRET", True, "Privy app secret — wallet ops + x402"),
    ("PRIVY_WALLET_ID", True, "Privy agentic wallet id"),
    ("MOLECULE_API_KEY", True, "x-api-key for direct Labs reads"),
    ("MOLECULE_SERVICE_TOKEN", True, "Service JWT for private/encrypted DEK calls"),
    ("WALLET_PRIVATE_KEY", True, "Owner EOA key (only for issue_owner_service_token)"),
]

# Per-flow required vars, for a quick readiness verdict.
_FLOW_REQUIREMENTS: list[tuple[str, list[str]]] = [
    ("privy_wallet_ops", ["PRIVY_APP_ID", "PRIVY_APP_SECRET", "PRIVY_WALLET_ID"]),
    ("x402_mutations", ["X402_GATEWAY_URL", "PRIVY_APP_ID", "PRIVY_APP_SECRET", "PRIVY_WALLET_ID", "CHAIN_ID"]),
    ("onchain_lab", ["ONCHAIN_LAB_FACTORY_ADDRESS", "ACCESS_RESOLVER_ADDRESS", "CHAIN_ID"]),
    ("labs_reads", ["MOLECULE_LABS_URL", "MOLECULE_API_KEY"]),
    ("private_encrypted_upload", ["MOLECULE_SERVICE_TOKEN", "MOLECULE_API_KEY"]),
]


@mcp.tool()
def config_doctor(showValues: bool = True) -> str:
    """Diagnose the molecule MCP configuration WITHOUT shell-grepping settings files.
    Reports the resolved PROJECT BASE (.claude dir) the env was loaded from, which
    settings files were loaded, and — for every known env var — whether it is set,
    where it resolved from (process env injected by the harness vs the project-base
    settings.json / settings.local.json), and, for NON-secret vars only when
    showValues, its value. Secrets are NEVER shown (only set/missing + length). Also
    reports, per flow (privy wallet ops, x402 mutations, on-chain lab, labs reads,
    private/encrypted upload), which required vars are still missing. Global ~/.claude
    is intentionally excluded — config comes from the project base you run in."""
    def source_of(name: str) -> str:
        if name in _PREEXISTING_ENV_KEYS:
            return "process env (harness)"
        return _ENV_SOURCES.get(name, "—")

    vars_report: list[dict[str, Any]] = []
    for name, is_secret, purpose in _ENV_CATALOG:
        val = os.environ.get(name)
        entry: dict[str, Any] = {
            "name": name,
            "set": bool(val),
            "secret": is_secret,
            "source": source_of(name) if val else "—",
            "purpose": purpose,
        }
        if val:
            if is_secret:
                entry["value"] = f"<redacted, len={len(val)}>"
            elif showValues:
                entry["value"] = val
        vars_report.append(entry)

    flows: list[dict[str, Any]] = []
    for flow, required in _FLOW_REQUIREMENTS:
        missing = [r for r in required if not os.environ.get(r)]
        flows.append({"flow": flow, "ready": not missing, "missing": missing})

    core_ready = all(
        f["ready"] for f in flows if f["flow"] in ("privy_wallet_ops", "x402_mutations", "onchain_lab")
    )
    try:
        global_excluded = str((Path.home() / ".claude").resolve())
    except Exception:
        global_excluded = "~/.claude"
    return dump(
        {
            "ok": core_ready,
            "projectBase": _CONFIG_BASE or "(no project-base .claude with an env block found)",
            "configFilesLoaded": _CONFIG_FILES_LOADED or ["(none)"],
            "globalSettingsExcluded": global_excluded,
            "envVars": vars_report,
            "flows": flows,
            "note": (
                "Env is loaded from process env (harness) first, then the nearest "
                "project-base .claude/settings.local.json (secrets) + settings.json "
                "(non-secrets); global ~/.claude is excluded. To fix a missing var, set "
                "it in the projectBase files above, then reload the MCP (/mcp)."
            ),
        }
    )


# --------------------------------------------------------------------------
# boot
# --------------------------------------------------------------------------


def main() -> None:
    log("molecule-mcp ready (stdio)")
    mcp.run()


if __name__ == "__main__":
    main()
