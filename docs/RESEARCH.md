# Research: controls beyond the app

Notes from looking for things the TouchMix control protocol can do that the
official app doesn't obviously expose. Everything here was found read-only:
from the parameter dictionary, and from the config and file index the mixer
sends to every client. Nothing in this file has been written to the mixer yet.

## Parameters with no obvious place in the app

All of these are ordinary parameters (5.1 param-set). The client can already
write them with `TouchMix.set()`, and none have been tried.

| Area | Parameters (`params.csv` path) | Value now | Possible use |
|------|--------------------------------|-----------|--------------|
| Power | `Global > Misc Input > Powerdown` (`0x00C5`) | 0 | remote shutdown? |
| Scenes and presets | `Global > FileMan`: Type, Location, Number, Category, Subcategory, Channel, Name, Session, Recall, Save, Save As, Delete, **Copy To User**, **Copy To External**, Omit Levels / Outputs / Routing / Tuning, With Name, With Phantom | Name "studio", Session "Default" | scene and preset recall and storage driven entirely by parameters (see below) |
| MP3 player | `Global > Control`: Mp3 playlist, Current Song, Mp3 playback Mode; `Global > Misc Input > Playback` | "No songs available" | USB music playback |
| Recording | `Global > Transport`: Transport, Song Position, Multi-track Enable, Song length, Set/Goto Locate, Busy; `2-Trk Rec > Control`: 2-Track Record Source, 2-Track Arm | Multi-track Enable 1 | **works**, see below |
| Diagnostics | `Global > Misc Input > Test screen`, `Global > Control`: Free Mem, Demo Enabled, Upgrade Display, Ignored Version; `Contacted Server`, `USB present` | Free Mem 128000 | status, service modes |
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

Transport is an integer state sent as n/7. 3, 4, 6 and 7 are not seen yet
(pause, fast-forward, rewind?). Arming a track makes the mixer itself set
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
