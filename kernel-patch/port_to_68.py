#!/usr/bin/env python3
"""Adapt the wvg-sd-528 patch to the kernel 6.8 API.

The patch assumes a tree where queue limits are passed around as
`struct queue_limits *lim`. No such API exists in 6.8 - limits are set either
directly in `q->limits` or through the `blk_queue_*` helpers.

The script fixes two places:

  1. `sd_disable_advanced_block_ops()` takes `lim` and touches `lim->max_*`.
     Rewrite the signature to `struct scsi_disk *sdkp` and the body to 6.8
     helpers.
  2. Hunk 10 (cap on `max_dev_sectors` while emulation is active) is inserted
     after `q->limits.max_dev_sectors = ...` in `sd_revalidate_disk()`.
"""
import re
import sys
from pathlib import Path

OLD_FN = """static void sd_disable_advanced_block_ops(struct scsi_disk *sdkp,
					  struct queue_limits *lim)
{
	sdkp->lbpme = 0;
	sdkp->lbprz = 0;
	sdkp->lbpu = 0;
	sdkp->lbpws = 0;
	sdkp->lbpws10 = 0;
	sdkp->lbpvpd = 0;
	sdkp->ws10 = 0;
	sdkp->ws16 = 0;
	sdkp->max_ws_blocks = 0;
	sdkp->zeroing_mode = SD_ZERO_WRITE;
	sdkp->provisioning_mode = SD_LBP_DISABLE;
	lim->max_hw_discard_sectors = 0;
	lim->max_discard_sectors = 0;
	lim->max_write_zeroes_sectors = 0;
}"""

NEW_FN = """static void sd_disable_advanced_block_ops(struct scsi_disk *sdkp)
{
	struct request_queue *q = sdkp->disk->queue;

	sdkp->lbpme = 0;
	sdkp->lbprz = 0;
	sdkp->lbpu = 0;
	sdkp->lbpws = 0;
	sdkp->lbpws10 = 0;
	sdkp->lbpvpd = 0;
	sdkp->ws10 = 0;
	sdkp->ws16 = 0;
	sdkp->max_ws_blocks = 0;
	sdkp->zeroing_mode = SD_ZERO_WRITE;
	sdkp->provisioning_mode = SD_LBP_DISABLE;
	blk_queue_max_discard_sectors(q, 0);
	blk_queue_max_write_zeroes_sectors(q, 0);
}"""

ANCHOR10 = "\tq->limits.max_dev_sectors = logical_to_sectors(sdp, dev_max);"

HUNK10 = """
	if (sdkp->emulate_512_from_528) {
		unsigned int emu_cap = sd_528_effective_max_sectors();

		emu_cap = min_t(unsigned int, emu_cap,
				sd_528_segments_to_sectors(q->limits.max_segments));
		q->limits.max_dev_sectors = min_t(unsigned int,
						  q->limits.max_dev_sectors,
						  emu_cap);
	}
"""


def main():
    tree = Path(sys.argv[1] if len(sys.argv) > 1 else "/usr/src/k/linux-6.8")
    sd_c = tree / "drivers/scsi/sd.c"
    text = sd_c.read_text()
    changed = []

    # --- 1) rewrite the function for the 6.8 API ------------------------
    if OLD_FN in text:
        text = text.replace(OLD_FN, NEW_FN)
        changed.append("sd_disable_advanced_block_ops -> 6.8 API")
    elif "static void sd_disable_advanced_block_ops(struct scsi_disk *sdkp)" in text:
        changed.append("sd_disable_advanced_block_ops already rewritten")
    else:
        print("WARNING: sd_disable_advanced_block_ops not found in the expected form")

    # call sites: drop the second argument
    calls = re.findall(r"sd_disable_advanced_block_ops\(([^)]*)\)", text)
    fixed_calls = 0
    def fix_call(m):
        nonlocal fixed_calls
        args = m.group(1)
        if "," in args and "struct" not in args:
            fixed_calls += 1
            return "sd_disable_advanced_block_ops(%s)" % args.split(",")[0].strip()
        return m.group(0)
    text = re.sub(r"sd_disable_advanced_block_ops\(([^)]*)\)", fix_call, text)
    if fixed_calls:
        changed.append("adjusted %d call site(s) (removed the lim argument)" % fixed_calls)

    # --- 2) hunk 10 ------------------------------------------------------
    if "emu_cap" in text:
        changed.append("hunk 10 already applied")
    else:
        idx = text.find(ANCHOR10)
        if idx < 0:
            print("ERROR: anchor for hunk 10 not found")
        else:
            end = idx + len(ANCHOR10)
            text = text[:end] + "\n" + HUNK10.strip("\n") + text[end:]
            changed.append("hunk 10 inserted after q->limits.max_dev_sectors")

    sd_c.write_text(text)
    for c in changed:
        print("  %s" % c)

    leftovers = sorted(set(re.findall(r"lim->[a-z_]+", text)))
    print("\n  remaining references to lim->: %s" % (leftovers if leftovers else "none"))
    print("  emu_cap occurrences in file: %dx" % text.count("emu_cap"))
    print("  total lines: %d" % text.count("\n"))


if __name__ == "__main__":
    main()
