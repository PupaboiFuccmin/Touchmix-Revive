"""Command line: python -m touchmix <command> ...

Examples:
  python -m touchmix discover
  python -m touchmix info
  python -m touchmix monitor
  python -m touchmix mute 4 on --verify
  python -m touchmix fader 4 -10db
  python -m touchmix pan 4 L50
  python -m touchmix trim 4 -3
  python -m touchmix send 4 aux1 -10db
  python -m touchmix send 4 fx1 -20db
  python -m touchmix gate 4 -40
  python -m touchmix param 4 Comp Thresh -20
  python -m touchmix param 4 EQ Q 2 --band 3
  python -m touchmix eq 4 1 gain -3
  python -m touchmix set "In 4 > Control > Mute" 1
  python -m touchmix find "aux 1 > control"
  python -m touchmix map list
  python -m touchmix map probe Fader --channels 1-16
"""
import argparse
import os
import re
import sys
import time

from .discover import discover, default_subnets, lookup_port, mdns_browse
from . import protocol as P
from . import mapping as M
from .mapping import parse_channels
from .client import TouchMix, TouchMixError
from .params import ParamTable, strip_path, send_name, curve_for


def pick_host(args):
    """Work out the mixer's address, and set args.port. The control port is not
    fixed (it changes when the mixer reboots), so unless --port or host:port
    says otherwise it comes from the mixer's mDNS advertisement."""
    host = args.host or os.environ.get('TOUCHMIX_HOST')
    if host:
        if ':' in host:
            host, _, port = host.rpartition(':')
            args.port = args.port or int(port)
        if not args.port:
            args.port = lookup_port(host) or P.PORT
        return host
    print('No --host given; searching the local network...', file=sys.stderr)
    found = discover(args.subnet, port=args.port)
    if not found:
        sys.exit('No mixer found. Check it is on the same network, or pass --host / --subnet.')
    if len(found) > 1:
        for m in found:
            print('  %-15s port %-6d %s' % (m.ip, m.port, m.name), file=sys.stderr)
        sys.exit('Several mixers found; pick one with --host.')
    m = found[0]
    print('Found %s at %s port %d' % (m.name, m.ip, m.port), file=sys.stderr)
    args.port = m.port
    return m.ip


def open_mixer(args):
    tm = TouchMix(pick_host(args), port=args.port, name=args.name)
    tm.connect()
    tm.wait_ready()
    return tm


NO_ACCESS = """!! The mixer gives this client ("%s") no access, so it will ignore every change
!! sent from here. Grant it on the mixer, in the Wi-Fi device access settings:
!! find this name in the device list and allow All (or the mixes you need).
!! The mixer remembers it per client name, so this is needed only once."""


def warn_no_access(tm):
    """Print a warning and return True if the mixer says this client may not write."""
    if tm.can_write() is False:
        print(NO_ACCESS % tm.name, file=sys.stderr)
        return True
    return False


def describe_access(access):
    if access is None:
        return 'unknown (no settings file received)'
    allowed = [k for k, v in access.items() if v and k != 'Locked']
    if access.get('All'):
        return 'all'
    return ', '.join(allowed) if allowed else 'NONE: changes from this PC are ignored'


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
    found = []
    if not args.subnet:
        print('Asking the network (mDNS) for TouchMix mixers...')
        found = discover(mdns=True, subnets=None, port=args.port) if mdns_browse() else []
    if not found:
        nets = args.subnet or [str(n) for n in default_subnets()]
        print('Scanning %s on port %d...' % (', '.join(map(str, nets)), args.port or P.PORT))
        found = discover(nets, port=args.port)
    for m in found:
        print('  %-15s port %-6d %-24s (%s)' % (m.ip, m.port, m.name, m.suffix))
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
        print('Client:   %s, access: %s' % (tm.name, describe_access(tm.wait_access())))
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
        curve = curve_for(par.path, par.name) if par else None
        print('%s 0x%04x %-9.4f %-11s raw=%08x tag=%04x %s' % (
            time.strftime('%H:%M:%S'), pid, value, curve.format(value) if curve else '',
            raw, tag, par.full_name if par else '?'), flush=True)

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
    warn_no_access(tm)
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


def cmd_trim(args):
    pid = ParamTable().find('%s > Control' % strip_path(args.channel), 'Trim').index
    return apply_and_verify(args, lambda tm: tm.trim(args.channel, args.value), pid)


def cmd_send(args):
    pid = ParamTable().find('%s > Control' % strip_path(args.channel), send_name(args.dest)).index
    return apply_and_verify(args, lambda tm: tm.send(args.channel, args.dest, args.value), pid)


def cmd_gate(args):
    """gate 4 on|off, or gate 4 -40 for the threshold in dB."""
    path = '%s > Gate' % strip_path(args.channel)
    v = args.value.strip().lower()
    if v in ('on', 'off'):
        pid = ParamTable().find(path, 'Enable').index
        return apply_and_verify(args, lambda tm: tm.gate(args.channel, v == 'on'), pid)
    db = float(v.replace('db', ''))
    pid = ParamTable().find(path, 'Thresh').index
    return apply_and_verify(args, lambda tm: tm.gate_threshold(args.channel, db), pid)


def cmd_param(args):
    table = ParamTable()
    par = table.find('%s > %s' % (strip_path(args.channel), args.section), args.param, args.band - 1)
    curve = curve_for(par.path, par.name)
    if curve is None:
        sys.exit('no curve mapped for %s yet; use "set" with a raw 0..1 value' % par.full_name)
    norm = curve.to_norm(args.value)
    print('%s = %s (wire %.6f)' % (par.full_name, curve.format(norm), norm))
    return apply_and_verify(args, lambda tm: tm.set(par.index, norm), par.index)


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


# ---------------------------------------------------------------- mapping

def map_session(args, recorder=None):
    s = M.Session(pick_host(args), args.port, args.name, recorder)
    s.open()
    print('Connected to %s' % s.tm.mixer_name)
    return s


def cmd_map_list(args):
    for name, (_, target, text) in M.SCRIPTS.items():
        print('  %-12s %-6s %s' % (name, target or '', text))
    print('\nRun one with: touchmix map run <name> [target] [--readback]')


def cmd_map_run(args):
    table = ParamTable()
    free = args.script == 'free'
    if args.file:
        steps = M.load_script(args.file)
    elif free:
        steps = []
    elif args.script in M.SCRIPTS:
        fn, default_target, _ = M.SCRIPTS[args.script]
        steps = fn(table, args.target or default_target) if default_target else fn(table)
    else:
        sys.exit('unknown script %r (see: touchmix map list)' % args.script)
    rec = M.Recorder(echo=print, include_status=args.all)
    s = map_session(args, rec)
    try:
        results = M.guided(s, steps, readback=args.readback, free=free)
    except (KeyboardInterrupt, EOFError):
        print('\nstopped')
        results = None
    finally:
        s.close()
    if results:
        out = args.out or M.default_out(args.script)
        M.save(out, 'guided', s.host, s.tm.mixer_name,
               dict(script=args.file or args.script, target=args.target, readback=args.readback, steps=results))
        print('\nSaved %s' % out)


def cmd_map_probe(args):
    table = ParamTable()
    chans = parse_channels(args.channels)
    pids = [table.find('%s > %s' % (strip_path(c), args.section), args.param, args.band - 1).index for c in chans]
    values = M.sweep_values(args.steps)
    rounds = -(-len(values) // len(pids))
    print('Probe %s > %s on %d channel(s): %s' % (args.section, args.param, len(pids),
                                                 ', '.join(table[p].path.split(' >')[0] for p in pids)))
    print('%d values from 0 to 1, %d reconnects. These are LIVE controls: turn the amps down.' % (len(values), rounds))
    if not args.yes and input('Type yes to go ahead > ').strip().lower() != 'yes':
        return 1
    s = map_session(args)
    if warn_no_access(s.tm):
        s.close()
        return 1
    os.makedirs('mappings', exist_ok=True)
    rp = os.path.join('mappings', M.RESTORE_POINT)
    checkpoint = lambda originals: M.save_restore_point(rp, s.host, s.tm.mixer_name, args.param, originals)
    try:
        result = M.probe(s, pids, values, restore=not args.no_restore, checkpoint=checkpoint)
    finally:
        s.close()
    if result.get('restored'):
        os.remove(rp)
    print('\n  wire      value')
    for w, e in M.curve_table(result['points']):
        print('  %.4f    %s' % (w, e))
    out = args.out or os.path.join('mappings', M.default_out('probe-%s' % args.param.lower().replace(' ', '')))
    M.save(out, 'probe', s.host, s.tm.mixer_name,
           dict(section=args.section, param=args.param, band=args.band, channels=chans, **result))
    print('\nSaved %s' % out)


def cmd_map_probe_all(args):
    names = [n.strip() for n in args.plan.split(',')]
    unknown = [n for n in names if n not in M.PLANS]
    if unknown:
        sys.exit('unknown plan %s (choose from %s)' % (', '.join(unknown), ', '.join(M.PLANS)))
    plan = M.plan_items([item for n in names for item in M.PLANS[n]])
    if args.only:
        want = [w.strip().lower() for w in args.only.split(',')]
        plan = [p for p in plan if any(w in M.plan_name(*p[:3]).lower() for w in want)]
    if not plan:
        sys.exit('nothing matches --only %r' % args.only)
    if args.channels:
        plan = [(sec, par, band, args.channels if ch == M.INPUTS else ch, st) for sec, par, band, ch, st in plan]
    if args.start > 1:
        plan = plan[args.start - 1:]
        if not plan:
            sys.exit('--start %d is past the end of the plan' % args.start)
    print('Probe batch (%s): %d controls, results in %s/' % (args.plan, len(plan), args.outdir))
    for i, (sec, par, band, ch, st) in enumerate(plan, args.start):
        print('  %3d. %-26s %-24s %d values' % (i, M.plan_name(sec, par, band), ch, st or args.steps))
    print('These are LIVE controls on every listed channel. Turn the amps down.')
    if not args.yes and input('Type yes to go ahead > ').strip().lower() != 'yes':
        return 1
    os.makedirs(args.outdir, exist_ok=True)
    s = map_session(args)
    if warn_no_access(s.tm):
        s.close()
        return 1
    mixer = s.tm.mixer_name
    save_fn = lambda path, body: M.save(path, 'probe', s.host, mixer, body)
    rp = os.path.join(args.outdir, M.RESTORE_POINT)
    checkpoint = lambda name, originals: M.save_restore_point(rp, s.host, mixer, name, originals)
    try:
        summary = M.probe_all(s, plan, args.steps, args.outdir, save_fn, restore=not args.no_restore,
                              checkpoint=checkpoint)
    finally:
        s.close()
    if summary and summary[-1][2][0] != 'error' and 'NOT RESTORED' not in summary[-1][2][0]:
        os.remove(rp)       # the last probe restored cleanly; earlier ones were checked as they ran
    print('\n' + M.format_summary(summary))
    bad = [n for n, _, (kind, _, _) in summary if kind == 'error' or 'NOT RESTORED' in kind]
    if bad:
        print('\n!! check these: %s' % ', '.join(bad))
    return 2 if bad else 0


def cmd_map_restore(args):
    """Put back the originals saved in a restore point (or any probe file)."""
    import json
    with open(args.file) as f:
        doc = json.load(f)
    originals = {int(k, 16): v for k, v in doc['originals'].items()}
    curve = [(p['wire'], p['eng']) for p in doc.get('points', [])]
    table = ParamTable()
    print('Restoring %d parameter(s) from %s (%s):' % (len(originals), args.file, doc.get('label') or doc.get('param')))
    for pid, eng in list(originals.items())[:10]:
        par = table.get(pid)
        print('  0x%04x %-40s -> %s' % (pid, par.full_name if par else '?', eng))
    if len(originals) > 10:
        print('  ... and %d more' % (len(originals) - 10))
    if not args.yes and input('Type yes to go ahead > ').strip().lower() != 'yes':
        return 1
    s = map_session(args)
    if warn_no_access(s.tm):
        s.close()
        return 1
    try:
        ok = M.restore_values(s, originals, curve)
    finally:
        s.close()
    return 0 if ok else 2


def cmd_map_show(args):
    import json
    with open(args.file) as f:
        doc = json.load(f)
    print('%s  %s  %s' % (doc.get('kind'), doc.get('mixer'), doc.get('created')))
    if doc.get('kind') == 'probe':
        print('%s > %s' % (doc['section'], doc['param']))
        for w, e in M.curve_table(doc['points']):
            print('  %.4f    %s' % (w, e))
        return
    for st in doc.get('steps', []):
        print('\n%d. %s%s' % (st['step'], st['prompt'], '  (skipped)' if st.get('skipped') else ''))
        if not st.get('skipped'):
            print(M._format_step(st))
        if st.get('note'):
            print('    note: %s' % st['note'])


def main(argv=None):
    ap = argparse.ArgumentParser(prog='touchmix', description='Control a QSC TouchMix-16 over the network.')
    ap.add_argument('--host', help='mixer IP or IP:port (default: $TOUCHMIX_HOST, else auto-discover)')
    ap.add_argument('--subnet', action='append', help='subnet to scan, e.g. 192.168.0.0/24 (repeatable)')
    ap.add_argument('--port', type=int, help='control port (default: what the mixer advertises over mDNS, else 42217)')
    ap.add_argument('--name', help='client name shown to the mixer (default: Revive-<hostname>)')
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('discover', help='find mixers on the local network').set_defaults(fn=cmd_discover)
    sub.add_parser('info', help='show mixer name, firmware and channel overview').set_defaults(fn=cmd_info)
    m = sub.add_parser('monitor', help='print every change the mixer reports')
    m.add_argument('--all', action='store_true', help='include status pushes (Wi-Fi strength etc.)')
    m.set_defaults(fn=cmd_monitor)

    def ctl(name, helptext, fn, *extra):
        p = sub.add_parser(name, help=helptext)
        p.add_argument('channel', help='1-16, st17, st19, main, aux1-10, fx1-4, dca1-8, pb (2-track), talkback')
        for a, kw in extra:
            p.add_argument(a, **kw)
        p.add_argument('--verify', action='store_true', help='reconnect and confirm the mixer stored the change')
        p.set_defaults(fn=fn)

    ctl('mute', 'mute on/off', cmd_mute, ('state', dict(type=on_off)))
    ctl('solo', 'cue on/off', cmd_solo, ('state', dict(type=on_off)))
    ctl('fader', 'fader: -10db, +6db, 0db, off (or raw 0..1)', cmd_fader, ('value', dict()))
    ctl('pan', 'pan: -100..100, L50, C, R', cmd_pan, ('value', dict()))
    ctl('trim', 'input trim (digital gain) in dB: -15..+15, stereo inputs 0..+12', cmd_trim,
        ('value', dict(type=float)))
    ctl('send', 'aux/FX send level: -10db, 0db, off (or raw 0..1)', cmd_send,
        ('dest', dict(help='aux1-aux10 or fx1-fx4')), ('value', dict()))
    ctl('gate', 'gate on/off, or threshold in dB (-60..0)', cmd_gate, ('value', dict()))
    ctl('param', 'any mapped control in real units: param 4 Comp Thresh -20', cmd_param,
        ('section', dict(help='Control, EQ, Lo-Cut, Hi-Cut, Gate, Comp, De-Esser')),
        ('param', dict(help='name in that section, e.g. Thresh, Ratio, "Delay Time"')),
        ('value', dict(help='in the control\'s units (dB, Hz, ms, ratio); on/off for switches')),
        ('--band', dict(type=int, default=1, help='which one, for EQ bands (default 1)')))
    ctl('eq', 'EQ band gain (dB) or freq (Hz)', cmd_eq,
        ('band', dict(type=int, choices=[1, 2, 3, 4, 5, 6], help='1-4 on inputs, 1-6 on main/aux')),
        ('what', dict(choices=['gain', 'freq'])),
        ('value', dict(type=float)))

    s = sub.add_parser('set', help='set any parameter by index or "Path > Name" to a raw 0..1 value')
    s.add_argument('param')
    s.add_argument('value', help='normalized float, or int:N for integer parameters')
    s.add_argument('--verify', action='store_true')
    s.set_defaults(fn=cmd_set)

    f = sub.add_parser('find', help='search parameter names (offline)')
    f.add_argument('text')
    f.set_defaults(fn=cmd_find)

    mp = sub.add_parser('map', help='mapping assistant: find IDs and curves (see "map list")')
    msub = mp.add_subparsers(dest='map_cmd', required=True)
    msub.add_parser('list', help='list the built-in guided scripts').set_defaults(fn=cmd_map_list)
    r = msub.add_parser('run', help='guided: prompt for actions and record what changes')
    r.add_argument('script', help='built-in script name (see "map list"), or "free"')
    r.add_argument('target', nargs='?', help='channel / bus for the script, e.g. 9, aux1, fx2, dca1')
    r.add_argument('--file', help='JSON script file instead of a built-in')
    r.add_argument('--readback', action='store_true',
                   help='reconnect after each step and record the mixer\'s own value (dB/Hz) for each change')
    r.add_argument('--all', action='store_true', help='include status pushes')
    r.add_argument('--out', help='output JSON (default mapping-<script>-<time>.json)')
    r.set_defaults(fn=cmd_map_run)
    pr = msub.add_parser('probe', help='write a sweep of values and read back the mixer\'s own units (curves)')
    pr.add_argument('param', help='parameter name within the section, e.g. Fader, Trim, Thresh, AuxSend1')
    pr.add_argument('--section', default='Control', help='section: Control, EQ, Gate, Comp, Lo-Cut... (default Control)')
    pr.add_argument('--band', type=int, default=1, help='which one, for repeated names such as EQ bands (default 1)')
    pr.add_argument('--channels', default='4', help='channels to sweep in parallel, e.g. 1-16 or 4 (default 4)')
    pr.add_argument('--steps', type=int, default=21, help='number of values from 0 to 1 (default 21)')
    pr.add_argument('--yes', action='store_true', help='do not ask for confirmation')
    pr.add_argument('--no-restore', action='store_true', help='leave the controls where the sweep ends')
    pr.add_argument('--out', help='output JSON (default mappings/mapping-probe-<param>-<time>.json)')
    pr.set_defaults(fn=cmd_map_probe)
    pa = msub.add_parser('probe-all', help='run a whole plan of curve probes in order (see --plan)')
    pa.add_argument('--plan', default='inputs',
                    help='which plan(s): %s, comma-separated (default inputs)' % ', '.join(M.PLANS))
    pa.add_argument('--only', help='comma-separated filters on the control name, e.g. "comp,gate attack"')
    pa.add_argument('--channels', help='input channels to sweep (default 1-16)')
    pa.add_argument('--steps', type=int, default=21, help='values from 0 to 1 per control (default 21)')
    pa.add_argument('--outdir', default='mappings', help='where to save the JSON files (default mappings)')
    pa.add_argument('--start', type=int, default=1, help='start at this step of the listed plan (to resume)')
    pa.add_argument('--yes', action='store_true', help='do not ask for confirmation')
    pa.add_argument('--no-restore', action='store_true', help=argparse.SUPPRESS)
    pa.set_defaults(fn=cmd_map_probe_all)
    rs = msub.add_parser('restore', help='put back the values saved in a restore point after a crash')
    rs.add_argument('file', nargs='?', default=os.path.join('mappings', M.RESTORE_POINT),
                    help='restore point or probe file (default mappings/%s)' % M.RESTORE_POINT)
    rs.add_argument('--yes', action='store_true', help='do not ask for confirmation')
    rs.set_defaults(fn=cmd_map_restore)
    sh = msub.add_parser('show', help='print a saved mapping file')
    sh.add_argument('file')
    sh.set_defaults(fn=cmd_map_show)

    # argparse takes '-10db' and '-inf' for options; a leading space makes them values
    argv = sys.argv[1:] if argv is None else list(argv)
    argv = [' ' + a if re.fullmatch(r'-(inf|[\d.]+ ?db)', a, re.I) else a for a in argv]
    args = ap.parse_args(argv)
    try:
        return args.fn(args) or 0
    except (TouchMixError, OSError) as e:
        sys.exit('error: %s' % e)
    except (KeyError, ValueError) as e:
        sys.exit('error: %s' % e)
