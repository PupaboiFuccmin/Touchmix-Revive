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
from fake_mixer import FakeMixer  # noqa: E402

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

    def test_decode_text(self):
        # 9.1 from the mixer when the MP3 player selected a song (2026-10-21)
        payload = bytes.fromhex('180100001200') + b'ERN FULL SONG (tw)'
        self.assertEqual(P.decode_text(payload), (0x0118, 'ERN FULL SONG (tw)'))

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

    def test_curves_match_every_probe(self):
        """Every point in mappings/*.json must come out of params.curve_for."""
        import glob
        import json
        files = glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'mappings', '*.json'))
        self.assertTrue(files)
        table = S.ParamTable()
        checked = 0
        for f in files:
            with open(f) as fh:
                doc = json.load(fh)
            if doc.get('kind') != 'probe' or doc.get('warning'):
                continue
            for p in doc['points']:
                par = table[int(p['id'], 16)]
                curve = S.curve_for(par.path, par.name)
                self.assertIsNotNone(curve, '%s (%s)' % (par.full_name, f))
                self.assertAlmostEqual(curve.from_norm(p['wire']), p['eng'], delta=0.01 + abs(p['eng']) * 1e-5,
                                       msg='%s at %s (%s)' % (par.full_name, p['wire'], f))
                if curve.kind not in ('switch', 'steps') and curve.from_norm(p['wire']) > S.FADER_MIN_DB:
                    self.assertAlmostEqual(curve.to_norm(p['eng']), p['wire'], places=4,
                                           msg='%s inverse at %s' % (par.full_name, p['eng']))
                checked += 1
        self.assertGreater(checked, 500)

    def test_curve_lookup(self):
        self.assertEqual(S.curve_for('Stereo In 17/18 > Control', 'Trim').hi, 12)
        self.assertEqual(S.curve_for('In 4 > Control', 'Trim').hi, 15)
        self.assertIs(S.curve_for('Aux 1 > Control', 'Fader'), S.FADER)
        self.assertIsNone(S.curve_for('Main > Comp', 'Mode'))       # enums are not mapped
        self.assertIsNone(S.curve_for('FX 1 > Editor', 'Param 1'))     # generic editor slots: unmapped
        self.assertEqual(S.curve_for('FX 1 > D-Reverb', 'Rev Time').format(0.5), '1.817 s')
        self.assertEqual(S.curve_for('FX 2 > PitchShift', 'Key').format(0.3), '4')
        self.assertAlmostEqual(S.curve_for('Global > Control', 'Phones / Cue Level').from_norm(1.0), 24.0)
        self.assertAlmostEqual(S.curve_for('Global > Control', 'Phones / Cue Level').to_norm('-55'), 0.0)
        self.assertIs(S.curve_for('Mute 3 > Control', 'Mute'), S.SWITCH)
        self.assertIsNone(S.curve_for('DCA 1 > Control', 'Trim'))
        self.assertEqual(S.curve_for('Main > Comp', 'Ratio').hi, 100)    # buses differ from inputs
        self.assertEqual(S.curve_for('In 4 > Comp', 'Ratio').hi, 20)
        self.assertIs(S.curve_for('Aux 3 > GEQ', '1000 '), S.EQ_GAIN)
        self.assertIs(S.curve_for('In 4 > Control', 'AuxSend9/10'), S.FADER)
        self.assertIs(S.curve_for('Talkback > Control', 'TB Fader'), S.FADER)
        self.assertAlmostEqual(S.curve_for('Main > Comp', 'Attack').to_norm(2.2727), 0.5, places=4)
        self.assertIsNone(S.curve_for('In 4 > Control', 'Name'))
        c = S.curve_for('In 4 > Control', 'Delay Time')
        self.assertEqual((c.to_norm('25ms'), c.format(0.25)), (0.25, '25 ms'))
        self.assertEqual(S.FADER.format(0), '-inf dB')
        self.assertEqual(S.curve_for('In 4 > Comp', 'Knee').to_norm('on'), 1.0)

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


class MDNS(unittest.TestCase):
    @staticmethod
    def name(n):
        return b''.join(bytes([len(p)]) + p.encode() for p in n.split('.')) + b'\0'

    def rr(self, name, rtype, data):
        return self.name(name) + struct.pack('>HHIH', rtype, 0x8001, 120, len(data)) + data

    def test_parse_mixer_answer(self):
        """Same records the TouchMix-16 sent for _ds2inet._tcp (2026-10-21)."""
        from touchmix.discover import parse_dns, dns_query
        inst = 'QSC TouchMix._ds2inet._tcp.local'
        txt = b''.join(bytes([len(t)]) + t for t in
                       (b'ProtocolVersion=1', b'SWVersion=196608', b'DeviceType=MicroX16'))
        pkt = (struct.pack('>HHHHHH', 0, 0x8400, 0, 4, 0, 0)
               + self.rr('_ds2inet._tcp.local', 12, self.name(inst))
               + self.rr(inst, 16, txt)
               + self.rr(inst, 33, struct.pack('>HHH', 0, 0, 54596) + self.name('QSC TouchMix.local'))
               + self.rr('QSC TouchMix.local', 1, bytes([192, 168, 10, 129])))
        recs = {(n, t): v for n, t, v in parse_dns(pkt)}
        self.assertEqual(recs[(inst, 33)], (54596, 'QSC TouchMix.local'))
        self.assertEqual(recs[(inst, 16)]['DeviceType'], 'MicroX16')
        self.assertEqual(recs[('QSC TouchMix.local', 1)], '192.168.10.129')
        self.assertEqual(recs[('_ds2inet._tcp.local', 12)], inst)
        self.assertTrue(dns_query('_ds2inet._tcp.local').endswith(b'\x08_ds2inet\x04_tcp\x05local\x00\x00\x0c\x00\x01'))

    def test_parse_compressed_names(self):
        from touchmix.discover import parse_dns
        # the PTR target points back at the question name (offset 12)
        q = self.name('_ds2inet._tcp.local')
        ans = b'\xc0\x0c' + struct.pack('>HHIH', 12, 1, 120, 2) + b'\xc0\x0c'
        pkt = struct.pack('>HHHHHH', 0, 0x8400, 1, 1, 0, 0) + q + struct.pack('>HH', 12, 1) + ans
        (name, rtype, value), = parse_dns(pkt)
        self.assertEqual((name, rtype, value), ('_ds2inet._tcp.local', 12, '_ds2inet._tcp.local'))


class Client(unittest.TestCase):
    def setUp(self):
        self.mixer = FakeMixer()

    def tearDown(self):
        self.mixer.close()

    def test_discover(self):
        found = discover(['127.0.0.1/32'], port=self.mixer.port)
        self.assertEqual(found, [('127.0.0.1', 'Test Mixer', 'MicroX16', self.mixer.port)])
        self.assertEqual(found[0].port, self.mixer.port)

    def test_cli_host_with_port(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = main(['--host', '127.0.0.1:%d' % self.mixer.port, 'info'])
        self.assertEqual(rc, 0)
        self.assertIn('Test Mixer', out.getvalue())

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
            self.assertEqual(tm.config_text[554], 'Vox')
            tm.mute(4, True)
            time.sleep(0.3)
        hello = [p for t, p in self.mixer.received if t == P.HELLO][0]
        self.assertEqual(P.decode_hello(hello), ('Revive-Test', 'MicroX16-AndroidTablet'))
        self.assertIn(P.POLL_REQ, [t for t, _ in self.mixer.received])
        self.assertEqual(self.mixer.mute, 1)

    def test_status_before_hello(self):
        self.mixer.status_before_hello = True
        with TouchMix('127.0.0.1', port=self.mixer.port) as tm:
            tm.wait_ready(5)
            self.assertEqual(tm.mixer_name, 'Test Mixer')
            time.sleep(0.2)
            self.assertIn(0xBC, tm.live)        # the early status is still handled

    def test_config_name_varies(self):
        self.mixer.config_name = 'NH8-config.xml.dscomp'
        with TouchMix('127.0.0.1', port=self.mixer.port) as tm:
            tm.wait_ready(5)
            self.assertEqual(tm.config_text[554], 'Vox')

    def test_config_refreshed_after_recall(self):
        with TouchMix('127.0.0.1', port=self.mixer.port) as tm:
            tm.wait_ready(5)
            self.assertEqual(tm.config.get(543), '0')
            self.mixer.values[543] = 1                       # the recalled scene has In 4 muted
            self.mixer.config_name = 'all-config.xml.dscomp'
            self.mixer.push_frame(self.mixer._config_file())
            time.sleep(0.3)
            self.assertEqual(tm.config.get(543), '1')

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
            self.assertEqual(main(['--host', '127.0.0.1', '--port', port, 'param', '4', 'Comp', 'Thresh', '-20']), 0)
            self.assertEqual(main(['--host', '127.0.0.1', '--port', port, 'param', '4', 'EQ', 'Q', '4', '--band', '3']), 0)
            self.assertEqual(main(['--host', '127.0.0.1', '--port', port, 'trim', 'st17', '6']), 0)
        sent = {pid: v for t, p in self.mixer.received if t == P.PARAM_SET
                for pid, _, _, v in P.decode_param_set(p)}
        self.assertAlmostEqual(sent[0x22C], 0.4, places=6)
        self.assertAlmostEqual(sent[0x126B], S.fader_db_to_norm(-10), places=6)
        self.assertAlmostEqual(sent[0x127E], S.FADER_0DB, places=6)
        self.assertAlmostEqual(sent[0x242], 1 / 3.0, places=6)
        self.assertEqual(sent[0x241], 1.0)
        self.assertAlmostEqual(sent[0x24A], 40 / 60.0, places=6)      # comp thresh -20 dB
        self.assertAlmostEqual(sent[0x260], 1.0, places=6)            # EQ band 3 Q = 4
        self.assertAlmostEqual(sent[0x5FE], 0.5, places=6)            # stereo trim 6 of 0..12 dB

    def test_cli_record(self):
        port = str(self.mixer.port)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--host', '127.0.0.1', '--port', port, 'record', 'arm', '1-2', '2trk']), 0)
            self.assertEqual(main(['--host', '127.0.0.1', '--port', port, 'record', 'start']), 0)
            self.assertEqual(main(['--host', '127.0.0.1', '--port', port, 'record', 'stop']), 0)
            self.assertEqual(main(['--host', '127.0.0.1', '--port', port, 'record', 'status']), 0)
        writes = [(pid, v) for t, p in self.mixer.received if t == P.PARAM_SET
                  for pid, _, _, v in P.decode_param_set(p)]
        # values the app sent in `map run transport`: Arm 1/0, Transport n/7
        self.assertIn((0x142, 1.0), writes)                 # In 1 > Control > Arm
        self.assertIn((0x18E, 1.0), writes)                 # In 2
        self.assertIn((0xDA6, 1.0), writes)                 # 2-Trk Rec > 2-Track Arm
        transport = [v for pid, v in writes if pid == 0xB8]
        self.assertEqual(len(transport), 2)
        self.assertAlmostEqual(transport[0], 2 / 7.0, places=6)
        self.assertEqual(transport[1], 0.0)
        self.assertIn('Transport:', out.getvalue())

    def test_cli_record_locate_and_wind(self):
        port = str(self.mixer.port)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            for args in (['ff'], ['rew'], ['locate', '0.5'], ['set'], ['goto']):
                self.assertEqual(main(['--host', '127.0.0.1', '--port', port, 'record'] + args), 0)
        writes = [(pid, round(v, 6)) for t, p in self.mixer.received if t == P.PARAM_SET
                  for pid, _, _, v in P.decode_param_set(p)]
        # what the mixer reported for >>, <<, Set and Go To on the Record/Play screen
        self.assertEqual(writes, [(0x00B8, round(3 / 7.0, 6)), (0x00B8, round(4 / 7.0, 6)), (0x00BF, 0.5),
                                  (0x00BD, 1.0), (0x00BD, 0.0), (0x00BE, 1.0), (0x00BE, 0.0)])

    def test_transport_values(self):
        self.assertAlmostEqual(S.transport_to_norm('play'), 0.142857, places=6)
        self.assertAlmostEqual(S.transport_to_norm('rtz'), 0.714286, places=6)
        self.assertEqual(S.transport_name('2.0'), 'record')
        self.assertEqual(S.transport_name(3), 'next')
        self.assertEqual(S.transport_name(6), '6')

    def test_text_messages_update_config_text(self):
        seen = []
        with TouchMix('127.0.0.1', port=self.mixer.port) as tm:
            tm.wait_ready(5)
            tm.on_change(lambda pid, v, raw, tag, src: seen.append((pid, src)))
            self.mixer.push_frame(P.frame(P.TEXT, bytes.fromhex('180100001100') + b'jackingcase(DEMO)'))
            time.sleep(0.3)
            self.assertEqual(tm.config_text[0x0118], 'jackingcase(DEMO)')
        self.assertIn((0x0118, 'text'), seen)

    def test_cli_mp3(self):
        port = str(self.mixer.port)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            for args in (['mode', 'on'], ['select', '2'], ['next'], ['top'], ['mode', 'off']):
                self.assertEqual(main(['--host', '127.0.0.1', '--port', port, 'mp3'] + args), 0)
        writes = [(pid, v) for t, p in self.mixer.received if t == P.PARAM_SET
                  for pid, _, _, v in P.decode_param_set(p)]
        # values the mixer reported in the MP3 player log
        self.assertEqual(writes[:2], [(0x00BA, 0.0), (0x00D2, 0.0)])               # MP3 mode
        self.assertIn((0x0117, 1 / 1024.0), writes)                               # song 2
        self.assertAlmostEqual(dict((p, v) for p, v in writes[3:4])[0x00B8], 3 / 7.0, places=6)   # next
        self.assertEqual(writes[-2:], [(0x00BA, 1.0), (0x00D2, 1 / 1024.0)])      # back to multitrack

    def test_cli_mp3_play_number(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'mp3', 'play', '2']), 0)
        writes = [(pid, v) for t, p in self.mixer.received if t == P.PARAM_SET
                  for pid, _, _, v in P.decode_param_set(p)]
        self.assertEqual(writes[0], (0x0117, 1 / 1024.0))          # select song 2 ...
        self.assertAlmostEqual(writes[1][1], 1 / 7.0, places=6)    # ... then play

    def test_monitor_log_with_notes(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), 'session.log')
        mixer = self.mixer

        class Typing(io.StringIO):
            """Types a note, then the 'action' happens on the mixer, then Enter on an empty line."""
            lines = ['recall scene STUDIO\n', '\n']

            def readline(self, *a):
                line = self.lines.pop(0)
                if not line.strip():
                    mixer.push(0x21F, 1.0)      # the action, after the note
                    time.sleep(0.3)
                return line
        real_stdin = sys.stdin
        sys.stdin = Typing()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'monitor', '--log', path])
        finally:
            sys.stdin = real_stdin
        self.assertEqual(rc, 0)
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
        kinds = [l.split()[1] for l in lines]
        self.assertEqual(kinds[0], 'CONNECTED')
        note = kinds.index('NOTE')
        self.assertIn('recall scene STUDIO', lines[note])
        self.assertTrue(any('In 4 > Control > Mute' in l for l in lines[note + 1:]))

    def test_map_tour_logs_steps(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), 'tour.log')
        mixer = self.mixer

        class Typing(io.StringIO):
            # fx section: step 1 takes a note, step 2 cycles options (two labels,
            # then empty), step 3 is plain
            lines = ['Lush Reverb\n', 'Hall\n', 'Plate\n', '\n', '\n']

            def readline(self, *a):
                line = self.lines.pop(0)
                if line == 'Hall\n':
                    mixer.push(0x21F, 1.0)      # what the mixer reports for that option
                    time.sleep(0.3)
                return line
        real_stdin = sys.stdin
        sys.stdin = Typing()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'map', 'tour', 'fx', '--out', path])
        finally:
            sys.stdin = real_stdin
        self.assertEqual(rc, 0)
        with open(path, encoding='utf-8') as f:
            log = f.read()
        kinds = [l.split()[1] for l in log.splitlines()]
        markers = [k for k in kinds if k in ('CONNECTED', 'SECTION', 'STEP', 'END')]
        self.assertEqual(markers, ['CONNECTED', 'SECTION'] + ['STEP', 'END'] * 3)
        # each answer in the fx section is followed by a read-back of FX 1's processor
        self.assertEqual(log.count(' READ '), 8)          # 4 answers x 2 watched settings
        self.assertIn('READ 2.2 0x0b96 FX 1 > Editor > FX type', log)
        self.assertIn('LABEL 1 Lush Reverb', log)
        self.assertLess(log.index('In 4 > Control > Mute'), log.index('LABEL 2.1 Hall'))
        self.assertIn('LABEL 2.2 Plate', log)
        self.assertIn('END 3', log)

    def test_cli_eq_shelf(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'eq', '16', '1', 'shelf', 'on']), 0)
            self.assertEqual(main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'eq', '16', '4', 'shelf', 'off']), 0)
            self.assertEqual(main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'eq', '4', '2', 'gain', '-3']), 0)
        writes = [(pid, v) for t, p in self.mixer.received if t == P.PARAM_SET
                  for pid, _, _, v in P.decode_param_set(p)]
        # what the mixer reported for the Low/High Shelf buttons on channel 16
        self.assertEqual(writes[:2], [(0x05E3, 0.0), (0x05F2, 1.0)])
        self.assertAlmostEqual(writes[2][1], S.eq_gain_to_norm(-3))
        with TouchMix('127.0.0.1', port=self.mixer.port) as tm:
            tm.wait_ready(5)
            self.assertRaises(ValueError, tm.eq_shelf, 16, 2)

    def test_cli_rename(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'name', '4', 'Kick', '--verify'])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(self.mixer.texts[554], 'Kick')
        self.assertIn("'Vox' -> 'Kick'", out.getvalue())
        # the bytes sent to the real mixer on 2026-10-22, which renamed In 16
        self.assertEqual(P.encode_text(0x05BA, 'In 16').hex(), '090001001100' + 'ba0500000500496e203136')

    def test_cli_fx_type(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'fx', '1', 'type', 'lush']), 0)
            self.assertEqual(main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'fx', '1', 'type', 'Mono Delay']), 0)
        writes = [(pid, v) for t, p in self.mixer.received if t == P.PARAM_SET
                  for pid, _, _, v in P.decode_param_set(p)]
        # the mixer reported Lush Reverb as 5/1024 and Mono Delay as 6/1024 on FX 1 (0x0B96)
        self.assertEqual(writes, [(0x0B96, 5 / 1024.0), (0x0B96, 6 / 1024.0)])
        self.assertEqual(S.fx_type_number('Stereo Delay'), 2)
        self.assertRaises(ValueError, S.fx_type_number, 'flanger')

    def test_risky_writes_refused(self):
        from touchmix import TouchMixError
        with TouchMix('127.0.0.1', port=self.mixer.port) as tm:
            tm.wait_ready(5)
            for param in ('Global > Control > Create Network', 'Global > Misc Input > Powerdown',
                          'Global > FileMan > Delete', 'Security > Control > Auto Lock', 'Auth > Access > All'):
                self.assertRaises(TouchMixError, tm.set, param, 1.0)
            self.assertRaises(TouchMixError, tm.set_text, 'Global > Control > Infra SSID', 'x')
            tm.set('Global > Misc Input > Test screen', 0.0)     # not risky: allowed
            tm.allow_risky = True
            tm.set('Global > FileMan > Delete', 0.0)            # allowed when asked for
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'set', 'Global > Misc Input > Powerdown', '1'])
        sent = [pid for t, p in self.mixer.received if t == P.PARAM_SET for pid, _, _, _ in P.decode_param_set(p)]
        self.assertNotIn(0x00C5, sent)                          # Powerdown never left the PC
        self.assertIn(0x00FA, sent)                             # Delete did, with allow_risky
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'set',
                                   'Global > Control > Demo Enabled', '0', '--force']), 0)

    def test_scene_list(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'scene', 'list']), 0)
        lines = out.getvalue().splitlines()
        self.assertTrue(any('factory' in l and '(Default)' in l for l in lines))
        self.assertTrue(any('user' in l and 'usr' in l and 'af18348e0817414895e398710bf6f0bb' in l for l in lines))
        with TouchMix('127.0.0.1', port=self.mixer.port) as tm:
            tm.wait_ready(5)
            self.assertEqual(tm.find_scene('TEST')['number'], 0)
            self.assertEqual(tm.find_scene('usr')['location'], 'user')

    def test_commands_keep_the_client_name(self):
        """The mixer keys access on the client name, so no command's own
        arguments may change it (a 'scene recall usr' once connected as 'usr')."""
        from touchmix.client import default_client_name
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'scene', 'list'])
            main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'name', '4', 'Kick'])
            main(['--host', '127.0.0.1', '--port', str(self.mixer.port), '--name', 'Studio-PC', 'name', '4', 'Vox'])
        hellos = [P.decode_hello(p)[0] for t, p in self.mixer.received if t == P.HELLO]
        self.assertEqual(hellos, [default_client_name(), default_client_name(), 'Studio-PC'])
        self.assertEqual(self.mixer.texts[554], 'Vox')

    def test_meters(self):
        # values seen with a held synth note on inputs 7/8 (sent to Aux 7/8 and 9/10)
        self.mixer.meters = [0.676, 0.676, 0, 0, 0, 0, 0, 0, 0.596, 0.631]
        with TouchMix('127.0.0.1', port=self.mixer.port) as tm:
            tm.wait_ready(5)
            time.sleep(0.4)
            m = tm.meters()
        self.assertAlmostEqual(m['main'][0], 0.676, places=3)
        self.assertAlmostEqual(m['aux7/8'], 0.596, places=3)
        self.assertAlmostEqual(m['aux9/10'], 0.631, places=3)
        self.assertEqual(m['aux'], [0.0] * 6)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'meters',
                                   '--seconds', '0.8', '--interval', '0.2']), 0)
        self.assertIn('Main L   -3.4', out.getvalue())
        # the real 42-byte reply body from the log decodes to the same layout
        body = bytes.fromhex('280003807b3f06807b3f' + '00' * 24 + '84706d3f73f87a3f')
        levels = P.decode_meters(b'\0\0\0\0' + body)
        self.assertAlmostEqual(levels[0], 0.9824, places=3)
        self.assertEqual(P.decode_meters(b'\0' * 30), None)

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

    def test_fit_curve(self):
        xs = [i / 20.0 for i in range(21)]
        kind, _, err = M.fit_curve([(x, 60 * x - 60) for x in xs])
        self.assertEqual(kind, 'linear')
        self.assertLess(err, 1e-9)
        kind, formula, err = M.fit_curve([(x, 20.0 * 1000 ** x) for x in xs])
        self.assertEqual(kind, 'exponential')
        self.assertIn('1000', formula)
        kind, _, err = M.fit_curve([(x, S.norm_to_fader_db(x)) for x in xs])
        self.assertEqual(kind, 'fader law')
        kind, _, _ = M.fit_curve([(x, float(round(x * 3))) for x in xs])
        self.assertIn('stepped', kind)
        self.assertEqual(M.fit_curve([(x, 'Peak') for x in xs])[0], 'text')
        self.assertEqual(M.fit_curve([(x, 1.0) for x in xs])[0], 'constant')
        kind, formula, _ = M.fit_curve([(x, 1.0 if x >= 0.5 else 0.0) for x in xs])
        self.assertEqual((kind, formula), ('switch', 'off up to 0.45, on from 0.5'))

    def test_probe_plans_resolve(self):
        t = S.ParamTable()
        for name, plan in M.PLANS.items():
            for item in M.plan_items(plan):
                self.assertTrue(M.plan_pids(t, *item[:4]), (name, item))

    def test_plan_pids_bands(self):
        t = S.ParamTable()
        pids = M.plan_pids(t, 'GEQ', '*', 1, 'main')
        names = [t[p].name.strip() for p in pids]
        self.assertIn('1000', names)
        self.assertNotIn('FxEnbl', names)
        self.assertEqual(len(M.plan_pids(t, 'GEQ', '*', 1, 'main,aux1')), 2 * len(pids))
        self.assertEqual(t.find('Main > GEQ', '1000').name, '1000 ')     # trailing space in QSC's XML
        self.assertEqual(S.strip_path('pb'), '2-Trk PB')

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
        self.assertAlmostEqual(c['config'], -0.0627, places=3)
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
        self.assertAlmostEqual(dict(table)[0.75], -0.0627, places=3)
        self.assertEqual(dict(table)[1.0], 12.0)
        self.assertTrue(res['restored'], self.out)
        self.assertEqual(self.mixer.values[542], -55.0)
        self.assertAlmostEqual(self.mixer.values[618], -6.0, delta=0.05)
        self.assertNotIn('warning', res)

    def test_probe_restores_text_parameter(self):
        s = self.session()
        try:
            res = M.probe(s, [0x221], M.sweep_values(5), say=self.out.append)
        finally:
            s.close()
        self.assertEqual([e for _, e in M.curve_table(res['points'])], ['L', 'L', 'C', 'R', 'R'])
        self.assertTrue(res['restored'], self.out)
        self.assertEqual(self.mixer.values[545], 'C')

    def test_probe_all_keeps_going(self):
        import tempfile
        outdir = tempfile.mkdtemp()
        saved = []

        def save_fn(path, body):
            saved.append(path)
            M.save(path, 'probe', '127.0.0.1', 'Test Mixer', body)
        plan = [('Control', 'Fader', 1, '4-5'), ('Nope', 'Missing', 1, '4'), ('Control', 'Pan', 1, '4')]
        s = self.session()
        try:
            summary = M.probe_all(s, plan, 5, outdir, save_fn, say=self.out.append)
        finally:
            s.close()
        kinds = [fit[0] for _, _, fit in summary]
        self.assertNotIn('error', kinds[0])    # the fake fader curve is made up, so any numeric fit
        self.assertEqual(kinds[1], 'error')
        self.assertEqual(kinds[2], 'text')
        self.assertEqual(len(saved), 2)
        self.assertTrue(all(os.path.dirname(p) == outdir for p in saved))
        self.assertIn('Control Fader', M.format_summary(summary))
        self.assertEqual(self.mixer.values[545], 'C')

    def test_probe_all_setup_and_put_back(self):
        """A plan item with a setup write gets it first, and the original is put back at the end."""
        import tempfile
        self.mixer.values[0x0B96] = 6.0           # FX 1 > Editor > FX type: Mono Delay
        outdir = tempfile.mkdtemp()
        plan = [('Control', 'Fader', 1, '4', 3, (('FX 1 > Editor > FX type', 5 / 1024.0),))]
        s = self.session()
        try:
            M.probe_all(s, plan, 3, outdir, lambda path, body: None, say=self.out.append)
        finally:
            s.close()
        fx_writes = [v for t, p in self.mixer.received if t == P.PARAM_SET
                     for pid, _, _, v in P.decode_param_set(p) if pid == 0x0B96]
        self.assertEqual(fx_writes, [5 / 1024.0, 6 / 1024.0])

    def test_probe_more_pids_than_values(self):
        s = self.session()
        try:
            res = M.probe(s, [0x21E, 0x26A, 0x21F], M.sweep_values(2), say=self.out.append, restore=False)
        finally:
            s.close()
        self.assertEqual(sorted(p['id'] for p in res['points']), ['0x021e', '0x021f', '0x026a'])
        self.assertEqual({p['id']: p['wire'] for p in res['points']},
                         {'0x021e': 0.0, '0x026a': 1.0, '0x021f': 0.0})

    def test_send_chunked_caps_message_size(self):
        class Rec:
            calls = []

            def set_many(self, changes):
                self.calls.append(list(changes))
        rec = Rec()
        M.send_chunked(rec, [(i, 0.0) for i in range(40)], size=16)
        self.assertEqual([len(c) for c in rec.calls], [16, 16, 8])

    def test_probe_batches_and_checkpoints(self):
        saved = []
        s = self.session()
        try:
            res = M.probe(s, [0x21E, 0x26A], M.sweep_values(4), say=self.out.append,
                          checkpoint=saved.append, max_batch=1)
        finally:
            s.close()
        self.assertEqual(saved, [{0x21E: -55.0, 0x26A: -6.0}])
        sizes = [P.decode_param_set(p) for t, p in self.mixer.received if t == P.PARAM_SET]
        self.assertTrue(all(len(list(d)) == 1 for d in sizes))
        self.assertEqual(len(res['points']), 4)
        self.assertTrue(res['restored'], self.out)

    def test_cli_map_restore(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), 'restore-point.json')
        M.save_restore_point(path, '127.0.0.1', 'Test Mixer', 'Fader', {0x21E: -20.0, 0x26A: -55.0})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'map', 'restore', path, '--yes'])
        self.assertEqual(rc, 0)
        self.assertEqual(self.mixer.values[618], -55.0)
        self.assertAlmostEqual(self.mixer.values[542], -20.0, places=3)     # straight from the known fader law

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
        self.assertIn('0.7500    -0.0627', out.getvalue())


if __name__ == '__main__':
    unittest.main()
