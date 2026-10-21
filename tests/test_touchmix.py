import contextlib
import io
import os
import struct
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from touchmix import TouchMix, discover, mapping as M, protocol as P, params as S  # noqa: E402
from touchmix.cli import main, parse_channels  # noqa: E402
from fake_mixer import FakeMixer, fader_db  # noqa: E402

# bytes captured from the official Android app (docs/PROTOCOL.md)
APP_HELLO = bytes.fromhex(
    '010003003b00' '01000000' '0300' '1300' '0000' '2900'
    + 'Lenovo-Tab-P11-Plus'.encode().hex() + 'MicroX16-AndroidTablet'.encode().hex())
APP_PAN_LEFT = bytes.fromhex('050001001400' '01000000' '0800' '2102' '0000' '00000000')
APP_POLL = bytes.fromhex('060001003400' '01000000' '28000000' '03000a000300'
                         '00000100000101000002010000030100000401000005010000060100' '00070100')


class Protocol(unittest.TestCase):
    def test_hello_matches_app(self):
        self.assertEqual(P.encode_hello('Lenovo-Tab-P11-Plus'), APP_HELLO)

    def test_param_set_matches_app(self):
        self.assertEqual(P.encode_param_set([(0x221, 0.0)]), APP_PAN_LEFT)

    def test_poll_matches_app(self):
        self.assertEqual(P.encode_poll(1), APP_POLL)

    def test_reader_splits_and_buffers(self):
        data = APP_HELLO + APP_PAN_LEFT
        r = P.Reader()
        self.assertEqual(r.feed(data[:10]), [])
        msgs = r.feed(data[10:])
        self.assertEqual([m[0] for m in msgs], [P.HELLO, P.PARAM_SET])

    def test_decode_param_set(self):
        (pid, tag, raw, value), = P.decode_param_set(APP_PAN_LEFT[6:])
        self.assertEqual((pid, tag, value), (0x221, 0, 0.0))


class Scaling(unittest.TestCase):
    def test_eq_freq_matches_captures(self):
        for hz, norm in ((80, 0.2007), (400, 0.4337), (2000, 0.6667), (20000, 1.0)):
            self.assertAlmostEqual(S.eq_freq_to_norm(hz), norm, places=3)

    def test_eq_gain(self):
        self.assertEqual(S.eq_gain_to_norm(-15), 0.0)
        self.assertEqual(S.eq_gain_to_norm(0), 0.5)
        self.assertEqual(S.eq_gain_to_norm(15), 1.0)

    def test_pan(self):
        self.assertEqual(S.pan_to_norm('L'), 0.0)
        self.assertEqual(S.pan_to_norm('C'), 0.5)
        self.assertEqual(S.pan_to_norm('R'), 1.0)
        self.assertEqual(S.pan_to_norm('L50'), 0.25)
        self.assertEqual(S.pan_to_norm(-50), 0.25)

    def test_fader(self):
        # wire -> dB read back from the mixer by `map probe Fader`
        for x, db in ((0.03125, -48.8691), (0.25, -29.2573), (0.5, -13.4698), (0.75, -0.0627),
                      (0.9375, 9.0818), (1.0, 12.0)):
            self.assertAlmostEqual(S.norm_to_fader_db(x), db, places=3)
            self.assertAlmostEqual(S.fader_to_norm('%gdb' % db), x, places=4)
        self.assertEqual(S.norm_to_fader_db(0), -55.0)
        self.assertAlmostEqual(S.fader_to_norm('0db'), 0.7512, places=4)
        self.assertEqual(S.fader_to_norm('unity'), S.fader_to_norm('0 dB'))
        self.assertEqual(S.fader_to_norm('off'), 0.0)
        self.assertEqual(S.fader_to_norm('-60db'), 0.0)
        self.assertEqual(S.fader_to_norm('+20db'), 1.0)
        self.assertEqual(S.fader_to_norm('0.6'), 0.6)

    def test_trim(self):
        # from `map probe Trim`: linear, -15 dB at 0, +15 dB at 1
        for x, db in ((0.0, -15.0), (0.35, -4.5), (0.5, 0.0), (0.9, 12.0), (1.0, 15.0)):
            self.assertAlmostEqual(S.norm_to_trim(x), db, places=6)
            self.assertAlmostEqual(S.trim_to_norm(db), x, places=6)
        self.assertEqual(S.trim_to_norm(40), 1.0)

    def test_send_name(self):
        self.assertEqual(S.send_name('aux1'), 'AuxSend1')
        self.assertEqual(S.send_name(6), 'AuxSend6')
        self.assertEqual(S.send_name('aux8'), 'AuxSend7/8')
        self.assertEqual(S.send_name('aux9/10'), 'AuxSend9/10')
        self.assertEqual(S.send_name('FX 3'), 'FXSend3')
        self.assertRaises(ValueError, S.send_name, 'aux11')
        self.assertRaises(ValueError, S.send_name, 'fx5')
        t = S.ParamTable()
        self.assertEqual(t.find('In 4 > Control', S.send_name(9)).index, 0x1277)
        self.assertEqual(t.find('In 4 > Control', S.send_name('fx1')).index, 0x127E)

    def test_gate_thresh(self):
        # from `map probe Thresh --section Gate`: linear, -60 dB at 0, 0 dB at 1
        for x, db in ((0.0, -60.0), (0.25, -45.0), (0.85, -9.0), (1.0, 0.0)):
            self.assertAlmostEqual(S.norm_to_gate_thresh(x), db, places=6)
            self.assertAlmostEqual(S.gate_thresh_to_norm(db), x, places=6)

    def test_channel_names(self):
        self.assertEqual(S.strip_path(4), 'In 4')
        self.assertEqual(S.strip_path('aux8'), 'Aux 7/8')
        self.assertEqual(S.strip_path('fx2'), 'FX 2')
        self.assertEqual(S.strip_path('st17'), 'Stereo In 17/18')
        self.assertEqual(S.strip_path('st20'), 'Stereo In 19/20')
        S.ParamTable().find(S.strip_path('st19') + ' > Control', 'Fader')
        self.assertRaises(ValueError, S.strip_path, 17)


class Params(unittest.TestCase):
    def test_known_ids(self):
        t = S.ParamTable()
        self.assertEqual(t.find('In 4 > Control', 'Fader').index, 0x21E)
        self.assertEqual(t.find('In 5 > Control', 'Mute').index, 0x26B)
        self.assertEqual(t.find('In 4 > EQ', 'Gain', 0).index, 0x255)
        self.assertEqual(t.find('In 4 > EQ', 'Freq', 3).index, 0x263)
        self.assertEqual(t.find('In 4 > Control', 'FXSend1').index, 0x127E)


class Client(unittest.TestCase):
    def setUp(self):
        self.mixer = FakeMixer()

    def tearDown(self):
        self.mixer.close()

    def test_discover(self):
        found = discover(['127.0.0.1/32'], port=self.mixer.port)
        self.assertEqual(found, [('127.0.0.1', 'Test Mixer', 'MicroX16')])

    def test_cli_auto_discovers(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = main(['--subnet', '127.0.0.1/32', '--port', str(self.mixer.port), 'info'])
        self.assertEqual(rc, 0)
        self.assertIn('Test Mixer', out.getvalue())

    def test_discover_ignores_closed_port(self):
        self.assertEqual(discover(['127.0.0.1/32'], port=1), [])

    def test_connect_and_control(self):
        with TouchMix('127.0.0.1', port=self.mixer.port, name='Revive-Test') as tm:
            tm.wait_ready(5)
            self.assertEqual(tm.mixer_name, 'Test Mixer')
            self.assertEqual(tm.config_value('In 4 > Control > Mute'), ('0', None))
            self.assertEqual(tm.config_text[548], 'Vox')
            tm.mute(4, True)
            time.sleep(0.3)
        hello = [p for t, p in self.mixer.received if t == P.HELLO][0]
        self.assertEqual(P.decode_hello(hello), ('Revive-Test', 'MicroX16-AndroidTablet'))
        self.assertIn(P.POLL_REQ, [t for t, _ in self.mixer.received])
        self.assertEqual(self.mixer.mute, 1)

    def test_access_rights(self):
        with TouchMix('127.0.0.1', port=self.mixer.port) as tm:
            tm.wait_ready(5)
            self.assertEqual(tm.wait_access(), {'All': True, 'Main Mix': False, 'Locked': False})
            self.assertTrue(tm.can_write())
        self.mixer.access = False
        with TouchMix('127.0.0.1', port=self.mixer.port) as tm:
            tm.wait_ready(5)
            self.assertFalse(tm.can_write())

    def test_cli_warns_without_access(self):
        self.mixer.access = False
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'mute', '4', 'on', '--verify'])
            rc_probe = main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'map', 'probe', 'Fader', '--yes'])
            main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'info'])
        self.assertEqual(rc, 2)
        self.assertEqual(rc_probe, 1)
        self.assertIn('no access', err.getvalue())
        self.assertIn('access: NONE', out.getvalue())
        self.assertEqual(self.mixer.mute, 0)

    def test_cli_fader_negative_db(self):
        for value, wire in (('-10db', S.fader_db_to_norm(-10)), ('-inf', 0.0)):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'fader', '4', value])
            self.assertEqual(rc, 0)
            sent = [v for t, p in self.mixer.received if t == P.PARAM_SET
                    for pid, _, _, v in P.decode_param_set(p) if pid == 0x21E]
            self.assertAlmostEqual(sent[-1], wire, places=6)

    def test_cli_trim_and_send(self):
        port = str(self.mixer.port)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--host', '127.0.0.1', '--port', port, 'trim', '4', '-3']), 0)
            self.assertEqual(main(['--host', '127.0.0.1', '--port', port, 'send', '4', 'aux2', '-10db']), 0)
            self.assertEqual(main(['--host', '127.0.0.1', '--port', port, 'send', '4', 'fx1', '0db']), 0)
            self.assertEqual(main(['--host', '127.0.0.1', '--port', port, 'gate', '4', '-40db']), 0)
            self.assertEqual(main(['--host', '127.0.0.1', '--port', port, 'gate', '4', 'on']), 0)
        sent = {pid: v for t, p in self.mixer.received if t == P.PARAM_SET
                for pid, _, _, v in P.decode_param_set(p)}
        self.assertAlmostEqual(sent[0x22C], 0.4, places=6)
        self.assertAlmostEqual(sent[0x126B], S.fader_db_to_norm(-10), places=6)
        self.assertAlmostEqual(sent[0x127E], S.FADER_0DB, places=6)
        self.assertAlmostEqual(sent[0x242], 1 / 3.0, places=6)
        self.assertEqual(sent[0x241], 1.0)

    def test_cli_mute_verify(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'mute', '4', 'on', '--verify'])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn('CHANGED', out.getvalue())



class MappingHelpers(unittest.TestCase):
    def test_sweep_values(self):
        self.assertEqual(M.sweep_values(5), [0.0, 0.25, 0.5, 0.75, 1.0])

    def test_interp_inverse(self):
        pts = [(0.0, -55.0), (0.5, -10.0), (1.0, 10.0)]
        self.assertAlmostEqual(M.interp_inverse(pts, 0.0), 0.75)
        self.assertEqual(M.interp_inverse(pts, -80.0), 0.0)
        self.assertEqual(M.interp_inverse(pts, 20.0), 1.0)
        self.assertIsNone(M.interp_inverse([(0.0, 1.0), (0.5, 3.0), (1.0, 2.0)], 2.5))
        self.assertIsNone(M.interp_inverse(pts, 'abc'))

    def test_parse_channels(self):
        self.assertEqual(parse_channels('1-3,main,aux2'), ['1', '2', '3', 'main', 'aux2'])
        self.assertEqual(parse_channels('aux1-3'), ['aux1', 'aux2', 'aux3'])

    def test_builtin_scripts_resolve(self):
        t = S.ParamTable()
        for name, (fn, target, _) in M.SCRIPTS.items():
            if fn:
                self.assertTrue(fn(t, target) if target else fn(t), name)


class Mapping(unittest.TestCase):
    def setUp(self):
        self.mixer = FakeMixer()
        self.out = []

    def tearDown(self):
        self.mixer.close()

    def session(self, rec=None):
        s = M.Session('127.0.0.1', self.mixer.port, 'Revive-Test', rec)
        s.open()
        return s

    def scripted(self, actions):
        """ask() replacement: each item is (callable or None, reply)."""
        actions = list(actions)

        def ask(prompt):
            act, reply = actions.pop(0)
            if act:
                act()
                time.sleep(0.2)
            return reply
        return ask

    def test_guided_records_changes_and_reads_back(self):
        rec = M.Recorder()
        s = self.session(rec)
        steps = [dict(prompt='In 4 fader to 0 dB', expect=['In 4 > Control > Fader']),
                 dict(prompt='In 4 mute on', expect=[0x21F]),
                 dict(prompt='skip me')]
        ask = self.scripted([
            (lambda: (self.mixer.push(0x21E, 0.5), self.mixer.apply(0x21E, 0.75), self.mixer.push(0x21E, 0.75)), ''),
            (lambda: self.mixer.push(0xCE, 0.5), 'n mixer screen showed nothing'),
            (None, ''),
            (None, 's'),
        ])
        try:
            res = M.guided(s, steps, readback=True, ask=ask, say=self.out.append)
        finally:
            s.close()
        first, second, third = res
        c, = first['changes']
        self.assertEqual((c['id'], c['first'], c['last'], c['count']), ('0x021e', 0.5, 0.75, 2))
        self.assertEqual(c['name'], 'In 4 > Control > Fader')
        self.assertEqual(c['config'], 0.0)
        self.assertTrue(first['expect_seen'])
        self.assertFalse(second['expect_seen'])
        self.assertTrue(second['changes'][0]['background'])
        self.assertEqual(second['note'], 'mixer screen showed nothing')
        self.assertTrue(third['skipped'])
        self.assertIn('!! expected 0x021f, not seen', '\n'.join(self.out))

    def test_guided_redo_and_free(self):
        rec = M.Recorder()
        s = self.session(rec)
        ask = self.scripted([
            (None, 'pan left'),
            (lambda: self.mixer.push(0x21F, 1.0), 'r'),
            (lambda: self.mixer.push(0x221, 0.0), ''),
            (None, ''),
        ])
        try:
            res = M.guided(s, [], ask=ask, say=self.out.append, free=True)
        finally:
            s.close()
        step, = res
        self.assertEqual(step['prompt'], 'pan left')
        self.assertEqual([c['id'] for c in step['changes']], ['0x0221'])

    def test_probe_sweeps_reads_back_and_restores(self):
        s = self.session()
        try:
            res = M.probe(s, [0x21E, 0x26A], M.sweep_values(9), say=self.out.append)
        finally:
            s.close()
        table = M.curve_table(res['points'])
        self.assertEqual(len(table), 9)
        self.assertEqual(table[0], (0.0, -55.0))
        self.assertEqual(dict(table)[0.75], 0.0)
        self.assertEqual(dict(table)[1.0], fader_db(1.0))
        self.assertTrue(res['restored'], self.out)
        self.assertEqual(self.mixer.values[542], -55.0)
        self.assertAlmostEqual(self.mixer.values[618], -6.0, delta=0.05)
        self.assertNotIn('warning', res)

    def test_probe_warns_when_nothing_changes(self):
        s = self.session()
        try:
            res = M.probe(s, [0x0FB1], M.sweep_values(3), say=self.out.append, restore=False)
        finally:
            s.close()
        self.assertIn('warning', res)

    def test_cli_probe_writes_json(self):
        import json
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), 'p.json')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'map', 'probe', 'Fader',
                       '--channels', '4-5', '--steps', '5', '--yes', '--out', path])
        self.assertEqual(rc, 0)
        with open(path) as f:
            doc = json.load(f)
        self.assertEqual((doc['kind'], doc['mixer'], doc['param']), ('probe', 'Test Mixer', 'Fader'))
        self.assertEqual(len(doc['points']), 5)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(['map', 'show', path])
        self.assertIn('0.7500    0.0', out.getvalue())


if __name__ == '__main__':
    unittest.main()
