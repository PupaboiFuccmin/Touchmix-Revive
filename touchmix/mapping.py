"""Mapping assistant: find out which parameter IDs a control uses and what its
wire values mean.

Two ways in:

* guided: prompts you to do one thing at a time on the mixer (or the app) and
  records every parameter change that arrives while you do it. With readback,
  it reconnects after each step and also records the mixer's own engineering
  value (dB, Hz, ...) for each changed ID, from NH<n>-config.xml.
* probe: writes a sweep of wire values to a parameter itself, reconnects, and
  reads the engineering values back. That maps a whole curve with no hands.
  The same control on several channels is swept in parallel, so one reconnect
  reads back one point per channel.

Results are written as JSON. They hold only IDs, names and values, never the
raw config, so they are safe to commit.
"""
import collections
import datetime
import json
import math
import os
import re
import threading
import time

from . import privacy
from .client import TouchMix
from .params import ParamTable, strip_path, norm_to_fader_db, curve_for, int_to_norm, FX_TYPES

# Most parameters written per message and per probe round. 196 GEQ sliders in
# one message made the mixer reboot (2026-10-21); 16 at a time never caused trouble.
MAX_BATCH = 16

# IDs that change on their own, without anyone touching a control
BACKGROUND = {0x00CE, 0x00D4}


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return s


# ---------------------------------------------------------------- scripts
# A step is {'prompt': str, 'expect': [ids]}. 'expect' is optional; the
# summary flags steps where an expected ID did not show up.

def _ids(table, target, section, name, occurrence=0):
    return [table.find('%s > %s' % (strip_path(target), section), name, occurrence).index]


def script_fader_curve(table, target='4'):
    who = strip_path(target)
    ids = _ids(table, target, 'Control', 'Fader')
    points = ['+10', '+5', '0', '-5', '-10', '-15', '-20', '-25', '-30', '-40', '-50', '-60', '-inf']
    return [dict(prompt='%s fader to exactly %s dB (read the value on the TouchMix screen)' % (who, p),
                 expect=ids) for p in points]


def script_channel(table, target='9'):
    """Short walk-through of an input channel. Run it on a channel other than
    In 4 to confirm the strides, and with --readback to get curve points."""
    who = strip_path(target)
    c = lambda section, name, occ=0: _ids(table, target, section, name, occ)
    return [
        dict(prompt='%s fader to MAX' % who, expect=c('Control', 'Fader')),
        dict(prompt='%s fader to 0 dB' % who, expect=c('Control', 'Fader')),
        dict(prompt='%s mute ON' % who, expect=c('Control', 'Mute')),
        dict(prompt='%s mute OFF' % who, expect=c('Control', 'Mute')),
        dict(prompt='%s pan hard LEFT' % who, expect=c('Control', 'Pan')),
        dict(prompt='%s pan CENTRE' % who, expect=c('Control', 'Pan')),
        dict(prompt='%s trim (digital gain) to MAX' % who, expect=c('Control', 'Trim')),
        dict(prompt='%s trim to MIN' % who, expect=c('Control', 'Trim')),
        dict(prompt='%s trim back to 0 dB' % who, expect=c('Control', 'Trim')),
        dict(prompt='%s phantom power ON (only if nothing is plugged in that minds!)' % who,
             expect=c('Control', 'Phantom')),
        dict(prompt='%s phantom power OFF' % who, expect=c('Control', 'Phantom')),
        dict(prompt='%s low cut (HPF) ON' % who, expect=c('Lo-Cut', 'BndEnbl') + c('Lo-Cut', 'FxEnbl')),
        dict(prompt='%s low cut frequency to MAX' % who, expect=c('Lo-Cut', 'Freq')),
        dict(prompt='%s low cut frequency to MIN, then OFF' % who, expect=c('Lo-Cut', 'Freq')),
        dict(prompt='%s gate threshold to MAX' % who, expect=c('Gate', 'Thresh')),
        dict(prompt='%s gate threshold to MIN' % who, expect=c('Gate', 'Thresh')),
        dict(prompt='%s comp threshold to MAX' % who, expect=c('Comp', 'Thresh')),
        dict(prompt='%s comp threshold to MIN' % who, expect=c('Comp', 'Thresh')),
        dict(prompt='%s comp ratio to MAX' % who, expect=c('Comp', 'Ratio')),
        dict(prompt='%s comp ratio to MIN' % who, expect=c('Comp', 'Ratio')),
        dict(prompt='%s EQ band 1 gain to +15' % who, expect=c('EQ', 'Gain', 0)),
        dict(prompt='%s EQ band 1 Q to MAX' % who, expect=c('EQ', 'Q', 0)),
        dict(prompt='%s EQ band 1 Q to MIN' % who, expect=c('EQ', 'Q', 0)),
        dict(prompt='%s EQ band 1 gain back to 0' % who, expect=c('EQ', 'Gain', 0)),
        dict(prompt='%s send to Aux 1 to 0 dB' % who, expect=c('Control', 'AuxSend1')),
        dict(prompt='%s send to Aux 1 off' % who, expect=c('Control', 'AuxSend1')),
        dict(prompt='%s send to FX 1 to 0 dB' % who, expect=c('Control', 'FXSend1')),
        dict(prompt='%s send to FX 1 off' % who, expect=c('Control', 'FXSend1')),
        dict(prompt='Assign %s to DCA 1' % who, expect=c('Control', 'DCA Asn1')),
        dict(prompt='Remove %s from DCA 1' % who, expect=c('Control', 'DCA Asn1')),
        dict(prompt='%s fader back to -inf' % who, expect=c('Control', 'Fader')),
    ]


def script_bus(table, target='aux1'):
    """Master section of an aux, FX return, DCA or Main."""
    who = strip_path(target)
    c = lambda name: _ids(table, target, 'Control', name)
    steps = [
        dict(prompt='%s master fader to MAX' % who, expect=c('Fader')),
        dict(prompt='%s master fader to 0 dB' % who, expect=c('Fader')),
        dict(prompt='%s master fader to -inf' % who, expect=c('Fader')),
        dict(prompt='%s mute ON' % who, expect=c('Mute')),
        dict(prompt='%s mute OFF' % who, expect=c('Mute')),
    ]
    if not who.startswith('DCA'):
        steps += [dict(prompt='%s EQ band 1 gain to +15, then back to 0' % who,
                       expect=_ids(table, target, 'EQ', 'Gain'))]
    return steps


def script_scenes(table, target=None):
    fm = lambda name: [table.find('Global > FileMan', name).index]
    return [
        dict(prompt='Open the scene list (just browse, do not recall anything)'),
        dict(prompt='Recall an existing user scene', expect=fm('Recall')),
        dict(prompt='Store the current mix as a NEW user scene (Save As)', expect=fm('Save As')),
        dict(prompt='Overwrite that scene (Save)', expect=fm('Save')),
        dict(prompt='Recall a scene with "omit levels" (or similar) turned on', expect=fm('Omit Levels')),
        dict(prompt='Recall a factory channel preset on In 4', expect=fm('Recall')),
        dict(prompt='Turn Channel Safe ON for In 4', expect=_ids(table, '4', 'Control', 'Channel Safe')),
        dict(prompt='Turn Channel Safe OFF for In 4', expect=_ids(table, '4', 'Control', 'Channel Safe')),
        dict(prompt='Delete the scene you made', expect=fm('Delete')),
    ]


def script_transport(table, target=None):
    tr = lambda name: [table.find('Global > Transport', name).index]
    arm = _ids(table, '1', 'Control', 'Arm')
    return [
        dict(prompt='Open the recorder screen (USB drive in)'),
        dict(prompt='Arm In 1 for recording', expect=arm),
        dict(prompt='Disarm In 1', expect=arm),
        dict(prompt='Arm all tracks'),
        dict(prompt='Disarm all tracks'),
        dict(prompt='Arm In 1 again, then press RECORD', expect=tr('Transport')),
        dict(prompt='Press STOP', expect=tr('Transport')),
        dict(prompt='Press PLAY', expect=tr('Transport')),
        dict(prompt='Press STOP', expect=tr('Transport')),
        dict(prompt='Rewind / go to start', expect=tr('Song Position (edit)')),
        dict(prompt='Set a locate point, then go to it', expect=tr('Set Locate') + tr('Goto Locate')),
        dict(prompt='Disarm In 1'),
    ]


def script_mutes(table, target=None):
    group = [table.find('Mute 1 > Control', 'Mute').index]
    return [
        dict(prompt='Add In 4 to mute group 1', expect=_ids(table, '4', 'Control', 'Mute Asn1')),
        dict(prompt='Mute group 1 ON', expect=group),
        dict(prompt='Mute group 1 OFF', expect=group),
        dict(prompt='Remove In 4 from mute group 1', expect=_ids(table, '4', 'Control', 'Mute Asn1')),
        dict(prompt='Press "clear mutes" (with something muted)',
             expect=[table.find('Global > Misc Input', 'Clear mute').index]),
        dict(prompt='Cue (solo) In 4, then press "clear solos"',
             expect=[table.find('Global > Misc Input', 'Clear solo').index]),
        dict(prompt='Press user button 1'),
        dict(prompt='Press user button 2'),
        dict(prompt='Talkback ON'),
        dict(prompt='Talkback OFF'),
    ]


# ---------------------------------------------------------------- tour
# Steps for `map tour`: done on the mixer's own screen while the PC logs what the
# mixer reports. (prompt, repeat[, watch]): repeat steps cycle through a
# setting's options and ask for each label; watch lists parameters to read back
# from the mixer after each answer (a reconnect), for settings it doesn't report. Screen paths are from the TouchMix-8/16 user guide.
# Channel 16 is used throughout, so leave it unused. Nothing here deletes
# anything or powers the mixer down. Saving a channel preset froze the mixer's
# screen once (2026-10-21), so the tour no longer does it.

TOUR_INTRO = """How this works
  - Each step says exactly where to go on the TouchMix's touchscreen and what to touch.
  - Do it, wait a second, then press Enter here.
  - If the screen doesn't have what a step describes, type "none" (or what you see
    instead) and press Enter. That is useful too.
  - Channel 16 is the test channel: nothing should be plugged into it.
  - Nothing here deletes anything or powers the mixer down.

Getting to channel 16 (used a lot below):
  press HOME > touch the "Inputs 9-16" bank > touch channel 16's name button
  at the top of its strip. Its screen opens with tabs along the top
  (EQ, Gate, Comp, FX, Aux, Setup ...)."""

CH16 = 'Channel 16 (HOME > Inputs 9-16 > touch channel 16\'s name button)'

# read back after each answer, because the mixer may not report these changes
FX_WATCH = ['FX 1 > Editor > FX type', 'FX 1 > Control > Preset']

TOUR = collections.OrderedDict([
    ('scenes', [
        ('Open the scenes screen: press MENU > touch "Scenes". Just look, change nothing.', False),
        ('Save the current mix as a new scene: touch "Save As", type TEST2 on the keyboard,\n'
         '     touch Enter / Save, and confirm with Yes.', False),
        ('Recall it: in the scene list touch TEST2 > "Recall" > "Yes".\n'
         '     It is the same mix, so nothing should change audibly.', False),
    ]),
    ('presets', [
        (CH16 + ':\n'
         '     touch the Preset name (top of the channel screen) > pick any factory preset\n'
         '     (e.g. Vocals > any) > "Recall" > "Yes".', False),
        (CH16 + ':\n'
         '     open the Preset list again and recall the "Default" (or "Reset") preset > "Yes".', False),
    ]),
    ('comp', [
        (CH16 + ', Comp tab:\n'
         '     touch the "Simple" button so it lights (most controls disappear).', False),
        ('Same screen: touch "Simple" again so it goes dark (all controls back).', False),
        ('Same screen: is there any Mode, Type or style selector on the Comp tab?\n'
         '     If yes, touch it and step through each option, typing its name after each.\n'
         '     If no, just press Enter on an empty line.', True),
    ]),
    ('eq', [
        (CH16 + ', EQ tab:\n'
         '     touch the "Low Shelf" button (band 1 becomes a shelf; its Bandwidth knob disappears).', False),
        ('Same screen: touch "Low Shelf" again (band 1 back to a bell).', False),
        ('Same screen: touch the "High Shelf" button (band 4 becomes a shelf).', False),
        ('Same screen: touch "High Shelf" again (band 4 back to a bell).', False),
        ('Same screen: touch the "Low Cut" button (low cut on).', False),
        ('Same screen: touch "Low Cut" again (off).', False),
        ('Same screen: touch the "High Cut" button (on).', False),
        ('Same screen: touch "High Cut" again (off).', False),
        ('Same screen: touch "Simple" (lights, cut and frequency controls disappear).', False),
        ('Same screen: touch "Simple" again (all controls back).', False),
        (CH16 + ', Gate tab: touch "Simple" on, then off again.', False),
    ]),
    ('source', [
        ('Press RECORD/PLAY > touch the "Inputs 9-16" bank.\n'
         '     For channel 16, touch its "Track" button (source becomes the recorder).', False),
        ('Same screen: touch channel 16\'s "Track" button again (or "Input" / "Live")\n'
         '     so it is back on its normal input.', False),
    ]),
    ('names', [
        (CH16 + ', Setup tab:\n'
         '     touch the Name field > type TEST16 on the keyboard > Enter.', False),
        ('Same screen: touch the Name field again > type the old name back\n'
         '     (or "In 16") > Enter.', False),
    ]),
    ('fx', [
        ('Press HOME > touch the "FX Masters" bank > touch FX 1.\n'
         '     A window opens listing the effect processors. Type the one that is\n'
         '     selected now (e.g. "Mono Delay"), press Enter. Change nothing yet.', False, FX_WATCH),
        ('In that window, touch a different processor. Type its name, press Enter.\n'
         '     (The PC then reads FX 1 back, which takes a couple of seconds.)\n'
         '     Repeat for every processor in the list, one at a time.', True, FX_WATCH),
        ('Put FX 1 back to the processor it had at the start.', False, FX_WATCH),
    ]),
    ('buttons', [
        ('Press MENU > touch "User Buttons" > touch "User Button 1" to select it.', False),
        ('Same screen: in the Action list touch "Mute", then pick channel 16 in the\n'
         '     Selection / Detail lists (whatever it offers), then touch "Assign".\n'
         '     Type what you picked, press Enter.', False),
        ('Press the physical U1 button on the mixer once (channel 16 should mute).', False),
        ('Press U1 again (unmute).', False),
        ('Back on MENU > User Buttons, User Button 1 still selected: touch "Reset"\n'
         '     so it goes back to its factory job (Nav Left).', False),
    ]),
    ('recorder', [
        ('Press RECORD/PLAY. If it shows MP3 mode, switch it back to Multitrack.\n'
         '     Make sure the session with your test recording is loaded ("New / Recall").', False),
        ('Touch Play (the session plays), wait about 3 seconds.', False),
        ('Touch Stop.', False),
        ('Drag the play pointer on the time line to about the middle.', False),
        ('Touch "Set" (the locate pointer moves there).', False),
        ('Drag the play pointer back to the start.', False),
        ('Touch "Go To" (the play pointer jumps to the locate point).', False),
        ('Touch any other transport buttons you see (rewind, fast forward, pause, ...),\n'
         '     one at a time, typing each name. Empty Enter when done.', True),
    ]),
    ('meters', [
        ('Silence: nothing playing. Wait 10 seconds, then Enter.', False),
        ('Start a HELD note (a pad or organ, not a plucked sound) on your synth on\n'
         '     inputs 7/8, at a normal level, and keep it held through every step below.\n'
         '     Wait 10 seconds. Type roughly what the Main meter shows (e.g. "-12"), Enter.', False),
        ('Still holding: MUTE channels 7 and 8 on the mixer. Wait 10 seconds, Enter.', False),
        ('Unmute 7 and 8. Pull the MAIN fader all the way down. Wait 10 seconds, Enter.', False),
        ("Put the Main fader back where it was. Pull channel 7's fader all the way\n"
         '     down (channel 8 stays up). Wait 10 seconds, Enter.', False),
        ("Put channel 7 back. Pull channel 8's fader all the way down. Wait 10 seconds, Enter.", False),
        ("Put channel 8 back. Turn the synth's own volume down by about half (not a\n"
         '     mixer fader). Wait 10 seconds. Type the Main meter reading, Enter.', False),
        ('Let go of the note: silence for 10 seconds, Enter.', False),
    ]),
])


SCRIPTS = {
    'fader-curve': (script_fader_curve, '4', 'set one fader to known dB points (guided curve)'),
    'channel': (script_channel, '9', 'input channel walk-through: confirms strides, curve end points'),
    'bus': (script_bus, 'aux1', 'aux / FX / DCA / main master: fader, mute, EQ'),
    'scenes': (script_scenes, None, 'scene and preset recall/store, Channel Safe'),
    'transport': (script_transport, None, 'record arm, record/play/stop, locate'),
    'mutes': (script_mutes, None, 'mute groups, clear mute/solo, user buttons, talkback'),
    'free': (None, None, 'open-ended: do anything, type a label for each step'),
}


def load_script(path):
    """A JSON list of steps: [{"prompt": "...", "expect": ["In 4 > Control > Fader", "0x21f"]}]."""
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------- recording

class Recorder:
    """Collects parameter changes between start() and stop()."""

    def __init__(self, echo=None, include_status=False):
        self.echo = echo
        self.include_status = include_status
        self.lock = threading.Lock()
        self.active = False
        self.changes = {}

    def attach(self, tm):
        tm.on_change(self._on_change)
        self._tm = tm

    def start(self):
        with self.lock:
            self.changes = {}
            self.active = True

    def stop(self):
        with self.lock:
            self.active = False
            return self.changes

    def _on_change(self, pid, value, raw, tag, source):
        if source == 'status' and not self.include_status:
            return
        with self.lock:
            if not self.active:
                return
            c = self.changes.get(pid)
            if c is None:
                c = self.changes[pid] = dict(first=value, count=0, tags=set(), raw_first=raw)
                if self.echo and pid not in BACKGROUND:
                    par = self._tm.params.get(pid)
                    self.echo('    0x%04x %s' % (pid, par.full_name if par else '?'))
            c['last'] = value
            c['raw_last'] = raw
            c['count'] += 1
            if tag:
                c['tags'].add(tag)


def _change_record(table, pid, c, config=None):
    par = table.get(pid)
    rec = dict(id='0x%04x' % pid, index=pid, name=par.full_name if par else None,
               first=round(c['first'], 6), last=round(c['last'], 6),
               raw_last='%08x' % c['raw_last'], count=c['count'],
               tags=sorted('%04x' % t for t in c['tags']))
    if pid in BACKGROUND:
        rec['background'] = True
    if config is not None:
        rec['config'] = _num(config.get(pid))
    return rec


def _resolve_expect(tm, items):
    out = []
    for e in items or []:
        out.append(e if isinstance(e, int) else tm.resolve(e))
    return out


# ---------------------------------------------------------------- guided

HELP = '''  Enter      done, record this step
  r          redo (forget what was recorded and try again)
  s          skip this step
  n <text>   add a note to this step, then press Enter when done
  q          save and quit'''


class Session:
    """A connection that can be dropped and reopened to re-read the config."""

    def __init__(self, host, port, name, recorder=None):
        self.host, self.port, self.name = host, port, name
        self.recorder = recorder
        self.params = ParamTable()
        self.tm = None

    def open(self):
        self.tm = TouchMix(self.host, port=self.port, name=self.name, params=self.params)
        if self.recorder:
            self.recorder.attach(self.tm)
        self.tm.connect()
        self.tm.wait_ready()
        return self.tm

    def reopen(self, settle=0.3):
        time.sleep(settle)
        self.close()
        time.sleep(0.3)
        return self.open()

    def close(self):
        if self.tm:
            self.tm.close()


def guided(session, steps, readback=False, ask=input, say=print, free=False):
    """Walk through steps (or open-ended with free=True). Returns the result dict."""
    rec = session.recorder
    tm = session.tm
    table = session.params
    results = []
    total = '?' if free else len(steps)
    say('Commands at each step:\n' + HELP)
    i = 0
    while free or i < len(steps):
        if free:
            label = ask('\n%d. What will you do? (blank to finish) > ' % (i + 1)).strip()
            if not label:
                break
            step = dict(prompt=label)
        else:
            step = steps[i]
        expect = _resolve_expect(tm, step.get('expect'))
        say('\n%s/%s  %s' % (i + 1, total, step['prompt']))
        note = ''
        rec.start()
        while True:
            cmd = ask('    ...then press Enter > ').strip()
            if cmd.lower() == 'r':
                rec.start()
                say('    (cleared, try again)')
                continue
            if cmd.lower().startswith('n '):
                note = cmd[2:].strip()
                continue
            break
        changes = rec.stop()
        if cmd.lower() == 'q':
            break
        if cmd.lower() == 's':
            results.append(dict(step=i + 1, prompt=step['prompt'], skipped=True))
            i += 1
            continue
        config = None
        if readback and changes:
            say('    reading back...')
            tm = session.reopen()
            config = tm.config
        records = [_change_record(table, pid, c, config) for pid, c in sorted(changes.items())]
        entry = dict(step=i + 1, prompt=step['prompt'], changes=records)
        if expect:
            entry['expect'] = ['0x%04x' % e for e in expect]
            entry['expect_seen'] = any(e in changes for e in expect)
        if note:
            entry['note'] = note
        results.append(entry)
        say(_format_step(entry))
        i += 1
    return results


def _format_step(entry):
    lines = []
    fg = [c for c in entry['changes'] if not c.get('background')]
    if not fg:
        lines.append('    !! nothing changed')
    for c in fg:
        eng = '  -> %s' % c['config'] if 'config' in c else ''
        tags = '  tags=%s' % ','.join(c['tags']) if c['tags'] else ''
        lines.append('    %s %-40s %.4f -> %.4f  (%d msgs)%s%s' % (
            c['id'], c['name'] or '?', c['first'], c['last'], c['count'], eng, tags))
    if 'expect_seen' in entry and not entry['expect_seen']:
        lines.append('    !! expected %s, not seen' % ', '.join(entry['expect']))
    return '\n'.join(lines)


# ---------------------------------------------------------------- probe

def sweep_values(steps):
    if steps < 2:
        raise ValueError('need at least 2 steps')
    return [round(i / (steps - 1.0), 6) for i in range(steps)]


def interp_inverse(points, eng):
    """Wire value that gives engineering value `eng`, from (wire, eng) points.
    Assumes the curve is monotonic. Returns None if it is not, or eng is not a number."""
    pts = sorted((w, e) for w, e in points if isinstance(e, float))
    if len(pts) < 2 or not isinstance(eng, float):
        return None
    es = [e for _, e in pts]
    rising = es[-1] >= es[0]
    if any((b < a) if rising else (b > a) for a, b in zip(es, es[1:])):
        return None
    lo, hi = (es[0], es[-1]) if rising else (es[-1], es[0])
    if eng <= lo:
        return pts[0][0] if rising else pts[-1][0]
    if eng >= hi:
        return pts[-1][0] if rising else pts[0][0]
    for (w0, e0), (w1, e1) in zip(pts, pts[1:]):
        if min(e0, e1) <= eng <= max(e0, e1):
            if e1 == e0:
                return w0
            return w0 + (w1 - w0) * (eng - e0) / (e1 - e0)
    return None


def send_chunked(tm, changes, size=MAX_BATCH):
    """set_many in messages of at most `size` entries, with a short gap between."""
    changes = list(changes)
    for i in range(0, len(changes), size):
        if i:
            time.sleep(0.05)
        tm.set_many(changes[i:i + size])


def probe(session, pids, values, say=print, restore=True, checkpoint=None, max_batch=MAX_BATCH):
    """Sweep `values` across `pids` and read the mixer's engineering values back.

    Each round writes up to max_batch pids, each with a different value from
    the list, then reconnects to read them back. With more pids than values the
    values repeat across pids, so every pid is still written once.
    checkpoint(originals) is called before anything is written, so the caller
    can save a restore point. Restores the original values at the end."""
    tm = session.tm
    originals = {pid: _num(tm.config.get(pid)) for pid in pids}
    if checkpoint:
        checkpoint(originals)
    points = []                      # (pid, wire, eng)
    total = max(len(values), len(pids))
    size = min(len(pids), max_batch)
    for rnd, start in enumerate(range(0, total, size), 1):
        batch = [(pids[k % len(pids)], values[k % len(values)])
                 for k in range(start, min(total, start + size))]
        tm.set_many(batch)
        say('round %d: sent %d values' % (rnd, len(batch)))
        tm = session.reopen()
        for pid, v in batch:
            eng = _num(tm.config.get(pid))
            points.append((pid, v, eng))
            if len(batch) <= 32:
                say('    0x%04x  %.4f -> %s' % (pid, v, eng))

    distinct = set(e for _, _, e in points)
    result = dict(points=[dict(id='0x%04x' % p, wire=w, eng=e) for p, w, e in points],
                  originals={'0x%04x' % p: e for p, e in originals.items()})
    if len(distinct) <= 1:
        result['warning'] = 'config value never changed: the mixer may not store writes, or this ID is wrong'
        say('!! ' + result['warning'])

    if restore:
        result['restored'] = restore_values(session, originals, [(w, e) for _, w, e in points], say,
                                            max_batch=max_batch)
    return result


def _close_enough(now, want):
    if now == want:
        return True
    return isinstance(now, float) and isinstance(want, float) and abs(now - want) <= max(0.05, abs(want) * 1e-3)


def restore_values(session, originals, curve=(), say=print, tries=3, max_batch=MAX_BATCH):
    """Put each pid back to its original engineering value. The first guess
    comes from params.curve_for when the curve is known, else from inverting
    the measured points in `curve`; each pass adds the points it lands on, so a
    curve that is not linear between sweep points converges in a pass or two."""
    curve = list(curve)
    left = dict(originals)
    known = {}
    for pid in left:
        par = session.params.get(pid)
        known[pid] = curve_for(par.path, par.name) if par else None
    for attempt in range(tries):
        back = []
        for pid, eng in left.items():
            w = None
            if attempt == 0 and known[pid] is not None and isinstance(eng, float):
                w = known[pid].to_norm(eng)
            if w is None:
                w = interp_inverse(curve, eng)
            if w is None:
                w = next((cw for cw, ce in curve if ce == eng), None)
            if w is not None:
                back.append((pid, w))
        if not back:
            break
        send_chunked(session.tm, back, max_batch)
        tm = session.reopen()
        for pid, w in back:
            now = _num(tm.config.get(pid))
            curve.append((w, now))
            if _close_enough(now, left[pid]):
                del left[pid]
        if not left:
            say('restored original values')
            return True
    say('!! could not restore exactly; originals were: ' + ', '.join(
        '0x%04x=%s' % (p, e) for p, e in left.items()))
    return False


def curve_table(points):
    """Pool probe points by wire value: [(wire, eng)] sorted, duplicates merged."""
    seen = {}
    for p in points:
        seen.setdefault(p['wire'], p['eng'])
    return sorted(seen.items())


# ---------------------------------------------------------------- batch

INPUTS = '1-16'
BUSES = 'main,aux1-6,aux7,aux9'
GEQ_BUSES = 'main,aux1-6'          # Aux 7/8 and 9/10 have no GEQ or anti-feedback
STEREO = 'st17,st19'
FX = 'fx1-4'

# Plans run by `map probe-all`, in order: (section, param, band, channels[, steps]).
# param '*' means every numbered band in the section (the GEQ sliders).
# Enums (Comp Mode, EQ/cut Type) are left out on purpose: switching a mode can
# reset other parameters, which the restore would not undo.

# Input channel processing (all mapped: see the curve tables in params.py)
PROBE_PLAN = [
    ('Control', 'Delay Time', 1, INPUTS),
    ('Lo-Cut', 'Freq', 1, INPUTS),
    ('Hi-Cut', 'Freq', 1, INPUTS),
    ('EQ', 'Freq', 1, INPUTS),          # known law; checks the probe against it
    ('EQ', 'Q', 1, INPUTS),
    ('Gate', 'Attack', 1, INPUTS),
    ('Gate', 'Release', 1, INPUTS),
    ('Gate', 'Hold', 1, INPUTS),
    ('Gate', 'Attenuation', 1, INPUTS),
    ('Gate', 'Ratio', 1, INPUTS),
    ('Comp', 'Thresh', 1, INPUTS),
    ('Comp', 'Ratio', 1, INPUTS),
    ('Comp', 'Makeup Gain', 1, INPUTS),
    ('Comp', 'Attack', 1, INPUTS),
    ('Comp', 'Release', 1, INPUTS),
    ('Comp', 'Knee', 1, INPUTS),
    ('Comp', 'Compression', 1, INPUTS),
    ('De-Esser', 'Amount', 1, INPUTS),
    ('De-Esser', 'Frequency Tune', 1, INPUTS),
    ('Control', 'AuxBalance1', 1, INPUTS),
    ('Control', 'Width', 1, 'st17,st19'),
    ('Control', 'Trim', 1, 'st17,st19'),
    ('Control', 'Fader', 1, 'aux7,aux9'),
]

# Bus, FX return, stereo input, 2-track and talkback processing
BUS_PLAN = [
    ('Control', 'Gain', 1, BUSES),
    ('Control', 'Delay Time', 1, BUSES),
    ('EQ', 'Freq', 1, BUSES), ('EQ', 'Gain', 1, BUSES), ('EQ', 'Q', 1, BUSES),
    ('EQ', 'Freq', 6, BUSES), ('EQ', 'Gain', 6, BUSES), ('EQ', 'Q', 6, BUSES),
    ('Lo-Cut', 'Freq', 1, BUSES),
    ('Hi-Cut', 'Freq', 1, BUSES),
    ('Comp', 'Thresh', 1, BUSES), ('Comp', 'Ratio', 1, BUSES), ('Comp', 'Attack', 1, BUSES),
    ('Comp', 'Release', 1, BUSES), ('Comp', 'Compression', 1, BUSES),
    ('GEQ', '*', 1, GEQ_BUSES),
    ('Anti FB', 'Freq', 1, GEQ_BUSES), ('Anti FB', 'Gain', 1, GEQ_BUSES), ('Anti FB', 'Q', 1, GEQ_BUSES),
    ('Anti FB', 'Depth', 1, GEQ_BUSES), ('Anti FB', 'Width', 1, GEQ_BUSES),
    ('EQ', 'Freq', 1, FX), ('EQ', 'Gain', 1, FX), ('EQ', 'Q', 1, FX), ('EQ', 'Freq', 2, FX),
    ('HPF', 'Freq', 1, FX),
    ('Control', 'Width', 1, FX), ('Control', 'AuxBalance1', 1, FX),
    ('EQ', 'Freq', 1, STEREO), ('EQ', 'Gain', 1, STEREO), ('EQ', 'Q', 1, STEREO),
    ('Lo-Cut', 'Freq', 1, STEREO), ('Hi-Cut', 'Freq', 1, STEREO),
    ('Gate', 'Thresh', 1, STEREO), ('Gate', 'Attack', 1, STEREO), ('Gate', 'Release', 1, STEREO),
    ('Comp', 'Thresh', 1, STEREO), ('Comp', 'Ratio', 1, STEREO), ('Comp', 'Makeup Gain', 1, STEREO),
    ('Comp', 'Attack', 1, STEREO), ('Comp', 'Release', 1, STEREO),
    ('Control', 'Digital Trim', 1, 'pb'),
    ('EQ', 'Freq', 1, 'pb'), ('EQ', 'Gain', 1, 'pb'), ('EQ', 'Q', 1, 'pb'),
    ('HPF', 'Freq', 1, 'pb'),
    ('Control', 'TB Fader', 1, 'talkback'),
]

# On/off flags: 5 steps, to see where they switch. Not probed on purpose:
# Phantom (48 V on whatever is plugged in), Link and Is stereo (linking copies
# settings between channels), SigGen, and the headphone/monitor levels.
FLAG_STEPS = 5
FLAG_PLAN = [(s, p, 1, ch, FLAG_STEPS) for s, p, ch in [
    ('Control', 'DCA Asn1', INPUTS), ('Control', 'Mute Asn1', INPUTS), ('Control', 'AuxMute1', INPUTS),
    ('Control', 'Channel Safe', INPUTS), ('Control', 'Arm', INPUTS), ('Control', 'Rec Pre/Post', INPUTS),
    ('EQ', 'FxEnbl', INPUTS), ('EQ', 'BndEnbl', INPUTS),
    ('Lo-Cut', 'FxEnbl', INPUTS), ('Lo-Cut', 'BndEnbl', INPUTS), ('Hi-Cut', 'BndEnbl', INPUTS),
    ('Gate', 'Enable', INPUTS), ('Comp', 'Enable', INPUTS), ('Comp', 'Auto', INPUTS),
    ('De-Esser', 'Enable', INPUTS),
    ('Control', 'DCA Asn1', BUSES), ('Control', 'Mute Asn1', BUSES), ('Control', 'Channel Safe', BUSES),
    ('Control', 'Pre/Post', 'aux1-6,aux7,aux9'),
    ('EQ', 'FxEnbl', BUSES), ('Comp', 'Enable', BUSES), ('GEQ', 'FxEnbl', GEQ_BUSES),
    ('Anti FB', 'FxEnbl', GEQ_BUSES),
    ('Control', 'Mute', 'main,aux1-6,fx1-4,dca1-8'),
]]

# FX processor settings, all on FX 1: each group first switches FX 1 to that
# processor (the setup write), and FX 1's processor is put back at the end.
# Tap Tempo is left out: writing it acts like tapping.
FX_STEPS = 11
FX_SETTINGS = [
    ('chorus', 'Chorus', ['Rate', 'Depth', 'Waveform', 'Delay', 'HPF', 'LPF']),
    ('delay', 'Delay', ['Delay', 'Regen', 'DelayR', 'Regen R', 'HpfL', 'HpfR', 'LpfL', 'LpfR',
                        'Param Link', 'Filter Link']),
    ('pitch', 'PitchShift', ['Left Shift', 'Right Shift', 'Delay L', 'Delay R', 'P.Corr. Rate', 'Coarse edit',
                             'HPF', 'LPF', 'Key']),
    ('dense', 'D-Reverb', ['Rev Time', 'Initial Delay', 'Hi Ratio', 'Room', 'Diffusion', 'Reverb Delay', 'ER',
                           'HPF', 'LPF']),
    ('lush', 'L-Reverb', ['Size', 'Pre Delay', 'Room', 'Low Rev Time', 'X-over', 'High Rev Time', 'Diffusion',
                          'HPF', 'LPF']),
    ('mono-delay', 'Mono Delay', ['Delay', 'Regen', 'HpfL', 'LpfL']),
]
FX_PLAN = [(section, param, 1, 'fx1', FX_STEPS, (('FX 1 > Editor > FX type', int_to_norm(FX_TYPES[kind])),))
           for kind, section, params in FX_SETTINGS for param in params]

# Mute groups, and the monitor/headphone levels. Unplug headphones first: the
# levels are swept to full.
EXTRAS_PLAN = [
    ('Control', 'Mute', 1, 'mute1-8', FLAG_STEPS),
    ('Control', 'Monitor / Cue Level', 1, 'global'),
    ('Control', 'Phones / Cue Level', 1, 'global'),
    ('Control', 'CR gain boost', 1, 'global'),
    ('Control', 'Phones gain boost', 1, 'global'),
]

PLANS = {'inputs': PROBE_PLAN, 'buses': BUS_PLAN, 'flags': FLAG_PLAN, 'fx': FX_PLAN, 'extras': EXTRAS_PLAN}


def plan_items(plan):
    """Normalize plan entries to (section, param, band, channels, steps or None,
    setup or None). setup is ((param, wire value), ...) written before the probe
    (FX 1's processor, say); the original values are put back after the plan."""
    return [tuple(item) + (None,) * (6 - len(item)) for item in plan]


def plan_pids(params, section, param, band, channels):
    """Parameter indexes for a plan entry."""
    pids = []
    for c in parse_channels(channels):
        path = '%s > %s' % (strip_path(c), section)
        if param == '*':
            found = [p.index for p in params.by_index.values()
                     if p.path == path and p.name.strip()[:1].isdigit()]
            if not found:
                raise KeyError('no numbered bands in %s' % path)
            pids.extend(found)
        else:
            pids.append(params.find(path, param, band - 1).index)
    return pids


def plan_name(section, param, band, *_):
    return '%s %s%s' % (section, 'bands' if param == '*' else param, ' %d' % band if band > 1 else '')


def fit_curve(table):
    """Describe a probed curve: [(wire, eng)] -> (kind, formula, max error).
    Tries linear, exponential (log-scaled, e.g. Hz or ms) and the fader law."""
    pts = [(w, e) for w, e in table if isinstance(e, float)]
    if len(pts) < 3:
        return 'text', 'non-numeric values: %s' % ', '.join(str(e) for _, e in table[:6]), None
    distinct = len(set(e for _, e in pts))
    if distinct == 1:
        return 'constant', 'never changed (%s)' % pts[0][1], None
    if set(e for _, e in pts) == {0.0, 1.0}:
        on = min(w for w, e in pts if e == 1.0)
        off = max(w for w, e in pts if e == 0.0)
        if off < on:
            return 'switch', 'off up to %g, on from %g' % (off, on), 0.0
        return 'switch?', 'not monotonic', None
    fits = []
    n = len(pts)
    xs, ys = [w for w, _ in pts], [e for _, e in pts]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    a = sum((x - mx) * (y - my) for x, y in pts) / sxx
    b = my - a * mx
    fits.append(('linear', 'eng = %.6g*x %+.6g' % (a, b), max(abs(a * x + b - y) for x, y in pts)))
    if all(y > 0 for y in ys):
        ly = [math.log(y) for y in ys]
        mly = sum(ly) / n
        k = sum((x - mx) * (l - mly) for x, l in zip(xs, ly)) / sxx
        c = math.exp(mly - k * mx)
        r = math.exp(k)
        err = max(abs(c * r ** x - y) / y for x, y in pts)   # relative
        fits.append(('exponential', 'eng = %.6g * %.6g^x' % (c, r), err * max(ys)))
    ends = dict(pts)
    # only for curves starting at 0 (or below), where a plain exponential cannot fit
    if 0.0 in ends and 0.5 in ends and 1.0 in ends and ends[0.0] <= 0 and ends[0.5] != ends[0.0]:
        lo, mid, hi = ends[0.0], ends[0.5], ends[1.0]
        u = (hi - lo) / (mid - lo) - 1          # r^0.5
        if u > 0 and u != 1:
            r = u * u
            f = lambda x: lo + (hi - lo) * (r ** x - 1) / (r - 1)
            fits.append(('exponential + offset', 'eng = %g + %g*(%.6g^x - 1)/%.6g' % (lo, hi - lo, r, r - 1),
                         max(abs(f(x) - y) for x, y in pts)))
    fits.append(('fader law', 'dB = 67*x^0.69 - 55', max(abs(norm_to_fader_db(x) - y) for x, y in pts)))
    kind, formula, err = min(fits, key=lambda f: f[2])
    if distinct < len(pts) * 0.6:
        kind += ', stepped (%d distinct values)' % distinct
    return kind, formula, err


RESTORE_POINT = 'restore-point.json'


def save_restore_point(path, host, mixer, label, originals):
    """Originals of the probe about to run, for `map restore` after a crash."""
    save(path, 'restore', host, mixer, dict(label=label, originals={'0x%04x' % p: e for p, e in originals.items()}))


def probe_all(session, plan, steps, outdir, save_fn, say=print, restore=True, checkpoint=None):
    """Run every probe in plan on one session. A failing probe is reported and
    skipped. checkpoint(name, originals) runs before each probe writes.
    Returns [(name, file or None, fit or error text)]."""
    summary = []
    plan = plan_items(plan)
    # setup parameters are integers (FX type): remember them to put back at the end
    setup_originals = {}
    for item in plan:
        for p, _ in item[5] or ():
            pid = session.params.find(*p.rsplit(' > ', 1)).index
            setup_originals.setdefault(pid, int_to_norm(int(float(session.tm.config.get(pid) or 0))))
    current_setup = None
    for i, (section, param, band, channels, item_steps, setup) in enumerate(plan, 1):
        name = plan_name(section, param, band)
        say('\n=== %d/%d  %s on %s' % (i, len(plan), name, channels))
        try:
            if setup and setup != current_setup:
                say('setup: %s' % ', '.join('%s = %g' % s for s in setup))
                session.tm.set_many(list(setup))
                time.sleep(1.5)                     # the mixer loads the processor's default preset
                session.reopen()
                current_setup = setup
            chans = parse_channels(channels)
            pids = plan_pids(session.params, section, param, band, channels)
            cp = (lambda originals, name=name: checkpoint(name, originals)) if checkpoint else None
            result = probe(session, pids, sweep_values(item_steps or steps), say=say, restore=restore,
                           checkpoint=cp)
            slug = '%s-%s' % (section, 'bands' if param == '*' else param) + ('-%d' % band if band > 1 else '')
            slug = ''.join(ch if ch.isalnum() else '-' for ch in slug.lower()).strip('-')
            path = os.path.join(outdir, default_out('probe-' + slug))
            save_fn(path, dict(section=section, param=param, band=band, channels=chans, **result))
            fit = fit_curve(curve_table(result['points']))
            if result.get('warning'):
                fit = ('constant', result['warning'], None)
            if restore and not result.get('restored'):
                fit = (fit[0] + ', NOT RESTORED', fit[1], fit[2])
            summary.append((name, path, fit))
        except Exception as e:      # keep going: one odd parameter must not stop the batch
            say('!! %s failed: %s' % (name, e))
            summary.append((name, None, ('error', str(e), None)))
            try:
                session.reopen()
            except Exception as e2:
                say('!! could not reconnect: %s' % e2)
                break
    if setup_originals:
        say('\nputting back: %s' % ', '.join('0x%04x = %g' % kv for kv in setup_originals.items()))
        try:
            session.tm.set_many(list(setup_originals.items()))
            time.sleep(1.5)
            session.reopen()
        except Exception as e:
            say('!! could not put the setup back: %s' % e)
    return summary


def format_summary(summary):
    lines = ['%-26s %-34s %-32s %s' % ('control', 'kind', 'formula', 'max err')]
    for name, _, (kind, formula, err) in summary:
        lines.append('%-26s %-34s %-32s %s' % (name, kind, formula, '' if err is None else '%.4g' % err))
    return '\n'.join(lines)


def parse_channels(text):
    """'1-16', '1,3,5', 'aux1-6', '1-4,main'."""
    out = []
    for part in text.split(','):
        part = part.strip()
        m = re.fullmatch(r'([a-zA-Z]*)(\d+)-(\d+)', part)
        if m:
            out.extend('%s%d' % (m.group(1), n) for n in range(int(m.group(2)), int(m.group(3)) + 1))
        elif part:
            out.append(part)
    return out


# ---------------------------------------------------------------- output

def save(path, kind, host, mixer_name, body):
    doc = dict(kind=kind, created=datetime.datetime.now().isoformat(timespec='seconds'),
               mixer=mixer_name, host=host)
    doc.update(body)
    with open(path, 'w') as f:
        f.write(privacy.redact(json.dumps(doc, indent=1)))     # no custom device names in shared files
    return path


def default_out(kind):
    return 'mapping-%s-%s.json' % (kind, datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
