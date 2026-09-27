#!/bin/sh
# Boot a patched kernel under QEMU against a scsi_debug disk with 528-byte
# sectors and run the checks in ./init. No hardware and no KVM needed.
#
#   cd linux-7.0
#   patch -p1 --forward --fuzz=3 < .../wvg-sd-528.patch
#   python3 .../port_universal.py .
#   patch -p1 < .../test/scsi_debug-528.patch      # test kernels only
#   make tinyconfig
#   scripts/config -e 64BIT -e PRINTK -e TTY -e SERIAL_8250 -e SERIAL_8250_CONSOLE \
#       -e BLK_DEV_INITRD -e RD_GZIP -e DEVTMPFS -e PROC_FS -e SYSFS -e BINFMT_ELF \
#       -e BINFMT_SCRIPT -e BLOCK -e SCSI -e BLK_DEV_SD -e SCSI_LOWLEVEL -e SCSI_DEBUG \
#       -e SMP -e MULTIUSER -e SHMEM -e TMPFS -e PCI -e DEBUG_FS -e DEBUG_KERNEL \
#       -e DEBUG_SG -d SLUB_TINY -e KASAN -e KASAN_GENERIC -e SLUB_DEBUG -e KALLSYMS \
#       -e PANIC_ON_OOPS
#   make olddefconfig && make -j$(nproc) bzImage
#   .../test/run-qemu.sh arch/x86/boot/bzImage
#
# Scenarios: add t=torture, t=trim, t=eh, t=pool, t=big or t=cdb16 to the
# extra kernel args (default t=basic); see ./init for what each needs, e.g.
#   run-qemu.sh bzImage "t=trim scsi_debug.lbpu=1 scsi_debug.lbprz=1"
#
# Needs qemu-system-x86_64, busybox-static, cpio and a C compiler.
set -e
KERNEL=${1:?usage: run-qemu.sh bzImage [extra kernel args]}
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/root/bin" "$WORK/root/proc" "$WORK/root/sys" "$WORK/root/dev" "$WORK/root/tmp"
cp "$(command -v busybox)" "$WORK/root/bin/"
for a in sh dd cmp md5sum mount echo cat sleep usleep head od tr grep sync reboot dmesg kill timeout; do
    ln -s busybox "$WORK/root/bin/$a"
done
cc -static -O2 -o "$WORK/root/bin/rawrd" "$HERE/rawrd.c"
cc -static -O2 -o "$WORK/root/bin/blkops" "$HERE/blkops.c"
cc -static -O2 -pthread -o "$WORK/root/bin/torture" "$HERE/torture.c"
cp "$HERE/init" "$WORK/root/init"; chmod +x "$WORK/root/init"
(cd "$WORK/root" && find . | cpio -o -H newc 2>/dev/null | gzip) > "$WORK/initrd.gz"

timeout ${QEMU_TIMEOUT:-900} qemu-system-x86_64 -m 1024 -smp 2 -nographic -no-reboot \
    -kernel "$KERNEL" -initrd "$WORK/initrd.gz" \
    -append "console=ttyS0 quiet loglevel=4 panic=-1 \
             scsi_debug.sector_size=528 scsi_debug.dev_size_mb=64 scsi_debug.physblk_exp=3 \
             sd_mod.emulate_512_from_fat_sectors=1 sd_mod.emulate_528_max_sectors=256 $2" \
    | sed -n '/=== TESTSTART/,/=== TESTEND/p;/KASAN\|BUG:\|Oops/p'
