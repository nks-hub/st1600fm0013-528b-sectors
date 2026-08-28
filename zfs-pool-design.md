# ZFS on eight emulated 528B disks: layout, tuning, and how it gets measured

Written 28 Aug 2026, before the pool exists. Predictions are marked as such;
measured numbers replace them once the benchmarks run.

## The hardware

Eight ST1600FM0013 (IBM `IBM-SSGSSVJ1P6`), 1.6 TB each, all locked to 528-byte
sectors, all reached through the `wvg-sd-528` emulation. Under emulation each
disk presents about 1.55 TiB of 512-byte sectors. Host: Xeon Gold 6212U,
48 threads, 125 GiB RAM, single SAS3216 HBA, single-port backplane, 6 Gb/s per
link.

## Layout: four two-way mirrors, striped

```
zpool create tank \
  mirror sdb sdc \
  mirror sdd sde \
  mirror sdf sdg \
  mirror sdh sdi
```

The brief asks for IOPS and throughput first, with safety assured. That settles
it, because on ZFS the number of vdevs is what multiplies random-write IOPS:

| layout | usable | random write IOPS | resilver | survives |
|---|---|---|---|---|
| 4x 2-way mirror | ~6.2 TiB | 4 vdevs | fast, copies used blocks only | 1 disk per vdev |
| 2x RAIDZ1(4) | ~9.3 TiB | 2 vdevs | full width | 1 disk per vdev |
| 1x RAIDZ2(8) | ~9.3 TiB | 1 vdev | full width, slowest | any 2 disks |

RAIDZ2 gives the strongest guarantee (any two disks) and half again the
capacity, but a single vdev means random writes run at roughly one disk's IOPS.
Mirrors give four times that and resilver in a fraction of the time, which
matters more here than the extra 3 TiB.

The honest cost: mirrors die if both halves of one vdev die. With eight disks
of the same model, age and workload, correlated failure is not hypothetical.
That is what the scrub schedule and the spare below are for.

**No hot spare in the pool.** Eight disks into four mirrors uses all of them.
Keep a ninth on the shelf and rely on fast SSD resilver plus monitoring; giving
up a whole vdev for a spare would cost 25 % of the IOPS this layout exists for.

## The bottleneck may not be the disks

Before tuning ZFS, note what sits underneath it. The emulation's bounce buffer
pool is **global across all disks**: 576 chunks of 64 KiB, 36 MiB total, while
queue depth is set per disk at 32. Eight disks can therefore submit 256
concurrent requests against a pool that serves about 33 full-size ones. See
[kernel-patch/PERFORMANCE.md](kernel-patch/PERFORMANCE.md).

Exhaustion is safe (`BLK_STS_RESOURCE`, requeue) but it caps throughput. So the
first benchmark is not a ZFS benchmark at all: it is raw `fio` against the bare
devices, to find where the emulation saturates. Tuning ZFS below that ceiling is
pointless.

## Pool and dataset properties

```
zpool create -o ashift=12 \
             -o autotrim=on \
             -O compression=lz4 \
             -O atime=off \
             -O xattr=sa \
             -O dnodesize=auto \
             -O recordsize=128K \
             tank <vdevs>
```

Why each:

**`ashift=12`.** The emulation presents 512-byte logical and physical sectors,
and one host sector maps onto exactly one device sector, so there is no
read-modify-write penalty at any alignment. `ashift=9` would match the device
one-to-one and waste less on small blocks, but it locks the pool to 512-byte
granularity forever and gains little on an SSD. 4 KiB is eight device sectors,
still a clean multiple.

**`compression=lz4`.** Every byte ZFS does not write is a byte that does not
pass through the bounce buffer. Compression is unusually valuable here because
the bottleneck is the copy, not the media.

**`autotrim=on`.** Only useful because the TRIM fix in
`kernel-patch/port_universal.py` keeps UNMAP alive through the emulation. With
the stock patch, discard is disabled and this setting does nothing. Verify with
`zpool status -t` that trim is actually supported before trusting it.

**`atime=off`** removes a write per read. **`xattr=sa`** and
**`dnodesize=auto`** cut metadata I/O.

`recordsize` stays at the 128 KiB default for general use and gets set per
dataset: 16 KiB for VM images backing databases, 1 MiB for bulk storage. Note
that a 1 MiB record hits the emulation's `emulate_528_max_sectors` cap of
2048 sectors exactly, so it cannot be aggregated further.

## Matching ZFS to the emulation

The two limits have to agree or ZFS spends its time being requeued:

```
zfs_vdev_aggregation_limit  <=  emulate_528_max_sectors * 512
```

At the defaults both are 1 MiB, which happens to line up. If
`emulate_528_max_sectors` is lowered to trade request size for parallelism, the
aggregation limit has to come down with it.

Vdev queue depths (`zfs_vdev_async_write_max_active` and friends, 10 per vdev)
across four vdevs plus sync traffic should stay inside what the bounce pool can
serve at once.

## Test plan

Each step gates the next; there is no point tuning past a limit that is not the
binding one.

1. **Raw device, one disk.** `fio` random 4 KiB read and write, sequential 1 MiB
   read and write, direct I/O. Establishes what one emulated disk does.
2. **Raw devices, all eight in parallel.** The same, run concurrently. If the
   aggregate is far below eight times the single-disk figure, the global pool is
   the ceiling and step 6 matters more than any ZFS setting.
3. **Vary `emulate_528_max_sectors`** across 2048, 256 and 128 with the queue
   depth left at 32, repeating step 2. This is a boot-parameter change, no
   rebuild.
4. **Create the pool**, repeat the same profiles through ZFS, and compare
   against step 2 to see what ZFS itself costs.
5. **TRIM.** `zpool trim tank`, then confirm through `sg_logs` that the device
   saw UNMAP, and read back a trimmed region: `LBPRZ=1` on these drives means it
   must come back zeroed.
6. **Rebuild with larger pools** if step 2 showed the ceiling, and repeat.
7. **Integrity under load.** `fio` with `verify=crc32c` for several hours, then
   a `zpool scrub`. ZFS checksums are the safety net here: any bug in the
   emulation's sector packing shows up as a checksum error rather than silent
   corruption, which is a good reason to run ZFS on this stack rather than ext4.

## Readiness for Proxmox

The target is to move this pool to the current Proxmox VE without recreating it.
Two things matter:

**Feature flags.** A pool created by a newer OpenZFS can refuse to import on an
older one. Check the ZFS versions on both sides first; if Proxmox is behind,
create with `-o compatibility=` pinned to what it supports rather than finding
out at import time.

**The kernel patch has to come along.** Proxmox ships its own kernel, so the
emulation must be rebuilt there. `kernel-patch/port_universal.py` detects the
queue-limits generation, so it should handle the PVE kernel the same way it
handles 7.0; the repository also carries the upstream `rebase_pve_528_patch.py`,
whose target path `patches/kernel/9999-wvg-sd-528-translation.patch` is the
Proxmox packaging convention.

Without the patch the pool is unimportable, because every member disk reports
zero bytes. That is worth writing on the case.
