# Code review of the 528 emulation, with ZFS in mind

Date: 27 Sep 2026. Starting point: `master` at `b2d3359`, tagged
`pre-zfs-review-2026-09-27`.

*Czech version: [HARDENING_CZ.md](HARDENING_CZ.md)*

## In short

The emulation packs and unpacks sectors correctly, and the on-disk layout is
right. What it gets wrong is the **life cycle of a SCSI command**. The emulation
swapped the bounce buffer back out in `sd_done()` and assumed the command ended
there. It does not. After a unit attention or a host reset the SCSI core sends
the same command again **without preparing it again**. That retry went to the
disk with the 512-byte host buffer and a CDB that still asked for 528-byte
blocks.

Reproduced under QEMU on 7.0 with a 528-byte `scsi_debug` disk. After a single
injected reset or unit attention:

- a **read** returned stale bounce buffer contents and reported success,
- a **write** stored data shifted by 16 bytes per sector and reported success.

On the real machine every disk sits behind one SAS3216. An HBA reset, a link
reset or a drive that reports "power on or reset occurred" hits every disk at
once, including both halves of each mirror. ZFS checksums catch the bad reads
and a scrub finds the bad writes. But when both copies of a freshly written
block went through the same reset, there is nothing left to repair them from.

Three more bugs were found and reproduced, one of which crashes the kernel.
All are fixed by a new step 10 in [port_universal.py](port_universal.py). The
on-disk format does not change: an existing pool reads back bit for bit the
same (test T17 below).

## What to do

1. Rebuild the kernel with the updated `port_universal.py` (usage in its
   docstring, now with `patch --fuzz=3`). Check that the log ends with
   `hardening  bounce table kept until uninit, per-command context`.
2. Run [test/run-qemu.sh](test/run-qemu.sh) against a test build of the same
   tree before rebooting the server. Everything except T6 and T7 must say OK.
3. After the reboot run `zpool scrub tank`, then `zpool status -v`. This is
   the check for anything the old kernel might have written wrong.
4. See whether the old kernel ever hit the path:
   `journalctl -k | grep -iE "power-on or device reset|unit attention|reset"`.
   No resets during I/O means the retry bug never fired.

Boot parameters stay as they are. Two visible changes:
`queue/physical_block_size` now reads 4096 instead of 512, and clearing
`emulate_512_from_fat_sectors` at runtime no longer takes a disk away (both
explained below).

## Findings

| # | Severity | Problem | Reproduced | Fixed |
|---|---|---|---|---|
| 1 | critical | a retry after UA/DID_RESET runs with the host buffer: bad data returned as good, shifted data written as good | yes, T2 to T5 | yes |
| 2 | critical | device offline with commands on the requeue list: `scsi_free_sgtables()` walks the bounce table, kernel oops | yes, T12 | yes |
| 3 | high | a short transfer (resid with GOOD status) was reported as a full one; the missing tail came from the bounce buffer | yes, T13 | yes |
| 4 | high | `sd_uninit_command()` freed `rq->end_io_data` as an emulation context on **every** request, emulated or not; dm-multipath keeps its own state there | from code | yes |
| 5 | high | a rescan cleared and set the emulation flags again; I/O in flight could see an unemulated disk, and clearing the parameter at runtime made a live disk 0 bytes | T11 | yes |
| 6 | high | `emulate_512_from_fat_sectors` also accepted 520-byte disks with no bounce path at all, so every block would get a 512-byte payload | from code | yes, 520 no longer emulated |
| 7 | medium | `physical_block_size` forced to 512, dropping the drive's 8 x 528 hint; a plain `zpool add` would pick ashift=9 | T10 | yes, 4096 |
| 8 | medium | `port_universal.py` detected the kernel generation from `lim->`, which hunk 1 itself adds, so 6.8 to 6.17 always got the 7.0 code and failed to build | build | yes |
| 9 | medium | `port_universal.py` took hunk 1 as present when it was rejected (it checked `sd_528_page_pool`, which hunk 12 adds too); with `--fuzz` the max_dev_sectors cap landed in a comment on 6.8 and was never applied | build | yes |
| 10 | low | the sd_done() alignment check ran on 528-byte resid with power-of-two arithmetic | from code | yes, skipped for emulated commands |
| 11 | low | pool creation sat between `sd_page_pool` allocation and its NULL test after `--fuzz` | from code | yes |

### 1. The retry path

`scsi_io_completion()` answers a unit attention on a fixed disk, and any
`DID_RESET`, with `ACTION_RETRY`: `__scsi_queue_insert(cmd, ..., false)`, the
same command with `RQF_DONTPREP` still set. `sd_setup_read_write_cmnd()` does not
run again, so nothing puts the bounce table back. `mpt3sas` returns
`DID_RESET` for `SCSI_TASK_TERMINATED` and `SCSI_EXT_TERMINATED`, that is for
commands ended by a task-management function or a host reset, and a drive
reports 29/00 after any reset it notices.

On the real HBA the read case plays out as in QEMU: the drive sends 528 bytes
per block into a buffer sized for 512, and `mpt3sas` turns
`SCSI_DATA_OVERRUN` into `DID_OK`. The core then sees success, and the old
completion copied the bounce buffer, left over from the failed first attempt,
over the host buffer.

The fix keeps the bounce table installed from preparation until
`sd_uninit_command()`. `sd_done()` only unpacks. A retry therefore goes out with
the buffer its CDB describes, and a READ is simply unpacked again.

### 2. Freeing the wrong table

With the bounce table installed, anything that frees the command without going
through `sd_done()` hands it to `sg_free_table_chained()`, which follows
"chain" pointers that are not there. That happens in `scsi_queue_rq()` when a
prepared command is dispatched to a device that has gone offline, which is
exactly what a failing disk in a mirror does.

The bounce table is now installed with `orig_nents = 0`. The core never builds a
data table like that, so `scsi_free_sgtables()` leaves it alone, and it marks
the command as emulated. `sd_528_free_emulation()` puts the host table back and
frees it with the exported `scsi_free_sgtables()`.

### 3. Short transfers

The old completion returned `host_len` whenever the status was GOOD. The fix
counts only whole 528-byte blocks the device confirmed (`dev_len - resid`),
unpacks those, and lets the core requeue the rest, as it does for any disk.
On `mpt3sas` this path is mostly closed anyway, because the patch sets
`underflow` to the full length and the driver turns a shorter underrun into
`DID_SOFT_ERROR`. Other HBAs report the resid with GOOD status.

A result short of the full transfer on an error (medium error, NO SENSE) is
still treated as nothing transferred. That fails the whole request rather than
part of it, which is conservative: ZFS reads the other side of the mirror and
rewrites the block. RECOVERED ERROR now counts as success, as it does in stock
`sd`. The old code re-ran such a command, and a sector that always reports
recovered errors would have kept it re-running forever.

### 4. `rq->end_io_data`

That field belongs to whoever submitted the request. Request-based
device-mapper (dm-multipath) stores its clone state there, and
`blk_execute_rq()` its completion. The emulation now finds its context from the
bounce table itself (`container_of` on the scatterlist, checked against a
back pointer), so it never touches the field.

### 5. Rescans

`sd_adjust_logical_sector_size()` cleared both flags and set them again on
every rescan, and the documented tuning procedure asks for rescans. The flags
are now written only when they change, and every I/O-path decision after
preparation uses the per-command context, not the disk flag. The flag also
sticks: once a disk is emulated it stays emulated while it reports 528.
Clearing the parameter only affects disks probed afterwards.

### 6. 520-byte sectors

The patch claimed 520 would work "when the transport can strip trailing
metadata". No SAS HBA does that for a drive that reports 520 as its logical
block length. Emulation now covers 528 only, and a 520 disk stays at 0 bytes
as in the stock driver, with a warning.

### 7. Physical block size

These drives report 8 logical blocks per physical block, 4224 bytes. The
emulation now translates that to 8 x 512 = 4096 instead of forcing 512. The
value is only a hint: it changes nothing about how existing data is read, and a
pool created with ashift=12 matches it exactly. What it changes is the default
for new vdevs. A `zpool add` without `-o ashift=12` would otherwise create an
ashift=9 vdev, and a pool with mixed ashift cannot have a top-level vdev
removed later.

## How it was tested

[test/](test/) holds everything needed:

- `scsi_debug-528.patch` lets `scsi_debug` present 528-byte sectors (one line,
  test kernels only),
- `init` is the busybox initramfs script with the checks,
- `rawrd.c` reads a native 528-byte block through SG_IO, for the layout check,
- `run-qemu.sh` builds the initramfs and boots the kernel, no KVM needed.

The test kernels had KASAN and `DEBUG_SG` enabled.

| test | unfixed 7.0 | fixed 7.0 | fixed 6.17 |
|---|---|---|---|
| T1 write/read 8 MiB | OK | OK | OK |
| T2 read + DID_RESET | **wrong data, success** | OK | OK |
| T3 read + unit attention | **wrong data, success** | OK | OK |
| T4 write + DID_RESET | **shifted data, success** | OK | OK |
| T5 write + unit attention | **shifted data, success** | OK | OK |
| T6/T7 injected recovered error, see below | OK | differs | differs |
| T8 eight parallel writers | OK | OK | OK |
| T9 17 unaligned sectors | OK | OK | OK |
| T10 physical block size | 512 | 4096 | 4096 |
| T11 parameter cleared + rescan | **disk 0 bytes** | kept | kept |
| T12 offline with requeued commands | **kernel oops** | OK | OK |
| T13 short transfers | **wrong data** | OK | OK |
| T14 real recovered error | OK | OK | OK |
| T15 transport errors | OK | OK | OK |
| T16 medium error | EIO | EIO | EIO |
| T17 on-disk layout | 512 B data + 16 x 00 | same | same |

T6 and T7 use an injection that returns RECOVERED ERROR without running the
command, which no real device does. A stock 512-byte `scsi_debug` disk with no
emulation fails them in exactly the same way, so the fixed emulation now behaves
like stock `sd`. The old code passed them only because it re-ran every such
command. T14 is the faithful version: the data moves, then RECOVERED ERROR is
reported, and both versions pass.

`port_universal.py` was run on vanilla 6.8, 6.14, 6.17 and 7.0 after
`patch --fuzz=3`. Each tree builds `sd.o` with `W=1` and no warnings, and a
second run changes nothing. On 7.0 the result is byte-identical to the tree the
QEMU tests ran on.

## Compatibility with existing data

- `sd_528_pack_sg_blocks()` and `sd_528_unpack_sg_bytes()`, the only code that
  moves bytes between host and device layout, are unchanged.
- Host LBA N is still device LBA N. Data sits in the first 512 bytes, and the
  16 trailing bytes are written as zeros (T17, checked with a native read).
- Capacity, logical block size and the boot parameters are unchanged.

## ZFS notes

- On SSDs ZFS aggregates up to `zfs_vdev_aggregation_limit_non_rotating`,
  128 KiB by default, not `zfs_vdev_aggregation_limit`. With
  `emulate_528_max_sectors=256` (128 KiB) the two already match. Check that
  `/sys/block/sdX/queue/rotational` reads 0 on the emulated disks.
- At 128 KiB a request takes 3 bounce chunks. Eight disks at queue depth 32 need
  8 x 32 x 3 = 768, so `emulate_528_pool_chunks=1024` (64 MiB) covers them.
  4608 (288 MiB) is not wrong, it just holds memory the ARC could use.
- Always pass `-o ashift=12` to `zpool add` and `zpool create`, whatever the
  kernel reports.
- The pool is ZFS for a good reason: checksums turn an emulation bug into a
  loud error instead of silent damage. Keep scrubs regular.

## Not changed

- The bounce pools are global, and 36 MiB is reserved at boot even with the
  emulation off. That costs memory only, and fixing it means making the
  parameter boot-only.
- An error that reports only part of a request as good fails the whole request
  (see 3).
- `alignment_offset` from READ CAPACITY(16) is still counted in 528-byte units.
  These drives report 0.
- Hosts with a virt boundary or DIX are now refused with EIO instead of being
  handed a table they cannot take. `mpt3sas` with SAS drives has neither.
