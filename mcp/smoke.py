#!/usr/bin/env python3
"""Offline smoke test: connect to the stdio server, list tools, and exercise the
pure-compute tools (OCL surface). No network or secrets required. Regression-checks
the compute outputs against known-good values.

Run:  .venv/bin/python smoke.py
"""

import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent

# Known-good calldata from the TypeScript implementation (chain-agnostic).
EXPECT_SAFE_TRANSFER = (
    "0x42842e0e000000000000000000000000acb7bfa4d926e8df448cd08918a0d38bd6b40b54"
    "000000000000000000000000a2ec2967da7bc51494f8a5427b9784cb5a05cd3c"
    "0000000000000000000000000000000000000000000000000000000000000118"
)

OCL_ID = "0x" + "11" * 32
LAB_ACCOUNT = "0x3333333333333333333333333333333333333333"


async def main() -> None:
    params = StdioServerParameters(
        command=os.environ.get("PYTHON", str(HERE / ".venv" / "bin" / "python")),
        args=[str(HERE / "server.py")],
        env={
            **os.environ,
            "EVM_WALLET_ADDRESS": "0xa2eC2967Da7bC51494F8a5427B9784Cb5a05cD3c",
            "ACCESS_RESOLVER_ADDRESS": "0x5493F472602C87318EA5Eff753cDD593bf9bF559",
            "ONCHAIN_LAB_FACTORY_ADDRESS": "0xd629FE2310b4309a212495F10A47f8436dcEfD90",
            "LABNFT_ADDRESS": "0x13Ff210695fdb54A7F928ECcc28BC3486c05BB28",
            "CHAIN_ID": "84532",  # Base Sepolia (OCL canonical chain)
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print("TOOL COUNT:", len(tools))
            tool_names = {t.name for t in tools}
            for t in sorted(tools, key=lambda x: x.name):
                print(" -", t.name)

            async def call(name, args):
                res = await session.call_tool(name, args)
                return json.loads(res.content[0].text)

            ok = True

            # Legacy IPNFT tools must be GONE.
            for gone in ("poi_register", "hex_to_uint256"):
                present = gone in tool_names
                ok &= not present
                print(f"removed {gone}:", not present)
            # New OCL primitives must be present.
            for needed in ("ocl_read", "ocl_tx_identity", "build_access_conditions"):
                present = needed in tool_names
                ok &= present
                print(f"has {needed}:", present)

            # abi_encode: known-good safeTransferFrom (regression).
            r = await call("abi_encode", {
                "functionSignature": "safeTransferFrom(address,address,uint256)",
                "args": ["0xacb7bfa4d926e8df448cd08918a0d38bd6b40b54", "0xa2eC2967Da7bC51494F8a5427B9784Cb5a05cD3c", "280"],
            })
            ok &= r["calldata"] == EXPECT_SAFE_TRANSFER
            print("abi safeTransferFrom matches TS:", r["calldata"] == EXPECT_SAFE_TRANSFER)

            # OCL calldata: mintAndCreateAccount(address) = 4-byte selector + 32-byte address.
            r = await call("abi_encode", {
                "functionSignature": "mintAndCreateAccount(address)",
                "args": ["0xa2eC2967Da7bC51494F8a5427B9784Cb5a05cD3c"],
            })
            ok &= r["calldata"].startswith("0x") and len(r["calldata"]) == 2 + 8 + 64
            print("abi mintAndCreateAccount:", r["calldata"])

            # grantRole(bytes32,address,uint8,uint64,bool) = selector + 5×32 bytes.
            r = await call("abi_encode", {
                "functionSignature": "grantRole(bytes32,address,uint8,uint64,bool)",
                "args": [OCL_ID, "0xa2eC2967Da7bC51494F8a5427B9784Cb5a05cD3c", 2, 0, False],
            })
            ok &= r["calldata"].startswith("0x") and len(r["calldata"]) == 2 + 8 + 64 * 5
            print("abi grantRole len ok:", len(r["calldata"]) == 2 + 8 + 64 * 5)

            # bytes32 without 0x must be REJECTED (not silently UTF-8 encoded).
            bad = await session.call_tool("abi_encode", {
                "functionSignature": "grantRole(bytes32,address,uint8,uint64,bool)",
                "args": ["deadbeef", "0xa2eC2967Da7bC51494F8a5427B9784Cb5a05cD3c", 2, 0, False],
            })
            rejected = bad.isError or ("must be 0x-prefixed hex" in bad.content[0].text)
            ok &= rejected
            print("abi rejects non-0x bytes32:", rejected)

            # OCL access conditions: hasRole OR isAuthorizedSignerForTba, chain sepolia-base.
            r = await call("build_access_conditions", {"oclId": OCL_ID, "labAccountAddress": LAB_ACCOUNT})
            conds = r["conditions"]
            shape_ok = (
                len(conds) == 3
                and conds[0]["functionName"] == "hasRole"
                and conds[0]["functionParams"] == [OCL_ID, ":userAddress", "2"]
                and conds[0]["chain"] == "sepolia-base"
                and conds[1] == {"operator": "or"}
                and conds[2]["functionName"] == "isAuthorizedSignerForTba"
                and conds[2]["functionParams"] == [":userAddress", LAB_ACCOUNT]
            )
            ok &= shape_ok
            print("OCL access conditions shape ok:", shape_ok)

            # viewer role variant -> role "1".
            r = await call("build_access_conditions", {"oclId": OCL_ID, "labAccountAddress": LAB_ACCOUNT, "role": "viewer"})
            ok &= r["conditions"][0]["functionParams"][2] == "1"
            print("viewer role -> 1:", r["conditions"][0]["functionParams"][2])

            r = await call("privy_get_wallet_address", {})  # env path, no network
            ok &= r["address"] == "0xa2eC2967Da7bC51494F8a5427B9784Cb5a05cD3c"
            print("privy_get_wallet_address (env, no net):", r)

            # round-trip encrypt/decrypt (no network: inject a fake DEK, call pure impls)
            import base64 as b64
            import server as srv
            dek = b64.b64encode(b"0" * 32).decode()
            tmp, enc, dec = HERE / "_smoke_plain.bin", HERE / "_smoke.enc", HERE / "_smoke.dec"
            tmp.write_bytes(b"hello molecule e2ee")
            e = srv.encrypt_file_impl(str(tmp), dek, str(enc))
            d = srv.decrypt_file_impl(str(enc), e["iv"], dek, str(dec))
            roundtrip = d["plaintextSha256"] == e["contentHash"] and dec.read_bytes() == b"hello molecule e2ee"
            ok &= roundtrip
            print("AES-256-GCM round-trip ok:", roundtrip)
            for p in (tmp, enc, dec):
                p.unlink(missing_ok=True)

            print("\nALL ASSERTIONS PASS:", ok)
            if not ok:
                raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
