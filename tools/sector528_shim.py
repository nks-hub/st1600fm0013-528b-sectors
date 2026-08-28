"""nbdkit plugin: zpristupni 528bajtovy SAS disk jako standardni 512bajtovy.

Kazdy fyzicky sektor ma 528 B = 512 B dat + 16 B metadat, ktera IBM pouziva
pro svoji ochranu integrity. Linux s takovym blokem neumi pracovat, takze
z kazdeho sektoru bereme prvnich 512 B a zbytek ignorujeme.

Mapovani je 1:1 na sektory, takze logicky blok N sedi na fyzicky sektor N.
Cena je 16/528 = 3,03 % kapacity.

Cteni i zapis jde primo pres SCSI READ(10)/WRITE(10) na /dev/sgN,
protoze blokove zarizeni /dev/sdX ma pri 528 B nulovou velikost.

Spusteni:
    nbdkit -f -v python /cesta/sector528_shim.py device=/dev/sg2
    nbd-client localhost 10809 /dev/nbd0 -b 512
"""
import os, fcntl, struct, ctypes, errno

import nbdkit
import threading

API_VERSION = 2

SG_IO = 0x2285
SG_SET_RESERVED_SIZE = 0x2275
SG_GET_RESERVED_SIZE = 0x2272
SG_DXFER_FROM_DEV = -3
SG_DXFER_TO_DEV = -2
SG_FLAG_DIRECT_IO = 1

PHYS_BS = 528          # skutecna velikost sektoru na disku
LOGICAL_BS = 512       # co ukazujeme ven

# O kolik rezervovaneho bufferu zadame; kernel muze dat min (limituje ho
# max_sectors_kb blokoveho zarizeni), takze skutecnou hodnotu si po nastaveni
# precteme zpatky a MAX_BLOCKS dopocitame az z ni.
RESERVED_WANT = 4 * 1024 * 1024

device = None
nsectors = 0
MAX_BLOCKS = 60        # bezpecny vychozi strop (60*528 = 31,7 kB), prepise se
_tls = threading.local()   # kazde vlakno ma vlastni fd, jinak by se SG_IO praly


class SGIOHdr(ctypes.Structure):
    _fields_ = [
        ("interface_id", ctypes.c_int),
        ("dxfer_direction", ctypes.c_int),
        ("cmd_len", ctypes.c_ubyte),
        ("mx_sb_len", ctypes.c_ubyte),
        ("iovec_count", ctypes.c_ushort),
        ("dxfer_len", ctypes.c_uint),
        ("dxferp", ctypes.c_void_p),
        ("cmdp", ctypes.c_void_p),
        ("sbp", ctypes.c_void_p),
        ("timeout", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("pack_id", ctypes.c_int),
        ("usr_ptr", ctypes.c_void_p),
        ("status", ctypes.c_ubyte),
        ("masked_status", ctypes.c_ubyte),
        ("msg_status", ctypes.c_ubyte),
        ("sb_len_wr", ctypes.c_ubyte),
        ("host_status", ctypes.c_ushort),
        ("driver_status", ctypes.c_ushort),
        ("resid", ctypes.c_int),
        ("duration", ctypes.c_uint),
        ("info", ctypes.c_uint),
    ]


def _get_fd():
    """Vlastni file descriptor pro kazde vlakno + vetsi rezervovany buffer.

    Vychozich 32 kB v sg driveru je pri jednom SG_IO strop, ktery drzi
    propustnost kolem 130 MB/s bez ohledu na to, jak velky pozadavek prijde.
    """
    fd = getattr(_tls, "fd", None)
    if fd is None:
        fd = os.open(device, os.O_RDWR)
        try:
            fcntl.ioctl(fd, SG_SET_RESERVED_SIZE,
                        struct.pack("i", RESERVED_WANT))
        except OSError:
            pass
        _tls.fd = fd
    return fd


def _reserved(fd):
    """Kolik driver skutecne rezervoval - o tolik se smi ptat na jeden prikaz."""
    try:
        return struct.unpack("i", fcntl.ioctl(fd, SG_GET_RESERVED_SIZE,
                                              struct.pack("i", 0)))[0]
    except OSError:
        return 32768


def _scsi(cdb, direction, buf, timeout=30000):
    cmd = (ctypes.c_ubyte * len(cdb))(*cdb)
    sense = (ctypes.c_ubyte * 32)()
    hdr = SGIOHdr()
    hdr.interface_id = ord('S')
    hdr.dxfer_direction = direction
    hdr.cmd_len = len(cdb)
    hdr.mx_sb_len = 32
    hdr.dxfer_len = len(buf)
    hdr.dxferp = ctypes.cast(buf, ctypes.c_void_p)
    hdr.cmdp = ctypes.cast(cmd, ctypes.c_void_p)
    hdr.sbp = ctypes.cast(sense, ctypes.c_void_p)
    hdr.timeout = timeout
    fcntl.ioctl(_get_fd(), SG_IO, hdr)
    if hdr.status != 0:
        sk = sense[2] & 0x0F if hdr.sb_len_wr > 2 else -1
        raise OSError(errno.EIO,
                      "SCSI status 0x%02x, sense key 0x%x" % (hdr.status, sk))
    return hdr


def _read_sectors(lba, count):
    buf = ctypes.create_string_buffer(count * PHYS_BS)
    cdb = [0x28, 0,
           (lba >> 24) & 0xFF, (lba >> 16) & 0xFF, (lba >> 8) & 0xFF, lba & 0xFF,
           0, (count >> 8) & 0xFF, count & 0xFF, 0]
    _scsi(cdb, SG_DXFER_FROM_DEV, buf)
    return buf.raw


def _write_sectors(lba, data):
    count = len(data) // PHYS_BS
    buf = ctypes.create_string_buffer(data, len(data))
    cdb = [0x2A, 0,
           (lba >> 24) & 0xFF, (lba >> 16) & 0xFF, (lba >> 8) & 0xFF, lba & 0xFF,
           0, (count >> 8) & 0xFF, count & 0xFF, 0]
    _scsi(cdb, SG_DXFER_TO_DEV, buf)


def _readcap():
    buf = ctypes.create_string_buffer(8)
    _scsi([0x25, 0, 0, 0, 0, 0, 0, 0, 0, 0], SG_DXFER_FROM_DEV, buf)
    last_lba, bs = struct.unpack(">II", buf.raw)
    return last_lba + 1, bs


# ---- nbdkit callbacks -------------------------------------------------
def config(key, value):
    global device
    if key == "device":
        device = value
    else:
        raise RuntimeError("neznamy parametr: %s" % key)


def thread_model():
    # vychozi je SERIALIZE_ALL_REQUESTS, coz drzi queue depth na jedne
    return nbdkit.THREAD_MODEL_PARALLEL


def config_complete():
    global nsectors
    if device is None:
        raise RuntimeError("chybi parametr device=/dev/sgN")
    nsectors, bs = _readcap()
    if bs != PHYS_BS:
        raise RuntimeError("disk hlasi %d B na sektor, cekal jsem %d" % (bs, PHYS_BS))
    global MAX_BLOCKS
    rsv = _reserved(_get_fd())
    # o vic nez rezervovany buffer se ptat nesmime, jinak SG_IO vrati EIO
    MAX_BLOCKS = max(8, rsv // PHYS_BS)
    print("sector528_shim: %s  %d sektoru x %d B  ->  %d x %d B (%.2f GB)"
          % (device, nsectors, PHYS_BS, nsectors, LOGICAL_BS,
             nsectors * LOGICAL_BS / 1e9), flush=True)
    print("sector528_shim: reserved buffer %d kB, max %d sektoru na prikaz, "
          "thread model PARALLEL" % (rsv // 1024, MAX_BLOCKS), flush=True)


def open(readonly):
    return {"ro": readonly}


def get_size(h):
    return nsectors * LOGICAL_BS


def block_size(h):
    return (LOGICAL_BS, LOGICAL_BS, 32 * 1024 * 1024)


def _strip(raw, n):
    """Z n sektoru po 528 B vytahne prvnich 512 B kazdeho."""
    out = bytearray(n * LOGICAL_BS)
    for i in range(n):
        out[i * LOGICAL_BS:(i + 1) * LOGICAL_BS] = raw[i * PHYS_BS:i * PHYS_BS + LOGICAL_BS]
    return out


def pread(h, count, offset, flags=0):
    out = bytearray()
    while count > 0:
        lba = offset // LOGICAL_BS
        skew = offset % LOGICAL_BS
        n = min(MAX_BLOCKS, (count + skew + LOGICAL_BS - 1) // LOGICAL_BS)
        chunk = _strip(_read_sectors(lba, n), n)
        take = min(count, len(chunk) - skew)
        out += chunk[skew:skew + take]
        offset += take
        count -= take
    return bytes(out)


def pwrite(h, buf, offset, flags=0):
    mv = memoryview(buf)
    pos = 0
    total = len(buf)
    while pos < total:
        lba = (offset + pos) // LOGICAL_BS
        skew = (offset + pos) % LOGICAL_BS
        n = min(MAX_BLOCKS, ((total - pos) + skew + LOGICAL_BS - 1) // LOGICAL_BS)
        take = min(total - pos, n * LOGICAL_BS - skew)

        if skew == 0 and take == n * LOGICAL_BS:
            # zarovnany zapis celych sektoru: metadata prepiseme nulami,
            # cist se predem nemusi
            raw = bytearray(n * PHYS_BS)
            for i in range(n):
                raw[i * PHYS_BS:i * PHYS_BS + LOGICAL_BS] = mv[pos + i * LOGICAL_BS:
                                                               pos + (i + 1) * LOGICAL_BS]
        else:
            # nezarovnany okraj: read-modify-write, at nezahodime cizi data
            raw = bytearray(_read_sectors(lba, n))
            tmp = _strip(raw, n)
            tmp[skew:skew + take] = mv[pos:pos + take]
            for i in range(n):
                raw[i * PHYS_BS:i * PHYS_BS + LOGICAL_BS] = tmp[i * LOGICAL_BS:
                                                                (i + 1) * LOGICAL_BS]

        _write_sectors(lba, bytes(raw))
        pos += take
    return None


def flush(h, flags=0):
    _scsi([0x35, 0, 0, 0, 0, 0, 0, 0, 0, 0], SG_DXFER_FROM_DEV,
          ctypes.create_string_buffer(0))


def can_write(h):
    return not h["ro"]


def can_flush(h):
    return True


def can_multi_conn(h):
    return True
