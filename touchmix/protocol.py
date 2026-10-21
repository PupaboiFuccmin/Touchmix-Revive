"""Wire format for the TouchMix-16 control protocol. See docs/PROTOCOL.md."""
import struct

PORT = 42217

# message types (u16 type, u16 subtype) as seen on the wire
HELLO = (1, 3)
SESSION = (2, 2)
FILE = (3, 4)
STATUS = (4, 2)
PARAM_SET = (5, 1)
POLL_REQ = (6, 1)
POLL_RESP = (7, 1)

PRODUCT = 'MicroX16'
FILE_START = 0xFFFFFFFF
FILE_END = 0xFFFFFFFE

# The three poll request bodies the Android app cycles through. The meaning of
# the entries is not decoded yet; the 52-byte one is not tied to the selected
# channel, so it is the one we send to keep the session alive.
POLL_ALL = bytes.fromhex('03000a000300'
                         '00000100' '00010100' '00020100' '00030100'
                         '00040100' '00050100' '00060100' '00070100')


def frame(msg_type, payload):
    ty, sub = msg_type
    return struct.pack('<HHH', ty, sub, len(payload) + 6) + payload


def encode_hello(name, device_type='AndroidTablet'):
    """Client hello. The app sends name='Lenovo-Tab-P11-Plus', which the
    mixer then uses to name its per-client settings file."""
    name_b = name.encode('ascii')
    suffix = ('%s-%s' % (PRODUCT, device_type)).encode('ascii')
    payload = struct.pack('<IHHHH', 1, 3, len(name_b), 0, len(name_b) + len(suffix)) + name_b + suffix
    return frame(HELLO, payload)


def decode_hello(payload):
    """Return (name, suffix) from a hello payload."""
    _, _, name_len, _, total = struct.unpack('<IHHHH', payload[:12])
    text = payload[12:12 + total].decode('ascii', errors='replace')
    return text[:name_len], text[name_len:]


def encode_param_set(changes):
    """changes: iterable of (param_id, value). Floats are sent as f32;
    pass ('int', n) to send a raw u32 instead."""
    entries = b''
    for pid, value in changes:
        if isinstance(value, tuple) and value[0] == 'int':
            entries += struct.pack('<HHI', pid, 0, value[1])
        else:
            entries += struct.pack('<HHf', pid, 0, float(value))
    count = len(entries) // 8
    return frame(PARAM_SET, struct.pack('<IH', count, len(entries)) + entries)


def decode_param_set(payload):
    """Yield (param_id, tag, raw_u32, float_value)."""
    count, _ = struct.unpack('<IH', payload[:6])
    for i in range(count):
        pid, tag, raw = struct.unpack('<HHI', payload[6 + 8 * i:14 + 8 * i])
        yield pid, tag, raw, struct.unpack('<f', struct.pack('<I', raw))[0]


def decode_status(payload):
    """Type 4: u16 id + f32, no count and no tag."""
    pid, raw = struct.unpack('<HI', payload[:6])
    return pid, raw, struct.unpack('<f', struct.pack('<I', raw))[0]


def encode_poll(seq, body=POLL_ALL):
    return frame(POLL_REQ, struct.pack('<II', seq, len(body) + 2) + body)


def decode_file_chunk(payload):
    """Return (offset, data, rest) of a type 3 message."""
    off, ln = struct.unpack('<IH', payload[:6])
    return off, payload[6:6 + ln], payload[6 + ln:]


class Reader:
    """Incremental splitter: feed() raw TCP bytes, get complete messages."""

    def __init__(self):
        self.buf = b''

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= 6:
            ty, sub, ln = struct.unpack('<HHH', self.buf[:6])
            if ln < 6:
                raise ValueError('bad message length %d (stream out of sync)' % ln)
            if len(self.buf) < ln:
                break
            out.append(((ty, sub), self.buf[6:ln]))
            self.buf = self.buf[ln:]
        return out
