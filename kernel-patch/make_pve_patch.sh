#!/bin/sh
# make_pve_patch.sh KERNEL_TREE [OUTPUT]
#
# Produce one unified patch against a pristine kernel tree that carries the
# 528 emulation with every fix from port_universal.py, ready to drop into the
# Proxmox kernel packaging as patches/kernel/9999-wvg-sd-528-translation.patch
# (the packaging applies it with patch -p1).
#
#   make_pve_patch.sh pve-kernel/submodules/ubuntu-kernel \
#       pve-kernel/patches/kernel/9999-wvg-sd-528-translation.patch
#
# The tree itself is not modified: the work happens on a copy of
# drivers/scsi/sd.c and sd.h. rebase_pve_528_patch.py, the upstream tool for
# the same job, carries none of the fixes; do not use its output.
set -e
TREE=${1:?usage: make_pve_patch.sh KERNEL_TREE [OUTPUT]}
OUT=${2:-9999-wvg-sd-528-translation.patch}
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

for f in drivers/scsi/sd.c drivers/scsi/sd.h; do
    [ -f "$TREE/$f" ] || { echo "not a kernel tree: $TREE/$f missing" >&2; exit 1; }
    mkdir -p "$WORK/a/drivers/scsi" "$WORK/b/drivers/scsi"
    cp "$TREE/$f" "$WORK/a/$f"
    cp "$TREE/$f" "$WORK/b/$f"
done
if grep -q sd_528 "$WORK/a/drivers/scsi/sd.c"; then
    echo "$TREE already carries the 528 emulation; start from a pristine tree" >&2
    exit 1
fi

(cd "$WORK/b" && patch -s -p1 --forward --fuzz=3 --no-backup-if-mismatch \
    -r - < "$HERE/wvg-sd-528.patch") || true
python3 "$HERE/port_universal.py" "$WORK/b"
for sym in sd_528_cmd_ctx sd_528_restrict_block_ops emu_cap \
           "sd_528_pool_chunks \* SD_528_BLOCKS_PER_CHUNK"; do
    grep -q "$sym" "$WORK/b/drivers/scsi/sd.c" || {
        echo "port incomplete: $sym missing, not writing $OUT" >&2; exit 1; }
done

(cd "$WORK" && diff -Naur a b) > "$WORK/out.patch" || true
# prove it applies to the pristine tree as it stands
(cd "$WORK/a" && patch -s -p1 --dry-run < "$WORK/out.patch")
cp "$WORK/out.patch" "$OUT"
echo "wrote $OUT ($(grep -c '^+' "$OUT") lines added)"
