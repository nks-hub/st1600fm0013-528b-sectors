# The kernel patch works: a 528B disk as a native 512B one

Date: 28 Aug 2026

> **Read this first:** if your disk accepts a plain `sg_format --size=512`, take
> that route instead: no capacity loss, stock kernel, full native speed. See
> [../reformat-528-512.md](../reformat-528-512.md). This patch is for disks that
> refuse the reformat.

## Conclusion

The `wvg-sd-528` patch, ported to kernel 6.8, **works**. A disk with 528-byte
sectors shows up in the system as a standard 512-byte block device. Verified by
writing and reading, including cross-validation against native 528B access.

The price is 16 bytes out of every sector: **1.55 TB instead of 1.60 TB** per
disk, roughly 388 GB out of 12.8 TB total.

**It does not address link speed.** The 6 Gb/s limit lives in the disk firmware
and the kernel can do nothing about it.

## How it was tested without a reboot

The host could not be rebooted, so testing happened in QEMU with SCSI
passthrough. The VM gets direct access to the physical disk while the host runs
untouched:

```bash
qemu-system-x86_64 -enable-kvm -m 2048 -smp 2 -nographic -no-reboot \
  -kernel /usr/src/k/linux-6.8/arch/x86/boot/bzImage \
  -initrd /tmp/qinitrd.gz \
  -append "console=ttyS0 sd_mod.emulate_512_from_fat_sectors=1" \
  -device virtio-scsi-pci,id=scsi0 \
  -drive file=/dev/sg2,if=none,id=d0,format=raw \
  -device scsi-generic,drive=d0,bus=scsi0.0
```

The essential part is `scsi-generic`, which QEMU describes as *"pass through
generic scsi device (/dev/sg*)"*. A busybox-based initramfs is enough.

## Results

### Control run against the live one

```
emulate_512_from_fat_sectors=0
    sda: sectors=0        physical_bs=4224     dd -> 0 records

emulate_512_from_fat_sectors=1
    sd 0:0:0:0: [sda] Attached SCSI disk
    sda: sectors=3030911576  logical_bs=512  physical_bs=512  ->  1445 GB
    dd -> 1+0 records in/out
```

### Data integrity

| Test | Result |
|---|---|
| Aligned 32 kB write at LBA 1000 | md5 match |
| **Unaligned** 17-sector write at LBA 2049 | match |
| 1 MB **across a 64 kB chunk boundary** at LBA 10240 | md5 match |

### Cross-validation

The strongest evidence. Data written through the emulated 512B device, read back
from the host natively as a 528-byte sector:

```
sg_raw -r 528 -o out.bin /dev/sg2 28 00 00 00 03 e8 00 00 01 00

through emulation in QEMU:  3b 0f a4 56 af f2 0e 24 c8 c9 87 4b a2 e2 6f 29
natively from the disk:     3b 0f a4 56 af f2 0e 24 c8 c9 87 4b a2 e2 6f 29   OK
                            25 24 02 a8 c3 21 4d d3 96 c4 7f 4b b9 50 4a 53   OK
metadata[512:528]:          all zeros
```

The data is physically stored in the right place in the right sectors, with the
16 metadata bytes zeroed. The disk after all tests: `SMART Health OK`, zero bad
sectors, block size unchanged at 528 B.

## Build recipe

```bash
# source
curl -O https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.8.tar.xz
tar xf linux-6.8.tar.xz && cd linux-6.8

# patch
patch -p1 --forward < wvg-sd-528.patch          # 10 of 13 hunks apply
python3 apply_manual_hunks.py $PWD              # applies the 3 rejected ones
python3 port_to_68.py $PWD                      # translates to the 6.8 API

# config
cp /boot/config-$(uname -r) .config
scripts/config --disable MODULE_SIG
scripts/config --disable SYSTEM_TRUSTED_KEYS
scripts/config --disable SYSTEM_REVOCATION_KEYS
scripts/config --disable DEBUG_INFO_BTF
# key drivers built in, so the original initrd suffices
for c in SCSI_MPT3SAS OVERLAY_FS IGB BLK_DEV_NBD; do
    scripts/config --set-val CONFIG_$c y
done
make olddefconfig
make -j$(nproc) bzImage modules
```

The finished `arch/x86/boot/bzImage` is 13,492,736 B.

## What had to be fixed in the patch

The patch targets a tree where queue limits are passed as
`struct queue_limits *lim`. That does not exist upstream, verified for 6.8
through 6.14.

| Location | Fix |
|---|---|
| `sd_disable_advanced_block_ops()` | took `lim`, touched `lim->max_discard_sectors` → rewritten to `blk_queue_max_discard_sectors(q, 0)` and `blk_queue_max_write_zeroes_sectors(q, 0)`, call site adjusted |
| Hunk 9 | the `sd_528_limit_queue_depth()` call moved ahead of `buffer = kmalloc(SD_BUF_SIZE, …)` |
| Hunk 10 | `lim->max_dev_sectors` → `q->limits.max_dev_sectors`, `lim->max_segments` → `q->limits.max_segments` |

A trap I fell into twice: the check
`if "sd_528_effective_max_sectors" in text` is a false positive, because that symbol
arrives with hunk 1, so hunk 10 looked done when it was not. The correct thing
to check is `emu_cap`.

## Deployment on the physical machine

The server PXE-boots from netbootxyz (an nginx container). A sample menu entry
is in `live-ubuntu.ipxe`:

```
kernel ${kernel_url}vmlinuz ip=dhcp boot=casper netboot=url url=${squash_url} \
       initrd=initrd.magic ${cmdline}
initrd ${kernel_url}initrd
```

It is enough to upload `bzImage` and add an entry with
`sd_mod.emulate_512_from_fat_sectors=1` on the cmdline. Because it is a live
boot, a failed boot is fixed by a plain restart, so nothing can break permanently.

## Before this touches real data

The tests above passed, but they were short. Before production use:

- `fio` with verification over several hours,
- `badblocks -w` across the whole disk,
- a test with a filesystem (mkfs, write, umount, fsck),
- verify the behaviour of a power loss in the middle of a write.

The emulation discards 16 bytes of metadata that the IBM firmware uses for its
own integrity checking. The disk will no longer look consistent to it, which
does not matter for us (we use it as an ordinary block device), but such a disk
has no business going back into an IBM array.
