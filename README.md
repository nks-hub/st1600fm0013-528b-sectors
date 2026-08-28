# IBM ST1600FM0013 — firmware, dumps and what is known about the lock

An archive of the attempt to unlock IBM-branded Seagate SAS SSDs that are hard
locked to 528-byte sectors and a 6 Gb/s link.

*Czech version: [README_CZ.md](README_CZ.md)*

**Status: 512 B SOLVED — two ways.**

1. **Reformat.** Disks that are not Seagate-locked accept
   `sg_format --size=512` outright: no capacity loss, stock kernel, full native
   speed. Verified on an HGST unit — see
   [`reformat-528-512.md`](reformat-528-512.md). **Try this first**; a
   non-destructive probe tells you in a fraction of a second whether it will
   work.
2. **Kernel patch.** For disks that refuse the reformat. The disk then appears as
   a native 512-byte block device at the cost of 16 bytes per sector. Verified in
   QEMU including cross-validation — see
   [`kernel-patch/RESULTS.md`](kernel-patch/RESULTS.md).

**12 Gb/s remains unsolved** — that limit lives in the disk firmware. The only
known route is PC-3000 SAS with its "unlock microprogram" function, which can
load another vendor's firmware.

---

## Our disks

| | |
|---|---|
| Model | ST1600FM0013 (Seagate Nytro 1200.2, codename **Koho**) |
| Reports itself as | `IBM-SSG` / `IBM-SSGSSVJ1P6` / firmware `6214` |
| IBM FRU | 02AM752, EC M09099 |
| Part number | `1NT2L2-039` (base `1NT2L2` = Seagate SED Mainstream Endurance) |
| Capacity | 1.6 TB, 3,030,911,576 × 528 B |
| Manufactured | October–November 2018 |
| Units | 8, all healthy (SMART OK, 0 bad sectors, 0–5 % wear) |
| Server | HPE CL2100 Gen10, Broadcom SAS3216 HBA in IT mode |

---

## Directory contents

```
dumps/
  ibm_ST1600FM0013_6214.bin       4 MB   SPI flash dump from an IBM disk (OUR MODEL)
  seagate_ST200FM0133_0007.bin    4 MB   dump from a genuine Seagate Koho disk, fw 0007
  seagate_ST200FM0133_000A.bin    4 MB   the same after upgrading to fw 000A

official-lod/
  12002SSD-Koho-SAS-0004.zip             official Seagate package

reformat-528-512.md                      what works and what does not when reformatting to 512 B
linkrate-6g-analysis.md                  why the disks run at 6 Gb/s, and what a different backplane changes
article-brief.md                         brief for a blog series based on this work

hba/                                     SAS3216 "9305-16i" controller: upgrade to P16.12
  backup_fw_15.00.00.00.bin              backup of the card's original firmware (irreplaceable)
  backup_bios_08.35.00.00.rom            option ROM backup
  my_clone_P16.bin                       the flashed P16.12 image for the SAS3216 clone
  (sas3flash, sas3ircu and backup_mpb.bin are deliberately NOT in the repo)
  README.md                              card identification, procedure, recovery
  firmware/KohoSSD-SED-0004.LOD          SED variant — names ST1600FM0013
  firmware/KohoSSD-STD-0004.LOD          standard (base)
  firmware/KohoSSD-FIPS-0004.LOD         FIPS 140-2
  linux cli tools/seaflashlin/           official Seagate flasher for Linux
  READMEFIRST-...pdf                     instructions + list of supported models

tools/
  hdd_firmware_tools/                    LOD file parser, branch ibm-wip
  patch_ibm_lock.py                      lock patcher (7 bytes + checksum)
  diff_config_records.py                 TLV record analysis, IBM vs Seagate
  classify_regions.py                    flash region classification
  map_lod_to_flash.py                    mapping LOD onto a dump
  dump_config_block.py                   configuration block listing
  sector528_shim.py                      nbdkit plugin 528 -> 512 (userspace)

kernel-patch/
  wvg-sd-528.patch                       third-party sd driver patch, PROVENANCE UNKNOWN
  rebase_pve_528_patch.py                its rebase tool
  apply_manual_hunks.py                  my attempt to apply the rejected hunks
  ORIGIN.md                              what we know about the patch
  VERIFICATION.md                        why it could not be used at first
  RESULTS.md                             test results
```

Every document has a Czech counterpart with a `_CZ` suffix.

### Checksums

```
ibm_ST1600FM0013_6214.bin
  sha256 f674540cf92f5ef5b6a1e042cdc1ed95926d05ae212c02910e50855e19a85f86
  md5    0eaece0f023f226c6b7b4796c50a5ce4

seagate_ST200FM0133_0007.bin
  sha256 86cab26df95e0f7cbb51f97f9b5f094ec678859078faa8642828fa9f55dba7a9
  md5    894f33a21acd7bfad5f165ee034289cf

seagate_ST200FM0133_000A.bin
  sha256 e160963cb8945c7631831328262327ac560fd03a2c2fc8a4c5bad7f2d7cff2e1
  md5    44e5c02cc389e571c733c8554991ac92

12002SSD-Koho-SAS-0004.zip
  sha256 adc2abf82352ccf4a63ec4c07ade161bcc18cabf55ce8288b699834e8ea64472
```

---

## Where it comes from

**The SPI flash dumps** come from the thread
[STH 4968, page 28](https://forums.servethehome.com/index.php?threads/how-to-reformat-hdd-ssd-to-512b-sector-size.4968/page-28):

- `ibm_ST1600FM0013_6214.bin` — user **Arslan109** (25 Sep 2025). Same model as
  ours. They disassembled the disk, desoldered the SPI chip with a hot air gun
  and had it read out with a programmer. Original name `ST1600FM0013.BIN`.
  [Google Drive](https://drive.google.com/file/d/1B8Z8wUzLMtlhvr3H_b23Bqouj3ITkssJ/view)

- `seagate_ST200FM0133_*.bin` — user **leromarinvit** (15 Nov 2025). They bought
  the cheapest Koho disk with genuine Seagate firmware (ST200FM0133, 200 GB),
  dumped it at version 0007, upgraded to 000A and dumped it again. Both dumps
  were verified by reading twice.
  [0007](https://drive.google.com/file/d/1-AObmSgD2IyKm1C-16PRllbjSrCIhljv/view) ·
  [000A](https://drive.google.com/file/d/1u4K-T84_KGIM-bj80xM7GcKl1OGgVWK9/view)

**The official LOD package** from
[touslesdrivers.com](https://www.touslesdrivers.com/index.php?v_page=23&v_code=48362),
classified as "Official". Seagate released it on 18 Feb 2016 and no longer
distributes it publicly.

**The parser** — [eurecom-s3/hdd_firmware_tools](https://github.com/eurecom-s3/hdd_firmware_tools),
here in [leromarinvit's fork, branch `ibm-wip`](https://github.com/leromarinvit/hdd_firmware_tools/tree/ibm-wip).
Latest commit: *"WIP: add artifact types found in Koho LOD"*.

---

## What the dumps contain

All three are **exactly 4,194,304 B**, the full capacity of a W25Q32 chip
(32 Mbit). Only about 10 % is used — the rest is `0x00` and `0xFF` padding, with
entropy around 1.3.

| Dump | ST model inside | IBM strings | Seagate mentions |
|---|---|---|---|
| IBM 6214 | `ST1600FM0013` | `IBM-SSG`, `IBM-SSGSS`, `ZAL`, `SSVJ` | 0 |
| Seagate 0007 | `ST200FM0133` | none | 2 |
| Seagate 000A | `ST200FM0133` | none | 2 |

**The first 32 bytes are identical across all three**
(`f3 00 00 00 30 00 00 00 00 40 01 00 …`), so the format is the same.

The degree of similarity is the interesting part:

```
IBM 6214     vs Seagate 000A   ->  89.7 % identical bytes
Seagate 0007 vs Seagate 000A   ->  80.0 % identical bytes
```

The IBM firmware is therefore **more similar to a Seagate version than two
Seagate firmware versions are to each other**. That suggests IBM 6214 derives
from a branch close to 000A, with the differences concentrated in configuration
rather than in the core.

---

## What the lock does

Firmware `6214` blocks three things with **a single vendor-specific code**,
`ASC 0x26 / ASCQ 0x99`:

1. **Sector size.** MODE SELECT accepts exactly one value:
   ```
   512  -> Illegal Request        520  -> Illegal Request
   524  -> Illegal Request        528  -> Good          <- the only one accepted
   4096 -> Illegal Request
   ```
   This holds for the LONGLBA variant (24-byte parameter list) too, and for
   FORMAT UNIT via openSeaChest. A plain `FORMAT UNIT` to the current size does
   go through, so it is exclusively the *change* that is blocked.

2. **Link rate.** All 8 disks report `desc[33] = 0xaa` (both programmed and
   hardware max 6 Gb/s), even at factory defaults — despite the HBA offering
   12 Gb/s and both the label and the Seagate manual stating 12 Gb/s. An attempt
   to overwrite it with `0xba` ends with the same `ASC 0x26 / ASCQ 0x99`.

3. **Firmware replacement.** Crossflashing with the official `seaflashlin` and a
   genuine signed `KohoSSD-SED-0004.LOD` ends after 22 segments with
   `sense_key=0x05`. Interestingly, the STD image is rejected *immediately* while
   the SED one gets further — so the disk did recognise the file type and only
   refused it at the customer status check.

The Seagate manual (100773817 Rev. D, section 6.7 "Authenticated firmware
download") lists three conditions an image must meet. The third one is the end of
the road:

> the download file must pass the acceptance criteria for the drive. For example it
> must be applicable to the correct drive model, and have compatible revision and
> **customer status**.

---

## What was tried and does not work

All verified on our own disk; the disk is undamaged after every attempt.

| Route | Result |
|---|---|
| `sg_format --size=512` (also `--six`) | Invalid field in parameter list, byte 13 bit 7 |
| `sg_raw` MODE SELECT, short LBA, 3 num_blocks variants | the same |
| `sg_raw` MODE SELECT, **LONGLBA** (24 B param list) | the same — 528 passes, 512 does not |
| `openSeaChest --setSectorSize 512` | "not supported on this device" |
| `openSeaChest --formatUnit` 512 / 520 / 524 / 4096 | "Format Unit Failed!" |
| `seaflashlin -f SED-0004.LOD` (also `-u`, `-w`) | sense 0x05 after 22 segments |
| `sg_write_buffer` mode 5 and mode 7 | ASC 0x26 / ASCQ 0x99 |
| TCG PSID revert (hand-rolled stack over `sg_raw`) | session layer does not respond — see below |
| `sedutil-cli` | `Invalid or unsupported disk` (SATA/NVMe only, not SAS) |

### On TCG and PSID

Level 0 Discovery **works**, but you need the right CDB — the allocation length
is given in blocks, not bytes:

```
sg_raw -r 512 -o out.bin /dev/sgN a2 01 00 01 80 00 00 00 00 01 00 00
                                              ^^ INC_512=1      ^^ 1 block
```

It returns: Opal SSC v1.00, Base ComID `0x07FE`, Locking
`Supported=1 Enabled=1 Locked=0`, MediaEncryption=1, block size 528.

An actual TCG session does not work, though. `SECURITY PROTOCOL OUT` returns
`Good`, `SECURITY PROTOCOL IN` always an empty payload. The decisive test: I sent
512 B of pure nonsense (`0xdeadbeef` repeated) to the session ComID and the disk
answered `Good` — an invalid packet has to end in an error, so the disk
**accepts and discards** packets.

It makes no difference to the outcome anyway: user *sick1655* on STH tested a
PSID revert on a disk where sedutil works normally for them, and reports that
*"sg_format after a fresh PSID reset ends with the same Invalid field in
parameter list"*. **A PSID revert does not unlock the sector size.**

The PSID of these disks is printed on the label, in two rows of 16 characters. It
is a security credential for a factory reset of a SED drive — do not copy it
anywhere.

---

## Where exactly the lock sits (own analysis, 28 Aug 2026)

The community on STH did not have this — leromarinvit was looking for the LOD to
flash mapping and did not get this far. Scripts to reproduce it are in `tools/`.

### The LOD → flash mapping checks out

The parser `seagate_fw_extract.py` on `KohoSSD-SED-0004.LOD` prints the
structure:

```
Artifact 3  type 0x22   0x9f000 B   Flash address = 0xfa0   <- main code
Artifact 5  type 0x9026 0xd0028 B   Flash address = 0x30
Artifact 9  type 0x1a   0x180 B     <- signature (384 B)
```

The contents of Artifact 3 were indeed found in the dumps:

```
seagate_000A -> offset 0xe0fcc
ibm_6214     -> offset 0xe0fcc      <- SAME layout
seagate_0007 -> offset 0x10fcc      (older version, different layout)
```

### The disk configuration is readable data

At `0x0e1140` sits the content of the INQUIRY response, simply stored as text:

```
IBM      9f 00 10 02  "IBM-SSG IBM-SSGSSVJ1P6  6214"  "ZAL15M5Q  216214"
Seagate  8b 01 10 02  "SEAGATE ST200FM0133     000A"  "ZAJ15QQ0"
```

Same structure, different content.

### The heart of the lock: 688 bytes Seagate does not have at all

Regions where IBM has data and Seagate has blank flash (`0xFF`):

```
0x0e1590 – 0x0e1820   656 B   <- main block
0x0e1c70 – 0x0e1c80    16 B
0x0e1f40 – 0x0e1f50    16 B
                      688 B total
```

They are named configuration records of the form
`[id:1][len:2][00 00][id:1][len-4:2][namelen:1][name][data]`. IBM added four:

| Offset | ID | Length | Name |
|---|---|---|---|
| 0x0e15b8 | 0xc4 | 0x28 | (spaces) |
| 0x0e15e4 | 0xc7 | 0xa0 | `SCDD` |
| **0x0e1688** | **0xc8** | **0xd8** | **`AIX      `** ← the lock |
| 0x0e1760 | 0xc9 | 0xac | `AIX      ` |

Record `0xc8` carries the block descriptor as plain data:

```
0e1690  09 41 49 58 20 20 20 20 20 20 00 00 00 08 b4 a8
0e16a0  0a 58 00 00 02 10 …
        "AIX      "         b4a80a58      000210
                            3030911576    528
```

### There is a record index too

```
IBM      … c0 c1 c3 [c4 c7 c8 c9] d1 d2 00     21 entries
Seagate  … c0 c1 c3               d1 d2 00     17 entries
```

IBM has exactly those four extra IDs in its catalogue.

### Configuration block header

```
0x0e1130   79 71 6e 49 | f0 06 | 79 9a | ff 00 a4 00 00 00 06 32    IBM
0x0e1130   79 71 6e 49 | 60 04 | ab 93 | ff 00 90 00 00 00 06 12    Seagate
           magic "yqnI"  length  ?
                         1776 B
                         1120 B
```

The length difference is **656 bytes — exactly the size of the main IBM-only
block**. That confirms the field `f0 06` is the length of the configuration area.

### Checksum — cracked

The field `79 9a` / `ab 93` is a checksum. The algorithm comes from an analysis
of the LOD format on
[hddguru](https://forum.hddguru.com/viewtopic.php?f=13&t=28252), which contains a
REXX function `GETSUMM`:

> Sum 16-bit **little-endian** words across the whole block **including the
> checksum field**. The result must be zero.

Verified against our data; it holds everywhere:

```
IBM config block  @0x0e1130, length 0x06f0  ->  sum 0x0000  OK
SGA config block  @0x0e1130, length 0x0460  ->  sum 0x0000  OK
all LOD headers                             ->  sum 0x0000  OK
```

### The patch: seven bytes are enough

`tools/patch_ibm_lock.py` finds the magic `yqnI`, rewrites the block descriptor
in the AIX record and recomputes the checksum:

```
python tools/patch_ibm_lock.py your_dump.bin output.bin --mode blocksize --blocks 3125627568
```

Seven bytes change:

```
0x0e1136-0x0e1137   checksum      0x9a79 -> 0xad33
0x0e169e-0x0e16a1   block count   b4a80a58 -> ba47d3b0   (3,125,627,568)
0x0e16a5            sector size   0x10 -> 0x00           (528 -> 512)
```

Capacity stays at 1600.32 GB, exactly per the Seagate table. Demonstration
outputs are in `patched/` — generated from a third-party dump, they serve only to
verify the tool.

### The dump has to come from your own disk

The configuration block contains **the serial number and calibration of that
specific unit**. The bundled `ibm_ST1600FM0013_6214.bin` belongs to user
Arslan109 (SN `ZAL15M5Q`). Writing it to our disk would mean overwriting its
identity with someone else's.

The correct procedure:

1. desolder the `W25Q32FWZEIG` from your own disk
2. read the dump with a programmer (CH341A)
3. run `patch_ibm_lock.py` on **your own** dump
4. write it back

### What follows from this

**The lock is not in the firmware code but in a data configuration record**, and
we can recompute the checksum for it. One unknown remains: whether the firmware
validates the configuration block by anything beyond the checksum. That can only
be established by writing.

---

## The open route: reprogramming the SPI flash

The only thing the community has not closed out.

**The chip:** `W25Q32FWZEIG`, WSON-8 package 8×6 mm, on the PCB near the SAS
connector. According to Arslan it comes off with a hot air gun in thirty seconds.
Both participants advise against SOIC clips — better to take the chip off and use
a socket adapter. A CH341A-class programmer is enough.

**What is missing:** understanding the LOD format well enough to build a flash
image from the official `KohoSSD-SED-0004.LOD`. leromarinvit is working on that
in the `ibm-wip` branch, but does not yet have the LOD to physical layout
mapping.

**A shortcut nobody has tried:** writing the Seagate dump from a `ST200FM0133`
straight onto an IBM disk. leromarinvit suggests it themselves: *"with some luck,
blindly flashing the Seagate dump to the IBM-branded drive might just work"*. The
catch is the different capacity — 200 GB vs 1.6 TB — so the NAND configuration
almost certainly does not match. A more sensible approach is to first compare
which regions differ between the IBM and Seagate dumps (10.3 %) and transfer only
the configuration parts.

The data for that is complete in this directory: we have the official LOD, a dump
from an IBM disk of our model, and dumps from a genuine Seagate disk in two
versions. That is exactly the combination leromarinvit was missing.

---

## Alternatives, if the hardware route never happens

1. **Deploy the disks where 528 B is native** — IBM Storwize, FlashSystem,
   DS8880. Full capacity, full performance, zero work.
2. **A shim over NBD/iSCSI** mapping 512B logical blocks onto 528B physical ones.
   It works in principle, but it is permanent overhead and non-standard
   operation.
3. **Sell them.** Eight healthy 1.6 TB SAS SSDs are worth full price to owners of
   IBM arrays, because there 528 B is a desirable property, not a defect.

For Proxmox, note that LVM, ext4, XFS and ZFS will not see a 528-byte disk — the
standard block layer does not work with it.

---

## Capacity after a possible conversion

The good news: **nothing would be lost.** Per the Seagate manual, table 3 for the
1600 GB model:

```
today: 3,030,911,576 × 528 B = 1,600,321,312,128 B
512 B: 3,125,627,568 × 512 B = 1,600,321,314,816 B
                              ────────────────────
difference:        +2,688 B per disk  (0.0000 %)
```

IBM keeps the full nominal 1.6 TB even with 528-byte sectors — the 16 metadata
bytes per sector come out of internal reserve, not user capacity.

---

## Links

- [STH 4968 — How to reformat HDD & SSD to 512B Sector Size](https://forums.servethehome.com/index.php?threads/how-to-reformat-hdd-ssd-to-512b-sector-size.4968/) (29 pages)
- [STH 26945 — Changing block size IBM branded Micron S650DC-800 SSD](https://forums.servethehome.com/index.php?threads/changing-block-size-ibm-branded-micron-s650dc-800-ssd.26945/) (91 posts, 2019–2025)
- [hddguru — analysis of the LOD format](https://forum.hddguru.com/viewtopic.php?f=13&t=28252)
- [Seagate 1200.2 SAS SSD Product Manual 100773817 Rev. D](https://www.seagate.com/content/dam/seagate/migrated-assets/www-content/product-content/ssd-fam/1200-ssd/en-us/docs/1200-2-sas-ssd-product-manual-100773817d.pdf)
- [Mattiwatti/sedutil](https://github.com/Mattiwatti/sedutil) — fork with SHA512, works on NetApp

### Which brands can be reformatted

| Works | Does not work |
|---|---|
| NetApp, EMC, Dell, Toshiba, HPE, Huawei, Micron (non-IBM) | **IBM branded** — Seagate and Micron alike, SED especially |

It failed even on genuine IBM Power8/Power9 with `iprconfig` and under AIX. IBM
documentation additionally states "JBOD is not supported on SSDs".

---

*Compiled 28 Aug 2026. Measured on a live disk, not taken from forums — with the
exception of the dumps themselves and quoted third-party experience, which are
attributed by author name.*

---

# Procedure: reprogramming the SPI flash

Written down in case a programmer turns up. The software route is exhausted (see
above); this is the only verified possibility.

## What you need

| Item | Note | Approx. price |
|---|---|---|
| CH341A SPI programmer | USB, the **3.3 V** version (black board; the green one supplies 5 V and destroys the chip) | €6–10 |
| WSON-8 / DFN-8 → DIP adapter | for `W25Q32FWZEIG`, 8×6 mm package | €4–8 |
| Hot air gun | for desoldering the chip | — |
| Flux, solder, tweezers | — | — |

A SOIC clip is **not recommended** — both people who did this on the forum agreed
that the dump does not come out reliably with one and they had to remove the chip
anyway.

## Procedure

**1. Identify the disk**

Before taking anything apart, note the serial number. You can make the disk in a
given slot blink like this:

```bash
timeout 2 sg_dd if=/dev/sgN bs=528 count=200000 of=/dev/null; sleep 1
```

The read has to go through `/dev/sgN`, not `/dev/sdX` — the block device has zero
size, so no I/O flows through it.

**2. Disassembly and desoldering**

The `W25Q32FWZEIG` chip is on the PCB near the SAS connector, WSON-8 package
8×6 mm. A hot air gun takes it off in a few tens of seconds. Note the orientation
(dot = pin 1).

**3. Dump — twice, immediately**

```bash
flashrom -p ch341a_spi -r dump1.bin
flashrom -p ch341a_spi -r dump2.bin
sha256sum dump1.bin dump2.bin      # MUST match
```

If the hashes differ, the contact is bad. Do not fix it in software, repeat the
read.

**4. Verify the dump makes sense**

```bash
python tools/patch_ibm_lock.py dump1.bin /dev/null 2>&1 | head -5
```

It must print `sum=0x0000 OK` and an INQUIRY with **your** serial number. If the
sum does not check out, the dump is corrupt.

**5. Patch**

```bash
python tools/patch_ibm_lock.py dump1.bin patched.bin \
       --mode blocksize --blocks 3125627568
```

Seven bytes change and the checksum is recomputed. The tool verifies for itself
that the sum came out zero and prints which offsets it changed.

**6. Write and verify**

```bash
flashrom -p ch341a_spi -w patched.bin -V
flashrom -p ch341a_spi -r verify.bin
sha256sum patched.bin verify.bin   # MUST match
```

**7. Back in and test**

Solder it back, plug it in, and check:

```bash
sg_readcap -l /dev/sgN      # expect 512 B and 3,125,627,568 blocks
sg_inq /dev/sgN             # INQUIRY should stay IBM-SSG / 6214
```

## What can go wrong

- **The firmware validates the configuration block with a signature as well.** We
  can do the checksum; a signature would be a problem. There is no way to find
  out other than by writing — but you have the original dump, so it can be
  reverted.
- **A bad dump due to poor contact.** That is why you read twice and compare.
- **Overheating the chip.** Hot air gun at a sensible temperature, not full
  blast.
- **Writing someone else's dump.** The configuration block carries the serial
  number and calibration of a specific unit. Never use
  `dumps/ibm_ST1600FM0013_6214.bin` — it belongs to a different disk
  (SN `ZAL15M5Q`).

Keep the original dump. As long as you have it, the whole operation is
reversible.
