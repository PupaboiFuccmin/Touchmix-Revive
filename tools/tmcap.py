#!/usr/bin/env python3
"""TouchMix-16 capture decoder (stdlib only).

Usage:
  tmcap.py summary  CAPTURE.pcap                 # flows + message type counts
  tmcap.py params   CAPTURE.pcap [-d params.csv] # decoded parameter changes, named
  tmcap.py files    CAPTURE.pcap OUTDIR          # extract + inflate *.dscomp transfers
  tmcap.py dict     CONFIG.xml params.csv [--values]
                                                 # build ID->name table from NH4-config.xml;
                                                 # --values also writes current values/text,
                                                 # which include the Wi-Fi password in clear
  tmcap.py raw      CAPTURE.pcap [TYPE ...]      # every message, hex payload

See docs/PROTOCOL.md for the wire format.
"""
import collections
import csv
import os
import re
import struct
import sys
import xml.etree.ElementTree as ET
import zlib

MIXER_PORT = 42217     # the port in the first captures; it changes when the mixer reboots

MSG_NAMES = {
    1: 'hello',
    2: 'session',
    3: 'file',
    4: 'status',
    5: 'param-set',
    6: 'poll-req',
    7: 'poll-resp',
    9: 'text',
}
KNOWN = {(1, 3), (2, 2), (3, 4), (4, 2), (5, 1), (6, 1), (7, 1), (9, 1)}
HELLO = struct.pack('<HH', 1, 3)       # type 1.3, the first message on every connection


# ---------------------------------------------------------------- pcap / tcp

def read_pcap(path):
    """Yield dicts for each TCP/UDP packet in a classic libpcap file."""
    d = open(path, 'rb').read()
    magic = d[:4]
    if magic in (b'\xd4\xc3\xb2\xa1', b'\x4d\x3c\xb2\xa1'):
        e = '<'
    elif magic in (b'\xa1\xb2\xc3\xd4', b'\xa1\xb2\x3c\x4d'):
        e = '>'
    else:
        sys.exit('not a classic pcap file (pcapng is not supported; export as pcap)')
    nano = magic in (b'\x4d\x3c\xb2\xa1', b'\xa1\xb2\x3c\x4d')
    link = struct.unpack(e + 'I', d[20:24])[0]
    off = 24
    while off + 16 <= len(d):
        ts, tu, incl, _ = struct.unpack(e + 'IIII', d[off:off + 16])
        off += 16
        pkt = d[off:off + incl]
        off += incl
        t = ts + tu / (1e9 if nano else 1e6)
        if link == 1:        # ethernet
            pkt = pkt[14:]
        elif link == 113:    # linux cooked
            pkt = pkt[16:]
        # 101/228/229 (raw IP, used by PCAPdroid) need no stripping
        if not pkt:
            continue
        ver = pkt[0] >> 4
        if ver == 4:
            ihl = (pkt[0] & 15) * 4
            proto = pkt[9]
            src, dst = '.'.join(map(str, pkt[12:16])), '.'.join(map(str, pkt[16:20]))
            total = struct.unpack('>H', pkt[2:4])[0]
            body = pkt[ihl:total]
        elif ver == 6:
            proto = pkt[6]
            src, dst = pkt[8:24].hex(), pkt[24:40].hex()
            body = pkt[40:]
        else:
            continue
        if proto == 6 and len(body) >= 20:
            sp, dp, seq = struct.unpack('>HHI', body[:8])
            doff = (body[12] >> 4) * 4
            yield dict(t=t, proto='tcp', src=src, dst=dst, sp=sp, dp=dp, seq=seq, data=body[doff:])
        elif proto == 17 and len(body) >= 8:
            sp, dp = struct.unpack('>HH', body[:4])
            yield dict(t=t, proto='udp', src=src, dst=dst, sp=sp, dp=dp, data=body[8:])


def tcp_streams(path):
    """Reassemble TCP payloads per direction, dropping retransmits."""
    out, seen = {}, {}
    for p in read_pcap(path):
        if p['proto'] != 'tcp' or not p['data']:
            continue
        key = (p['src'], p['sp'], p['dst'], p['dp'])
        s = seen.setdefault(key, set())
        if p['seq'] in s:
            continue
        s.add(p['seq'])
        out.setdefault(key, []).append((p['t'], p['seq'], p['data']))
    for key, chunks in out.items():
        base = chunks[0][1]
        chunks.sort(key=lambda c: (c[1] - base) % 2**32)
    return out


# ---------------------------------------------------------------- messages

def mixer_ports(streams):
    """{(ip, port)} of the mixer, found by its hello: it greets first on every
    connection, and its hello ends in a bare product code ('...MicroX16'),
    where the app's ends in '-AndroidTablet'. Falls back to MIXER_PORT."""
    found = set()
    for key, chunks in streams.items():
        head = chunks[0][2] if chunks else b''
        if len(head) < 6 or not head.startswith(HELLO):
            continue
        ln = struct.unpack('<H', head[4:6])[0]
        if re.search(rb'MicroX\d+$', head[6:ln]):
            found.add((key[0], key[1]))
    if not found:
        found = {(k[0], k[1]) for k in streams if k[1] == MIXER_PORT}
    return found


def messages(path):
    """Return [(t, dir, conn, type, sub, payload)] sorted by time.

    Every message is: u16 type, u16 subtype, u16 total length (incl. header), LE.
    """
    out = []
    streams = tcp_streams(path)
    ports = mixer_ports(streams)
    for key, chunks in streams.items():
        mixer_side = (key[0], key[1]) in ports
        if not mixer_side and (key[2], key[3]) not in ports:
            continue
        direction = 'M>A' if mixer_side else 'A>M'
        conn = key[3] if direction == 'M>A' else key[1]
        buf = b''
        for t, _, data in chunks:
            buf += data
            while len(buf) >= 6:
                ty, sub, ln = struct.unpack('<HHH', buf[:6])
                if ln < 6:
                    print('desync on', key, buf[:16].hex(), file=sys.stderr)
                    buf = b''
                    break
                if len(buf) < ln:
                    break
                out.append((t, direction, conn, ty, sub, buf[6:ln]))
                buf = buf[ln:]
        if buf:
            print('%d trailing bytes on %s' % (len(buf), key), file=sys.stderr)
    out.sort(key=lambda m: m[0])
    return out


def f32(raw):
    return struct.unpack('<f', struct.pack('<I', raw))[0]


def param_entries(ty, payload):
    """Yield (param_id, tag, raw_u32) from type 4 or type 5 messages."""
    if ty == 4:                    # u16 id, f32 value (no tag)
        pid, raw = struct.unpack('<HI', payload[:6])
        yield pid, 0, raw
    elif ty == 5:                  # u32 count, u16 bytes, count x (u16 id, u16 tag, f32)
        count, _nbytes = struct.unpack('<IH', payload[:6])
        for i in range(count):
            yield struct.unpack('<HHI', payload[6 + 8 * i:14 + 8 * i])


def load_dict(path):
    names = {}
    if path and os.path.exists(path):
        for row in csv.DictReader(open(path, newline='')):
            names[int(row['index'])] = '%s > %s' % (row['path'], row['name'])
    return names


# ---------------------------------------------------------------- commands

def cmd_summary(path):
    flows = collections.Counter()
    for p in read_pcap(path):
        flows[(p['proto'], p['src'], p['sp'], p['dst'], p['dp'])] += 1
    print('flows:')
    for k, v in flows.most_common(20):
        print('  %6d  %s %s:%d -> %s:%d' % ((v,) + k))
    ms = messages(path)
    counts, sizes = collections.Counter(), collections.defaultdict(collections.Counter)
    for _, d, conn, ty, sub, p in ms:
        k = (conn, d, ty, sub)
        counts[k] += 1
        sizes[k][len(p) + 6] += 1
    print('messages:')
    for k in sorted(counts):
        conn, d, ty, sub = k
        print('  conn %-5d %s  %d.%d %-10s x%-6d sizes %s%s' % (
            conn, d, ty, sub, MSG_NAMES.get(ty, '?'), counts[k], dict(sizes[k].most_common(4)),
            '   <-- NEW message type' if (ty, sub) not in KNOWN else ''))
    if ms:
        print('duration %.1fs' % (ms[-1][0] - ms[0][0]))


def cmd_params(path, dict_path):
    names = load_dict(dict_path)
    ms = messages(path)
    t0 = ms[0][0] if ms else 0
    for t, d, conn, ty, sub, p in ms:
        if ty not in (4, 5):
            continue
        for pid, tag, raw in param_entries(ty, p):
            print('%8.2f %s %d.%d 0x%04x %-5d tag=%04x raw=%08x %10.4f  %s' % (
                t - t0, d, ty, sub, pid, pid, tag, raw, f32(raw), names.get(pid, '')))


def cmd_files(path, outdir):
    os.makedirs(outdir, exist_ok=True)
    files, current = collections.OrderedDict(), {}
    for _, d, conn, ty, sub, p in messages(path):
        if ty != 3:
            continue
        off, ln = struct.unpack('<IH', p[:6])
        body = p[6:6 + ln]
        if off == 0xffffffff:      # start: file name
            current[conn] = body.decode(errors='replace')
            files[(conn, current[conn])] = bytearray()
        elif off == 0xfffffffe:    # end: full path on the mixer + 6 trailer bytes
            print('conn %d: %s (trailer %s)' % (conn, body.decode(errors='replace'), p[6 + ln:].hex()))
        else:
            buf = files[(conn, current[conn])]
            if off + ln > len(buf):
                buf.extend(b'\0' * (off + ln - len(buf)))
            buf[off:off + ln] = body
    for (conn, name), data in files.items():
        dest = os.path.join(outdir, '%d-%s' % (conn, name.replace('.dscomp', '')))
        try:
            data = zlib.decompress(bytes(data))
        except zlib.error:
            dest += '.dscomp'
        open(dest, 'wb').write(data)
        print('wrote %s (%d bytes)' % (dest, len(data)))


def cmd_dict(xml_path, csv_path, with_values=False):
    rows = []

    def walk(el, path):
        for c in el:
            if c.tag == 'Parameter':
                rows.append((int(c.get('Index')), ' > '.join(path), c.get('name'),
                             c.get('Type'), c.get('value'), c.get('text') or ''))
            else:
                walk(c, path + [c.get('Name') or c.tag])

    walk(ET.parse(xml_path).getroot(), [])
    rows.sort()
    with open(csv_path, 'w', newline='') as f:
        w = csv.writer(f)
        header = ['index', 'index_hex', 'path', 'name', 'type']
        w.writerow(header + ['value', 'text'] if with_values else header)
        for r in rows:
            w.writerow([r[0], '0x%04x' % r[0]] + list(r[1:] if with_values else r[1:4]))
    print('%d parameters -> %s' % (len(rows), csv_path))


def cmd_raw(path, types):
    ms = messages(path)
    t0 = ms[0][0] if ms else 0
    for t, d, conn, ty, sub, p in ms:
        if not types or ty in types:
            print('%8.3f %s %d %d.%d %s' % (t - t0, d, conn, ty, sub, p.hex(' ')))


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    cmd, path = argv[1], argv[2]
    if cmd == 'summary':
        cmd_summary(path)
    elif cmd == 'params':
        default = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'touchmix', 'data', 'params.csv')
        cmd_params(path, argv[argv.index('-d') + 1] if '-d' in argv else default)
    elif cmd == 'files' and len(argv) > 3:
        cmd_files(path, argv[3])
    elif cmd == 'dict' and len(argv) > 3:
        cmd_dict(path, argv[3], '--values' in argv)
    elif cmd == 'raw':
        cmd_raw(path, [int(x) for x in argv[3:]])
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main(sys.argv)
