#!/usr/bin/env python3
"""Apply the wvg-sd-528 emulation to any recent kernel tree, and keep TRIM working.

The upstream patch is a plain diff, so it only applies to the tree it was cut
against. Every kernel release moves some of the lines it hangs on, and a hunk
that loses its context is rejected. This tool does not depend on context: it
lands the hunks that still apply, then puts every missing piece in by anchoring
on the structure of sd.c (function bodies, declarations, labels), and finally
proves that each piece is present exactly once.

Things that move between kernel versions:

  * how queue limits reach sd_revalidate_disk(),
  * where the module init/exit error labels sit,
  * the signature of sd_config_discard(),
  * what surrounds sd_page_pool and sd_uninit_command() (7.0.0-44 added a large
    page pool and an early return).

    generation  queue limits in sd_revalidate_disk   detected by
    ----------  ---------------------------------    -----------------------
    "none"      not present (<= 6.10)                no queue_limits in file
    "local"     struct queue_limits lim; + lim.      "lim." present
    "ptr"       struct queue_limits *lim + lim->     "lim->" present (7.0)

It also narrows the block-op restriction so that discard keeps working; see
the sd_528_restrict_block_ops() docstring below.

Usage:
    python3 port_universal.py <kernel-tree> [--patch wvg-sd-528.patch]
                              [--emit-patch OUT.patch] [--semantic-only] [--check]

    --patch          the original diff; hunk text is taken from it (default: the
                     file next to this script)
    --emit-patch     also write the result as a unified diff of sd.c and sd.h
                     against the tree as it was before the run, and prove with
                     `patch --dry-run --fuzz=0` that it applies to the original
                     files (the form Proxmox wants in patches/kernel/)
    --semantic-only  do not run GNU patch, insert every piece by anchor (used by
                     the regression test to exercise all insertions)
    --check          change nothing; report what is missing (exit 1 if anything)

Idempotent: running it twice changes nothing. Exit status 0 when every piece is
present exactly once, 2 when something could not be placed (never silent).

"Already there?" is always decided by the definition or the call site, never by
a mention of the symbol: sd_528_page_pool and sd_528_effective_max_sectors
appear in the first hunk even when later hunks are missing, and so does every
symbol that other pieces refer to.
"""
import argparse
import difflib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SD_C = "drivers/scsi/sd.c"
SD_H = "drivers/scsi/sd.h"

# ---------------------------------------------------------------- anchors
# Chosen because they have been stable across every version checked.
A_GLOBALS = "static struct lock_class_key sd_bio_compl_lkclass;"

# init_sd() lost the .gendrv member of sd_template; try each shape in turn.
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

# --------------------------------------------------- small fixed insertions
WS_BLOCK = """	if (sdkp->emulate_512_from_fat) {
		sdkp->max_ws_blocks = 0;
		sdkp->zeroing_mode = SD_ZERO_WRITE;
		goto out;
	}

"""

RW_PREPARE = """	if (sdkp->emulate_512_from_528) {
		ret = sd_528_prepare_emulation(cmd, write);
		if (ret != BLK_STS_OK)
			goto fail;
	}

"""

RW_SIZES = """	if (sdkp->emulate_512_from_528) {
		cmd->transfersize = sdkp->device_sector_size;
		cmd->underflow = nr_blocks * sdkp->device_sector_size;
	} else {
		cmd->transfersize = sdp->sector_size;
		cmd->underflow = nr_blocks << 9;
	}
"""

DONE_SECTOR = """	unsigned int sector_size = sdkp->emulate_512_from_528 ?
		sdkp->device_sector_size : SCpnt->device->sector_size;
"""

DONE_COMPLETE = """	if (sdkp->emulate_512_from_528) {
		good_bytes = sd_528_complete_emulation(SCpnt, good_bytes,
						       SCpnt->result);
		result = SCpnt->result;
	}
"""

H_ENUM = """	SD_528_MEMPOOL_SIZE = 576,  /* 64 KiB aligned bounce chunk reserve */
	SD_528_CTX_POOL_SIZE = 64,  /* preallocated 528 emulation contexts */
"""
H_FIELD_SIZE = "\tunsigned int\tdevice_sector_size;\n"
H_FIELD_BITS = ("\tunsigned\temulate_512_from_fat : 1;\n"
                "\tunsigned\temulate_512_from_528 : 1;\n")

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

CLAMP = ("\tsd_528_pool_chunks = clamp_t(unsigned int, sd_528_pool_chunks,\n"
         "\t\t\t\t     1, 65536);\n"
         "\tsd_528_pool_contexts = clamp_t(unsigned int, sd_528_pool_contexts,\n"
         "\t\t\t\t       1, 65536);\n")


class Incomplete(Exception):
    pass


# ----------------------------------------------------------- text helpers
def detect(text):
    """Which queue-limits generation is this tree?

    Decided on the body of sd_revalidate_disk() alone: the emulation code that
    the patch adds elsewhere in the file also uses `lim->`, so looking at the
    whole file would call every tree "ptr" as soon as the first hunk has landed.
    """
    s, e = function_span(text, r"^static (?:int|void)\s+sd_revalidate_disk\s*\([^)]*\)",
                         "sd_revalidate_disk")
    body = text[s:e]
    if "lim->" in body:
        return "ptr"
    if re.search(r"\blim\.", body):
        return "local"
    return "none"


def first_anchor(text, anchors):
    """Return the first of several candidate anchors that occurs in text."""
    for a in anchors:
        if a in text:
            return a
    return None


def has(text, pattern):
    return re.search(pattern, text, re.M) is not None


def count(text, pattern):
    return len(re.findall(pattern, text, re.M))


def function_span(text, signature, what):
    """(start, end) of the one function *definition* whose header matches.

    A prototype does not count: the header must be followed by an opening brace.
    The body is walked with a small scanner that skips strings, characters and
    comments, so braces inside them do not confuse it.
    """
    valid = [m for m in re.finditer(signature, text, re.M)
             if re.match(r"\s*\{", text[m.end():])]
    if len(valid) != 1:
        raise Incomplete("%s: expected one function definition, found %d"
                         % (what, len(valid)))
    start = valid[0].start()
    i = text.find("{", valid[0].end())
    depth = 0
    state = None
    n = len(text)
    while i < n:
        c = text[i]
        d = text[i + 1] if i + 1 < n else ""
        if state == "line":
            if c == "\n":
                state = None
        elif state == "block":
            if c == "*" and d == "/":
                state = None
                i += 1
        elif state in ('"', "'"):
            if c == "\\":
                i += 1
            elif c == state:
                state = None
        elif c == "/" and d == "/":
            state = "line"
            i += 1
        elif c == "/" and d == "*":
            state = "block"
            i += 1
        elif c in ('"', "'"):
            state = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return start, i + 1
        i += 1
    raise Incomplete("%s: unterminated function body" % what)


def edit_function(text, signature, what, edit):
    """Apply edit(body) -> body to one function, leave the rest of the file."""
    s, e = function_span(text, signature, what)
    return text[:s] + edit(text[s:e]) + text[e:]


def insert_after_exact(text, anchor, payload, what):
    n = text.count(anchor)
    if n != 1:
        raise Incomplete("%s: anchor expected once, found %d: %r" % (what, n, anchor))
    return text.replace(anchor, anchor + payload, 1)


# ------------------------------------------------------- patch text access
def parse_file_hunks(patch_text, wanted_path):
    lines = patch_text.splitlines(keepends=True)
    in_file = False
    hunks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- a/"):
            in_file = line[6:].split("\t", 1)[0].strip() == wanted_path
            i += 1
            continue
        if in_file and line.startswith("@@ "):
            body = []
            i += 1
            while i < len(lines) and not lines[i].startswith(("@@ ", "--- a/")):
                body.append(lines[i])
                i += 1
            hunks.append(body)
            continue
        i += 1
    return hunks


def addition_runs(hunk_lines):
    runs, cur = [], []
    for line in hunk_lines:
        if line.startswith("+") and not line.startswith("+++"):
            cur.append(line[1:])
        elif cur:
            runs.append("".join(cur))
            cur = []
    if cur:
        runs.append("".join(cur))
    return runs


def first_hunk_runs(patch_path):
    """The three blocks hunk 1 adds to sd.c, taken from the original diff."""
    if not patch_path or not Path(patch_path).is_file():
        raise Incomplete("the infrastructure block (hunk 1) is missing and no readable "
                         "--patch file was given to take it from")
    text = Path(patch_path).read_text(encoding="utf-8").replace("\r\n", "\n")
    hunks = parse_file_hunks(text, SD_C)
    if not hunks:
        raise Incomplete("%s has no hunks for %s" % (patch_path, SD_C))
    runs = addition_runs(hunks[0])
    if len(runs) != 3:
        raise Incomplete("unexpected first hunk in %s: %d addition blocks, expected 3"
                         % (patch_path, len(runs)))
    return runs


# ----------------------------------------------------------------- the tool
class Port:
    def __init__(self, tree, patch_path, gen):
        self.tree = Path(tree)
        self.patch_path = patch_path
        self.gen = gen
        self.log = []
        self.c = (self.tree / SD_C).read_text()
        self.h = (self.tree / SD_H).read_text()
        self._runs = None

    def note(self, what, status):
        self.log.append("  %-26s %s" % (what, status))

    def runs(self):
        if self._runs is None:
            self._runs = first_hunk_runs(self.patch_path)
        return self._runs

    # ---- one piece = one (present?, apply) pair --------------------------
    def piece(self, what, present, apply):
        if present():
            self.note(what, "already present")
            return
        try:
            apply()
        except Incomplete as exc:
            self.note(what, "NOT PLACED: %s" % exc)
            return
        self.note(what, "inserted")

    def missing_summary(self):
        return [l for l in self.log if "NOT PLACED" in l]

    # ---- hunks the diff usually lands, with an anchor-based fallback -----
    def sd_c_globals(self):
        c = self

        def a():
            c.c = insert_after_exact(c.c, "static mempool_t *sd_page_pool;\n",
                                     c.runs()[0], "globals")
        c.piece("hunk 1a: pool pointers",
                lambda: has(c.c, r"^static mempool_t \*sd_528_page_pool;"), a)

        def b():
            c.c = insert_after_exact(c.c, A_GLOBALS + "\n", c.runs()[1], "module parameters")
        c.piece("hunk 1b: module params",
                lambda: has(c.c, r"^static bool sd_emulate_512_from_fat_sectors;"), b)

        def d():
            m = re.search(r"^static const char \*sd_cache_types\[\]\s*=\s*\{.*?^\};\n",
                          c.c, re.S | re.M)
            if not m:
                raise Incomplete("sd_cache_types array not found")
            c.c = c.c[:m.end()] + c.runs()[2] + c.c[m.end():]
        c.piece("hunk 1c: emulation core",
                lambda: has(c.c, r"^struct sd_528_bounce_chunk \{"), d)

    def sd_c_small_hunks(self):
        c = self

        def ws():
            def edit(body):
                a = "\tunsigned int logical_block_size = sdkp->device->sector_size;\n\n"
                if body.count(a) != 1:
                    raise Incomplete("logical_block_size line not found once")
                return body.replace(a, a + WS_BLOCK, 1)
            c.c = edit_function(c.c, r"^static void sd_config_write_same\s*\([^)]*\)",
                                "sd_config_write_same", edit)
        c.piece("hunk 2: write same",
                lambda: "emulate_512_from_fat" in function_span_text(
                    c.c, r"^static void sd_config_write_same\s*\([^)]*\)"), ws)

        def rw():
            def edit(body):
                note = "\t/*\n\t * We shouldn't disconnect in the middle of a sector"
                if body.count(note) != 1:
                    raise Incomplete("disconnect comment not found once")
                body = body.replace(note, RW_PREPARE + note, 1)
                m = re.search(r"\t(\w+)->transfersize = sdp->sector_size;\n"
                              r"\t\1->underflow = nr_blocks << 9;\n", body)
                if not m:
                    raise Incomplete("transfersize/underflow lines not found")
                body = body[:m.start()] + RW_SIZES + body[m.end():]
                m = re.search(r"\t(\w+)->sdb\.length = nr_blocks \* sdp->sector_size;\n", body)
                if not m:
                    raise Incomplete("sdb.length line not found")
                return (body[:m.start()]
                        + "\tif (!sdkp->emulate_512_from_528)\n\t" + m.group(0)
                        + body[m.end():])
            c.c = edit_function(c.c, r"^static blk_status_t sd_setup_read_write_cmnd\s*\([^)]*\)",
                                "sd_setup_read_write_cmnd", edit)
        c.piece("hunk 3: read/write cmnd",
                lambda: "sd_528_prepare_emulation(cmd, write)" in function_span_text(
                    c.c, r"^static blk_status_t sd_setup_read_write_cmnd\s*\([^)]*\)"), rw)

        def free():
            def edit(body):
                m = re.search(r"^\tif \(.*RQF_SPECIAL_PAYLOAD.*\n", body, re.M)
                if not m:
                    raise Incomplete("RQF_SPECIAL_PAYLOAD test not found")
                return body[:m.start()] + "\tsd_528_free_emulation(rq);\n" + body[m.start():]
            c.c = edit_function(c.c, r"^static void sd_uninit_command\s*\([^)]*\)",
                                "sd_uninit_command", edit)
        c.piece("hunk 4: uninit command",
                lambda: "sd_528_free_emulation(rq);" in function_span_text(
                    c.c, r"^static void sd_uninit_command\s*\([^)]*\)"), free)

        def done_size():
            def edit(body):
                old = re.search(r"^\tunsigned int sector_size = SCpnt->device->sector_size;\n",
                                body, re.M)
                anchor = re.search(r"^\tstruct scsi_disk \*sdkp = scsi_disk\(req->q->disk\);\n",
                                   body, re.M)
                if not old or not anchor or old.start() > anchor.start():
                    raise Incomplete("sd_done sector_size/sdkp lines not found in order")
                return (body[:old.start()] + body[old.end():anchor.end()]
                        + DONE_SECTOR + body[anchor.end():])
            c.c = edit_function(c.c, r"^static void sd_done\s*\([^)]*\)|"
                                     r"^static int sd_done\s*\([^)]*\)", "sd_done", edit)
        c.piece("hunk 5: sd_done sector size",
                lambda: "sdkp->device_sector_size : SCpnt->device->sector_size" in c.c, done_size)

        def done_complete():
            def edit(body):
                m = re.search(r"^\tif \([^\n]*\)\n"
                              r"\t\tgood_bytes = sd_zbc_complete\([^\n]*\);\n",
                              body, re.M)
                if not m:
                    raise Incomplete("sd_zbc_complete block not found")
                return body[:m.end()] + DONE_COMPLETE + body[m.end():]
            c.c = edit_function(c.c, r"^static void sd_done\s*\([^)]*\)|"
                                     r"^static int sd_done\s*\([^)]*\)", "sd_done", edit)
        c.piece("hunk 7: sd_done complete",
                lambda: has(c.c, r"^\t+good_bytes = sd_528_complete_emulation\("), done_complete)

        def adjust():
            m = re.search(r'(assuming 512\.\\n"\);\n\t\}\n)', c.c)
            if not m:
                raise Incomplete("'Sector size 0 reported' block not found")
            c.c = (c.c[:m.end()]
                   + "\n\tsector_size = sd_adjust_logical_sector_size(sdkp, sector_size);\n"
                   + c.c[m.end():])
        c.piece("hunk 8: adjust sector size",
                lambda: has(c.c, r"^\tsector_size = sd_adjust_logical_sector_size\(sdkp, sector_size\);"),
                adjust)

    def sd_h(self):
        c = self

        def enum():
            m = re.search(r"^\tSD_MEMPOOL_SIZE\s*=\s*\d+,[^\n]*\n", c.h, re.M)
            if not m:
                raise Incomplete("SD_MEMPOOL_SIZE not found in sd.h")
            c.h = c.h[:m.end()] + H_ENUM + c.h[m.end():]
        c.piece("sd.h: pool sizes", lambda: has(c.h, r"^\tSD_528_MEMPOOL_SIZE\s*="), enum)

        def size():
            a = "\tunsigned int\tphysical_block_size;\n"
            c.h = insert_after_exact(c.h, a, H_FIELD_SIZE, "device_sector_size")
        c.piece("sd.h: device_sector_size",
                lambda: has(c.h, r"^\tunsigned int\tdevice_sector_size;"), size)

        def bits():
            a = "\tunsigned\tws16 : 1;\n"
            c.h = insert_after_exact(c.h, a, H_FIELD_BITS, "emulation flags")
        c.piece("sd.h: emulation flags",
                lambda: has(c.h, r"^\tunsigned\temulate_512_from_528 : 1;"), bits)

    # ---- the pieces that always needed a decision of their own -----------
    def revalidate_pieces(self):
        c = self
        gen = c.gen
        c.log.insert(0, "generation: %s" % gen)

        # max_dev_sectors cap
        def cap():
            anchor = {
                "ptr": "\tlim->max_dev_sectors = logical_to_sectors(sdp, dev_max);",
                "local": "\tlim.max_dev_sectors = logical_to_sectors(sdp, dev_max);",
                "none": "\tq->limits.max_dev_sectors = logical_to_sectors(sdp, dev_max);",
            }[gen]
            payload = {"ptr": CAP_PTR, "local": CAP_LOCAL, "none": CAP_NONE}[gen]
            if anchor not in c.c:
                raise Incomplete("max_dev_sectors anchor not found")
            i = c.c.index(anchor) + len(anchor)
            c.c = c.c[:i] + "\n" + payload.strip("\n") + c.c[i:]
        c.piece("max_dev_sectors cap", lambda: "emu_cap" in c.c, cap)

        # init_sd: error labels and pool creation
        def labels():
            def edit(body):
                label = body.find("err_out_driver:")
                if label < 0:
                    raise Incomplete("err_out_driver label not found")
                d = body.find("mempool_destroy(sd_page_pool);", label)
                if d < 0:
                    raise Incomplete("sd_page_pool cleanup not found")
                return (body[:d] + "mempool_destroy(sd_528_page_pool);\n"
                        "err_out_528_page_pool:\n\tmempool_destroy(sd_528_ctx_pool);\n"
                        "err_out_528_ctx_pool:\n\t" + body[d:])
            c.c = edit_function(c.c, r"^static int __init init_sd\s*\(void\)", "init_sd", edit)
        c.piece("init_sd: error labels", lambda: has(c.c, r"^err_out_528_ctx_pool:"), labels)

        def create():
            a = first_anchor(c.c, A_INIT)
            if not a:
                raise Incomplete("scsi_register_driver anchor not found")
            i = c.c.index(a)
            c.c = c.c[:i] + POOL_CREATE + "\n" + c.c[i:]
        c.piece("pool create",
                lambda: has(c.c, r"^\tsd_528_ctx_pool = mempool_create_kmalloc_pool\("), create)

        # exit_sd: the pools are freed in exit_sd itself, not in init_sd's error path
        def destroy():
            def edit(body):
                m = re.search(r"^\tscsi_unregister_driver\([^;]*\);\n", body, re.M)
                if not m:
                    raise Incomplete("scsi_unregister_driver not found in exit_sd")
                return body[:m.end()] + POOL_DESTROY + body[m.end():]
            c.c = edit_function(c.c, r"^static void __exit exit_sd\s*\(void\)", "exit_sd", edit)
        c.piece("exit_sd: pool destroy",
                lambda: "mempool_destroy(sd_528_page_pool)" in function_span_text(
                    c.c, r"^static void __exit exit_sd\s*\(void\)"), destroy)

        # TRIM
        fwd = {
            "ptr": "static void sd_config_discard(struct scsi_disk *sdkp,\n"
                   "\t\tstruct queue_limits *lim, unsigned int mode);\n",
            "local": "static void sd_config_discard(struct scsi_disk *sdkp,\n"
                     "\t\tstruct queue_limits *lim, unsigned int mode);\n",
            "none": "static void sd_config_discard(struct scsi_disk *sdkp,\n"
                    "\t\tunsigned int mode);\n",
        }[gen]

        # sd_config_discard() is defined several hundred lines below the point
        # where hunk 1 inserts the emulation block and sd.c does not forward
        # declare it, so the call needs a prototype or the build fails on an
        # implicit declaration.
        def proto():
            a = first_anchor(c.c, ["static void  sd_revalidate_disk(struct gendisk *);",
                                   "static void sd_revalidate_disk(struct gendisk *);",
                                   A_GLOBALS])
            if not a:
                raise Incomplete("no anchor for the discard prototype")
            i = c.c.index(a)
            c.c = c.c[:i] + fwd + c.c[i:]
        c.piece("discard prototype",
                lambda: has(c.c, r"^static void sd_config_discard\([^)]*\);"), proto)

        def restrict():
            m = re.search(r"static void sd_disable_advanced_block_ops\([^)]*\)\s*\{.*?\n\}",
                          c.c, re.S)
            if not m:
                raise Incomplete("sd_disable_advanced_block_ops not found")
            body = RESTRICT_NONE if gen == "none" else RESTRICT_PTR
            c.c = c.c[:m.start()] + body + c.c[m.end():]
            c.c = c.c.replace("sd_disable_advanced_block_ops(", "sd_528_restrict_block_ops(")
        c.piece("TRIM restriction",
                lambda: has(c.c, r"^static void sd_528_restrict_block_ops\("), restrict)

        # An older port may have written the weak test. UNMAP is only usable
        # when MAXIMUM UNMAP LBA COUNT is non-zero, the same condition the
        # stock sd_discard_mode() applies.
        if "if (sdkp->lbpu) {" in c.c:
            c.c = c.c.replace("if (sdkp->lbpu) {", "if (sdkp->lbpu && sdkp->max_unmap_blocks) {")
            c.note("UNMAP condition", "guarded on max_unmap_blocks")

        # Placement of both calls the patch adds. They have to run after
        # sd_read_block_provisioning(), sd_config_discard() and
        # sd_read_write_same(), or the stock code that follows overwrites
        # everything they set -- including the WRITE SAME disable that keeps an
        # unresizable payload off the wire. The patch puts the restriction
        # inside sd_read_capacity() and anchors the queue-depth cap on
        # `if (!scsi_device_online(sdp))`, whose first occurrence is in
        # sd_sync_cache(), where it never runs on an idle disk. Both go to the
        # end of sd_revalidate_disk().
        late_anchors = ["\t\tsd_config_protection(sdkp, lim);\n",
                        "\t\tsd_config_protection(sdkp, &lim);\n",
                        "\t\tsd_config_protection(sdkp);\n",
                        "\t\tsd_read_security(sdkp, buffer);\n"]
        args = {"ptr": "sdkp, lim", "local": "sdkp, &lim", "none": "sdkp"}[gen]
        block = ("\n\t\tif (sdkp->emulate_512_from_fat) {\n"
                 "\t\t\tsd_528_restrict_block_ops(%s);\n"
                 "\t\t\tsd_528_limit_queue_depth(sdkp);\n"
                 "\t\t}\n" % args)
        # a call the plain patch put at the top of sd_revalidate_disk() is removed first
        if block in c.c:
            c.note("late call block", "already placed")
        else:
            c.c = re.sub(r"[ \t]*if \(sdkp->emulate_512_from_fat\)\n"
                         r"[ \t]*sd_528_restrict_block_ops\([^;]*\);\n", "", c.c)
            c.c = re.sub(r"\n\tsd_528_limit_queue_depth\(sdkp\);\n", "\n", c.c)
            s, e = function_span(c.c, r"^static (?:int|void)\s+sd_revalidate_disk\s*\([^)]*\)",
                                 "sd_revalidate_disk")
            body = c.c[s:e]
            late = first_anchor(body, late_anchors)
            if late:
                i = body.index(late) + len(late)
                c.c = c.c[:s] + body[:i] + block + body[i:] + c.c[e:]
                c.note("late call block", "placed after %s" % late.strip())
            else:
                c.note("late call block", "NOT PLACED: anchor not found")

        # init_sd() frees the context pool twice when the page pool fails to
        # allocate: once inline, then again through the err_out_528_page_pool
        # label it jumps past. Route the failure through the label.
        double_free = re.compile(
            r"(\tif \(!sd_528_page_pool\) \{\n)"
            r"\t\tmempool_destroy\(sd_528_ctx_pool\);\n"
            r"(\t\tprintk\(KERN_ERR [^\n]*\n\t\terr = -ENOMEM;\n\t\tgoto )err_out_driver(;\n\t\})")
        c.c, n = double_free.subn(r"\1\2err_out_528_page_pool\3", c.c)
        if n:
            c.note("init_sd error path", "double free removed")
        elif "goto err_out_528_page_pool;" in c.c:
            c.note("init_sd error path", "already fixed")
        else:
            c.note("init_sd error path", "NOT PLACED: pattern not matched")

        # The reserve sizes are boot parameters. SD_528_MEMPOOL_SIZE and
        # SD_528_CTX_POOL_SIZE are plain counts, never array bounds. Measured
        # on eight disks, the pool is what caps large-request throughput, and
        # a rebuild is a poor way to tune it. The enum in sd.h stays the default.
        def params():
            a = "MODULE_PARM_DESC(emulate_528_max_sectors,\n"
            if a not in c.c:
                raise Incomplete("emulate_528_max_sectors description not found")
            i = c.c.index(a) + len(a)
            i = c.c.index("\n", i) + 1
            c.c = c.c[:i] + PARAMS + c.c[i:]
            c.c = c.c.replace("SD_528_MEMPOOL_SIZE / chunks", "sd_528_pool_chunks / chunks")
            c.c = c.c.replace("page_cap, SD_528_CTX_POOL_SIZE", "page_cap, sd_528_pool_contexts")
            c.c = c.c.replace(
                "\t\tSD_528_CTX_POOL_SIZE, sizeof(struct sd_528_emulation_ctx));",
                "\t\tsd_528_pool_contexts, sizeof(struct sd_528_emulation_ctx));")
            c.c = c.c.replace("\t\tSD_528_MEMPOOL_SIZE,\n", "\t\tsd_528_pool_chunks,\n")
        c.piece("pool size parameters",
                lambda: has(c.c, r"^static unsigned int sd_528_pool_chunks ="), params)

        # A zero or absurd reserve either starves the pool or fails the
        # allocation at init, which means no sd driver and no root device.
        def clamp():
            b = "\tSCSI_LOG_HLQUEUE(3, printk(\"init_sd: sd driver entry point\\n\"));\n"
            if b not in c.c:
                raise Incomplete("init_sd log line not found")
            j = c.c.index(b) + len(b)
            c.c = c.c[:j] + "\n" + CLAMP + c.c[j:]
        c.piece("pool size clamp",
                lambda: has(c.c, r"^\tsd_528_pool_chunks = clamp_t\("), clamp)

    # ---- the final proof --------------------------------------------------
    EXACTLY_ONCE_C = [
        ("pool pointer", r"^static mempool_t \*sd_528_page_pool;"),
        ("context pool pointer", r"^static mempool_t \*sd_528_ctx_pool;"),
        ("module parameter flag", r"^static bool sd_emulate_512_from_fat_sectors;"),
        ("bounce chunk type", r"^struct sd_528_bounce_chunk \{"),
        ("adjust definition", r"^static unsigned int sd_adjust_logical_sector_size\("),
        ("adjust call", r"^\tsector_size = sd_adjust_logical_sector_size\(sdkp, sector_size\);"),
        ("queue depth definition", r"^static void sd_528_limit_queue_depth\("),
        ("queue depth call", r"^\t+sd_528_limit_queue_depth\(sdkp\);"),
        ("restrict definition", r"^static void sd_528_restrict_block_ops\("),
        ("restrict call", r"^\t+sd_528_restrict_block_ops\(sdkp(, &?lim)?\);"),
        ("free call", r"^\tsd_528_free_emulation\(rq\);"),
        ("prepare call", r"^\t\tret = sd_528_prepare_emulation\(cmd, write\);"),
        ("complete call", r"^\t\tgood_bytes = sd_528_complete_emulation\("),
        ("max_dev_sectors cap", r"^\t\tunsigned int emu_cap = sd_528_effective_max_sectors\(\);"),
        ("pool create", r"^\tsd_528_ctx_pool = mempool_create_kmalloc_pool\("),
        ("init error labels", r"^err_out_528_ctx_pool:"),
    ]
    EXACTLY_ONCE_H = [
        ("pool size enum", r"^\tSD_528_MEMPOOL_SIZE\s*="),
        ("device sector size field", r"^\tunsigned int\tdevice_sector_size;"),
        ("emulation flags", r"^\tunsigned\temulate_512_from_528 : 1;"),
    ]

    def verify(self):
        bad = []
        for what, pat in self.EXACTLY_ONCE_C:
            n = count(self.c, pat)
            if n != 1:
                bad.append("sd.c %s: found %d times, expected once" % (what, n))
        for what, pat in self.EXACTLY_ONCE_H:
            n = count(self.h, pat)
            if n != 1:
                bad.append("sd.h %s: found %d times, expected once" % (what, n))
        try:
            if "mempool_destroy(sd_528_page_pool)" not in function_span_text(
                    self.c, r"^static void __exit exit_sd\s*\(void\)"):
                bad.append("sd.c exit_sd does not destroy the pools")
        except Incomplete as exc:
            bad.append(str(exc))
        if "sd_disable_advanced_block_ops" in self.c:
            bad.append("sd.c still refers to sd_disable_advanced_block_ops")
        if self.gen == "none":
            stray = sorted(set(re.findall(r"\blim->[a-z_]+", self.c)))
            if stray:
                bad.append("sd.c has queue_limits references in a tree without them: %s" % stray)
        return bad


def function_span_text(text, signature):
    s, e = function_span(text, signature, signature)
    return text[s:e]


def land_patch(tree, patch_path, log):
    """Let GNU patch land every hunk whose context still matches (-N: skip
    hunks that are already applied, -r -: do not leave .rej files behind)."""
    if not patch_path or not Path(patch_path).is_file():
        log.append("  GNU patch                  skipped (no --patch file)")
        return
    if not shutil.which("patch"):
        log.append("  GNU patch                  skipped (patch not installed)")
        return
    p = subprocess.run(["patch", "-N", "--batch", "-p1", "-r", "-", "-d", str(tree),
                        "-i", str(Path(patch_path).resolve())],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode > 1:
        raise SystemExit("GNU patch failed (%d):\n%s" % (p.returncode, p.stdout))
    bad = len(re.findall(r"Hunk #\d+ FAILED", p.stdout))
    log.append("  GNU patch                  ran; %d hunks lost their context "
               "(placed by anchor below)" % bad)


def unified(old, new, label):
    return "".join(difflib.unified_diff(old.splitlines(keepends=True),
                                        new.splitlines(keepends=True),
                                        "a/" + label, "b/" + label))


def run(tree, patch_path, semantic_only=False, check=False, emit=None):
    tree = Path(tree)
    for rel in (SD_C, SD_H):
        if not (tree / rel).is_file():
            raise SystemExit("not a kernel tree: %s missing" % (tree / rel))
    orig_c = (tree / SD_C).read_text()
    gen = detect(orig_c)
    orig_h = (tree / SD_H).read_text()

    work = tree
    tmp = None
    if check:
        tmp = tempfile.mkdtemp(prefix="port528-")
        work = Path(tmp)
        for rel in (SD_C, SD_H):
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tree / rel, work / rel)

    log = []
    # GNU patch only while something is missing: a finished tree is not touched by it
    if not semantic_only and _incomplete(work):
        land_patch(work, patch_path, log)
    port = Port(work, patch_path, gen)
    port.sd_c_globals()
    port.sd_c_small_hunks()
    port.sd_h()
    port.revalidate_pieces()
    problems = port.missing_summary()
    bad = port.verify()

    print("\n".join(log + port.log))
    print()
    for sym in ("sd_528_page_pool", "sd_528_limit_queue_depth", "emu_cap",
                "sd_528_restrict_block_ops", "SD_LBP_UNMAP"):
        print("  %-28s %s" % (sym, "present" if sym in port.c else "MISSING"))
    for b in bad:
        print("  PROBLEM: %s" % b)

    ok = not problems and not bad
    if ok and not check:
        (tree / SD_C).write_text(port.c)
        (tree / SD_H).write_text(port.h)
    if tmp:
        changed = (port.c != orig_c) or (port.h != orig_h)
        shutil.rmtree(tmp)
        print("\n--check: %s" % ("tree would change" if changed else "tree is complete"))
        return 1 if changed or not ok else 0
    if not ok:
        # leave the tree as it was: a half-ported sd.c is worse than none
        (tree / SD_C).write_text(orig_c)
        (tree / SD_H).write_text(orig_h)
        print("\nINCOMPLETE: the tree was restored, nothing was changed.")
        return 2

    if emit:
        diff = unified(orig_c, port.c, SD_C) + unified(orig_h, port.h, SD_H)
        Path(emit).write_text(diff)
        if shutil.which("patch"):
            t = Path(tempfile.mkdtemp(prefix="port528-verify-"))
            for rel, txt in ((SD_C, orig_c), (SD_H, orig_h)):
                (t / rel).parent.mkdir(parents=True, exist_ok=True)
                (t / rel).write_text(txt)
            p = subprocess.run(["patch", "--dry-run", "--batch", "--fuzz=0", "-p1", "-d", str(t),
                                "-i", str(Path(emit).resolve())],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            shutil.rmtree(t)
            if p.returncode:
                print(p.stdout)
                raise SystemExit("the emitted patch does not apply to the original files")
        print("\npatch written to %s (applies to the original files with fuzz 0)" % emit)
    return 0


def _incomplete(tree):
    """True while any of the pieces is missing, i.e. while GNU patch is worth running."""
    c = (tree / SD_C).read_text()
    h = (tree / SD_H).read_text()
    for pat in (r"^static mempool_t \*sd_528_page_pool;", r"^struct sd_528_bounce_chunk \{",
                r"^\tsector_size = sd_adjust_logical_sector_size\(", r"^\tsd_528_free_emulation\(rq\);",
                r"^\t\tret = sd_528_prepare_emulation\(", r"^\t\tgood_bytes = sd_528_complete_emulation\("):
        if not has(c, pat):
            return True
    return not has(h, r"^\tSD_528_MEMPOOL_SIZE\s*=")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("tree", nargs="?", default=".")
    ap.add_argument("--patch", default=str(Path(__file__).resolve().parent / "wvg-sd-528.patch"))
    ap.add_argument("--emit-patch")
    ap.add_argument("--semantic-only", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    sys.exit(run(a.tree, a.patch, a.semantic_only, a.check, a.emit_patch))


if __name__ == "__main__":
    main()
