#!/usr/bin/env python3
"""Split the SPI flash contents into firmware code vs. per-unit configuration.

Key insight: the 0007 and 000A dumps come from the SAME disk, only the firmware
version differs.
  - what differs between them   -> belongs to the firmware version (code)
  - what is identical           -> is tied to that unit (serial, capacity, calibration)

Combined with the IBM dump this gives an estimate of which regions are worth
rewriting and which ones should NOT be touched.
"""
import os, re, struct

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda n: os.path.join(BASE, "dumps", n)
ibm  = open(P("ibm_ST1600FM0013_6214.bin"), "rb").read()
sg7  = open(P("seagate_ST200FM0133_0007.bin"), "rb").read()
sgA  = open(P("seagate_ST200FM0133_000A.bin"), "rb").read()
BLK = 4096
N = len(ibm) // BLK

def regions(mask):
    out, cur = [], None
    for i, v in enumerate(mask):
        if v and cur is None:
            cur = i
        elif not v and cur is not None:
            out.append((cur * BLK, i * BLK)); cur = None
    if cur is not None:
        out.append((cur * BLK, len(mask) * BLK))
    return out

ver_diff  = [sg7[i*BLK:(i+1)*BLK] != sgA[i*BLK:(i+1)*BLK] for i in range(N)]
vend_diff = [ibm[i*BLK:(i+1)*BLK] != sgA[i*BLK:(i+1)*BLK] for i in range(N)]

print("=" * 78)
print("SPI FLASH REGION CLASSIFICATION (4 kB blocks)")
print("=" * 78)
cat = {"code": [], "config": [], "both": [], "same": []}
for i in range(N):
    v, d = ver_diff[i], vend_diff[i]
    if v and d:   cat["both"].append(i)
    elif v:       cat["code"].append(i)
    elif d:       cat["config"].append(i)
    else:         cat["same"].append(i)

for k in ("same", "code", "config", "both"):
    print("  %-8s %4d blocks  %8d B  %5.1f %%" %
          (k, len(cat[k]), len(cat[k]) * BLK, 100 * len(cat[k]) / N))
print()
print("  code   = differs between versions 0007/000A, IBM same as 000A")
print("  config = same across versions but IBM differs -> tied to unit/model")
print("  both   = differs in both directions")

print()
print("=" * 78)
print("'config' REGIONS - IBM differs, firmware version does not change them")
print("(capacity, model, serial, calibration live here - rewriting is risky)")
print("=" * 78)
mask = [i in set(cat["config"]) for i in range(N)]
for s, e in regions(mask):
    seg = ibm[s:e]
    nz = sum(1 for x in seg if x not in (0, 0xFF))
    txt = b" ".join(re.findall(rb"[\x20-\x7e]{4,}", seg))[:90]
    print("  0x%06x-0x%06x %6d B  used=%6d  %s" % (s, e, e - s, nz, txt.decode("ascii", "ignore")))

print()
print("=" * 78)
print("'code' REGIONS - change with firmware version, IBM matches 000A")
print("=" * 78)
mask = [i in set(cat["code"]) for i in range(N)]
for s, e in regions(mask):
    print("  0x%06x-0x%06x %6d B" % (s, e, e - s))

print()
print("=" * 78)
print("SEARCHING FOR SPECIFIC VALUES IN THE IBM DUMP")
print("=" * 78)
targets = {
    "block size 528 (0x0210 BE)":        b"\x02\x10",
    "block size 512 (0x0200 BE)":        b"\x02\x00",
    "block count 3030911576 (BE)":       struct.pack(">I", 3030911576),
    "block count 3125627568 (BE,512)":   struct.pack(">I", 3125627568),
    "link rate byte 0xaa":               b"\xaa",
    "model ST1600FM0013":                b"ST1600FM0013",
    "IBM-SSGSSVJ1P6":                    b"IBM-SSGSSVJ1P6",
    "serial ZAL":                        b"ZAL",
    "PN 1NT2L2":                         b"1NT2L2",
}
for name, pat in targets.items():
    if len(pat) < 3:
        continue
    pos = []
    start = 0
    while len(pos) < 6:
        i = ibm.find(pat, start)
        if i < 0: break
        pos.append(i); start = i + 1
    inA = sgA.find(pat) if len(pat) >= 3 else -1
    print("  %-34s IBM: %s" % (name, ", ".join("0x%06x" % x for x in pos) if pos else "not found"))
    if pos:
        print("  %-34s 000A: %s" % ("", ("0x%06x" % inA) if inA >= 0 else "not found"))
