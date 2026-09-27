/* blkops DEV discard|zeroout OFFSET_SECTORS LEN_SECTORS: issue BLKDISCARD or BLKZEROOUT */
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <linux/fs.h>

int main(int argc, char **argv)
{
	uint64_t r[2];
	int fd;

	if (argc != 5)
		return 2;
	fd = open(argv[1], O_RDWR);
	r[0] = strtoull(argv[3], 0, 0) * 512;
	r[1] = strtoull(argv[4], 0, 0) * 512;
	if (fd < 0 || ioctl(fd, strcmp(argv[2], "discard") ? BLKZEROOUT : BLKDISCARD, r)) {
		perror(argv[2]);
		return 1;
	}
	return 0;
}
