# Reformát 528 → 512 B: co projde a co ne

Zjištění z 28. 8. 2026. Server test-server, řadič SAS3216 klon.

## Zámek není „IBM", je Seagate-specifický

Dlouho to vypadalo, že za vším stojí IBM firmware. Není to tak. Rozhodl kontrolní
experiment: stejná nedestruktivní sonda na dva IBM-brandované disky od různých
výrobců.

```bash
# hlavička MODE SELECT(10) 8 B + block descriptor 8 B, block length 512
printf '\x00\x00\x00\x00\x00\x00\x00\x08\x00\x00\x00\x00\x00\x00\x02\x00' > /tmp/ms512.bin
sg_raw -s 16 -i /tmp/ms512.bin /dev/sdX 55 10 00 00 00 00 00 00 10 00
```

CDB: `55` = MODE SELECT(10), byte 1 = `0x10` → PF=1, **SP=0**, takže se nic neukládá
a na medium se nesahá. Bajty 7–8 = délka parametrů `0x0010`.

| Disk | Výrobce | Výsledek |
|---|---|---|
| IBM-SSG **HSPX400** 400 GB | HGST (OUI `5000cca`) | `SCSI Status: Good` |
| IBM-SSG **IBM-SSGSSVJ1P6** 1,6 TB | Seagate | `Illegal Request — Invalid field in parameter list` |

Sonda trvá zlomek vteřiny a nic nezničí. **Je to první věc, kterou má smysl na
takovém disku spustit** — řekne dopředu, jestli má cenu řešit kernel patch nebo SPI
flash, nebo jestli stačí `sg_format`.

## Kapacita se neztratí

Hlavní obava byla, že přijdeme o 16 B z každého sektoru, tedy ~3 %. Nepotvrdila se.
Disk si po změně délky bloku přepočítal počet LBA z fyzické kapacity:

```
před:  757 743 288 × 528 B = 400 088 456 064 B
po:    781 422 768 × 512 B = 400 088 457 216 B
```

Rozdíl 1 152 bajtů, tedy nic. Stejný závěr vyšel i u Seagate z jeho vlastní tabulky
formátů — 1,600 TB v obou variantách.

## Fast format existuje, ale nezachrání den

`sg_format` 1.62 umí `--ffmt` (pole FAST FORMAT z SBC-4) a `--dcrt` (vynechá
certifikaci média). HGST disk z roku 2013 **FFMT=1 přijal**.

Změřené tempo, ne odhad:

| režim | tempo | celkem |
|---|---|---|
| bez `--ffmt` | 2,35 %/min | ~42 min |
| `--ffmt=1 --dcrt` | 2,82 %/min | ~35 min |

Zrychlení asi 15 %, ne řádové. **Kvůli tomu nemá smysl přerušovat už běžící formát.**

## Přerušit formát je bezpečné

Ověřeno prakticky. Ukončit proces `sg_format` je nutné adresně, podle konkrétního
PID zjištěného předem — hromadné ukončování podle jména procesu zabije i všechno
ostatní, co se jmenuje stejně. Pak následuje reset zařízení:

```bash
sg_reset --device /dev/sdX
sg_reset --target /dev/sdX
```

Disk poté hlásí `Sense key: Medium Error, Additional sense: Medium format corrupted`.
Vypadá to hrozivě, ale je to očekávaný a vratný stav — stačí spustit FORMAT UNIT
znovu. Zajímavé je, že změna délky bloku na 512 se v block descriptoru projevila už
po tom přerušeném formátu.

## Během formátu

`sg_turs` vrací „device not ready", `sg_readcap` totéž, kernel drží `size=0`
a `physical_bs=4224`. Rescan s tím nic neudělá. Průběh jde číst jedině přes:

```bash
sg_requests --progress /dev/sdX
```

Odhad zbývajícího času dělej až po ~5 minutách běhu. Vzorek z prvních 150 s dal
odhad 2 hodiny, skutečnost byla 42 minut — rozjezd je nelineární.

## Výsledek: hotovo a ověřeno

Formát doběhl 28. 8. 2026 ve 13:32 a trval 36 minut. Tempo bylo po celou dobu
lineární (20,68 % ve 13:03 až 98,06 % ve 13:31), takže odhad z ustáleného běhu seděl.

```
Last LBA = 781422767,  Number of logical blocks = 781422768
Logical block length  = 512 bytes
physical block length = 4096 bytes          (bylo 4224 = 8 × 528)
Device size           = 400 088 457 216 B = 400,09 GB
```

Kernel disk konečně vidí:

```
NAME HCTL       TRAN  VENDOR   MODEL     SIZE
sdb  14:0:11:0  sas   IBM-SSG  HSPX400   372,6G
```

### Testy integrity

| test | výsledek |
|---|---|
| zarovnaný zápis 8 MB, zpětné čtení, md5 | shoda (`065a63acf1c056a47d62b333bfe1e328`) |
| nezarovnaný zápis, LBA 2049, 17 sektorů | OK |
| zápis na konec disku, LBA 781 420 720 | OK |
| SMART po testech | OK, endurance stále 1 % |

### Rychlost

```
536 870 912 B zapsáno za 1,371 s = 392 MB/s   (oflag=direct)
```

Pro srovnání: nbdkit shim, který jsme zamítli kvůli výkonu, dával 118 MB/s. Nativní
přístup je tedy přes třikrát rychlejší a bez jakýchkoliv vrstev navíc.

### Co z toho plyne

Tahle cesta je jednoznačně nejlepší ze tří, pokud disk sondu projde: **žádná ztráta
kapacity, standardní kernel, plná nativní rychlost**. Kernel patch má smysl výhradně
u disků, které sondu neprojdou.

## Srovnání tří cest na 512 B

| cesta | funguje na | ztráta kapacity | čas | nároky |
|---|---|---|---|---|
| `sg_format --size=512` | disky bez zámku (HGST) | žádná | ~35–42 min | nic |
| kernel patch (emulace) | čemkoliv | 16 B/sektor → 1,55 TB z 1,60 | build kernelu | nestandardní kernel |
| přepis SPI flash | i zamčené | žádná | hodiny | programátor, rozebrat disk |

Pořadí volby je dané: nejdřív sonda, a když projde, `sg_format`. Kernel patch má
smysl jen u disků, které sondu neprojdou.
