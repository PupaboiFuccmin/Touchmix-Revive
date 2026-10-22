import contextlib
import io
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)

from touchmix import collect as C  # noqa: E402
from touchmix.cli import main  # noqa: E402
from fake_mixer import FakeMixer  # noqa: E402


def make_session(root, name, tracks, header=True, age=0):
    """A session laid out like the mixer writes it: {track number: [region sizes]}."""
    path = os.path.join(root, name + C.SESSION_SUFFIX)
    for n in range(1, 23):
        d = os.path.join(path, 'Track%d' % n)
        os.makedirs(d)
        for i, size in enumerate(tracks.get(n, []), 1):
            with open(os.path.join(d, 'Region-%d.wav' % i), 'wb') as f:
                f.write(b'RIFF' + bytes(size))
    if header:
        with open(os.path.join(root, name + C.HEADER_SUFFIX), 'w') as f:     # e.g. DEFAULT.tmRecordhdr
            f.write('<DQData/>')
    t = time.time() - age
    for dirpath, dirs, files in os.walk(path):
        for x in dirs + files:
            os.utime(os.path.join(dirpath, x), (t, t))
    os.utime(path, (t, t))
    return path


class Drive(unittest.TestCase):
    def setUp(self):
        self.drive = tempfile.mkdtemp()
        self.out = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.drive, 'TouchMix-Presets'))
        make_session(self.drive, 'OLD', {1: [10]}, age=3600)
        make_session(self.drive, 'GIG', {1: [100, 50], 4: [100], 17: [80], 18: [80], 21: [60], 22: [60]})
        make_session(self.drive, 'BROKEN', {2: [10]}, header=False, age=7200)

    def test_finds_drive_and_newest_session(self):
        self.assertEqual(C.find_drives([self.drive, self.out]), [self.drive])
        found = C.sessions(self.drive)
        self.assertEqual([s.name for s in found], ['GIG', 'OLD', 'BROKEN'])
        gig = found[0]
        self.assertEqual([t.number for t in gig.tracks], [1, 4, 17, 18, 21, 22])
        self.assertEqual([os.path.basename(r) for r in gig.tracks[0].regions], ['Region-1.wav', 'Region-2.wav'])
        self.assertTrue(gig.has_header)
        self.assertFalse(found[2].has_header)

    def test_names_and_file_plan(self):
        gig = C.label_tracks(C.sessions(self.drive)[0], {'In 4': 'Vox', 'Stereo In 17/18': 'Keys/Synth'})
        names = [os.path.basename(d) for _, d in C.plan_copy(gig, self.out)]
        self.assertEqual(names, ['01 In 1 - R1.wav', '01 In 1 - R2.wav', '04 Vox.wav', '17 Keys-Synth L.wav',
                                 '18 Keys-Synth R.wav', '21 2-Track L.wav', '22 2-Track R.wav'])

    def test_copy_never_overwrites(self):
        gig = C.label_tracks(C.sessions(self.drive)[0])
        plan = C.plan_copy(gig, self.out)
        quiet = lambda *a: None
        self.assertEqual(C.copy_session(plan, say=quiet), (7, 0))
        self.assertEqual(C.copy_session(plan, say=quiet), (0, 7))       # same size: skipped
        with open(plan[2][1], 'wb') as f:
            f.write(b'edited in the DAW')
        self.assertEqual(C.copy_session(plan, say=quiet), (1, 6))
        self.assertTrue(os.path.exists(plan[2][1].replace('.wav', ' (2).wav')))
        with open(plan[2][1], 'rb') as f:
            self.assertEqual(f.read(), b'edited in the DAW')             # the user's file is untouched

    def test_cli_collect_with_mixer_names(self):
        mixer = FakeMixer()          # its In 4 is named 'Vox'
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = main(['--host', '127.0.0.1', '--port', str(mixer.port),
                           'collect', '--drive', self.drive, '--out', self.out])
        finally:
            mixer.close()
        self.assertEqual(rc, 0, out.getvalue())
        folder, = os.listdir(self.out)
        self.assertTrue(folder.startswith('GIG '))
        self.assertIn('04 Vox.wav', os.listdir(os.path.join(self.out, folder)))
        self.assertIn('Channel names from Test Mixer', out.getvalue())

    def test_cli_collect_list_and_session(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(['collect', '--drive', self.drive, '--list'])
            main(['collect', '--drive', self.drive, '--session', 'old', '--no-names', '--out', self.out])
        self.assertIn('no header', out.getvalue())
        folder, = os.listdir(self.out)
        self.assertEqual(os.listdir(os.path.join(self.out, folder)), ['01 In 1.wav'])


if __name__ == '__main__':
    unittest.main()
