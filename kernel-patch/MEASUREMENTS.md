# What the emulation actually does, measured

Eight ST1600FM0013 (IBM `IBM-SSGSSVJ1P6`, firmware 6214) locked to 528-byte
sectors, reached through the `wvg-sd-528` emulation on a patched Linux 7.0.
Host: 48 threads, 125 GiB RAM, one SAS3216 controller at P16.12, single-port
backplane, 6 Gb/s per link. Every number below comes from a run on that
machine; nothing here is projected.

## Three bugs that had to be fixed first

The patch as published does not build, and two of the things it does build are
in the wrong place. All three are fixed in [port_universal.py](port_universal.py).

**It does not compile.** `sd_528_restrict_block_ops()` calls
`sd_config_discard()`, which `sd.c` defines several hundred lines further down
and never forward declares. The result is an implicit declaration and then a
static-follows-non-static error. A prototype above the emulation block fixes it.

**The block-op restriction ran too early to matter.** The call sat inside
`sd_read_capacity()`, which runs long before `sd_read_block_provisioning()`,
`sd_config_discard()` and `sd_read_write_same()`. Everything it set was
overwritten by the stock code that followed. Two consequences: `sdkp->lbpu` was
not yet known, so discard was disabled unconditionally, and the WRITE SAME
disable, which exists so that a payload the emulation cannot resize never
reaches the device, silently did nothing. The original patch put its own
`sd_disable_advanced_block_ops()` in the same spot, so its safety measure never
took effect either.

**The queue depth cap ran in the wrong function entirely.** The patch anchors
`sd_528_limit_queue_depth()` on `if (!scsi_device_online(sdp))`, and the first
occurrence of that line in `sd.c` is in `sd_sync_cache()`, not
`sd_revalidate_disk()`. Queue depth was therefore adjusted on cache flushes, and
on an idle disk the function is never reached at all. Measured before the fix:
`device/queue_depth` reported 254 despite `emulate_528_queue_depth=32` on the
command line. After moving both calls to the end of `sd_revalidate_disk()` it
reports 32.

There is also a double free on an out-of-memory path in `init_sd()`: when the
page pool fails to allocate, the context pool is destroyed inline and then again
through the `err_out_528_page_pool` label the code falls into. The compiler was
pointing straight at it with an unused-label warning.

## What the emulation presents

```
device                  3,030,911,576 blocks of 528 bytes
host                    1,551,826,726,912 B = 1.41 TiB
logical/physical        512 / 512
discard_max_bytes       268,435,456
write_zeroes_max_bytes  0
LBPU / LBPRZ            1 / 1
```

The block count is unchanged and only the block size differs, so the mapping is
one to one. The 16 metadata bytes per sector are the whole of the 3 % capacity
difference against a native 512-byte format.

## TRIM works end to end

Random data written at LBA 100000, `blkdiscard` over the same megabyte, read
back: all zeros, which is what `LBPRZ=1` promises. `zpool trim` on the finished
pool also runs and reports progress on all eight members, so UNMAP survives the
emulation under ZFS and not only as a one-off ioctl on a raw device.

## Raw devices

Single disk, then all eight concurrently with one fio job each so the offered
queue depth is 8 x 32 rather than 32.

| profile | one disk | eight disks | ratio |
|---|---|---|---|
| sequential read 1 MiB | 505.8 MB/s | 2,098.5 MB/s | 4.15x |
| sequential write 1 MiB | 468.6 MB/s | 2,025.8 MB/s | 4.32x |
| random read 4 KiB | 98,763 IOPS | 725,420 IOPS | 7.34x |
| random write 4 KiB | 104,076 IOPS | 687,300 IOPS | 6.60x |

Random 4 KiB scales almost linearly, sequential 1 MiB only halfway. That is the
signature of the global bounce pool, and the arithmetic agrees: a 1 MiB request
takes 17 of the 576 chunks, so 33 fit at once while eight disks offer 256. A
4 KiB request takes one chunk, so 576 slots cover 256 requests with room over.

Latency says the same thing. Sequential went from 63 ms on one disk to 122 ms on
eight, which is the requeue delay. Random stayed at 643 to 706 us.

Worth noting that the sequential aggregate is *lower* than the random one,
2.1 against 2.8 GB/s. If cabling or the controller were the limit, large blocks
could not be slower than small ones. That rules out the transport.

## Request size against pool size

`emulate_528_max_sectors` is settable at runtime, so this sweep needs no
rebuild, only a rescan. Eight disks, aggregate figures.

| max_sectors | request | 576 chunks read / write | 4608 chunks read / write |
|---|---|---|---|
| 2048 | 1 MiB | 2,579 / 1,988 MB/s | 3,542 / 2,963 MB/s |
| 512 | 256 KiB | 3,078 / 2,732 MB/s | 3,862 / 3,443 MB/s |
| 256 | 128 KiB | 3,953 / 3,385 MB/s | 4,065 / 3,522 MB/s |
| 128 | 64 KiB | 4,095 / 3,391 MB/s | 4,095 / 3,511 MB/s |

Random 4 KiB stayed within one percent of 780,000 IOPS across every row of both
columns, exactly as the chunk arithmetic predicts.

Two ways out of the bottleneck, then. Shrinking requests works but forbids ZFS
from aggregating above that size. Enlarging the reserve keeps large requests and
recovers most of the loss, 37 % on read and 49 % on write at 1 MiB, but not all
of it: 3,542 against 4,095 MB/s. Something else still costs on large requests,
most likely the copy across a seventeen entry scatter-gather list.

The best combination is neither extreme. **128 KiB requests with the large pool
give 4,065 / 3,522 MB/s**, within noise of the 64 KiB result, and 128 KiB is the
ZFS default `recordsize`, so a record maps onto exactly one device request.
4,095 MB/s across eight disks is 512 MB/s each, which is what a single disk does
on its own: the ceiling has moved out of the emulation and into the drives.

Deeper queues do not help. At 128 KiB requests, queue depth 32 gave 4,072 MB/s
sequential read, 64 gave 3,931 and 128 gave 3,798, while random stayed flat. 32
stays.

## Settings this arrives at

```
sd_mod.emulate_512_from_fat_sectors=1
sd_mod.emulate_528_queue_depth=32
sd_mod.emulate_528_max_sectors=256
sd_mod.emulate_528_pool_chunks=4608
sd_mod.emulate_528_pool_contexts=512
```

The last two are new. `SD_528_MEMPOOL_SIZE` and `SD_528_CTX_POOL_SIZE` were
compile-time constants used only as counts, never as array bounds, so nothing
stopped them being parameters. 4608 chunks is 288 MiB of the machine's 125 GiB
and covers 271 concurrent 1 MiB requests against the 256 that eight disks offer.
They are read once at init and clamped to 1..65536, because zero leaves the pool
empty and an absurd value fails the allocation, which means no `sd` driver and
no root filesystem.

## Through ZFS

Pool is four two-way mirrors striped, `ashift=12`, `lz4`, `atime=off`,
`xattr=sa`, `autotrim=on`. 5.62 T raw, 5.50 T usable.

| profile | dataset | threads | result | latency |
|---|---|---|---|---|
| sequential read 1 MiB | `recordsize=128K` | 32 | 4,129 MB/s | |
| sequential write 1 MiB | `recordsize=128K` | 32 | 1,231 MB/s | |
| random read 4 KiB | `recordsize=16K` | 32 | 276,065 IOPS | 14.2 ms |
| random write 4 KiB | `recordsize=16K` | 32 | 98,181 IOPS | 20.5 ms |
| random read 16 KiB | `recordsize=16K` | 32 | 189,347 IOPS | 13.9 ms |
| random write 16 KiB | `recordsize=16K` | 32 | 51,256 IOPS | 39.4 ms |

Sequential read through ZFS beats the raw eight-disk figure, 4,129 against
4,095 MB/s, because a mirror can read from either half and the pool therefore
has more spindles available for reads than a plain stripe.

Sequential write looks like a third of raw until you account for the layout: a
mirror writes every block twice, so the pool's write ceiling is half of
3,522 MB/s, about 1,760. 1,231 MB/s is 70 % of that, which is ordinary ZFS
overhead for checksums, metadata and transaction groups.

### recordsize is the whole story for random IO

Measured on separate datasets, `primarycache=metadata` so nothing is served from
ARC, 8 threads:

| recordsize | random read 4K | random write 4K | read 16K | sequential write |
|---|---|---|---|---|
| 4K | 52,289 IOPS | 73,648 IOPS | 29,778 | 823 MB/s |
| 16K | 33,646 | 60,507 | 34,569 | 952 MB/s |
| 64K | 17,912 | 34,568 | 18,026 | 1,114 MB/s |
| 128K | 11,787 | 23,655 | 11,720 | 1,107 MB/s |

A 4 KiB read from a 128 KiB record makes ZFS read the whole record. At 11,787
IOPS that is 1,500 MB/s of real device traffic, of which the application asked
for one part in thirty-two. Nothing is broken; it is read amplification, and the
only cure is a record size that matches the access pattern.

### The first ZFS numbers were wrong because the test was

Eight threads is not enough load for a four-vdev pool with a deep ZIO pipeline.
Scaling the same random 4 KiB read on a `recordsize=4K` dataset, with CPU
sampled during each run:

| threads | IOPS | throughput | latency | CPU |
|---|---|---|---|---|
| 8 | 52,646 | 205.6 MB/s | 9.6 ms | 12.7 % |
| 16 | 106,098 | 414.4 MB/s | 9.5 ms | 8.3 % |
| 32 | 144,897 | 566.0 MB/s | 13.9 ms | 10.2 % |
| 64 | 190,841 | 745.5 MB/s | 21.1 ms | 10.4 % |
| 96 | 240,321 | 938.7 MB/s | 25.1 ms | 12.3 % |
| 128 | 258,408 | 1,009.3 MB/s | 31.1 ms | 2.0 % |
| 192 | **292,558** | 1,142.7 MB/s | 41.0 ms | 8.5 % |
| 256 | 280,887 | 1,097.1 MB/s | 56.8 ms | 1.9 % |

The processor never goes above 13 %. The pool is queue limited, not compute
limited, and the curve has a knee at 192 threads. Peak random 4 KiB read is
292,558 IOPS, but at 41 ms that is a benchmark number rather than an operating
point. Pick the concurrency from the latency the workload tolerates.

## Integrity

`fio` wrote 8 GiB across 8 jobs with `verify=crc32c` and read it back:
**no verification errors**. A following `zpool scrub` repaired 0 B with 0
errors. If the emulation misplaced or reordered a sector while packing 528 into
512, both of these would fail, and the ZFS checksums would turn a silent
corruption into a loud one. That is a good reason to run ZFS on this stack.

## Survives a reboot

After a cold restart the boot parameters take effect, the emulation comes up
before ZFS, all eight disks enumerate and the pool imports itself from
`zpool.cache` with the datasets mounted. Nothing needs a hand.

## What this means for Proxmox

Active feature flags on the pool are `empty_bpobj`, `lz4_compress`,
`spacemap_histogram`, `enabled_txg`, `hole_birth`, `extensible_dataset`,
`embedded_data`, `large_dnode`, `userobj_accounting`, `project_quota`,
`spacemap_v2`, `log_spacemap`, `zilsaxattr`, `head_errlog` and `vdev_zaps_v2`.
The last three arrived in OpenZFS 2.2, so the importing side needs 2.2 or newer.
Check `zfs version` there before moving the pool; if it is older, recreate with
`-o compatibility=` pinned to what it supports rather than finding out at import
time.

The kernel patch has to come along. Proxmox ships its own kernel, so the
emulation must be rebuilt there, and `port_universal.py` detects the
queue-limits generation the same way it did for 7.0. Without it every member
disk reports zero bytes and the pool cannot be imported at all. That is worth
writing on the case.
