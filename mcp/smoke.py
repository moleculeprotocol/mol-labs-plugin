#!/usr/bin/env python3
"""Offline smoke test: connect to the stdio server, list tools, and exercise the
pure-compute tools. No network or secrets required. Regression-checks the compute
outputs against the known-good values from the original TypeScript implementation.

Run:  .venv/bin/python smoke.py
"""

import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent

EXPECT = {
    "hex_to_uint256(0x35554760)": ("decimal", "894781280"),
    "abi safeTransferFrom": (
        "calldata",
        "0x42842e0e000000000000000000000000acb7bfa4d926e8df448cd08918a0d38bd6b40b54"
        "000000000000000000000000a2ec2967da7bc51494f8a5427b9784cb5a05cd3c"
        "0000000000000000000000000000000000000000000000000000000000000118",
    ),
}


async def main() -> None:
    params = StdioServerParameters(
        command=os.environ.get("PYTHON", str(HERE / ".venv" / "bin" / "python")),
        args=[str(HERE / "server.py")],
        env={
            **os.environ,
            "EVM_WALLET_ADDRESS": "0xa2eC2967Da7bC51494F8a5427B9784Cb5a05cD3c",
            "ACCESS_RESOLVER_ADDRESS": "0x5493F472602C87318EA5Eff753cDD593bf9bF559",
            "CHAIN_ID": "84532",
            "ENVIRONMENT": "migration",
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print("TOOL COUNT:", len(tools))
            for t in sorted(tools, key=lambda x: x.name):
                print(" -", t.name)

            async def call(name, args):
                res = await session.call_tool(name, args)
                return json.loads(res.content[0].text)

            ok = True

            r = await call("hex_to_uint256", {"hex": "0x35554760"})
            ok &= r["decimal"] == EXPECT["hex_to_uint256(0x35554760)"][1]
            print("hex_to_uint256:", r)

            print("hex small:", await call("hex_to_uint256", {"hex": "0x01"}))

            r = await call("abi_encode", {
                "functionSignature": "safeTransferFrom(address,address,uint256)",
                "args": ["0xacb7bfa4d926e8df448cd08918a0d38bd6b40b54", "0xa2eC2967Da7bC51494F8a5427B9784Cb5a05cD3c", "280"],
            })
            ok &= r["calldata"] == EXPECT["abi safeTransferFrom"][1]
            print("abi safeTransferFrom matches TS:", r["calldata"] == EXPECT["abi safeTransferFrom"][1])

            r = await call("abi_encode", {
                "functionSignature": "mintReservation(address,uint256,string,string,bytes)",
                "args": ["0xa2eC2967Da7bC51494F8a5427B9784Cb5a05cD3c", "123456789012345678901234567890", "ipfs://Qm", "SYMB", "0xdeadbeef"],
            })
            print("abi mintReservation calldata:", r["calldata"][:18], "...")

            # bytes without 0x must be REJECTED (not silently UTF-8 encoded)
            bad = await session.call_tool("abi_encode", {
                "functionSignature": "mintReservation(address,uint256,string,string,bytes)",
                "args": ["0xa2eC2967Da7bC51494F8a5427B9784Cb5a05cD3c", "1", "x", "y", "deadbeef"],
            })
            rejected = bad.isError or ("must be 0x-prefixed hex" in bad.content[0].text)
            ok &= rejected
            print("abi rejects non-0x bytes:", rejected)

            r = await call("build_access_conditions", {"mode": "ipnft-signer", "reservationId": "123456789"})
            ok &= r["conditions"][0]["functionName"] == "isAuthorizedSignerForIpnft" and r["conditions"][0]["chain"] == "baseSepolia"
            print("ipnft-signer ok:", r["conditions"][0]["functionName"], r["conditions"][0]["chain"])

            r = await call("privy_get_wallet_address", {})  # env path, no network
            ok &= r["address"] == "0xa2eC2967Da7bC51494F8a5427B9784Cb5a05cD3c"
            print("privy_get_wallet_address (env, no net):", r)

            # round-trip encrypt/decrypt via dekHandle (no network: inject a fake DEK)
            import base64 as b64
            dek = b64.b64encode(b"0" * 32).decode()
            tmp = HERE / "_smoke_plain.bin"
            enc = HERE / "_smoke.enc"
            dec = HERE / "_smoke.dec"
            tmp.write_bytes(b"hello molecule e2ee")
            # We can't call labs_generate_dek (network); test the crypto impl directly:
            import server as srv
            h = srv.put_dek(dek)
            e = json.loads((await session.call_tool("encrypt_file", {"filePath": str(tmp), "dekHandle": h, "outPath": str(enc)})).content[0].text) if False else None
            # encrypt/decrypt impls are pure; call them directly for the round-trip check
            e = srv.encrypt_file_impl(str(tmp), dek, str(enc))
            d = srv.decrypt_file_impl(str(enc), e["iv"], dek, str(dec))
            roundtrip = d["plaintextSha256"] == e["contentHash"] and dec.read_bytes() == b"hello molecule e2ee"
            ok &= roundtrip
            print("AES-256-GCM round-trip ok:", roundtrip)
            for p in (tmp, enc, dec):
                p.unlink(missing_ok=True)

            print("\nALL ASSERTIONS PASS:" , ok)
            if not ok:
                raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
