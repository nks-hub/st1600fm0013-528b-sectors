#!/usr/bin/env python3
"""Apply the wvg-sd-528 emulation to any recent kernel tree, and keep TRIM working.

The upstream patch is a plain diff, so it only applies to the tree it was cut
against. Three things move between kernel versions and break it:

  * how queue limits reach sd_revalidate_disk(),
  * where the module init/exit error labels sit,
  * the signature of sd_config_discard().

This script detects which generation the tree belongs to and inserts the
version-specific pieces itself. Checked to build with W=1 on 6.8, 6.14, 6.17
and 7.0.

    generation  queue limits in sd_revalidate_disk   detected by
    ----------  ---------------------------------    ---------------------------
    "none"      not present (<= 6.10)                neither form in the function
    "local"     struct queue_limits lim; + lim.      "struct queue_limits lim;"
    "ptr"       struct queue_limits *lim + lim->     "struct queue_limits *lim"

It also narrows the block-op restriction so that discard keeps working; see
the sd_528_restrict_block_ops() docstring below. Step 10 (harden()) fixes the
data-integrity bugs in the emulation itself; see kernel-patch/HARDENING.md.

Usage:
    patch -p1 --forward --fuzz=3 < wvg-sd-528.patch   # lands what it can
    python3 port_universal.py <kernel-tree>          # fixes up the rest

--fuzz=3 is needed for hunk 1 on 7.0. It also lets some hunks land in the
wrong place; the steps below move every one of them to where it belongs.

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


# --------------------------------------------------------------- hardening
# Step 10 below. The upstream emulation swaps the command's data buffer for
# the 528-byte bounce table in sd_setup_read_write_cmnd() and swaps it back
# in sd_done(). sd_done() is not the end of a command's life, though:
#
#   * after a unit attention or DID_RESET, scsi_io_completion() retries the
#     very same command without preparing it again (ACTION_RETRY). It went
#     back to the device with the 512-byte host buffer and a CDB that still
#     asked for 528-byte blocks. Reads returned stale bounce contents as
#     success, writes stored shifted data and reported success. Reproduced
#     under QEMU with scsi_debug on 7.0, see kernel-patch/HARDENING.md.
#   * a prepared command the core fails before completion (device offline
#     with commands on the requeue list) is freed with the bounce table still
#     installed, and scsi_free_sgtables() walks it as a chained table.
#   * rq->end_io_data belongs to whoever owns the request. dm-multipath keeps
#     its clone state there, and sd_uninit_command() freed it as an emulation
#     context on every request, emulated or not.
#
# The replacement keeps the bounce table installed from preparation until
# sd_uninit_command(), finds the context from the table itself instead of
# end_io_data, and decides per command rather than from the disk flag, which
# a rescan rewrites while I/O is in flight.

CTX_LOOKUP = r"""/*
 * The emulation context of a command whose data buffer is the bounce table,
 * or NULL. sd_528_prepare_emulation() installs the bounce table with
 * orig_nents == 0. The SCSI core never builds a data table like that (it
 * always has orig_nents >= nents >= 1), and it makes scsi_free_sgtables()
 * leave the bounce table alone; sd_528_free_emulation() frees the host table.
 */
static struct sd_528_emulation_ctx *sd_528_cmd_ctx(struct scsi_cmnd *cmd)
{
	struct sg_table *t = &cmd->sdb.table;
	struct sd_528_emulation_ctx *ctx;

	if (!t->sgl || !t->nents || t->orig_nents)
		return NULL;

	ctx = container_of(t->sgl, struct sd_528_emulation_ctx, bounce_sgl[0]);
	if (WARN_ON_ONCE(ctx->cmd != cmd))
		return NULL;
	return ctx;
}

"""

ADJUST = r"""static unsigned int sd_adjust_logical_sector_size(struct scsi_disk *sdkp,
						  unsigned int sector_size)
{
	bool emulate = false;
	unsigned int ratio;

	/*
	 * Only 528 has a bounce path. 520 used to be accepted here with
	 * nothing behind it, so every 520-byte block would have been fed a
	 * 512-byte payload. Leave it unsupported, as the stock driver does.
	 *
	 * Once a disk is emulated it stays emulated while it still reports
	 * 528: clearing the parameter and rescanning must not turn a live
	 * disk into a 0-byte one under an imported pool.
	 */
	if (sector_size == SD_528_DEV_SECTOR_SIZE)
		emulate = sd_emulate_512_from_fat_sectors ||
			  sdkp->emulate_512_from_528;
	else if (sector_size == 520 && sd_emulate_512_from_fat_sectors)
		sd_first_printk(KERN_WARNING, sdkp,
				"520-byte sectors are not emulated, only 528\n");

	/*
	 * Write the flags only when they change. Clearing them first and
	 * setting them again, as before, left a window in which I/O in flight
	 * during a rescan saw an unemulated disk.
	 */
	if (sdkp->emulate_512_from_528 != emulate) {
		sdkp->emulate_512_from_fat = emulate;
		sdkp->emulate_512_from_528 = emulate;
		if (emulate)
			sd_printk(KERN_NOTICE, sdkp,
				  "Emulating 512-byte sectors on 528-byte media, 16 bytes per sector are not used\n");
	}
	sdkp->device_sector_size = sector_size;
	if (!emulate)
		return sector_size;

	sdkp->protection_type = 0;

	/*
	 * Keep the device's logical-per-physical exponent. These drives
	 * report 8 x 528, which is 8 x 512 = 4096 on the host side. A 512-byte
	 * physical size would steer ZFS to ashift=9 on a plain zpool add.
	 */
	ratio = sdkp->physical_block_size / SD_528_DEV_SECTOR_SIZE;
	if (sdkp->physical_block_size % SD_528_DEV_SECTOR_SIZE == 0 &&
	    is_power_of_2(ratio))
		sdkp->physical_block_size = ratio * SD_528_HOST_SECTOR_SIZE;
	else
		sdkp->physical_block_size = SD_528_HOST_SECTOR_SIZE;

	return SD_528_HOST_SECTOR_SIZE;
}"""

FREE = r"""static void sd_528_free_emulation(struct scsi_cmnd *cmd)
{
	struct sd_528_emulation_ctx *ctx = sd_528_cmd_ctx(cmd);

	if (!ctx)
		return;

	/*
	 * Put the host table back and free it; the core skipped it while the
	 * bounce table was installed. Clearing nents keeps a later
	 * scsi_free_sgtables() from freeing it a second time.
	 */
	cmd->sdb = ctx->orig_sdb;
	scsi_free_sgtables(cmd);
	cmd->sdb.table.nents = 0;
	ctx->cmd = NULL;
	sd_528_release_emulation_ctx(ctx);
}"""

PREPARE = r"""static blk_status_t sd_528_prepare_emulation(struct scsi_cmnd *cmd, bool write,
					     unsigned int blocks)
{
	struct request *rq = scsi_cmd_to_rq(cmd);
	const struct queue_limits *lim = &rq->q->limits;
	struct sd_528_emulation_ctx *ctx;
	unsigned int bounce_segments = sd_528_blocks_to_chunks(blocks);
	int ret;

	/* blocks is what the CDB transfers, not necessarily blk_rq_bytes() */
	if (!blocks || blocks > SD_528_MAX_HOST_SECTORS)
		return BLK_STS_IOERR;

	if (lim->max_segments && bounce_segments > lim->max_segments)
		return BLK_STS_IOERR;

	/*
	 * Bounce segments are up to SD_528_CHUNK_USED_BYTES long and do not
	 * end on a page boundary. Refuse a host that cannot take that rather
	 * than hand it a table that breaks its limits.
	 */
	if (lim->virt_boundary_mask ||
	    lim->max_segment_size < min(blocks * SD_528_DEV_SECTOR_SIZE,
					SD_528_CHUNK_USED_BYTES))
		return BLK_STS_IOERR;

	/* Protection data would be laid out for 512-byte blocks. */
	if (scsi_prot_sg_count(cmd))
		return BLK_STS_IOERR;

	ctx = mempool_alloc(sd_528_ctx_pool, GFP_ATOMIC);
	if (!ctx)
		return BLK_STS_RESOURCE;

	ctx->cmd = cmd;
	ctx->host_len = blocks * SD_528_HOST_SECTOR_SIZE;
	ctx->dev_len = blocks * SD_528_DEV_SECTOR_SIZE;
	ctx->nr_chunks = 0;
	ctx->bounce_sgt.sgl = ctx->bounce_sgl;
	ctx->bounce_sgt.nents = 0;
	ctx->bounce_sgt.orig_nents = 0;

	ret = sd_528_alloc_emulation_buffer(ctx);
	if (ret != BLK_STS_OK)
		goto resource;

	if (write) {
		ret = sd_528_pack_sg_blocks(&cmd->sdb, &ctx->bounce_sgt,
					    blocks);
		if (ret)
			goto ioerr;
	}

	ctx->orig_sdb = cmd->sdb;
	cmd->sdb.table = ctx->bounce_sgt;
	cmd->sdb.table.orig_nents = 0;		/* see sd_528_cmd_ctx() */
	cmd->sdb.length = ctx->dev_len;
	return BLK_STS_OK;

resource:
	ctx->cmd = NULL;
	sd_528_release_emulation_ctx(ctx);
	return BLK_STS_RESOURCE;
ioerr:
	ctx->cmd = NULL;
	sd_528_release_emulation_ctx(ctx);
	return BLK_STS_IOERR;
}"""

COMPLETE = r"""static unsigned int sd_528_complete_emulation(struct scsi_cmnd *cmd,
					      struct sd_528_emulation_ctx *ctx,
					      unsigned int good_bytes)
{
	struct request *rq = scsi_cmd_to_rq(cmd);
	unsigned int dev_good, host_good;

	/*
	 * good_bytes counts bounce bytes. Short of the whole transfer it is
	 * not trusted: on a medium error sd_completed_bytes() mixes host and
	 * device units, and NO SENSE means an unknown amount arrived. Only
	 * whole 528-byte blocks the device confirmed are passed up.
	 */
	if (good_bytes < ctx->dev_len)
		dev_good = 0;
	else if (cmd->result)
		dev_good = ctx->dev_len;	/* recovered error */
	else
		dev_good = ctx->dev_len - min(scsi_get_resid(cmd), ctx->dev_len);

	host_good = dev_good / SD_528_DEV_SECTOR_SIZE * SD_528_HOST_SECTOR_SIZE;

	if (req_op(rq) == REQ_OP_READ && host_good &&
	    sd_528_unpack_sg_bytes(&ctx->orig_sdb, &ctx->bounce_sgt,
				   host_good)) {
		set_host_byte(cmd, DID_ERROR);
		host_good = 0;
	}

	/*
	 * The bounce table stays installed. scsi_io_completion() may send
	 * this exact command again without preparing it (ACTION_RETRY after
	 * a unit attention or DID_RESET), and the retry has to carry the
	 * 528-byte buffer its CDB describes. sd_528_free_emulation() tears it
	 * down once the core is done with the command.
	 */
	scsi_set_resid(cmd, ctx->host_len - host_good);
	return host_good;
}"""


def replace_function(text, name, body):
    """Swap the definition of a static function, found by name, for body."""
    m = re.search(r"^static [^\n;]*\b%s\([^;{]*\)\n\{.*?^\}" % re.escape(name),
                  text, re.S | re.M)
    if not m:
        return text, False
    return text[:m.start()] + body + text[m.end():], True


def harden(text, log):
    """Step 10: make the bounce buffer survive retries and rescans."""
    what = "hardening"
    if "sd_528_cmd_ctx(" in text:
        log.append("  %-26s already present" % what)
        return text

    edits = []

    # the context remembers its command, so a lookup can verify itself
    a = "\tunsigned int nr_chunks;\n\tstruct scatterlist bounce_sgl[SD_528_MAX_CHUNKS];"
    edits.append((a, "\tstruct scsi_cmnd *cmd;\n" + a))

    # call sites in sd_setup_read_write_cmnd(): pass the CDB block count and
    # decide per command from here on
    edits.append(("ret = sd_528_prepare_emulation(cmd, write);",
                  "ret = sd_528_prepare_emulation(cmd, write, nr_blocks);"))
    edits.append(("\tif (sdkp->emulate_512_from_528) {\n"
                  "\t\tcmd->transfersize = sdkp->device_sector_size;\n"
                  "\t\tcmd->underflow = nr_blocks * sdkp->device_sector_size;",
                  "\tif (sd_528_cmd_ctx(cmd)) {\n"
                  "\t\tcmd->transfersize = SD_528_DEV_SECTOR_SIZE;\n"
                  "\t\tcmd->underflow = nr_blocks * SD_528_DEV_SECTOR_SIZE;"))
    edits.append(("\tif (!sdkp->emulate_512_from_528)\n\t\tcmd->sdb.length",
                  "\tif (!sd_528_cmd_ctx(cmd))\n\t\tcmd->sdb.length"))

    # sd_uninit_command()
    edits.append(("\tsd_528_free_emulation(rq);\n",
                  "\tsd_528_free_emulation(SCpnt);\n"))

    # sd_done(): the per-command context, not the disk flag
    edits.append(("\tunsigned int sector_size = sdkp->emulate_512_from_528 ?\n"
                  "\t\tsdkp->device_sector_size : SCpnt->device->sector_size;\n",
                  "\tunsigned int sector_size = SCpnt->device->sector_size;\n"
                  "\tstruct sd_528_emulation_ctx *emu = sd_528_cmd_ctx(SCpnt);\n"))
    # resid is in 528-byte units there and round_up() needs a power of two;
    # the emulation does its own accounting
    edits.append(("\t\tif (resid & (sector_size - 1)) {",
                  "\t\tif (!emu && (resid & (sector_size - 1))) {"))
    edits.append(("\tif (sdkp->emulate_512_from_528) {\n"
                  "\t\tgood_bytes = sd_528_complete_emulation(SCpnt, good_bytes,\n"
                  "\t\t\t\t\t\t       SCpnt->result);\n"
                  "\t\tresult = SCpnt->result;\n"
                  "\t}\n",
                  "\tif (emu)\n"
                  "\t\tgood_bytes = sd_528_complete_emulation(SCpnt, emu,\n"
                  "\t\t\t\t\t\t       good_bytes);\n"))

    edits.append(('"Expose 520/528-byte sectors as 512-byte host sectors when the transport can strip trailing metadata"',
                  '"Expose 528-byte sectors as 512-byte host sectors through a bounce buffer (520 is not supported)"'))

    missing = [old.strip().split("\n")[0] for old, _ in edits if old not in text]
    if missing:
        log.append("  %-26s anchors not found, SKIPPED: %s" % (what, missing))
        return text
    for old, new in edits:
        text = text.replace(old, new, 1)

    for name, body in (("sd_adjust_logical_sector_size", ADJUST),
                       ("sd_528_free_emulation", FREE),
                       ("sd_528_prepare_emulation", PREPARE),
                       ("sd_528_complete_emulation", COMPLETE)):
        text, ok = replace_function(text, name, body)
        if not ok:
            raise SystemExit("hardening: %s not found after the call sites "
                             "were edited; the tree is half done, start over "
                             "from a clean sd.c" % name)

    a = "static void sd_528_release_emulation_ctx("
    text = text.replace(a, CTX_LOOKUP + a, 1)

    # 10b) The 528 pools must be created after the sd_page_pool check, not
    #      between the allocation and its test, which is where patch --fuzz
    #      puts them on 7.0. Otherwise a failed sd_page_pool leaks both.
    blk = re.search(r"\tsd_528_ctx_pool = mempool_create_kmalloc_pool\(.*?"
                    r"goto err_out_528_page_pool;\n\t\}\n\n?", text, re.S)
    init = first_anchor(text, A_INIT)
    if blk and init and text.index(init) > blk.end():
        chunk = blk.group(0).rstrip("\n") + "\n\n"
        text = text[:blk.start()] + text[blk.end():]
        i = text.index(init)
        text = text[:i] + chunk + text[i:]

    log.append("  %-26s bounce table kept until uninit, per-command context" % what)
    return text


POOL_CAP_OLD = """	return min_t(unsigned int, max_sectors, SD_528_MAX_HOST_SECTORS);
}"""

POOL_CAP_NEW = """	max_sectors = min_t(unsigned int, max_sectors, SD_528_MAX_HOST_SECTORS);

	/*
	 * A request needs all its bounce chunks at once, from a reserve that
	 * never grows. One larger than the whole reserve can never be served
	 * and is requeued forever, stalling the disk. Cap it at what the
	 * reserve holds.
	 */
	return min_t(unsigned int, max_sectors,
		     sd_528_pool_chunks * SD_528_BLOCKS_PER_CHUNK);
}"""


def harden_pool(text, log):
    """Step 10c: never allow a request larger than the whole bounce reserve."""
    what = "pool-sized request cap"
    if "sd_528_pool_chunks * SD_528_BLOCKS_PER_CHUNK" in text:
        log.append("  %-26s already present" % what)
        return text
    m = re.search(r"^static unsigned int sd_528_effective_max_sectors\(void\)\n\{.*?^\}",
                  text, re.S | re.M)
    if not m or POOL_CAP_OLD not in m.group(0) or "sd_528_pool_chunks" not in text:
        log.append("  %-26s anchor not found, SKIPPED" % what)
        return text
    body = m.group(0).replace(POOL_CAP_OLD, POOL_CAP_NEW)
    log.append("  %-26s inserted" % what)
    return text[:m.start()] + body + text[m.end():]


def detect(text):
    """Which queue-limits generation is this tree?

    Look only inside sd_revalidate_disk(). Searching the whole file for
    "lim->" answers "ptr" on every tree once hunk 1 has landed, because hunk 1
    itself writes lim->max_discard_sectors, and 6.8 to 6.17 then got the 7.0
    code and failed to build.
    """
    m = re.search(r"^static (?:int|void) sd_revalidate_disk\(struct gendisk \*disk\)"
                  r"\n\{.*?^\}", text, re.S | re.M)
    body = m.group(0) if m else ""
    if "struct queue_limits *lim" in body:
        return "ptr"
    if "struct queue_limits lim;" in body:
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
    # sd_528_page_pool alone is not proof: hunk 12 (the init_sd error labels)
    # mentions it too and lands even when hunk 1 is rejected, which is exactly
    # what happens on vanilla 7.0. Look for the context struct instead.
    if "struct sd_528_emulation_ctx {" not in text:
        raise SystemExit(
            "hunk 1 (emulation infrastructure) is not present.\n"
            "Apply wvg-sd-528.patch first, then run this script;\n"
            "if hunk 1 was rejected, retry with patch --fuzz=3 or apply it\n"
            "from drivers/scsi/sd.c.rej by hand.")

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

    # 3) max_dev_sectors cap, generation specific.
    #    "emu_cap" being somewhere in the file is not enough: with patch
    #    --fuzz the hunk lands wherever its context fits, on 6.8 inside the
    #    kernel-doc comment of sd_format_disk_name(), on 6.14 in the wrong
    #    lim flavour. Without the cap, requests above 4096 sectors fail with
    #    EIO. Take out whatever is there and put the right variant right
    #    after the line it has to follow.
    cap = {"ptr": CAP_PTR, "local": CAP_LOCAL, "none": CAP_NONE}[gen].strip("\n")
    anchor = {
        "ptr": "\tlim->max_dev_sectors = logical_to_sectors(sdp, dev_max);",
        "local": "\tlim.max_dev_sectors = logical_to_sectors(sdp, dev_max);",
        "none": "\tq->limits.max_dev_sectors = logical_to_sectors(sdp, dev_max);",
    }[gen]
    if anchor + "\n" + cap + "\n" in text and text.count("emu_cap = sd_528_effective") == 1:
        log.append("  %-26s already present" % "max_dev_sectors cap")
    else:
        text = re.sub(r"\n?\tif \(sdkp->emulate_512_from_528\) \{\n"
                      r"\t\tunsigned int emu_cap = sd_528_effective_max_sectors\(\);\n"
                      r".*?\n\t\}\n", "\n", text, flags=re.S)
        text = insert_after(text, anchor, cap, log, "max_dev_sectors cap")

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
        # 6.11 to 6.17 keep lim on the stack, but sd_config_discard() already
        # takes a pointer there, so the pointer variant fits both.
        restrict = RESTRICT_PTR if gen in ("ptr", "local") else RESTRICT_NONE
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
    args = {"ptr": "sdkp, lim", "local": "sdkp, &lim", "none": "sdkp"}[gen]
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
    # 10) hardening, see harden() above
    text = harden(text, log)
    text = harden_pool(text, log)
    sd_c.write_text(text)

    print("\n".join(log))
    print()
    leftover = sorted(set(re.findall(r"\blim->[a-z_]+", text))) if gen != "ptr" else []
    print("  stray lim-> references: %s" % (leftover if leftover else "none"))
    for sym in ("sd_528_page_pool", "sd_528_limit_queue_depth", "emu_cap",
                "sd_528_restrict_block_ops", "SD_LBP_UNMAP", "sd_528_cmd_ctx"):
        print("  %-28s %s" % (sym, "present" if sym in text else "MISSING"))


if __name__ == "__main__":
    main()
