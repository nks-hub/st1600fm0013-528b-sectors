#!/usr/bin/env python3
"""Doaplikovat tri hunky, ktere na vanilla 6.8 nesedely, a prelozit je na 6.8 API.

Patch wvg-sd-528 cili na strom, kde `sd_revalidate_disk()` pracuje s
`struct queue_limits *lim` alokovanou pres kmalloc. To v upstream neexistuje
(overeno pro 6.8 az 6.14) - do 6.10 tam queue_limits nejsou vubec, od 6.11 je
to lokalni promenna na zasobniku.

Ve 6.8 se limity nastavuji primo do `q->limits`, takze hunk 10 se da prelozit
mechanicky. Hunk 9 je jen vlozeni volani na jine misto teze funkce. Hunk 1 je
na API nezavisly - je to blok globalnich promennych a pomocnych funkci.

  hunk 1  globalni promenne, module_param a emulacni infrastruktura
  hunk 9  volani sd_528_limit_queue_depth() v sd_revalidate_disk()
  hunk 10 omezeni max_dev_sectors, kdyz je emulace aktivni  -> prepsano na q->limits

Pouziti: python3 apply_manual_hunks.py <kernel-tree>
"""
import re
import sys
from pathlib import Path

# --- kotvy ve vanilla 6.8 -------------------------------------------------
ANCHOR_GLOBALS = "static struct lock_class_key sd_bio_compl_lkclass;"
ANCHOR_HUNK9 = "\tbuffer = kmalloc(SD_BUF_SIZE, GFP_KERNEL);"
ANCHOR_HUNK10 = "\tq->limits.max_dev_sectors = logical_to_sectors(sdp, dev_max);"

# hunk 10 prelozeny z lim-> na q->limits.
HUNK10_68 = """
	if (sdkp->emulate_512_from_528) {
		unsigned int emu_cap = sd_528_effective_max_sectors();

		emu_cap = min_t(unsigned int, emu_cap,
				sd_528_segments_to_sectors(q->limits.max_segments));
		q->limits.max_dev_sectors = min_t(unsigned int,
						  q->limits.max_dev_sectors,
						  emu_cap);
	}
"""


def split_hunks(rej_text):
    parts = re.split(r"(?m)^(@@ [^\n]*)$", rej_text)
    return [(parts[i], parts[i + 1]) for i in range(1, len(parts), 2)]


def insert_after(text, anchor, payload, what):
    idx = text.find(anchor)
    if idx < 0:
        raise SystemExit("kotva nenalezena (%s): %r" % (what, anchor[:70]))
    end = idx + len(anchor)
    return text[:end] + "\n" + payload + text[end:]


def insert_before(text, anchor, payload, what):
    idx = text.find(anchor)
    if idx < 0:
        raise SystemExit("kotva nenalezena (%s): %r" % (what, anchor[:70]))
    return text[:idx] + payload + "\n" + text[idx:]


def main():
    tree = Path(sys.argv[1] if len(sys.argv) > 1 else "/usr/src/k/linux-6.8")
    sd_c = tree / "drivers/scsi/sd.c"
    rej = tree / "drivers/scsi/sd.c.rej"

    text = sd_c.read_text()
    hunks = split_hunks(rej.read_text())
    print("odmitnutych hunku v .rej: %d" % len(hunks))

    for hdr, body in hunks:
        # --- hunk 1: globalni promenne + infrastruktura -------------------
        if hdr.startswith("@@ -114,"):
            if "static mempool_t *sd_528_page_pool;" in text:
                print("  hunk 1  : jiz aplikovan")
                continue
            pre, post, seen_lock = [], [], False
            for ln in body.split("\n"):
                if "sd_bio_compl_lkclass" in ln and not ln.startswith("+"):
                    seen_lock = True
                    continue
                if ln.startswith("+"):
                    (post if seen_lock else pre).append(ln[1:])
            text = insert_before(text, ANCHOR_GLOBALS, "\n".join(pre), "hunk1-pre")
            text = insert_after(text, ANCHOR_GLOBALS, "\n".join(post), "hunk1-post")
            print("  hunk 1  : vlozeno %d radku pred a %d za kotvou" % (len(pre), len(post)))

        # --- hunk 9: volani limitu fronty ---------------------------------
        elif hdr.startswith("@@ -3740,"):
            if "sd_528_limit_queue_depth(sdkp);" in text:
                print("  hunk 9  : jiz aplikovan")
                continue
            text = insert_before(text, ANCHOR_HUNK9,
                                 "\tsd_528_limit_queue_depth(sdkp);\n", "hunk9")
            print("  hunk 9  : vlozeno volani sd_528_limit_queue_depth()")

        # --- hunk 10: strop velikosti pozadavku ---------------------------
        elif hdr.startswith("@@ -3809,"):
            if "sd_528_effective_max_sectors()" in text:
                print("  hunk 10 : jiz aplikovan")
                continue
            text = insert_after(text, ANCHOR_HUNK10, HUNK10_68.strip("\n"), "hunk10")
            print("  hunk 10 : vlozeno omezeni max_dev_sectors (prelozeno na q->limits)")

        else:
            print("  neznamy hunk %s - preskakuji" % hdr.strip())

    sd_c.write_text(text)
    print("zapsano %s (%d radku)" % (sd_c, text.count("\n")))

    print("\nkontrola symbolu:")
    ok = True
    for sym in ("static mempool_t *sd_528_page_pool;",
                "sd_emulate_512_from_fat_sectors",
                "sd_528_limit_queue_depth",
                "sd_528_effective_max_sectors",
                "sd_528_segments_to_sectors"):
        present = sym in text
        ok = ok and present
        print("  %-38s %s" % (sym, "OK" if present else "CHYBI"))

    # zbyla nekde reference na neexistujici lim-> ?
    leftovers = re.findall(r"lim->[a-z_]+", text)
    if leftovers:
        print("\n  POZOR, zbyly reference na lim->: %s" % sorted(set(leftovers)))
        ok = False

    print("\n%s" % ("vse na miste" if ok else "necо chybi, prelozit se to nemusi"))


if __name__ == "__main__":
    main()
