# Ověření patche wvg-sd-528 proti upstream kernelům

Datum: 28. 8. 2026

> **Pozdější vývoj:** problémy popsané níže se podařilo odstranit a patch nakonec
> běží. Viz [RESULTS_CZ.md](RESULTS_CZ.md). Tenhle dokument je snímek z fáze
> posuzování, ještě před doportováním na API 6.8.

## Závěr napřed

**Patch cílí na API, které v žádném upstream kernelu neexistuje.** Nelze ho
aplikovat ani na 6.8, ani na novější řady, protože tři z jeho hunků očekávají
kód, který v `drivers/scsi/sd.c` nikdy nebyl.

## Co bylo změřeno

Staženy originály `drivers/scsi/sd.c` z `git.kernel.org` pro tagy v6.8 až v6.14
a porovnány s konstrukcemi, které patch v kontextových řádcích vyžaduje.

| Kernel | `lim = kmalloc` | `lim->max_dev_sectors` | `lim.max_dev_sectors` | `struct queue_limits lim;` |
|---|---|---|---|---|
| 6.8 | 0 | 0 | 0 | 0 |
| 6.9 | 0 | 0 | 0 | 0 |
| 6.10 | 0 | 0 | 0 | 0 |
| 6.11 | 0 | 0 | 1 | 4 |
| 6.12 | 0 | 0 | 1 | 4 |
| 6.13 | 0 | 0 | 1 | 4 |
| 6.14 | 0 | 0 | 1 | 4 |

Patch přitom v hunku 9 očekává:

```c
	if (!scsi_device_online(sdp))
		goto out;

	lim = kmalloc(sizeof(*lim), GFP_KERNEL);
	if (!lim)
		goto out;
```

a v hunku 10 pracuje s `lim->max_dev_sectors`, tedy s **ukazatelem**.

Skutečnost je jiná. Do 6.10 včetně tam žádné `queue_limits` nejsou. Od 6.11 sice
`struct queue_limits lim;` existuje, ale jako **lokální proměnná na zásobníku** –
přistupuje se k ní tečkou (`lim.max_dev_sectors`), ne šipkou, a nikde se
nealokuje přes `kmalloc`.

## Jak dopadla aplikace

Na vanilla 6.8:

```
sd.c   10 ze 13 hunků prošlo, 3 selhaly (#1, #9, #10)
sd.h    3 ze 3 prošly
```

Deset hunků prošlo jen proto, že `patch` toleruje posun a fuzz – jsou to drobné
vsuvky do funkcí, které se od té doby nezměnily. Tři selhaly na místech, kde se
kód rozešel zásadně.

Přiložený `rebase_pve_528_patch.py` selhal také:

```
ERROR: sd_revalidate_disk: expected one function definition, found 0
```

Skript hledá tvar funkce, který ve stromu nenašel.

## Co z toho plyne

Sečteno dohromady:

- patch **nemá žádnou hlavičku** – chybí autor, `Signed-off-by`, copyright, SPDX,
- jeho identifikátory (`emulate_512_from_fat_sectors`, `sd_528_emulation`,
  `wvg-sd-528`) **nejsou nikde na internetu**,
- cílí na **API, které v upstream neexistuje** v žádné verzi,
- přiložený rebase nástroj na stromu selže hned na první kontrole.

To dohromady odpovídá kódu, který **nikdy neprošel překladem**. Kdyby ho autor
zkompiloval, na tenhle rozpor by narazil hned.

Nedá se vyloučit, že existuje downstream fork s takto upraveným
`sd_revalidate_disk` – ale nenašel jsem po něm stopu a Proxmox ani Ubuntu ho
nemají.

## Co by bylo potřeba, aby se dal použít

Přepsat hunky 9 a 10 pro reálné API cílového kernelu. To není mechanická práce:
je nutné pochopit, kde se v dané verzi nastavují limity fronty, a zasadit tam
volání `sd_528_limit_queue_depth()` a omezení `max_dev_sectors` správně. Zbytek
patche (velký hunk 1 s emulační infrastrukturou) na to navazuje a musel by se
proti tomu ověřit celý.

Než by na tom někdo stavěl, mělo by se ověřit, jestli ta infrastruktura vůbec
dává smysl – dosud nikdo nepotvrdil, že se to přeložilo, natož že to běželo.

## Doporučení

Nepoužívat, dokud se to nepodaří přeložit a projít testem integrity dat proti
nativnímu čtení přes `sg_dd`. Sám o sobě to není důkaz, že je patch špatný –
je to důkaz, že je **neověřený**.
