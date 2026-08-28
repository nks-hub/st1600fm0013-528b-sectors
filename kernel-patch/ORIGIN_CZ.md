# Kernel patch pro 520/528B sektory – co o něm víme

Dva soubory doručené 28. 8. 2026 přes Telegram.

## Co to je

`wvg-sd-528.patch` – unified diff proti `drivers/scsi/sd.c` a `drivers/scsi/sd.h`,
605 přidaných řádků, 6 odebraných. Přidává do `sd` driveru emulaci: disk s 520
nebo 528bajtovými sektory se hostu ukáže jako 512bajtový, metadata se cestou
zahazují.

`rebase_pve_528_patch.py` – nástroj, který patch přenáší na jiné verze kernelu.
Aplikuje hunky, které ještě sedí, a ty, co se rozešly, sémanticky přemisťuje.
Cílová cesta v něm: `patches/kernel/9999-wvg-sd-528-translation.patch`, což je
konvence Proxmoxu.

## Původ – nezjištěn

- Patch **nemá žádnou hlavičku**: chybí `Signed-off-by`, autor, copyright, SPDX.
- Identifikátory `emulate_512_from_fat_sectors`, `sd_528_emulation`,
  `wvg-sd-528` **nejsou nikde na internetu** – ani v kernel gitu, ani na LKML,
  ani na GitHubu.
- Zkratka „WVG“ se v docstringu skriptu bere jako známá věc, ale co znamená,
  se dohledat nepodařilo.
- Časová razítka v diffu: zdroj 19. 2. 2026, upravená verze 25. 4. 2026.

Není to tedy nic veřejného. Buď soukromá práce, nebo generované.

## Technické posouzení

Kód působí kompetentně:

- Převody dělá `DIV_ROUND_UP`, ne bitovým posunem – což je u 528 nutnost,
  protože to není mocnina dvojky.
- Bounce buffery jdou z předalokovaného `mempool` (`SD_528_MEMPOOL_SIZE = 576`
  chunků po 64 kB, `SD_528_CTX_POOL_SIZE = 64` kontextů), takže se nealokuje
  v I/O cestě.
- Má ošetřené chyby (`goto out_eio`, `return -EIO`) a stropy na velikost
  požadavku i hloubku fronty.
- Správně nuluje `protection_type` – 16 B navíc u 528 nejsou T10 PI.
- Rozšiřuje `struct scsi_disk` o `device_sector_size` a dva bitové příznaky.
- Tři laditelné modulové parametry: zapnutí emulace, hloubka fronty, strop
  velikosti požadavku.

Ověřeno proti vanilla 6.8 (`git.kernel.org`, tag v6.8):

```
sd.c   10 ze 13 hunků sedí, 3 selhaly (#1, #9, #10)
sd.h    3 ze 3 hunků sedí
```

Selhávající hunky jsou přesně to, na co je přiložený rebase skript.

## Proč to zatím nejde nasadit

Dvě věci, obě mimo patch:

1. **`CONFIG_BLK_DEV_SD=y`** – `sd` je v kernelu napevno, ne modul. Nejde
   vyměnit za běhu, musí se přeložit celý kernel.
2. **Testovací server běží z live média (read-only)**. Nový kernel by nepřežil
   reboot, takže rebuild tam nemá smysl.

Aby to šlo vyzkoušet, musel by systém běžet z disku – v serveru je Kingston
SA400 240 GB volný.

## Než to někdo pustí na data

605 řádků v DMA cestě SCSI vrstvy. Chyba v offsetu 528bajtového sektoru se
neprojeví hláškou, ale tichým poškozením dat. Minimálně:

- zapsat známý vzor přes emulaci, přečíst nativně přes `sg_dd` a porovnat bajt
  po bajtu,
- totéž na několika offsetech, včetně nezarovnaných a přes hranici chunku,
- nechat běžet `fio` s verifikací a dlouhý `badblocks -w` na obětním disku,
- teprve pak uvažovat o ostrých datech.
