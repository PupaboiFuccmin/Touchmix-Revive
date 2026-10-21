"""Parameter table (from the mixer's own NH4-config.xml) and value scaling."""
import csv
import math
import os

_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'params.csv')


class Param:
    __slots__ = ('index', 'path', 'name', 'type')

    def __init__(self, index, path, name, type_):
        self.index, self.path, self.name, self.type = index, path, name, type_

    @property
    def full_name(self):
        return '%s > %s' % (self.path, self.name)

    def __repr__(self):
        return '<Param 0x%04x %s>' % (self.index, self.full_name)


class ParamTable:
    def __init__(self, csv_path=_CSV):
        self.by_index = {}
        self._by_key = {}
        with open(csv_path, newline='') as f:
            for row in csv.DictReader(f):
                p = Param(int(row['index']), row['path'], row['name'], int(row['type']))
                self.by_index[p.index] = p
                # several params share path+name (e.g. the four EQ bands' "Freq"),
                # kept in index order so occurrence n is band n+1
                self._by_key.setdefault((p.path.lower(), p.name.lower()), []).append(p)

    def __getitem__(self, index):
        return self.by_index[index]

    def get(self, index):
        return self.by_index.get(index)

    def find(self, path, name, occurrence=0):
        hits = self._by_key.get((path.lower(), name.lower()))
        if not hits or occurrence >= len(hits):
            raise KeyError('no parameter %r > %r (#%d)' % (path, name, occurrence + 1))
        return hits[occurrence]

    def search(self, text):
        text = text.lower()
        return [p for p in self.by_index.values() if text in p.full_name.lower()]


def strip_path(target):
    """Map a short channel name to its path prefix in the table.

    1..16 -> 'In N', st17/st19 -> 'Stereo In 17/18' / 'Stereo In 19/20', main, aux1..aux6,
    aux7 (or aux7/8), aux9 (or aux9/10), fx1..fx4, dca1..dca8.
    """
    t = str(target).strip().lower().replace(' ', '')
    if t.isdigit() and 1 <= int(t) <= 16:
        return 'In %d' % int(t)
    if t in ('main', 'mainmix', 'lr'):
        return 'Main'
    if t.startswith('st') and t[2:].isdigit():
        return 'Stereo In 17/18' if int(t[2:]) in (17, 18) else 'Stereo In 19/20'
    for prefix, label in (('aux', 'Aux'), ('fx', 'FX'), ('dca', 'DCA')):
        if t.startswith(prefix):
            n = t[len(prefix):].split('/')[0]
            if n.isdigit():
                n = int(n)
                if prefix == 'aux' and n in (7, 8):
                    return 'Aux 7/8'
                if prefix == 'aux' and n in (9, 10):
                    return 'Aux 9/10'
                return '%s %d' % (label, n)
    raise ValueError('unknown channel %r (use 1-16, st17, st19, main, aux1-10, fx1-4, dca1-8)' % target)


# ---------------------------------------------------------------- scaling
# Wire values are normalized 0..1. Only the conversions below are confirmed
# from captures; see docs/PROTOCOL.md "Value scaling".

def clamp01(x):
    return min(1.0, max(0.0, float(x)))


def eq_freq_to_norm(hz):
    return clamp01(math.log(float(hz) / 20.0) / math.log(1000.0))


def norm_to_eq_freq(x):
    return 20.0 * 1000.0 ** x


def eq_gain_to_norm(db):
    return clamp01((float(db) + 15.0) / 30.0)


def norm_to_eq_gain(x):
    return 30.0 * x - 15.0


def trim_to_norm(db):
    """Input trim (digital gain), linear +-15 dB: from `map probe Trim`."""
    return clamp01((float(db) + 15.0) / 30.0)


def norm_to_trim(x):
    return 30.0 * x - 15.0


def send_name(dest):
    """Send parameter name on an input: 'aux1' -> 'AuxSend1', aux 7 or 8 ->
    'AuxSend7/8', 9 or 10 -> 'AuxSend9/10', 'fx2' -> 'FXSend2'. A bare number is an aux."""
    t = str(dest).strip().lower().replace(' ', '')
    fx = t.startswith('fx')
    n = (t[2:] if fx else t.replace('aux', '')).split('/')[0]
    if n.isdigit():
        n = int(n)
        if fx and 1 <= n <= 4:
            return 'FXSend%d' % n
        if not fx and 1 <= n <= 6:
            return 'AuxSend%d' % n
        if not fx and n in (7, 8):
            return 'AuxSend7/8'
        if not fx and n in (9, 10):
            return 'AuxSend9/10'
    raise ValueError('unknown send %r (use aux1-aux10 or fx1-fx4)' % dest)


def gate_thresh_to_norm(db):
    """Gate threshold, linear -60..0 dB: from `map probe Thresh --section Gate`."""
    return clamp01((float(db) + 60.0) / 60.0)


def norm_to_gate_thresh(x):
    return 60.0 * x - 60.0


def pan_to_norm(pan):
    """pan: -100 (hard left) .. 0 (centre) .. +100 (hard right).
    Also accepts 'L', 'C', 'R', 'L30', 'R25'."""
    s = str(pan).strip().upper()
    if s == 'C':
        v = 0.0
    elif s[:1] in ('L', 'R'):
        amount = float(s[1:]) if len(s) > 1 else 100.0
        v = -amount if s[0] == 'L' else amount
    else:
        v = float(s)
    v = max(-100.0, min(100.0, v))
    return round((v + 100.0) / 2.0) / 100.0   # the app only ever sends multiples of 0.005


def norm_to_pan(x):
    return x * 200.0 - 100.0


# Fader law, from `map probe Fader` on In 1-16 (33 points, exact to 0.0001 dB):
#   dB = 67 * x^0.69 - 55,  x = 0 is -inf (the config stores it as -55), x = 1 is +12 dB
# Every master probed (Main, Aux 1-6, FX 1-4, DCA 1-8) and the input aux and FX
# sends follow the same law.
FADER_MIN_DB = -55.0
FADER_MAX_DB = 12.0
FADER_EXP = 0.69


def norm_to_fader_db(x):
    """Wire value -> dB. Returns FADER_MIN_DB (-55, meaning -inf) at 0."""
    x = clamp01(x)
    return FADER_MIN_DB + (FADER_MAX_DB - FADER_MIN_DB) * x ** FADER_EXP


def fader_db_to_norm(db):
    db = float(db)
    if db <= FADER_MIN_DB:
        return 0.0
    if db >= FADER_MAX_DB:
        return 1.0
    return ((db - FADER_MIN_DB) / (FADER_MAX_DB - FADER_MIN_DB)) ** (1.0 / FADER_EXP)


FADER_0DB = fader_db_to_norm(0.0)   # 0.7512; the app sends 0.75 (-0.06 dB)


def fader_to_norm(value):
    """Fader position. Accepts a 0..1 float, 'off' / '-inf', or dB as
    '-10db', '+6dB', 'unity' (-55 dB and below is off, +12 dB is the top)."""
    s = str(value).strip().lower().replace(' ', '')
    if s in ('off', '-inf', 'inf', '-infdb'):
        return 0.0
    if s == 'unity':
        return FADER_0DB
    if s.endswith('db'):
        return fader_db_to_norm(s[:-2])
    return clamp01(s)
