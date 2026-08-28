# Kernel patch for 520/528B sectors — what we know about it

Two files delivered on 28 Aug 2026 via Telegram.

## What it is

`wvg-sd-528.patch` — a unified diff against `drivers/scsi/sd.c` and
`drivers/scsi/sd.h`, 605 lines added, 6 removed. It adds emulation to the `sd`
driver: a disk with 520- or 528-byte sectors is presented to the host as a
512-byte one, and the metadata is discarded along the way.

`rebase_pve_528_patch.py` — a tool that ports the patch to other kernel
versions. It applies the hunks that still fit and semantically relocates the
ones that have drifted. The target path inside it is
`patches/kernel/9999-wvg-sd-528-translation.patch`, which is a Proxmox
convention.

## Provenance — unknown

- The patch has **no header at all**: no `Signed-off-by`, no author, no
  copyright, no SPDX.
- The identifiers `emulate_512_from_fat_sectors`, `sd_528_emulation` and
  `wvg-sd-528` **appear nowhere on the internet** — not in the kernel git, not
  on LKML, not on GitHub.
- The script's docstring treats the abbreviation "WVG" as common knowledge, but
  what it stands for could not be established.
- Timestamps in the diff: source 19 Feb 2026, modified version 25 Apr 2026.

So this is not a public patch. Either private work, or generated.

## Technical assessment

The code looks competent:

- Conversions use `DIV_ROUND_UP` rather than bit shifts — a necessity at 528,
  since it is not a power of two.
- Bounce buffers come from a preallocated `mempool` (`SD_528_MEMPOOL_SIZE = 576`
  chunks of 64 kB, `SD_528_CTX_POOL_SIZE = 64` contexts), so nothing is
  allocated in the I/O path.
- Error handling is in place (`goto out_eio`, `return -EIO`), along with caps on
  both request size and queue depth.
- It correctly zeroes `protection_type` — the extra 16 B at 528 are not T10 PI.
- It extends `struct scsi_disk` with `device_sector_size` and two bit flags.
- Three tunable module parameters: enable emulation, queue depth, request size
  cap.

Verified against vanilla 6.8 (`git.kernel.org`, tag v6.8):

```
sd.c   10 of 13 hunks apply, 3 failed (#1, #9, #10)
sd.h    3 of 3 hunks apply
```

The failing hunks are exactly what the bundled rebase script is for.

## Why it could not be deployed at first

Two obstacles, both outside the patch itself:

1. **`CONFIG_BLK_DEV_SD=y`** — `sd` is built into the kernel, not a module. It
   cannot be swapped at runtime; the whole kernel has to be rebuilt.
2. **The test server boots from live media (read-only).** A new kernel would not
   survive a reboot, so rebuilding there is pointless.

To try it out, the system would have to run from disk — there is a free Kingston
SA400 240 GB in the server.

## Before anyone points this at real data

605 lines in the DMA path of the SCSI layer. A mistake in a 528-byte sector
offset does not show up as an error message but as silent data corruption. At
minimum:

- write a known pattern through the emulation, read it back natively via
  `sg_dd`, and compare byte for byte,
- repeat at several offsets, including unaligned ones and across a chunk
  boundary,
- run `fio` with verification and a long `badblocks -w` on a sacrificial disk,
- only then consider real data.
