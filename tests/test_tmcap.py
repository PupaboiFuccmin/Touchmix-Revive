import contextlib
import io
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(HERE, '..', 'tools'))

import tmcap  # noqa: E402
from touchmix import protocol as P  # noqa: E402

MIXER = ('192.168.10.129', 54596)     # not 42217: the port changes when the mixer reboots
APP = ('192.168.10.50', 40001)


def ip(addr):
    return bytes(int(x) for x in addr.split('.'))


def packet(src, dst, seq, data):
    """A raw-IP (linktype 101, as PCAPdroid writes) IPv4/TCP packet."""
    tcp = struct.pack('>HHIIBBHHH', src[1], dst[1], seq, 0, 5 << 4, 0x18, 65535, 0, 0) + data
    hdr = struct.pack('>BBHHHBBH4s4s', 0x45, 0, 20 + len(tcp), 0, 0, 64, 6, 0, ip(src[0]), ip(dst[0]))
    return hdr + tcp


def write_pcap(path, packets):
    with open(path, 'wb') as f:
        f.write(struct.pack('<IHHiIII', 0xA1B2C3D4, 2, 4, 0, 0, 65535, 101))
        for i, pkt in enumerate(packets):
            f.write(struct.pack('<IIII', 1000 + i, 0, len(pkt), len(pkt)) + pkt)


def hello(name, suffix):
    n, s = name.encode(), suffix.encode()
    return P.frame(P.HELLO, struct.pack('<IHHHH', 1, 3, len(n), 0, len(n) + len(s)) + n + s)


class Capture(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), 'cap.pcap')
        seq = {MIXER: 1000, APP: 5000}

        def send(src, dst, data):
            p = packet(src, dst, seq[src], data)
            seq[src] += len(data)
            return p
        write_pcap(self.path, [
            send(MIXER, APP, hello('QSC JD HOME', 'MicroX16')),
            send(APP, MIXER, hello('Lenovo-Tab-P11-Plus', 'MicroX16-AndroidTablet')),
            send(APP, MIXER, P.encode_param_set([(0x221, 0.0)])),
            send(MIXER, APP, P.frame(P.TEXT, bytes.fromhex('180100001200') + b'ERN FULL SONG (tw)')),
            send(APP, MIXER, P.frame((10, 1), b'\x01\x02\x03')),      # a request type never seen before
        ])

    def test_finds_mixer_on_any_port(self):
        msgs = tmcap.messages(self.path)
        self.assertEqual([(d, ty, sub) for _, d, _, ty, sub, _ in msgs],
                         [('M>A', 1, 3), ('A>M', 1, 3), ('A>M', 5, 1), ('M>A', 9, 1), ('A>M', 10, 1)])

    def test_summary_flags_new_types(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            tmcap.cmd_summary(self.path)
        lines = [l for l in out.getvalue().splitlines() if 'NEW message type' in l]
        self.assertEqual(len(lines), 1)
        self.assertIn('10.1', lines[0])
        self.assertIn('9.1 text', out.getvalue())


if __name__ == '__main__':
    unittest.main()
