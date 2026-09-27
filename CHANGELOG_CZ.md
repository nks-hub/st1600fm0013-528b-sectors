# Changelog a postupy

*English version: [CHANGELOG.md](CHANGELOG.md)*

Změny kernel patche pro 528bajtové disky a postupy, jak ho sestavit, otestovat,
nasadit na existující pool a jak se vrátit. Podrobnosti ke každé opravě, včetně
reprodukce, jsou v [kernel-patch/HARDENING_CZ.md](kernel-patch/HARDENING_CZ.md).

Obsah:

- [Změny](#změny)
- [Postup A: build kernelu](#postup-a-build-kernelu)
- [Postup B: build pro Proxmox](#postup-b-build-pro-proxmox)
- [Postup C: test před nasazením](#postup-c-test-před-nasazením)
- [Postup D: přechod existujícího poolu](#postup-d-přechod-existujícího-poolu)
- [Postup E: návrat ke starému kernelu](#postup-e-návrat-ke-starému-kernelu)
- [Postup F: po přechodu](#postup-f-po-přechodu)

---

## Změny

### 2026-09-27: ověření na kernelech Proxmoxu

Zjištěno z gitu balíčkování kernelu Proxmoxu (`git.proxmox.com/git/pve-kernel.git`):

| řada | poslední verze | větev, datum | základ Ubuntu | upstream |
|---|---|---|---|---|
| **7.0** | **`proxmox-kernel-7.0.14-20-pve`** | `master`, 2026-09-24 | `Ubuntu-7.0.0-39.39` | 7.0.14 |
| 6.17 | `proxmox-kernel-6.17.13-21-pve` | `trixie-6.17`, 2026-07-28 | `Ubuntu-hwe-6.17-6.17.0-42.42` | 6.17.13 |

**Patch sedí na obě řady bez ruční práce.** Postup B (`make_pve_patch.sh`)
proběhne sám a všechny kroky `port_universal.py` najdou své kotvy.

Testy běžely na zdrojácích Ubuntu 38.38, kde je upstream také 7.0.14. Přesný
tag `Ubuntu-7.0.0-39.39` (commit `874594fa`, ten, který Proxmox připíná) jsem
stáhl ze zrcadla Proxmoxu a porovnal: `sd.c` i `sd.h` jsou s 38.38 bajtově
shodné a patch `9999-…` z něj vygenerovaný je obsahově totožný s otestovaným.
Mezi 38.38 a 39.39 se v okolí mění jen dvě věci. Ve `scsi_lib.c` se vynulují
doplňovací bajty při `dma_pad_mask`, což se týká jen ATAPI a proběhne dřív,
než emulace vymění buffer. Ve `scsi_error.c` se jinak čte příznak
power-managementu v EH. Ani jedno se emulace nedotýká.

| kontrola | 7.0.14 (Ubuntu 38.38 / 39.39) | 6.17.13 (Ubuntu 42.42) |
|---|---|---|
| `make_pve_patch.sh`, všechny kroky | OK | OK |
| generace queue limits | `ptr` | `ptr` (Ubuntu převzal změnu ze 7.0) |
| výsledný `sd.c` proti otestovanému 7.0 | liší se jen 2 nesouvisejícími řádky Ubuntu | – |
| build `sd.o` s `W=1` | bez varování | bez varování |
| `basic`, `trim`, `big`, `cdb16`, `eh`, `pool` | OK (T6/T7 jen srovnávací) | `basic`, `trim`, `big` OK |
| `torture` s chybami | 0 špatných, 0 neúspěšných | 0 špatných, 0 neúspěšných (512B iovecs) |

Žádný ze 124 patchů Proxmoxu v řadě 7.0 nesahá na `drivers/scsi/sd.c`,
`sd.h`, `scsi_lib.c` ani na blokovou vrstvu. Jediný, který se týká
`mpt3sas` (0122), mění výpočet cpumasky v `mpt3sas_base.c`, ne datovou cestu.
Náš `9999-…` patch se proto aplikuje přesně na `sd.c` z Ubuntu.

Na 6.17 vrací torture se 64bajtovými iovecs `EINVAL`. To dělá bloková vrstva
6.17, která pro O_DIRECT chce segmenty v násobcích 512 B, ještě než se
požadavek dostane k ovladači. Se ZFS to nesouvisí, ZFS posílá celé stránky.

### 2026-09-27: hardening emulace (větev `claude/clever-dirac-82ozij`)

Výchozí stav: `master` na `b2d3359`, lokálně označený tagem
`pre-zfs-review-2026-09-27`. Commity `99bab03`, `7a87892`, `df20ce5`, `02a492a`,
`4859a32`.

**Formát dat na disku se nemění.** Host LBA N je pořád LBA N na disku, 512
bajtů dat a 16 nulových bajtů. Stejná data zapsaná starou i novou verzí dávají
bajtově shodný surový obraz disku (test `t=xver`).

#### Opraveno: integrita dat

- **Opakování příkazu po resetu nebo UNIT ATTENTION** šlo na disk s 512bajtovým
  bufferem a 528bajtovým CDB. Čtení vracelo cizí data jako úspěch, zápis
  ukládal posunutá data jako úspěch. Bounce buffer teď zůstává nainstalovaný po
  celý život příkazu. (`99bab03`)
- **Zkrácený přenos** (resid při stavu GOOD) se hlásil jako celý a chybějící
  konec pocházel z bounce bufferu. Teď se počítají jen bloky, které disk
  potvrdil. (`99bab03`)
- **Kernel oops**, když disk přešel do offline s příkazy ve frontě. (`99bab03`)
- **`rq->end_io_data`** se uvolňovalo jako emulační kontext u každého
  požadavku, a to je pole, které patří dm-multipath. Kontext se teď hledá přes
  bounce tabulku. (`99bab03`)
- **Rescan za běhu** krátce vypínal emulaci a vypnutí parametru udělalo
  z živého disku 0 bajtů. Příznaky se zapisují jen při změně a emulace drží,
  dokud disk hlásí 528. (`99bab03`)
- **520bajtové disky** se přijímaly bez převodní cesty. Teď se neemulují a
  zůstanou na 0 bajtech jako ve stock ovladači. (`99bab03`)
- **Zamrznutí disku**, když byl bounce pool menší než jeden požadavek.
  Velikost požadavku se teď omezí podle poolu (krok 10c). (`df20ce5`)

#### Opraveno: build

- `port_universal.py` špatně určoval generaci kernelu a 6.8 až 6.17 se
  nepřeložily. Detekce teď hledá jen v `sd_revalidate_disk()`. (`99bab03`)
- `port_universal.py` nepoznal odmítnutý hunk 1. S `--fuzz` skončil strop
  `max_dev_sectors` na 6.8 v komentáři a vytvoření poolů na špatném místě.
  Obojí se teď přesune na správné místo. (`99bab03`)
- Build recept v `RESULTS_CZ.md` a `rebase_pve_528_patch.py` vyrábějí kernel
  bez oprav. Recept je označený jako překonaný, pro Proxmox je nový
  `make_pve_patch.sh`. (`df20ce5`)

#### Změněno: viditelné chování

| co | dřív | teď | dopad |
|---|---|---|---|
| `queue/physical_block_size` | 512 | 4096 (disk hlásí 8 × 528) | jen nápověda; `zpool add` bez `-o ashift` nezvolí 9 |
| `queue/discard_granularity` | 512 | 4096 | TRIM vynechá kousky menší než 4 KiB, u ashift=12 žádný |
| 520bajtové disky s parametrem | „emulované“ bez bounce | neemulované, 0 bajtů, varování | žádný pro 528 |
| vypnutí `emulate_512_from_fat_sectors` za běhu + rescan | disk 0 bajtů | disk zůstane emulovaný | parametr platí pro nově nalezené disky |
| `emulate_528_pool_chunks` menší než jeden požadavek | disk navždy zaseklý | strop požadavku se sníží | žádný při rozumném nastavení |
| RECOVERED ERROR | příkaz se opakoval | úspěch, jako stock `sd` | žádný |
| hostitel s DIX, virt boundary nebo malým `max_segment_size` | nedefinované | EIO | na `mpt3sas` se SAS disky nenastane |

Bootovací parametry i kapacita zůstávají.

#### Přidáno

- `kernel-patch/make_pve_patch.sh`: patch pro `patches/kernel/` Proxmoxu se
  všemi opravami.
- `kernel-patch/verify_upgrade.sh`: důkaz na skutečném stroji, že nový kernel
  čte disky stejně jako starý, jen čtením.
- `kernel-patch/test/`: testovací sada v QEMU se `scsi_debug` (528bajtové
  sektory), scénáře `basic`, `torture`, `trim`, `eh`, `pool`, `big`, `cdb16`,
  `xver`, `xverify`.
- `kernel-patch/HARDENING.md` a `HARDENING_CZ.md`: code review, výsledky testů,
  poznámky k ZFS.

### 2026-08-28: port a první opravy (`b2d3359`)

Stav, na kterém dnes běží pool. `port_universal.py` portuje cizí patch na
nové kernely, zapíná TRIM (UNMAP), opravuje umístění restrikce blokových
operací a stropu hloubky fronty, double free v `init_sd()` a dělá z velikostí
poolů bootovací parametry. Měření v
[kernel-patch/MEASUREMENTS_CZ.md](kernel-patch/MEASUREMENTS_CZ.md).

**Známé vady této verze** jsou všechny body „Opraveno“ výš.

### 2026-08-28: převzetí patche

`wvg-sd-528.patch` a `rebase_pve_528_patch.py` neznámého původu, viz
[kernel-patch/ORIGIN_CZ.md](kernel-patch/ORIGIN_CZ.md).

---

## Postup A: build kernelu

Na čistém stromu (ověřeno pro 6.8, 6.14, 6.17 a 7.0):

```bash
cd linux-7.0
patch -p1 --forward --fuzz=3 < /cesta/kernel-patch/wvg-sd-528.patch
python3 /cesta/kernel-patch/port_universal.py .
```

Výpis skriptu musí obsahovat:

```
  hardening                  bounce table kept until uninit, per-command context
  pool-sized request cap     inserted
  ...
  sd_528_cmd_ctx               present
```

Hlášky `rejects`/`FAILED` od `patch` jsou v pořádku, skript chybějící části
doplní. Když skript skončí chybou „hunk 1 … is not present“, patch neproběhl
s `--fuzz=3`.

Pak obvyklý build se stávající konfigurací:

```bash
cp /boot/config-$(uname -r) .config
make olddefconfig
make -j$(nproc) bzImage modules      # nebo bindeb-pkg pro .deb balíčky
```

## Postup B: build pro Proxmox

`rebase_pve_528_patch.py` **nepoužívat**, nenese žádnou opravu.

```bash
apt install devscripts
git clone https://git.proxmox.com/git/pve-kernel.git     # master = řada 7.0
cd pve-kernel                                            # (trixie-6.17 pro 6.17)
make submodule                                           # čistý submodules/ubuntu-kernel

# žádný patch Proxmoxu nesmí měnit sd.c/sd.h; výstup musí být prázdný
grep -l 'drivers/scsi/sd\.[ch]' patches/kernel/*.patch

/cesta/kernel-patch/make_pve_patch.sh submodules/ubuntu-kernel \
    patches/kernel/9999-wvg-sd-528-translation.patch

make build-dir-fresh
mk-build-deps -ir proxmox-kernel-*/debian/control        # build závislosti
make deb
```

Skript pracuje na kopii `sd.c`/`sd.h`, strom nemění. Když některá oprava chybí,
nic nezapíše. Na konci ověří, že patch jde na strom aplikovat. Build Proxmoxu
aplikuje `patches/kernel/*.patch` v abecedním pořadí přes `patch --batch`,
takže `9999-…` jde poslední a při nesouladu se build zastaví, místo aby
vyrobil špatný kernel. Kdyby `grep` výš něco vypsal, patch vyrob z adresáře
s už aplikovanými patchi Proxmoxu (build-dir po `make build-dir-fresh`)
místo ze submodulu.

## Postup C: test před nasazením

Testuje se testovací build **téhož stromu**, jen s malou konfigurací a
`scsi_debug`, který umí 528bajtové sektory. Nepotřebuje disky ani KVM, stačí
`qemu-system-x86_64`, `busybox-static`, `cpio` a `cc`.

```bash
cp -a linux-7.0 linux-7.0-test && cd linux-7.0-test
patch -p1 < /cesta/kernel-patch/test/scsi_debug-528.patch     # jen do testovacího stromu
# konfigurace: viz hlavička kernel-patch/test/run-qemu.sh (tinyconfig + SCSI_DEBUG, KASAN)
make -j$(nproc) bzImage

T=/cesta/kernel-patch/test
$T/run-qemu.sh arch/x86/boot/bzImage                   # basic
$T/run-qemu.sh arch/x86/boot/bzImage t=torture
$T/run-qemu.sh arch/x86/boot/bzImage "t=trim scsi_debug.lbpu=1 scsi_debug.lbprz=1"
$T/run-qemu.sh arch/x86/boot/bzImage t=eh
```

Očekávání:

- `basic`: všechno OK kromě T6/T7. Ty jsou jen srovnávací, stock `sd` na
  512bajtovém disku dopadne stejně.
- `torture`: `0 bad sectors` v obou řádcích.
- Nikde `KASAN`, `BUG:` ani `Oops`.

**Test přechodu staré → nové verze** (`t=xver`): `sd` a `scsi_debug` jako
moduly (`scripts/config -e MODULES -e MODULE_UNLOAD -m BLK_DEV_SD -m SCSI_DEBUG`),
`sd_mod.ko` přeložený dvakrát proti stejnému jádru. Poprvé se strom připraví
starou verzí skriptu z výchozího stavu
(`git show b2d3359:kernel-patch/port_universal.py > port_old.py`), podruhé
novou:

```bash
mkdir mods
cp drivers/scsi/scsi_debug.ko mods/
# ... sd_mod.ko ze staré přípravy jako mods/sd-old.ko, z nové jako mods/sd-new.ko
MODS=$PWD/mods $T/run-qemu.sh arch/x86/boot/bzImage t=xver
MODS=$PWD/mods $T/run-qemu.sh arch/x86/boot/bzImage t=xverify
```

`X1` až `X8` a `V1` až `V3` musí odpovídat očekávání v závorkách.

## Postup D: přechod existujícího poolu

Pool `tank`, osm disků. Cesty `/dev/disk/by-id/...` doplň podle
`ls -l /dev/disk/by-id/ | grep -v part`.

**1. Na starém kernelu, za provozu (nic nemění):**

```bash
zdb -C tank | grep ashift                    # očekávám 12
zpool status -v tank > /root/pre-zpool-status.txt
cat /proc/cmdline                            # sd_mod.* parametry, nový kernel musí mít stejné
```

**2. Nový kernel nainstalovat a nabootovat jen jednou:**

```bash
proxmox-boot-tool kernel list
proxmox-boot-tool kernel pin <nová-verze> --next-boot
```

Starý kernel zůstává výchozí. Každý další obyčejný restart vrátí starý.

**3. Na starém kernelu: zastavit pool a zabránit automatickému importu:**

```bash
# zastavit VM/CT, které pool používají
pvesm set <storage-id> --disable 1                 # storage plugin by jinak pool importoval
systemctl disable zfs-import@tank.service 2>/dev/null
zpool export tank
```

**4. Snapshot disků (jen čtení):**

```bash
/cesta/kernel-patch/verify_upgrade.sh snapshot /root/pre.txt --sample 16 \
    /dev/disk/by-id/wwn-0x5000c500aaaaaaaa \
    /dev/disk/by-id/wwn-0x5000c500bbbbbbbb   # ... všech osm
```

Bez `--sample` se čte všechno, zhruba hodinu, a je to nejsilnější doklad.
S `--sample 16` to trvá minuty. Skript nejdřív ověří, že nový kernel disky
obslouží, a odmítne běžet, pokud je disk v importovaném poolu.

**5. Restart do nového kernelu:**

```bash
reboot
# po startu:
uname -r
dmesg | grep -c "Emulating 512-byte sectors"      # 8
```

**6. Porovnání (jen čtení):**

```bash
/cesta/kernel-patch/verify_upgrade.sh compare /root/pre.txt
```

- `IDENTICAL`: pokračovat.
- `NOT IDENTICAL`: **nic neimportovat**, `reboot` (vrátí starý kernel) a
  ozvat se s výstupem.

**7. Import jen pro čtení:**

```bash
zpool import -o readonly=on tank
zpool status -v tank            # porovnat s /root/pre-zpool-status.txt
# volitelně přečíst data: ZFS při čtení ověřuje kontrolní součty
zpool export tank
```

**8. Ostrý import, scrub, vrátit krok 3:**

```bash
zpool import tank
zpool scrub tank
pvesm set <storage-id> --disable 0
systemctl enable zfs-import@tank.service 2>/dev/null
zpool status -v tank            # po doběhnutí scrubu: 0 errors
proxmox-boot-tool kernel pin <nová-verze>        # natrvalo, až je scrub čistý
```

## Postup E: návrat ke starému kernelu

Kdykoli, i po ostrém importu a zápisech: formát na disku je v obou směrech
stejný (testy X6 a X7).

```bash
proxmox-boot-tool kernel pin <stará-verze>
reboot
```

Během kroků 5 až 7 postupu D stačí obyčejný `reboot`, pin `--next-boot` platí
jen jednou. Vrátit se na starý kernel znamená vrátit i jeho chyby, tedy
riziko při resetech na SAS.

## Postup F: po přechodu

Zjistit, jestli starý kernel mohl data poškodit, tedy jestli během provozu
nastal reset:

```bash
journalctl -k --list-boots
journalctl -k -b <boot-se-starým-kernelem> | grep -iE "power-on or device reset|unit attention|DID_RESET|reset"
```

Žádné resety během I/O znamenají, že se chyba starého kernelu nikdy
neprojevila. Každopádně rozhoduje scrub z kroku 8:

- `0 errors`: pool je v pořádku.
- Opravené chyby (`repaired`): mirror je spravil z druhé kopie.
- `Permanent errors`: `zpool status -v` vypíše postižené soubory. Ty je potřeba
  obnovit ze zálohy.

Doporučené nastavení (beze změny oproti měření, jen menší pool):

```
sd_mod.emulate_512_from_fat_sectors=1
sd_mod.emulate_528_queue_depth=32
sd_mod.emulate_528_max_sectors=256
sd_mod.emulate_528_pool_chunks=1024      # 4608 funguje taky, jen drží 224 MiB navíc
sd_mod.emulate_528_pool_contexts=512
```
