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

A request could still hide behind a confirm button nobody has pressed in a
capture yet. Until one shows up, recordings come off the USB drive, and
`touchmix collect` copies them off with channel names (see the README).

**Recording format**, from a 9 s test recorded from the PC on 2026-10-22: mono
broadcast WAV (`JUNK`, `fmt `, `bext` with originator "MicroX", `data`), format
tag 1 (integer PCM), 32 bits, at the mixer's sample rate (44.1 kHz that day).
Read as int32 the samples are plausible input noise (about −68 dBFS); read as
float they are meaningless denormals. So despite QSC's "32-bit float", these
are 32-bit integers, and DAWs read them without conversion.

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
