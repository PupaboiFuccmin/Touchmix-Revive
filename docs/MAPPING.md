# Mapping the mixer

Every parameter ID on the wire is known from the mixer's own config (see
[`params.csv`](../touchmix/data/params.csv)), but the wire carries 0..1
values. What 0.5 means for a given control (dB? Hz? which curve?) has to be
measured. `python -m touchmix map` does that measuring. It is how every curve
in [`touchmix/params.py`](../touchmix/params.py) was found.

> ⚠️ Probes move **live** controls through their whole range: faders to
> +12 dB, gates and compressors on and off, mutes. Turn the amps down or
> disconnect the outputs before running one. They put every control back
> afterwards, but you will hear what happens in between.

## How it works

The mixer sends its full configuration, **in real units**, to every client
that connects. So a probe:

1. reads each control's current value (and saves it to a restore point on disk),
2. writes a sweep of wire values, e.g. 0, 0.05, …, 1,
3. reconnects, and reads back what the mixer now reports in dB, Hz or ms,
4. puts the original values back and checks that they stuck.

It probes the same control on many channels at once: one reconnect reads one
point from each of 16 channels. Each probe saves a JSON file in `mappings/`
with every point. The tests check every point in those files against the
curve tables, so the tables can't drift from the measurements.

## Probe one control

```sh
python -m touchmix map probe Fader --channels 1-16 --steps 33
python -m touchmix map probe Thresh --section Comp --channels 1-8
python -m touchmix map probe Gain --section EQ --band 3 --channels main,aux1-6
python -m touchmix map show mappings/mapping-probe-thresh-*.json
```

`--channels` takes `1-16`, `aux1-6`, `fx1-4`, `main`, `st17,st19`, `pb`,
`talkback`, and comma-separated mixes of those. `--band N` picks the Nth
parameter of that name in the section (EQ bands, notch filters).

## Probe a whole plan

```sh
python -m touchmix map probe-all --plan inputs     # 23 input-channel controls
python -m touchmix map probe-all --plan buses      # 47: Main/Aux/FX/stereo/2-track/talkback processing
python -m touchmix map probe-all --plan flags      # 24 on/off flags, 5 steps each
python -m touchmix map probe-all --plan buses --only comp,geq
python -m touchmix map probe-all --plan buses --start 16    # resume after step 15
```

The plans are in [`touchmix/mapping.py`](../touchmix/mapping.py) (`PLANS`).
`probe-all` asks once, then runs every probe in order on one connection,
saves one JSON per control, and carries on if one fails. At the end it prints
a table that fits each curve:

```
control                    kind                       formula                          max err
Comp Thresh                linear                     eng = 60*x -60                   1.421e-14
Comp Ratio                 exponential                eng = 5 * 20^x                   1.165e-05
Comp Attack                exponential + offset       eng = 0 + 25*(100^x - 1)/99      1.75e-06
Comp Knee                  switch                     off up to 0.45, on from 0.5      0
```

Every probe so far has fit a simple law exactly, to within float rounding.

**Left out of the plans on purpose:**

* **Phantom power:** it would put 48 V on whatever is plugged in.
* **Channel link and stereo link:** linking copies settings between channels,
  which a restore would not undo.
* **Enum or mode parameters** (compressor mode, EQ band type, filter type):
  switching modes may reset other parameters.
* **The signal generator, and the headphone and monitor levels.**

Map those with a guided run instead (below).

## If the mixer crashes mid-probe

Every probe saves the original values to `mappings/restore-point.json` before
it writes anything. If the mixer stops answering:

1. Wait for it to come back (power-cycle it if needed). Its control port will
   probably have changed. The tools find the new one over mDNS.
2. Put the originals back:

   ```sh
   python -m touchmix map restore
   ```

3. Resume the plan with `--start N`.

This is not hypothetical. One early probe wrote all 196 graphic-EQ sliders in
a single message, and the mixer rebooted. Probes now write at most 16
parameters per message and per round (`mapping.MAX_BATCH`).

## Guided runs

For things a sweep can't find (which ID a button press uses, what a mode
switch writes, scene recall), `map run` prompts you to do one thing at a time
on the mixer or in the app, and records every parameter that changes:

```sh
python -m touchmix map list                       # built-in scripts
python -m touchmix map run channel 9 --readback
python -m touchmix map run bus fx1
python -m touchmix map run scenes
python -m touchmix map run transport
python -m touchmix map run mutes                  # mute groups, user buttons, talkback
python -m touchmix map run free                   # you name each step yourself
```

At each step, press Enter when done, `r` to redo, `s` to skip, `n <text>` to
add a note, or `q` to save and quit. `--readback` reconnects after each step,
to also record each changed control's value in real units. Write your own
script as a JSON list of `{"prompt": ..., "expect": ["In 4 > Control > Mute"]}`
and pass it with `--file`.

The scenes, transport and mutes scripts have not been run on a mixer yet.

## Adding a curve

1. Probe the control on every channel type it exists on.
2. Read the fit from the summary (or `map show`). Pick the matching `Curve`:
   `linear` (lo…hi), `exp` (lo…hi, even on a log scale), `expoff` (starts at 0;
   give `ratio`), `fader`, or `switch`.
3. Add it to the right table in `touchmix/params.py`: `INPUT_CURVES`,
   `BUS_CURVES`, `FX_CURVES`, `PLAYBACK_CURVES` or `TALKBACK_CURVES`. Don't
   assume a curve carries across channel types: the bus compressor's ratio
   and attack, and the bus high-cut, all differ from the inputs'.
4. Commit the JSON from `mappings/` with it, and run the tests. They check the
   new curve against every point.

## What's left

* **FX engines** (`FX 1 > D-Reverb`, `Delay`, `Chorus`, `PitchShift`, …): each
  slot has to have that effect loaded before its parameters can be probed.
* **Enums and modes:** compressor mode, EQ and filter types, speaker type,
  effect type.
* **Actions:** scene recall and store (`Global > FileMan`), recording
  transport, user buttons, the feedback and room-tuning wizards.
* **Aux Pre/Post:** stored as an integer ×1024 (Aux 1–6 read 0, the stereo
  auxes read 1). It is mapped as such, but what 0 and 1 mean is not
  confirmed.
* **Meters and RTA:** these come back in the poll replies (6.1/7.1), not as
  parameters. Decoding them needs a capture with audio playing.
* **Global, security and network settings:** deliberately left alone.
