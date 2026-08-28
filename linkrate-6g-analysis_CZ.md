# Proč disky jedou 6 Gb/s a co s tím udělá jiný backplane

Rozbor tvrzení z 28. 8. 2026. Měřeno na testovacím serveru, disk `IBM-SSG IBM-SSGSSVJ1P6`
(fw 6214), řadič SAS3216 klon po upgradu na P16.12.

## Tvrzení

1. „máte single SAS backplane"
2. „ty SSD jsou dual portový"
3. „potřebujete dualní SAS line"
4. „některý disky jedou 12G i na single sas, ale tyhle zrovna ne"

## Body 1 a 2 sedí

Disk je skutečně dual-port a připojený je jen jeden port.

Log stránka 0x18 (Protocol Specific Port):

```
relative target port id = 1
  number of phys = 1, phy identifier = 0
    negotiated logical link rate: 6 Gbps
    reason: loss of dword synchronization
    SAS address          = 0x5000c500…ff9
    attached SAS address = 0x500062b2…780   (HBA, phy 4)
    Invalid DWORD count = 27   Running disparity error count = 26
    Loss of DWORD synchronization count = 8   Phy reset problem count = 487

relative target port id = 2
  number of phys = 1, phy identifier = 1
    attached SAS device type: no device attached
    negotiated logical link rate: phy enabled; unknown rate
    SAS address          = 0x5000c500…ffa
    attached SAS address = 0x0
    všechny čítače = 0
```

Port B je zapnutý, ale nevede k němu nic — `attached SAS address = 0x0` a nulové
čítače. Single-port backplane potvrzen.

## Body 3 a 4 neplatí

Rozhodující je stránka 19h/01h „Phy control and discover", kterou hlásí sám disk:

```
>> Phy control and discover (SAS), page_control: current
 00     d9 01 00 64 00 06 02 02  00 00 00 00 11 4a 0e 00
 10     50 00 c5 00 xx xx 1f f9  50 00 62 b2 xx xx a7 80
 20     04 41 00 00 00 00 00 00  88 aa 00 00 00 00 00 00
 30     00 00 00 00 00 00 00 00  00 01 00 00 00 00 00 00
 40     50 00 c5 00 xx xx 1f fa  00 00 00 00 00 00 00 00
 50     00 00 00 00 00 00 00 00  88 aa 00 00 00 00 00 00
```

Hlavička: `d9` = PS|SPF|page 0x19, `01` = subpage, `06` = protokol SAS,
`02` = **počet phy 2**. Deskriptory po 48 bajtech začínají na offsetu 8.

Podle SPL platí v deskriptoru:

```
byte 32 = programmed_min << 4 | hardware_min
byte 33 = programmed_max << 4 | hardware_max
kódy rychlostí: 0x8 = 1,5 G   0x9 = 3 G   0xA = 6 G   0xB = 12 G
```

| phy | SAS adresa | byte 32 | byte 33 | hardware max |
|---|---|---|---|---|
| 0 (port A) | `…bb351ff9` | `0x88` | `0xaa` | **0xA = 6 Gb/s** |
| 1 (port B) | `…bb351ffa` | `0x88` | `0xaa` | **0xA = 6 Gb/s** |

**Obě phy hlásí hardwarové maximum 6 Gb/s.** Tím padá celá úvaha o druhé lince:
i kdyby se port B zapojil, běžel by taky na 6 Gb/s. Není to vlastnost zapojení, sedí
to ve firmwaru disku.

Navíc v SAS se rychlost vyjednává na každé phy zvlášť a nezávisle. Druhý port nikdy
nezvyšuje rychlost prvního linku — dává redundanci a součet propustnosti, ne vyšší
rychlost jednoho spoje.

Řadič přitom 12 Gb/s nabízí:

```
/sys/class/sas_phy/phy-14:*/maximum_linkrate      12.0 Gbit
/sys/class/sas_phy/phy-14:*/maximum_linkrate_hw   12.0 Gbit
/sys/class/sas_phy/phy-14:4/negotiated_linkrate    6.0 Gbit
```

Strop je tedy jednoznačně na straně disku, ne na straně HBA ani kabeláže.

Bod 4 si navíc sám odporuje s bodem 3: když některé disky na single SAS 12G zvládnou,
pak single SAS není tou příčinou. Závěr „tyhle zrovna ne" je věcně správný, ale
z nesprávného důvodu — není to kvůli backplane, ale proto, že firmware disku deklaruje
6 Gb/s jako své hardwarové maximum.

## Souvislost se zámkem sektorů

Pokus přepsat programmed maximum na 12 G (`0xB`) skončil na `ASC 0x26 / ASCQ 0x99` —
tedy stejným vendor-specific kódem, jakým disk odmítá i změnu velikosti sektoru z 528
na 512. Je to jeden a týž zámek IBM firmwaru. Štítek disku přitom uvádí 12Gb/s SAS,
takže křemík 12 G umí; hlásí se jinak.

## Co by nový backplane doopravdy přinesl

Ne 12Gb/s link. Ale dual-port backplane má i tak smysl, jen z jiného důvodu:

- **Propustnost.** Dvě 6G linky v multipath dají zhruba 1 200 MB/s na disk, tedy
  přibližně tolik, co jedna 12G linka. Pro Seagate 1200 SSD, jehož sekvenční čtení se
  pohybuje kolem 1 000 MB/s, je to reálný strop.
- **Redundance.** Výpadek jedné cesty disk neodstaví.

Za to se platí: dual-port backplane s expandéry, obě cesty nakabelované do HBA
(nebo dva HBA) a nastavený `dm-multipath`. Šestnáct linek HBA pak vystačí na 8 disků.

Pokud je cílem číslo „12G" na lince, nový backplane nepomůže. Pokud je cílem
propustnost, je to schůdná cesta.

## Co ještě stojí za pozornost

Čítače na straně disku se resetem HBA nevynulovaly:

```
Invalid DWORD count = 27      Running disparity error count = 26
Loss of DWORD sync   = 8      Phy reset problem count = 487
```

Důvodem posledního resetu linky byla `loss of dword synchronization`. Těch 487 resetů
odpovídá opakovanému přepojování disků, takže to samo o sobě není důkaz vadné
kabeláže. Zbylé čítače ale nejsou nulové a stojí za kontrolu po delším klidném běhu.

## Metoda

```bash
sg_vpd  -p di       /dev/sdc      # dual-port: 2 relative target porty
sg_logs -p 0x18     /dev/sdc      # stav a čítače obou portů
sg_modes -p 0x19,1  /dev/sdc      # hardware max link rate obou phy
sg_modes -p 0x19,1 -H /dev/sdc    # syrové bajty k dekódování
```

První `sg_modes` po hotplugu spadne na `Unit Attention` (mode parameters changed) —
stačí zavolat dvakrát.
