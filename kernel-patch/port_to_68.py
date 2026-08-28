#!/usr/bin/env python3
"""Doladit patch wvg-sd-528 na API kernelu 6.8.

Patch pocita se stromem, kde se limity fronty predavaji jako
`struct queue_limits *lim`. Ve 6.8 zadne takove API neni - limity se nastavuji
bud primo do `q->limits`, nebo pres `blk_queue_*` helpery.

Skript opravi dve mista:

  1. `sd_disable_advanced_block_ops()` bere `lim` a saha na `lim->max_*`.
     Prepiseme signaturu na `struct scsi_disk *sdkp` a telo na 6.8 helpery.
  2. Hunk 10 (strop `max_dev_sectors` pri aktivni emulaci) doplnime za
     `q->limits.max_dev_sectors = ...` v `sd_revalidate_disk()`.
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

    # --- 1) prepis funkce na 6.8 API ------------------------------------
    if OLD_FN in text:
        text = text.replace(OLD_FN, NEW_FN)
        changed.append("sd_disable_advanced_block_ops -> 6.8 API")
    elif "static void sd_disable_advanced_block_ops(struct scsi_disk *sdkp)" in text:
        changed.append("sd_disable_advanced_block_ops jiz prepsana")
    else:
        print("VAROVANI: funkce sd_disable_advanced_block_ops nenalezena v ocekavanem tvaru")

    # volajici mista: odstranit druhy argument
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
        changed.append("upraveno %d volani (odstranen argument lim)" % fixed_calls)

    # --- 2) hunk 10 ------------------------------------------------------
    if "emu_cap" in text:
        changed.append("hunk 10 jiz aplikovan")
    else:
        idx = text.find(ANCHOR10)
        if idx < 0:
            print("CHYBA: kotva pro hunk 10 nenalezena")
        else:
            end = idx + len(ANCHOR10)
            text = text[:end] + "\n" + HUNK10.strip("\n") + text[end:]
            changed.append("hunk 10 vlozen za q->limits.max_dev_sectors")

    sd_c.write_text(text)
    for c in changed:
        print("  %s" % c)

    leftovers = sorted(set(re.findall(r"lim->[a-z_]+", text)))
    print("\n  zbyle reference na lim->: %s" % (leftovers if leftovers else "zadne"))
    print("  emu_cap v souboru: %dx" % text.count("emu_cap"))
    print("  radku celkem: %d" % text.count("\n"))


if __name__ == "__main__":
    main()
