"""Copy a recording session off a TouchMix USB drive, one file per track,
named after the mixer's channels.

The mixer records to the drive as (TouchMix user guide, "Recording Directory
Structure"):

    SessionName.tmRecord/
        Track1/ Region-1.wav Region-2.wav ...   one region per start/stop
        ...
    SessionName.tmRecordhdr                     written when STOP is pressed

On a TouchMix-16, Track1-16 are inputs 1-16, Track17/18 and Track19/20 are the
two stereo inputs (left, right), and Track21/22 the 2-track recording (left,
right). Unarmed channels leave their folder empty. The files are mono
broadcast WAV (a `bext` chunk, originator "MicroX") holding 32-bit integer PCM
at the mixer's sample rate. QSC's docs say 32-bit float, but on firmware
3.0.9551 the format tag and the sample values are both integer. Most DAWs
(Cubase included) import them directly.

Nothing on the drive is changed: the guide warns that editing anything there
makes the session unusable on the mixer.
"""
import glob
import os
import re
import shutil
import string
import sys

SESSION_SUFFIX = '.tmRecord'
HEADER_SUFFIX = '.tmRecordhdr'


class Track:
    __slots__ = ('number', 'label', 'regions')

    def __init__(self, number, label, regions):
        self.number, self.label, self.regions = number, label, regions


class Session:
    __slots__ = ('name', 'path', 'mtime', 'has_header', 'tracks')

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)[:-len(SESSION_SUFFIX)]
        self.has_header = os.path.exists(path + 'hdr')
        self.tracks = []
        times = [os.path.getmtime(path)]
        for d in glob.glob(os.path.join(path, 'Track*')):
            m = re.fullmatch(r'Track(\d+)', os.path.basename(d))
            if not m or not os.path.isdir(d):
                continue
            # one listing, matched case-insensitively (globbing *.wav and *.WAV
            # finds every file twice on Windows)
            regions = sorted((os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith('.wav')),
                             key=_region_order)
            if regions:
                self.tracks.append(Track(int(m.group(1)), None, regions))
                times.extend(os.path.getmtime(r) for r in regions)
        self.tracks.sort(key=lambda t: t.number)
        self.mtime = max(times)

    @property
    def size(self):
        return sum(os.path.getsize(r) for t in self.tracks for r in t.regions)


def _region_order(path):
    m = re.search(r'(\d+)\.wav$', path, re.I)
    return (int(m.group(1)) if m else 0, path)


# ---------------------------------------------------------------- finding

def candidate_roots():
    """Places a USB drive may be mounted."""
    if sys.platform.startswith('win'):
        return ['%s:\\' % c for c in string.ascii_uppercase if c not in 'AB' and os.path.exists('%s:\\' % c)]
    roots = glob.glob('/Volumes/*')
    user = os.environ.get('USER', '')
    for pattern in ('/media/%s/*' % user, '/run/media/%s/*' % user, '/media/*', '/mnt/*'):
        roots += glob.glob(pattern)
    return roots


def find_drives(roots=None):
    """Roots that hold at least one TouchMix session."""
    return [r for r in (roots if roots is not None else candidate_roots())
            if glob.glob(os.path.join(r, '*' + SESSION_SUFFIX))]


def sessions(root):
    """Sessions on a drive, newest first."""
    found = [Session(p) for p in glob.glob(os.path.join(root, '*' + SESSION_SUFFIX)) if os.path.isdir(p)]
    return sorted(found, key=lambda s: s.mtime, reverse=True)


# ---------------------------------------------------------------- naming

def track_channel(number):
    """(params.csv channel path, side) for a TouchMix-16 track number."""
    if 1 <= number <= 16:
        return 'In %d' % number, ''
    if number in (17, 18):
        return 'Stereo In 17/18', 'L' if number == 17 else 'R'
    if number in (19, 20):
        return 'Stereo In 19/20', 'L' if number == 19 else 'R'
    if number in (21, 22):
        return '2-Track', 'L' if number == 21 else 'R'
    return 'Track %d' % number, ''


def default_label(number):
    channel, side = track_channel(number)
    short = {'Stereo In 17/18': 'St 17-18', 'Stereo In 19/20': 'St 19-20'}.get(channel, channel)
    return ('%s %s' % (short, side)).strip()


def mixer_channel_names(tm):
    """{channel path: name} from a connected TouchMix, for channels that have a name."""
    names = {}
    for ch in ['In %d' % n for n in range(1, 17)] + ['Stereo In 17/18', 'Stereo In 19/20']:
        try:
            pid = tm.params.find(ch + ' > Control', 'Name').index
        except KeyError:
            continue
        text = (tm.config_text.get(pid) or '').strip()
        if text:
            names[ch] = text
    return names


def label_tracks(session, names=None):
    names = names or {}
    for t in session.tracks:
        channel, side = track_channel(t.number)
        name = names.get(channel)
        t.label = ('%s %s' % (name, side)).strip() if name else default_label(t.number)
    return session


def safe_filename(text):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', text).strip(' .')
    return text or 'track'


def plan_copy(session, out_dir):
    """[(source, destination)] for every region of every recorded track."""
    plan = []
    for t in session.tracks:
        base = '%02d %s' % (t.number, safe_filename(t.label))
        for i, src in enumerate(t.regions, 1):
            name = base + ('' if len(t.regions) == 1 else ' - R%d' % i) + '.wav'
            plan.append((src, os.path.join(out_dir, name)))
    return plan


def copy_session(plan, say=print):
    """Copy, never overwriting. A file that already exists with the same size is
    skipped; one with a different size gets ' (2)', ' (3)', ... Returns
    (copied, skipped)."""
    copied = skipped = 0
    for src, dst in plan:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
            skipped += 1
            say('  skip  %s (already there)' % os.path.basename(dst))
            continue
        root, ext = os.path.splitext(dst)
        n = 2
        while os.path.exists(dst):
            dst = '%s (%d)%s' % (root, n, ext)
            n += 1
        shutil.copy2(src, dst)
        copied += 1
        say('  copy  %s' % os.path.basename(dst))
    return copied, skipped


def default_out_dir():
    music = os.path.join(os.path.expanduser('~'), 'Music')
    return os.path.join(music if os.path.isdir(music) else os.getcwd(), 'TouchMix')
