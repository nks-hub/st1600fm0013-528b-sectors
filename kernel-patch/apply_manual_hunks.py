#!/usr/bin/env python3
"""Apply the three hunks that did not fit vanilla 6.8, translating them to its API.

The wvg-sd-528 patch targets a tree where `sd_revalidate_disk()` works with a
`struct queue_limits *lim` allocated via kmalloc. That does not exist upstream
(verified for 6.8 through 6.14) - up to 6.10 there are no queue_limits there at
all, and from 6.11 it is a local variable on the stack.

In 6.8 the limits are set directly in `q->limits`, so hunk 10 can be translated
mechanically. Hunk 9 is just a call inserted at a different place in the same
function. Hunk 1 is API-independent - it is a block of globals and helpers.

  hunk 1  globals, module_param and the emulation infrastructure
  hunk 9  the sd_528_limit_queue_depth() call in sd_revalidate_disk()
  hunk 10 cap on max_dev_sectors while emulation is active -> rewritten to q->limits

Usage: python3 apply_manual_hunks.py <kernel-tree>
"""
import re
import sys
from pathlib import Path

# --- anchors in vanilla 6.8 ----------------------------------------------
ANCHOR_GLOBALS = "static struct lock_class_key sd_bio_compl_lkclass;"
ANCHOR_HUNK9 = "\tbuffer = kmalloc(SD_BUF_SIZE, GFP_KERNEL);"
ANCHOR_HUNK10 = "\tq->limits.max_dev_sectors = logical_to_sectors(sdp, dev_max);"

# hunk 10 translated from lim-> to q->limits.
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
        raise SystemExit("anchor not found (%s): %r" % (what, anchor[:70]))
    end = idx + len(anchor)
    return text[:end] + "\n" + payload + text[end:]


def insert_before(text, anchor, payload, what):
    idx = text.find(anchor)
    if idx < 0:
        raise SystemExit("anchor not found (%s): %r" % (what, anchor[:70]))
    return text[:idx] + payload + "\n" + text[idx:]


def main():
    tree = Path(sys.argv[1] if len(sys.argv) > 1 else "/usr/src/k/linux-6.8")
    sd_c = tree / "drivers/scsi/sd.c"
    rej = tree / "drivers/scsi/sd.c.rej"

    text = sd_c.read_text()
    hunks = split_hunks(rej.read_text())
    print("rejected hunks in .rej: %d" % len(hunks))

    for hdr, body in hunks:
        # --- hunk 1: globals + infrastructure ------------------------------
        if hdr.startswith("@@ -114,"):
            if "static mempool_t *sd_528_page_pool;" in text:
                print("  hunk 1  : already applied")
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
            print("  hunk 1  : inserted %d lines before and %d after the anchor" % (len(pre), len(post)))

        # --- hunk 9: queue limit call --------------------------------------
        elif hdr.startswith("@@ -3740,"):
            if "sd_528_limit_queue_depth(sdkp);" in text:
                print("  hunk 9  : already applied")
                continue
            text = insert_before(text, ANCHOR_HUNK9,
                                 "\tsd_528_limit_queue_depth(sdkp);\n", "hunk9")
            print("  hunk 9  : inserted the sd_528_limit_queue_depth() call")

        # --- hunk 10: request size cap -------------------------------------
        elif hdr.startswith("@@ -3809,"):
            if "sd_528_effective_max_sectors()" in text:
                print("  hunk 10 : already applied")
                continue
            text = insert_after(text, ANCHOR_HUNK10, HUNK10_68.strip("\n"), "hunk10")
            print("  hunk 10 : inserted the max_dev_sectors cap (translated to q->limits)")

        else:
            print("  unknown hunk %s - skipping" % hdr.strip())

    sd_c.write_text(text)
    print("wrote %s (%d lines)" % (sd_c, text.count("\n")))

    print("\nsymbol check:")
    ok = True
    for sym in ("static mempool_t *sd_528_page_pool;",
                "sd_emulate_512_from_fat_sectors",
                "sd_528_limit_queue_depth",
                "sd_528_effective_max_sectors",
                "sd_528_segments_to_sectors"):
        present = sym in text
        ok = ok and present
        print("  %-38s %s" % (sym, "OK" if present else "MISSING"))

    # any leftover reference to a non-existent lim-> ?
    leftovers = re.findall(r"lim->[a-z_]+", text)
    if leftovers:
        print("\n  WARNING, leftover references to lim->: %s" % sorted(set(leftovers)))
        ok = False

    print("\n%s" % ("all in place" if ok else "something is missing, it may not compile"))


if __name__ == "__main__":
    main()
