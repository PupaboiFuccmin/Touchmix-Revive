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
ACTION = (8, 3)      # file actions (scene/preset recall, save, delete, copy); see encode_action
TEXT = (9, 1)        # a text parameter's value, e.g. the current MP3 title
TEXT_ALT = (9, 9)    # same layout; seen for Rta Local > Used by device (the mixer's name)

# Action codes, from the app's FileActionClient::doAction (docs/PROTOCOL.md,
# "8.3 action"). Recall is confirmed on the mixer; the others are read from the
# app's code only.
ACTION_DELETE = 1
ACTION_SAVE_AS = 2
ACTION_RECALL = 3
ACTION_SAVE = 5
ACTION_COPY = 6

PRODUCT = 'MicroX16'
FILE_START = 0xFFFFFFFF
FILE_END = 0xFFFFFFFE

# Poll requests are meter requests: a 6-byte header, then 4-byte entries.
# Entry (0, k, 1, 0) asks for input k+1's meter (before fader and mute). The
# reply is Main L/R plus one value per entry, in request order (2026-10-22,
# synth on inputs 7/8). POLL_ALL is the app's 52-byte request, inputs 1-8;
# the app also sends (0, 0, 15, 0) + (10, 0, 15, 0) and an empty request.
METER_HEADER = bytes.fromhex('03000a000300')


def meter_request(inputs):
    """Poll body asking for the meters of these inputs (1-based)."""
    return METER_HEADER + b''.join(bytes((0, n - 1, 1, 0)) for n in inputs)


POLL_ALL = meter_request(range(1, 9))
POLL_INPUTS_9_16 = meter_request(range(9, 17))


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


def encode_text(pid, text):
    """Type 9.1 to the mixer: set a text parameter (a channel name, say).
    Accepted by the mixer (2026-10-22: renamed In 16), which reports it to the
    other clients but sends nothing back to the sender."""
    data = text.encode('ascii')
    return frame(TEXT, struct.pack('<HHH', pid, 0, len(data)) + data)


def decode_text(payload):
    """Type 9.1: u16 id, u16 (0 so far), u16 length, then the text."""
    pid, _, ln = struct.unpack('<HHH', payload[:6])
    return pid, payload[6:6 + ln].decode('utf-8', errors='replace')


def file_specifier(location, ftype, number):
    """Pack a file reference the way the app's FileActionParameter::fileSpecifierToInt
    does: type in the top byte, location in the next, number in the low 16 bits.
    Confirmed on the mixer: a user scene (type 0, location 1) is 0x0001nnnn."""
    return ((ftype & 0xff) << 24) | ((location & 0xff) << 16) | (number & 0xffff)


def encode_action(code, number, text='', value=0.0):
    """Type 8.3 to the mixer: a file action (e.g. recall a scene).

    u16 code, f32 value, then u32 number, u16 length and the text (like the
    hello message). Confirmed for recall on 2026-10-27; see docs/PROTOCOL.md,
    "8.3 action"."""
    data = text.encode('ascii')
    payload = struct.pack('<Hf', code, float(value)) + struct.pack('<IH', number, len(data)) + data
    return frame(ACTION, payload)


def decode_meters(payload):
    """(seq, [levels]) from a 7.1 poll reply: Main L, Main R, then one value per
    requested entry, linear amplitude. None if the reply doesn't parse."""
    if len(payload) < 6:
        return None
    seq, n_bytes = struct.unpack('<IH', payload[:6])
    body = payload[6:6 + n_bytes]
    if n_bytes % 4 or len(body) != n_bytes:
        return None
    return seq, list(struct.unpack('<%df' % (n_bytes // 4), body))


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
