"""Find TouchMix mixers on the local network.

The control port is not fixed: it was 42217 in every capture and session until
the mixer rebooted (2026-10-21), then 54596. The mixer advertises it over mDNS / DNS-SD, which
is how the official app finds it:

    QSC JD HOME._ds2inet._tcp.local  SRV port=54596  A 192.168.10.129
    TXT ProtocolVersion=1 SWVersion=196608 DeviceType=MicroX16

So discover() asks for _ds2inet._tcp first. If nothing answers (mDNS blocked,
another subnet), it falls back to trying one port on every address of the
local /24 subnet(s). Either way each candidate is confirmed by reading the
hello the mixer sends on connect, then hanging up without sending anything.
The mixer never sees a client log in.
"""
import collections
import ipaddress
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor

from . import protocol as P

SERVICE = '_ds2inet._tcp.local'
MDNS_ADDR = ('224.0.0.251', 5353)

Mixer = collections.namedtuple('Mixer', 'ip name suffix port')


def local_ipv4_addresses():
    addrs = set()
    # the address used for the default route; connect() on UDP sends nothing
    for probe_ip in ('192.0.2.1', '10.255.255.255'):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((probe_ip, 9))
            addrs.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith('127.') and not a.startswith('169.254.'))


def default_subnets():
    nets = {ipaddress.ip_network(a + '/24', strict=False) for a in local_ipv4_addresses()}
    return sorted(nets, key=str)


# ---------------------------------------------------------------- mDNS

def dns_query(name, qtype=12):
    """A one-question DNS query (PTR by default), as sent to the mDNS group."""
    qname = b''.join(bytes([len(p)]) + p.encode() for p in name.split('.')) + b'\0'
    return struct.pack('>HHHHHH', 0, 0, 1, 0, 0, 0) + qname + struct.pack('>HH', qtype, 1)


def _read_name(data, i, depth=0):
    labels = []
    while True:
        n = data[i]
        if n == 0:
            return '.'.join(labels), i + 1
        if n & 0xC0 == 0xC0:
            if depth > 10:
                raise ValueError('DNS name pointer loop')
            target = ((n & 0x3F) << 8) | data[i + 1]
            labels.append(_read_name(data, target, depth + 1)[0])
            return '.'.join(labels), i + 2
        labels.append(data[i + 1:i + 1 + n].decode('utf-8', errors='replace'))
        i += 1 + n


def parse_dns(data):
    """Yield (name, type, value) for every record in a DNS message. value is the
    target name for PTR, (port, target) for SRV, a dict for TXT, the address for A."""
    _, _, qd, an, ns, ar = struct.unpack('>HHHHHH', data[:12])
    i = 12
    for _ in range(qd):
        _, i = _read_name(data, i)
        i += 4
    for _ in range(an + ns + ar):
        name, i = _read_name(data, i)
        rtype, _, _, ln = struct.unpack('>HHIH', data[i:i + 10])
        i += 10
        rd = data[i:i + ln]
        if rtype == 12:
            value = _read_name(data, i)[0]
        elif rtype == 33:
            value = (struct.unpack('>H', rd[4:6])[0], _read_name(data, i + 6)[0])
        elif rtype == 16:
            value, j = {}, 0
            while j < len(rd):
                item = rd[j + 1:j + 1 + rd[j]].decode('utf-8', errors='replace')
                key, _, val = item.partition('=')
                value[key] = val
                j += 1 + rd[j]
        elif rtype == 1 and ln == 4:
            value = socket.inet_ntoa(rd)
        else:
            value = rd
        yield name, rtype, value
        i += ln


def mdns_browse(timeout=1.5, service=SERVICE):
    """Ask the local network for `service`. Returns [(ip, port, instance, txt)]."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        s.settimeout(0.2)
        s.sendto(dns_query(service), MDNS_ADDR)
        srv, txt, addr = {}, {}, {}
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, (src, _) = s.recvfrom(9000)
            except socket.timeout:
                continue
            try:
                for name, rtype, value in parse_dns(data):
                    if rtype == 33 and name.endswith(service):
                        srv[name] = (value, src)
                    elif rtype == 16 and name.endswith(service):
                        txt[name] = value
                    elif rtype == 1:
                        addr[name] = value
            except (ValueError, IndexError, struct.error):
                continue        # someone else's malformed packet
    except OSError:
        return []
    finally:
        s.close()
    found = []
    for instance, ((port, target), src) in srv.items():
        ip = addr.get(target, src)
        found.append((ip, port, instance[:-len(service) - 1], txt.get(instance, {})))
    return found


def lookup_port(host, timeout=1.5):
    """The control port the mixer at `host` advertises, or None."""
    for ip, port, _, _ in mdns_browse(timeout):
        if ip == host:
            return port
    return None


# ---------------------------------------------------------------- scan

def probe(host, port=P.PORT, timeout=0.5, read_timeout=1.5):
    """Return a Mixer if host:port answers like a TouchMix, else None."""
    try:
        with socket.create_connection((str(host), port), timeout=timeout) as s:
            s.settimeout(read_timeout)
            buf = b''
            while len(buf) < 6 or len(buf) < struct.unpack('<H', buf[4:6])[0]:
                chunk = s.recv(4096)
                if not chunk:
                    return None
                buf += chunk
                if len(buf) > 512:
                    return None
    except OSError:
        return None
    ty, sub, ln = struct.unpack('<HHH', buf[:6])
    if (ty, sub) != P.HELLO:
        return None
    try:
        name, suffix = P.decode_hello(buf[6:ln])
    except struct.error:
        return None
    if not suffix.startswith('MicroX'):
        return None
    return Mixer(str(host), name, suffix, port)


def discover(subnets=None, port=None, timeout=0.5, workers=128, mdns=True):
    """Find mixers. Without explicit subnets, ask mDNS first; if that finds
    nothing, scan the /24 around each local address on `port` (default 42217).
    Returns [Mixer(ip, name, suffix, port)]."""
    if mdns and not subnets:
        found = [m for m in (probe(ip, p, timeout) for ip, p, _, _ in mdns_browse()) if m]
        if found:
            return found
    nets = [ipaddress.ip_network(n, strict=False) for n in subnets] if subnets else default_subnets()
    # hosts() of a /32 or /31 is empty before Python 3.9, so take every address there
    hosts = [h for n in nets for h in (n.hosts() if n.num_addresses > 2 else n)]
    port = port or P.PORT
    found = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(lambda h: probe(h, port, timeout), hosts):
            if result:
                found.append(result)
    return found
