# Revive command map

A roadmap of everything the **stock network protocol** can drive — what the
client already does, what is reachable today but not yet wrapped in a friendly
command, and what still needs a capture or decode. This is the no-root side: it
works on **any** TouchMix over Wi-Fi, gated only by the mixer's per-client write
grant (reads never need it). The on-device / rooted ideas live in the private
`TouchMix-Root-Research` repo instead.

Everything here is grounded in the 5,694-entry parameter dictionary
([`params.csv`](../touchmix/data/params.csv)) and the protocol notes
([PROTOCOL.md](PROTOCOL.md), [RESEARCH.md](RESEARCH.md)).

**Status legend**

- ✅ **done** — a command and/or `TouchMix` method exists
- 🟡 **reachable now** — writable today with `set` / `param`, no friendly command yet
- 🔬 **needs work** — needs a capture, probe, or decode before it is reliable
- 💡 **client-side** — new PC-side code, not a single parameter
- ⚠️ **risky / guarded** — can take the mixer off the network, lock clients out, power it down, or delete data (behind `set --force`)

---

## 1. Channel strip — inputs 1–16 and stereo inputs 17/18, 19/20

| Area | Params (`params.csv`) | Now | Command idea |
|------|-----------------------|-----|--------------|
| Fader / Mute / Solo | `Control > Fader/Mute/Solo` | ✅ | `fader`, `mute`, `solo` |
| Pan / Width | `Control > Pan/Width` | ✅/🟡 | `pan`; add `width` for stereo |
| Trim (digital gain) | `Control > Trim` | ✅ | `trim` |
| Phase / polarity | `Control > Phase` | 🟡 | `phase N on` |
| Delay | `Control > Delay Time/Enable` | 🟡 | `delay N 12ms`, `delay N off` |
| Source / tracking | `Control > Source/Tracking` | 🟡 | `source N live|track` (RESEARCH.md) |
| Channel safe | `Control > Channel Safe` | 🟡 | `safe N on` |
| EQ (4-band) | `EQ > Freq/Gain/Q/BndEnbl/FxEnbl` | ✅/🟡 | `eq` does gain/freq/shelf; add `eq N band Q`, band on/off |
| Lo-Cut / Hi-Cut | `Lo-Cut`, `Hi-Cut > Freq/BndEnbl` | 🟡 | `locut N 80`, `hicut N off` |
| Gate | `Gate > Enable/Thresh/Attack/Release/Hold/Ratio/Attenuation` | ✅/🟡 | `gate` does enable+thresh; add the rest via `param` or a `gate N attack …` |
| Compressor | `Comp > Enable/Thresh/Ratio/Attack/Release/Makeup Gain/Knee/Auto/Compression` | 🟡 | a `comp` command (all reachable via `param` today) |
| De-Esser | `De-Esser > Enable/Amount/Frequency Tune` | 🟡 | `deess N on 5` |
| Sends to aux / FX | `Control > AuxSend*/AuxMute*/AuxBalance*/FXSend*` | ✅ | `send` |
| DCA / mute-group assign | `Control > DCA Asn*/Mute Asn*` | 🟡 | `assign N dca3 on`, `assign N mute2 on` |
| Rename | `Control > Name` (9.1 text) | ✅ | `name` |

The input main block is 76 IDs per channel, the send block 42 — both confirmed
on the wire, so any of the above generalizes to all 16 inputs and the stereo
inputs with the strides in PROTOCOL.md.

## 2. Buses — Main and Aux 1–10

| Area | Params | Now | Command idea |
|------|--------|-----|--------------|
| Master fader / gain | `Control > Fader/Gain` | ✅ | `fader main`, `fader aux1` |
| 6-band EQ | `EQ > …` (bands 1–6) | ✅/🟡 | `eq` (bands 1–6 on buses) |
| 31-band graphic EQ | `GEQ > #` (Main, Aux 1–6) | 🟡 | `geq aux1 1000 -6` (by band name) |
| Anti-feedback | `Anti FB > Freq/Gain/Q/Depth/Width/FxEnbl` | 🟡 | `antifb aux1 …` |
| Comp / Lo-Cut / Hi-Cut | as inputs, different ranges | 🟡 | shared `comp` / `locut` / `hicut` |
| Delay | `Control > Delay Time` | 🟡 | `delay main 8ms` |
| Aux pre/post | `Control > Pre/Post` | 🔬 | meaning of the stored value unconfirmed |

## 3. FX returns 1–4 and their processors

| Area | Params | Now | Command idea |
|------|--------|-----|--------------|
| Pick processor | `Editor > FX type` | ✅ | `fx N type lush` |
| Processor settings | per-processor sections | ✅ | `fx N show` / `fx N set "Rev Time" 2.5` |
| Return EQ (2-band) / HPF | `EQ`, `HPF` | 🟡 | `eq fxN …` |
| Return sends / balance | `Control > Width/AuxBalance*` | 🟡 | `send fxN …` |

Processor curves are probed (`FX_CURVES`); enum labels (waveform, key, room)
are still numbers — a `map tour` would name them.

## 4. DCAs, mute groups, monitor and talkback

| Area | Params | Now | Command idea |
|------|--------|-----|--------------|
| DCA 1–8 faders / mutes | `DCA N > Control` | ✅/🟡 | `fader dca1`, `mute dca1` |
| Mute groups 1–8 | `Mute N > Control > Mute` | ✅ | `mute mute3 on` |
| Monitor / phones level | `Global > Control > Monitor / Cue Level`, `Phones / Cue Level` | 🟡 | `monitor -10db`, `phones -6db` |
| CR / phones gain boost | `Global > Control > CR/Phones gain boost` | 🟡 | fold into the above |
| Talkback | `Talkback > Control > Talback Enable/TB Fader/Assign*` | 🟡 | `talkback on`, `talkback assign aux1` |
| Signal / function generator | `Talkback > Control > SigGen*/FuncGen*` | ✅ | `siggen` |

## 5. Generators, analyzer, tuner

| Area | Params | Now | Command idea |
|------|--------|-----|--------------|
| Pink noise | `Global > Misc Output > Pink Enable/Level/Source*/Dry Level` | ✅ | `pink` |
| Signal generator | `Talkback > Control` | ✅ | `siggen` |
| RTA source | `Global > Misc Output > RTA3 source`; `Rta Local > Control` | ✅/🟡 | `rta source N`; add `rta on`, source-type select |
| Tuner precision | `Global > Misc Output > Tuning Precision` | ✅ | `tuner N` |
| Meters | poll replies (6.1/7.1) | ✅ | `meters` (Main + inputs 1–16) |
| RTA spectrum stream | poll request shapes `(0,0,15,0)` / `(10,0,15,0)` | 🔬 | decode the analyzer bands → `rta show` |

## 6. Scenes, presets and files (`Global > FileMan`)

| Action | Mechanism | Now | Command idea |
|--------|-----------|-----|--------------|
| List scenes | `fileIndex.xml` | ✅ | `scene list` |
| Recall scene | 8.3 action message | ✅ | `scene recall NAME` |
| Save / Save As scene | `FileMan > Save/Save As` + name | 🔬 | `scene save NAME` — capture a confirmed save first |
| Delete scene/preset | `FileMan > Delete` | ⚠️🔬 | guarded; needs the confirmed sequence |
| Channel / output preset recall | `FileMan > Type/Location/Number` + 8.3 | 🔬 | `preset recall …` — the 8.3 enum beyond scenes |
| Copy to user / external (USB) | `FileMan > Copy To User/External` | 🔬 | `preset copy …` |
| Session recall mode | `Global > Misc Input > Session recall mode` | 🟡 | option flag |

The **8.3 action message** is the key lead: scene recall is almost certainly
just one action in an enum the app's `libTouchMix.so` defines. Decoding the rest
unlocks preset recall, save and copy — the biggest gap in the map.

## 7. Recorder and MP3 player

| Area | Params | Now |
|------|--------|-----|
| Transport (rec/play/stop/rtz/ff/rew) | `Global > Transport > Transport` | ✅ |
| Arm tracks / 2-track | `In N > Control > Arm`, `2-Trk Rec > Control > 2-Track Arm` | ✅ |
| Locate / Set / Go To | `Global > Transport > Locate/Set Locate/Goto Locate` | 🟡 (untested on a mixer) |
| 2-track record source | `2-Trk Rec > Control > 2-Track Record Source` | 🟡 |
| MP3 mode / select / transport | `Global > Transport`, `Global > Control > Mp3 playlist` | ✅ |
| Pull recordings over the network | — | 💡 only via the rooted SFTP collector; stock protocol never moves audio |

## 8. Global, system and front-panel

| Area | Params | Now | Command idea |
|------|--------|-----|--------------|
| Screen brightness / language | `Global > Misc Input > Brightness/Language` | ✅ | `screen` |
| Follow-mixer selection | `Global > Control > Follow Mixer/Following device` | ✅ | `follow` |
| Powerdown | `Global > Misc Input > Powerdown` | ✅ | `power off` (confirmed) |
| Service / status read | versions, Free Mem, Demo, USB, Wi-Fi | ✅ | `diag` |
| Simple mode flags | `Global > Control > Global Simple Mode/Simple Mode 0–15` | 🟡 | per-tab Simple view |
| GUI options / pan overlay | `Global > Misc Input > GUI Options/Pan Overlay Hide Show` | 🟡 | UI prefs |
| Clear clip / solo / mute | `Global > Misc Input > Clear clip/Clear solo/Clear mute` | 🟡 | `clear clip|solo|mute` (momentary) |
| Sample rate | `Global > Misc Input > Sample Rate` | ⚠️🟡 | disruptive mid-session; guard it |
| Pitch-correct insert | `Global > Pitch Correct Insert` | ✅ | `pitch` |
| Network / Wi-Fi / passwords | `Global > Control` network set | ⚠️ | guarded — can take the mixer off the net |

## 9. User buttons (U1–U16)

| Area | Params | Now | Command idea |
|------|--------|-----|--------------|
| Assign action | `U N > Control > Action/Selection/Detail/Name` | 🟡 | `button 1 mute 16` |
| Setup packing | `U N > Setup > Type/Spec 1–6/Value/Momentary/PresetName` | 🔬 | decode the `Spec` packing |
| MIDI out? | `U N > Control > Midi` | ❓🔬 | could the mixer emit MIDI? worth a capture |

## 10. Status / telemetry (read-only)

`Wireless Strength`, `USB present`, `Network connected/name`, `Free Mem`,
`Contacted Server`, plus the player's `Song Position/length`, `Time display` and
`Current Song`. Today `info` / `diag` / `meters` surface some; a `watch` command
could stream them (💡).

## 11. Higher-level, client-side (💡 — new PC code, not single params)

These are where "Revive" becomes more than a remote control:

- **Snapshot / restore all** — pull the full config, save it, and push selected
  parameters back (a PC-side scene/backup that works without the mixer's own
  file system).
- **Config diff** — compare two pulled configs (what changed between shows).
- **Channel copy** — copy one input's whole strip to another (strides are known).
- **Scene import / export** — read and write `.scene.dqx` files from the PC
  (format partly known; pairs with the rooted SFTP push).
- **Macros / cue lists** — a sequence of commands fired on a key or a timer
  (timed mutes, auto-recall, show control).
- **Automation / watch** — react to meters or status (auto-arm on signal, etc.).
- **OSC / MIDI bridge** — map OSC or MIDI to protocol calls for DAW / lighting /
  Stream Deck.
- **Web / GUI control surface** — a browser or desktop front end over the client
  (especially valuable for a unit with a dead touchscreen).
- **Multi-mixer** — discover and drive several mixers at once.

## Priorities

1. **Decode the 8.3 action enum** (scene save, preset recall/copy) — the
   biggest stock-protocol gap, pure capture work.
2. **Wrap the reachable processing** (comp, de-esser, lo/hi-cut, delay, GEQ,
   anti-FB) into friendly commands — all 🟡 today via `param`.
3. **`map tour generators`** to name the generator/pitch enums (turns the new
   commands from indexes into real units).
4. **RTA spectrum decode** and a `watch`/telemetry command.
5. The client-side workflow tools (snapshot/restore, macros, OSC bridge, web UI).
