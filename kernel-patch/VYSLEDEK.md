# Kernel patch funguje — 528B disk jako nativní 512B

Datum: 28. 8. 2026

## Závěr

Patch `wvg-sd-528`, doportovaný na kernel 6.8, **funguje**. Disk se 528bajtovými
sektory se v systému objeví jako standardní 512bajtové blokové zařízení. Ověřeno
zápisem i čtením včetně křížové validace proti nativnímu 528B přístupu.

Cena je 16 bajtů z každého sektoru: **1,55 TB místo 1,60 TB** na disk, tedy
zhruba 388 GB z celkových 12,8 TB.

**Rychlost linku to neřeší.** Limit 6 Gb/s sedí ve firmwaru disku a kernel s ním
nic nezmůže.

## Jak se to testovalo bez restartu

Hostitel se restartovat nesměl, takže se testovalo v QEMU se SCSI passthrough —
virtuálka dostane přímý přístup k fyzickému disku, hostitel běží nedotčený:

```bash
qemu-system-x86_64 -enable-kvm -m 2048 -smp 2 -nographic -no-reboot \
  -kernel /usr/src/k/linux-6.8/arch/x86/boot/bzImage \
  -initrd /tmp/qinitrd.gz \
  -append "console=ttyS0 sd_mod.emulate_512_from_fat_sectors=1" \
  -device virtio-scsi-pci,id=scsi0 \
  -drive file=/dev/sg2,if=none,id=d0,format=raw \
  -device scsi-generic,drive=d0,bus=scsi0.0
```

Podstatné je `scsi-generic` — QEMU ho popisuje jako *„pass through generic scsi
device (/dev/sg*)"*. Initramfs stačí postavit z busyboxu.

## Výsledky

### Kontrolní běh proti ostrému

```
emulate_512_from_fat_sectors=0
    sda: sektoru=0        physical_bs=4224     dd → 0 records

emulate_512_from_fat_sectors=1
    sd 0:0:0:0: [sda] Attached SCSI disk
    sda: sektoru=3030911576  logical_bs=512  physical_bs=512  →  1445 GB
    dd → 1+0 records in/out
```

### Integrita dat

| Test | Výsledek |
|---|---|
| Zarovnaný zápis 32 kB na LBA 1000 | md5 shoda |
| **Nezarovnaný** zápis 17 sektorů na LBA 2049 | shoda |
| 1 MB **přes hranici 64kB chunku** na LBA 10240 | md5 shoda |

### Křížová validace

Nejsilnější důkaz. Data zapsaná přes emulované 512B zařízení přečtena
z hostitele nativně jako 528bajtový sektor:

```
sg_raw -r 528 -o out.bin /dev/sg2 28 00 00 00 03 e8 00 00 01 00

emulace v QEMU:    3b 0f a4 56 af f2 0e 24 c8 c9 87 4b a2 e2 6f 29
nativně na disku:  3b 0f a4 56 af f2 0e 24 c8 c9 87 4b a2 e2 6f 29   ✓
                   25 24 02 a8 c3 21 4d d3 96 c4 7f 4b b9 50 4a 53   ✓
metadata[512:528]: samé nuly
```

Data jsou fyzicky uložená na správném místě ve správných sektorech, 16 bajtů
metadat vynulováno. Disk po všech testech: `SMART Health OK`, nula vadných
sektorů, block size beze změny 528 B.

## Build recept

```bash
# zdroj
curl -O https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.8.tar.xz
tar xf linux-6.8.tar.xz && cd linux-6.8

# patch
patch -p1 --forward < wvg-sd-528.patch          # 10/13 hunků projde
python3 apply_manual_hunks.py $PWD              # doaplikuje 3 odmítnuté
python3 port_to_68.py $PWD                      # přeloží na 6.8 API

# config
cp /boot/config-$(uname -r) .config
scripts/config --disable MODULE_SIG
scripts/config --disable SYSTEM_TRUSTED_KEYS
scripts/config --disable SYSTEM_REVOCATION_KEYS
scripts/config --disable DEBUG_INFO_BTF
# klíčové ovladače napevno, aby stačila původní initrd
for c in SCSI_MPT3SAS OVERLAY_FS IGB BLK_DEV_NBD; do
    scripts/config --set-val CONFIG_$c y
done
make olddefconfig
make -j$(nproc) bzImage modules
```

Hotový `arch/x86/boot/bzImage` má 13 492 736 B.

## Co bylo potřeba opravit v patchi

Patch cílil na strom, kde se limity fronty předávají jako
`struct queue_limits *lim`. To v upstream neexistuje — ověřeno pro 6.8 až 6.14.

| Místo | Oprava |
|---|---|
| `sd_disable_advanced_block_ops()` | brala `lim`, sahala na `lim->max_discard_sectors` → přepsáno na `blk_queue_max_discard_sectors(q, 0)` a `blk_queue_max_write_zeroes_sectors(q, 0)`, upraveno volající místo |
| Hunk 9 | volání `sd_528_limit_queue_depth()` přesunuto před `buffer = kmalloc(SD_BUF_SIZE, …)` |
| Hunk 10 | `lim->max_dev_sectors` → `q->limits.max_dev_sectors`, `lim->max_segments` → `q->limits.max_segments` |

Past, na kterou jsem dvakrát naletěl: kontrola typu
`if "sd_528_effective_max_sectors" in text` je falešně pozitivní — ten symbol
přijde už s hunkem 1, takže se hunk 10 tvářil jako hotový, i když nebyl.
Správně se kontroluje `emu_cap`.

## Nasazení na fyzický stroj

Server bootuje PXE z netbootxyz (LXC 117 na PVE, netboot-host, nginx nad
`/var/www/html`). Vzor menu položky je v `live-ubuntu.ipxe`:

```
kernel ${kernel_url}vmlinuz ip=dhcp boot=casper netboot=url url=${squash_url} \
       initrd=initrd.magic ${cmdline}
initrd ${kernel_url}initrd
```

Stačí nahrát `bzImage` a přidat položku s `sd_mod.emulate_512_from_fat_sectors=1`
v cmdline. Protože je to liveboot, neúspěšný boot vyřeší prostý restart — nic se
nemůže trvale rozbít.

## Než na to půjdou ostrá data

Testy výše prošly, ale byly krátké. Před produkčním nasazením:

- `fio` s verifikací přes několik hodin,
- `badblocks -w` na celý disk,
- test s filesystémem (mkfs, zápis, umount, fsck),
- ověřit chování při výpadku napájení uprostřed zápisu.

Emulace zahazuje 16 bajtů metadat, které si IBM firmware používá pro vlastní
kontrolu integrity. Disk je nadále nebude vidět konzistentní — pro nás to nevadí
(používáme ho jako obyčejný blokový disk), ale v IBM poli by ten disk už neměl
co dělat.
