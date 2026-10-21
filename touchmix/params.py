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

    1..16 -> 'In N', st17/st19 -> 'Stereo In N', main, aux1..aux6,
    aux7 (or aux7/8), aux9 (or aux9/10), fx1..fx4, dca1..dca8.
    """
    t = str(target).strip().lower().replace(' ', '')
    if t.isdigit() and 1 <= int(t) <= 16:
        return 'In %d' % int(t)
    if t in ('main', 'mainmix', 'lr'):
        return 'Main'
    if t.startswith('st') and t[2:].isdigit():
        return 'Stereo In %d' % (17 if int(t[2:]) in (17, 18) else 19)
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


FADER_0DB = 0.75   # observed: app sends 0.75 for 0 dB


def fader_to_norm(value):
    """Fader position. Accepts a 0..1 float, 'off' / '-inf', or '0db'.
    Other dB values need the fader curve, which is not known yet."""
    s = str(value).strip().lower()
    if s in ('off', '-inf', 'inf'):
        return 0.0
    if s in ('0db', '0 db', 'unity'):
        return FADER_0DB
    if s.endswith('db'):
        raise ValueError('only 0dB is known so far; give the fader as 0..1 (0.75 = 0 dB)')
    return clamp01(s)
