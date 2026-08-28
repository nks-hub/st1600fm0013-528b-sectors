# HBA: SAS3216 klon „9305-16i" — upgrade firmwaru na P16.12

Datum: 28. 8. 2026. Server test-server, slot 3, PCI `b3:00.0`.

## Co ta karta vlastně je

Prodává se jako 9305-16i, ale není to retail Broadcom. Skutečný 9305-16i stojí na
čipu **SAS3224**; tahle karta má **SAS3216** a k němu interní SFF-8643 konektory —
kombinaci, na kterou žádný oficiální firmware nemíří.

Že jde o klon, prozradí `sas3flash -c 0 -list`:

```
Board Name          : Avago SAS3216      generický název čipu, ne „SAS9305-16i"
Board Assembly      : N/A
Board Tracer Number : N/A
```

Prázdné Assembly i Tracer znamenají generický výrobní region. Retail kus by tam měl
číslo sestavy a sériový tracer.

Stav před zásahem:

| | |
|---|---|
| Čip | SAS3216(A1), PCI `1000:00c9`, subsystém `1000:3180` |
| Firmware | 15.00.00.00 |
| NVDATA | 0b.04.00.23 |
| BIOS | 08.35.00.00 |
| SAS adresa | `500062b-2-REDACTED-a780` |

Subsystém `3180` patří 9305-**16e**, protože 16e je taky SAS3216. Karta má ale porty
interní. Právě tenhle rozpor definuje ten klon.

## Proč nešel oficiální firmware

Oba balíky P16.12 od 45Drives (16i i 24i) nesou v sobě řetězec `LSISAS3224`. Naše
karta je SAS3216 a její vlastní firmware obsahuje `LSISAS3216`. `sas3flash` takový
nesoulad odmítne:

```
ERROR: NVDATA Image does not match Controller Device ID!
Device ID - NVDATA:0xc4 Controller:0xc9
```

Odmítnutí je bezpečné, kartu nepoškodí. Nebezpečné je něco jiného: firmware od
Supermicro se podle diskuse na TrueNAS fóru nahraje *bez chyby*, ale karta pak
nefunguje vůbec.

Oficiálně poslední verze pro SAS3216 je 15.00.00.00 — tedy to, co na kartě už bylo.

## Řešení

[kjake/sas3216-9305-firmware](https://github.com/kjake/sas3216-9305-firmware) vezme
stock 9305-16i P16.12 a přepíše v něm NVDATA: identitu čipu na SAS3216 a k tomu
ponechá 16portovou interní PHY mapu z 16i. Přepočítá kontrolní součty jednotlivých
záznamů a obraz vyváží.

### Ověřovací řetězec

Nic se neflashovalo, dokud nesouhlasily všechny čtyři body:

1. **Naše záloha je bajt po bajtu shodná s referenční zálohou klonu** z repa
   (`firmware/original-clone-P15/firmware0.fw`):
   `cmp -l` → 0 rozdílných bajtů z 959 848. Naše karta tedy *je* přesně ten kus
   hardwaru, proti kterému autor celý nástroj validoval.
2. **Vlastní build z nezávisle staženého základu** (45Drives) dal obraz bajtově
   shodný s předpřipraveným v repu. Dodaný obraz tedy není podstrčený a transformace
   je deterministická.
3. **Kontrolní součty repa souhlasí** s hodnotami v jeho dokumentaci.
4. **Sám `sas3flash` při zápisu potvrdil** `NVDATA Device ID and Chip Revision match
   verified` — tedy právě tu kontrolu, na které stock 3224 obraz padal.

Sedí i drobnost: repo píše, že `phys(24)` je kosmetické, firmware dědí 24 PHY slotů
z 16i/3224 NVDATA a enumeruje jen 16 zapojených. Přesně to karta hlásí — phy 0–7 a
16–23 aktivní, 8–15 vypnuté.

## Provedený postup

```bash
# nástroje
curl -LO http://images.45drives.com/tools/sas3ircu
curl -LO http://images.45drives.com/Firmware/LSI9305/sas3flash/linux/sas3flash
chmod +x sas3ircu sas3flash

# zálohy (stdin na /dev/null, jinak si utilita sežere zbytek skriptu ve stránkovači)
./sas3flash -o -c 0 -ufirmware backup_fw_15.00.00.00.bin  < /dev/null
./sas3flash -o -c 0 -ubios     backup_bios_08.35.00.00.rom < /dev/null
./sas3flash -o -c 0 -umpb      backup_mpb.bin              < /dev/null

# build
curl -LO http://images.45drives.com/Firmware/LSI9305/16i/SAS9305_16i_IT_P.bin
git clone https://github.com/kjake/sas3216-9305-firmware.git
python3 sas3216-9305-firmware/build_3216_clone_fw.py \
        --base SAS9305_16i_IT_P.bin --out muj_clone_P16.bin

# flash, bez option ROM (IT mode, ze systému se z HBA nebootuje)
./sas3flash -o -c 0 -f muj_clone_P16.bin < /dev/null
```

## Výsledek

```
Firmware Version 16.00.12.00
Firmware Image compatible with Controller.
Valid NVDATA Image found.  NVDATA Major Version 10.00
NVDATA Device ID and Chip Revision match verified.
Firmware Flash Successful.   Adapter Successfully Reset.
```

| | před | po |
|---|---|---|
| Firmware | 15.00.00.00 | **16.00.12.00** |
| NVDATA | 0b.04.00.23 | 10.00.00.24 |
| SAS adresa | `500062b-2-REDACTED-a780` | beze změny |
| Board Name | Avago SAS3216 | beze změny |

Reset řadiče proběhl za provozu (`mpt3sas_base_hard_reset_handler: SUCCESS`), disk
zůstal viditelný. Chybové čítače linky jsou po resetu vynulované — je to nová
základna, ne důkaz zlepšení; před zásahem stálo `phy_reset_problem_count` na 486,
což byl důsledek přepojování disků.

Pro plné uplatnění nového firmwaru doporučuje dokumentace **studený start**, ne
teplý restart.

## Co to nevyřešilo

Rychlost linku zůstává 6 Gb/s. Řadič nabízí 12 Gb/s na všech phy (`maximum_linkrate
12.0 Gbit`, `hw_max 12.0 Gbit`) — strop drží disk, ne HBA. Upgrade firmwaru řadiče
na tom nemohl nic změnit a taky nezměnil.

## Obnova

Zálohy v tomhle adresáři jsou pro tuhle kartu nenahraditelné — server běží jako
liveboot, takže cokoliv v `/root` zmizí při restartu.

```bash
./sas3flash -o -c 0 -f backup_fw_15.00.00.00.bin < /dev/null
./sas3flash -o -c 0 -b backup_bios_08.35.00.00.rom < /dev/null   # jen když je potřeba option ROM
./sas3flash -o -c 0 -sasadd REDACTED-SAS-ADDRESS < /dev/null          # jen když je adresa vynulovaná
```

`-e 6` maže firmware, ale výrobní oblast nechává, takže SAS adresa přežije.
`-e 7` smaže i ji — pak je nutné adresu vrátit ručně.

## Obsah adresáře

| Soubor | SHA-256 | Co to je |
|---|---|---|
| `backup_fw_15.00.00.00.bin` | `e2fc1ee7…24cfd3a` | **Záloha původního firmwaru karty.** Shodná s referenčním klonem z repa. |
| `backup_bios_08.35.00.00.rom` | `28a9e758…1d63d0` | Záloha option ROM. |
| `backup_mpb.bin` | `9acf33aa…5ec9af9d` | Výrobní blok (SAS adresa, identita desky). |
| `muj_clone_P16.bin` | `2ddb5ee0…8a27314` | Nahraný obraz. Vlastní build, bajtově shodný s validovaným z repa. |
| `SAS9305_16i_IT_P.bin` | `917d0c11…b316464` | Stock základ P16.12 (SAS3224, sám o sobě nepoužitelný). |
| `SAS9305_24i_IT_P.bin` | `3ed68273…b74a65b` | Stock 24i, jen pro srovnání. |
| `sas3flash`, `sas3ircu` | | Nástroje od 45Drives. |

## Zdroje

- [kjake/sas3216-9305-firmware](https://github.com/kjake/sas3216-9305-firmware)
- [TrueNAS: Help finding updated firmware for Avago SAS3216 9305-16i HBA card](https://forums.truenas.com/t/help-finding-updated-firmware-for-avago-sas3216-9305-16i-hba-card/62254)
- [45Drives KB451408 — Flashing LSI 9305 Controllers Firmware in Ubuntu and Rocky Linux](https://knowledgebase.45drives.com/kb/kb451408/)
