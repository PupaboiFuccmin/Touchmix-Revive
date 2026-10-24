# Research: controls beyond the mixing screens

Notes on what the TouchMix control protocol can do besides mixing. Most of
this was found read-only, from the parameter dictionary and from the config
and file index the mixer sends to every client.

**Most of these are not hidden.** The official app and the mixer's own screen
have recording, scenes, presets, the RTA, pink noise and user buttons. What is
new here is driving them from a PC. Which ones the app does *not* expose has
not been checked; the likeliest candidates are Powerdown, Test screen, Demo
Enabled and the MP3 player settings.

## Non-mixing parameters

All of these are ordinary parameters (5.1 param-set), and `TouchMix.set()` can
write them. Only the recorder has been tried (see below).

| Area | Parameters (`params.csv` path) | Value now | Possible use |
|------|--------------------------------|-----------|--------------|
| Power | `Global > Misc Input > Powerdown` (`0x00C5`) | 0 | remote shutdown? |
| Scenes and presets | `Global > FileMan`: Type, Location, Number, Category, Subcategory, Channel, Name, Session, Recall, Save, Save As, Delete, **Copy To User**, **Copy To External**, Omit Levels / Outputs / Routing / Tuning, With Name, With Phantom | Name "studio", Session "Default" | scene and preset recall and storage driven entirely by parameters (see below) |
| MP3 player | `Global > Control`: Mp3 playlist, Current Song, Mp3 playback Mode; `Global > Misc Input > Playback` | | **works**, see below |
| Recording | `Global > Transport`: Transport, Song Position, Multi-track Enable, Song length, Set/Goto Locate, Busy; `2-Trk Rec > Control`: 2-Track Record Source, 2-Track Arm | Multi-track Enable 1 | **works**, see below |
| Diagnostics | `Global > Misc Input > Test screen` (switches on and off from the PC; stored as n × 1024), `Global > Control`: Free Mem, Demo Enabled, Upgrade Display, Ignored Version; `Contacted Server`, `USB present` | Free Mem 128000 | status, service modes |
| Screen | Brightness, Language, GUI Options, Pan Overlay Hide Show, Global Simple Mode, Simple Mode 0–15 | | front-panel settings |
| Analyzer | `Rta Local > Control`: Enable, Source Type, Source Inst, Used by device, Confidence; `Rta Local > Zoom`; `Global > Misc Output > RTA3 source` | Enable 1 | choose what the RTA shows |
| Test signals | `Global > Misc Output`: Pink Enable, Pink Source Type/Inst/Select, Pink Level, Dry Level; `Talkback > Control`: SigGen Enable, SigGen Fader, FuncGen type, FuncGen freq | Pink off | pink noise and test tones (loud!) |
| Tuner | `Global > Misc Output > Tuning Precision`, `Global > Pitch Correct Insert` | | |
| User buttons | `U1`–`U16 > Control`: Action, Selection, Detail, **Midi**; `> Setup`: Type, Spec 1–6, Value, Momentary, PresetName | U1 Action 7 | assign buttons, possibly MIDI out |
| Mixer follow | `Global > Control`: Follow Mixer, Following device | | the app's "follow mixer selection" |

## USB recorder: working ✅

Captured with `map run transport` (`mappings/mapping-transport-*.json`), then
replayed from the PC on 2026-10-21. The mixer reported each change back.

| Action | Write | Mixer stores |
|--------|-------|--------------|
| Arm a track | `In N > Control > Arm` (also `Stereo In 17/18`, `19/20`) = 1, disarm = 0 | 1 / 0 |
| Arm the 2-track recorder | `2-Trk Rec > Control > 2-Track Arm` = 1 | 1 |
| Record | `Global > Transport > Transport` = 2/7 | 2 |
| Play | Transport = 1/7 | 1 |
| Stop | Transport = 0 | 0 |
| Return to zero | Transport = 5/7 | 0 once done |
| Locate | `Goto Locate`, `Locate` (0–1, apparently a fraction of the song) | |

Transport is an integer state sent as n/7: 0 stop, 1 play, 2 record, 3 next,
4 previous, 5 return to zero / top of list. 6 and 7 are not seen yet. Arming a track makes the mixer itself set
that channel's `Tracking` to 2; the app does not send it. "Arm all" in the
app arms In 1–16, both stereo inputs and the 2-track recorder.

```sh
python -m touchmix record status
python -m touchmix record arm 1-8 st17
python -m touchmix record start
python -m touchmix record stop
python -m touchmix record disarm all
```

`TouchMix.arm(channel, on)` and `TouchMix.transport('record' | 'play' |
'stop' | 'rtz')` do the same from Python.

Not yet tried: locate, the unknown transport states, file names
(`Global > FileMan > Session` reads "Default"), and the MP3 player.

## USB MP3 player: working ✅

Logged with `monitor --raw --polls` while using the player on the mixer, then
replayed from the PC on 2026-10-21. The player was the first feature here
whose state did not come through as plain parameter changes: song titles
arrive as a new message type, 9.1 (see PROTOCOL.md).

| Action | Write | Notes |
|--------|-------|-------|
| MP3 mode on | `Global > Transport > Multi-track Enable` = 0 and `Global > Misc Input > Playback` = 0 | the mixer reports both when the mode is switched |
| MP3 mode off | Multi-track Enable = 1, Playback = 1/1024 | |
| Select song N | `Global > Control > Mp3 playlist` = (N-1)/1024 | selecting **stops** the player; a play sent right after it is lost, so wait about 1 s |
| Play / stop | `Global > Transport > Transport` = 1/7 / 0 | the same parameter as the recorder |
| Next / previous | Transport = 3/7 / 4/7 | the mixer stops after changing song |
| Top of list | Transport = 5/7 | the recorder's return to zero |

What the mixer reports while playing:

* `Current Song` (`0x0118`): the title, as a 9.1 text message.
* `Song length` (`0x00BB`): a u32 sample count (2,884,656 = 65.4 s at 44.1 kHz).
* `Song Position` (`0x00B9`): the fraction played, about once a second.
* `Time display` (`0x00BC`): elapsed samples as a u32, about 5 times a second.
* One poll reply shape (42 bytes) changes only while a song plays. These are
  probably the meters, and the best lead yet for decoding them.

Requirements found along the way: the drive is best formatted on the mixer
(Recording screen, Format USB); MP3s go in the root; the recorder's mode must
be MP3 Playback; and the mixer's sample rate must match the files' (QSC's
TouchMix-30 Pro manual).

```sh
python -m touchmix mp3 status
python -m touchmix mp3 mode on
python -m touchmix mp3 play 2         # select song 2 and play it
python -m touchmix mp3 next
python -m touchmix mp3 stop
python -m touchmix mp3 mode off
```

## Option labels in the config

The config carries option labels for a few menu parameters, as newline-separated
`text`. So far only the user-button menus have them:

* **Action:** Recall Input Preset, Recall Output Preset, Recall Scene, Mute,
  Go To Screen, Recording, Select, Utility
* **Selection:** Selected parameter, Clear, Pitch Correct, Miscellaneous
* **Detail:** Nav left, Nav right, Fader to 0 dB, Fader off

The lists change with the other fields, so they look like the mixer's live
menu contents. Other enums (comp mode, EQ type, FX type) carry no labels and
still need a guided run.

## The file index

`fileIndex.xml` (sent on connect) lists every scene and preset file:

| FileMan Type | Contents | Location 0 (factory) | Location 1 (user) | Location 3 |
|-------------:|----------|---------------------:|------------------:|-----------:|
| 0 | scenes (`*.scene.dqx`) | 12 | 1 | 0 |
| 1 | per-device settings (`*.settings.dqx`) | 1 | 4 | 4 |
| 2 | ? | 0 | 0 | 0 |
| 100 | input channel presets | 113 | 0 | |
| 101 | output presets (speaker models: K, KW, E, KLA series…) | 45 | 0 | |
| 102 | ? | 0 | 0 | |
| 200–201, 203–204 | ? | 0 | 0 | |
| 202 | graphic EQ curves | 6 | 0 | |
| 205 | chorus | 1 | 0 | |
| 206 | stereo delay | 3 | 0 | |
| 207 | pitch shift | 2 | 0 | |
| 208 | dense reverb | 8 | 0 | |
| 209 | lush reverb | 8 | 0 | |
| 210 | mono delay | 4 | 0 | |
| 255 | effect wizard presets | 245 | | |

Each `File` has `Path`, `Uuid`, `Category`, `Subcategory`, `Description`,
`Info` and `Version`. Location 0 is `/usr/share/ds2i/factory/…`, location 1
is `/usr/share/ds2i/user/…`. Location 3 is probably the USB drive, which
would fit `Copy To External`.

**Likely recall sequence** (unconfirmed): set `FileMan > Type` and
`Location`, pick the file (`Number`, maybe `Category`/`Subcategory`, or a
scene UUID in `Scene Uuid0–7`), then write `Recall`. The `Omit …` and
`With …` flags match the app's recall options.

## Scenes and presets: what the mixer reports

From `map tour scenes` on the mixer's own screen (2026-10-21,
`mappings/tour-scenes-20261021-234423.log`):

**Save As (new scene "TEST"):** `FileMan > Save As` = 0, then about 2 s later
`FileMan > Name` = "TEST" (as 9.1 text), `FileMan > Session` = "DEFAULT",
`FileMan > Scene Uuid0`–`7`, and a fresh `fileIndex.xml` with the new scene.

**Recall (scene, and a channel preset):** `FileMan > Recall` = 0, then about
2 s later the mixer sends **`all-config.xml.dscomp`**, the complete mixer
state after the recall, to every client. The client now treats it like the
connect-time config, so `tm.config` stays current.

**Scene IDs:** `Scene Uuid0`–`7` are the scene's UUID from the file index, as
eight little-endian u16 words, each sent as n/65536:

```
fileIndex  TEST.scene.dqx  Uuid 790091c8bc2044e880d34b355d7bbd21
wire       0x0079 0xc891 0x20bc 0xe844 0xd380 0x354b 0x7b5d 0x21bd
bytes      79 00  91 c8  bc 20  44 e8  80 d3  4b 35  5d 7b  bd 21
```

No `Type`, `Location` or `Number` changes were reported, so the mixer's own
screen picks the file internally. The likeliest client-side recall is: write
the scene's UUID to `Scene Uuid0`–`7`, then `Recall`, and expect an
`all-config` back. Untested. The app's earlier `Recall = 0` writes (without a
UUID, behind its broken confirm) did not recall anything.

The tour stopped at step 6 (saving a channel preset) when the mixer's screen
froze. The mixer restarted, and its control port changed again (to 45262).

## Scene recall from the PC: not working yet

`touchmix scene list` works: it reads the file index and marks the scene in
`FileMan > Scene Uuid0`–`7`. Recalling did not work in any of these attempts
(2026-10-22). The test: channel 16 was named SCNA, saved as scene SCNA, then
renamed SCNB from the PC. A working recall would have brought "SCNA" back.

| Attempt | Result |
|---------|--------|
| `Scene Uuid0`–`7` = target scene, then `Recall` = 0 | UUID stored (the mixer then reports that scene as current), nothing recalled, no `all-config` |
| same, `Recall` = 1 | nothing |
| same, plus `FileMan > Type` = 0, `Location` = 1 (user), `Number` = position in the file index (n/1024), `Recall` = 0 or 1 | nothing; channel 16 stayed "SCNB" |

**Why it failed (from the app's code, 2026-10-24):** a confirmed recall is not a
parameter write at all. The app sends a separate *action* message, type 8.3,
with action code 3 and the scene packed into its fields (PROTOCOL.md, "8.3
action"). The FileMan parameter writes only drive the dialog. Next step: send
an 8.3 recall from the PC with the mixer in front of you, on a scene you don't
mind loading.

What the app sends for a *confirmed* recall has never been captured: its
confirm button didn't work. Its unconfirmed `Save As` / `Recall` writes
carried tag `9229` where ours send 0, which may or may not matter. Next step:
capture a confirmed recall from the app (another device, or after an app
update) with `monitor --raw` running and PCAPdroid, then retry.

## Input EQ and dynamics buttons

From `map tour comp,eq` on channel 16 (2026-10-22,
`mappings/tour-comp-eq-20261022-000450.log`). Each button produced exactly one
change:

| Button | Parameter | Values |
|--------|-----------|--------|
| EQ "Low Shelf" | `In N > EQ > BndEnbl` of **band 1** | 0 = shelf, 1 = bell |
| EQ "High Shelf" | `BndEnbl` of **band 4** | 0 = shelf, 1 = bell |
| EQ "Low Cut" | `In N > Lo-Cut > BndEnbl` | 1 = on |
| EQ "High Cut" | `In N > Hi-Cut > BndEnbl` | 1 = on |
| "Simple" on the Comp, EQ or Gate tab | `Global > Control > Simple Mode 0` | a small integer (n/65536): 0, 4 and 12 seen, probably one bit per tab; screen only, no effect on the sound |

So on bands 1 and 4, `BndEnbl` is the bell/shelf switch, not a band on/off.
`eq N 1 shelf on` and `TouchMix.eq_shelf()` set it. The input compressor
screen has **no mode selector** (confirmed on the mixer), so the `Comp > Mode`
value the mixer reports on connect isn't a front-panel control for inputs.

## Input source and channel names

From `map tour source,names` on channel 16 (2026-10-22,
`mappings/tour-source-names-20261022-001134.log`):

* **Track / Input** (Record/Play screen): `In N > Control > Source`, 0 = live
  input, 1 = Track (the recorder). The mixer also reports
  `In N > Control > Tracking`, the channel's recorder state as n/2: 0 normal,
  1 playing from Track, 2 armed for recording (seen earlier when arming).
* **Renaming** (Setup tab > Name): the mixer reports a **9.1 text** message
  with the new name for `In N > Control > Name` (`0x05BA` on channel 16),
  plus a 5.1 `Name` = 0. **Sending the same 9.1 from the PC renames the
  channel** (tested on In 16: "in 16" → "In 16"). The mixer does not echo it
  back to the sender. `touchmix name 16 Kick` does this.

## FX processors

From `map tour fx` (2026-10-22, `mappings/tour-fx-20261022-002752.log`).
On firmware 3, touching FX 1 in the FX Masters bank opens the processor picker
directly. The choice is `FX N > Editor > FX type`, an integer sent as n/1024:

| FX type | Processor | Default preset loaded | File index preset type |
|--------:|-----------|-----------------------|-----------------------:|
| 0 | None | | |
| 1 | Chorus | Chorus Med | 205 |
| 2 | Stereo Delay | ST Med Delay | 206 |
| 3 | Pitch Shift | Pitch-Light Detune | 207 |
| 4 | Dense Reverb | D Live Plate Med | 208 |
| 5 | Lush Reverb | L Med Hall | 209 |
| 6 | Mono Delay | Mono 250 Delay | 210 |

Picking a processor makes the mixer load its default preset. The preset's name
arrives as 9.1 text on `FX N > Control > Preset`, and the processor's own
parameters update. **Writing `FX type` from the PC does the same** (tested:
FX 1 Mono Delay → Lush Reverb → Mono Delay, each confirmed by read-back).
`touchmix fx 1 type lush` and `TouchMix.fx_type()` do this.

## User buttons

From `map tour buttons` (2026-10-22, `mappings/tour-buttons-20261022-003437.log`).
U1 was assigned to mute channel 16 in Menu > User Buttons, pressed, then Reset:

| Setting | Mute In 16 | After Reset (factory) |
|---------|-----------:|----------------------:|
| `U1 > Control > Action` | 3 (Mute) | 7 (Utility) |
| `U1 > Control > Selection` | 0 | 0 (Selected parameter) |
| `U1 > Control > Detail` | 15 (channel 16) | 0 (Nav left) |
| `U1 > Control > Name` (9.1 text) | "Mute In 16" | "Nav left" |
| `U1 > Setup > Type` | 4 | 10 |

All are integers sent as n/1024, indexes into the option lists the config
carries as `text` (Action: Recall Input Preset, Recall Output Preset, Recall
Scene, Mute, Go To Screen, Recording, Select, Utility, Tap Tempo). The Setup
`Spec 1`–`6` and `Value` changed too, as packed values not decoded yet.
Pressing the physical button toggles the target (In 16 Mute) and the mixer
reports it like any other change.

**The mute side batch comes from the mixer.** Each U1 press reported
`In 16 > Control > Mute` together with `AuxSend1`–`6` = 0 and `AuxMute1`–`6`
= 0, the same batch the app sends with every mute (open question 2 in
PROTOCOL.md). So the batch is the mixer's own report of a mute, not something
a client has to send; plain mute writes work (confirmed earlier by probes).

## FX processor settings, mute groups, monitor levels

`probe-all --plan fx,extras` (2026-10-22, 52 files in `mappings/`). For each
processor, FX 1 was switched to it, its settings swept in 11 steps, and FX 1
put back on Mono Delay. All restored, all exact fits. The curves are in
`FX_CURVES` in `params.py`:

| Processor | Settings |
|-----------|----------|
| Chorus | Rate lin 0.35–5 Hz, Depth lin 0.1–3.5, Waveform steps 0–2, Delay lin 0–20 ms, HPF exp 20–2000 Hz, LPF exp 200–20000 Hz |
| Stereo Delay / Mono Delay | Delay (L/R) lin 1–1500 ms, Regen lin 0–99 %, HPF / LPF as above, Param Link and Filter Link switches |
| Pitch Shift | Shift (L/R) lin ±1300 cents, Delay (L/R) lin 0–100 ms, P.Corr. Rate lin 0–10, Coarse edit switch, Key steps 0–12, HPF / LPF |
| Dense Reverb | Rev Time exp 0.33–10 s, Initial Delay lin 0–200 ms, Hi Ratio lin 0.1–1, Room steps 1–4, Diffusion lin 0–10, Reverb Delay lin 0–100 ms, ER lin 0–100, HPF / LPF |
| Lush Reverb | Size lin 22.5–39.1, Pre Delay lin 0–200 ms, Room steps 1–5, Low / High Rev Time lin 0.2–4 s, X-over exp 100–12000 Hz, Diffusion lin 0–100, HPF / LPF |

"steps" settings are whole numbers, `lo + round(x * (hi - lo))`. Their labels
(which waveform, which key, which room) aren't known yet.

**Extras:** mute groups 1–8 `Mute` switch at 0.5 (`mute mute3 on` works). The
monitor level follows the fader law (−55…+12 dB). The **headphone level
follows the same law but to +24 dB** (`79·x^0.69 − 55`). CR and phones gain
boost are lin ±20 dB.

`touchmix fx 1 show` lists FX 1's current settings in real units, and
`touchmix fx 1 set "Rev Time" 2.5` sets one. **The set is untested on the
mixer.**

## Recorder buttons

From `map tour recorder` (2026-10-22, `mappings/tour-recorder-meters-20261022-014726.log`):

| Button | Mixer reported |
|--------|----------------|
| `|<<` | `Transport` = 5, then 0 |
| `<<` rewind | `Transport` = 4, then 0 |
| `>` play | 1 |
| `>>` fast forward | 3 |
| stop / rec | 0 / 2 |
| Set | `Locate` = play position (fraction of the session, 0.527), then `Set Locate` 1 → 0 |
| Go To | `Goto Locate` 1 → 0 |

Dragging the play pointer reported nothing. Opening "New / Recall" and
recalling the recording session reported `FileMan > Recall` = 0, the session
name, the scene UUID and the MP3 playlist (as text). `record ff`, `rew`,
`set`, `goto` and `locate F` send these. **They are untested on the mixer.**

## Meters

**Poll requests are meter requests.** After a 6-byte header (`03 00 0a 00 03
00`), each 4-byte entry `(0, k, 1, 0)` asks for **input k+1's meter, before
fader and mute**. The reply is `u16 n_bytes`, then **Main L/R** and one value
per entry, in request order, as linear amplitude (1.0 = 0 dBFS). The app's
52-byte request (`POLL_ALL`, the one the client always sent) asks for inputs
1–8. The app also sends an empty request (Main only) and one with entries
`(0, 0, 15, 0)` and `(10, 0, 15, 0)` (not decoded).

Worked out in three steps (2026-10-22/23):

1. `map tour meters`, held synth note on inputs 7/8
   (`mappings/tour-meters-20261022-195825.log`): of the 10 values, 0–1 went
   silent with the channel mutes and the Main fader and dropped with each
   channel fader (**Main L/R**). Values 8–9 ignored mutes and channel faders
   and followed only the synth's own volume. 2–7 stayed silent.
2. Changing inputs 7/8's sends to Aux 7/8 and 9/10 did not move 8–9, so they
   are not aux outputs (an earlier commit guessed that).
3. Requests sent from the PC while the synth held a note: entries 6 and 7
   alone returned −8.0 and −7.2 dBFS (inputs 7 and 8). Entries 7, 6, 15, 0
   returned −7.2, −8.0, silence, silence, so each value follows its entry. 16
   entries returned 2 + 16 values. Entries 16–23 were silent except 23
   (+9.9 dB, not an input level; unknown).

Inputs 7/8 are confirmed with sound; 1–6 read silent with nothing plugged in;
9–16 use the same numbering but had no signal to prove it. The client now
alternates two requests (inputs 1–8, inputs 9–16), as the app alternates its
own. `touchmix meters` and `TouchMix.meters()` show Main L/R and inputs 1–16.
Full synth volume read about −3 dBFS, matching the user's volume tool; the
mixer's screen shows meters as bars only.

## Pulling recordings over the network: no sign of it

Brainstorm priority 2 asked whether recorded tracks can come off the mixer
over the control connection. So far everything points to no:

* In every capture and monitor session, files only flow **mixer → client**,
  and only the three sent on connect (config, file index, the client's
  settings).
* `PCAPdroid_21_Oct_23_11_21` (2026-10-21, 116 s): the app opened the
  recording page, switched MP3 → multitrack, opened save and recall dialogs.
  `tmcap summary` found **no new message types**. The app sent only:
  `Playback` = 1/1024 + `Multi-track Enable` = 1 (the mode switch, the same
  as `mp3 mode off`), `FileMan > Save As` = 0, and `FileMan > Recall` = 0
  three times. A UI bug in the app stopped the confirm buttons working, so
  the actual recall and save were not captured. The `= 0` writes look like
  "open the dialog".
* The app never requests a listing; what it knows about files comes from
  `fileIndex.xml`, which covers scenes and presets but not recording sessions.

**The app's code agrees** (TouchMix Control 3.0.22494 for Android, see
"Inside the official app" below). In the app's native library, file data
(message type 3) is only ever *received*: the receive loop hands type-3
messages to `clientControl::rxFile`, and nothing in the app calls the
functions that pack a file for sending (`ControlMessage::packFileToData`,
`FileTransferMessage::setup`). There is no function for asking the mixer for
a file or a directory listing. The only place `.wav` appears is the file filter
for the USB player (`.wav .bwf`).

The file code itself is generic: given any path, the packer compresses the
file and sends it from `/usr/share/ds2i/tmp/`. So the firmware *could* send any
file, but only when its own code decides to; there is no request message for
it. The app's `rxFile` only acts on `config.xml`, `config.session.xml`,
`fileIndex.xml`, `.settings.dqx` and `mp3list.xml`.

**No borrowed file-transfer library either.** The app is built on JUCE, which
ships HTTP (`juce::URL`, `WebInputStream`), raw sockets and an
`InterprocessConnection` class. They are compiled in, but no QSC code calls
any of them. QSC's link uses its own socket code (`SocketMan`) and JUCE's
bundled zlib for `.dscomp`. The only other services on the mixer are the ones
the port scan found: SSH, Telnet and netperf (PROTOCOL.md).

So the app cannot fetch recordings, and nothing it sends can make the mixer
send them. The mixer's own firmware could still have a path the app never
uses, but there is no hint of one. Recordings come off the USB drive, and
`touchmix collect` copies them off with channel names (see the README).

**Recording format**, from a 9 s test recorded from the PC on 2026-10-22: mono
broadcast WAV (`JUNK`, `fmt `, `bext` with originator "MicroX", `data`), format
tag 1 (integer PCM), 32 bits, at the mixer's sample rate (44.1 kHz that day).
Read as int32 the samples are plausible input noise (about −68 dBFS); read as
float they are meaningless denormals. So despite QSC's "32-bit float", these
are 32-bit integers, and DAWs read them without conversion.

## Inside the official app

From TouchMix Control **3.0.22494** for Android (`qscaudio.touchmix.app1`),
pulled off the tablet with `adb` on 2026-10-23. The app's files themselves are
QSC's and stay out of this repository; these are our notes on them.

* **The Java side is a thin shell** (about 1,700 lines in 13 classes). It finds
  mixers with mDNS (jmDNS, or Android's NSD), shows the screens, and passes
  every event into a native library. It has no networking or protocol code.
* **The screens are web pages.** They live in `assets/offline-assets.zip`
  (TM16 and TM8 page sets) and talk to the native code through a JavaScript
  bridge called `GuiLink`. The mixer has the same pages under
  `/usr/share/ds2i/html/`.
* **Everything else is `libTouchMix.so`** (arm64, 7.4 MB, C++ on JUCE). It is
  stripped but still exports about 19,000 symbols with their C++ names, so
  classes and functions are readable. It contains the mixer's own code too:
  the server half of the link (`controlLink::serverThreadBody`), the file
  manager, the DSP blocks (`GateProc`, `BiquadProc`, `PitchShifter`, the
  reverbs) and the screen logic, so the app and the mixer appear to be built
  from one code base.

Names that line up with the protocol (see PROTOCOL.md):

| In the app | What it matches |
|------------|-----------------|
| `controlLink`: `sendHelloMessage`, `parseHelloMessage`, `parseLinkAcceptedMessage`, `txMessage`, `processMessages` | 1.3 hello and the handshake |
| `EditMessage(int, float)`, `ForceSingleEditMessage(int, float)`, `BlockEditMessage` | 5.1 param-set (single and block edits) |
| `StringParmMessage(int, const char*)` | text parameter writes (9.1 is the mixer's side) |
| `MeterRequestMessage`, `MeterResponseMessage`, `clientControl::requestMeters`, `MeterMan::formMeterId(u8, u8, int)` / `decodeMeterId` | 6.1 / 7.1 poll |
| `clientControl::rxFile`, `ControlMessage::getFileFromData` | 3.4 file transfer (receive only) |
| `ActionMessage(u16, u32, float, const char*)`, `clientControl::handleActionMessage` | 8.3 action: scene/preset save, recall, delete, copy (never captured) |
| `clientControl::handleSetAuthorization`, `rxDeviceAuthorizations` | per-client access |
| `FileActionClient::transmitFileAction(int, int, const char*)`, `fileMan::recall*` | scene and preset recall |
| `clientControl::runProductionTestCommand`, `loadProductionTestPreset`, `parameterEditFromProductionTest`, `simulateHardwareMessage` | a factory-test interface |

The library also names the mixer's settings files under `/usr/share/ds2i/`
(network, language, demo mode and so on) and the preset file extensions
(`.scene.dqx`, `.input.dqx`, `.comp.dqx`, `.routing.dqx`, ...).

Scene and preset actions turned out to be `ActionMessage`s (type 8.3), now in
PROTOCOL.md. The source paths left in the library show QSC's internal names:
the platform is "ds2i", the project "Sonora", and this is the `tm16-v3`
branch.

Still to read in the code: the exact `text` fields of a file action, what the
mixer-to-app action codes mean, what `transmitFileAction` is used for, and
every meter ID `formMeterId` can build.

## Next steps

In order of risk:

1. ~~Recorder transport~~ done (above).
2. **Guided capture of one scene recall** (`map run scenes`, or `map run
   free`): recall a scene in the app and record exactly which FileMan
   parameters it writes, and in what order. Read-only on our side.
3. **Same for a channel-preset recall and a USB export.** Read-only.
4. **Replay a recorded recall** from the PC, on a scene you don't mind
   loading.
5. **Poll requests (6.1).** The app sends three request shapes. Other
   selector values may return the meters and RTA, or other data. They are
   requests for data, but unknown request bodies could upset the mixer, so
   try them one at a time.
6. **Diagnostics** (Test screen, Powerdown): only with someone at the mixer.
