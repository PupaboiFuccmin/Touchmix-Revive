# TouchMix-16 Control Protocol

Reverse-engineering notes for the QSC TouchMix-16 Wi-Fi control protocol, from
PCAPdroid captures of the official TouchMix-8/16 Control Android app.

**Last updated:** 2026-10-21 (2 captures)
**Firmware seen:** config `Version="3.0.9551"`, product codename `MicroX16`

Status legend: ✅ confirmed from captures · 🟡 strong guess · ❓ unknown

## Transport ✅

| Item | Value |
|------|-------|
| Transport | TCP |
| Mixer port | **42217** |
| Mixer address (Wi-Fi AP mode) | 192.168.1.1 |
| Byte order | Little-endian throughout |
| Mixer OS | Linux (file paths under `/usr/share/ds2i/`) |

In both captures the app opened a connection, then dropped it and reconnected
about 10 s later. Both connections ran the same handshake, and commands sent on
the first one took effect. The second connection is the one that persists.

## Message framing ✅

Every message starts with the same 6-byte header. A single TCP segment often
holds several messages, so always split them by the length field and never by
packet boundaries.

```
u16 type
u16 subtype
u16 length        total message length, header included
u8  payload[length-6]
```

| Type.Sub | Name | Direction | Notes |
|---------:|------|-----------|-------|
| 1.3 | hello | both | identity exchange, first message on each side |
| 2.2 | session | M→A | 6-byte payload (`b4 9c 01 00 00 00`) ❓ |
| 3.4 | file | M→A | chunked file transfer (scene/config XML) |
| 4.2 | status | M→A | single unsolicited parameter push |
| 5.1 | param-set | both | **parameter changes**: what you send to control the mixer |
| 6.1 | poll-req | A→M | periodic request, sequence-numbered |
| 7.1 | poll-resp | M→A | reply to 6.1 with the same sequence number |

### 1.3 hello ✅

```
u32 1               (version?)
u16 3               ❓
u16 name_len
u16 0
u16 total_len       name_len + len(suffix)
char name[name_len] + suffix       (no terminator)
```

The **mixer sends its hello first**, as soon as the TCP connection opens. The
client answers with its own. `touchmix discover` relies on this: it reads the
greeting and disconnects without logging in.

* Mixer: name `QSC JD HOME` (the mixer's configured name) + suffix `MicroX16`.
* App: name `Lenovo-Tab-P11-Plus` + suffix `MicroX16-AndroidTablet`.

The suffix seems to identify the product and the client type. The mixer saves a
per-client settings file named after the client (see below).

### 3.4 file transfer ✅

```
u32 offset
u16 len
u8  data[len]
```

* `offset = 0xFFFFFFFF`: start. `data` is the file name, e.g. `NH4-config.xml.dscomp`.
* `offset = 0xFFFFFFFE`: end. `data` is the full path on the mixer, followed by
  6 trailer bytes (probably a checksum ❓).
* Anything else: a chunk of file data at that offset. Chunks are 1012 bytes.

**`.dscomp` is plain zlib** (`78 9c`). After connecting, the mixer sends:

| File | Size (inflated) | Contents |
|------|-----------------|----------|
| `/usr/share/ds2i/tmp/NH4-config.xml.dscomp` | ~516 KB | **The full current mixer state, with every parameter's name and index** |
| `/usr/share/ds2i/tmp/fileIndex.xml.dscomp` | ~243 KB | List of factory and user scenes and presets |
| `/usr/share/ds2i/user/settings/<CLIENT>.settings.dqx.dscomp` | ~2 KB | Per-client access rights (see "Per-client access") |

The app builds its initial UI from `NH4-config.xml`, where values are in
engineering units (dB, Hz). After that, all changes arrive as 5.1 messages with
normalized floats.

### 4.2 status ✅

```
u16 param_id
f32 value
```

Sent unsolicited right after connecting (aux link and compressor-mode states),
and occasionally later, e.g. `0x00CE Global > Misc Input > Wireless Strength`.

### 5.1 param-set ✅

```
u32 count
u16 bytes            = 8 * count
repeat count:
  u16 param_id
  u16 tag            usually 0 (see open questions)
  f32 value          normalized 0.0 – 1.0 for continuous controls
```

* The app sends one of these for each UI change. A fader drag produces a stream
  of them, about every 110 ms.
* The mixer sends them to the app when a control changes on the mixer itself
  (e.g. cue pressed on the TouchMix screen, or a channel reset).
* Some IDs carry integers, not floats. `0x00D4 Mixer Selected Channel`
  arrived as raw `0x00010003` when channel 4 (index 3) was selected.

### 6.1 / 7.1 poll 🟡

```
6.1: u32 seq, u32 n_bytes, u8 request[n_bytes]
7.1: u32 seq, u32 n_bytes, u8 reply[n_bytes]     (array of f32)
```

The app cycles through three request shapes (24/32/52-byte messages), one
roughly every 80 ms, and each reply echoes `seq`. The replies are mostly zero
with an occasional 1.0. That fits **meters** (no audio was connected during
the capture). To confirm, capture again with a signal on a channel.

## Per-client access ✅

The mixer saves a profile for each client name it sees, at
`/usr/share/ds2i/user/settings/<NAME IN CAPS>.settings.dqx`, and sends it
after the config. Its `Access` effect has one flag per area, matching the
`Auth > Access` parameters (`0x0DE0`–`0x0DF4`): `All`, `Security Settings`,
`Main Mix`, `Aux 1`…`Aux 9/10`, `Record`, `User Buttons`, plus several
`Locked` entries.

**A new client gets every flag at 0.** It still receives the full config and
all live changes, but the mixer **silently drops its 5.1 writes**. No error
comes back. Confirmed 2026-10-21: a probe from `Revive-LAPTOP-4THKAI2Q`
wrote In 4 fader values 0…1, and the config read back unchanged. Its profile
showed all zeros. Access is granted on the mixer, in its Wi-Fi device access
settings. After the grant, the same probe worked. The client library exposes the flags as `TouchMix.access` and
`can_write()`.

Not yet understood: a second connection from the same PC, opened while the
first was still connected, got the hello but never received the config. The
mixer may allow one session per IP, or something on its screen may have been
waiting for input.

## Network settings (from the config)

`Global > Control` has `Create Network` (1 = mixer makes its own access point),
`Create AP`, `Infra SSID` / `Infra PW` (join an existing Wi-Fi), `IP Assign
Mode`, `Static IP` / `Static GW` / `Static NM`, `Current IP`, and `Net
Security`. `Global > Misc Input` has `Network connected`, `Network name` and
`Wired Net Status`. How the official app discovers a mixer on a shared network
has not been captured yet. (All captures so far were made on the mixer's own
access point.)

## Parameter IDs ✅

**The parameter ID on the wire is the `Index` attribute in `NH4-config.xml`.**
[`params.csv`](../touchmix/data/params.csv) lists all 5,694 parameters: index, path, name and
type code. Rebuild it from any capture with `tools/tmcap.py files` followed by
`tools/tmcap.py dict`. Add `--values` to include the current values (engineering
units) and text fields, but **don't commit that version**; see the security note.

> ⚠️ **Security note:** `NH4-config.xml` contains the mixer's Wi-Fi network
> password (`0x010C Global > Control > Network Password`), the mixer password
> fields, and the security override password (`0x0DB1`), all in plain text. The
> mixer sends this file unencrypted to every client that completes the hello
> exchange. Never publish raw captures or extracted XML.

### Block layout

| Block | First ID | Stride | Formula |
|-------|----------|--------|---------|
| Input channel main block (In 1–16) | In 1 fader `0x013A` | **76** (`0x4C`) ✅ on wire (In 4 ↔ In 5) | `0x013A + (ch-1)*76 + offset` |
| Input channel send block (In 1–16) | In 1 AuxSend1 `0x11EB` | **42** (`0x2A`) ✅ on wire (In 4 ↔ In 5) | `0x11EB + (ch-1)*42 + offset` |
| Aux 1–6 bus | Aux 1 fader `0x0688` | **187** (`0xBB`) | `0x0688 + (aux-1)*187 + offset` |
| Main | fader `0x0000` | — | |
| FX 1/2 | fader `0x0B7A` / `0x0BE5` | 107 | |
| DCA 1 | fader `0x0D42` | 13 | |

### Input channel, confirmed on In 4 (main block base = fader = `0x021E`)

| Offset | In 4 ID | Name | Wire value |
|-------:|---------|------|-----------|
| +0 | `0x021E` | Fader | 0–1; see scaling (0.7512 = 0 dB) |
| +1 | `0x021F` | Mute | 0/1 |
| +2 | `0x0220` | Phase (polarity) | 0/1 |
| +3 | `0x0221` | Pan | 0 = L, 0.5 = C, 1 = R |
| +4 | `0x0222` | Solo (cue) | 0/1 |
| +14 | `0x022C` | Trim (digital gain) | 0–1, 0.5 = default |
| +17 | `0x022F` | Delay Time | 0–1 |
| +18 | `0x0230` | Delay Feet | sent by the app along with Delay Time |
| +19 | `0x0231` | Delay Meters | sent by the app along with Delay Time |
| +20 | `0x0232` | Delay Enable ("In") | 0/1 |
| +35 | `0x0241` | Gate > Enable | 0/1 |
| +47 | `0x024D` | Comp > Enable | 0/1 |
| +53 | `0x0253` | EQ band 1 > BndEnbl | |
| +54 | `0x0254` | EQ band 1 > Freq | see scaling |
| +55 | `0x0255` | EQ band 1 > Gain | see scaling |
| +56 | `0x0256` | EQ band 1 > Q | |
| +59/+60 | `0x0259`/`0x025A` | EQ band 2 Freq / Gain | |
| +64/+65 | `0x025E`/`0x025F` | EQ band 3 Freq / Gain | |
| +69/+70 | `0x0263`/`0x0264` | EQ band 4 Freq / Gain | |

EQ bands are 5 IDs apart. See `params.csv` for the rest (HPF, gate/comp
parameters, names, etc.).

### Input send block (In 4 base = `0x1269`)

| Offset | In 4 ID | Name |
|-------:|---------|------|
| +0,+2,…,+10 | `0x1269`…`0x1273` (odd) | AuxSend1–6 |
| +1,+3,…,+11 | `0x126A`…`0x1274` (even) | AuxMute1–6 |
| +12 | `0x1275` | AuxSend7/8 |
| +14 | `0x1277` | AuxSend9/10 |
| +16…+20 | `0x1279`…`0x127D` | AuxBalance1–5 |
| +21…+24 | `0x127E`…`0x1281` | FXSend1–4 |
| +25… | `0x1282`… | DCA Asn1… |

(QSC's own XML names `0x1276` "AuxMute6" and `0x1278` "AuxMute7". Those are
probably the stereo aux mutes with sloppy labels.)

### Value scaling

All of these are in `touchmix/params.py` as `CURVES`. Every point in
`mappings/*.json` is checked against them by the tests. "lin a…b" means
`a + (b-a)*x`, and "exp a…b" means `a * (b/a)^x`, which is even steps on a log
scale. Each one is an exact fit to 21 or more read-back points on In 1–16
unless noted. Units are the likely ones for each range: the config stores
numbers only.

| Control | Curve | Notes |
|---------|-------|-------|
| Fader, every master (Main, Aux 1–10, FX 1–4, DCA 1–8), aux and FX sends | `dB = 67 * x^0.69 - 55` | 0 = −∞ (the config stores −55), 0.7512 = 0 dB (the app sends 0.75 = −0.06 dB), 1 = +12 dB |
| Trim, mono inputs | lin −15…+15 dB | |
| Trim, stereo inputs 17/18, 19/20 | lin 0…+12 dB | **different from mono** |
| Pan, Width (stereo), AuxBalance | lin −100…+100 | pan from the app is always a multiple of 0.005 |
| Delay Time | lin 0…100 (ms) | |
| EQ Freq | exp 20…20 000 Hz | also confirmed from the app captures |
| EQ Gain | lin −15…+15 dB | from captures |
| EQ Q | exp 0.4…4 | |
| Lo-Cut Freq | exp 20…2 000 Hz | |
| Hi-Cut Freq | exp 1 000…20 000 Hz | |
| Gate Thresh | lin −60…0 dB | |
| Gate Attack | exp 0.5…50 (ms) | |
| Gate Release, Gate Hold | exp 10…2 000 (ms) | |
| Gate Attenuation | lin 0…100 (dB) | |
| Gate Ratio | lin 1…100 | linear, unlike comp ratio |
| Comp Thresh | lin −60…0 dB | |
| Comp Ratio | exp 1…20 | 0.5 = 4.47:1 |
| Comp Makeup Gain | lin 0…20 dB | |
| Comp Attack | exp 0.5…50 (ms) | |
| Comp Release | exp 50…1 000 (ms) | |
| Comp Knee | switch: 0 below 0.5, 1 from 0.5 up | probably hard/soft |
| Comp Compression | lin 0…1 | the single Simple-mode knob |
| De-Esser Amount | lin 0…10 | |
| De-Esser Frequency Tune | exp 1 000…10 000 Hz | |
| Mute, Solo, Phase, Enable flags | 0 / 1 | |

Not mapped yet: processing on Main, aux and FX buses (EQ, comp, GEQ, the
anti-feedback notches), FX parameters, enum parameters (Comp Mode, EQ and cut
Type), and the stereo inputs' processing.

## Capture log

### 2026-10-21 18:30: fader probe (`mappings/mapping-probe-fader-*.json`)

`touchmix map probe Fader --channels 1-16 --steps 33` wrote 0…1 in steps of
1/32 to the In 1–16 faders, and read the dB value back from the config after
each reconnect. Every point fits `dB = 67 * x^0.69 - 55` to within 0.0001 dB,
so this is the firmware's own law. The probe restored all 16 faders afterwards.
This also confirms that **the config sent on connect reflects writes made by
earlier clients**, so `--verify` and probe readback can be trusted.
Follow-up probes the same evening: every master fader probed (Main, Aux 1–6,
FX 1–4, DCA 1–8) and the In 1–16 → Aux 1 and FX 1 sends follow the exact same
law, even though aux and FX sends have different type codes (110 and 111).
Trim is linear ±15 dB, and gate threshold is linear −60 … 0 dB. Two repeat
sweeps of the In 1–16 faders read back bit-identical values.

### 2026-10-21 18:56: `map probe-all`

All 23 remaining continuous controls in one batch, 21 points each, all
restored. Every one fits a simple law exactly (see "Value scaling"): 10 are
linear, 11 are exponential, the Aux 7/8 and 9/10 masters follow the fader law,
and Comp Knee is a switch. Two results stand out: stereo-input trim is
0…+12 dB, not ±15 dB, and gate ratio is linear while comp ratio is
exponential.

### 2026-10-20 23:39: In 4 walk-through (`PCAPdroid_20_Oct_23_39_13.pcap`)

Times are seconds from the start of the capture.

| t (s) | Action | IDs seen |
|------:|--------|----------|
| 4.6–7.5 | In 4 fader −∞ → 0 dB | `0x021E` 0 → 0.75 |
| 48.1 / 51.9 | mute / unmute | `0x021F` 1/0 (+ batch on AuxSend/AuxMute 1–6, see open questions) |
| 68.0 / 70.4 | cue / uncue | `0x0222` 1/0 |
| 83.9–99.5 | pan L → R → C | `0x0221` 0 → 1 → ~0.51 |
| 123 | EQ1 +15 dB @ 86.6 Hz | `0x0254`, `0x0255` |
| 145 | EQ2 +15 dB @ 362 Hz | `0x0259`, `0x025A` |
| 178 | EQ3 −15 dB @ 1.87 kHz (the action notes said "eq2") | `0x025E`, `0x025F` |
| 186–188 | cue/uncue on the TouchMix itself | M→A `0x0222` (tag `45ef`), `0x00D4` selected channel |
| 239 | EQ4 +15 dB @ 20 kHz | `0x0263`, `0x0264` |
| 262 / 300 | gate on / off | `0x0241` |
| 287 / 303 | comp on / off | `0x024D` |
| 316–371 | FX1–4 sends max, then off | `0x127E`–`0x1281` |
| 398–453 | Aux 1–6 sends max, then off | `0x1269`…`0x1273` |
| 472–503 | Aux 7/8, 9/10 max, then min | `0x1275`, `0x1277` |
| 512–519 | digital gain max → min | `0x022C` |
| 544–549 | delay max → min | `0x022F` (+ `0x0230`/`0x0231`, tag `9164`) |
| 559 / 573 | delay "In" on / off | `0x0232` |
| 577 / 600 | polarity on / off | `0x0220` |
| 635 | channel reset on TouchMix | M→A batch: fader 0, pan/trim/EQ gains 0.5, EQ freqs to 80/400/2k/6k |
| 662–669 | In 5 fader max → min | `0x026A` |

### 2026-10-21 00:45: In 4 / In 5 mute, pan, EQ1 gain (`PCAPdroid_21_Oct_00_45_50.pcap`)

The app reconnected at 9.9 s (`39130` → `39134`), right after the first mute.

| t (s) | Action | IDs seen |
|------:|--------|----------|
| 9.4 / 13.0 | In 4 mute on → off | `0x021F` 1/0, plus the AuxSend/AuxMute 1–6 side batch |
| 27–43 | In 4 pan L → C → R → C | `0x0221` 0 → ~0.5 → 1 → 0.51 |
| 57–74 | In 4 EQ1 gain min → max → 0 | `0x0255` 0 → 1 → ~0.49; `0x0254` freq drifts 0.18–0.21 |
| 91.9 / 93.3 | In 5 unmute → mute | `0x026B` 0/1, plus side batch on `0x1293`–`0x129E` |
| 96–106 | In 5 pan L → C → R → C | `0x026D` |
| 109–123 | In 5 EQ1 gain max → min → 0 | `0x02A1`, `0x02A0` |

Findings:

* In 5 IDs are exactly In 4 + 76, and its send block is exactly In 4's + 42.
  Both strides are now confirmed from wire data, not just from the XML.
* Mute order, from the config snapshots: In 4 started **unmuted** (muted, then
  unmuted). In 5 started **muted** (unmuted, then muted again).
* EQ gain is set by dragging a point on the EQ graph, so the band's frequency
  is sent along with it and drifts slightly. To send gain alone, write only
  the gain ID.
* The side-batch tags were `3136`/`8f24` this session versus `3136`/`9161`
  last session, so the tag is not a fixed property of the parameter.

## Open questions

1. **The `tag` field in 5.1 entries.** It's 0 for direct user actions. It's
   nonzero for derived or side-effect writes: delay feet/meters `9164`, the
   mute side batch `3136`/`9161`, and changes from the mixer's own screen
   `45ef`. It may be a source or session token. Try sending with tag 0 first.
2. **Mute side batch.** Every mute or unmute (seen on In 4 and In 5, 4 times)
   also sends AuxSend1–6 and AuxMute1–6 with value 0 and nonzero tags. Odd
   (send) IDs always carry tag `3136`. Even (mute) IDs carry a tag that
   changes per session. The sends were not actually zeroed, so these entries
   probably mean something other than "set to 0". Maybe "re-evaluate" or
   "follow mute"? A test client could check whether a plain mute with no
   batch works.
3. **2.2 session payload** `0x00019CB4`: a session ID, uptime, or build number?
4. **File-end trailer** (6 bytes): CRC32 plus something else?
5. **Poll (6.1/7.1) request contents**: which meters each entry selects.
6. **Fader / trim / send / delay curves**: set known dB values on the mixer and
   record the wire floats.

## Suggested next captures

Most of these can now be done with `touchmix map` (see the README) instead of
a phone capture:

* One signal into a channel, with the app's meter screen open, to decode 6.1/7.1.
  This still needs a capture, because the mapping assistant does not decode polls.
* ~~All input-channel curves~~ done (`map probe-all`). Still open: bus
  processing (Main/Aux/FX EQ, comp, GEQ), FX parameters, and enum parameters
  (Comp Mode, EQ and cut Type). Enums are not in the probe plan, because
  switching a mode may reset other parameters. They need a guided run instead.
* Strides on another channel and on Aux 1 / FX 1: `map run channel 9`,
  `map run bus aux1`, `map run bus fx1`.
* Scene recall, mute groups, user buttons, transport: `map run scenes`,
  `map run mutes`, `map run transport`.
* Try writing: a small client that does the hello exchange and sends one 5.1.
