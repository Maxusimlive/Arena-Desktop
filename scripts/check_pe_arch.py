"""Fail unless a Windows PE file has the expected COFF machine type.

Usage: python scripts/check_pe_arch.py path/to/Arena-Desktop.exe x64|arm64
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

MACHINES = {
    0x8664: "x64",
    0xAA64: "arm64",
    0x014C: "x86",
    0x01C0: "arm",
    0x01C4: "armv7",
}


def pe_architecture(path: Path) -> str:
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("not a Windows PE file (missing MZ header)")
    (pe_offset,) = struct.unpack_from("<I", data, 0x3C)
    if pe_offset < 0 or pe_offset + 6 > len(data) or data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise ValueError("not a Windows PE file (missing PE header)")
    (machine,) = struct.unpack_from("<H", data, pe_offset + 4)
    return MACHINES.get(machine, f"unknown-0x{machine:04x}")


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[2].lower() not in ("x64", "arm64"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    path = Path(argv[1])
    expected = argv[2].lower()
    try:
        actual = pe_architecture(path)
    except Exception as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 1
    print(f"{path}: PE architecture {actual} (expected {expected})")
    return 0 if actual == expected else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
