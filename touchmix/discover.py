"""Find TouchMix mixers on the local network.

The mixer greets every new TCP connection on port 42217 with a hello naming
itself, before the client says anything. So we try that port on every address
of the local /24 subnet(s), read the greeting, and hang up without sending
anything. The mixer never sees a client log in.

How the official app finds the mixer is not known yet (the captures were made
on the mixer's own Wi-Fi, where it is always 192.168.1.1). If a broadcast
protocol turns up later, it can be added here.
"""
import ipaddress
import socket
import struct
from concurrent.futures import ThreadPoolExecutor

from . import protocol as P


def local_ipv4_addresses():
    addrs = set()
    # the address used for the default route; connect() on UDP sends nothing
    for probe in ('192.0.2.1', '10.255.255.255'):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((probe, 9))
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


def probe(host, port=P.PORT, timeout=0.5, read_timeout=1.5):
    """Return (host, mixer_name, suffix) if host answers like a TouchMix, else None."""
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
    return str(host), name, suffix


def discover(subnets=None, port=P.PORT, timeout=0.5, workers=128):
    """Scan subnets (default: the /24 around each local address)."""
    nets = [ipaddress.ip_network(n, strict=False) for n in subnets] if subnets else default_subnets()
    hosts = [h for n in nets for h in n.hosts()]
    found = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(lambda h: probe(h, port, timeout), hosts):
            if result:
                found.append(result)
    return found
