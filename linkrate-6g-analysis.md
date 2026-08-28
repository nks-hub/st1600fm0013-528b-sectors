# Why the disks run at 6 Gb/s, and what a different backplane would change

Analysis of claims made on 28 Aug 2026. Measured on the test server, disk
`IBM-SSG IBM-SSGSSVJ1P6` (fw 6214), SAS3216 clone controller after the upgrade
to P16.12.

## The claims

1. "you have a single SAS backplane"
2. "those SSDs are dual-port"
3. "you need a dual SAS line"
4. "some disks do 12G even on single SAS, but not these ones"

## Points 1 and 2 hold

The disk really is dual-port, and only one port is connected.

Log page 0x18 (Protocol Specific Port):

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
    all counters = 0
```

Port B is enabled but nothing leads to it — `attached SAS address = 0x0` and
zeroed counters. Single-port backplane confirmed.

## Points 3 and 4 do not hold

The decisive evidence is mode page 19h/01h, "Phy control and discover", as
reported by the disk itself:

```
>> Phy control and discover (SAS), page_control: current
 00     d9 01 00 64 00 06 02 02  00 00 00 00 11 4a 0e 00
 10     50 00 c5 00 xx xx 1f f9  50 00 62 b2 xx xx a7 80
 20     04 41 00 00 00 00 00 00  88 aa 00 00 00 00 00 00
 30     00 00 00 00 00 00 00 00  00 01 00 00 00 00 00 00
 40     50 00 c5 00 xx xx 1f fa  00 00 00 00 00 00 00 00
 50     00 00 00 00 00 00 00 00  88 aa 00 00 00 00 00 00
```

Header: `d9` = PS|SPF|page 0x19, `01` = subpage, `06` = SAS protocol,
`02` = **number of phys, 2**. The 48-byte descriptors start at offset 8.

Per SPL, within a descriptor:

```
byte 32 = programmed_min << 4 | hardware_min
byte 33 = programmed_max << 4 | hardware_max
rate codes: 0x8 = 1.5 G   0x9 = 3 G   0xA = 6 G   0xB = 12 G
```

| phy | SAS address | byte 32 | byte 33 | hardware max |
|---|---|---|---|---|
| 0 (port A) | `…1f f9` | `0x88` | `0xaa` | **0xA = 6 Gb/s** |
| 1 (port B) | `…1f fa` | `0x88` | `0xaa` | **0xA = 6 Gb/s** |

**Both phys report a hardware maximum of 6 Gb/s.** That demolishes the whole
second-link argument: even if port B were wired up, it would also run at
6 Gb/s. This is not a property of the wiring, it sits in the disk firmware.

On top of that, SAS negotiates the rate per phy, independently. A second port
never raises the rate of the first link — it provides redundancy and aggregate
bandwidth, not a faster single connection.

The controller offers 12 Gb/s all along:

```
/sys/class/sas_phy/phy-N:*/maximum_linkrate      12.0 Gbit
/sys/class/sas_phy/phy-N:*/maximum_linkrate_hw   12.0 Gbit
/sys/class/sas_phy/phy-N:4/negotiated_linkrate    6.0 Gbit
```

The ceiling is unambiguously on the disk side, not the HBA and not the cabling.

Point 4 also contradicts point 3: if some disks manage 12G on single SAS, then
single SAS is not the cause. The conclusion "but not these ones" is factually
right but for the wrong reason — it is not because of the backplane, it is
because the disk firmware declares 6 Gb/s as its hardware maximum.

## Connection to the sector lock

An attempt to write a programmed maximum of 12 G (`0xB`) ended with
`ASC 0x26 / ASCQ 0x99` — the same vendor-specific code with which the disk
refuses a sector size change from 528 to 512. It is one and the same lock in the
IBM firmware. The disk label meanwhile says 12Gb/s SAS, so the silicon can do
12 G; it just reports otherwise.

## What a new backplane would actually buy

Not a 12 Gb/s link. But a dual-port backplane still makes sense, for a different
reason:

- **Throughput.** Two 6G links under multipath give roughly 1,200 MB/s per disk,
  about what a single 12G link delivers. For the Seagate 1200 SSD, whose
  sequential read sits around 1,000 MB/s, that is the real ceiling.
- **Redundancy.** A single path failure no longer takes the disk offline.

The price: a dual-port backplane with expanders, both paths cabled to the HBA
(or two HBAs), and `dm-multipath` configured. Sixteen HBA lanes then cover
8 disks.

If the goal is the number "12G" on the link, a new backplane will not help. If
the goal is throughput, it is a viable route.

## One more thing worth noting

The counters on the disk side did not reset when the HBA was reset:

```
Invalid DWORD count = 27      Running disparity error count = 26
Loss of DWORD sync   = 8      Phy reset problem count = 487
```

The reason for the last link reset was `loss of dword synchronization`. Those
487 resets match repeated re-plugging of disks, so on their own they are not
evidence of bad cabling. The remaining counters are non-zero though, and are
worth re-checking after a longer quiet run.

## Method

```bash
sg_vpd  -p di       /dev/sdX      # dual-port: 2 relative target ports
sg_logs -p 0x18     /dev/sdX      # state and counters of both ports
sg_modes -p 0x19,1  /dev/sdX      # hardware max link rate of both phys
sg_modes -p 0x19,1 -H /dev/sdX    # raw bytes for manual decoding
```

The first `sg_modes` after a hotplug fails with `Unit Attention` (mode parameters
changed) — just call it twice.
