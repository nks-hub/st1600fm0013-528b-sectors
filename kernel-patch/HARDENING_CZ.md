# Code review emulace 528 s ohledem na ZFS

Datum: 27. 9. 2026. Výchozí stav: `master` na `b2d3359`, označený tagem
`pre-zfs-review-2026-09-27`.

*English version: [HARDENING.md](HARDENING.md)*

## Stručně

Emulace sektory balí a rozbaluje správně a rozložení dat na disku sedí. Chybně
ale zachází s **životním cyklem SCSI příkazu**. Bounce buffer vracela zpátky
v `sd_done()` a počítala s tím, že tam příkaz končí. Nekončí. Po UNIT ATTENTION
nebo resetu hostitele pošle SCSI vrstva stejný příkaz znovu, **aniž by ho znovu
připravila**. Takový opakovaný pokus šel na disk s 512bajtovým bufferem
hostitele a s CDB, které pořád žádalo 528bajtové bloky.

Reprodukováno v QEMU na 7.0 s 528bajtovým diskem `scsi_debug`. Po jediném
vloženém resetu nebo UNIT ATTENTION:

- **čtení** vrátilo starý obsah bounce bufferu a ohlásilo úspěch,
- **zápis** uložil data posunutá o 16 bajtů na sektor a ohlásil úspěch.

Na skutečném stroji visí všechny disky na jednom SAS3216. Reset HBA, reset
linky nebo disk hlásící „power on or reset occurred“ zasáhne všechny disky
najednou, včetně obou polovin každého mirroru. Kontrolní součty ZFS špatná
čtení zachytí a scrub najde špatné zápisy. Když ale oběma kopiemi čerstvě
zapsaného bloku prošel tentýž reset, není odkud je opravit.

Našly a reprodukovaly se ještě tři další chyby, jedna z nich shodí kernel.
Všechny opravuje nový krok 10 v [port_universal.py](port_universal.py). Formát
dat na disku se nemění: stávající pool se přečte bit po bitu stejně (test T17
níž).

## Co udělat

1. Přeložit kernel s aktualizovaným `port_universal.py` (postup v jeho
   docstringu, nově s `patch --fuzz=3`). Zkontrolovat, že výpis končí řádkem
   `hardening  bounce table kept until uninit, per-command context`.
2. Před restartem serveru pustit [test/run-qemu.sh](test/run-qemu.sh) proti
   testovacímu buildu téhož stromu. Všechno kromě T6 a T7 musí hlásit OK.
3. Po restartu spustit `zpool scrub tank` a pak `zpool status -v`. To je
   kontrola všeho, co mohl starý kernel zapsat špatně.
4. Zjistit, jestli starý kernel na tu cestu vůbec narazil:
   `journalctl -k | grep -iE "power-on or device reset|unit attention|reset"`.
   Žádné resety během I/O znamenají, že se chyba s opakováním nikdy neprojevila.

Bootovací parametry zůstávají, jak jsou. Dvě viditelné změny:
`queue/physical_block_size` teď ukazuje 4096 místo 512 a vypnutí
`emulate_512_from_fat_sectors` za běhu už disk nesebere (obojí vysvětleno níž).

## Nálezy

| # | Závažnost | Problém | Reprodukováno | Opraveno |
|---|---|---|---|---|
| 1 | kritická | opakování po UA/DID_RESET běží s bufferem hostitele: špatná data vrácena jako dobrá, posunutá data zapsána jako dobrá | ano, T2 až T5 | ano |
| 2 | kritická | disk offline s příkazy v requeue frontě: `scsi_free_sgtables()` prochází bounce tabulku, kernel oops | ano, T12 | ano |
| 3 | vysoká | zkrácený přenos (resid při stavu GOOD) se hlásil jako celý; chybějící konec pocházel z bounce bufferu | ano, T13 | ano |
| 4 | vysoká | `sd_uninit_command()` uvolňoval `rq->end_io_data` jako emulační kontext u **každého** požadavku, emulovaného i ne; dm-multipath tam má vlastní stav | z kódu | ano |
| 5 | vysoká | rescan příznaky emulace vynuloval a znovu nastavil; I/O za letu mohlo vidět neemulovaný disk a vypnutí parametru za běhu udělalo z živého disku 0 bajtů | T11 | ano |
| 6 | vysoká | `emulate_512_from_fat_sectors` bral i 520bajtové disky, pro které žádná bounce cesta není, takže by každý blok dostal 512bajtový payload | z kódu | ano, 520 se už neemuluje |
| 7 | střední | `physical_block_size` natvrdo 512, čímž se zahodila informace disku 8 × 528; obyčejný `zpool add` by zvolil ashift=9 | T10 | ano, 4096 |
| 8 | střední | `port_universal.py` určoval generaci kernelu podle `lim->`, které přidává už hunk 1, takže 6.8 až 6.17 vždy dostaly kód pro 7.0 a nepřeložily se | build | ano |
| 9 | střední | `port_universal.py` považoval hunk 1 za přítomný, i když byl odmítnutý (hledal `sd_528_page_pool`, které přidává i hunk 12); s `--fuzz` skončil strop max_dev_sectors na 6.8 v komentáři a nikdy neplatil | build | ano |
| 10 | nízká | kontrola zarovnání v sd_done() počítala 528bajtový resid aritmetikou pro mocniny dvou | z kódu | ano, u emulovaných příkazů se přeskakuje |
| 11 | nízká | vytvoření poolů po `--fuzz` sedělo mezi alokací `sd_page_pool` a testem na NULL | z kódu | ano |

### 1. Cesta opakování

`scsi_io_completion()` odpovídá na UNIT ATTENTION u pevného disku a na každý
`DID_RESET` akcí `ACTION_RETRY`: `__scsi_queue_insert(cmd, ..., false)`, tedy
týž příkaz s ponechaným `RQF_DONTPREP`. `sd_setup_read_write_cmnd()` znovu
neběží, takže bounce tabulku nikdo nevrátí. `mpt3sas` vrací `DID_RESET` pro
`SCSI_TASK_TERMINATED` a `SCSI_EXT_TERMINATED`, tedy pro příkazy ukončené
task-management funkcí nebo resetem hostitele, a disk hlásí 29/00 po každém
resetu, kterého si všimne.

Na skutečném HBA proběhne čtení stejně jako v QEMU: disk pošle 528 bajtů na blok
do bufferu dimenzovaného na 512 a `mpt3sas` udělá ze `SCSI_DATA_OVERRUN`
`DID_OK`. SCSI vrstva pak vidí úspěch a staré dokončení zkopírovalo přes buffer
hostitele bounce buffer, který zbyl po neúspěšném prvním pokusu.

Oprava nechává bounce tabulku nainstalovanou od přípravy až do
`sd_uninit_command()`. `sd_done()` už jen rozbaluje. Opakovaný pokus tak odejde
s bufferem, který odpovídá jeho CDB, a READ se prostě rozbalí znovu.

### 2. Uvolnění nesprávné tabulky

S nainstalovanou bounce tabulkou ji cokoli, co příkaz uvolní bez průchodu
`sd_done()`, předá `sg_free_table_chained()`, a ta sleduje „chain“ ukazatele,
které tam nejsou. Stane se to v `scsi_queue_rq()`, když se připravený příkaz
posílá na zařízení, které mezitím přešlo do offline. Přesně to dělá umírající
disk v mirroru.

Bounce tabulka se teď instaluje s `orig_nents = 0`. Takovou datovou tabulku
SCSI vrstva nikdy nevytvoří, takže ji `scsi_free_sgtables()` nechá být, a
zároveň to příkaz označí jako emulovaný. `sd_528_free_emulation()` vrátí
tabulku hostitele a uvolní ji exportovanou `scsi_free_sgtables()`.

### 3. Zkrácené přenosy

Staré dokončení vracelo `host_len`, kdykoli byl stav GOOD. Oprava počítá jen
celé 528bajtové bloky, které disk potvrdil (`dev_len - resid`), rozbalí je a
zbytek nechá SCSI vrstvu zařadit znovu, jako u každého disku. Na `mpt3sas` je
tahle cesta stejně většinou zavřená, protože patch nastavuje `underflow` na
celou délku a ovladač kratší underrun převede na `DID_SOFT_ERROR`. Jiné HBA
hlásí resid se stavem GOOD.

Výsledek kratší než celý přenos při chybě (medium error, NO SENSE) se dál bere
jako nic nepřeneseno. Selže tak celý požadavek místo jeho části, což je
konzervativní: ZFS přečte druhou stranu mirroru a blok přepíše. RECOVERED ERROR
se nově počítá jako úspěch, stejně jako ve stock `sd`. Starý kód takový příkaz
spouštěl znovu, a sektor, který hlásí recovered error pokaždé, by ho spouštěl
donekonečna.

### 4. `rq->end_io_data`

To pole patří tomu, kdo požadavek odeslal. Request-based device-mapper
(dm-multipath) si tam ukládá stav klonu a `blk_execute_rq()` svoje dokončení.
Emulace teď svůj kontext najde ze samotné bounce tabulky (`container_of` na
scatterlistu, ověřené zpětným ukazatelem), takže na to pole vůbec nesahá.

### 5. Rescany

`sd_adjust_logical_sector_size()` při každém rescanu oba příznaky vynulovala a
znovu nastavila, a zdokumentovaný postup ladění rescany vyžaduje. Příznaky se
teď zapisují jen při změně a každé rozhodnutí v I/O cestě po přípravě se řídí
kontextem příkazu, ne příznakem disku. Příznak je navíc lepivý: jednou
emulovaný disk zůstane emulovaný, dokud hlásí 528. Vypnutí parametru ovlivní
jen disky nalezené potom.

### 6. 520bajtové sektory

Patch tvrdil, že 520 bude fungovat, „když transport umí odříznout koncová
metadata“. Žádné SAS HBA to u disku, který hlásí 520 jako délku logického bloku,
nedělá. Emulace teď pokrývá jen 528 a 520bajtový disk zůstane na 0 bajtech jako
ve stock ovladači, s varováním.

### 7. Velikost fyzického bloku

Tyto disky hlásí 8 logických bloků na fyzický, tedy 4224 bajtů. Emulace to teď
převádí na 8 × 512 = 4096 místo natvrdo 512. Hodnota je jen nápověda: na čtení
existujících dat nemění nic a pool vytvořený s ashift=12 jí přesně odpovídá.
Mění výchozí volbu pro nové vdevy. `zpool add` bez `-o ashift=12` by jinak
vytvořil vdev s ashift=9, a z poolu se smíšeným ashift už nejde top-level vdev
později odebrat.

## Jak se testovalo

V [test/](test/) je všechno potřebné:

- `scsi_debug-528.patch` naučí `scsi_debug` 528bajtové sektory (jeden řádek,
  jen pro testovací kernely),
- `init` je skript pro busybox initramfs s testy,
- `rawrd.c` čte nativní 528bajtový blok přes SG_IO, pro kontrolu rozložení,
- `run-qemu.sh` sestaví initramfs a nabootuje kernel, KVM není potřeba.

Testovací kernely měly zapnutý KASAN a `DEBUG_SG`.

| test | 7.0 bez opravy | 7.0 s opravou | 6.17 s opravou |
|---|---|---|---|
| T1 zápis/čtení 8 MiB | OK | OK | OK |
| T2 čtení + DID_RESET | **špatná data, úspěch** | OK | OK |
| T3 čtení + UNIT ATTENTION | **špatná data, úspěch** | OK | OK |
| T4 zápis + DID_RESET | **posunutá data, úspěch** | OK | OK |
| T5 zápis + UNIT ATTENTION | **posunutá data, úspěch** | OK | OK |
| T6/T7 vložený recovered error, viz níž | OK | liší se | liší se |
| T8 osm paralelních zapisovačů | OK | OK | OK |
| T9 17 nezarovnaných sektorů | OK | OK | OK |
| T10 velikost fyzického bloku | 512 | 4096 | 4096 |
| T11 vypnutý parametr + rescan | **disk 0 bajtů** | zachován | zachován |
| T12 offline s příkazy ve frontě | **kernel oops** | OK | OK |
| T13 zkrácené přenosy | **špatná data** | OK | OK |
| T14 skutečný recovered error | OK | OK | OK |
| T15 chyby transportu | OK | OK | OK |
| T16 medium error | EIO | EIO | EIO |
| T17 rozložení na disku | 512 B dat + 16 × 00 | stejné | stejné |

T6 a T7 používají vkládání chyby, které vrátí RECOVERED ERROR, aniž by příkaz
provedlo, a to žádný skutečný disk nedělá. Stock 512bajtový disk `scsi_debug`
bez emulace v nich selže úplně stejně, takže opravená emulace se teď chová jako
stock `sd`. Starý kód jimi prošel jen proto, že každý takový příkaz spouštěl
znovu. T14 je věrná varianta: data se přenesou a teprve pak se ohlásí RECOVERED
ERROR. Projdou obě verze.

`port_universal.py` proběhl na čistých 6.8, 6.14, 6.17 a 7.0 po
`patch --fuzz=3`. Každý strom přeloží `sd.o` s `W=1` bez varování a druhý běh
nic nezmění. Na 7.0 je výsledek bajtově shodný se stromem, na kterém běžely
testy v QEMU.

## Kompatibilita se stávajícími daty

- `sd_528_pack_sg_blocks()` a `sd_528_unpack_sg_bytes()`, jediný kód, který
  přesouvá bajty mezi rozložením hostitele a disku, jsou beze změny.
- Host LBA N je pořád LBA N na disku. Data jsou v prvních 512 bajtech a 16
  koncových bajtů se zapisuje jako nuly (T17, ověřeno nativním čtením).
- Kapacita, velikost logického bloku i bootovací parametry jsou beze změny.

## Poznámky k ZFS

- Na SSD agreguje ZFS do `zfs_vdev_aggregation_limit_non_rotating`, výchozí
  128 KiB, ne do `zfs_vdev_aggregation_limit`. S `emulate_528_max_sectors=256`
  (128 KiB) už obě hodnoty sedí. Ověř, že
  `/sys/block/sdX/queue/rotational` je u emulovaných disků 0.
- Při 128 KiB zabere požadavek 3 bounce chunky. Osm disků s hloubkou fronty 32
  potřebuje 8 × 32 × 3 = 768, takže `emulate_528_pool_chunks=1024` (64 MiB)
  stačí. 4608 (288 MiB) není chyba, jen drží paměť, kterou by mohl využít ARC.
- Do `zpool add` i `zpool create` vždy dávej `-o ashift=12`, ať kernel hlásí
  cokoli.
- Pool je na ZFS z dobrého důvodu: kontrolní součty udělají z chyby emulace
  hlasitou chybu místo tichého poškození. Scruby pravidelně.

## Co se neměnilo

- Bounce pooly jsou globální a 36 MiB se rezervuje při startu i s vypnutou
  emulací. Stojí to jen paměť a oprava by znamenala udělat z parametru čistě
  bootovací.
- Chyba, která hlásí jako dobrou jen část požadavku, shodí celý požadavek
  (viz 3).
- `alignment_offset` z READ CAPACITY(16) se pořád počítá v 528bajtových
  jednotkách. Tyto disky hlásí 0.
- Hostitelé s virt boundary nebo DIX teď dostanou EIO místo tabulky, kterou
  neunesou. `mpt3sas` s SAS disky nemá ani jedno.
