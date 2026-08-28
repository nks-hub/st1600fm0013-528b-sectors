#!/usr/bin/env python3
"""Dissect the disk configuration block in SPI flash (~0x0e1000).

Model, serial, block count and most likely the sector size live there.
The goal is to find out whether the sector size is stored as data (and thus
theoretically rewritable) or hard-wired in the code.
"""
import os, re, struct, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda n: os.path.join(BASE, "dumps", n)
ibm = open(P("ibm_ST1600FM0013_6214.bin"), "rb").read()
sgA = open(P("seagate_ST200FM0133_000A.bin"), "rb").read()
sg7 = open(P("seagate_ST200FM0133_0007.bin"), "rb").read()

def hexdump(b, base=0, width=16, maxlen=None):
    out = []
    n = len(b) if maxlen is None else min(len(b), maxlen)
    for i in range(0, n, width):
        chunk = b[i:i + width]
        txt = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
        out.append("  %06x  %-*s  %s" % (base + i, width * 3, chunk.hex(" "), txt))
    return "\n".join(out)

print("=" * 78)
print("IBM configuration block 0x0e1100 - 0x0e1300")
print("=" * 78)
print(hexdump(ibm[0x0e1100:0x0e1300], 0x0e1100))

print()
print("=" * 78)
print("Around the block count (0x0e169e) in the IBM dump")
print("=" * 78)
print(hexdump(ibm[0x0e1660:0x0e1720], 0x0e1660))

print()
print("=" * 78)
print("SEARCHING FOR THE SECTOR SIZE around the configuration")
print("=" * 78)
# 528 = 0x210, 4224 = 0x1080
for name, val, size in [("528", 528, 2), ("528", 528, 4), ("4224", 4224, 2), ("4224", 4224, 4)]:
    for endian in (">", "<"):
        pat = struct.pack(endian + ("H" if size == 2 else "I"), val)
        hits = []
        start = 0x0e0000
        while True:
            i = ibm.find(pat, start, 0x0e3000)
            if i < 0: break
            hits.append(i); start = i + 1
            if len(hits) > 8: break
        if hits:
            print("  %-5s %s %dB: %s" % (name, "BE" if endian == ">" else "LE", size,
                                          ", ".join("0x%06x" % h for h in hits)))

print()
print("=" * 78)
print("SEAGATE 000A - same region, what its configuration looks like")
print("=" * 78)
# find the model string in the Seagate dump
i = sgA.find(b"ST200FM0133")
print("  ST200FM0133 found at: %s" % (("0x%06x" % i) if i >= 0 else "not found"))
if i >= 0:
    lo = max(0, i - 0x400)
    print(hexdump(sgA[lo:i + 0x100], lo))

print()
print("=" * 78)
print("Jak vypada IBM na tomtez offsetu jako Seagate model string")
print("=" * 78)
if i >= 0:
    print(hexdump(ibm[i - 0x80:i + 0x100], i - 0x80))

print()
print("=" * 78)
print("Vsechny citelne retezce v IBM konfig oblasti 0x0e1000-0x0e2000")
print("=" * 78)
seg = ibm[0x0e1000:0x0e2000]
for m in re.finditer(rb"[\x20-\x7e]{5,}", seg):
    print("  0x%06x  %s" % (0x0e1000 + m.start(), m.group().decode("ascii", "ignore")[:100]))
