#include <fcntl.h>
#include <scsi/sg.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <unistd.h>
/* rawrd DEV LBA: READ(10) of one native 528-byte block, raw to stdout */
int main(int c, char **v){ unsigned lba=strtoul(v[2],0,0); unsigned char cdb[10]={0x28,0,lba>>24,lba>>16,lba>>8,lba,0,0,1,0},buf[528],sense[32];
 int fd=open(v[1],O_RDONLY); sg_io_hdr_t h={0}; h.interface_id='S'; h.dxfer_direction=SG_DXFER_FROM_DEV; h.cmd_len=10; h.mx_sb_len=32;
 h.dxfer_len=528; h.dxferp=buf; h.cmdp=cdb; h.sbp=sense; h.timeout=5000;
 if(ioctl(fd,SG_IO,&h)<0||h.status||h.host_status){fprintf(stderr,"sgio fail %d %d\n",h.status,h.host_status);return 1;}
 fwrite(buf,1,528,stdout); return 0;}
