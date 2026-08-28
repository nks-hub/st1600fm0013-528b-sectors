#!/usr/bin/env python3
"""Rozdelit obsah SPI flash na kod firmwaru vs. konfiguraci konkretniho disku.

Klic: dumpy 0007 a 000A jsou ze STEJNEHO disku, jen jina verze firmwaru.
  - co se mezi nimi lisi  -> patri k verzi firmwaru (kod)
  - co je mezi nimi stejne -> je vazane na ten kus (serial, kapacita, kalibrace)

Kombinaci s IBM dumpem se da odhadnout, ktere oblasti nese smysl prepsat
a ktere by se prepsat NEMELY.
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
print("KLASIFIKACE OBLASTI SPI FLASH (bloky po 4 kB)")
print("=" * 78)
cat = {"kod": [], "config": [], "obojí": [], "shodne": []}
for i in range(N):
    v, d = ver_diff[i], vend_diff[i]
    if v and d:   cat["obojí"].append(i)
    elif v:       cat["kod"].append(i)
    elif d:       cat["config"].append(i)
    else:         cat["shodne"].append(i)

for k in ("shodne", "kod", "config", "obojí"):
    print("  %-8s %4d bloku  %8d B  %5.1f %%" %
          (k, len(cat[k]), len(cat[k]) * BLK, 100 * len(cat[k]) / N))
print()
print("  kod    = lisi se mezi verzemi 0007/000A, IBM stejny jako 000A")
print("  config = mezi verzemi stejne, ale IBM se lisi -> vazane na kus/model")
print("  obojí  = lisi se v obou smerech")

print()
print("=" * 78)
print("OBLASTI 'config' — IBM se lisi, ale verze firmwaru to nemeni")
print("(tady bude kapacita, model, serial, kalibrace — prepsat = riziko)")
print("=" * 78)
mask = [i in set(cat["config"]) for i in range(N)]
for s, e in regions(mask):
    seg = ibm[s:e]
    nz = sum(1 for x in seg if x not in (0, 0xFF))
    txt = b" ".join(re.findall(rb"[\x20-\x7e]{4,}", seg))[:90]
    print("  0x%06x-0x%06x %6d B  uziteh=%6d  %s" % (s, e, e - s, nz, txt.decode("ascii", "ignore")))

print()
print("=" * 78)
print("OBLASTI 'kod' — meni se s verzi firmwaru, IBM shodny s 000A")
print("=" * 78)
mask = [i in set(cat["kod"]) for i in range(N)]
for s, e in regions(mask):
    print("  0x%06x-0x%06x %6d B" % (s, e, e - s))

print()
print("=" * 78)
print("HLEDANI KONKRETNICH HODNOT V IBM DUMPU")
print("=" * 78)
targets = {
    "block size 528 (0x0210 BE)":      b"\x02\x10",
    "block size 512 (0x0200 BE)":      b"\x02\x00",
    "pocet bloku 3030911576 (BE)":     struct.pack(">I", 3030911576),
    "pocet bloku 3125627568 (BE,512)": struct.pack(">I", 3125627568),
    "link rate byte 0xaa":             b"\xaa",
    "model ST1600FM0013":              b"ST1600FM0013",
    "IBM-SSGSSVJ1P6":                  b"IBM-SSGSSVJ1P6",
    "serial ZAL":                      b"ZAL",
    "PN 1NT2L2":                       b"1NT2L2",
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
    print("  %-34s IBM: %s" % (name, ", ".join("0x%06x" % x for x in pos) if pos else "nenalezeno"))
    if pos:
        print("  %-34s 000A: %s" % ("", ("0x%06x" % inA) if inA >= 0 else "nenalezeno"))
