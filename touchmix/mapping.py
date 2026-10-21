"""Mapping assistant: find out which parameter IDs a control uses and what its
wire values mean.

Two ways in:

* guided: prompts you to do one thing at a time on the mixer (or the app) and
  records every parameter change that arrives while you do it. With readback,
  it reconnects after each step and also records the mixer's own engineering
  value (dB, Hz, ...) for each changed ID, from NH4-config.xml.
* probe: writes a sweep of wire values to a parameter itself, reconnects, and
  reads the engineering values back. That maps a whole curve with no hands.
  The same control on several channels is swept in parallel, so one reconnect
  reads back one point per channel.

Results are written as JSON. They hold only IDs, names and values, never the
raw config, so they are safe to commit.
"""
import datetime
import json
import threading
import time

from .client import TouchMix
from .params import ParamTable, strip_path

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


def probe(session, pids, values, say=print, restore=True):
    """Sweep `values` across `pids` and read the mixer's engineering values back.

    Each round gives every pid a different value from the list, so with N pids
    it needs ceil(len(values) / N) reconnects. Restores the original values at
    the end when the curve allows it."""
    tm = session.tm
    originals = {pid: _num(tm.config.get(pid)) for pid in pids}
    points = []                      # (pid, wire, eng)
    todo = list(values)
    rnd = 0
    while todo:
        rnd += 1
        batch = [(pid, todo.pop(0)) for pid in pids if todo]
        tm.set_many(batch)
        say('round %d: sent %s' % (rnd, ', '.join('%.4f' % v for _, v in batch)))
        tm = session.reopen()
        for pid, v in batch:
            eng = _num(tm.config.get(pid))
            points.append((pid, v, eng))
            say('    0x%04x  %.4f -> %s' % (pid, v, eng))

    distinct = set(e for _, _, e in points)
    result = dict(points=[dict(id='0x%04x' % p, wire=w, eng=e) for p, w, e in points],
                  originals={'0x%04x' % p: e for p, e in originals.items()})
    if len(distinct) <= 1:
        result['warning'] = 'config value never changed: the mixer may not store writes, or this ID is wrong'
        say('!! ' + result['warning'])

    if restore:
        result['restored'] = _restore(session, originals, [(w, e) for _, w, e in points], say)
    return result


def _restore(session, originals, curve, say, tries=3):
    """Put each pid back to its original engineering value by inverting the
    measured curve. Each pass adds the points it lands on, so a curve that is
    not linear between sweep points converges in a pass or two."""
    curve = list(curve)
    left = dict(originals)
    for _ in range(tries):
        back = []
        for pid, eng in left.items():
            w = interp_inverse(curve, eng)
            if w is not None:
                back.append((pid, w))
        if not back:
            break
        session.tm.set_many(back)
        tm = session.reopen()
        for pid, w in back:
            now = _num(tm.config.get(pid))
            curve.append((w, now))
            if now == left[pid] or (isinstance(now, float) and abs(now - left[pid]) < 0.05):
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


# ---------------------------------------------------------------- output

def save(path, kind, host, mixer_name, body):
    doc = dict(kind=kind, created=datetime.datetime.now().isoformat(timespec='seconds'),
               mixer=mixer_name, host=host)
    doc.update(body)
    with open(path, 'w') as f:
        json.dump(doc, f, indent=1)
    return path


def default_out(kind):
    return 'mapping-%s-%s.json' % (kind, datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
