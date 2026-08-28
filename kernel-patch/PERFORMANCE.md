# Emulation performance: where the throttles are, and what to tune

Analysis of 28 Aug 2026, prompted by the plan to put ZFS on eight emulated disks.

## There is no ZFS code in the patch

Searched `wvg-sd-528.patch` for `zfs`, `spl`, `arc`, `zvol`, `txg`, `dmu`: zero
matches. Whatever "ZFS acceleration patch" may be circulating, it is not in this
one. What the patch does contain is three tunables that decide how fast ZFS will
be able to run on top of it.

## The three knobs

```c
static bool         sd_emulate_512_from_fat_sectors;         /* off by default */
static unsigned int sd_528_emulation_queue_depth = 32;
static unsigned int sd_528_emulation_max_sectors = 2048;     /* 1 MiB */
```

All three are `module_param_named`, so queue depth and request size can be
changed at boot without rebuilding:

```
sd_mod.emulate_512_from_fat_sectors=1
sd_mod.emulate_528_queue_depth=32
sd_mod.emulate_528_max_sectors=2048
```

Hard ceilings that a module parameter cannot exceed:

```c
#define SD_528_MAX_HOST_SECTORS  4096U     /* 2 MiB per request */
#define SD_528_MAX_CHUNK_ORDER   4         /* 64 KiB bounce chunks */
enum { SD_528_MEMPOOL_SIZE = 576,          /* 36 MiB of bounce buffers */
       SD_528_CTX_POOL_SIZE = 64 };        /* concurrent emulations */
```

## The pools are global, the queue depth is per disk

This is the part that matters for an eight-disk pool:

```c
static mempool_t *sd_528_page_pool;   /* one for the whole sd module */
static mempool_t *sd_528_ctx_pool;
```

The arithmetic at the default settings:

```
bounce chunk            65536 B
device sectors/chunk    65536 / 528            = 124
chunks per 1 MiB rq     DIV_ROUND_UP(2048,124) =  17
concurrent max-size rq  576 / 17               =  33
contexts available                                64
```

`sd_528_queue_depth_cap()` computes that 33 and caps the per-disk queue depth by
it. But `sd_528_limit_queue_depth()` then applies the result **to each disk
separately**. With eight disks the block layer is allowed to submit
8 x 32 = 256 concurrent requests against pools that can serve 33 full-size
requests, and 64 emulations of any size.

## Running out is safe, just slow

The obvious worry is what happens when the pool is empty. It is handled
correctly:

```c
page = mempool_alloc(sd_528_page_pool, GFP_ATOMIC);
if (!page)
        return BLK_STS_RESOURCE;
```

`BLK_STS_RESOURCE` tells the block layer to requeue and retry, which is
backpressure, not failure. No `BLK_STS_IOERR`, no data loss. The context
allocation does the same. So pool exhaustion under eight-disk ZFS load costs
throughput and adds latency; it does not corrupt anything.

The one path that does return an error is a request whose bounce scatter-gather
list would exceed the queue's segment limit, and that is bounded in advance by
`sd_528_segments_to_sectors()` feeding the `max_dev_sectors` cap.

## What to change

Ordered by expected effect. Nothing here is measured yet; the numbers are what
the arithmetic predicts.

### 1. Size the pools for the actual disk count (needs a rebuild)

576 chunks is 36 MiB. The machine has 125 GiB of RAM. Raising the reserve costs
memory that is otherwise idle:

| SD_528_MEMPOOL_SIZE | reserve | concurrent 1 MiB requests |
|---|---|---|
| 576 (current) | 36 MiB | 33 |
| 4608 | 288 MiB | 271 |
| 8192 | 512 MiB | 481 |

`SD_528_CTX_POOL_SIZE` has to rise with it, otherwise it becomes the new
ceiling. The context struct is small, so 512 costs almost nothing.

### 2. Trade request size for parallelism (no rebuild)

Fewer chunks per request means more requests fit in the same pool. This is
tunable from the kernel command line, so it is worth measuring before touching
the source:

| emulate_528_max_sectors | request | chunks | concurrent from 576 |
|---|---|---|---|
| 2048 | 1 MiB | 17 | 33 |
| 256 | 128 KiB | 3 | 192 |
| 128 | 64 KiB | 2 | 288 |

128 KiB matches the ZFS default `recordsize`, which makes it the natural first
thing to try for a random-IOPS workload.

### 3. Align ZFS with whatever limit remains

This is the closest thing to the "ZFS acceleration" the patch does not contain.
ZFS aggregates adjacent I/O up to `zfs_vdev_aggregation_limit` (1 MiB by
default). Aggregating past the emulation's request cap only produces requests
that get split again, so the two should match:

```
zfs_vdev_aggregation_limit  <=  emulate_528_max_sectors * 512
```

The vdev queue depths (`zfs_vdev_async_write_max_active` and friends, 10 per
vdev by default) should also stay within what the bounce pool can serve across
all vdevs at once.

## How this gets tested

Measurement has to come before any claim about speed:

1. Baseline: `fio` on a single emulated disk, random 4 KiB and sequential 1 MiB,
   at the default settings.
2. Same across all eight in parallel, which is where the global pool should
   start to bind.
3. Repeat at `emulate_528_max_sectors` of 256 and 128 to see whether trading
   request size for parallelism helps.
4. Only then rebuild with larger pools and repeat.
5. Watch for requeues while testing: rising `BLK_STS_RESOURCE` handling shows up
   as growing latency at stable throughput.

Results and the chosen settings belong in this file once they exist.
