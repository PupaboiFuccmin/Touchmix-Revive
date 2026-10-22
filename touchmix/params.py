"""Parameter table (from the mixer's own NH<n>-config.xml) and value scaling."""
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
                self._by_key.setdefault((p.path.lower(), p.name.strip().lower()), []).append(p)

    def __getitem__(self, index):
        return self.by_index[index]

    def get(self, index):
        return self.by_index.get(index)

    def find(self, path, name, occurrence=0):
        hits = self._by_key.get((path.strip().lower(), name.strip().lower()))
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
    if t in ('pb', '2trk', '2-trk', '2-trkpb', 'playback'):
        return '2-Trk PB'
    if t in ('talkback', 'tb'):
        return 'Talkback'
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


# ---------------------------------------------------------------- curve table
# Every control mapped so far. All from `map probe` / `map probe-all` readbacks
# (mappings/*.json), each an exact fit, and the tests check every saved point
# against these tables. Units are the likely ones for the range; the config
# stores numbers only.

class Curve:
    """kind is 'linear' (lo + (hi-lo)*x), 'exp' (lo * (hi/lo)^x), 'expoff'
    (lo + (hi-lo) * (r^x - 1)/(r - 1), an exponential that starts at 0),
    'fader' or 'switch'."""

    def __init__(self, kind, lo=0.0, hi=1.0, unit='', ratio=None):
        self.kind, self.lo, self.hi, self.unit, self.ratio = kind, lo, hi, unit, ratio

    def from_norm(self, x):
        x = clamp01(x)
        if self.kind == 'fader':
            return norm_to_fader_db(x)
        if self.kind == 'switch':
            return 1.0 if x >= 0.5 else 0.0
        if self.kind == 'exp':
            return self.lo * (self.hi / self.lo) ** x
        if self.kind == 'expoff':
            return self.lo + (self.hi - self.lo) * (self.ratio ** x - 1) / (self.ratio - 1)
        return self.lo + (self.hi - self.lo) * x

    def to_norm(self, value):
        """Real units -> wire value. Plain numbers are in the curve's unit (dB for faders)."""
        s = str(value).strip().lower()
        if self.kind == 'fader':
            try:
                return fader_db_to_norm(float(s))
            except ValueError:
                return fader_to_norm(s)     # '-10db', 'off', 'unity'
        if self.kind == 'switch':
            return 1.0 if s in ('on', '1', '1.0', 'true', 'yes') else 0.0
        if self.unit and s.endswith(self.unit.lower()):
            s = s[:-len(self.unit)]
        v = float(s)
        if self.kind == 'exp':
            v = min(max(v, min(self.lo, self.hi)), max(self.lo, self.hi))
            return clamp01(math.log(v / self.lo) / math.log(self.hi / self.lo))
        if self.kind == 'expoff':
            v = min(max(v, self.lo), self.hi)
            return clamp01(math.log(1 + (v - self.lo) * (self.ratio - 1) / (self.hi - self.lo)) / math.log(self.ratio))
        return clamp01((v - self.lo) / (self.hi - self.lo))

    def format(self, x):
        v = self.from_norm(x)
        if self.kind == 'fader' and v <= FADER_MIN_DB:
            return '-inf dB'
        if self.kind == 'switch':
            return 'on' if v else 'off'
        return '%.4g%s' % (v, ' ' + self.unit if self.unit else '')

    def __repr__(self):
        return '<Curve %s %g..%g %s>' % (self.kind, self.lo, self.hi, self.unit)


FADER = Curve('fader', FADER_MIN_DB, FADER_MAX_DB, 'dB')
SWITCH = Curve('switch')                # every on/off flag probed switches at 0.5
PAN = Curve('linear', -100, 100)
EQ_FREQ = Curve('exp', 20, 20000, 'Hz')
EQ_GAIN = Curve('linear', -15, 15, 'dB')
EQ_Q = Curve('exp', 0.4, 4)
LOW_CUT = Curve('exp', 20, 2000, 'Hz')  # input low cut, bus low cut, FX/2-track HPF

# Curves by channel type, then (section, name). A name ending in '*' matches
# any name starting with the rest ('AuxSend*' covers AuxSend1..AuxSend9/10).
COMMON = {
    ('Control', 'Fader'): FADER,
    ('Control', 'Mute'): SWITCH,
    ('Control', 'Solo'): SWITCH,
}

# In 1-16 and the stereo inputs (with STEREO_CURVES on top)
INPUT_CURVES = {
    ('Control', 'Trim'): Curve('linear', -15, 15, 'dB'),
    ('Control', 'Pan'): PAN,
    ('Control', 'Width'): PAN,
    ('Control', 'Delay Time'): Curve('linear', 0, 100, 'ms'),
    ('Control', 'AuxSend*'): FADER,
    ('Control', 'FXSend*'): FADER,
    ('Control', 'AuxBalance*'): PAN,
    ('Control', 'AuxMute*'): SWITCH,
    ('Control', 'DCA Asn*'): SWITCH,
    ('Control', 'Mute Asn*'): SWITCH,
    ('Control', 'Phase'): SWITCH,
    ('Control', 'Delay Enable'): SWITCH,
    ('Control', 'Channel Safe'): SWITCH,
    ('Control', 'Arm'): SWITCH,
    ('Control', 'Rec Pre/Post'): SWITCH,
    ('EQ', 'Freq'): EQ_FREQ,
    ('EQ', 'Gain'): EQ_GAIN,
    ('EQ', 'Q'): EQ_Q,
    ('EQ', 'FxEnbl'): SWITCH,
    ('EQ', 'BndEnbl'): SWITCH,
    ('Lo-Cut', 'Freq'): LOW_CUT,
    ('Lo-Cut', 'FxEnbl'): SWITCH,
    ('Lo-Cut', 'BndEnbl'): SWITCH,
    ('Hi-Cut', 'Freq'): Curve('exp', 1000, 20000, 'Hz'),
    ('Hi-Cut', 'BndEnbl'): SWITCH,
    ('Gate', 'Enable'): SWITCH,
    ('Gate', 'Thresh'): Curve('linear', -60, 0, 'dB'),
    ('Gate', 'Attack'): Curve('exp', 0.5, 50, 'ms'),
    ('Gate', 'Release'): Curve('exp', 10, 2000, 'ms'),
    ('Gate', 'Hold'): Curve('exp', 10, 2000, 'ms'),
    ('Gate', 'Attenuation'): Curve('linear', 0, 100, 'dB'),
    ('Gate', 'Ratio'): Curve('linear', 1, 100),        # linear, unlike comp ratio
    ('Comp', 'Enable'): SWITCH,
    ('Comp', 'Auto'): SWITCH,
    ('Comp', 'Thresh'): Curve('linear', -60, 0, 'dB'),
    ('Comp', 'Ratio'): Curve('exp', 1, 20),
    ('Comp', 'Makeup Gain'): Curve('linear', 0, 20, 'dB'),
    ('Comp', 'Attack'): Curve('exp', 0.5, 50, 'ms'),
    ('Comp', 'Release'): Curve('exp', 50, 1000, 'ms'),
    ('Comp', 'Knee'): SWITCH,
    ('Comp', 'Compression'): Curve('linear', 0, 1),    # the Simple-mode single knob
    ('De-Esser', 'Enable'): SWITCH,
    ('De-Esser', 'Amount'): Curve('linear', 0, 10),
    ('De-Esser', 'Frequency Tune'): Curve('exp', 1000, 10000, 'Hz'),
}

STEREO_CURVES = {
    ('Control', 'Trim'): Curve('linear', 0, 12, 'dB'),   # stereo inputs: 0..+12, not +-15
}

# Main and Aux 1-10 (Aux 7/8 and 9/10 have no GEQ or Anti FB)
BUS_CURVES = {
    ('Control', 'Gain'): Curve('linear', -10, 20, 'dB'),
    ('Control', 'Delay Time'): Curve('linear', 0, 100, 'ms'),
    ('Control', 'DCA Asn*'): SWITCH,
    ('Control', 'Mute Asn*'): SWITCH,
    ('Control', 'Channel Safe'): SWITCH,
    # stored as wire * 1024; Aux 1-6 read 0 and Aux 7/8, 9/10 read 1. Meaning unconfirmed.
    ('Control', 'Pre/Post'): Curve('linear', 0, 1024),
    ('EQ', 'Freq'): EQ_FREQ,
    ('EQ', 'Gain'): EQ_GAIN,
    ('EQ', 'Q'): EQ_Q,
    ('EQ', 'FxEnbl'): SWITCH,
    ('Lo-Cut', 'Freq'): LOW_CUT,
    ('Hi-Cut', 'Freq'): Curve('exp', 50, 20000, 'Hz'),   # wider than on inputs
    ('Comp', 'Enable'): SWITCH,
    ('Comp', 'Thresh'): Curve('linear', -60, 0, 'dB'),
    ('Comp', 'Ratio'): Curve('exp', 5, 100),              # not 1..20 as on inputs
    ('Comp', 'Attack'): Curve('expoff', 0, 25, 'ms', ratio=100),
    ('Comp', 'Release'): Curve('exp', 50, 1000, 'ms'),
    ('Comp', 'Compression'): Curve('linear', 0, 1),
    ('GEQ', 'FxEnbl'): SWITCH,
    ('GEQ', '#'): EQ_GAIN,                                # '#': the numbered band sliders
    ('Anti FB', 'FxEnbl'): SWITCH,
    ('Anti FB', 'Freq'): EQ_FREQ,
    ('Anti FB', 'Gain'): Curve('linear', -20, 0, 'dB'),
    ('Anti FB', 'Q'): Curve('exp', 3, 14),
    ('Anti FB', 'Depth'): Curve('linear', 0, 100),
    ('Anti FB', 'Width'): Curve('linear', 10, 30),
}

FX_CURVES = {                           # FX 1-4 returns
    ('Control', 'Width'): PAN,
    ('Control', 'AuxBalance*'): PAN,
    ('EQ', 'Freq'): EQ_FREQ,
    ('EQ', 'Gain'): EQ_GAIN,
    ('EQ', 'Q'): EQ_Q,
    ('HPF', 'Freq'): LOW_CUT,
}

PLAYBACK_CURVES = {                     # 2-Trk PB
    ('Control', 'Digital Trim'): Curve('linear', -12, 0, 'dB'),
    ('EQ', 'Freq'): EQ_FREQ,
    ('EQ', 'Gain'): EQ_GAIN,
    ('EQ', 'Q'): EQ_Q,
    ('HPF', 'Freq'): LOW_CUT,
}

TALKBACK_CURVES = {
    ('Control', 'TB Fader'): FADER,
}


def channel_kind(channel):
    """'input', 'bus', 'fx', 'playback', 'talkback', 'dca' or None, from a path prefix."""
    if channel.startswith(('In ', 'Stereo In')):
        return 'input'
    if channel == 'Main' or channel.startswith('Aux '):
        return 'bus'
    for prefix, kind in (('FX ', 'fx'), ('2-Trk PB', 'playback'), ('Talkback', 'talkback'), ('DCA ', 'dca')):
        if channel.startswith(prefix):
            return kind
    return None


KIND_CURVES = {'input': INPUT_CURVES, 'bus': BUS_CURVES, 'fx': FX_CURVES,
               'playback': PLAYBACK_CURVES, 'talkback': TALKBACK_CURVES, 'dca': {}}


def _lookup(table, section, name):
    if (section, name) in table:
        return table[(section, name)]
    if name[:1].isdigit() and (section, '#') in table:
        return table[(section, '#')]
    for (sec, pattern), curve in table.items():
        if sec == section and pattern.endswith('*') and name.startswith(pattern[:-1]):
            return curve
    return None


def curve_for(path, name):
    """Curve for a parameter, e.g. curve_for('In 4 > Comp', 'Thresh'), or None if not mapped."""
    channel, _, section = path.rpartition(' > ')
    name = name.strip()
    kind = channel_kind(channel)
    if kind is None:
        return None
    if channel.startswith('Stereo In'):
        curve = _lookup(STEREO_CURVES, section, name)
        if curve:
            return curve
    return _lookup(KIND_CURVES[kind], section, name) or _lookup(COMMON, section, name)
