"""A tiny stand-in for a TouchMix-16, for tests.

Greets with a hello, sends a session message and a zlib'd NH<n>-config.xml in
1012-byte chunks like the real mixer, answers polls, and applies param-set
messages to its config so a later connection sees the change. Faders store a
dB value by the real fader law, like the mixer's config.
"""
import socket
import struct
import threading
import time
import zlib

from touchmix import protocol as P
from touchmix.params import norm_to_fader_db

CONFIG = '''<?xml version="1.0" encoding="UTF-8"?>
<Config Version="3.0.9551" Name="UI">
  <Channel Type="0" Instance="3" Version="0" Name="In 4">
    <Effect Type="26" Instance="0" Version="0" Name="Control">
      <Parameter Type="41" Instance="0" name="Fader" value="{v[542]}" Index="542"/>
      <Parameter Type="2" Instance="0" name="Mute" value="{v[543]}" Index="543"/>
      <Parameter Type="4" Instance="0" name="Pan" value="{v[545]}" Index="545"/>
      <Parameter Type="165" Instance="0" name="Name" value="0" Index="554" text="{t[554]}"/>
      <Parameter Type="140" Instance="0" name="FX type" value="{v[2966]}" Index="2966"/>
    </Effect>
  </Channel>
  <Channel Type="5" Instance="0" Version="0" Name="Global">
    <Effect Type="1" Instance="0" Version="0" Name="Transport">
      <Parameter Type="101" Instance="0" name="Transport" value="{v[184]}" Index="184"/>
      <Parameter Type="105" Instance="0" name="Multi-track Enable" value="{v[186]}" Index="186"/>
      <Parameter Type="139" Instance="0" name="Locate" value="{v[191]}" Index="191"/>
    </Effect>
    <Effect Type="2" Instance="0" Version="0" Name="Misc Input">
      <Parameter Type="175" Instance="0" name="USB present" value="{v[202]}" Index="202"/>
    </Effect>
  </Channel>
  <Channel Type="0" Instance="4" Version="0" Name="In 5">
    <Effect Type="26" Instance="0" Version="0" Name="Control">
      <Parameter Type="41" Instance="0" name="Fader" value="{v[618]}" Index="618"/>
    </Effect>
  </Channel>
  {padding}
</Config>
'''


SETTINGS = '''<?xml version="1.0" encoding="UTF-8"?>
<DQData Version="1">
  <Data>
    <EffectList>
      <Effect Type="49" Instance="0" Version="0" Name="Access">
        <Parameter Type="191" Instance="0" name="All" value="{all}" Index="3552"/>
        <Parameter Type="191" Instance="2" name="Main Mix" value="0" Index="3554"/>
        <Parameter Type="191" Instance="11" name="Locked" value="0" Index="3563"/>
        <Parameter Type="191" Instance="12" name="Locked" value="0" Index="3564"/>
        <Parameter Type="193" Instance="0" name="Device Type" value="0" Index="3572" text="AndroidTablet"/>
      </Effect>
    </EffectList>
  </Data>
</DQData>
'''

FILE_INDEX = '''<?xml version="1.0" encoding="UTF-8"?>
<Index Version="1">
  <Collection Type="0" Location="0">
    <File Path="/usr/share/ds2i/factory/scenes/DEFAULT.scene.dqx" Uuid="5ef99f1a29a3fe853a6d2c4a7c51e85f" Description="(Default)"/>
  </Collection>
  <Collection Type="0" Location="1">
    <File Path="/usr/share/ds2i/user/scenes/TEST.scene.dqx" Uuid="790091c8bc2044e880d34b355d7bbd21" Description="TEST"/>
    <File Path="/usr/share/ds2i/user/scenes/USR.scene.dqx" Uuid="af18348e0817414895e398710bf6f0bb" Description="usr"/>
  </Collection>
  <Collection Type="100" Location="0"/>
</Index>
'''

FADERS = (542, 618)


def fader_db(x):
    """The mixer's fader law (as float32, like the config)."""
    return struct.unpack('<f', struct.pack('<f', norm_to_fader_db(x)))[0]


class FakeMixer:
    def __init__(self, name='Test Mixer', access=True, config_name='NH4-config.xml.dscomp'):
        self.name = name
        self.config_name = config_name
        self.status_before_hello = False   # the real mixer does this while playing MP3s
        self.access = access     # like the real mixer, writes are ignored without it
        self.values = {542: -55.0, 543: 0, 545: 'C', 618: -6.0, 0x0B96: 6,
                       184: 0, 186: 1, 191: 0.0, 202: 1}
        self.position = 0.0           # play pointer, a fraction of the session
        self.speed = 0.0              # fraction per second: play, ff, rew
        self.texts = {554: 'Vox'}
        self.main_meter = (0.0, 0.0)
        self.input_meters = {}           # input number -> linear level
        self.received = []
        self.clients = []
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(('127.0.0.1', 0))
        self.srv.listen()
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def mute(self):
        return self.values[543]

    def close(self):
        self.srv.close()

    def push(self, pid, value, tag=0):
        """Send a 5.1 change to every connected client, as if a control moved on the mixer."""
        self.push_frame(P.frame(P.PARAM_SET, struct.pack('<IHHHf', 1, 8, pid, tag, value)))

    def push_frame(self, msg):
        for c in list(self.clients):
            try:
                c.sendall(msg)
            except OSError:
                pass

    RECORDER_SPEEDS = {0: 0.0, 1: 0.02, 3: 0.12, 4: -0.12}   # stop, play, ff, rew

    def _recorder(self, pid, value):
        """A simulated recorder: transport, locate, Set and Go To, reporting the
        play position (0x00B9) as the real mixer does while it moves."""
        if pid == 184:
            state = round(value * 7)
            self.values[184] = state
            if state == 5:
                self._move_to(0.0)
            else:
                self.speed = self.RECORDER_SPEEDS.get(state, 0.0)
                if self.speed:
                    threading.Thread(target=self._run, daemon=True).start()
        elif pid == 191:
            self.values[191] = round(value, 4)
        elif pid == 189 and value >= 0.5:      # Set Locate
            self.values[191] = round(self.position, 4)
        elif pid == 190 and value >= 0.5:      # Goto Locate
            self._move_to(self.values[191])

    def _move_to(self, p):
        self.speed = 0.0
        self.position = p
        self._report()

    def _report(self):
        msg = P.frame(P.STATUS, struct.pack('<Hf', 0x00B9, self.position))
        self.push_frame(msg)

    def _run(self):
        speed = self.speed
        while self.speed == speed and speed:
            time.sleep(0.2)
            if self.speed != speed:
                return
            self.position = min(1.0, max(0.0, self.position + speed * 0.2))
            self._report()

    def apply(self, pid, value):
        if pid in (184, 189, 190, 191):
            return self._recorder(pid, value)
        if pid in FADERS:
            self.values[pid] = fader_db(value)
        elif pid == 543:
            self.values[pid] = int(value)
        elif pid == 0x0B96:                     # an integer setting, sent as n/1024
            self.values[pid] = round(value * 1024)
        elif pid == 545:    # stored as text, to test probing a stepped/enum parameter
            self.values[pid] = 'L' if value < 0.34 else 'C' if value < 0.67 else 'R'
        elif pid in self.values:
            self.values[pid] = round(value, 4)

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _hello(self):
        name = self.name.encode()
        payload = struct.pack('<IHHHH', 1, 3, len(name), 0, len(name) + 8) + name + b'MicroX16'
        return P.frame(P.HELLO, payload)

    def _config_file(self):
        # padding makes the file span several chunks
        xml = CONFIG.format(v=self.values, t=self.texts, padding='<!-- %s -->' % ('x' * 3000))
        return self._file(self.config_name, xml, 'tmp')

    def _settings_file(self, client):
        xml = SETTINGS.format(all=int(self.access))
        return self._file('%s.settings.dqx.dscomp' % client.upper(), xml, 'user/settings')

    def _file(self, name, xml, folder):
        data = zlib.compress(xml.encode())
        fname = name.encode()
        out = P.frame(P.FILE, struct.pack('<IH', P.FILE_START, len(fname)) + fname)
        for off in range(0, len(data), 1012):
            chunk = data[off:off + 1012]
            out += P.frame(P.FILE, struct.pack('<IH', off, len(chunk)) + chunk)
        path = ('/usr/share/ds2i/%s/' % folder).encode() + fname
        out += P.frame(P.FILE, struct.pack('<IH', P.FILE_END, len(path)) + path + b'\0' * 6)
        return out

    def _serve(self, c):
        try:
            if self.status_before_hello:
                c.sendall(P.frame(P.STATUS, struct.pack('<Hf', 0xBC, 0.0)))
            c.sendall(self._hello())
            reader = P.Reader()
            greeted = False
            while True:
                data = c.recv(65536)
                if not data:
                    return
                for msg_type, payload in reader.feed(data):
                    self.received.append((msg_type, payload))
                    if msg_type == P.HELLO and not greeted:
                        greeted = True
                        self.clients.append(c)
                        c.sendall(P.frame(P.SESSION, b'\xb4\x9c\x01\x00\x00\x00')
                                  + P.frame(P.STATUS, struct.pack('<Hf', 0xCE, 0.99))
                                  + self._config_file()
                                  + self._file('fileIndex.xml.dscomp', FILE_INDEX, 'tmp')
                                  + self._settings_file(P.decode_hello(payload)[0]))
                    elif msg_type == P.POLL_REQ:
                        # like the real mixer: Main L/R, then one level per (0, k, 1, 0) entry
                        seq, entries = payload[:4], payload[8 + 6:]
                        levels = list(self.main_meter) + [self.input_meters.get(entries[i + 1] + 1, 0.0)
                                                          for i in range(0, len(entries), 4)]
                        c.sendall(P.frame(P.POLL_RESP, seq + struct.pack('<H%df' % len(levels), 4 * len(levels),
                                                                            *levels)))
                    elif msg_type == P.PARAM_SET:
                        for pid, _, _, value in P.decode_param_set(payload):
                            if self.access:
                                self.apply(pid, value)
                    elif msg_type == P.TEXT:
                        pid, text = P.decode_text(payload)
                        if self.access and pid in self.texts:
                            self.texts[pid] = text
        except OSError:
            pass
        finally:
            if c in self.clients:
                self.clients.remove(c)
            c.close()
