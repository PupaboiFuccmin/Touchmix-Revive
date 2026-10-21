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
| `/usr/share/ds2i/user/settings/<CLIENT>.settings.dqx.dscomp` | ~2 KB | Per-client access rights (which mixes this tablet may control) |

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

## Parameter IDs ✅

**The parameter ID on the wire is the `Index` attribute in `NH4-config.xml`.**
[`params.csv`](params.csv) lists all 5,694 parameters: index, path, name and
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
| +0 | `0x021E` | Fader | 0–1; **0.75 ≈ 0 dB** |
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

| Control | Normalized → real | Evidence |
|---------|-------------------|----------|
| EQ frequency | `Hz = 20 * 1000^x` (log, 20 Hz–20 kHz) | ✅ 0.2125→86.6 Hz, 0.419→362 Hz, 0.657→1.87 kHz, 1.0→20 kHz; reset defaults 0.2007/0.4337/0.6667/0.8257 = 80/400/2k/6k Hz |
| EQ gain | `dB = 30*x - 15` (±15 dB) | ✅ endpoints, 0.5 after reset |
| Pan | 0 L / 0.5 C / 1 R, always a multiple of 0.005 (201 steps) | ✅ |
| Fader | 0 = −∞ (the XML stores this as −55), 0.75 ≈ 0 dB, 1.0 = max | 🟡 curve between points unknown |
| Trim, sends, delay | 0–1 | ❓ curves unknown |

## Capture log

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

* One signal into a channel, with the app's meter screen open, to decode 6.1/7.1.
* Fader at known positions (+10, 0, −10, −20, −40 dB) to fit the fader curve.
* The same walk-through on a different channel (e.g. In 9) and on Aux 1 / FX 1,
  to confirm strides from data and not only from the XML.
* Scene recall, mute groups, DCAs and the FX parameters.
* Try writing: a small client that does the hello exchange and sends one 5.1.
