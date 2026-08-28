# Co emulace doopravdy umí, naměřeno

Osm disků ST1600FM0013 (IBM `IBM-SSGSSVJ1P6`, firmware 6214) zamčených na
528bajtové sektory, zpřístupněných přes emulaci `wvg-sd-528` na patchnutém Linuxu
7.0. Stroj: 48 vláken, 125 GiB RAM, jeden řadič SAS3216 na P16.12, jednoportová
backplane, 6 Gb/s na linku. Každé číslo níž pochází z běhu na tom stroji, nic
tady není odhad.

## Tři chyby, které se musely opravit nejdřív

Patch v publikované podobě se nepřeloží a dvě věci, které přeložit jde, sedí na
špatném místě. Všechny tři opravy jsou v [port_universal.py](port_universal.py).

**Nepřeloží se.** `sd_528_restrict_block_ops()` volá `sd_config_discard()`, kterou
`sd.c` definuje o několik set řádků níž a nikde ji dopředně nedeklaruje. Výsledkem
je implicitní deklarace a pak chyba „static declaration follows non-static".
Prototyp nad emulačním blokem to řeší.

**Restrikce blokových operací běžela příliš brzo na to, aby k něčemu byla.** Volání
sedělo uvnitř `sd_read_capacity()`, která běží dávno před
`sd_read_block_provisioning()`, `sd_config_discard()` a `sd_read_write_same()`.
Všechno, co nastavila, přepsal stock kód, který následoval. Dva důsledky:
`sdkp->lbpu` ještě nebylo načtené, takže se discard vypínal bez ohledu na to, co
disk umí, a vypnutí WRITE SAME, které existuje proto, aby se na disk nedostal
payload, jejž emulace neumí přeškálovat, tiše nedělalo nic. Původní patch měl na
stejném místě vlastní `sd_disable_advanced_block_ops()`, takže ani jeho
bezpečnostní opatření nikdy nesepnulo.

**Strop hloubky fronty běžel rovnou v jiné funkci.** Patch kotví
`sd_528_limit_queue_depth()` na řádek `if (!scsi_device_online(sdp))`, jenže první
výskyt téhle podmínky v `sd.c` je v `sd_sync_cache()`, ne v `sd_revalidate_disk()`.
Hloubka fronty se tedy upravovala při flushi cache a na nezatíženém disku se ta
funkce nezavolá vůbec. Naměřeno před opravou: `device/queue_depth` hlásil 254
navzdory `emulate_528_queue_depth=32` na příkazové řádce. Po přesunu obou volání
na konec `sd_revalidate_disk()` hlásí 32.

Navíc je v `init_sd()` double free na OOM cestě: když selže alokace stránkového
poolu, kontextový pool se uvolní inline a pak ještě jednou přes návěští
`err_out_528_page_pool`, do kterého kód propadne. Kompilátor na to mířil
varováním o nepoužitém návěští.

## Co emulace ukazuje

```
zařízení                3 030 911 576 bloků po 528 bajtech
hostitel                1 551 826 726 912 B = 1,41 TiB
logický/fyzický blok    512 / 512
discard_max_bytes       268 435 456
write_zeroes_max_bytes  0
LBPU / LBPRZ            1 / 1
```

Počet bloků se nemění, mění se jen jejich velikost, takže mapování je jedna ku
jedné. Těch 16 metadatových bajtů na sektor je celý ten tříprocentní rozdíl v
kapacitě oproti nativnímu 512bajtovému formátu.

## TRIM projde celým řetězem

Náhodná data zapsaná na LBA 100000, `blkdiscard` přes stejný megabajt, čtení zpět:
samé nuly, tedy přesně to, co slibuje `LBPRZ=1`. `zpool trim` nad hotovým polem
taky běží a hlásí postup na všech osmi členech, takže UNMAP přežije emulaci i pod
ZFS, ne jen jako jednorázové ioctl na syrovém zařízení.

## Syrová zařízení

Nejdřív jeden disk, pak všech osm současně, každý s vlastním fio jobem, aby
nabízená hloubka fronty byla 8 × 32, ne 32.

| profil | jeden disk | osm disků | násobek |
|---|---|---|---|
| sekvenční čtení 1 MiB | 505,8 MB/s | 2 098,5 MB/s | 4,15× |
| sekvenční zápis 1 MiB | 468,6 MB/s | 2 025,8 MB/s | 4,32× |
| náhodné čtení 4 KiB | 98 763 IOPS | 725 420 IOPS | 7,34× |
| náhodný zápis 4 KiB | 104 076 IOPS | 687 300 IOPS | 6,60× |

Náhodné 4 KiB škáluje skoro lineárně, sekvenční 1 MiB jen na polovinu. To je
podpis globálního bounce poolu a aritmetika souhlasí: megabajtový požadavek
spotřebuje 17 z 576 chunků, takže se jich najednou vejde 33, přitom osm disků jich
pouští 256. Požadavek o 4 KiB spotřebuje jeden chunk, takže 576 slotů na 256
požadavků bohatě stačí.

Totéž říká latence. Sekvenční vyskočila z 63 ms na jednom disku na 122 ms na osmi,
což je to zdržení z requeue. Náhodná zůstala na 643 až 706 µs.

Stojí za povšimnutí, že sekvenční agregát je *nižší* než náhodný, 2,1 proti
2,8 GB/s. Kdyby strop drželo vedení nebo řadič, velké bloky by nemohly být pomalejší
než malé. To vylučuje přenosovou cestu.

## Velikost požadavku proti velikosti poolu

`emulate_528_max_sectors` se dá měnit za běhu, takže tenhle sweep nepotřebuje
překlad, jen rescan. Osm disků, souhrnná čísla.

| max_sectors | požadavek | 576 chunků čtení / zápis | 4608 chunků čtení / zápis |
|---|---|---|---|
| 2048 | 1 MiB | 2 579 / 1 988 MB/s | 3 542 / 2 963 MB/s |
| 512 | 256 KiB | 3 078 / 2 732 MB/s | 3 862 / 3 443 MB/s |
| 256 | 128 KiB | 3 953 / 3 385 MB/s | 4 065 / 3 522 MB/s |
| 128 | 64 KiB | 4 095 / 3 391 MB/s | 4 095 / 3 511 MB/s |

Náhodné 4 KiB se v každém řádku obou sloupců drželo do jednoho procenta od
780 000 IOPS, přesně jak aritmetika chunků předpovídá.

Z úzkého hrdla tedy vedou dvě cesty. Zmenšení požadavků funguje, ale zakáže ZFS
agregovat nad tu velikost. Zvětšení rezervy velké požadavky zachová a vrátí většinu
ztráty, 37 % na čtení a 49 % na zápis při 1 MiB, ale ne všechnu: 3 542 proti
4 095 MB/s. Něco velké požadavky stojí dál, nejspíš kopie přes sedmnáctipoložkový
scatter-gather seznam.

Nejlepší kombinace není ani jeden extrém. **128KiB požadavky s velkým poolem dají
4 065 / 3 522 MB/s**, což je v rámci šumu totéž co 64 KiB, a 128 KiB je výchozí
`recordsize` ZFS, takže se záznam mapuje přesně na jeden požadavek. Těch
4 095 MB/s napříč osmi disky je 512 MB/s na disk, tedy hodnota, kterou dá jeden
disk samostatně: strop se přesunul z emulace do disků.

Hlubší fronta nepomáhá. Při 128KiB požadavcích dala hloubka 32 sekvenční čtení
4 072 MB/s, 64 dala 3 931 a 128 dala 3 798, přičemž náhodné se nehnulo. Zůstává 32.

## Nastavení, ke kterému to vede

```
sd_mod.emulate_512_from_fat_sectors=1
sd_mod.emulate_528_queue_depth=32
sd_mod.emulate_528_max_sectors=256
sd_mod.emulate_528_pool_chunks=4608
sd_mod.emulate_528_pool_contexts=512
```

Poslední dva jsou nové. `SD_528_MEMPOOL_SIZE` a `SD_528_CTX_POOL_SIZE` byly
konstanty známé při překladu, používané jen jako počty, nikde ne jako rozměry
polí, takže nic nebránilo udělat z nich parametry. 4608 chunků je 288 MiB ze
125 GiB, které stroj má, a pokryje 271 souběžných megabajtových požadavků proti
256, které osm disků pouští. Čtou se jednou při startu a jsou omezené na rozsah
1 až 65536, protože nula nechá pool prázdný a nesmyslná hodnota shodí alokaci, což
znamená žádný ovladač `sd` a žádný kořenový svazek.

## Přes ZFS

Pole jsou čtyři dvoucestné mirrory ve stripu, `ashift=12`, `lz4`, `atime=off`,
`xattr=sa`, `autotrim=on`. 5,62 T hrubě, 5,50 T využitelně.

| profil | dataset | vlákna | výsledek | latence |
|---|---|---|---|---|
| sekvenční čtení 1 MiB | `recordsize=128K` | 32 | 4 129 MB/s | |
| sekvenční zápis 1 MiB | `recordsize=128K` | 32 | 1 231 MB/s | |
| náhodné čtení 4 KiB | `recordsize=16K` | 32 | 276 065 IOPS | 14,2 ms |
| náhodný zápis 4 KiB | `recordsize=16K` | 32 | 98 181 IOPS | 20,5 ms |
| náhodné čtení 16 KiB | `recordsize=16K` | 32 | 189 347 IOPS | 13,9 ms |
| náhodný zápis 16 KiB | `recordsize=16K` | 32 | 51 256 IOPS | 39,4 ms |

Sekvenční čtení přes ZFS překoná syrové osmidiskové číslo, 4 129 proti
4 095 MB/s, protože mirror může číst z obou polovin a pole má tedy na čtení k
dispozici víc vřeten než prostý stripe.

Sekvenční zápis vypadá na třetinu syrového čísla, dokud nezapočítáte uspořádání:
mirror zapisuje každý blok dvakrát, takže strop pole na zápis je polovina z
3 522 MB/s, tedy asi 1 760. Naměřených 1 231 MB/s je 70 % z toho, což je běžná
režie ZFS na kontrolní součty, metadata a transakční skupiny.

### U náhodných operací rozhoduje recordsize

Měřeno na samostatných datasetech, `primarycache=metadata`, aby se nic
neobsluhovalo z ARC, 8 vláken:

| recordsize | náhodné čtení 4K | náhodný zápis 4K | čtení 16K | sekvenční zápis |
|---|---|---|---|---|
| 4K | 52 289 IOPS | 73 648 IOPS | 29 778 | 823 MB/s |
| 16K | 33 646 | 60 507 | 34 569 | 952 MB/s |
| 64K | 17 912 | 34 568 | 18 026 | 1 114 MB/s |
| 128K | 11 787 | 23 655 | 11 720 | 1 107 MB/s |

Čtení 4 KiB ze záznamu o 128 KiB donutí ZFS přečíst celý záznam. Při 11 787 IOPS
to je 1 500 MB/s skutečného provozu na discích, z čehož aplikace chtěla jednu
dvaatřicetinu. Nic není rozbité, je to čtecí amplifikace a jediný lék je velikost
záznamu odpovídající přístupovému vzoru.

### První čísla přes ZFS byla špatně, protože byl špatně test

Osm vláken není dost zátěže pro pole se čtyřmi vdevy a hlubokou ZIO pipeline.
Škálování téhož náhodného čtení 4 KiB na datasetu s `recordsize=4K`, s vytížením
procesoru vzorkovaným během každého běhu:

| vlákna | IOPS | propustnost | latence | CPU |
|---|---|---|---|---|
| 8 | 52 646 | 205,6 MB/s | 9,6 ms | 12,7 % |
| 16 | 106 098 | 414,4 MB/s | 9,5 ms | 8,3 % |
| 32 | 144 897 | 566,0 MB/s | 13,9 ms | 10,2 % |
| 64 | 190 841 | 745,5 MB/s | 21,1 ms | 10,4 % |
| 96 | 240 321 | 938,7 MB/s | 25,1 ms | 12,3 % |
| 128 | 258 408 | 1 009,3 MB/s | 31,1 ms | 2,0 % |
| 192 | **292 558** | 1 142,7 MB/s | 41,0 ms | 8,5 % |
| 256 | 280 887 | 1 097,1 MB/s | 56,8 ms | 1,9 % |

Procesor se nedostane nad 13 %. Pole je omezené frontou, ne výpočtem, a křivka má
koleno při 192 vláknech. Vrchol náhodného čtení 4 KiB je 292 558 IOPS, ale při
41 ms je to číslo do tabulky, ne provozní bod. Souběžnost se volí podle latence,
kterou zátěž snese.

## Integrita

`fio` zapsal 8 GiB v osmi jobech s `verify=crc32c` a přečetl je zpět: **žádné
chyby ověření**. Následný `zpool scrub` opravil 0 B s nula chybami. Kdyby emulace
při skládání 528 na 512 sektor posunula nebo prohodila, oboje by selhalo, a
kontrolní součty ZFS by z tiché koruze udělaly hlasitou. To je dobrý důvod
provozovat na téhle sestavě ZFS.

## Přežije restart

Po studeném startu se bootovací parametry uplatní, emulace naběhne dřív než ZFS,
všech osm disků se vyjmenuje a pole se naimportuje samo z `zpool.cache` i s
připojenými datasety. Nic nepotřebuje zásah.

## Co z toho plyne pro Proxmox

Aktivní feature flagy pole jsou `empty_bpobj`, `lz4_compress`,
`spacemap_histogram`, `enabled_txg`, `hole_birth`, `extensible_dataset`,
`embedded_data`, `large_dnode`, `userobj_accounting`, `project_quota`,
`spacemap_v2`, `log_spacemap`, `zilsaxattr`, `head_errlog` a `vdev_zaps_v2`.
Poslední tři přišly v OpenZFS 2.2, takže importující strana potřebuje 2.2 nebo
novější. Před přesunem pole tam ověřte `zfs version`; když je starší, vytvořte
pool s `-o compatibility=` přišpendleným na to, co podporuje, místo abyste na to
přišli až při importu.

Kernel patch musí jet s sebou. Proxmox dodává vlastní kernel, takže se emulace
musí přeložit tam, a `port_universal.py` rozpozná generaci queue-limits stejně,
jako to udělal u 7.0. Bez něj každý členský disk hlásí nula bajtů a pole nejde
naimportovat vůbec. To si zaslouží nálepku na skříni.
