#!/usr/bin/env python3
"""Odstranit IBM zamek (528 B / AIX profil) z dumpu SPI flash Koho SSD.

POZOR: dump MUSI byt z toho konkretniho disku, na ktery se bude zapisovat.
Konfiguracni blok obsahuje seriove cislo a kalibraci daneho kusu.

Checksum: soucet 16bitovych little-endian slov pres cely blok
(vcetne pole checksumu) musi byt 0. Zjisteno z hddguru rozboru LOD formatu.

Pouziti:
  python patch_ibm_lock.py vstup.bin vystup.bin [--mode blocksize|strip]

  blocksize  zmeni jen velikost sektoru 528->512 a pocet bloku  (8 bajtu)
  strip      odstrani cely AIX profil vcetne polozek v indexu   (688 bajtu)
"""
import sys, struct, argparse, os

MAGIC = b"\x79\x71\x6e\x49"

def summ_le16(seg):
    s = 0
    for i in range(0, len(seg) - 1, 2):
        s = (s + struct.unpack("<H", seg[i:i + 2])[0]) & 0xFFFF
    return s

def fix_checksum(data, base, length, cks_off=6):
    """Prepocita checksum tak, aby soucet celeho bloku byl nula."""
    blk = bytearray(data[base:base + length])
    blk[cks_off:cks_off + 2] = b"\x00\x00"
    s = summ_le16(blk)
    cks = (-s) & 0xFFFF
    blk[cks_off:cks_off + 2] = struct.pack("<H", cks)
    assert summ_le16(blk) == 0, "checksum se nepodarilo srovnat"
    data[base:base + length] = blk
    return cks

def find_config(data):
    base = data.find(MAGIC)
    if base < 0:
        raise SystemExit("konfiguracni blok (magic yqnI) nenalezen")
    length = struct.unpack("<H", data[base + 4:base + 6])[0]
    cks = struct.unpack("<H", data[base + 6:base + 8])[0]
    return base, length, cks

def show(data, label):
    base, length, cks = find_config(data)
    s = summ_le16(data[base:base + length])
    inq = bytes(data[base + 0x14:base + 0x14 + 44]).decode("ascii", "replace")
    print("  %-10s magic@0x%06x delka=0x%04x cks=0x%04x soucet=0x%04x %s"
          % (label, base, length, cks, s, "OK" if s == 0 else "!! NESEDI"))
    print("  %-10s INQUIRY: %r" % ("", inq.strip()))
    return base, length

def patch_blocksize(data, new_bs=512, new_blocks=None):
    """Zmeni block descriptor v zaznamu 0xc8 (AIX)."""
    # najdi 'AIX' nasledovane block descriptorem: 00 00 00 08 <4B pocet> 00 00 <2B velikost>
    idx = -1
    start = 0
    while True:
        i = data.find(b"AIX", start)
        if i < 0:
            break
        # descriptor zacina 7 bajtu za 'AIX      ' (9 znaku jmena)
        p = i + 9
        if data[p:p + 4] == b"\x00\x00\x00\x08":
            idx = p
            break
        start = i + 1
    if idx < 0:
        raise SystemExit("block descriptor v AIX zaznamu nenalezen")

    old_blocks = struct.unpack(">I", data[idx + 4:idx + 8])[0]
    old_bs = struct.unpack(">I", data[idx + 8:idx + 12])[0]
    print("  nalezen descriptor na 0x%06x: %d bloku x %d B = %.2f GB"
          % (idx, old_blocks, old_bs, old_blocks * old_bs / 1e9))

    if new_blocks is None:
        total = old_blocks * old_bs
        new_blocks = total // new_bs
    data[idx + 4:idx + 8] = struct.pack(">I", new_blocks)
    data[idx + 8:idx + 12] = struct.pack(">I", new_bs)
    print("  zmeneno na:                  %d bloku x %d B = %.2f GB"
          % (new_blocks, new_bs, new_blocks * new_bs / 1e9))
    return idx

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("infile")
    ap.add_argument("outfile")
    ap.add_argument("--mode", choices=("blocksize", "strip"), default="blocksize")
    ap.add_argument("--blocks", type=int, default=None,
                    help="rucne zadany pocet bloku pro novou velikost")
    a = ap.parse_args()

    data = bytearray(open(a.infile, "rb").read())
    print("vstup: %s (%d B)" % (a.infile, len(data)))
    base, length = show(data, "puvodni")

    if a.mode == "blocksize":
        print()
        print("  rezim: zmena velikosti sektoru")
        patch_blocksize(data, 512, a.blocks)
    else:
        raise SystemExit("rezim 'strip' zatim neimplementovan - vyzaduje "
                         "prepocet delky bloku i indexu zaznamu")

    print()
    cks = fix_checksum(data, base, length)
    print("  novy checksum: 0x%04x" % cks)
    show(data, "upraveny")

    if len(data) != os.path.getsize(a.infile):
        raise SystemExit("velikost se zmenila - to je chyba")
    open(a.outfile, "wb").write(data)
    print()
    print("zapsano: %s" % a.outfile)
    orig = open(a.infile, "rb").read()
    diff = [i for i in range(len(data)) if data[i] != orig[i]]
    print("zmeneno bajtu: %d na offsetech %s"
          % (len(diff), ", ".join("0x%06x" % d for d in diff)))

if __name__ == "__main__":
    main()
