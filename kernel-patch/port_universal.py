#!/usr/bin/env python3
"""Apply the wvg-sd-528 emulation to any recent kernel tree, and keep TRIM working.

The upstream patch is a plain diff, so it only applies to the tree it was cut
against. Three things move between kernel versions and break it:

  * how queue limits reach sd_revalidate_disk(),
  * where the module init/exit error labels sit,
  * the signature of sd_config_discard().

This script detects which generation the tree belongs to and inserts the
version-specific pieces itself, so the same tool works on 6.8, on 6.11 to 6.14
and on 7.0.

    generation  queue limits in sd_revalidate_disk   detected by
    ----------  ---------------------------------    -----------------------
    "none"      not present (<= 6.10)                no queue_limits in file
    "local"     struct queue_limits lim; + lim.      "lim." present
    "ptr"       struct queue_limits *lim + lim->     "lim->" present (7.0)

It also narrows the block-op restriction so that discard keeps working; see
the sd_528_restrict_block_ops() docstring below.

Usage:
    patch -p1 --forward < wvg-sd-528.patch    # lands what it can
    python3 port_universal.py <kernel-tree>   # fixes up the rest

Idempotent: running it twice changes nothing.
"""
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- anchors
# Chosen because they have been stable across every version checked.
A_GLOBALS = "static struct lock_class_key sd_bio_compl_lkclass;"
A_EXIT = "\tmempool_destroy(sd_page_pool);"

# sd_revalidate_disk() bails with a goto in older trees and returns -ENODEV in
# 7.0, and init_sd() lost the .gendrv member. Try each shape in turn.
A_ONLINE = ["\tif (!scsi_device_online(sdp))\n\t\treturn -ENODEV;",
            "\tif (!scsi_device_online(sdp))\n\t\tgoto out;"]
A_INIT = ["\terr = scsi_register_driver(&sd_template);",
          "\terr = scsi_register_driver(&sd_template.gendrv);"]

POOL_CREATE = """	sd_528_ctx_pool = mempool_create_kmalloc_pool(
		SD_528_CTX_POOL_SIZE, sizeof(struct sd_528_emulation_ctx));
	if (!sd_528_ctx_pool) {
		printk(KERN_ERR "sd: can't init 528 emulation context pool\\n");
		err = -ENOMEM;
		goto err_out_528_ctx_pool;
	}

	sd_528_page_pool = mempool_create(
		SD_528_MEMPOOL_SIZE,
		sd_528_mempool_page_alloc,
		sd_528_mempool_page_free,
		(void *)(unsigned long)SD_528_MAX_CHUNK_ORDER);
	if (!sd_528_page_pool) {
		mempool_destroy(sd_528_ctx_pool);
		printk(KERN_ERR "sd: can't init 528 emulation page pool\\n");
		err = -ENOMEM;
		goto err_out_driver;
	}
"""

POOL_DESTROY = """	mempool_destroy(sd_528_page_pool);
	mempool_destroy(sd_528_ctx_pool);
"""

# Cap on max_dev_sectors while emulation is active. One variant per generation.
CAP_PTR = """
	if (sdkp->emulate_512_from_528) {
		unsigned int emu_cap = sd_528_effective_max_sectors();

		emu_cap = min_t(unsigned int, emu_cap,
				sd_528_segments_to_sectors(lim->max_segments));
		lim->max_dev_sectors = min_t(unsigned int,
					     lim->max_dev_sectors, emu_cap);
	}
"""

CAP_LOCAL = CAP_PTR.replace("lim->", "lim.")

CAP_NONE = """
	if (sdkp->emulate_512_from_528) {
		unsigned int emu_cap = sd_528_effective_max_sectors();

		emu_cap = min_t(unsigned int, emu_cap,
				sd_528_segments_to_sectors(q->limits.max_segments));
		q->limits.max_dev_sectors = min_t(unsigned int,
						  q->limits.max_dev_sectors,
						  emu_cap);
	}
"""

# ------------------------------------------------------------------ TRIM
# The upstream patch turns off discard together with WRITE SAME. That is more
# than it needs to.
#
# WRITE SAME has to go: it carries a one-logical-block payload that the device
# expects at 528 bytes, and the emulation does not resize command payloads.
#
# UNMAP can stay. It carries only LBA descriptors, no block data, and the
# emulation maps host LBA N onto device LBA N one to one, so the descriptors
# are already correct. The emulation hook sits in sd_setup_read_write_cmnd(),
# while UNMAP is built by sd_setup_unmap_cmnd(), so the command never passes
# through the bounce path at all.
RESTRICT_PTR = """static void sd_528_restrict_block_ops(struct scsi_disk *sdkp,
					      struct queue_limits *lim)
{
	/* WRITE SAME carries a block payload the emulation cannot resize. */
	sdkp->lbpws = 0;
	sdkp->lbpws10 = 0;
	sdkp->ws10 = 0;
	sdkp->ws16 = 0;
	sdkp->max_ws_blocks = 0;
	sdkp->zeroing_mode = SD_ZERO_WRITE;
	lim->max_write_zeroes_sectors = 0;

	/* UNMAP carries only LBA descriptors and the LBA mapping is 1:1,
	 * so it passes straight through to the device.
	 */
	if (sdkp->lbpu) {
		sdkp->provisioning_mode = SD_LBP_UNMAP;
		sd_config_discard(sdkp, lim, SD_LBP_UNMAP);
	} else {
		sdkp->provisioning_mode = SD_LBP_DISABLE;
		lim->max_hw_discard_sectors = 0;
		lim->max_discard_sectors = 0;
	}
}"""

RESTRICT_NONE = """static void sd_528_restrict_block_ops(struct scsi_disk *sdkp)
{
	struct request_queue *q = sdkp->disk->queue;

	/* WRITE SAME carries a block payload the emulation cannot resize. */
	sdkp->lbpws = 0;
	sdkp->lbpws10 = 0;
	sdkp->ws10 = 0;
	sdkp->ws16 = 0;
	sdkp->max_ws_blocks = 0;
	sdkp->zeroing_mode = SD_ZERO_WRITE;
	blk_queue_max_write_zeroes_sectors(q, 0);

	/* UNMAP carries only LBA descriptors and the LBA mapping is 1:1,
	 * so it passes straight through to the device.
	 */
	if (sdkp->lbpu) {
		sdkp->provisioning_mode = SD_LBP_UNMAP;
		sd_config_discard(sdkp, SD_LBP_UNMAP);
	} else {
		sdkp->provisioning_mode = SD_LBP_DISABLE;
		blk_queue_max_discard_sectors(q, 0);
	}
}"""


def detect(text):
    """Which queue-limits generation is this tree?"""
    if "lim->" in text:
        return "ptr"
    if re.search(r"\blim\.", text):
        return "local"
    return "none"


def first_anchor(text, anchors):
    """Return the first of several candidate anchors that occurs in text."""
    for a in anchors:
        if a in text:
            return a
    return None


def insert_after(text, anchor, payload, log, what):
    if anchor not in text:
        log.append("  %-26s anchor not found, SKIPPED" % what)
        return text
    i = text.index(anchor) + len(anchor)
    log.append("  %-26s inserted" % what)
    return text[:i] + "\n" + payload + text[i:]


def main():
    tree = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    sd_c = tree / "drivers/scsi/sd.c"
    if not sd_c.exists():
        raise SystemExit("not a kernel tree: %s missing" % sd_c)

    text = sd_c.read_text()
    gen = detect(text)
    log = ["generation: %s" % gen]

    # 1) infrastructure from hunk 1 must already be there; it is too large to
    #    re-emit here, so require the plain patch to have landed it or bail.
    if "sd_528_page_pool" not in text:
        raise SystemExit(
            "hunk 1 (emulation infrastructure) is not present.\n"
            "Apply wvg-sd-528.patch first, then run this script;\n"
            "if hunk 1 was rejected, apply it from drivers/scsi/sd.c.rej by hand.")

    # 2) queue depth call in sd_revalidate_disk
    if "sd_528_limit_queue_depth(sdkp);" in text:
        log.append("  %-26s already present" % "queue depth call")
    else:
        a = first_anchor(text, A_ONLINE)
        if a:
            text = insert_after(text, a, "\tsd_528_limit_queue_depth(sdkp);\n",
                                log, "queue depth call")
        else:
            log.append("  %-26s no anchor matched, SKIPPED" % "queue depth call")

    # 3) max_dev_sectors cap, generation specific
    if "emu_cap" in text:
        log.append("  %-26s already present" % "max_dev_sectors cap")
    else:
        cap = {"ptr": CAP_PTR, "local": CAP_LOCAL, "none": CAP_NONE}[gen]
        anchor = {
            "ptr": "\tlim->max_dev_sectors = logical_to_sectors(sdp, dev_max);",
            "local": "\tlim.max_dev_sectors = logical_to_sectors(sdp, dev_max);",
            "none": "\tq->limits.max_dev_sectors = logical_to_sectors(sdp, dev_max);",
        }[gen]
        text = insert_after(text, anchor, cap.strip("\n"), log, "max_dev_sectors cap")

    # 4) module init / exit pools
    if "sd_528_ctx_pool = mempool_create" in text:
        log.append("  %-26s already present" % "pool create")
    else:
        a = first_anchor(text, A_INIT)
        idx = text.find(a) if a else -1
        if idx < 0:
            log.append("  %-26s no anchor matched, SKIPPED" % "pool create")
        else:
            text = text[:idx] + POOL_CREATE + "\n" + text[idx:]
            log.append("  %-26s inserted" % "pool create")

    if "mempool_destroy(sd_528_page_pool)" in text:
        log.append("  %-26s already present" % "pool destroy")
    else:
        idx = text.find(A_EXIT)
        if idx < 0:
            log.append("  %-26s anchor not found, SKIPPED" % "pool destroy")
        else:
            text = text[:idx] + POOL_DESTROY + text[idx:]
            log.append("  %-26s inserted" % "pool destroy")

    # 5) TRIM: replace the blanket disable with the narrow restriction
    if "sd_528_restrict_block_ops" in text:
        log.append("  %-26s already present" % "TRIM restriction")
    else:
        restrict = RESTRICT_PTR if gen == "ptr" else RESTRICT_NONE
        m = re.search(r"static void sd_disable_advanced_block_ops\([^)]*\)\s*\{.*?\n\}",
                      text, re.S)
        if m:
            text = text[:m.start()] + restrict + text[m.end():]
            text = text.replace("sd_disable_advanced_block_ops(",
                                "sd_528_restrict_block_ops(")
            log.append("  %-26s replaced blanket disable" % "TRIM restriction")
        else:
            log.append("  %-26s sd_disable_advanced_block_ops not found" % "TRIM restriction")

    sd_c.write_text(text)

    print("\n".join(log))
    print()
    leftover = sorted(set(re.findall(r"\blim->[a-z_]+", text))) if gen != "ptr" else []
    print("  stray lim-> references: %s" % (leftover if leftover else "none"))
    for sym in ("sd_528_page_pool", "sd_528_limit_queue_depth", "emu_cap",
                "sd_528_restrict_block_ops", "SD_LBP_UNMAP"):
        print("  %-28s %s" % (sym, "present" if sym in text else "MISSING"))


if __name__ == "__main__":
    main()
