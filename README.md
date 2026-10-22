# Touchmix-Revive

Control a **QSC TouchMix-16** from a PC over the network. QSC publishes no API,
so this project reverse engineered the protocol the official TouchMix Control
app uses, and mapped what the mixer's controls mean in real units.

* Every fader, mute, solo, pan, send, trim, EQ, gate, compressor, de-esser,
  graphic EQ and feedback notch on the inputs, buses and FX returns, in dB, Hz
  and ms
* Finds the mixer on your network by itself
* Watches everything that changes on the mixer, live, in real units
* A Python library and a command-line tool, both pure standard library
  (Python 3.8+, nothing to install)

> ⚠️ **This sends real commands to a live mixer.** Keep the amps down or the
> outputs muted until you trust what a command does.

| Where | What |
|-------|------|
| [`touchmix/`](touchmix) | The client library and the `python -m touchmix` command line |
| [`docs/PYTHON.md`](docs/PYTHON.md) | Using it from Python |
| [`docs/MAPPING.md`](docs/MAPPING.md) | The mapping tools that measured every curve, and how to extend the map |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | The wire protocol, parameter layout, value scaling, and the capture log |
| [`touchmix/data/params.csv`](touchmix/data/params.csv) | All 5,694 parameter IDs with names, from the mixer's own config |
| [`mappings/`](mappings) | The raw measurements behind every curve |
| [`tools/tmcap.py`](tools/tmcap.py) | Decoder for PCAPdroid captures of the official app |

Tested with a TouchMix-16 on firmware 3.0.9551 (`MicroX16`). The TouchMix-8
probably speaks the same protocol with fewer channels, but nobody has tried
it.

On Windows, type `py` instead of `python3` in the examples below.

## Getting started

### 1. Put the mixer on your network

Out of the box the TouchMix creates its own Wi-Fi network. To reach it from a
PC on your normal network, have the mixer **join your Wi-Fi** instead: in the
mixer's network settings, switch from creating a network to joining one, pick
your router's network and enter its password. The PC can be on Wi-Fi or
Ethernet, as long as it is on the same router.

(You can also join the PC to the mixer's own network. The mixer is then at
192.168.1.1.)

### 2. Find it

```sh
python3 -m touchmix discover
```
```
Asking the network (mDNS) for TouchMix mixers...
  192.168.10.129  port 54596  QSC JD HOME              (MicroX16)
```

The mixer announces itself over mDNS (Bonjour), the same way the official app
finds it. **Its control port changes when it reboots**, so every command looks
the port up this way. If mDNS is blocked on your network, `discover` falls
back to scanning the local subnet on port 42217. Use `--subnet` for a larger
or different network.

Every other command discovers the mixer too. To skip that, pass `--host` or
set the `TOUCHMIX_HOST` environment variable. Either can include the port,
e.g. `--host 192.168.10.129:54596`.

### 3. Give this PC access (once)

```sh
python3 -m touchmix info
```
```
Mixer:    QSC JD HOME at 192.168.10.129
Firmware: 3.0.09551
Mixer IP: 192.168.10.129
Client:   Revive-LAPTOP-4THKAI2Q, access: NONE: changes from this PC are ignored
```

The mixer keeps an access profile for every device that connects, and **a new
device starts with no rights**. It still gets the full mixer state, but the
mixer silently ignores every change it sends. On the mixer, open the Wi-Fi
device access settings, find this PC's client name (the `Client:` line), and
allow **All** (or just the mixes it needs). Run `info` again: it should say
`access: all`.

The profile is stored per client name, which defaults to `Revive-<computer
name>`. If you pass `--name`, keep using the same one.

### 4. Check that it works

```sh
python3 -m touchmix monitor          # move a fader on the mixer: it shows up here
python3 -m touchmix mute 4 on --verify
python3 -m touchmix mute 4 off
```

`--verify` reconnects and reads the value back from the mixer, and prints
`CHANGED` when the mixer stored it.

## Commands

```sh
python3 -m touchmix info                       # name, firmware, access, channel overview
python3 -m touchmix monitor                    # every change on the mixer or the app, in real units
python3 -m touchmix mute 4 on                  # on / off
python3 -m touchmix solo 4 on                  # cue
python3 -m touchmix fader 4 -10db              # -10db, +6db, 0db, unity, off (or a raw 0..1)
python3 -m touchmix fader main 0db
python3 -m touchmix pan 4 L50                  # -100..100, L50, C, R
python3 -m touchmix trim 4 -3                  # dB: -15..+15, stereo inputs 0..+12
python3 -m touchmix send 4 aux1 -10db          # input to aux1-aux10 or fx1-fx4
python3 -m touchmix send 4 fx1 off
python3 -m touchmix gate 4 -40                 # threshold in dB (-60..0), or on / off
python3 -m touchmix eq 4 1 gain -3             # band 1-4 (1-6 on main/aux), gain in dB
python3 -m touchmix eq 4 1 freq 120            # frequency in Hz
python3 -m touchmix param 4 Comp Thresh -20    # any mapped control, in its own units
python3 -m touchmix param 4 Comp Ratio 4
python3 -m touchmix param 4 EQ Q 2 --band 3
python3 -m touchmix param aux1 GEQ 1000 -6     # graphic EQ slider, by its band name
python3 -m touchmix param main "Anti FB" Freq 2500 --band 2
python3 -m touchmix record status              # USB recorder: drive, transport, armed tracks
python3 -m touchmix record arm 1-8 st17        # or disarm; 'all' for every track, 2trk for 2-track
python3 -m touchmix record start               # start / stop / play / rtz (return to zero)
python3 -m touchmix find "aux 1 > comp"        # search parameter names (offline)
python3 -m touchmix set "In 4 > Control > Phase" 1   # any parameter, raw 0..1 value
```

Every command that changes something accepts `--verify`.

**Channels:** `1`–`16`, `st17` and `st19` (the stereo inputs), `main`,
`aux1`–`aux10` (7/8 and 9/10 are stereo pairs, so `aux7` and `aux8` are the
same bus), `fx1`–`fx4` (the effect returns), `dca1`–`dca8`, `pb` (2-track
playback) and `talkback`.

**`param`** takes a section and a name as they appear in
[`params.csv`](touchmix/data/params.csv): sections are `Control`, `EQ`,
`Lo-Cut`, `Hi-Cut`, `Gate`, `Comp`, `De-Esser`, `GEQ`, `Anti FB` and `HPF`.
Values are in the control's own units (dB, Hz, ms, ratio, or on/off). See
[Value scaling](docs/PROTOCOL.md#value-scaling) for every range.

## From Python

```python
from touchmix import TouchMix

with TouchMix('192.168.10.129') as tm:     # the port is looked up over mDNS
    tm.wait_ready()                        # the mixer has sent its full state
    if not tm.can_write():
        raise SystemExit('grant this PC access on the mixer first')
    tm.fader(4, '-10db')
    tm.send(4, 'aux1', '-6db')
    tm.set_value(4, 'Comp', 'Thresh', -20)
    tm.on_change(lambda pid, value, raw, tag, src: print(hex(pid), value))
```

See [`docs/PYTHON.md`](docs/PYTHON.md) for the whole API.

## What's mapped

About 2,600 of the mixer's ~5,000 settable parameters have a measured curve,
which covers everything you need to run a mix:

* **Inputs 1–16, stereo inputs:** fader, mute, solo, pan/width, trim, phase,
  delay, low/high cut, 4-band EQ, gate, compressor, de-esser, all aux and FX
  sends and their mutes, DCA and mute-group assignment, record arm, Channel
  Safe
* **Main, Aux 1–10:** fader, mute, output gain, delay, 6-band EQ, low/high cut,
  compressor, 31-band graphic EQ, feedback notch filters (Main and Aux 1–6)
* **FX 1–4 returns, 2-track playback, talkback, DCA 1–8:** faders, mutes, EQ,
  HPF, width, balance, trim

**Not mapped yet:** the effect engines' own settings (reverb time, delay time
and so on), mode switches such as compressor mode and EQ band type, scene
recall, user buttons, the wizards, and the meters and RTA. The USB recorder
works (arm, record, play, stop) but locate is untested. [`docs/MAPPING.md`](docs/MAPPING.md) explains how to map more.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `info` shows `access: NONE` | The mixer ignores this PC until you allow it. See step 3. |
| `discover` finds nothing, but the app works | mDNS may be blocked between the PC and the mixer (guest networks often do this). Find the port with the app or your router, and pass `--host IP:PORT`. |
| `Connection refused` | The control port changed (the mixer rebooted). Leave out `--port` so it is looked up again. |
| `mixer did not send its config within 15s` | Another session from this PC may still be open. Close other `touchmix` commands and try again. |
| A command runs but nothing changes | Check `info` for access. Then run `monitor` while you make the same change in the app, and compare. |

## Captures

The protocol was decoded from captures of the official Android app (made with
PCAPdroid). To decode your own:

```sh
python3 tools/tmcap.py summary capture.pcap
python3 tools/tmcap.py params  capture.pcap          # every parameter change, with names
python3 tools/tmcap.py files   capture.pcap out/     # extract and inflate the .dscomp XML files
```

> ⚠️ **Never publish raw captures or extracted config files.** The mixer sends
> its full configuration, **including its Wi-Fi password in plain text**, to
> every client that connects. `.gitignore` keeps captures out of the repo. The
> files in `mappings/` hold only parameter IDs and values and are safe.

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests run against a small fake mixer ([`tests/fake_mixer.py`](tests/fake_mixer.py)),
so they need no hardware. They also check every curve against every
measurement in `mappings/`.
