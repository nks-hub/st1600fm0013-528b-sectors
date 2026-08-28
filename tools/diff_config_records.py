#!/usr/bin/env python3
"""Parse the configuration block into TLV records and compare IBM vs Seagate.

Record format:  [id:1][len:2][00 00][id:1][len-4:2][data...]
The goal is to find every place where IBM differs from Seagate, and to tell
records tied to a specific unit (serial, SAS address) apart from those that
carry behavioural configuration.
"""
import os, sys, struct
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda n: os.path.join(BASE, "dumps", n)


def parse_records(data, start, length):
    """Walk the configuration block and return a list of (offset, id, len, body)."""
    recs = []
    pos = start
    end = start + length
    while pos < end - 8:
        rid = data[pos]
        rlen = (data[pos + 1] << 8) | data[pos + 2]
        # validation: second occurrence of the id, with a length 4 smaller
        if rlen < 4 or rlen > 0x400 or pos + rlen > end:
            pos += 1
            continue
        if data[pos + 5] != rid:
            pos += 1
            continue
        inner = (data[pos + 6] << 8) | data[pos + 7]
        if inner != rlen - 4:
            pos += 1
            continue
        recs.append((pos, rid, rlen, bytes(data[pos + 8:pos + rlen])))
        pos += rlen
    return recs


def main():
    ibm = open(P("ibm_ST1600FM0013_6214.bin"), "rb").read()
    sga = open(P("seagate_ST200FM0133_000A.bin"), "rb").read()

    out = {}
    for name, d in (("IBM", ibm), ("SGA", sga)):
        base = d.find(b"\x79\x71\x6e\x49")
        ln = struct.unpack("<H", d[base + 4:base + 6])[0]
        recs = parse_records(d, base, ln)
        out[name] = recs
        print("%s: config block @0x%06x length 0x%04x -> %d records"
              % (name, base, ln, len(recs)))
    print()

    ibm_ids = {r[1] for r in out["IBM"]}
    sga_ids = {r[1] for r in out["SGA"]}
    print("IDs only in IBM :", " ".join("0x%02x" % x for x in sorted(ibm_ids - sga_ids)) or "(none)")
    print("IDs only in SGA :", " ".join("0x%02x" % x for x in sorted(sga_ids - ibm_ids)) or "(none)")
    print("IDs in both     :", " ".join("0x%02x" % x for x in sorted(ibm_ids & sga_ids)))
    print()

    print("=" * 76)
    print("RECORDS IN BOTH - where the content differs")
    print("=" * 76)
    ibm_map, sga_map = {}, {}
    for o, i, l, b in out["IBM"]:
        ibm_map.setdefault(i, []).append((o, l, b))
    for o, i, l, b in out["SGA"]:
        sga_map.setdefault(i, []).append((o, l, b))

    for rid in sorted(ibm_ids & sga_ids):
        for (io, il, ib), (so, sl, sb) in zip(ibm_map[rid], sga_map[rid]):
            if ib == sb:
                continue
            n = min(len(ib), len(sb))
            diffs = [k for k in range(n) if ib[k] != sb[k]]
            # flag records whose only difference is the SAS address or the serial
            tag = ""
            if b"\x50\x00\xc5\x00" in ib[:16] or b"ZA" in ib[:20]:
                tag = "  (obsahuje SAS adresu / serial -> vazano na kus)"
            print("\nID 0x%02x  len=0x%03x  IBM@0x%06x SGA@0x%06x  differing bytes: %d%s"
                  % (rid, il, io, so, len(diffs), tag))
            print("   IBM: %s" % ib[:40].hex(" "))
            print("   SGA: %s" % sb[:40].hex(" "))
            if len(diffs) <= 12:
                print("   difference offsets (rel.): %s"
                      % ", ".join("+%d (%02x->%02x)" % (k, sb[k], ib[k]) for k in diffs[:12]))

    print()
    print("=" * 76)
    print("RECORDS ONLY IN IBM (the heart of the lock)")
    print("=" * 76)
    for o, i, l, b in out["IBM"]:
        if i in sga_ids:
            continue
        txt = "".join(chr(c) if 32 <= c < 127 else "." for c in b[:32])
        print("\nID 0x%02x  @0x%06x  len=0x%03x" % (i, o, l))
        print("   %s" % b[:48].hex(" "))
        print("   %s" % txt)


if __name__ == "__main__":
    main()
