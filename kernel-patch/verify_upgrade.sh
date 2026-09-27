#!/bin/sh
# verify_upgrade.sh snapshot FILE [--sample N] DISK...
# verify_upgrade.sh compare FILE
#
# Proves on the real machine that a new kernel reads the emulated disks
# exactly as the old one did, before anything is written with it.
#
#   old kernel:  zpool export tank
#                verify_upgrade.sh snapshot /root/pre.txt /dev/disk/by-id/wwn-0x5000c500...
#   reboot into the new kernel (the exported pool is not imported at boot)
#   new kernel:  verify_upgrade.sh compare /root/pre.txt
#                -> only if it says "IDENTICAL": zpool import -o readonly=on tank ...
#
# It only ever reads, with O_DIRECT so the page cache cannot answer for the
# disk. Every disk is hashed in 1 GiB chunks with sha256; with --sample N only
# every Nth chunk plus the first and last two (where the ZFS labels live) are
# read, which takes minutes instead of about an hour for 8 x 1.55 TB.
# compare reads exactly the chunks the snapshot has, from the disks it names,
# so the disk arguments should be stable /dev/disk/by-id paths.
#
# snapshot also checks, on the old kernel, the three conditions under which
# the new one refuses I/O (integrity profile, virt boundary, DMA segment size
# below 65472), so a disk the new kernel would not serve is found before the
# pool is exported.
#
# It refuses to run on a disk that is part of an imported pool or mounted:
# an imported pool changes its disks all the time, and the hashes would differ
# for reasons that have nothing to do with the kernel.
set -eu

die() { echo "verify_upgrade: $*" >&2; exit 2; }
usage() { sed -n '2,3p' "$0" | sed 's/^# //' >&2; exit 2; }

check_idle() {
	real=$(readlink -f "$1") || die "$1: not found"
	[ -b "$real" ] || die "$1: not a block device"
	name=${real##*/}
	if command -v zpool >/dev/null 2>&1 &&
	   zpool status -PL 2>/dev/null | grep -Eq "/dev/${name}([0-9]+|p[0-9]+)?([[:space:]]|$)"; then
		die "$1 ($name) belongs to an imported pool; zpool export it first"
	fi
	if grep -Eq "^/dev/${name}([0-9]+|p[0-9]+)? " /proc/mounts; then
		die "$1 ($name) or one of its partitions is mounted"
	fi
	if [ -n "$(ls /sys/class/block/$name/holders 2>/dev/null)" ]; then
		die "$1 ($name) is held by another device (dm, md, ...)"
	fi
}

# hash_chunks DISK CHUNK... : print "DISK INDEX SHA256" per chunk
hash_chunks() {
	d=$1; shift
	for i in "$@"; do
		h=$(dd if="$d" bs=1M skip=$((i * 1024)) count=1024 iflag=direct 2>/dev/null |
		    sha256sum | cut -c1-64)
		echo "$d $i $h"
	done
}

# The new kernel refuses (EIO) to emulate on a disk with an integrity profile,
# a virt boundary or a DMA segment limit below one 65472-byte bounce chunk;
# the old one did not look. Catch that on the old kernel, before the export.
preflight() {
	name=${1##*/}
	q=/sys/class/block/$name/queue
	f=$(cat /sys/class/block/$name/integrity/format 2>/dev/null || echo none)
	[ "$f" = none ] || die "$1: integrity profile '$f'; the new kernel would refuse I/O on it"
	v=$(cat $q/virt_boundary_mask 2>/dev/null || echo 0)
	[ "$v" = 0 ] || die "$1: virt_boundary_mask $v; the new kernel would refuse I/O on it"
	m=$(cat $q/max_segment_size 2>/dev/null || echo 4294967295)
	[ "$m" -ge 65472 ] || die "$1: max_segment_size $m < 65472; the new kernel would refuse I/O on it"
}

props() {
	name=${1##*/}
	q=/sys/class/block/$name/queue
	echo "# $1 = $name size=$(cat /sys/class/block/$name/size)" \
	     "lbs=$(cat $q/logical_block_size) pbs=$(cat $q/physical_block_size)" \
	     "max_kb=$(cat $q/max_sectors_kb) seg=$(cat $q/max_segment_size 2>/dev/null)" \
	     "vbm=$(cat $q/virt_boundary_mask 2>/dev/null)" \
	     "integrity=$(cat /sys/class/block/$name/integrity/format 2>/dev/null || echo -)"
}

[ $# -ge 2 ] || usage
mode=$1; file=$2; shift 2
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

case $mode in
snapshot)
	sample=1
	if [ "${1:-}" = --sample ]; then sample=${2:?}; shift 2; fi
	[ $# -gt 0 ] || usage
	[ ! -e "$file" ] || die "$file exists; not overwriting a snapshot"
	for d in "$@"; do check_idle "$d"; preflight "$(readlink -f "$d")"; done
	{
		echo "# verify_upgrade snapshot, kernel $(uname -r), $(date -u +%FT%TZ), sample $sample"
		for d in "$@"; do props "$(readlink -f "$d")"; done
	} > "$tmp/head"
	n=0
	for d in "$@"; do
		real=$(readlink -f "$d")
		bytes=$(( $(cat /sys/class/block/${real##*/}/size) * 512 ))
		chunks=$(( (bytes + 1073741823) / 1073741824 ))
		list=""
		i=0
		while [ $i -lt $chunks ]; do
			if [ $i -lt 2 ] || [ $i -ge $((chunks - 2)) ] || [ $((i % sample)) -eq 0 ]; then
				list="$list $i"
			fi
			i=$((i + 1))
		done
		echo "$d size $bytes" > "$tmp/d$n"
		# shellcheck disable=SC2086
		hash_chunks "$d" $list >> "$tmp/d$n" &
		n=$((n + 1))
	done
	wait
	cat "$tmp/head" "$tmp"/d* > "$file"
	echo "snapshot of $n disk(s), $(grep -vc -e '^#' -e ' size ' "$file") chunks, written to $file"
	;;
compare)
	[ -f "$file" ] || die "$file: no such snapshot"
	grep '^#' "$file" | head -1
	echo "# now: kernel $(uname -r)"
	disks=$(grep ' size ' "$file" | cut -d' ' -f1)
	for d in $disks; do check_idle "$d"; done
	n=0
	for d in $disks; do
		real=$(readlink -f "$d")
		bytes=$(( $(cat /sys/class/block/${real##*/}/size) * 512 ))
		want=$(grep "^$d size " "$file" | cut -d' ' -f3)
		[ "$bytes" = "$want" ] || echo "$d size $bytes (was $want)" > "$tmp/sizebad$n"
		props "$real"
		# shellcheck disable=SC2046
		hash_chunks "$d" $(grep "^$d [0-9]* " "$file" | cut -d' ' -f2) > "$tmp/d$n" &
		n=$((n + 1))
	done
	wait
	grep '^# /' "$file" | sed 's/^# /# before: /'
	cat "$tmp"/d* | sort > "$tmp/now"
	grep -v -e '^#' -e ' size ' "$file" | sort > "$tmp/then"
	bad=0
	if ls "$tmp"/sizebad* >/dev/null 2>&1; then cat "$tmp"/sizebad*; bad=1; fi
	if ! cmp -s "$tmp/then" "$tmp/now"; then
		echo "DIFFERENT chunks (- before, + now: disk index sha256):"
		diff -u "$tmp/then" "$tmp/now" | grep -E '^[-+]/' | head -40
		bad=1
	fi
	if [ $bad = 0 ]; then
		echo "IDENTICAL: $(wc -l < "$tmp/now") chunks on $n disk(s) read back bit for bit as before"
	else
		echo "NOT IDENTICAL: do not import the pool with this kernel; boot the old one"
		exit 1
	fi
	;;
*)
	usage
	;;
esac
