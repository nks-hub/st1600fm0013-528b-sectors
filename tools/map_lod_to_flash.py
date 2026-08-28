#!/usr/bin/env python3
"""Work out how the contents of a LOD file map onto a physical SPI flash dump.

It takes chunks of data from the LOD and looks for them in the dump. When they
are found it computes the offset and checks whether it stays constant -> that
gives the mapping.
"""
import sys, struct, os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOD = os.path.join(BASE, "official-lod", "firmware", "KohoSSD-SED-0004.LOD")
DUMPS = {
    "seagate_000A": os.path.join(BASE, "dumps", "seagate_ST200FM0133_000A.bin"),
    "seagate_0007": os.path.join(BASE, "dumps", "seagate_ST200FM0133_0007.bin"),
    "ibm_6214":     os.path.join(BASE, "dumps", "ibm_ST1600FM0013_6214.bin"),
}

lod = open(LOD, "rb").read()
dumps = {k: open(v, "rb").read() for k, v in DUMPS.items()}

# artifacts found by the parser: (index, file_offset, size, type)
ARTIFACTS = [
    (2, 0x248,     0x10000, 0x53),
    (3, 0x10288,   0x9f000, 0x22),
    (4, 0xaf2c8,   0x1000,  0x1d),
    (5, 0xb0308,   0xd0028, 0x9026),
    (6, 0x180370,  0x1a000, 0x45),
    (7, 0x19a3b0,  0x1a000, 0x45),
    (8, 0x1b43f0,  0x10810, 0x7f),
    (9, 0x1c4c40,  0x180,   0x1a),
]

def find_all(hay, needle, limit=5):
    out, start = [], 0
    while len(out) < limit:
        i = hay.find(needle, start)
        if i < 0:
            break
        out.append(i)
        start = i + 1
    return out

print("=" * 78)
print("LOOKING FOR LOD ARTIFACT CONTENT IN THE SPI FLASH DUMPS")
print("=" * 78)

for idx, off, size, atype in ARTIFACTS:
    body = lod[off:off + size]
    # sample from various places in the artifact, skip the header and zero blocks
    samples = []
    for frac in (0.10, 0.30, 0.55, 0.80):
        p = int(size * frac)
        chunk = body[p:p + 32]
        if chunk and len(set(chunk)) > 8:      # dost entropie, ne vypln
            samples.append((p, chunk))
    print("\nArtifact %d  type=0x%x  size=0x%x" % (idx, atype, size))
    if not samples:
        print("   (all zero/padding blocks, skipping)")
        continue
    for name, d in dumps.items():
        hits = []
        for p, chunk in samples:
            pos = find_all(d, chunk, 3)
            for x in pos:
                hits.append((p, x, x - p))
        if hits:
            deltas = {}
            for p, x, dl in hits:
                deltas.setdefault(dl, 0)
                deltas[dl] += 1
            best = sorted(deltas.items(), key=lambda kv: -kv[1])[:3]
            print("   %-14s found %d matches, most common offset: %s"
                  % (name, len(hits),
                     ", ".join("0x%x (%dx)" % (d_, c) for d_, c in best)))
        else:
            print("   %-14s no match" % name)

print()
print("=" * 78)
print("WHERE THE IBM AND SEAGATE DUMPS DIFFER (4 kB blocks)")
print("=" * 78)
a, b = dumps["ibm_6214"], dumps["seagate_000A"]
blk = 4096
diffs, cur = [], None
for i in range(0, min(len(a), len(b)), blk):
    d = a[i:i + blk] != b[i:i + blk]
    if d and cur is None:
        cur = i
    elif not d and cur is not None:
        diffs.append((cur, i))
        cur = None
if cur is not None:
    diffs.append((cur, min(len(a), len(b))))
tot = sum(e - s for s, e in diffs)
print("differing regions: %d, total %d B (%.1f %% of 4 MB)" % (len(diffs), tot, 100 * tot / len(a)))
for s, e in diffs:
    # how much of that is not padding
    seg_a = a[s:e]
    nz = sum(1 for x in seg_a if x not in (0, 0xFF))
    print("   0x%06x - 0x%06x  (%7d B)  useful in IBM: %d" % (s, e, e - s, nz))
