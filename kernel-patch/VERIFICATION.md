# Verifying the wvg-sd-528 patch against upstream kernels

Date: 28 Aug 2026

> **Later outcome:** the problems described here were subsequently fixed and the
> patch was made to build and run. See [RESULTS.md](RESULTS.md). This document is
> the snapshot from the assessment stage, before the port to the 6.8 API.

## Conclusion first

**The patch targets an API that exists in no upstream kernel.** It cannot be
applied to 6.8 or to any newer series, because three of its hunks expect code
that has never been in `drivers/scsi/sd.c`.

## What was measured

The original `drivers/scsi/sd.c` was downloaded from `git.kernel.org` for tags
v6.8 through v6.14 and compared against the constructs the patch requires in its
context lines.

| Kernel | `lim = kmalloc` | `lim->max_dev_sectors` | `lim.max_dev_sectors` | `struct queue_limits lim;` |
|---|---|---|---|---|
| 6.8 | 0 | 0 | 0 | 0 |
| 6.9 | 0 | 0 | 0 | 0 |
| 6.10 | 0 | 0 | 0 | 0 |
| 6.11 | 0 | 0 | 1 | 4 |
| 6.12 | 0 | 0 | 1 | 4 |
| 6.13 | 0 | 0 | 1 | 4 |
| 6.14 | 0 | 0 | 1 | 4 |

Yet hunk 9 of the patch expects:

```c
	if (!scsi_device_online(sdp))
		goto out;

	lim = kmalloc(sizeof(*lim), GFP_KERNEL);
	if (!lim)
		goto out;
```

and hunk 10 works with `lim->max_dev_sectors`, that is, with a **pointer**.

Reality differs. Up to and including 6.10 there are no `queue_limits` there at
all. From 6.11 `struct queue_limits lim;` does exist, but as a **local variable
on the stack**: it is accessed with a dot (`lim.max_dev_sectors`), not an
arrow, and it is never allocated via `kmalloc`.

## How the application went

On vanilla 6.8:

```
sd.c   10 of 13 hunks applied, 3 failed (#1, #9, #10)
sd.h    3 of 3 applied
```

The ten hunks applied only because `patch` tolerates offset and fuzz. They are
small insertions into functions that have not changed since. The three that
failed are at places where the code has diverged substantially.

The bundled `rebase_pve_528_patch.py` failed as well:

```
ERROR: sd_revalidate_disk: expected one function definition, found 0
```

The script looks for a function shape it could not find in the tree.

## What follows from this

Taken together:

- the patch has **no header at all**: no author, no `Signed-off-by`, no
  copyright, no SPDX,
- its identifiers (`emulate_512_from_fat_sectors`, `sd_528_emulation`,
  `wvg-sd-528`) **appear nowhere on the internet**,
- it targets an **API that does not exist upstream** in any version,
- the bundled rebase tool fails against the tree on its very first check.

Together that is consistent with code that **has never been compiled**. Had the
author built it, they would have hit this contradiction immediately.

It cannot be ruled out that a downstream fork exists with `sd_revalidate_disk`
modified this way, but no trace of one was found, and neither Proxmox nor
Ubuntu carries it.

## What would be needed to make it usable

Rewrite hunks 9 and 10 for the real API of the target kernel. That is not
mechanical work: you have to understand where queue limits are set in that
particular version, and place the `sd_528_limit_queue_depth()` call and the
`max_dev_sectors` cap correctly. The rest of the patch (the large hunk 1 with
the emulation infrastructure) builds on that and would have to be verified
against it in full.

Before anyone builds on this, it should be established whether the
infrastructure makes sense at all. So far nobody has confirmed that it even
compiles, let alone that it ran.

## Recommendation

Do not use until it compiles and passes a data integrity test against a native
read via `sg_dd`. On its own this is not proof that the patch is wrong. It is
proof that it is **unverified**.
