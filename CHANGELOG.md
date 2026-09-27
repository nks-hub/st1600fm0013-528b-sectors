# Changelog and procedures

*Czech version: [CHANGELOG_CZ.md](CHANGELOG_CZ.md)*

Changes to the kernel patch for 528-byte disks, and how to build, test and
deploy it on an existing pool, and how to go back. Each fix, with its
reproduction, is described in [kernel-patch/HARDENING.md](kernel-patch/HARDENING.md).

Contents:

- [Changes](#changes)
- [Procedure A: building the kernel](#procedure-a-building-the-kernel)
- [Procedure B: building for Proxmox](#procedure-b-building-for-proxmox)
- [Procedure C: testing before deployment](#procedure-c-testing-before-deployment)
- [Procedure D: upgrading an existing pool](#procedure-d-upgrading-an-existing-pool)
- [Procedure E: going back to the old kernel](#procedure-e-going-back-to-the-old-kernel)
- [Procedure F: after the upgrade](#procedure-f-after-the-upgrade)

---

## Changes

### 2026-09-27: checked against the Proxmox kernels

Taken from the Proxmox kernel packaging git (`git.proxmox.com/git/pve-kernel.git`):

| series | latest version | branch, date | Ubuntu base | upstream |
|---|---|---|---|---|
| **7.0** | **`proxmox-kernel-7.0.14-20-pve`** | `master`, 2026-09-24 | `Ubuntu-7.0.0-39.39` | 7.0.14 |
| 6.17 | `proxmox-kernel-6.17.13-21-pve` | `trixie-6.17`, 2026-07-28 | `Ubuntu-hwe-6.17-6.17.0-42.42` | 6.17.13 |

**The patch fits both series with no manual work.** Procedure B
(`make_pve_patch.sh`) runs through on its own, and every step of
`port_universal.py` finds its anchor.

The tests ran on the Ubuntu 38.38 sources, also upstream 7.0.14. The exact
tag `Ubuntu-7.0.0-39.39` (commit `874594fa`, the one Proxmox pins) was fetched
from the Proxmox mirror and compared: `sd.c` and `sd.h` are byte-identical to
38.38, and the `9999-...` patch generated from it is identical in content to
the tested one. Between 38.38 and 39.39 only two things change nearby.
`scsi_lib.c` zeroes the padding bytes under `dma_pad_mask`, which only ATAPI
uses and which happens before the emulation swaps the buffer. `scsi_error.c`
reads a power-management flag differently in the EH. Neither touches the
emulation.

| check | 7.0.14 (Ubuntu 38.38 / 39.39) | 6.17.13 (Ubuntu 42.42) |
|---|---|---|
| `make_pve_patch.sh`, all steps | OK | OK |
| queue limits generation | `ptr` | `ptr` (Ubuntu took the change from 7.0) |
| resulting `sd.c` against the tested 7.0 | differs only by 2 unrelated Ubuntu lines | - |
| build `sd.o` with `W=1` | no warnings | no warnings |
| `basic`, `trim`, `big`, `cdb16`, `eh`, `pool` | OK (T6/T7 comparison only) | `basic`, `trim`, `big` OK |
| `torture` with faults | 0 bad, 0 failed | 0 bad, 0 failed (512-byte iovecs) |

None of the 124 Proxmox patches in the 7.0 series touches `drivers/scsi/sd.c`,
`sd.h`, `scsi_lib.c` or the block layer. The only one about `mpt3sas` (0122)
changes a cpumask computation in `mpt3sas_base.c`, not the data path. Our
`9999-...` patch therefore applies to Ubuntu's `sd.c` exactly.

On 6.17 the torture run with 64-byte iovecs gets `EINVAL`. That is the 6.17
block layer, which wants O_DIRECT segments in multiples of 512 bytes, refusing
the request before it reaches the driver. It has nothing to do with ZFS, which
submits whole pages.

### 2026-09-27: emulation hardening (branch `claude/clever-dirac-82ozij`)

Starting point: `master` at `b2d3359`, tagged locally as
`pre-zfs-review-2026-09-27`. Commits `99bab03`, `7a87892`, `df20ce5`, `02a492a`,
`4859a32`.

**The on-disk format does not change.** Host LBA N is still device LBA N, 512
bytes of data and 16 zero bytes. The same data written by the old and the new
version gives a byte-identical raw disk image (test `t=xver`).

#### Fixed: data integrity

- **A retry after a reset or unit attention** went to the disk with the 512-byte
  buffer and a 528-byte CDB. Reads returned foreign data as success, writes
  stored shifted data as success. The bounce buffer now stays installed for the
  whole life of the command. (`99bab03`)
- **A short transfer** (resid with GOOD status) was reported as complete, and
  the missing tail came from the bounce buffer. Only blocks the device confirmed
  are counted now. (`99bab03`)
- **Kernel oops** when a disk went offline with commands on the requeue list.
  (`99bab03`)
- **`rq->end_io_data`** was freed as an emulation context on every request,
  and that field belongs to dm-multipath. The context is now found through the
  bounce table. (`99bab03`)
- **A rescan at runtime** briefly turned the emulation off, and clearing the
  parameter made a live disk 0 bytes. The flags are written only on change, and
  emulation holds while the disk reports 528. (`99bab03`)
- **520-byte disks** were accepted with no bounce path. They are not emulated
  any more and stay at 0 bytes, as in the stock driver. (`99bab03`)
- **A permanently stalled disk** when the bounce pool was smaller than one
  request. The request size is now capped at what the pool holds (step 10c).
  (`df20ce5`)

#### Fixed: build

- `port_universal.py` detected the kernel generation wrongly, and 6.8 to 6.17
  did not build. Detection now looks only inside `sd_revalidate_disk()`.
  (`99bab03`)
- `port_universal.py` did not notice a rejected hunk 1. With `--fuzz` the
  `max_dev_sectors` cap landed inside a comment on 6.8, and pool creation in the
  wrong place. Both are now moved to where they belong. (`99bab03`)
- The build recipe in `RESULTS.md` and `rebase_pve_528_patch.py` produce a
  kernel without the fixes. The recipe is marked superseded; Proxmox gets the
  new `make_pve_patch.sh`. (`df20ce5`)

#### Changed: visible behaviour

| what | before | now | effect |
|---|---|---|---|
| `queue/physical_block_size` | 512 | 4096 (the drive reports 8 x 528) | a hint only; `zpool add` without `-o ashift` no longer picks 9 |
| `queue/discard_granularity` | 512 | 4096 | TRIM skips fragments below 4 KiB, none at ashift=12 |
| 520-byte disks with the parameter set | "emulated" without a bounce path | not emulated, 0 bytes, warning | none for 528 |
| clearing `emulate_512_from_fat_sectors` at runtime + rescan | disk 0 bytes | disk stays emulated | the parameter applies to newly found disks |
| `emulate_528_pool_chunks` below one request | disk stalled for good | request cap lowered | none with sane settings |
| RECOVERED ERROR | command re-run | success, as in stock `sd` | none |
| host with DIX, a virt boundary or a small `max_segment_size` | undefined | EIO | does not happen on `mpt3sas` with SAS drives |

Boot parameters and capacity stay the same.

#### Added

- `kernel-patch/make_pve_patch.sh`: a patch for Proxmox's `patches/kernel/`
  with every fix.
- `kernel-patch/verify_upgrade.sh`: read-only proof on the real machine that the
  new kernel reads the disks exactly as the old one.
- `kernel-patch/test/`: QEMU test suite with `scsi_debug` (528-byte sectors),
  scenarios `basic`, `torture`, `trim`, `eh`, `pool`, `big`, `cdb16`, `xver`,
  `xverify`.
- `kernel-patch/HARDENING.md` and `HARDENING_CZ.md`: code review, test results,
  ZFS notes.

### 2026-08-28: port and first fixes (`b2d3359`)

The state the pool runs on today. `port_universal.py` ports the third-party
patch to newer kernels, enables TRIM (UNMAP), fixes where the block-op
restriction and the queue depth cap run, the double free in `init_sd()`, and
makes the pool sizes boot parameters. Measurements in
[kernel-patch/MEASUREMENTS.md](kernel-patch/MEASUREMENTS.md).

**Known defects of this version** are every item under "Fixed" above.

### 2026-08-28: patch received

`wvg-sd-528.patch` and `rebase_pve_528_patch.py`, provenance unknown, see
[kernel-patch/ORIGIN.md](kernel-patch/ORIGIN.md).

---

## Procedure A: building the kernel

On a pristine tree (checked for 6.8, 6.14, 6.17 and 7.0):

```bash
cd linux-7.0
patch -p1 --forward --fuzz=3 < /path/kernel-patch/wvg-sd-528.patch
python3 /path/kernel-patch/port_universal.py .
```

The script's output must contain:

```
  hardening                  bounce table kept until uninit, per-command context
  pool-sized request cap     inserted
  ...
  sd_528_cmd_ctx               present
```

`rejects`/`FAILED` messages from `patch` are fine; the script fills in the
missing parts. If the script stops with "hunk 1 ... is not present", the patch
was not run with `--fuzz=3`.

Then the usual build with the existing configuration:

```bash
cp /boot/config-$(uname -r) .config
make olddefconfig
make -j$(nproc) bzImage modules      # or bindeb-pkg for .deb packages
```

## Procedure B: building for Proxmox

**Do not use** `rebase_pve_528_patch.py`; it carries none of the fixes.

```bash
apt install devscripts
git clone https://git.proxmox.com/git/pve-kernel.git     # master = the 7.0 series
cd pve-kernel                                            # (trixie-6.17 for 6.17)
make submodule                                           # pristine submodules/ubuntu-kernel

# no Proxmox patch may change sd.c/sd.h; this must print nothing
grep -l 'drivers/scsi/sd\.[ch]' patches/kernel/*.patch

/path/kernel-patch/make_pve_patch.sh submodules/ubuntu-kernel \
    patches/kernel/9999-wvg-sd-528-translation.patch

make build-dir-fresh
mk-build-deps -ir proxmox-kernel-*/debian/control        # build dependencies
make deb
```

The script works on a copy of `sd.c`/`sd.h` and leaves the tree alone. If any
fix is missing it writes nothing. At the end it checks that the patch applies
to the tree. The Proxmox build applies `patches/kernel/*.patch` in alphabetical
order with `patch --batch`, so `9999-...` goes last, and a mismatch stops the
build instead of producing a wrong kernel. If the `grep` above prints anything,
make the patch from the directory with the Proxmox patches already applied (the
build dir after `make build-dir-fresh`) instead of from the submodule.

## Procedure C: testing before deployment

What gets tested is a test build of **the same tree**, with a small
configuration and a `scsi_debug` that can do 528-byte sectors. No disks and no
KVM are needed, only `qemu-system-x86_64`, `busybox-static`, `cpio` and `cc`.

```bash
cp -a linux-7.0 linux-7.0-test && cd linux-7.0-test
patch -p1 < /path/kernel-patch/test/scsi_debug-528.patch     # test tree only
# configuration: see the header of kernel-patch/test/run-qemu.sh (tinyconfig + SCSI_DEBUG, KASAN)
make -j$(nproc) bzImage

T=/path/kernel-patch/test
$T/run-qemu.sh arch/x86/boot/bzImage                   # basic
$T/run-qemu.sh arch/x86/boot/bzImage t=torture
$T/run-qemu.sh arch/x86/boot/bzImage "t=trim scsi_debug.lbpu=1 scsi_debug.lbprz=1"
$T/run-qemu.sh arch/x86/boot/bzImage t=eh
```

Expected:

- `basic`: everything OK except T6/T7. Those are for comparison only; stock
  `sd` on a 512-byte disk gives the same result.
- `torture`: `0 bad sectors` on both lines.
- No `KASAN`, `BUG:` or `Oops` anywhere.

**Testing the old to new upgrade** (`t=xver`): `sd` and `scsi_debug` as modules
(`scripts/config -e MODULES -e MODULE_UNLOAD -m BLK_DEV_SD -m SCSI_DEBUG`), with
`sd_mod.ko` built twice against the same kernel. The first time the tree is
prepared with the old script from the starting point
(`git show b2d3359:kernel-patch/port_universal.py > port_old.py`), the second
time with the new one:

```bash
mkdir mods
cp drivers/scsi/scsi_debug.ko mods/
# ... sd_mod.ko from the old preparation as mods/sd-old.ko, from the new one as mods/sd-new.ko
MODS=$PWD/mods $T/run-qemu.sh arch/x86/boot/bzImage t=xver
MODS=$PWD/mods $T/run-qemu.sh arch/x86/boot/bzImage t=xverify
```

`X1` to `X8` and `V1` to `V3` must match the expectation in parentheses.

## Procedure D: upgrading an existing pool

Pool `tank`, eight disks. Fill in the `/dev/disk/by-id/...` paths from
`ls -l /dev/disk/by-id/ | grep -v part`.

**1. On the old kernel, in service (changes nothing):**

```bash
zdb -C tank | grep ashift                    # expect 12
zpool status -v tank > /root/pre-zpool-status.txt
cat /proc/cmdline                            # sd_mod.* parameters; the new kernel needs the same
```

**2. Install the new kernel and boot it once only:**

```bash
proxmox-boot-tool kernel list
proxmox-boot-tool kernel pin <new-version> --next-boot
```

The old kernel stays the default. Every later plain reboot goes back to it.

**3. On the old kernel: stop the pool and prevent automatic imports:**

```bash
# stop the VMs/CTs that use the pool
pvesm set <storage-id> --disable 1                 # the storage plugin would import the pool otherwise
systemctl disable zfs-import@tank.service 2>/dev/null
zpool export tank
```

**4. Snapshot the disks (read-only):**

```bash
/path/kernel-patch/verify_upgrade.sh snapshot /root/pre.txt --sample 16 \
    /dev/disk/by-id/wwn-0x5000c500aaaaaaaa \
    /dev/disk/by-id/wwn-0x5000c500bbbbbbbb   # ... all eight
```

Without `--sample` it reads everything, about an hour, and is the strongest
proof. With `--sample 16` it takes minutes. The script first checks that the
new kernel will serve the disks, and refuses to run if a disk is in an imported
pool.

**5. Reboot into the new kernel:**

```bash
reboot
# after boot:
uname -r
dmesg | grep -c "Emulating 512-byte sectors"      # 8
```

**6. Compare (read-only):**

```bash
/path/kernel-patch/verify_upgrade.sh compare /root/pre.txt
```

- `IDENTICAL`: go on.
- `NOT IDENTICAL`: **import nothing**, `reboot` (brings back the old kernel),
  and report with the output.

**7. Read-only import:**

```bash
zpool import -o readonly=on tank
zpool status -v tank            # compare with /root/pre-zpool-status.txt
# optionally read the data: ZFS verifies checksums on every read
zpool export tank
```

**8. Normal import, scrub, undo step 3:**

```bash
zpool import tank
zpool scrub tank
pvesm set <storage-id> --disable 0
systemctl enable zfs-import@tank.service 2>/dev/null
zpool status -v tank            # once the scrub is done: 0 errors
proxmox-boot-tool kernel pin <new-version>       # permanently, once the scrub is clean
```

## Procedure E: going back to the old kernel

At any time, even after the normal import and writes: the on-disk format is the
same in both directions (tests X6 and X7).

```bash
proxmox-boot-tool kernel pin <old-version>
reboot
```

During steps 5 to 7 of procedure D a plain `reboot` is enough, since the
`--next-boot` pin lasts one boot only. Going back to the old kernel also brings
back its bugs, that is the risk on SAS resets.

## Procedure F: after the upgrade

Find out whether the old kernel could have damaged data, that is whether a
reset happened while it was running:

```bash
journalctl -k --list-boots
journalctl -k -b <boot-with-old-kernel> | grep -iE "power-on or device reset|unit attention|DID_RESET|reset"
```

No resets during I/O means the old kernel's bug never fired. Either way, the
scrub from step 8 decides:

- `0 errors`: the pool is fine.
- Repaired errors (`repaired`): the mirror fixed them from the other copy.
- `Permanent errors`: `zpool status -v` lists the affected files. Those need
  restoring from backup.

Recommended settings (as measured, only a smaller pool):

```
sd_mod.emulate_512_from_fat_sectors=1
sd_mod.emulate_528_queue_depth=32
sd_mod.emulate_528_max_sectors=256
sd_mod.emulate_528_pool_chunks=1024      # 4608 works too, it just holds 224 MiB more
sd_mod.emulate_528_pool_contexts=512
```
