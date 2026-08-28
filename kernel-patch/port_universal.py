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
	if (sdkp->lbpu && sdkp->max_unmap_blocks) {
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
	if (sdkp->lbpu && sdkp->max_unmap_blocks) {
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

    # 5) TRIM: replace the blanket disable with the narrow restriction.
    #    sd_config_discard() is defined several hundred lines below the point
    #    where hunk 1 inserts the emulation block, and sd.c does not forward
    #    declare it, so the call needs a prototype or the build fails on an
    #    implicit declaration.
    FWD = {
        "ptr": "static void sd_config_discard(struct scsi_disk *sdkp,\n"
               "\t\tstruct queue_limits *lim, unsigned int mode);\n",
        "local": "static void sd_config_discard(struct scsi_disk *sdkp,\n"
                 "\t\tstruct queue_limits *lim, unsigned int mode);\n",
        "none": "static void sd_config_discard(struct scsi_disk *sdkp,\n"
                "\t\tunsigned int mode);\n",
    }[gen]
    if "sd_config_discard" in text.split(A_GLOBALS)[0]:
        log.append("  %-26s already declared" % "discard prototype")
    else:
        a = first_anchor(text, ["static void  sd_revalidate_disk(struct gendisk *);",
                                "static void sd_revalidate_disk(struct gendisk *);",
                                A_GLOBALS])
        if a:
            i = text.index(a)
            text = text[:i] + FWD + text[i:]
            log.append("  %-26s inserted" % "discard prototype")
        else:
            log.append("  %-26s no anchor matched, SKIPPED" % "discard prototype")

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

    # 5b) An older port may have written the weak test. UNMAP is only usable
    #     when MAXIMUM UNMAP LBA COUNT is non-zero, the same condition the
    #     stock sd_discard_mode() applies.
    if "if (sdkp->lbpu) {" in text:
        text = text.replace("if (sdkp->lbpu) {",
                            "if (sdkp->lbpu && sdkp->max_unmap_blocks) {")
        log.append("  %-26s guarded on max_unmap_blocks" % "UNMAP condition")


    # 6) Placement of both calls the patch adds.
    #    They have to run after sd_read_block_provisioning(),
    #    sd_config_discard() and sd_read_write_same(), or the stock code that
    #    follows overwrites everything they set -- including the WRITE SAME
    #    disable that keeps an unresizable payload off the wire.
    #    The patch instead puts the restriction inside sd_read_capacity(), and
    #    anchors the queue-depth cap on `if (!scsi_device_online(sdp))`, whose
    #    first occurrence in sd.c is in sd_sync_cache(). A cache flush is not
    #    where queue depth belongs, and on an idle disk it never runs at all,
    #    so the cap silently never applies. Move both to the end of
    #    sd_revalidate_disk().
    A_LATE = ["\t\tsd_config_protection(sdkp, lim);\n",
              "\t\tsd_config_protection(sdkp);\n",
              "\t\tsd_read_security(sdkp, buffer);\n"]
    args = "sdkp, lim" if gen == "ptr" else "sdkp"
    BLOCK = ("\n\t\tif (sdkp->emulate_512_from_fat) {\n"
             "\t\t\tsd_528_restrict_block_ops(%s);\n"
             "\t\t\tsd_528_limit_queue_depth(sdkp);\n"
             "\t\t}\n" % args)
    if BLOCK in text:
        log.append("  %-26s already placed" % "late call block")
    else:
        text = re.sub(r"[ \t]*if \(sdkp->emulate_512_from_fat\)\n"
                      r"[ \t]*sd_528_restrict_block_ops\([^;]*\);\n", "", text)
        text = re.sub(r"[ \t]*sd_528_limit_queue_depth\(sdkp\);\n", "", text)
        late = first_anchor(text, A_LATE)
        if late:
            i = text.index(late) + len(late)
            text = text[:i] + BLOCK + text[i:]
            log.append("  %-26s moved after %s" % ("late call block", late.strip()))
        else:
            log.append("  %-26s anchor not found, SKIPPED" % "late call block")

    # 7) init_sd() frees the context pool twice when the page pool fails to
    #    allocate: once inline, then again through the err_out_528_page_pool
    #    label it jumps past. The label itself is left unreferenced, which is
    #    exactly the warning gcc emits. Route the failure through the label.
    DOUBLE_FREE = re.compile(
        r"(\tif \(!sd_528_page_pool\) \{\n)"
        r"\t\tmempool_destroy\(sd_528_ctx_pool\);\n"
        r"(\t\tprintk\(KERN_ERR [^\n]*\n\t\terr = -ENOMEM;\n\t\tgoto )err_out_driver(;\n\t\})")
    text, n = DOUBLE_FREE.subn(r"\1\2err_out_528_page_pool\3", text)
    if n:
        log.append("  %-26s double free removed" % "init_sd error path")
    elif "goto err_out_528_page_pool;" in text:
        log.append("  %-26s already fixed" % "init_sd error path")
    else:
        log.append("  %-26s pattern not matched, SKIPPED" % "init_sd error path")


    # 9) Make the reserve sizes boot parameters.
    #    SD_528_MEMPOOL_SIZE and SD_528_CTX_POOL_SIZE are plain counts, never
    #    array bounds, so nothing stops them being tunable. Measured on eight
    #    disks, the pool is what caps large-request throughput, and leaving the
    #    only way to change it as an edit-and-rebuild makes that impossible to
    #    tune in place. The enum in sd.h stays as the default.
    PARAMS = (
        "static unsigned int sd_528_pool_chunks = SD_528_MEMPOOL_SIZE;\n"
        "static unsigned int sd_528_pool_contexts = SD_528_CTX_POOL_SIZE;\n"
        "module_param_named(emulate_528_pool_chunks, sd_528_pool_chunks,\n"
        "\t\t   uint, 0444);\n"
        "MODULE_PARM_DESC(emulate_528_pool_chunks,\n"
        "\t\t \"Bounce chunks reserved for 528-byte emulation, 64 KiB each "
        "(read at init only)\");\n"
        "module_param_named(emulate_528_pool_contexts, sd_528_pool_contexts,\n"
        "\t\t   uint, 0444);\n"
        "MODULE_PARM_DESC(emulate_528_pool_contexts,\n"
        "\t\t \"Preallocated 528-byte emulation contexts (read at init only)\");\n"
    )
    if "sd_528_pool_chunks" in text:
        log.append("  %-26s already present" % "pool size parameters")
    else:
        a = first_anchor(text, ["MODULE_PARM_DESC(emulate_528_max_sectors,\n"])
        if not a:
            log.append("  %-26s anchor not found, SKIPPED" % "pool size parameters")
        else:
            i = text.index(a) + len(a)
            i = text.index("\n", i) + 1          # past the description string
            text = text[:i] + PARAMS + text[i:]
            # the two counts are used in the depth cap and in init_sd
            text = text.replace("SD_528_MEMPOOL_SIZE / chunks",
                                "sd_528_pool_chunks / chunks")
            text = text.replace("page_cap, SD_528_CTX_POOL_SIZE",
                                "page_cap, sd_528_pool_contexts")
            text = text.replace("\t\tSD_528_CTX_POOL_SIZE, sizeof(struct sd_528_emulation_ctx));",
                                "\t\tsd_528_pool_contexts, sizeof(struct sd_528_emulation_ctx));")
            text = text.replace("\t\tSD_528_MEMPOOL_SIZE,\n",
                                "\t\tsd_528_pool_chunks,\n")

    # 9b) A zero or absurd reserve either starves the pool or fails the
    #     allocation at init, which means no sd driver and no root device.
    #     Clamp what the boot line asks for.
    CLAMP = ("\tsd_528_pool_chunks = clamp_t(unsigned int, sd_528_pool_chunks,\n"
             "\t\t\t\t     1, 65536);\n"
             "\tsd_528_pool_contexts = clamp_t(unsigned int, sd_528_pool_contexts,\n"
             "\t\t\t\t       1, 65536);\n")
    b = "\tSCSI_LOG_HLQUEUE(3, printk(\"init_sd: sd driver entry point\\n\"));\n"
    if "sd_528_pool_chunks = clamp_t" in text:
        log.append("  %-26s already clamped" % "pool size parameters")
    elif b in text:
        j = text.index(b) + len(b)
        text = text[:j] + "\n" + CLAMP + text[j:]
        log.append("  %-26s clamped in init_sd" % "pool size parameters")
    else:
        log.append("  %-26s clamp anchor not found" % "pool size parameters")
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
