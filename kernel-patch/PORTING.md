# Porting the patch to a new kernel

Date: 20 Nov 2026

`wvg-sd-528.patch` is a plain diff, so every kernel release that moves a line
next to one of its hunks makes `patch` reject that hunk. Of its 13 hunks in
`sd.c`, `patch` rejects 2 on vanilla 6.12 and 6.14, 3 on 6.8, 4 on Proxmox
7.0.14-15 and 6 on Proxmox 7.0.14-30. This page
describes how `port_universal.py` now lands the whole emulation on such a tree
without manual work, and how `check_trees.py` proves that it still does.

## Using it

```bash
# any kernel tree: lands the emulation in drivers/scsi/sd.c and sd.h
python3 port_universal.py /path/to/linux

# the same, and also write it as one unified diff for a package build
python3 port_universal.py /path/to/linux --emit-patch 9999-wvg-sd-528-translation.patch

# Proxmox: cut the diff against the Ubuntu tree of the pve-kernel checkout,
# replaying the Proxmox patches that touch sd.c first
python3 rebase_pve_528_patch.py --kernel-tree submodules/ubuntu-kernel \
    --pve-patches patches/kernel --output patches/kernel/9999-wvg-sd-528-translation.patch
```

Exit status 0 means every piece is in place. Status 2 means a piece could not
be placed: the tree is left exactly as it was and the output names the piece.
A second run on a finished tree changes nothing. `--check` only reports.

## What it does

1. Decides the queue-limits generation from the body of `sd_revalidate_disk()`
   alone (`none` up to 6.10, `local` for 6.11 to 6.14, `ptr` for 7.0). The
   emulation code uses `lim->` elsewhere in the file, so looking at the whole
   file calls every tree `ptr` as soon as the first hunk has landed.
2. Runs GNU patch with `-N -r -` while something is missing, so every hunk whose
   context still matches lands as before and no `.rej` files are left behind.
3. Places each piece that is still missing by anchoring on structure instead of
   context lines: inside a named function body, after a declaration, before a
   label. The large first hunk is taken from the diff itself, so the code is
   the same text either way.
4. Replaces the blanket "disable discard" of the original patch by a narrow
   restriction (WRITE SAME off, UNMAP kept) and moves the queue depth cap and
   that restriction to the end of `sd_revalidate_disk()`. Both are described in
   the comments of `port_universal.py`.
5. Checks that each piece is present exactly once, by definition or call site,
   never by a mention of the symbol.

## Why "already there" is decided by the definition

`sd_528_page_pool` and `sd_528_effective_max_sectors` appear in the first hunk,
so they are in the file as soon as that hunk has landed, whether or not the hunks
that use them have. A check that looks for the name reports a half-ported file
as finished. The same went for `sd_528_restrict_block_ops` and for the pool
destruction: the old check found a `mempool_destroy(sd_528_page_pool)` in the
error path of `init_sd()` and never added the one `exit_sd()` needs. Every check
now looks for the definition, for the call site inside the function that must
contain it, or for the exact statement. `emu_cap` appears only in the
`max_dev_sectors` cap and is a valid marker for it.

## What moved between kernels

| Tree | Hunks `patch` rejects | Why |
|---|---|---|
| 6.8 | 1, 9, 10 | `sd_revalidate_disk()` has no queue limits (the cap goes on `q->limits`) |
| 6.12, 6.14 | 9, 10 | `struct queue_limits lim;` is a local variable, so `lim.` instead of `lim->` |
| Proxmox 7.0.14-15 | 1, 9, 11, 13 | `scsi_register_driver(&sd_template)` has no `.gendrv` any more; context of hunks 1 and 9 changed |
| Proxmox 7.0.14-30 | 1, 4, 8, 9, 11, 13 | the same, plus `sd_large_page_pool` beside `sd_page_pool` and an early return in `sd_uninit_command()` (Ubuntu 7.0.0-44) |

## Regression test

`check_trees.py` takes any number of trees and, for each, checks with a pristine
copy of `sd.c` and `sd.h`:

1. the port finishes without manual work,
2. a second run changes nothing,
3. `--semantic-only` (GNU patch not used, every piece inserted by anchor) gives the
   same code, blank lines aside,
4. the emitted diff applies to the original files with `patch --fuzz=0`,
5. `make drivers/scsi/sd.o` builds from the ported files and prints no warning
   that the unmodified file does not print.

```bash
python3 check_trees.py \
    --tree 6.8=/src/linux-6.8 \
    --tree 6.12=/src/linux-6.12 \
    --tree 6.14=/src/linux-6.14 \
    --tree pve=/src/ubuntu-kernel,patches=/src/pve-kernel/patches/kernel,config=/src/pve.config
```

Per tree: `NAME=PATH[,patches=DIR][,config=FILE]`. `patches` is a directory of
patches applied before ours in the real build; those that touch `sd.c` or `sd.h`
are replayed first. `config` is a `.config`; without it the tree's own
`.config` is used, else `make defconfig` with SCSI and `BLK_DEV_SD` switched on.
The compile step builds inside the tree you name: the original files are put
back afterwards, but objects and a `.config` stay, so use a tree you can
spend. `--no-compile` skips step 5. It needs gcc, make, bc, flex, bison,
libssl-dev and libelf-dev, plus libdw-dev for a Proxmox or Ubuntu config.

Run on 20 Nov 2026 (x86_64, gcc from Debian 13):

| Tree | Generation | Steps 1 to 4 | `sd.o` (new warnings) |
|---|---|---|---|
| vanilla 6.8 | none | pass | builds (0) |
| vanilla 6.12 | local | pass | builds (0) |
| vanilla 6.14 | local | pass | builds (0) |
| Proxmox 7.0.14-15 (+ its patches) | ptr | pass | builds (0) |
| Proxmox 7.0.14-30 (+ patch 0141) | ptr | pass | builds (0) |

## Limits

- The tool reads and writes `drivers/scsi/sd.c` and `sd.h` only. A kernel that
  changes how the SCSI midlayer hands over data buffers can still build and be
  wrong; the QEMU test in [RESULTS.md](RESULTS.md) is what shows that, and a
  new kernel should go through it before it is booted on real disks.
- If an anchor is gone, the run stops with exit status 2 and names the piece.
  Add the anchor for the new shape to `port_universal.py` and add the tree to
  the regression test.
- Only amd64 was compiled.

## Automatic builds

The Proxmox build in the NKS Hub apt repository fetches this
repository on every run, cuts the diff with `rebase_pve_528_patch.py` against the
tree of the new Proxmox kernel, and builds only if that succeeds. A failure
stops the run before anything is built or published.
