# HBA: SAS3216 "9305-16i" clone — firmware upgrade to P16.12

Date: 28 Aug 2026. Test server, slot 3, PCI `b3:00.0`.

## What the card actually is

It is sold as a 9305-16i, but it is not a retail Broadcom. A genuine 9305-16i is
built on the **SAS3224** chip; this card has a **SAS3216** with internal SFF-8643
connectors — a combination no official firmware targets.

`sas3flash -c 0 -list` gives the clone away:

```
Board Name          : Avago SAS3216      generic chip name, not "SAS9305-16i"
Board Assembly      : N/A
Board Tracer Number : N/A
```

Empty Assembly and Tracer mean a generic manufacturing region. A retail unit
would carry an assembly number and a serial tracer there.

State before the work:

| | |
|---|---|
| Chip | SAS3216(A1), PCI `1000:00c9`, subsystem `1000:3180` |
| Firmware | 15.00.00.00 |
| NVDATA | 0b.04.00.23 |
| BIOS | 08.35.00.00 |
| SAS address | `500062b-2-…-a780` |

Subsystem `3180` belongs to the 9305-**16e**, because the 16e is also a SAS3216.
This card, however, has internal ports. That contradiction is what defines the
clone.

## Why official firmware would not work

Both P16.12 packages from 45Drives (16i and 24i) carry the string `LSISAS3224`
inside. Our card is a SAS3216 and its own firmware contains `LSISAS3216`.
`sas3flash` refuses such a mismatch:

```
ERROR: NVDATA Image does not match Controller Device ID!
Device ID - NVDATA:0xc4 Controller:0xc9
```

The refusal is safe and does not harm the card. Something else is dangerous:
according to the discussion on the TrueNAS forum, firmware from Supermicro
flashes *without an error*, but the card then does not work at all.

The officially latest version for the SAS3216 is 15.00.00.00 — which is what was
already on the card.

## The solution

[kjake/sas3216-9305-firmware](https://github.com/kjake/sas3216-9305-firmware)
takes stock 9305-16i P16.12 and rewrites the NVDATA in it: the chip identity
becomes SAS3216 while the 16-port internal PHY map from the 16i is kept. It
recalculates the per-record checksums and rebalances the image.

### The verification chain

Nothing was flashed until all four points lined up:

1. **Our backup is byte-for-byte identical to the reference clone backup** in the
   repository (`firmware/original-clone-P15/firmware0.fw`): `cmp -l` → 0 differing
   bytes out of 959,848. Our card therefore *is* precisely the hardware the
   author validated the whole tool against.
2. **A local build from an independently downloaded base** (45Drives) produced an
   image byte-identical to the prebuilt one in the repository. The supplied image
   is therefore not tampered with and the transform is deterministic.
3. **The repository checksums match** the values in its documentation.
4. **`sas3flash` itself confirmed during the write**: `NVDATA Device ID and Chip
   Revision match verified` — exactly the check the stock 3224 image failed.

A small detail fits too: the repository notes that `phys(24)` is cosmetic, that
the firmware inherits 24 PHY slots from the 16i/3224 base NVDATA and only
enumerates the 16 that are wired. That is exactly what the card reports — phys
0–7 and 16–23 active, 8–15 disabled.

## The procedure used

```bash
# tools
curl -LO http://images.45drives.com/tools/sas3ircu
curl -LO http://images.45drives.com/Firmware/LSI9305/sas3flash/linux/sas3flash
chmod +x sas3ircu sas3flash

# backups (stdin from /dev/null, or the utility's pager eats the rest of the script)
./sas3flash -o -c 0 -ufirmware backup_fw_15.00.00.00.bin  < /dev/null
./sas3flash -o -c 0 -ubios     backup_bios_08.35.00.00.rom < /dev/null
./sas3flash -o -c 0 -umpb      backup_mpb.bin              < /dev/null

# build
curl -LO http://images.45drives.com/Firmware/LSI9305/16i/SAS9305_16i_IT_P.bin
git clone https://github.com/kjake/sas3216-9305-firmware.git
python3 sas3216-9305-firmware/build_3216_clone_fw.py \
        --base SAS9305_16i_IT_P.bin --out my_clone_P16.bin

# flash, no option ROM (IT mode, the OS does not boot from the HBA)
./sas3flash -o -c 0 -f my_clone_P16.bin < /dev/null
```

## Result

```
Firmware Version 16.00.12.00
Firmware Image compatible with Controller.
Valid NVDATA Image found.  NVDATA Major Version 10.00
NVDATA Device ID and Chip Revision match verified.
Firmware Flash Successful.   Adapter Successfully Reset.
```

| | before | after |
|---|---|---|
| Firmware | 15.00.00.00 | **16.00.12.00** |
| NVDATA | 0b.04.00.23 | 10.00.00.24 |
| SAS address | `500062b-2-…-a780` | unchanged |
| Board Name | Avago SAS3216 | unchanged |

The controller reset happened while the system was running
(`mpt3sas_base_hard_reset_handler: SUCCESS`) and the disk stayed visible. The
link error counters are zeroed after the reset — that is a new baseline, not
proof of improvement; before the work `phy_reset_problem_count` stood at 487,
which was the result of re-plugging disks.

To apply the new firmware fully, the documentation recommends a **cold start**,
not a warm reboot.

## What the upgrade brought

Broadcom never published a cumulative Phase 15 → Phase 16 changelog. Two things
are available: the release notes for the point release itself
(`docs/Intruder_Release_Notes_16.00.12.00.pdf`) and the firmware images
themselves, which can be compared.

### What is in the release notes for 16.00.12.00

Two fixes, **both SATA only** — so they have no effect on our SAS disks:

| ID | What it addresses |
|---|---|
| DCSG00398894 | WRITE SAME NCQ encapsulation sent a Non-Data NCQ command to a drive that does not support it, even when it supports Zero EXT. Symptom: I/O errors during `mkfs.ext4`. |
| DCSG00411882 | Recursion while completing an ATA pass-through command with a pending I/O carrying an invalid CDB → controller hang. Symptom: creating a ZFS pool on directly attached SSDs. |

The community says Phase 16 addresses "performance issues causing the controller
to reset". Nobody has publicly backed that with before/after measurements.

### What comparing the images showed

A local string diff between the P15 backup and the flashed P16.12:

```
P15  5400 unique strings, image 959,848 B
P16  5467 unique strings, image 998,280 B   (+38 kB, +4 %)
1021 added, 954 removed
```

The changes are not cosmetic. Whole groups appeared:

- **FPE (Fast Path Engine)** — `FPE Control Request Pause/UNPause`, `FPE Dev State
  Table`, `FPE Timeout Error`, `FPE Timeout Missed IOs`, `FPE Start Pend
  Postponed`. New logic around fast-path pausing and timeouts. That fits the
  claim about fixing controller resets.
- **Discovery** — `DISC: SAS/SATA Port Enable Complete` / `not complete yet` with
  the pending state printed, and `DISC: The SMP Discover response indicated that
  devH is no longer there`. Better handling of a device disappearing mid-discovery.
- **Enclosure management** — `EM VppGetCableSwapConnID`, `EM VppI2CDrvPresPoll`,
  `EM VppSetSlotNum`, `EM SesPg0AMap PhyIdx`. Backplane slot mapping and drive
  presence polling.
- **Task management** — `Error: TM request failed with status`, `ERROR: Unable to
  find an outstanding IO for DevHandle`.
- **Firmware download** — `FWDL Failed LogInfo`, `FWDL Status IOCStatus`.

One concrete fix is visible directly in the diff — swapped bit-field widths when
decoding the port error state:

```
P15   Clear PORTERR: Core(2):Link(6):IntStatus(8):PllcState(16)
P16   Clear PORTERR: Core(2):Link(6):IntStatus(16):PllcState(8)
```

A `CSW SPICO ECC error` report was also added (ECC errors in the SerDes
microcontroller), plus a `Width` column in the device listing. Diagnostics got
more precise in general — many format specifiers widened from `%x` to `%08x` /
`%04x`.

For us the link-layer part is the relevant one: we measured a
`phy_reset_problem_count` of 487 and invalid dwords, which is exactly the area
the PORTERR fix and the SPICO ECC reporting touch.

The MPI interface moved from 206.30 to 206.32.

## What this did not solve

The link rate stays at 6 Gb/s. The controller offers 12 Gb/s on all phys
(`maximum_linkrate` and `hw_max` both 12.0 Gbit) — the ceiling is held by the
disk, not the HBA. A controller firmware upgrade could not have changed that,
and it did not.

## Recovery

The backups in this directory are irreplaceable for this particular card — the
server runs as a live boot, so anything in `/root` disappears on restart.

```bash
./sas3flash -o -c 0 -f backup_fw_15.00.00.00.bin < /dev/null
./sas3flash -o -c 0 -b backup_bios_08.35.00.00.rom < /dev/null   # only if you need the option ROM
./sas3flash -o -c 0 -sasadd <your card's SAS address> < /dev/null # only if the address is zeroed
```

`-e 6` erases the firmware but leaves the manufacturing area, so the SAS address
survives. `-e 7` erases that too — then the address has to be restored by hand.

## Directory contents

| File | SHA-256 | What it is |
|---|---|---|
| `backup_fw_15.00.00.00.bin` | `e2fc1ee7…24cfd3a` | **Backup of the card's original firmware.** Identical to the reference clone in the upstream repository. |
| `backup_bios_08.35.00.00.rom` | `28a9e758…1d63d0` | Option ROM backup. |
| `backup_mpb.bin` | `9acf33aa…5ec9af9d` | Manufacturing block (SAS address, board identity). |
| `my_clone_P16.bin` | `2ddb5ee0…8a27314` | The image that was flashed. A local build, byte-identical to the validated one from the repository. |
| `SAS9305_16i_IT_P.bin` | `917d0c11…b316464` | Stock P16.12 base (SAS3224, unusable on its own). |
| `SAS9305_24i_IT_P.bin` | `3ed68273…b74a65b` | Stock 24i, for comparison only. |
| `sas3flash`, `sas3ircu` | | Tools from 45Drives. |
| `docs/*.pdf` | | Documentation from the P16.12 package: release notes, BIOS, UEFI BSD, `sas3flash` quick reference. |

## Sources

- [kjake/sas3216-9305-firmware](https://github.com/kjake/sas3216-9305-firmware)
- [TrueNAS: Help finding updated firmware for Avago SAS3216 9305-16i HBA card](https://forums.truenas.com/t/help-finding-updated-firmware-for-avago-sas3216-9305-16i-hba-card/62254)
- [45Drives KB451408 — Flashing LSI 9305 Controllers Firmware in Ubuntu and Rocky Linux](https://knowledgebase.45drives.com/kb/kb451408/)
