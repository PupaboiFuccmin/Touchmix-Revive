# Touchmix-Revive

Control a QSC TouchMix-16 from a PC over the network, by reverse engineering
the protocol the official TouchMix Control app uses.

* [`touchmix/`](touchmix): Python client library and command-line tool
* [`docs/PROTOCOL.md`](docs/PROTOCOL.md): wire format, message types, parameter layout, capture log
* [`touchmix/data/params.csv`](touchmix/data/params.csv): all 5,694 parameter IDs with names, from the mixer's own config
* [`tools/tmcap.py`](tools/tmcap.py): decoder for PCAPdroid captures

Everything is Python 3.8+ standard library. There is nothing to install. On
Windows, use `py` in place of `python3`.

> ⚠️ **This is experimental.** It sends real commands to a live mixer. Test with
> the amps off or the volume down until you trust it.

## 1. Put the mixer on your network

Out of the box the TouchMix creates its own Wi-Fi network (mixer at
192.168.1.1). To reach it from your PC without joining that network, have the
mixer **join your existing Wi-Fi** instead. On the mixer, open the network /
Wi-Fi settings, switch from creating a network to joining one, pick your
router's network and enter its password. The mixer's config has settings for
exactly this (`Create Network`, `Infra SSID`, `Infra PW`, `IP Assign Mode`,
`Static IP`). The menu labels on screen may differ slightly. Use DHCP unless
you want a fixed address.

Your PC can then be on Wi-Fi or Ethernet, as long as it is connected to the
same router.

## 2. Find it

```sh
python3 -m touchmix discover
```

This tries port 42217 on every address in your PC's local /24 subnet(s). A
TouchMix greets every new connection with its name, so the scan just reads
that greeting and hangs up. It never logs in. If your network is larger than a
/24, or the mixer is on another subnet, add `--subnet 192.168.0.0/22`.

All other commands auto-discover too. To skip the scan, pass `--host` or set
`TOUCHMIX_HOST=192.168.x.y`.

## 3. Control it

```sh
python3 -m touchmix info                     # name, firmware, channels, faders, mutes
python3 -m touchmix monitor                  # print every change made on the mixer/app
python3 -m touchmix mute 4 on --verify       # --verify reconnects and checks the mixer stored it
python3 -m touchmix mute 4 off
python3 -m touchmix solo 4 on
python3 -m touchmix fader 4 -10db            # dB (-55..+12), off, or raw 0..1
python3 -m touchmix pan 4 L50                # -100..100, L50, C, R
python3 -m touchmix trim 4 -3                 # input trim (digital gain) in dB, ±15
python3 -m touchmix send 4 aux1 -10db         # aux/FX send level: dB, off, or raw 0..1
python3 -m touchmix send 4 fx1 -20db
python3 -m touchmix gate 4 -40                # gate threshold in dB (-60..0), or on / off
python3 -m touchmix eq 4 1 gain -3           # EQ band 1..4 gain in dB (±15)
python3 -m touchmix eq 4 1 freq 120          # EQ band frequency in Hz
python3 -m touchmix find "aux 1 > control"   # search parameter names (offline)
python3 -m touchmix set "In 4 > Control > Phase" 1
```

Channels: `1`–`16`, `st17`, `st19`, `main`, `aux1`–`aux10`, `fx1`–`fx4`, `dca1`–`dca8`.

From Python:

```python
from touchmix import TouchMix, discover

ip, name, _ = discover()[0]
with TouchMix(ip) as tm:
    tm.wait_ready()                      # mixer has sent its full state
    print(tm.config_value('In 4 > Control > Fader'))
    tm.on_change(lambda pid, value, raw, tag, src: print(hex(pid), value))
    tm.mute(4, True)
```

## First test checklist

1. `discover` finds the mixer.
2. `info` shows your channel names, and `Client: ... access: all`.
3. `monitor`, then move a fader on the mixer: changes appear.
4. `mute 4 on --verify` prints `CHANGED`, and channel 4 shows muted on the mixer.

**If `info` says `access: NONE`:** the mixer keeps an access profile for each
connected device, and a new device starts with no rights. The mixer still
sends it the full state, but ignores every change it sends back. On the
mixer, open the Wi-Fi device access settings, find this PC's client name (the
`Client:` line, default `Revive-<hostname>`), and allow All (or the mixes you
need). This is needed only once per client name, so keep `--name` the same.

If step 4 fails even with access, the app's extra mute side batch might be
needed (see "Open questions" in `docs/PROTOCOL.md`). Run `monitor` and send me
the output.

## Mapping assistant

Fills gaps in the parameter map without phone captures. There are two modes.

**Probe** maps a curve with no hands. It writes a sweep of wire values, then
reconnects and reads the mixer's own value (dB, Hz, ms) back from its config.
It sweeps the same control on several channels at once, so each reconnect
reads one point per channel. At the end it puts every control back where it
was.

```sh
python3 -m touchmix map probe Fader --channels 1-16 --steps 33     # fader curve
python3 -m touchmix map probe Trim --channels 1-16
python3 -m touchmix map probe Thresh --section Gate --channels 1-8
python3 -m touchmix map probe AuxSend1 --channels 1-16
```

> ⚠️ Probe moves **live** controls through their full range (faders to +10 dB).
> Turn the amps down or unplug the outputs first.

**Guided** prompts you to do one thing at a time on the mixer or in the app,
and records every parameter that changes while you do it. Add `--readback` to
also record the mixer's value in real units for each change. That costs a
reconnect, a second or two, after each step.

```sh
python3 -m touchmix map list                      # built-in scripts
python3 -m touchmix map run channel 9 --readback  # confirm strides on another channel
python3 -m touchmix map run bus fx1
python3 -m touchmix map run scenes
python3 -m touchmix map run transport
python3 -m touchmix map run free                  # you name each step yourself
python3 -m touchmix map show mapping-channel-*.json
```

At each step, press Enter when done, `r` to redo, `s` to skip, `n <text>`
to add a note, or `q` to save and quit. Each run writes a
`mapping-*.json`. These files hold only IDs, names and values, never the raw
config, so they are safe to share.

## Captures

```sh
python3 tools/tmcap.py summary capture.pcap
python3 tools/tmcap.py params  capture.pcap          # every parameter change, with names
python3 tools/tmcap.py files   capture.pcap out/     # extract and inflate the .dscomp XML files
```

Captures are not committed (see `.gitignore`). They include your network
details and the mixer's full config, **including its Wi-Fi password in plain
text**.

## Tests

```sh
python3 -m unittest discover -s tests
```
