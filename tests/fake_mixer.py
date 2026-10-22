"""A tiny stand-in for a TouchMix-16, for tests.

Greets with a hello, sends a session message and a zlib'd NH<n>-config.xml in
1012-byte chunks like the real mixer, answers polls, and applies param-set
messages to its config so a later connection sees the change. Faders store a
dB value by the real fader law, like the mixer's config.
"""
import socket
import struct
import threading
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
      <Parameter Type="165" Instance="0" name="Name" value="0" Index="554" text="Vox"/>
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
        self.values = {542: -55.0, 543: 0, 545: 'C', 618: -6.0}
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

    def apply(self, pid, value):
        if pid in FADERS:
            self.values[pid] = fader_db(value)
        elif pid == 543:
            self.values[pid] = int(value)
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
        xml = CONFIG.format(v=self.values, padding='<!-- %s -->' % ('x' * 3000))
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
                                  + self._settings_file(P.decode_hello(payload)[0]))
                    elif msg_type == P.POLL_REQ:
                        seq = payload[:4]
                        c.sendall(P.frame(P.POLL_RESP, seq + payload[4:8] + b'\0' * (len(payload) - 8)))
                    elif msg_type == P.PARAM_SET:
                        for pid, _, _, value in P.decode_param_set(payload):
                            if self.access:
                                self.apply(pid, value)
        except OSError:
            pass
        finally:
            if c in self.clients:
                self.clients.remove(c)
            c.close()
