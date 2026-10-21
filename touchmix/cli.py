"""Command line: python -m touchmix <command> ...

Examples:
  python -m touchmix discover
  python -m touchmix info
  python -m touchmix monitor
  python -m touchmix mute 4 on --verify
  python -m touchmix fader 4 0db
  python -m touchmix pan 4 L50
  python -m touchmix eq 4 1 gain -3
  python -m touchmix set "In 4 > Control > Mute" 1
  python -m touchmix find "aux 1 > control"
"""
import argparse
import os
import sys
import time

from . import discover as D
from .client import TouchMix, TouchMixError
from .params import ParamTable, strip_path


def pick_host(args):
    if args.host:
        return args.host
    if os.environ.get('TOUCHMIX_HOST'):
        return os.environ['TOUCHMIX_HOST']
    print('No --host given; searching the local network...', file=sys.stderr)
    found = D.discover(args.subnet, port=args.port)
    if not found:
        sys.exit('No mixer found. Check it is on the same network, or pass --host / --subnet.')
    if len(found) > 1:
        for ip, name, _ in found:
            print('  %-15s %s' % (ip, name), file=sys.stderr)
        sys.exit('Several mixers found; pick one with --host.')
    ip, name, _ = found[0]
    print('Found %s at %s' % (name, ip), file=sys.stderr)
    return ip


def open_mixer(args):
    tm = TouchMix(pick_host(args), port=args.port, name=args.name)
    tm.connect()
    tm.wait_ready()
    return tm


def on_off(s):
    s = s.lower()
    if s in ('on', '1', 'true', 'yes'):
        return True
    if s in ('off', '0', 'false', 'no'):
        return False
    raise argparse.ArgumentTypeError('expected on/off')


def fmt_db(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return '-inf' if f <= -55 else '%+.1f dB' % f


# ---------------------------------------------------------------- commands

def cmd_discover(args):
    nets = args.subnet or [str(n) for n in D.default_subnets()]
    print('Scanning %s for TouchMix on port 42217...' % ', '.join(map(str, nets)))
    found = D.discover(nets, port=args.port)
    for ip, name, suffix in found:
        print('  %-15s %-24s (%s)' % (ip, name, suffix))
    if not found:
        print('  none found')
        return 1


def cmd_info(args):
    tm = open_mixer(args)
    try:
        p = tm.params
        text = lambda path, name: tm.config_text.get(p.find(path, name).index, '')
        print('Mixer:    %s at %s' % (tm.mixer_name, tm.host))
        print('Firmware: %s' % text('Global > Control', 'Version'))
        print('Mixer IP: %s' % text('Global > Control', 'Current IP'))
        print()
        print('  %-4s %-12s %-10s %s' % ('ch', 'name', 'fader', 'mute'))
        for ch in range(1, 17):
            path = 'In %d > Control' % ch
            name = text(path, 'Name')
            fader = tm.config.get(p.find(path, 'Fader').index)
            mute = tm.config.get(p.find(path, 'Mute').index)
            print('  %-4d %-12s %-10s %s' % (ch, name, fmt_db(fader), 'MUTED' if mute == '1' else ''))
    finally:
        tm.close()


def cmd_monitor(args):
    tm = TouchMix(pick_host(args), port=args.port, name=args.name)

    @tm.on_change
    def show(pid, value, raw, tag, source):
        if source == 'status' and not args.all:
            return
        par = tm.params.get(pid)
        print('%s 0x%04x %-9.4f raw=%08x tag=%04x %s' % (
            time.strftime('%H:%M:%S'), pid, value, raw, tag, par.full_name if par else '?'), flush=True)

    tm.connect()
    tm.wait_ready()
    print('Connected to %s. Change something on the mixer (Ctrl+C to stop).' % tm.mixer_name)
    try:
        while tm.connected:
            time.sleep(0.5)
        print('Disconnected:', tm.error)
    except KeyboardInterrupt:
        pass
    finally:
        tm.close()


def apply_and_verify(args, action, pid):
    """Run action(tm); with --verify, reconnect and read the value back from the mixer's config."""
    tm = open_mixer(args)
    try:
        before = tm.config.get(pid)
        action(tm)
        time.sleep(0.3)
    finally:
        tm.close()
    par = tm.params.get(pid)
    print('sent %s (0x%04x)' % (par.full_name if par else '?', pid))
    if args.verify:
        time.sleep(0.5)
        check = TouchMix(tm.host, port=args.port, name=args.name)
        check.connect()
        check.wait_ready()
        after = check.config.get(pid)
        check.close()
        print('config before: %s   after: %s   %s' % (before, after, 'CHANGED' if before != after else 'unchanged'))
        return 0 if before != after else 2


def cmd_mute(args):
    pid = ParamTable().find('%s > Control' % strip_path(args.channel), 'Mute').index
    return apply_and_verify(args, lambda tm: tm.mute(args.channel, args.state), pid)


def cmd_solo(args):
    pid = ParamTable().find('%s > Control' % strip_path(args.channel), 'Solo').index
    return apply_and_verify(args, lambda tm: tm.solo(args.channel, args.state), pid)


def cmd_fader(args):
    pid = ParamTable().find('%s > Control' % strip_path(args.channel), 'Fader').index
    return apply_and_verify(args, lambda tm: tm.fader(args.channel, args.value), pid)


def cmd_pan(args):
    pid = ParamTable().find('%s > Control' % strip_path(args.channel), 'Pan').index
    return apply_and_verify(args, lambda tm: tm.pan(args.channel, args.value), pid)


def cmd_eq(args):
    name = {'gain': 'Gain', 'freq': 'Freq'}[args.what]
    pid = ParamTable().find('%s > EQ' % strip_path(args.channel), name, args.band - 1).index
    if args.what == 'gain':
        act = lambda tm: tm.eq_gain(args.channel, args.band, args.value)
    else:
        act = lambda tm: tm.eq_freq(args.channel, args.band, args.value)
    return apply_and_verify(args, act, pid)


def cmd_set(args):
    table = ParamTable()
    probe = TouchMix('-', params=table)
    pid = probe.resolve(args.param)
    value = ('int', int(args.value[4:], 0)) if args.value.startswith('int:') else float(args.value)
    return apply_and_verify(args, lambda tm: tm.set(pid, value), pid)


def cmd_find(args):
    for p in ParamTable().search(args.text):
        print('%5d 0x%04x  %s' % (p.index, p.index, p.full_name))


def main(argv=None):
    ap = argparse.ArgumentParser(prog='touchmix', description='Control a QSC TouchMix-16 over the network.')
    ap.add_argument('--host', help='mixer IP (default: $TOUCHMIX_HOST, else auto-discover)')
    ap.add_argument('--subnet', action='append', help='subnet to scan, e.g. 192.168.0.0/24 (repeatable)')
    ap.add_argument('--port', type=int, default=42217, help=argparse.SUPPRESS)
    ap.add_argument('--name', help='client name shown to the mixer (default: Revive-<hostname>)')
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('discover', help='find mixers on the local network').set_defaults(fn=cmd_discover)
    sub.add_parser('info', help='show mixer name, firmware and channel overview').set_defaults(fn=cmd_info)
    m = sub.add_parser('monitor', help='print every change the mixer reports')
    m.add_argument('--all', action='store_true', help='include status pushes (Wi-Fi strength etc.)')
    m.set_defaults(fn=cmd_monitor)

    def ctl(name, helptext, fn, *extra):
        p = sub.add_parser(name, help=helptext)
        p.add_argument('channel', help='1-16, st17, st19, main, aux1-10, fx1-4, dca1-8')
        for a, kw in extra:
            p.add_argument(a, **kw)
        p.add_argument('--verify', action='store_true', help='reconnect and confirm the mixer stored the change')
        p.set_defaults(fn=fn)

    ctl('mute', 'mute on/off', cmd_mute, ('state', dict(type=on_off)))
    ctl('solo', 'cue on/off', cmd_solo, ('state', dict(type=on_off)))
    ctl('fader', 'fader: 0..1, 0db, or off', cmd_fader, ('value', dict()))
    ctl('pan', 'pan: -100..100, L50, C, R', cmd_pan, ('value', dict()))
    ctl('eq', 'EQ band gain (dB) or freq (Hz)', cmd_eq,
        ('band', dict(type=int, choices=[1, 2, 3, 4])), ('what', dict(choices=['gain', 'freq'])),
        ('value', dict(type=float)))

    s = sub.add_parser('set', help='set any parameter by index or "Path > Name" to a raw 0..1 value')
    s.add_argument('param')
    s.add_argument('value', help='normalized float, or int:N for integer parameters')
    s.add_argument('--verify', action='store_true')
    s.set_defaults(fn=cmd_set)

    f = sub.add_parser('find', help='search parameter names (offline)')
    f.add_argument('text')
    f.set_defaults(fn=cmd_find)

    args = ap.parse_args(argv)
    try:
        return args.fn(args) or 0
    except (TouchMixError, OSError) as e:
        sys.exit('error: %s' % e)
    except (KeyError, ValueError) as e:
        sys.exit('error: %s' % e)
