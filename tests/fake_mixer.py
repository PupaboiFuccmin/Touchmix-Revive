"""A tiny stand-in for a TouchMix-16, for tests.

Greets with a hello, sends a session message and a zlib'd NH4-config.xml in
1012-byte chunks like the real mixer, answers polls, and applies param-set
messages to its config so a later connection sees the change.
"""
import socket
import struct
import threading
import zlib

from touchmix import protocol as P

CONFIG = '''<?xml version="1.0" encoding="UTF-8"?>
<Config Version="3.0.9551" Name="UI">
  <Channel Type="0" Instance="3" Version="0" Name="In 4">
    <Effect Type="26" Instance="0" Version="0" Name="Control">
      <Parameter Type="41" Instance="0" name="Fader" value="-55" Index="542"/>
      <Parameter Type="2" Instance="0" name="Mute" value="{mute}" Index="543"/>
      <Parameter Type="4" Instance="0" name="Pan" value="0" Index="545"/>
      <Parameter Type="165" Instance="0" name="Name" value="0" Index="548" text="Vox"/>
    </Effect>
  </Channel>
  {padding}
</Config>
'''


class FakeMixer:
    def __init__(self, name='Test Mixer'):
        self.name = name
        self.mute = 0
        self.received = []
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(('127.0.0.1', 0))
        self.srv.listen()
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.srv.close()

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
        xml = CONFIG.format(mute=self.mute, padding='<!-- %s -->' % ('x' * 3000)).encode()
        data = zlib.compress(xml)
        fname = b'NH4-config.xml.dscomp'
        out = P.frame(P.FILE, struct.pack('<IH', P.FILE_START, len(fname)) + fname)
        for off in range(0, len(data), 1012):
            chunk = data[off:off + 1012]
            out += P.frame(P.FILE, struct.pack('<IH', off, len(chunk)) + chunk)
        path = b'/usr/share/ds2i/tmp/' + fname
        out += P.frame(P.FILE, struct.pack('<IH', P.FILE_END, len(path)) + path + b'\0' * 6)
        return out

    def _serve(self, c):
        try:
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
                        c.sendall(P.frame(P.SESSION, b'\xb4\x9c\x01\x00\x00\x00')
                                  + P.frame(P.STATUS, struct.pack('<Hf', 0xCE, 0.99))
                                  + self._config_file())
                    elif msg_type == P.POLL_REQ:
                        seq = payload[:4]
                        c.sendall(P.frame(P.POLL_RESP, seq + payload[4:8] + b'\0' * (len(payload) - 8)))
                    elif msg_type == P.PARAM_SET:
                        for pid, _, _, value in P.decode_param_set(payload):
                            if pid == 543:
                                self.mute = int(value)
        except OSError:
            pass
        finally:
            c.close()
