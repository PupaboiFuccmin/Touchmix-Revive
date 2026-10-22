"""TouchMix-16 network client."""
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
import zlib

from . import protocol as P
from .params import (ParamTable, strip_path, fader_to_norm, pan_to_norm, eq_gain_to_norm, eq_freq_to_norm,
                     send_name, curve_for, transport_to_norm, int_to_norm)

# The full mixer state. Sent on connect as NH<n>-config (NH4 on some
# connections, NH8 on others), and again as all-config after every scene or
# preset recall (seen 2026-10-22), so tm.config stays current.
CONFIG_FILE = re.compile(r'(NH\d+|all)-config\.xml\.dscomp$')
SETTINGS_SUFFIX = '.settings.dqx.dscomp'


def default_client_name():
    host = re.sub(r'[^A-Za-z0-9-]', '-', socket.gethostname())[:24] or 'PC'
    return 'Revive-' + host


class TouchMixError(Exception):
    pass


class TouchMix:
    """Connect to a mixer, track its state, and send parameter changes.

        with TouchMix('192.168.1.1') as tm:
            tm.wait_ready()
            tm.mute(4, True)

    Without a port, connect() asks the network (mDNS) which port the mixer
    is listening on: it changes when the mixer reboots.
    """

    def __init__(self, host, port=None, name=None, device_type='AndroidTablet',
                 poll_interval=0.1, timeout=5.0, params=None):
        self.host, self.port = host, port   # None: looked up over mDNS on connect
        self.name = name or default_client_name()
        self.device_type = device_type
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.params = params or ParamTable()

        self.mixer_name = None
        self.mixer_suffix = None
        self.config = {}        # index -> value string from NH<n>-config.xml (engineering units)
        self.config_text = {}   # index -> text attribute (names etc.)
        self.live = {}          # index -> normalized float last seen on the wire
        self.files = {}         # file name -> inflated bytes
        self.access = None      # 'All', 'Main Mix', 'Aux 1', ... -> bool, from this client's settings file
        self.listeners = []     # fn(pid, value, raw, tag, source)
        self.message_listeners = []   # fn(msg_type, payload), every message as received

        self._sock = None
        self._reader = P.Reader()
        self._pending = []
        self._send_lock = threading.Lock()
        self._ready = threading.Event()
        self._access_known = threading.Event()
        self._closed = threading.Event()
        self._file_buf = {}
        self._file_name = None
        self._seq = 1
        self._threads = []
        self.error = None

    # ------------------------------------------------------------ lifecycle

    def connect(self):
        if not self.port:
            # the control port changes when the mixer reboots; it advertises it over mDNS
            from .discover import lookup_port
            self.port = lookup_port(self.host) or P.PORT
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # the mixer greets first, though while it is busy (MP3 playing) a few
        # status pushes can come ahead of the hello; keep those for the reader
        early = []
        msg = self._read_one()
        while msg[0] != P.HELLO:
            if msg[0] not in (P.STATUS, P.PARAM_SET, P.TEXT, P.TEXT_ALT) or len(early) > 50:
                raise TouchMixError('expected hello from mixer, got type %d.%d' % msg[0])
            early.append(msg)
            msg = self._read_one()
        self._pending[:0] = early
        self.mixer_name, self.mixer_suffix = P.decode_hello(msg[1])
        if not self.mixer_suffix.startswith('MicroX'):
            raise TouchMixError('unexpected device %r%r' % (self.mixer_name, self.mixer_suffix))
        self._send(P.encode_hello(self.name, self.device_type))
        self._sock.settimeout(None)
        for target in (self._read_loop, self._poll_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def close(self):
        self._closed.set()
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    def wait_ready(self, timeout=15.0):
        """Block until the mixer has sent its config (the full current state)."""
        if not self._ready.wait(timeout):
            raise TouchMixError(self.error or 'mixer did not send its config within %.0fs' % timeout)
        return self

    def wait_access(self, timeout=5.0):
        """This client's access rights, from the per-client settings file the
        mixer sends after the config. None if it did not arrive in time.

        The mixer keeps a profile per client name and ignores writes outside
        it. A new client starts with nothing allowed until someone grants it
        access on the mixer (Wi-Fi device access)."""
        self._access_known.wait(timeout)
        return self.access

    def can_write(self, timeout=5.0):
        """True / False from the access rights, None if unknown."""
        access = self.wait_access(timeout)
        if access is None:
            return None
        return any(v for k, v in access.items() if k not in ('Locked', 'Security Settings'))

    @property
    def connected(self):
        return self._sock is not None and not self._closed.is_set()

    # ------------------------------------------------------------ sending

    def set(self, param, value):
        """Set one parameter. `param` is an index, or 'Path > Name'."""
        self.set_many([(param, value)])

    def set_many(self, changes):
        resolved = [(self.resolve(p), v) for p, v in changes]
        self._send(P.encode_param_set(resolved))
        for pid, v in resolved:
            if not isinstance(v, tuple):
                self.live[pid] = float(v)

    def resolve(self, param):
        if isinstance(param, int):
            return param
        s = str(param).strip()
        if re.fullmatch(r'0x[0-9a-fA-F]+', s):
            return int(s, 16)
        if s.isdigit():
            return int(s)
        if '>' in s:
            path, name = s.rsplit('>', 1)
            return self.params.find(path.strip(), name.strip()).index
        raise KeyError('cannot resolve parameter %r' % param)

    def channel_param(self, channel, name, section='Control', occurrence=0):
        return self.params.find('%s > %s' % (strip_path(channel), section), name, occurrence).index

    def mute(self, channel, on=True):
        self.set(self.channel_param(channel, 'Mute'), 1.0 if on else 0.0)

    def solo(self, channel, on=True):
        self.set(self.channel_param(channel, 'Solo'), 1.0 if on else 0.0)

    def fader(self, channel, value):
        self.set(self.channel_param(channel, 'Fader'), fader_to_norm(value))

    def pan(self, channel, value):
        self.set(self.channel_param(channel, 'Pan'), pan_to_norm(value))

    def set_value(self, channel, section, name, value, band=1):
        """Set a control in real units, via the curve tables in params.py:
        set_value(4, 'Comp', 'Thresh', -20), set_value(4, 'EQ', 'Q', 2, band=3)."""
        par = self.params.find('%s > %s' % (strip_path(channel), section), name, band - 1)
        curve = curve_for(par.path, par.name)
        if curve is None:
            raise KeyError('no curve mapped for %s yet; use set() with a 0..1 value' % par.full_name)
        self.set(par.index, curve.to_norm(value))

    def trim(self, channel, db):
        """dB: -15..+15 on mono inputs, 0..+12 on the stereo inputs."""
        self.set_value(channel, 'Control', 'Trim', db)

    def send(self, channel, dest, value):
        """Send level from an input to an aux or FX ('aux1', 'fx2').
        Same values as fader(): '-10db', 'off', or 0..1."""
        self.set(self.channel_param(channel, send_name(dest)), fader_to_norm(value))

    def gate(self, channel, on=True):
        self.set(self.channel_param(channel, 'Enable', 'Gate'), 1.0 if on else 0.0)

    def gate_threshold(self, channel, db):
        self.set_value(channel, 'Gate', 'Thresh', db)

    # USB recorder (needs a drive in the mixer)

    def arm(self, channel, on=True):
        """Arm an input (1-16, st17, st19) for multitrack recording, or '2trk'
        for the 2-track recorder."""
        if str(channel).lower() in ('2trk', '2-trk', '2track'):
            pid = self.params.find('2-Trk Rec > Control', '2-Track Arm').index
        else:
            pid = self.channel_param(channel, 'Arm')
        self.set(pid, 1.0 if on else 0.0)

    def transport(self, state):
        """'record', 'play', 'stop', 'next', 'previous' or 'rtz' (return to
        zero; top of list in MP3 mode)."""
        self.set('Global > Transport > Transport', transport_to_norm(state))

    def mp3_mode(self, on=True):
        """Switch the USB player between MP3 playback and multitrack recording.
        These are the two values the mixer reported when the mode was switched."""
        self.set_many([('Global > Transport > Multi-track Enable', 0.0 if on else 1.0),
                       ('Global > Misc Input > Playback', int_to_norm(0 if on else 1))])

    def mp3_select(self, index):
        """Select song `index` (0 = first) in the MP3 list."""
        self.set('Global > Control > Mp3 playlist', int_to_norm(index))

    def eq_gain(self, channel, band, db):
        self.set(self.channel_param(channel, 'Gain', 'EQ', band - 1), eq_gain_to_norm(db))

    def eq_freq(self, channel, band, hz):
        self.set(self.channel_param(channel, 'Freq', 'EQ', band - 1), eq_freq_to_norm(hz))

    # ------------------------------------------------------------ reading

    def config_value(self, param):
        """Value from the config the mixer sent at connect (engineering units)."""
        pid = self.resolve(param)
        return self.config.get(pid), self.config_text.get(pid)

    def on_change(self, fn):
        self.listeners.append(fn)
        return fn

    def on_message(self, fn):
        """fn(msg_type, payload) for every message the mixer sends, including
        polls, files and types the client does not know. For research."""
        self.message_listeners.append(fn)
        return fn

    # ------------------------------------------------------------ internals

    def _send(self, data):
        with self._send_lock:
            self._sock.sendall(data)

    def _read_one(self):
        while not self._pending:
            data = self._sock.recv(65536)
            if not data:
                raise TouchMixError('mixer closed the connection')
            self._pending.extend(self._reader.feed(data))
        return self._pending.pop(0)

    def _poll_loop(self):
        while not self._closed.wait(self.poll_interval):
            try:
                self._send(P.encode_poll(self._seq))
                self._seq += 1
            except OSError:
                break

    def _read_loop(self):
        try:
            while self._pending:
                self._handle(*self._pending.pop(0))
            while not self._closed.is_set():
                data = self._sock.recv(65536)
                if not data:
                    self.error = 'mixer closed the connection'
                    break
                for m in self._reader.feed(data):
                    self._handle(*m)
        except (OSError, ValueError) as e:
            if not self._closed.is_set():
                self.error = str(e)
        finally:
            self._closed.set()

    def _notify(self, pid, value, raw, tag, source):
        for fn in self.listeners:
            try:
                fn(pid, value, raw, tag, source)
            except Exception as e:  # a bad listener must not kill the reader
                print('listener error:', e)

    def _handle(self, msg_type, payload):
        for fn in self.message_listeners:
            try:
                fn(msg_type, payload)
            except Exception as e:
                print('message listener error:', e)
        if msg_type == P.PARAM_SET:
            for pid, tag, raw, value in P.decode_param_set(payload):
                self.live[pid] = value
                self._notify(pid, value, raw, tag, 'set')
        elif msg_type == P.STATUS:
            pid, raw, value = P.decode_status(payload)
            self.live[pid] = value
            self._notify(pid, value, raw, 0, 'status')
        elif msg_type in (P.TEXT, P.TEXT_ALT):
            pid, text = P.decode_text(payload)
            self.config_text[pid] = text
            self._notify(pid, 0.0, 0, 0, 'text')
        elif msg_type == P.FILE:
            self._handle_file(payload)

    def _handle_file(self, payload):
        off, data, _ = P.decode_file_chunk(payload)
        if off == P.FILE_START:
            self._file_name = data.decode('ascii', errors='replace')
            self._file_buf[self._file_name] = bytearray()
        elif off == P.FILE_END:
            name = self._file_name
            raw = bytes(self._file_buf.pop(name, b''))
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                pass
            self.files[name] = raw
            if CONFIG_FILE.match(name):
                self._load_config(raw)
                self._ready.set()
            elif name.endswith(SETTINGS_SUFFIX):
                self._load_access(raw)
        elif self._file_name is not None:
            buf = self._file_buf[self._file_name]
            if off + len(data) > len(buf):
                buf.extend(b'\0' * (off + len(data) - len(buf)))
            buf[off:off + len(data)] = data

    def _load_access(self, xml_bytes):
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError:
            return
        access = {}
        for el in root.iter('Effect'):
            if el.get('Name') != 'Access':
                continue
            for p in el.iter('Parameter'):
                name = p.get('name')
                if name and name != 'Device Type':
                    access[name] = access.get(name, False) or p.get('value') not in ('0', None)
        self.access = access
        self._access_known.set()

    def _load_config(self, xml_bytes):
        root = ET.fromstring(xml_bytes)
        for el in root.iter('Parameter'):
            try:
                idx = int(el.get('Index'))
            except (TypeError, ValueError):
                continue
            self.config[idx] = el.get('value')
            if el.get('text'):
                self.config_text[idx] = el.get('text')
