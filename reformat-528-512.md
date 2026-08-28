# Reformatting 528 → 512 B: what works and what does not

Findings from 28 Aug 2026. Test server, SAS3216 clone controller.

## The lock is not "IBM", it is Seagate-specific

For a long time it looked as though IBM firmware was behind all of it. It is
not. A control experiment settled it: the same non-destructive probe against two
IBM-branded disks from different manufacturers.

```bash
# MODE SELECT(10) header 8 B + block descriptor 8 B, block length 512
printf '\x00\x00\x00\x00\x00\x00\x00\x08\x00\x00\x00\x00\x00\x00\x02\x00' > /tmp/ms512.bin
sg_raw -s 16 -i /tmp/ms512.bin /dev/sdX 55 10 00 00 00 00 00 00 10 00
```

CDB: `55` = MODE SELECT(10), byte 1 = `0x10` → PF=1, **SP=0**, so nothing is
saved and the medium is not touched. Bytes 7–8 = parameter length `0x0010`.

| Disk | Manufacturer | Result |
|---|---|---|
| IBM-SSG **HSPX400** 400 GB | HGST (OUI `5000cca`) | `SCSI Status: Good` |
| IBM-SSG **IBM-SSGSSVJ1P6** 1.6 TB | Seagate | `Illegal Request, Invalid field in parameter list` |

The probe takes a fraction of a second and destroys nothing. **It is the first
thing worth running on such a disk**: it tells you in advance whether you need
to bother with a kernel patch or SPI flash, or whether `sg_format` will do.

## No capacity is lost

The main worry was losing 16 B out of every sector, roughly 3 %. It did not
materialise. After the block length change the disk recalculated its LBA count
from the physical capacity:

```
before: 757,743,288 × 528 B = 400,088,456,064 B
after:  781,422,768 × 512 B = 400,088,457,216 B
```

A difference of 1,152 bytes, i.e. nothing. The same conclusion came out for the
Seagate from its own format table: 1.600 TB either way.

## Fast format exists, but does not save the day

`sg_format` 1.62 supports `--ffmt` (the SBC-4 FAST FORMAT field) and `--dcrt`
(skip media certification). The 2013-vintage HGST disk **accepted FFMT=1**.

Measured rates, not estimates:

| mode | rate | total |
|---|---|---|
| without `--ffmt` | 2.35 %/min | ~42 min |
| `--ffmt=1 --dcrt` | 2.82 %/min | ~35 min |

About 15 % faster, not an order of magnitude. **Not worth aborting a running
format for.**

## Aborting a format is safe

Verified in practice. The `sg_format` process has to be terminated by its
specific PID, looked up beforehand; terminating by process name would take down
everything else sharing that name. Then reset the device:

```bash
sg_reset --device /dev/sdX
sg_reset --target /dev/sdX
```

The disk then reports `Sense key: Medium Error, Additional sense: Medium format
corrupted`. It looks alarming, but it is an expected and recoverable state: just
run FORMAT UNIT again. Interestingly, the block length change to 512 already
showed up in the block descriptor after that aborted format.

## During the format

`sg_turs` returns "device not ready", `sg_readcap` the same, the kernel keeps
`size=0` and `physical_bs=4224`. A rescan changes nothing. Progress can only be
read via:

```bash
sg_requests --progress /dev/sdX
```

Only estimate the remaining time after ~5 minutes of running. A sample from the
first 150 s produced an estimate of 2 hours; the reality was 42 minutes. The
ramp-up is non-linear.

## Result: done and verified

The format finished on 28 Aug 2026 at 13:32 and took 36 minutes. The rate was
linear throughout (20.68 % at 13:03 up to 98.06 % at 13:31), so the estimate
from the steady-state run held.

```
Last LBA = 781422767,  Number of logical blocks = 781422768
Logical block length  = 512 bytes
physical block length = 4096 bytes          (was 4224 = 8 × 528)
Device size           = 400,088,457,216 B = 400.09 GB
```

The kernel finally sees the disk:

```
NAME HCTL       TRAN  VENDOR   MODEL     SIZE
sdb  14:0:11:0  sas   IBM-SSG  HSPX400   372.6G
```

### Integrity tests

| test | result |
|---|---|
| aligned 8 MB write, read back, md5 | match (`065a63acf1c056a47d62b333bfe1e328`) |
| unaligned write, LBA 2049, 17 sectors | OK |
| write at end of disk, LBA 781,420,720 | OK |
| SMART after the tests | OK, endurance still 1 % |

### Speed

```
536,870,912 B written in 1.371 s = 392 MB/s   (oflag=direct)
```

For comparison: the nbdkit shim we rejected on performance grounds managed
118 MB/s. Native access is thus more than three times faster, with no extra
layers at all.

### What this means

This route is the best of the three when the disk passes the probe: **no
capacity loss, stock kernel, full native speed**. The kernel patch is worth it
only for disks that fail the probe.

## Comparing the three routes to 512 B

| route | works on | capacity loss | time | requirements |
|---|---|---|---|---|
| `sg_format --size=512` | disks without the lock (HGST) | none | ~35–42 min | nothing |
| kernel patch (emulation) | anything | 16 B/sector → 1.55 TB of 1.60 | kernel build | non-standard kernel |
| SPI flash rewrite | even locked ones | none | hours | programmer, disassemble the disk |

The order of preference follows from that: probe first, and if it passes,
`sg_format`. The kernel patch only makes sense for disks that fail the probe.
