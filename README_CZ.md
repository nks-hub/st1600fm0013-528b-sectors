# IBM ST1600FM0013 — firmware, dumpy a co se o zámku ví

Archiv k pokusu odemknout IBM-branded Seagate SAS SSD, které jsou natvrdo zamčené
na 528bajtové sektory a 6 Gb/s link.

**Stav: 512 B VYŘEŠENO** kernel patchem — disk se v systému objeví jako nativní
512bajtové blokové zařízení. Ověřeno v QEMU včetně křížové validace, podrobnosti
v [`kernel-patch/RESULTS_CZ.md`](kernel-patch/RESULTS_CZ.md).

**12 Gb/s zůstává nevyřešené** — ten limit je ve firmwaru disku. Jediná známá
cesta je PC-3000 SAS s funkcí „odemknout mikroprogram", která umí nahrát
firmware jiného vendora.

---

## Naše disky

| | |
|---|---|
| Model | ST1600FM0013 (Seagate Nytro 1200.2, kódové jméno **Koho**) |
| Hlásí se jako | `IBM-SSG` / `IBM-SSGSSVJ1P6` / firmware `6214` |
| IBM FRU | 02AM752, EC M09099 |
| Part number | `1NT2L2-039` (základ `1NT2L2` = Seagate SED Mainstream Endurance) |
| Kapacita | 1,6 TB, 3 030 911 576 × 528 B |
| Datum výroby | říjen–listopad 2018 |
| Kusů | 8, všechny zdravé (SMART OK, 0 vadných sektorů, 0–5 % opotřebení) |
| Server | HPE CL2100 Gen10, HBA Broadcom SAS3216 v IT módu |

---

## Obsah adresáře

```
dumps/
  ibm_ST1600FM0013_6214.bin       4 MB   dump SPI flash z IBM disku (NÁŠ MODEL)
  seagate_ST200FM0133_0007.bin    4 MB   dump z pravého Seagate Koho disku, fw 0007
  seagate_ST200FM0133_000A.bin    4 MB   totéž po upgradu na fw 000A

official-lod/
  12002SSD-Koho-SAS-0004.zip             oficiální Seagate balíček

reformat-528-512_CZ.md                   co projde a co ne pri reformatu na 512 B
linkrate-6g-analysis_CZ.md               proč disky jedou 6 Gb/s a co s tím udělá jiný backplane

hba/                                     řadič SAS3216 „9305-16i“: upgrade na P16.12
  backup_fw_15.00.00.00.bin              záloha původního firmwaru karty (nenahraditelná)
  backup_bios_08.35.00.00.rom            záloha option ROM
  my_clone_P16.bin                       nahraný obraz P16.12 pro SAS3216 klon
  (sas3flash, sas3ircu ani backup_mpb.bin v repu záměrně NEJSOU)
  README_CZ.md                           identifikace karty, postup, obnova
  firmware/KohoSSD-SED-0004.LOD          SED varianta — jmenuje ST1600FM0013
  firmware/KohoSSD-STD-0004.LOD          standard (base)
  firmware/KohoSSD-FIPS-0004.LOD         FIPS 140-2
  linux cli tools/seaflashlin/           oficiální Seagate flasher pro Linux
  READMEFIRST-...pdf                     instrukce + seznam podporovaných modelů

tools/
  hdd_firmware_tools/                    parser LOD souborů, větev ibm-wip
  patch_ibm_lock.py                      patcher zámku (7 bajtů + checksum)
  diff_config_records.py                 rozbor TLV záznamů, IBM vs Seagate
  classify_regions.py                    klasifikace oblastí flash
  map_lod_to_flash.py                    mapování LOD na dump
  dump_config_block.py                   výpis konfiguračního bloku
  sector528_shim.py                      nbdkit plugin 528 -> 512 (userspace)

kernel-patch/
  wvg-sd-528.patch                       cizí patch sd driveru, PŮVOD NEZNÁMÝ
  rebase_pve_528_patch.py                jeho rebase nástroj
  apply_manual_hunks.py                  můj pokus doaplikovat odmítnuté hunky
  ORIGIN_CZ.md                           co o patchi víme
  VERIFICATION_CZ.md                     proč ho zatím nelze použít
  RESULTS_CZ.md                          výsledky testů
```

> **`kernel-patch/`** — patch původně cílil na API, které v upstream neexistuje
> (viz `VERIFICATION_CZ.md`). Po doportování na 6.8 se přeložil a **funguje** — výsledky
> testů v `RESULTS_CZ.md`.

### Kontrolní součty

```
ibm_ST1600FM0013_6214.bin
  sha256 f674540cf92f5ef5b6a1e042cdc1ed95926d05ae212c02910e50855e19a85f86
  md5    0eaece0f023f226c6b7b4796c50a5ce4

seagate_ST200FM0133_0007.bin
  sha256 86cab26df95e0f7cbb51f97f9b5f094ec678859078faa8642828fa9f55dba7a9
  md5    894f33a21acd7bfad5f165ee034289cf

seagate_ST200FM0133_000A.bin
  sha256 e160963cb8945c7631831328262327ac560fd03a2c2fc8a4c5bad7f2d7cff2e1
  md5    44e5c02cc389e571c733c8554991ac92

12002SSD-Koho-SAS-0004.zip
  sha256 adc2abf82352ccf4a63ec4c07ade161bcc18cabf55ce8288b699834e8ea64472
```

---

## Odkud to je

**Dumpy SPI flash** pochází z vlákna
[STH 4968, strana 28](https://forums.servethehome.com/index.php?threads/how-to-reformat-hdd-ssd-to-512b-sector-size.4968/page-28):

- `ibm_ST1600FM0013_6214.bin` — uživatel **Arslan109** (25. 9. 2025). Má stejný model jako my.
  Rozebral disk, odpájel SPI čip horkovzdušnou pistolí a nechal ho vyčíst programátorem.
  Původní jméno `ST1600FM0013.BIN`.
  [Google Drive](https://drive.google.com/file/d/1B8Z8wUzLMtlhvr3H_b23Bqouj3ITkssJ/view)

- `seagate_ST200FM0133_*.bin` — uživatel **leromarinvit** (15. 11. 2025). Koupil nejlevnější
  Koho disk s pravým Seagate firmwarem (ST200FM0133, 200 GB), dumpnul ho ve verzi 0007,
  upgradoval na 000A a dumpnul znovu. Oba dumpy ověřil dvojím čtením.
  [0007](https://drive.google.com/file/d/1-AObmSgD2IyKm1C-16PRllbjSrCIhljv/view) ·
  [000A](https://drive.google.com/file/d/1u4K-T84_KGIM-bj80xM7GcKl1OGgVWK9/view)

**Oficiální LOD balíček** z [touslesdrivers.com](https://www.touslesdrivers.com/index.php?v_page=23&v_code=48362),
klasifikovaný jako „Official". Seagate ho vydal 18. 2. 2016, veřejně už ho nedistribuuje.

**Parser** — [eurecom-s3/hdd_firmware_tools](https://github.com/eurecom-s3/hdd_firmware_tools),
zde ve [forku od leromarinvita, větev `ibm-wip`](https://github.com/leromarinvit/hdd_firmware_tools/tree/ibm-wip).
Poslední commit: *„WIP: add artifact types found in Koho LOD"*.

---

## Co dumpy obsahují

Všechny tři mají **přesně 4 194 304 B**, tedy plnou kapacitu čipu W25Q32 (32 Mbit).
Využito je jen kolem 10 % — zbytek je `0x00` a `0xFF` výplň, entropie kolem 1,3.

| Dump | ST model uvnitř | IBM řetězce | Zmínky Seagate |
|---|---|---|---|
| IBM 6214 | `ST1600FM0013` | `IBM-SSG`, `IBM-SSGSS`, `ZAL`, `SSVJ` | 0 |
| Seagate 0007 | `ST200FM0133` | žádné | 2 |
| Seagate 000A | `ST200FM0133` | žádné | 2 |

**Prvních 32 bajtů je u všech tří identických** (`f3 00 00 00 30 00 00 00 00 40 01 00 …`),
takže jde o stejný formát.

Nejzajímavější je míra podobnosti:

```
IBM 6214    vs Seagate 000A   →  89,7 % shodných bajtů
Seagate 0007 vs Seagate 000A  →  80,0 % shodných bajtů
```

IBM firmware je tedy Seagate verzi **podobnější, než jsou si dvě verze Seagate firmwaru
navzájem**. Naznačuje to, že IBM 6214 vychází z větve blízké 000A a rozdíly jsou
soustředěné do konfigurace, ne do jádra.

---

## Co ten zámek dělá

Firmware `6214` blokuje tři věci **jedním vendor-specific kódem** `ASC 0x26 / ASCQ 0x99`:

1. **Velikost sektoru.** MODE SELECT přijme jedinou hodnotu:
   ```
   512  → Illegal Request        520  → Illegal Request
   524  → Illegal Request        528  → Good          ← jediná přijatá
   4096 → Illegal Request
   ```
   Platí i pro LONGLBA variantu (24bajtový parameter list) a pro FORMAT UNIT
   přes openSeaChest. Samotný `FORMAT UNIT` na stávající velikost přitom projde,
   takže blokovaná je výhradně *změna*.

2. **Rychlost linku.** Všech 8 disků hlásí `desc[33] = 0xaa` (programmed i hardware
   max 6 Gb/s), a to i v továrních default hodnotách — přestože HBA nabízí 12 Gb/s
   a štítek i Seagate manuál uvádějí 12 Gb/s. Pokus přepsat na `0xba` skončí na
   stejném `ASC 0x26 / ASCQ 0x99`.

3. **Výměnu firmwaru.** Crossflash oficiálním `seaflashlin` s pravým podepsaným
   `KohoSSD-SED-0004.LOD` končí po 22 segmentech na `sense_key=0x05`.
   Zajímavé je, že STD image je odmítnut *okamžitě*, zatímco SED se dostane dál —
   disk tedy uznal typ souboru a odmítl ho až na kontrole customer status.

Seagate manuál (100773817 Rev. D, sekce 6.7 „Authenticated firmware download")
uvádí tři podmínky, které musí image splnit. Ta třetí je konec cesty:

> the download file must pass the acceptance criteria for the drive. For example it
> must be applicable to the correct drive model, and have compatible revision and
> **customer status**.

---

## Co bylo vyzkoušeno a nefunguje

Vše ověřeno na našem disku (náš kus), disk je po všech pokusech nepoškozený.

| Cesta | Výsledek |
|---|---|
| `sg_format --size=512` (i `--six`) | Invalid field in parameter list, byte 13 bit 7 |
| `sg_raw` MODE SELECT, short LBA, 3 varianty num_blocks | totéž |
| `sg_raw` MODE SELECT, **LONGLBA** (24 B param list) | totéž — 528 projde, 512 ne |
| `openSeaChest --setSectorSize 512` | „not supported on this device" |
| `openSeaChest --formatUnit` 512 / 520 / 524 / 4096 | „Format Unit Failed!" |
| `seaflashlin -f SED-0004.LOD` (i `-u`, `-w`) | sense 0x05 po 22 segmentech |
| `sg_write_buffer` mode 5 i mode 7 | ASC 0x26 / ASCQ 0x99 |
| TCG PSID revert (ruční stack nad `sg_raw`) | session vrstva nereaguje — viz níže |
| `sedutil-cli` | `Invalid or unsupported disk` (umí jen SATA/NVMe, ne SAS) |

### K TCG a PSID

Level 0 Discovery **funguje**, ale je potřeba správný CDB — alokační délka se udává
v blocích, ne v bajtech:

```
sg_raw -r 512 -o out.bin /dev/sgN a2 01 00 01 80 00 00 00 00 01 00 00
                                              ^^ INC_512=1      ^^ 1 blok
```

Vrátí: Opal SSC v1.00, Base ComID `0x07FE`, Locking `Supported=1 Enabled=1 Locked=0`,
MediaEncryption=1, block size 528.

Skutečná TCG session ale nefunguje. `SECURITY PROTOCOL OUT` vrací `Good`,
`SECURITY PROTOCOL IN` vždy prázdný payload. Rozhodující test: poslal jsem 512 B
čistého nesmyslu (`0xdeadbeef` dokola) na session ComID a disk odpověděl `Good` —
neplatný packet musí skončit chybou, takže disk pakety **přijímá a zahazuje**.

Na výsledku to ale nic nemění: uživatel *sick1655* na STH testoval PSID revert
na disku, kde mu sedutil normálně funguje, a hlásí, že *„sg_format po čerstvém
PSID resetu skončí na stejném Invalid field in parameter list"*. **PSID revert
velikost sektoru neodemkne.**

PSID disků je vytištěný na etiketě, ve dvou řádcích po 16 znacích.
Je to bezpečnostní credential pro factory reset SED disku — nikam ho nekopíruj.

---

## Kde přesně zámek sedí (vlastní analýza, 28. 8. 2026)

Tohle komunita na STH neměla — leromarinvit hledal mapování LOD na flash a nedostal
se sem. Skripty k reprodukci jsou v `tools/`.

### Mapování LOD → flash sedí

Parser `seagate_fw_extract.py` na `KohoSSD-SED-0004.LOD` vypíše strukturu:

```
Artifact 3  type 0x22   0x9f000 B   Flash address = 0xfa0   ← hlavní kód
Artifact 5  type 0x9026 0xd0028 B   Flash address = 0x30
Artifact 9  type 0x1a   0x180 B     ← podpis (384 B)
```

Obsah Artifactu 3 se v dumpech skutečně našel:

```
seagate_000A → posun 0xe0fcc
ibm_6214     → posun 0xe0fcc      ← STEJNÝ layout
seagate_0007 → posun 0x10fcc      (starší verze, jiný layout)
```

### Konfigurace disku je čitelná data

Na `0x0e1140` sedí obsah INQUIRY odpovědi uložený prostě jako text:

```
IBM      9f 00 10 02  "IBM-SSG IBM-SSGSSVJ1P6  6214"  "ZAL15M5Q  216214"
Seagate  8b 01 10 02  "SEAGATE ST200FM0133     000A"  "ZAJ15QQ0"
```

Stejná struktura, jiný obsah.

### Jádro zámku: 688 bajtů, které Seagate nemá vůbec

Oblasti, kde má IBM data a Seagate čistou flash (`0xFF`):

```
0x0e1590 – 0x0e1820   656 B   ← hlavní blok
0x0e1c70 – 0x0e1c80    16 B
0x0e1f40 – 0x0e1f50    16 B
                      688 B celkem
```

Jsou to pojmenované konfigurační záznamy ve tvaru
`[id:1][len:2][00 00][id:1][len-4:2][namelen:1][name][data]`. IBM přidal čtyři:

| Offset | ID | Délka | Jméno |
|---|---|---|---|
| 0x0e15b8 | 0xc4 | 0x28 | (mezery) |
| 0x0e15e4 | 0xc7 | 0xa0 | `SCDD` |
| **0x0e1688** | **0xc8** | **0xd8** | **`AIX      `** ← zámek |
| 0x0e1760 | 0xc9 | 0xac | `AIX      ` |

Záznam `0xc8` nese block descriptor jako holá data:

```
0e1690  09 41 49 58 20 20 20 20 20 20 00 00 00 08 b4 a8
0e16a0  0a 58 00 00 02 10 …
        "AIX      "         b4a80a58      000210
                            3030911576    528
```

### Existuje i index záznamů

```
IBM      … c0 c1 c3 [c4 c7 c8 c9] d1 d2 00     21 položek
Seagate  … c0 c1 c3               d1 d2 00     17 položek
```

IBM má v katalogu navíc přesně ta čtyři ID.

### Hlavička konfiguračního bloku

```
0x0e1130   79 71 6e 49 | f0 06 | 79 9a | ff 00 a4 00 00 00 06 32    IBM
0x0e1130   79 71 6e 49 | 60 04 | ab 93 | ff 00 90 00 00 00 06 12    Seagate
           magic "yqnI"  délka   ?
                         1776 B
                         1120 B
```

Rozdíl délek je **656 bajtů — přesně velikost hlavního IBM-only bloku**. To potvrzuje,
že pole `f0 06` je délka konfigurační oblasti.

### Checksum — rozluštěn

Pole `79 9a` / `ab 93` je kontrolní součet. Algoritmus pochází z rozboru LOD formátu
na [hddguru](https://forum.hddguru.com/viewtopic.php?f=13&t=28252), kde je REXX funkce
`GETSUMM`:

> Sečti 16bitová **little-endian** slova přes celý blok **včetně pole checksumu**.
> Výsledek musí být nula.

Ověřeno na našich datech, sedí všude:

```
IBM konfig blok  @0x0e1130, délka 0x06f0  →  součet 0x0000  OK
SGA konfig blok  @0x0e1130, délka 0x0460  →  součet 0x0000  OK
všechny LOD hlavičky                      →  součet 0x0000  OK
```

### Patch: stačí sedm bajtů

`tools/patch_ibm_lock.py` najde magic `yqnI`, přepíše block descriptor v AIX záznamu
a dopočítá checksum:

```
python tools/patch_ibm_lock.py vlastni_dump.bin vystup.bin        --mode blocksize --blocks 3125627568
```

Změní se sedm bajtů:

```
0x0e1136-0x0e1137   checksum        0x9a79 -> 0xad33
0x0e169e-0x0e16a1   počet bloků     b4a80a58 -> ba47d3b0   (3 125 627 568)
0x0e16a5            velikost sekt.  0x10 -> 0x00           (528 -> 512)
```

Kapacita zůstává 1600,32 GB, přesně podle Seagate tabulky. Demonstrační výstupy
jsou v `patched/` — vygenerované z cizího dumpu, slouží jen k ověření nástroje.

### Dump musí být z vlastního disku

Konfigurační blok obsahuje **sériové číslo a kalibraci konkrétního kusu**.
Přiložený `ibm_ST1600FM0013_6214.bin` je od uživatele Arslan109 (SN `ZAL15M5Q`).
Nahrát ho na náš disk by znamenalo přepsat jeho identitu cizí.

Správný postup:

1. odpájet `W25Q32FWZEIG` z vlastního disku
2. vyčíst dump programátorem (CH341A)
3. `patch_ibm_lock.py` na **vlastním** dumpu
4. zapsat zpět

### Co z toho plyne

**Zámek není v kódu firmwaru, ale v datovém konfiguračním záznamu**, a kontrolní součet
k němu umíme dopočítat. Zbývá jediná neznámá: jestli firmware ověřuje konfigurační blok
kromě checksumu ještě něčím dalším. To se dá zjistit jen zápisem.

---

## Otevřená cesta: přeprogramovat SPI flash

Jediné, co komunita ještě neuzavřela.

**Čip:** `W25Q32FWZEIG`, pouzdro WSON-8 8×6 mm, na PCB poblíž SAS konektoru.
Podle Arslana jde odpájet horkovzdušnou pistolí za třicet sekund. SOIC klipsy
oba účastníci nedoporučují — lepší je čip sundat a použít socket adaptér.
Programátor typu CH341A stačí.

**Co chybí:** pochopit formát LOD, aby šlo z oficiálního `KohoSSD-SED-0004.LOD`
sestavit obraz flash. leromarinvit na tom pracuje ve větvi `ibm-wip`, ale
mapování LOD na fyzický layout zatím nemá.

**Zkratka, kterou nikdo nezkusil:** nahrát Seagate dump z `ST200FM0133` přímo
na IBM disk. leromarinvit to sám navrhuje slovy *„with some luck, blindly flashing
the Seagate dump to the IBM-branded drive might just work"*. Háček je jiná kapacita —
200 GB vs 1,6 TB — takže konfigurace NAND se skoro jistě neshoduje. Rozumnější
je nejdřív porovnat, které oblasti se mezi IBM a Seagate dumpem liší (10,3 %),
a přenést jen konfigurační části.

Data k tomu jsou v tomto adresáři kompletní: máme oficiální LOD, dump z IBM disku
našeho modelu i dumpy z pravého Seagate disku ve dvou verzích. To je přesně
kombinace, která leromarinvitovi chyběla.

---

## Alternativy, pokud na hardware nedojde

1. **Nasadit disky tam, kde je 528 B nativní** — IBM Storwize, FlashSystem, DS8880.
   Plná kapacita, plný výkon, nulová práce.
2. **Shim přes NBD/iSCSI**, který mapuje 512B logické bloky na 528B fyzické.
   Funguje principiálně, ale je to trvalá režie a nestandardní provoz.
3. **Prodat.** Osm zdravých 1,6TB SAS SSD má u majitelů IBM polí plnou hodnotu,
   protože tam je 528 B žádaná vlastnost, ne vada.

Pro Proxmox platí, že 528bajtový disk neuvidí LVM, ext4, XFS ani ZFS — standardní
block layer s ním nepracuje.

---

## Kapacita po případné konverzi

Dobrá zpráva: **nic by se neztratilo.** Podle Seagate manuálu, tabulka 3 pro
1600GB model:

```
dnes:  3 030 911 576 × 528 B = 1 600 321 312 128 B
512 B: 3 125 627 568 × 512 B = 1 600 321 314 816 B
                              ────────────────────
rozdíl:            +2 688 B na disk  (0,0000 %)
```

IBM drží plný nominál 1,6 TB i s 528bajtovými sektory — 16 bajtů metadat na sektor
bere z interní rezervy, ne z uživatelské kapacity.

---

## Odkazy

- [STH 4968 — How to reformat HDD & SSD to 512B Sector Size](https://forums.servethehome.com/index.php?threads/how-to-reformat-hdd-ssd-to-512b-sector-size.4968/) (29 stran)
- [STH 26945 — Changing block size IBM branded Micron S650DC-800 SSD](https://forums.servethehome.com/index.php?threads/changing-block-size-ibm-branded-micron-s650dc-800-ssd.26945/) (91 příspěvků, 2019–2025)
- [hddguru — analýza formátu LOD](https://forum.hddguru.com/viewtopic.php?f=13&t=28252)
- [Seagate 1200.2 SAS SSD Product Manual 100773817 Rev. D](https://www.seagate.com/content/dam/seagate/migrated-assets/www-content/product-content/ssd-fam/1200-ssd/en-us/docs/1200-2-sas-ssd-product-manual-100773817d.pdf)
- [Mattiwatti/sedutil](https://github.com/Mattiwatti/sedutil) — fork se SHA512, funguje na NetApp

### Které značky jdou reformátovat

| Funguje | Nefunguje |
|---|---|
| NetApp, EMC, Dell, Toshiba, HPE, Huawei, Micron (ne-IBM) | **IBM branded** — Seagate i Micron, zvlášť SED |

Selhalo to i na originálním IBM Power8/Power9 s `iprconfig` a pod AIX. IBM dokumentace
k tomu navíc uvádí „JBOD is not supported on SSDs".

---

*Sestaveno 28. 8. 2026. Měřeno na živém disku, ne převzato z fór — s výjimkou
samotných dumpů a citovaných cizích zkušeností, které jsou označené jménem autora.*

---

# Postup: přeprogramování SPI flash

Zapsáno pro případ, že se objeví programátor. Softwarová cesta je vyčerpaná
(viz níže), tohle je jediná ověřená možnost.

## Co je potřeba

| Položka | Poznámka | Orientační cena |
|---|---|---|
| SPI programátor CH341A | USB, verze **3,3 V** (černá deska; zelená dává 5 V a čip zničí) | 150–250 Kč |
| Adaptér WSON-8 / DFN-8 → DIP | pro `W25Q32FWZEIG`, pouzdro 8×6 mm | 100–200 Kč |
| Horkovzdušná pistole | na odpájení čipu | — |
| Tavidlo, cín, pinzeta | — | — |

SOIC klips **nedoporučuji** — oba lidé, kteří to na fóru dělali, se shodli,
že s ním dump nevyjde spolehlivě a čip stejně museli sundat.

## Postup

**1. Identifikace disku**

Než cokoli rozebereš, poznač si sériové číslo. Disk ve slotu 7 rozblikáš takto:

```bash
timeout 2 sg_dd if=/dev/sgN bs=528 count=200000 of=/dev/null; sleep 1
```

Čtení musí jít přes `/dev/sgN`, ne přes `/dev/sdX` — blokové zařízení má
nulovou velikost, takže přes něj I/O neproteče.

**2. Rozebrání a odpájení**

Čip `W25Q32FWZEIG` je na PCB poblíž SAS konektoru, pouzdro WSON-8 8×6 mm.
Horkovzdušnou pistolí jde dolů za pár desítek sekund. Poznač si orientaci
(tečka = pin 1).

**3. Dump — a hned dvakrát**

```bash
flashrom -p ch341a_spi -r dump1.bin
flashrom -p ch341a_spi -r dump2.bin
sha256sum dump1.bin dump2.bin      # MUSÍ se shodovat
```

Když se hashe liší, je špatný kontakt. Neopravuj to softwarově, opakuj čtení.

**4. Ověření, že dump dává smysl**

```bash
python tools/patch_ibm_lock.py dump1.bin /dev/null 2>&1 | head -5
```

Musí vypsat `soucet=0x0000 OK` a INQUIRY s **tvým** sériovým číslem.
Když součet nesedí, dump je poškozený.

**5. Patch**

```bash
python tools/patch_ibm_lock.py dump1.bin patched.bin \
       --mode blocksize --blocks 3125627568
```

Změní se sedm bajtů a checksum se dopočítá. Nástroj sám ověří, že součet
vyšel nula, a vypíše, které offsety změnil.

**6. Zápis a verifikace**

```bash
flashrom -p ch341a_spi -w patched.bin -V
flashrom -p ch341a_spi -r verify.bin
sha256sum patched.bin verify.bin   # MUSÍ se shodovat
```

**7. Zpět a test**

Zapájet, zapojit, a zkontrolovat:

```bash
sg_readcap -l /dev/sgN      # čekáme 512 B a 3 125 627 568 bloků
sg_inq /dev/sgN             # INQUIRY by mělo zůstat IBM-SSG / 6214
```

## Co se může pokazit

- **Firmware ověřuje konfigurační blok ještě podpisem.** Checksum umíme,
  podpis by byl problém. Nedá se zjistit jinak než zápisem — ale původní
  dump máš, takže se dá vrátit.
- **Špatný dump kvůli kontaktu.** Proto se čte dvakrát a porovnává.
- **Přehřátí čipu.** Horkovzdušná pistole na rozumnou teplotu, ne naplno.
- **Zápis cizího dumpu.** Konfigurační blok nese sériové číslo a kalibraci
  konkrétního kusu. Nikdy nepoužívej `dumps/ibm_ST1600FM0013_6214.bin` —
  je od cizího disku (SN `ZAL15M5Q`).

Původní dump si schovej. Dokud ho máš, je celá operace vratná.
