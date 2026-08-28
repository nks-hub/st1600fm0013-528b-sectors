#!/usr/bin/env python3
"""Doaplikovat tri hunky, ktere na vanilla 6.8 nesedely.

`patch` je odmitne kvuli posunutemu kontextu, ale obsah je prenositelny -
staci ho vlozit na spravna mista podle kotev v kodu.

  hunk 1  globalni promenne, module_param a cela emulacni infrastruktura
  hunk 9  volani sd_528_limit_queue_depth() v sd_revalidate_disk()
  hunk 10 omezeni max_dev_sectors, kdyz je emulace aktivni

Pouziti: python3 apply_manual_hunks.py <kernel-tree> <rej-soubor>
"""
import re
import sys
from pathlib import Path


def split_hunks(rej_text):
    """Rozdeli .rej na jednotlive hunky podle @@ hlavicek."""
    parts = re.split(r"(?m)^(@@ [^\n]*)$", rej_text)
    out = []
    for i in range(1, len(parts), 2):
        out.append((parts[i], parts[i + 1]))
    return out


def hunk_added_lines(body):
    """Z tela hunku vytahne jen pridane radky (bez vodiciho +)."""
    return [ln[1:] for ln in body.split("\n") if ln.startswith("+")]


def insert_after(text, anchor, payload, what):
    idx = text.find(anchor)
    if idx < 0:
        raise SystemExit("kotva nenalezena (%s): %r" % (what, anchor[:60]))
    end = idx + len(anchor)
    return text[:end] + "\n" + payload + text[end:]


def insert_before(text, anchor, payload, what):
    idx = text.find(anchor)
    if idx < 0:
        raise SystemExit("kotva nenalezena (%s): %r" % (what, anchor[:60]))
    return text[:idx] + payload + "\n" + text[idx:]


def main():
    tree = Path(sys.argv[1] if len(sys.argv) > 1 else "/usr/src/k/linux-6.8")
    rej = Path(sys.argv[2] if len(sys.argv) > 2 else tree / "drivers/scsi/sd.c.rej")

    sd_c = tree / "drivers/scsi/sd.c"
    text = sd_c.read_text()
    hunks = split_hunks(rej.read_text())
    print("nalezeno %d odmitnutych hunku" % len(hunks))

    for hdr, body in hunks:
        added = hunk_added_lines(body)
        payload = "\n".join(added)

        if hdr.startswith("@@ -114,"):
            # velky blok: vse pred 'static const char *sd_cache_types'
            anchor = "static struct lock_class_key sd_bio_compl_lkclass;"
            if "sd_528_page_pool" in text:
                print("  hunk 1: uz aplikovan, preskakuji")
                continue
            # rozdelit: cast pred lock_class_key a cast za nim
            pre, post = [], []
            seen_lock = False
            for ln in body.split("\n"):
                if "sd_bio_compl_lkclass" in ln and not ln.startswith("+"):
                    seen_lock = True
                    continue
                if not ln.startswith("+"):
                    continue
                (post if seen_lock else pre).append(ln[1:])
            text = insert_before(text, anchor, "\n".join(pre), "hunk1-pre")
            text = insert_after(text, anchor, "\n".join(post), "hunk1-post")
            print("  hunk 1: vlozeno %d + %d radku" % (len(pre), len(post)))

        elif hdr.startswith("@@ -3740,"):
            anchor = "\tlim = kmalloc(sizeof(*lim), GFP_KERNEL);"
            if "sd_528_limit_queue_depth(sdkp);" in text:
                print("  hunk 9: uz aplikovan, preskakuji")
                continue
            text = insert_before(text, anchor,
                                 "\tsd_528_limit_queue_depth(sdkp);\n", "hunk9")
            print("  hunk 9: vlozeno volani sd_528_limit_queue_depth()")

        elif hdr.startswith("@@ -3809,"):
            anchor = "\tlim->max_dev_sectors = logical_to_sectors(sdp, dev_max);"
            if "sd_528_effective_max_sectors()" in text.split(anchor)[-1][:600]:
                print("  hunk 10: uz aplikovan, preskakuji")
                continue
            text = insert_after(text, anchor, "\n".join(added), "hunk10")
            print("  hunk 10: vlozeno omezeni max_dev_sectors (%d radku)" % len(added))

        else:
            print("  neznamy hunk %s - preskakuji" % hdr)

    sd_c.write_text(text)
    print("zapsano %s (%d radku)" % (sd_c, text.count("\n")))

    # rychla kontrola, ze klicove symboly v souboru jsou
    for sym in ("sd_528_page_pool", "sd_emulate_512_from_fat_sectors",
                "sd_528_limit_queue_depth", "sd_528_effective_max_sectors"):
        print("  %-34s %s" % (sym, "OK" if sym in text else "CHYBI"))


if __name__ == "__main__":
    main()
