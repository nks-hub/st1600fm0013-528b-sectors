#include <fcntl.h>
#include <scsi/sg.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <unistd.h>
/*
 * rawrd DEV LBA [COUNT]: READ(16) of COUNT (default 1) native 528-byte blocks
 * through SG_IO, raw to stdout, 64 blocks per command. Passthrough commands
 * bypass the emulation, so this is what is physically on the medium,
 * trailer bytes included.
 */
#define BATCH 64
int main(int c, char **v)
{
	unsigned long long lba, count;
	static unsigned char buf[BATCH * 528];
	unsigned char sense[32];
	int fd;

	if (c < 3)
		return 2;
	lba = strtoull(v[2], 0, 0);
	count = c > 3 ? strtoull(v[3], 0, 0) : 1;
	fd = open(v[1], O_RDONLY);
	while (count) {
		unsigned n = count < BATCH ? count : BATCH;
		unsigned char cdb[16] = { 0x88, 0,
			lba >> 56, lba >> 48, lba >> 40, lba >> 32,
			lba >> 24, lba >> 16, lba >> 8, lba,
			n >> 24, n >> 16, n >> 8, n, 0, 0 };
		sg_io_hdr_t h = { 0 };

		h.interface_id = 'S';
		h.dxfer_direction = SG_DXFER_FROM_DEV;
		h.cmd_len = 16;
		h.mx_sb_len = sizeof(sense);
		h.dxfer_len = n * 528;
		h.dxferp = buf;
		h.cmdp = cdb;
		h.sbp = sense;
		h.timeout = 20000;
		if (fd < 0 || ioctl(fd, SG_IO, &h) < 0 || h.status || h.host_status || h.resid) {
			fprintf(stderr, "sgio fail at %llu: status %d host %d resid %d\n",
				lba, h.status, h.host_status, h.resid);
			return 1;
		}
		fwrite(buf, 1, n * 528, stdout);
		lba += n;
		count -= n;
	}
	return 0;
}
