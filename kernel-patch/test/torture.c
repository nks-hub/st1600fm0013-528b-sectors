/*
 * torture DEV THREADS SECONDS [ALIGN]
 *
 * Multi-threaded O_DIRECT write/read-verify against a block device. Each
 * thread owns a disjoint region and keeps a shadow generation number per
 * 512-byte sector; every sector's contents are derived from (lba, gen), so a
 * read can say exactly which sector is wrong and whether it holds stale,
 * shifted or foreign data. Requests have random offset and length (1 to 1024
 * sectors) and are split into iovecs of random size in multiples of ALIGN
 * bytes (default 64, the SCSI dma_alignment + 1), so 512-byte sectors
 * straddle segment boundaries.
 *
 * A write that fails leaves its sectors undefined; they are skipped until
 * written again. A read that fails is counted, not verified. Exit status is 1
 * if any sector read back wrong.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>
#include <linux/fs.h>

#define SEC 512
#define MAXSEC 1024
#define UNDEF 0xffffffffu

static const char *dev;
static unsigned nthreads, align = 64;
static uint64_t region;			/* sectors per thread */
static volatile int stop;
static unsigned long long n_wr, n_rd, n_wr_err, n_rd_err, n_bad;
static pthread_mutex_t lk = PTHREAD_MUTEX_INITIALIZER;

static uint64_t mix(uint64_t x)
{
	x ^= x >> 33; x *= 0xff51afd7ed558ccdULL;
	x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL;
	return x ^ (x >> 33);
}

static void fill(uint8_t *p, uint64_t lba, uint32_t gen)
{
	uint64_t *q = (uint64_t *)p;
	q[0] = lba; q[1] = gen;
	for (int i = 2; i < SEC / 8; i++)
		q[i] = mix(lba * 1000003 + gen * 7919 + i);
}

/* what is in this sector: 0 = as expected, else a short diagnosis */
static const char *judge(const uint8_t *p, uint64_t lba, uint32_t gen)
{
	static __thread uint8_t want[SEC];
	const uint64_t *q = (const uint64_t *)p;

	fill(want, lba, gen);
	if (!memcmp(p, want, SEC))
		return NULL;
	if (q[0] == lba)
		return "stale generation";
	if (q[0] < (uint64_t)nthreads * region + 16)
		return "another sector's data";
	return "garbage/shifted";
}

static void split_iov(struct iovec *iov, int *niov, uint8_t *buf, size_t len,
		      unsigned *seed)
{
	size_t off = 0;
	int n = 0;

	while (off < len) {
		size_t chunk = align * (1 + rand_r(seed) % (2 * SEC / align + 3));
		if (rand_r(seed) % 4 == 0)
			chunk = 4096;
		if (chunk > len - off || n == 63)
			chunk = len - off;
		iov[n].iov_base = buf + off;
		iov[n].iov_len = chunk;
		off += chunk;
		n++;
	}
	*niov = n;
}

static void *worker(void *arg)
{
	unsigned id = (unsigned)(uintptr_t)arg, seed = id * 7777 + time(NULL);
	uint64_t base = (uint64_t)id * region;
	uint32_t *gen = calloc(region, sizeof(*gen));
	uint8_t *buf;
	struct iovec iov[64];
	int fd = open(dev, O_RDWR | O_DIRECT), niov;
	unsigned long long wr = 0, rd = 0, we = 0, re = 0, bad = 0;

	if (fd < 0 || posix_memalign((void **)&buf, 4096, MAXSEC * SEC)) {
		perror("open/alloc");
		exit(2);
	}
	/* first pass: define every sector */
	for (uint64_t s = 0; s < region; s += MAXSEC) {
		uint64_t n = region - s < MAXSEC ? region - s : MAXSEC;
		for (uint64_t i = 0; i < n; i++) {
			gen[s + i] = 1;
			fill(buf + i * SEC, base + s + i, 1);
		}
		if (pwrite(fd, buf, n * SEC, (base + s) * SEC) != (ssize_t)(n * SEC)) {
			for (uint64_t i = 0; i < n; i++)
				gen[s + i] = UNDEF;
			we++;
		}
	}
	while (!stop) {
		uint64_t n = 1 + rand_r(&seed) % MAXSEC, s;
		int write_op = rand_r(&seed) % 10 < 6;
		ssize_t r;

		if (rand_r(&seed) % 3 == 0)
			n = 1 + rand_r(&seed) % 8;
		if (n > region)
			n = region;
		s = rand_r(&seed) % (region - n + 1);
		split_iov(iov, &niov, buf, n * SEC, &seed);
		if (write_op) {
			for (uint64_t i = 0; i < n; i++) {
				uint32_t g = gen[s + i] == UNDEF ? 2 : gen[s + i] + 1;
				fill(buf + i * SEC, base + s + i, g);
			}
			r = pwritev(fd, iov, niov, (base + s) * SEC);
			for (uint64_t i = 0; i < n; i++)
				gen[s + i] = r == (ssize_t)(n * SEC) ?
					((uint64_t *)(buf + i * SEC))[1] : UNDEF;
			if (r != (ssize_t)(n * SEC))
				we++;
			wr++;
		} else {
			memset(buf, 0xa5, n * SEC);
			r = preadv(fd, iov, niov, (base + s) * SEC);
			rd++;
			if (r != (ssize_t)(n * SEC)) {
				re++;
				continue;
			}
			for (uint64_t i = 0; i < n; i++) {
				const char *why;
				if (gen[s + i] == UNDEF)
					continue;
				why = judge(buf + i * SEC, base + s + i, gen[s + i]);
				if (why) {
					if (bad < 5)
						fprintf(stderr, "BAD t%u lba %llu gen %u: %s (req %llu+%llu, %d iov)\n",
							id, (unsigned long long)(base + s + i), gen[s + i], why,
							(unsigned long long)(base + s), (unsigned long long)n, niov);
					bad++;
				}
			}
		}
	}
	/* final full verify */
	for (uint64_t s = 0; s < region; s += MAXSEC) {
		uint64_t n = region - s < MAXSEC ? region - s : MAXSEC;
		if (pread(fd, buf, n * SEC, (base + s) * SEC) != (ssize_t)(n * SEC)) {
			re++;
			continue;
		}
		for (uint64_t i = 0; i < n; i++) {
			const char *why;
			if (gen[s + i] == UNDEF)
				continue;
			why = judge(buf + i * SEC, base + s + i, gen[s + i]);
			if (why) {
				if (bad < 5)
					fprintf(stderr, "BAD(final) t%u lba %llu: %s\n", id,
						(unsigned long long)(base + s + i), why);
				bad++;
			}
		}
	}
	pthread_mutex_lock(&lk);
	n_wr += wr; n_rd += rd; n_wr_err += we; n_rd_err += re; n_bad += bad;
	pthread_mutex_unlock(&lk);
	close(fd);
	return NULL;
}

int main(int argc, char **argv)
{
	pthread_t th[64];
	uint64_t bytes;
	int fd;

	if (argc < 4) {
		fprintf(stderr, "usage: torture DEV THREADS SECONDS [ALIGN]\n");
		return 2;
	}
	dev = argv[1];
	nthreads = atoi(argv[2]);
	if (argc > 4)
		align = atoi(argv[4]);
	fd = open(dev, O_RDONLY);
	if (fd < 0 || ioctl(fd, BLKGETSIZE64, &bytes)) {
		perror(dev);
		return 2;
	}
	close(fd);
	region = bytes / SEC / nthreads;
	for (unsigned i = 0; i < nthreads; i++)
		pthread_create(&th[i], NULL, worker, (void *)(uintptr_t)i);
	sleep(atoi(argv[3]));
	stop = 1;
	for (unsigned i = 0; i < nthreads; i++)
		pthread_join(th[i], NULL);
	printf("torture: %llu writes (%llu failed), %llu reads (%llu failed), %llu bad sectors\n",
	       n_wr, n_wr_err, n_rd, n_rd_err, n_bad);
	return n_bad ? 1 : 0;
}
