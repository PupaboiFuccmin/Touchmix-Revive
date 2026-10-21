import contextlib
import io
import os
import struct
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from touchmix import TouchMix, discover, protocol as P, params as S  # noqa: E402
from touchmix.cli import main  # noqa: E402
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
        self.assertEqual(S.fader_to_norm('0db'), 0.75)
        self.assertEqual(S.fader_to_norm('off'), 0.0)
        self.assertRaises(ValueError, S.fader_to_norm, '-10db')

    def test_channel_names(self):
        self.assertEqual(S.strip_path(4), 'In 4')
        self.assertEqual(S.strip_path('aux8'), 'Aux 7/8')
        self.assertEqual(S.strip_path('fx2'), 'FX 2')
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

    def test_cli_mute_verify(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = main(['--host', '127.0.0.1', '--port', str(self.mixer.port), 'mute', '4', 'on', '--verify'])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn('CHANGED', out.getvalue())


if __name__ == '__main__':
    unittest.main()
