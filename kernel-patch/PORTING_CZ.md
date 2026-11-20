# Přenos patche na nový kernel

Datum: 20. 11. 2026

`wvg-sd-528.patch` je obyčejný diff. Když nový kernel posune řádek vedle některého
hunku, `patch` ten hunk odmítne. Z 13 hunků v `sd.c` jich `patch` odmítá 2 na
vanilla 6.12 a 6.14, 3 na 6.8, 4 na Proxmoxu 7.0.14-15 a 6 na Proxmoxu 7.0.14-30.
Tahle stránka popisuje, jak `port_universal.py` nasadí celou emulaci na takový
strom bez ruční práce a jak to `check_trees.py` dokazuje.

## Použití

```bash
# libovolný strom kernelu: nasadí emulaci do drivers/scsi/sd.c a sd.h
python3 port_universal.py /cesta/k/linux

# totéž a výsledek se zapíše jako jeden diff pro build balíčku
python3 port_universal.py /cesta/k/linux --emit-patch 9999-wvg-sd-528-translation.patch

# Proxmox: diff se vyrobí proti stromu Ubuntu kernelu v checkoutu pve-kernel,
# nejdřív se přehrají patche Proxmoxu, které sahají na sd.c
python3 rebase_pve_528_patch.py --kernel-tree submodules/ubuntu-kernel \
    --pve-patches patches/kernel --output patches/kernel/9999-wvg-sd-528-translation.patch
```

Návratový kód 0 znamená, že je na místě každý kus. Kód 2 znamená, že se některý kus
nepodařilo umístit: strom zůstane přesně tak, jak byl, a výstup kus jmenuje.
Druhý běh na hotovém stromu nic nezmění. `--check` jen hlásí stav.

## Co nástroj dělá

1. Generaci queue limits určí jen z těla `sd_revalidate_disk()` (`none` do 6.10,
   `local` pro 6.11 až 6.14, `ptr` pro 7.0). Kód emulace používá `lim->` i jinde
   v souboru, takže při pohledu na celý soubor by byl každý strom `ptr`, jakmile
   přistane první hunk.
2. Dokud něco chybí, pustí GNU patch s `-N -r -`. Hunky, jejichž kontext sedí,
   přistanou jako dřív a nezůstanou po nich soubory `.rej`.
3. Každý kus, který chybí, umístí podle struktury, ne podle kontextových řádků:
   uvnitř pojmenované funkce, za deklaraci, před návěští. Velký první hunk se bere
   z diffu samotného, takže kód je tentýž text.
4. Plošné vypnutí discardu z původního patche nahradí úzkým omezením (WRITE SAME
   pryč, UNMAP zůstává) a omezení spolu s limitem hloubky fronty přesune na konec
   `sd_revalidate_disk()`. Obojí je popsané v komentářích `port_universal.py`.
5. Ověří, že každý kus je přítomen právě jednou, podle definice nebo místa volání,
   nikdy podle zmínky o symbolu.

## Proč se „už je vloženo?“ určuje podle definice

`sd_528_page_pool` a `sd_528_effective_max_sectors` jsou v prvním hunku, takže v
souboru jsou, jakmile ten hunk přistane, ať už přistály hunky, které je používají,
nebo ne. Kontrola podle jména pak označí napůl portovaný soubor za hotový. Stejně
dopadlo `sd_528_restrict_block_ops` a ničení poolů: stará kontrola našla
`mempool_destroy(sd_528_page_pool)` v chybové větvi `init_sd()` a nikdy nepřidala
to, které potřebuje `exit_sd()`. Teď se hledá definice, volání uvnitř funkce, která
ho musí obsahovat, nebo přesný příkaz. `emu_cap` je jen v omezení `max_dev_sectors`
a jako značka pro něj stačí.

## Co se mezi kernely pohnulo

| Strom | Hunky, které `patch` odmítá | Proč |
|---|---|---|
| 6.8 | 1, 9, 10 | `sd_revalidate_disk()` nemá queue limits (limit se píše do `q->limits`) |
| 6.12, 6.14 | 9, 10 | `struct queue_limits lim;` je lokální proměnná, takže `lim.` místo `lim->` |
| Proxmox 7.0.14-15 | 1, 9, 11, 13 | `scsi_register_driver(&sd_template)` už nemá `.gendrv`, kontext hunků 1 a 9 se změnil |
| Proxmox 7.0.14-30 | 1, 4, 8, 9, 11, 13 | totéž a navíc `sd_large_page_pool` vedle `sd_page_pool` a předčasný návrat v `sd_uninit_command()` (Ubuntu 7.0.0-44) |

## Regresní test

`check_trees.py` vezme libovolný počet stromů a u každého s čistou kopií `sd.c` a
`sd.h` ověří:

1. port doběhne bez ruční práce,
2. druhý běh nic nezmění,
3. `--semantic-only` (bez GNU patch, každý kus vložený podle kotvy) dá stejný kód,
   až na prázdné řádky,
4. vydaný diff sedí na původní soubory s `patch --fuzz=0`,
5. `make drivers/scsi/sd.o` z portovaných souborů projde a nevypíše varování,
   které neopatchovaný soubor nevypisuje.

```bash
python3 check_trees.py \
    --tree 6.8=/src/linux-6.8 \
    --tree 6.12=/src/linux-6.12 \
    --tree 6.14=/src/linux-6.14 \
    --tree pve=/src/ubuntu-kernel,patches=/src/pve-kernel/patches/kernel,config=/src/pve.config
```

U stromu: `NAME=CESTA[,patches=ADRESÁŘ][,config=SOUBOR]`. `patches` je adresář s patchi,
které se v ostrém buildu aplikují před naším; ty, co sahají na `sd.c` nebo `sd.h`,
se přehrají jako první. `config` je soubor `.config`; bez něj se použije vlastní
`.config` stromu, jinak `make defconfig` se zapnutým SCSI a `BLK_DEV_SD`.
Kompilace běží přímo v pojmenovaném stromu: původní soubory se po ní vrátí, ale
objekty a `.config` zůstanou, takže použij strom, který nevadí zaplnit. Přepínač
`--no-compile` krok 5 vynechá. Potřebuje gcc, make, bc, flex, bison, libssl-dev
a libelf-dev, u konfigurace Proxmoxu nebo Ubuntu navíc libdw-dev.

Běh z 20. 11. 2026 (x86_64, gcc z Debianu 13):

| Strom | Generace | Kroky 1 až 4 | `sd.o` (nová varování) |
|---|---|---|---|
| vanilla 6.8 | none | prošlo | přeloží se (0) |
| vanilla 6.12 | local | prošlo | přeloží se (0) |
| vanilla 6.14 | local | prošlo | přeloží se (0) |
| Proxmox 7.0.14-15 (+ its patches) | ptr | prošlo | přeloží se (0) |
| Proxmox 7.0.14-30 (+ patch 0141) | ptr | prošlo | přeloží se (0) |

## Meze

- Nástroj čte a mění jen `drivers/scsi/sd.c` a `sd.h`. Kernel, který změní, jak
  midlayer SCSI předává datové buffery, se může přeložit a přesto být špatně.
  Ukáže to teprve test v QEMU z [RESULTS_CZ.md](RESULTS_CZ.md), takže nový kernel
  přes něj projde dřív, než se spustí na skutečných discích.
- Když zmizí kotva, běh skončí kódem 2 a kus jmenuje. Pro nový tvar kódu se do
  `port_universal.py` přidá kotva a do regresního testu strom.
- Přeložilo se jen pro amd64.

## Automatické buildy

Build Proxmoxu v apt repozitáři NKS Hub při každém běhu stáhne tento
repozitář, diff proti stromu nového kernelu Proxmoxu vyrobí
`rebase_pve_528_patch.py` a staví jen tehdy, když to projde. Při selhání se běh
zastaví dřív, než se cokoli postaví nebo publikuje.
