# TouchMix-16 Control Protocol

Reverse-engineering notes for the QSC TouchMix-16 Wi-Fi control protocol, from
PCAPdroid captures of the official TouchMix-8/16 Control Android app.

**Last updated:** 2026-10-21 (2 captures, 102 probe results in `mappings/`)
**Firmware seen:** config `Version="3.0.9551"`, product codename `MicroX16`

Status legend: ✅ confirmed (captures or probes) · 🟡 strong guess · ❓ unknown

## Transport ✅

| Item | Value |
|------|-------|
| Transport | TCP |
| Mixer port | **not fixed**: 42217 in every capture and session until a reboot, then 54596. Advertised over mDNS (below) |
| Mixer address (Wi-Fi AP mode) | 192.168.1.1 |
| Byte order | Little-endian throughout |
| Mixer OS | Linux (file paths under `/usr/share/ds2i/`) |

### Discovery: mDNS / DNS-SD ✅

The mixer advertises its control port as the DNS-SD service `_ds2inet._tcp`
(ds2i is QSC's name for the platform; see the file paths below). This is how
the official app finds it on a shared network:

```
_ds2inet._tcp.local              PTR  QSC TouchMix._ds2inet._tcp.local
QSC TouchMix._ds2inet._tcp.local  SRV  port 54596, target QSC TouchMix.local
QSC TouchMix._ds2inet._tcp.local  TXT  ProtocolVersion=1 SWVersion=196608 DeviceType=MicroX16
QSC TouchMix.local                A    192.168.10.129
```

`SWVersion=196608` is `0x30000`, i.e. 3.0.0. The instance name is the mixer's
configured name. It also advertises `_workstation`, `_ssh` and `_sftp-ssh`.
`touchmix.discover` sends a single PTR query to 224.0.0.251:5353 and reads the
answers.

The port changed from 42217 to 54596 when the mixer rebooted, so it is
probably picked at startup. A client must look it up and not hard-code it.

A port scan of the mixer (on the home network) found TCP 22 (SSH), 23 (Telnet),
12865 (netperf) and the control port open. Nothing has been tried on them.

### Connection notes

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
| 9.1 | text | M→A | a text parameter's new value, e.g. the MP3 title |

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

* Mixer: name `QSC TouchMix` (the mixer's configured name) + suffix `MicroX16`.
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
| `/usr/share/ds2i/tmp/NH<n>-config.xml.dscomp` | ~516 KB | **The full current mixer state, with every parameter's name and index** |
| `/usr/share/ds2i/tmp/fileIndex.xml.dscomp` | ~243 KB | List of factory and user scenes and presets |
| `/usr/share/ds2i/user/settings/<CLIENT>.settings.dqx.dscomp` | ~2 KB | Per-client access rights (see "Per-client access") |

The number in the config's name varies: `NH4` in the first captures and
runs, `NH8` on later connections the same evening. Match `NH<n>-config`.

The app builds its initial UI from `NH<n>-config.xml`, where values are in
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
* **Keep messages small.** One message with 196 entries (every GEQ slider on
  Main and Aux 1–6) made the mixer stop answering and reboot. Messages of 16
  entries have never caused trouble. The client splits larger writes.

### 9.1 text ✅

```
u16 param_id
u16 0               (always 0 so far)
u16 len
char text[len]      (no terminator)
```

Seen when the MP3 player changes song: `18 01 00 00 12 00` + `ERN FULL SONG
(tw)`, i.e. `0x0118 Global > Control > Current Song`, and when a channel is
renamed on the mixer (`In N > Control > Name`, together with a 5.1 `Name` = 0).

**A client can send 9.1 too.** `ba 05 00 00 05 00` + `In 16` renamed input 16
(2026-10-22). The mixer stored it, and the config on the next connect showed
the new name. It sent nothing back to the sender. `TouchMix.set_text()` and
`rename()` use this.

9.9 has the same layout. It has been seen for `Rta Local > Used by device`
(the mixer's own name).

### Integer parameters ✅

Some parameters hold small integers (the MP3 playlist index, `Playback`,
`Test screen`, aux `Pre/Post`). On the wire they are n/1024, and the config
shows the wire value × 1024. `Transport` is the exception: n/7.

### 6.1 / 7.1 poll 🟡

```
6.1: u32 seq, u32 n_bytes, u8 request[n_bytes]
7.1: u32 seq, u32 n_bytes, u8 reply[n_bytes]     (array of f32)
```

The app cycles through three request shapes (24/32/52-byte messages), one
roughly every 80 ms, and each reply echoes `seq`. **The replies are meters.**
Requests: a 6-byte header `03 00 0a 00 03 00`, then 4-byte entries.
`(0, k, 1, 0)` asks for input k+1's meter (before fader and mute). Replies:
Main L/R, then one value per entry in request order, as linear amplitude.
The 52-byte request asks for inputs 1–8. See RESEARCH.md, "Meters". The
app's `(0, 0, 15, 0)` / `(10, 0, 15, 0)` request is not decoded.

## Per-client access ✅

The mixer saves a profile for each client name it sees, at
`/usr/share/ds2i/user/settings/<NAME IN CAPS>.settings.dqx`, and sends it
after the config. Its `Access` effect has one flag per area, matching the
`Auth > Access` parameters (`0x0DE0`–`0x0DF4`): `All`, `Security Settings`,
`Main Mix`, `Aux 1`…`Aux 9/10`, `Record`, `User Buttons`, plus several
`Locked` entries.

**A new client gets every flag at 0.** It still receives the full config and
all live changes, but the mixer **silently drops its 5.1 writes**. No error
comes back. Confirmed 2026-10-21: a probe from `Revive-MyPC`
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
`Wired Net Status`. On a shared network the app finds the mixer over mDNS (see
"Discovery").

## Parameter IDs ✅

**The parameter ID on the wire is the `Index` attribute in `NH<n>-config.xml`.**
Probes confirmed this on every channel they touched: they write by table index
and read the same index back.
[`params.csv`](../touchmix/data/params.csv) lists all 5,694 parameters: index, path, name and
type code. Rebuild it from any capture with `tools/tmcap.py files` followed by
`tools/tmcap.py dict`. Add `--values` to include the current values (engineering
units) and text fields, but **don't commit that version**; see the security note.

> ⚠️ **Security note:** `NH<n>-config.xml` contains the mixer's Wi-Fi network
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

Every curve below was measured with `touchmix map probe` (see
[MAPPING.md](MAPPING.md)): write 0…1, reconnect, read the value back from the
config. Each is an exact fit, to float rounding, of 21 or more points on every
channel listed. The measurements are in [`mappings/`](../mappings), the curves
are in [`touchmix/params.py`](../touchmix/params.py), and the tests check one
against the other.

"lin a…b" means `a + (b-a)*x`. "exp a…b" means `a * (b/a)^x`, which is even
steps on a log scale. Units are the likely ones for each range: the config
stores bare numbers.

**Faders and sends** (every channel type)

| Control | Curve |
|---------|-------|
| Fader on every channel and master (In, Stereo, Main, Aux 1–10, FX 1–4, DCA 1–8, Talkback `TB Fader`), and input → aux / FX sends | `dB = 67 * x^0.69 - 55`. 0 = −∞ (the config stores −55), 0.7512 = 0 dB (the app sends 0.75 = −0.06 dB), 1 = +12 dB |
| Every on/off flag probed: Mute, Solo, enables, DCA and mute-group assignment, aux-send mutes, Channel Safe, record arm, Comp Auto | switch: off below 0.5, on from 0.5 |

**Inputs 1–16 and stereo inputs 17/18, 19/20**

| Control | Curve | Notes |
|---------|-------|-------|
| Trim | lin −15…+15 dB | stereo inputs: lin **0…+12 dB** |
| Pan, Width, AuxBalance | lin −100…+100 | pan from the app is always a multiple of 0.005 |
| Delay Time | lin 0…100 (ms) | |
| EQ Freq | exp 20…20 000 Hz | matches the app captures |
| EQ Gain | lin −15…+15 dB | |
| EQ Q | exp 0.4…4 | |
| Lo-Cut Freq | exp 20…2 000 Hz | |
| Hi-Cut Freq | exp 1 000…20 000 Hz | |
| Gate Thresh | lin −60…0 dB | |
| Gate Attack | exp 0.5…50 (ms) | |
| Gate Release, Hold | exp 10…2 000 (ms) | |
| Gate Attenuation | lin 0…100 (dB) | |
| Gate Ratio | lin 1…100 | linear, unlike the comp ratio |
| Comp Thresh | lin −60…0 dB | |
| Comp Ratio | exp 1…20 | 0.5 = 4.47:1 |
| Comp Makeup Gain | lin 0…20 dB | |
| Comp Attack | exp 0.5…50 (ms) | |
| Comp Release | exp 50…1 000 (ms) | |
| Comp Knee | switch | probably hard/soft |
| Comp Compression | lin 0…1 | the single Simple-mode knob |
| De-Esser Amount | lin 0…10 | |
| De-Esser Frequency Tune | exp 1 000…10 000 Hz | |

**Main and Aux 1–10**

| Control | Curve | Notes |
|---------|-------|-------|
| Control Gain | lin −10…+20 dB | output gain |
| Delay Time | lin 0…100 (ms) | |
| EQ Freq / Gain / Q (6 bands) | as on inputs | bands 1 and 6 probed |
| Lo-Cut Freq | exp 20…2 000 Hz | |
| Hi-Cut Freq | exp **50**…20 000 Hz | wider than on inputs |
| Comp Thresh | lin −60…0 dB | |
| Comp Ratio | exp **5…100** | not 1…20 as on inputs |
| Comp Attack | `25 * (100^x - 1) / 99`: **0…25 ms** | an exponential that starts at 0 |
| Comp Release | exp 50…1 000 (ms) | |
| Comp Compression | lin 0…1 | |
| GEQ sliders (31 bands; Main, Aux 1–6) | lin −15…+15 dB | all 196 sliders probed |
| Anti FB Freq | exp 20…20 000 Hz | feedback notches; Main, Aux 1–6 |
| Anti FB Gain | lin −20…0 dB | |
| Anti FB Q | exp 3…14 | |
| Anti FB Depth | lin 0…100 | |
| Anti FB Width | lin 10…30 | |
| Pre/Post (Aux) | config shows wire × 1024 | Aux 1–6 read 0, Aux 7/8 and 9/10 read 1; meaning unconfirmed |

**FX 1–4 returns, 2-track playback, talkback**

| Control | Curve |
|---------|-------|
| FX EQ Freq / Gain / Q (2 bands) | as on inputs |
| FX HPF Freq, 2-Trk HPF Freq | exp 20…2 000 Hz |
| FX Width, AuxBalance | lin −100…+100 |
| 2-Trk Digital Trim | lin −12…0 dB |
| 2-Trk EQ Freq / Gain / Q | as on inputs |

## Capture log

Newest first.

### 2026-10-21 from 19:28: `map probe-all --plan buses,flags`

Bus processing (Main, Aux 1–10, FX returns, stereo inputs, 2-track,
talkback) and 24 on/off flags, 71 controls. Everything fit exactly (see
"Value scaling"). Buses differ from inputs in three places: hi-cut range,
comp ratio range, and comp attack, the only curve so far that starts at 0.

Step 16 wrote all 196 GEQ sliders in one 5.1 message. The mixer stopped
answering and rebooted. The sliders read 0 dB afterwards, as they had before,
so either the write never applied or the reboot reloaded saved state. After the
reboot:

* the control port had moved from 42217 to 54596, which led to finding the
  mDNS advertisement above;
* the config was named `NH8-config.xml.dscomp` instead of `NH4-…` (this had
  also happened once earlier that evening without a reboot).

The run resumed from step 16 with at most 16 entries per message and finished
without trouble.

### 2026-10-21 18:56: `map probe-all`

All 23 remaining continuous controls in one batch, 21 points each, all
restored. Every one fits a simple law exactly (see "Value scaling"): 10 are
linear, 11 are exponential, the Aux 7/8 and 9/10 masters follow the fader law,
and Comp Knee is a switch. Two results stand out: stereo-input trim is
0…+12 dB, not ±15 dB, and gate ratio is linear while comp ratio is
exponential.

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

## Open questions

1. **The `tag` field in 5.1 entries.** It's 0 for direct user actions. It's
   nonzero for derived or side-effect writes: delay feet/meters `9164`, the
   mute side batch `3136`/`9161`, and changes from the mixer's own screen
   `45ef`. It may be a source or session token. Try sending with tag 0 first.
2. **Mute side batch.** Every mute or unmute from the app (seen on In 4 and
   In 5, 4 times) also sends AuxSend1–6 and AuxMute1–6 with value 0 and
   nonzero tags. Odd (send) IDs always carry tag `3136`. Even (mute) IDs carry
   a tag that changes per session. The sends were not actually zeroed, so
   these entries probably mean something other than "set to 0". Plain writes
   with tag 0 do stick: probes muted and unmuted Main, Aux, FX and DCA, plus
   the aux-send mutes, and the config read back each change. **The mixer
   emits the same batch itself** when a user button mutes a channel
   (RESEARCH.md, "User buttons"), so it reports a mute and is not a
   requirement for clients. Its exact meaning is still unknown.
3. **2.2 session payload** `0x00019CB4`: a session ID, uptime, or build number?
4. **File-end trailer** (6 bytes): CRC32 plus something else?
5. **Poll (6.1/7.1) request contents**: which meters each entry selects.
6. **The `NH<n>` number** in the config file name (4, then 8): a session or
   slot counter?
7. **How the control port is chosen** at startup (42217 until a reboot, then 54596).
8. **One session per PC?** A second connection opened while the first was
   still up got the hello but never the config.
9. **Aux Pre/Post** is stored as an integer ×1024. What do 0 and 1 mean, and
   are there other values?

## Still to map

See [MAPPING.md](MAPPING.md#whats-left) for the full list. The ones that need
a phone capture and not a probe:

* **Meters / RTA:** one signal into a channel, with the app's meter screen
  open, to decode the 6.1/7.1 poll replies.
* **Scenes, transport, user buttons:** what the app sends for a scene recall,
  record/play/stop, or a user-button press. `map run scenes` and
  `map run transport` record the parameter side of these; a capture would
  show any other message types.
