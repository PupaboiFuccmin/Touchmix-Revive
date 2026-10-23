"""TouchMix-16 network client."""
import re
import socket
import struct
import threading
import time
import xml.etree.ElementTree as ET
import zlib

from . import protocol as P
from .params import (ParamTable, strip_path, risky, fader_to_norm, pan_to_norm, eq_gain_to_norm, eq_freq_to_norm,
                     send_name, curve_for, transport_to_norm, int_to_norm, fx_type_number, FX_SECTIONS)

# The full mixer state. Sent on connect as NH<n>-config (NH4 on some
# connections, NH8 on others), and again as all-config after every scene or
# preset recall (seen 2026-10-22), so tm.config stays current.
CONFIG_FILE = re.compile(r'(NH\d+|all)-config\.xml\.dscomp$')
SETTINGS_SUFFIX = '.settings.dqx.dscomp'


def default_client_name():
    host = re.sub(r'[^A-Za-z0-9-]', '-', socket.gethostname())[:24] or 'PC'
    return 'Revive-' + host


# Longest name tried so far is 6 characters ('TEST16'); the mixer's own
# keyboard may allow more. Keep names short until the limit is known.
MAX_NAME = 16


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
        self.meter_values = None   # latest 10 meter levels (linear), see meters()
        self.meter_time = 0.0
        self.allow_risky = False   # writes to network/security/Powerdown/Delete are refused unless True
        self.listeners = []     # fn(pid, value, raw, tag, source)
        self.message_listeners = []   # fn(msg_type, payload), every message as received

        self._sock = None
        self._reader = P.Reader()
        self._pending = []
        self._send_lock = threading.Lock()
        self._ready = threading.Event()
        self._access_known = threading.Event()
        self._index_known = threading.Event()
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
        for pid, _ in resolved:
            self._check_safe(pid)
        self._send(P.encode_param_set(resolved))
        for pid, v in resolved:
            if not isinstance(v, tuple):
                self.live[pid] = float(v)

    def _check_safe(self, pid):
        par = self.params.get(pid)
        why = risky(par) if par else None
        if why and not self.allow_risky:
            raise TouchMixError('refusing to write %s: %s. Set allow_risky (or use set --force) '
                                'if you really mean it.' % (par.full_name, why))

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
        """'record', 'play', 'stop', 'ff', 'rew' or 'rtz' (return to zero).
        In MP3 mode 'next' / 'previous' (the same values as ff / rew) and
        'rtz' (top of list)."""
        self.set('Global > Transport > Transport', transport_to_norm(state))

    def _pulse(self, param):
        """Write 1 then 0, as the mixer reports its momentary buttons."""
        self.set(param, 1.0)
        time.sleep(0.1)
        self.set(param, 0.0)

    def locate_set(self):
        """The recorder's Set button: the locate point moves to the play pointer."""
        self._pulse('Global > Transport > Set Locate')

    def locate_goto(self):
        """The recorder's Go To button: the play pointer jumps to the locate point."""
        self._pulse('Global > Transport > Goto Locate')

    def locate(self, fraction):
        """Put the locate point at a fraction (0..1) of the session's length."""
        self.set('Global > Transport > Locate', min(1.0, max(0.0, float(fraction))))

    def mp3_mode(self, on=True):
        """Switch the USB player between MP3 playback and multitrack recording.
        These are the two values the mixer reported when the mode was switched."""
        self.set_many([('Global > Transport > Multi-track Enable', 0.0 if on else 1.0),
                       ('Global > Misc Input > Playback', int_to_norm(0 if on else 1))])

    # Scenes. Listing works; recalling from the PC does not yet (UUID + Recall,
    # with or without Type/Location/Number, did nothing on 2026-10-22). See
    # docs/RESEARCH.md, "Scene recall from the PC".

    SCENE_LOCATIONS = {'0': 'factory', '1': 'user', '3': 'usb'}

    def scenes(self, timeout=5.0):
        """Scenes in the file index: [{'name', 'file', 'uuid', 'location', 'current'}]."""
        self._index_known.wait(timeout)
        raw = next((v for k, v in self.files.items() if k.startswith('fileIndex')), None)
        if raw is None:
            return []
        current = self.current_scene_uuid()
        out = []
        for c in ET.fromstring(raw).iter('Collection'):
            if c.get('Type') != '0':
                continue
            for number, f in enumerate(c.iter('File')):
                out.append(dict(name=f.get('Description', ''), file=f.get('Path', '').rsplit('/', 1)[-1],
                                uuid=f.get('Uuid', ''), location=self.SCENE_LOCATIONS.get(c.get('Location'), c.get('Location')),
                                location_code=int(c.get('Location')), number=number,
                                current=f.get('Uuid') == current))
        return out

    def current_scene_uuid(self):
        """The scene last saved or recalled, from FileMan > Scene Uuid0-7."""
        try:
            words = [int(float(self.config.get(self.resolve('Global > FileMan > Scene Uuid%d' % i)))) for i in range(8)]
        except (TypeError, ValueError):
            return None
        return b''.join(struct.pack('<H', w) for w in words).hex()

    def find_scene(self, name):
        """A scene by name, file name or UUID (case-insensitive)."""
        key = name.strip().lower()
        for s in self.scenes():
            if key in (s['name'].lower(), s['file'].lower(), s['file'].lower().split('.')[0], s['uuid']):
                return s
        raise KeyError('no scene %r (see scene list)' % name)

    def fx_type(self, fx, kind):
        """Pick FX 1-4's processor: 'none', 'chorus', 'delay', 'pitch', 'dense',
        'lush' or 'mono-delay'. The mixer loads that processor's default preset."""
        self.set('FX %d > Editor > FX type' % int(fx), int_to_norm(fx_type_number(kind)))

    def fx_section(self, fx):
        """The section holding FX N's current processor settings ('L-Reverb', ...), or None."""
        n = int(float(self.config.get(self.resolve('FX %d > Editor > FX type' % int(fx))) or 0))
        return FX_SECTIONS.get(n)

    def fx_set(self, fx, name, value):
        """Set a setting of FX N's current processor in real units:
        fx_set(1, 'Rev Time', 2.5) with Dense Reverb loaded."""
        section = self.fx_section(fx)
        if section is None:
            raise KeyError('FX %d has no processor loaded' % int(fx))
        self.set_value('fx%d' % int(fx), section, name, value)

    def set_text(self, param, text):
        """Set a text parameter, e.g. 'In 4 > Control > Name'. ASCII only."""
        pid = self.resolve(param)
        self._check_safe(pid)
        self._send(P.encode_text(pid, text))
        self.config_text[pid] = text

    def rename(self, channel, name):
        """Rename a channel (1-16, st17, aux1, fx2, dca3, ...), as on its Setup tab."""
        if not name or len(name) > MAX_NAME:
            raise ValueError('channel names are 1-%d characters' % MAX_NAME)
        self.set_text(self.channel_param(channel, 'Name'), name)

    def mp3_select(self, index):
        """Select song `index` (0 = first) in the MP3 list."""
        self.set('Global > Control > Mp3 playlist', int_to_norm(index))

    def eq_gain(self, channel, band, db):
        self.set(self.channel_param(channel, 'Gain', 'EQ', band - 1), eq_gain_to_norm(db))

    def eq_freq(self, channel, band, hz):
        self.set(self.channel_param(channel, 'Freq', 'EQ', band - 1), eq_freq_to_norm(hz))

    def eq_shelf(self, channel, band, on=True):
        """Bands 1 and 4 on an input: shelf (on) or bell (off). The Low/High
        Shelf buttons write the band's BndEnbl: 0 = shelf, 1 = bell."""
        if band not in (1, 4):
            raise ValueError('only bands 1 and 4 can be shelves')
        self.set(self.channel_param(channel, 'BndEnbl', 'EQ', band - 1), 0.0 if on else 1.0)

    # ------------------------------------------------------------ reading

    def config_value(self, param):
        """Value from the config the mixer sent at connect (engineering units)."""
        pid = self.resolve(param)
        return self.config.get(pid), self.config_text.get(pid)

    def meters(self):
        """The output meters as linear amplitude (1.0 = full scale), updated
        about 10 times a second from the poll replies, or None before the first:
        {'main': (L, R), 'aux': [Aux 1..6], 'aux7/8': x, 'aux9/10': x, 'raw': [10 values]}."""
        v = self.meter_values
        if v is None:
            return None
        return {'main': (v[0], v[1]), 'aux': list(v[2:8]), 'aux7/8': v[8], 'aux9/10': v[9], 'raw': list(v)}

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
        elif msg_type == P.POLL_RESP:
            levels = P.decode_meters(payload)
            if levels is not None:
                self.meter_values, self.meter_time = levels, time.time()
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
            elif name.startswith('fileIndex'):
                self._index_known.set()
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
