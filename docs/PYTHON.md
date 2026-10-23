# Using Touchmix-Revive from Python

The `touchmix` package is pure standard library (Python 3.8+). Copy the
`touchmix/` folder next to your script, or run your script from the repo root.

```python
from touchmix import TouchMix, discover
```

## Connecting

```python
mixer = discover()[0]                       # Mixer(ip, name, suffix, port)
with TouchMix(mixer.ip, port=mixer.port) as tm:
    tm.wait_ready()
    ...
```

* **`discover(subnets=None, port=None)`** returns a list of
  `Mixer(ip, name, suffix, port)`. It asks over mDNS first, and if nothing
  answers it scans the local /24 on `port` (default 42217). Pass
  `subnets=['192.168.0.0/22']` to scan instead.
* **`TouchMix(host, port=None, name=None)`**. With no port, `connect()` looks
  it up over mDNS. The port changes when the mixer reboots, so don't
  hard-code it. `name` is the client name the mixer sees, and it keys the
  mixer's access profile for this client (default `Revive-<computer name>`).
* **`connect()` / `close()`**, or use it as a context manager.
* **`wait_ready(timeout=15)`** blocks until the mixer has sent its full
  configuration. Do this before reading or writing.
* **`can_write()`** returns True if the mixer gives this client any access,
  False if it ignores this client's changes, or None if unknown. A new client
  starts with no access: see the README, step 3. `tm.access` holds the
  individual flags (`All`, `Main Mix`, `Aux 1`, …).
* **`connected`** is False once the connection drops. `tm.error` says why.

The client keeps the session alive with polls in a background thread, and
reads the mixer's messages in another.

## Changing things

Channels are given as in the CLI: `4`, `'st17'`, `'main'`, `'aux1'`, `'fx2'`,
`'dca3'`, `'pb'`, `'talkback'`.

```python
tm.mute(4, True)
tm.solo(4, False)
tm.fader(4, '-10db')          # also '+6db', 'unity', 'off', or a raw 0..1 float
tm.pan(4, 'L30')              # -100..100, 'L30', 'C', 'R'
tm.trim(4, -3)                # dB
tm.send(4, 'aux1', '-6db')    # aux1..aux10, fx1..fx4; same values as fader()
tm.gate(4, True)
tm.gate_threshold(4, -40)
tm.eq_gain(4, 2, -3)          # channel, band, dB
tm.eq_freq(4, 2, 250)         # channel, band, Hz
```

**The USB recorder** (needs a drive in the mixer):

```python
tm.arm(1)                     # or tm.arm(1, False); 'st17', 'st19', '2trk'
tm.transport('record')        # 'play', 'stop', 'next', 'previous', 'rtz' (return to zero)
```

**The MP3 player** (MP3s in the root of a drive formatted by the mixer):

```python
tm.mp3_mode(True)             # MP3 playback; False = back to multitrack
tm.mp3_select(1)              # second song (0 = first); this stops the player
time.sleep(1)                 # let it load, or the next play is lost
tm.transport('play')
title = tm.config_text[0x0118]   # 'Global > Control > Current Song', kept up to date
```

**FX processors:**

```python
tm.fx_type(1, 'lush')         # chorus, delay, pitch, dense, lush, mono-delay, none
```

**Names and other text settings:**

```python
tm.rename(16, 'Kick')                                  # as on the channel's Setup tab
tm.set_text('Aux 1 > Control > Name', 'Drums')         # any text parameter
```

**Any mapped control, in real units:**

```python
tm.set_value(4, 'Comp', 'Thresh', -20)
tm.set_value(4, 'Comp', 'Ratio', 4)
tm.set_value(4, 'EQ', 'Q', 2, band=3)
tm.set_value('aux1', 'GEQ', '1000', -6)          # graphic EQ slider by band name
tm.set_value('main', 'Anti FB', 'Freq', 2500, band=2)
tm.set_value(4, 'Comp', 'Knee', 'on')            # switches take on/off
```

`set_value` raises `KeyError` for a parameter that has no measured curve yet.

**Raw writes** take the wire value (0..1) directly:

```python
tm.set('In 4 > Control > Phase', 1.0)            # by "Path > Name"
tm.set(0x0220, 1.0)                              # or by index
tm.set_many([(0x021E, 0.5), (0x026A, 0.5)])      # one message, several parameters
tm.set(0x00D4, ('int', 3))                       # a raw u32, for integer parameters
```

Writes to network, security, device-access, Powerdown and `FileMan > Delete`
parameters raise `TouchMixError` unless you set `tm.allow_risky = True` first.
They can take the mixer off the network or lock every client out.

Keep `set_many` batches small. **196 writes in one message made the mixer
reboot**. 16 at a time has always been fine (`mapping.send_chunked` does the
splitting).

## Reading state

```python
value, text = tm.config_value('In 4 > Control > Fader')   # ('-10', None), engineering units
name = tm.config_text[tm.resolve('In 4 > Control > Name')]
```

* **`tm.config`**: index → value string from the configuration the mixer sent
  on connect, in engineering units (dB, Hz, ms). It does not update as things
  change. Reconnect for a fresh copy.
* **`tm.live`**: index → latest wire value (0..1) seen on the connection,
  including your own writes.
* **`tm.meters()`**: meters as linear amplitude (1.0 = 0 dBFS), updated about
  10 times a second: `{'main': (L, R), 'inputs': {1: x, ..., 16: x}}`. Input
  meters are before the fader and mute.
* **`tm.on_change(fn)`**: `fn(pid, value, raw, tag, source)` runs for every
  change the mixer reports. `value` is the wire float, `raw` the same 4 bytes
  as an integer, and `source` is `'set'` (a 5.1 parameter change) or
  `'status'` (an unsolicited 4.2 push such as Wi-Fi strength). It runs on the
  reader thread, so keep it quick.

To turn a wire value into real units:

```python
from touchmix.params import curve_for

par = tm.params.get(pid)
curve = curve_for(par.path, par.name)
if curve:
    print(par.full_name, curve.format(value))     # e.g. 'In 4 > Comp > Thresh -20 dB'
```

## The parameter table

```python
from touchmix import ParamTable
from touchmix.params import strip_path

t = ParamTable()                       # from touchmix/data/params.csv
p = t.find('In 4 > EQ', 'Gain', 1)     # path, name, occurrence (band 2 here)
p.index, p.path, p.name, p.full_name   # 0x025A, 'In 4 > EQ', 'Gain', 'In 4 > EQ > Gain'
t.search('aux 1 > comp')               # substring search on full names
strip_path('aux8')                     # 'Aux 7/8'
```

## Curves

`touchmix.params` has a measured curve for every mapped control, grouped by
channel type: `INPUT_CURVES`, `STEREO_CURVES`, `BUS_CURVES`, `FX_CURVES`,
`PLAYBACK_CURVES` and `TALKBACK_CURVES`. Use `curve_for(path, name)` to look one
up.

```python
c = curve_for('Main > Comp', 'Ratio')  # <Curve exp 5..100>
c.to_norm(10)                          # real units -> wire value
c.from_norm(0.5)                       # wire value -> real units (22.36)
c.format(0.5)                          # '22.36'
```

Kinds: `linear`, `exp` (even steps on a log scale), `expoff` (exponential
starting at 0), `fader` (the fader law), and `switch` (on from 0.5). The fader
helpers `fader_to_norm`, `fader_db_to_norm` and `norm_to_fader_db` are also
there.

## Errors

* `TouchMixError`: the mixer did not greet, closed the connection, or did not
  send its configuration in time.
* `KeyError`: an unknown parameter or channel name, or no curve for `set_value`.
* `ValueError`: a value that cannot be parsed, e.g. `fader(4, 'loud')`.
* `OSError`: network errors from the socket.
