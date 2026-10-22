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
  python -m touchmix record status
  python -m touchmix record arm 1-8
  python -m touchmix record start
  python -m touchmix collect
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
import struct
import sys
import threading
import time

from .discover import discover, default_subnets, lookup_port, mdns_browse
from . import protocol as P
from . import collect as C
from . import mapping as M
from .mapping import parse_channels
from .client import TouchMix, TouchMixError
from .params import (ParamTable, strip_path, send_name, curve_for, TRANSPORT_STATES, transport_name,
                     FX_TYPE_NAMES, fx_type_number)


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


def make_writer(path, echo=True):
    """out(line): append to `path` (if given) and print (if echo or no file)."""
    log = open(path, 'a', encoding='utf-8') if path else None
    lock = threading.Lock()

    def out(line):
        with lock:
            if log:
                log.write(line + '\n')
                log.flush()
            if not log or echo:
                print(line, flush=True)
    out.close = log.close if log else (lambda: None)
    return out


def attach_logger(tm, out, status=False, raw=False, polls=False):
    """Report what the mixer sends through out(): parameter changes in real
    units, text values, and optionally status pushes, unknown message types,
    file transfers and changed poll replies."""

    @tm.on_change
    def show(pid, value, raw_u32, tag, source):
        if source == 'status' and not status:
            return
        par = tm.params.get(pid)
        if source == 'text':
            out('%s 0x%04x text %r  %s' % (time.strftime('%H:%M:%S'), pid, tm.config_text.get(pid),
                                           par.full_name if par else '?'))
            return
        curve = curve_for(par.path, par.name) if par else None
        out('%s 0x%04x %-9.4f %-11s raw=%08x tag=%04x %s' % (
            time.strftime('%H:%M:%S'), pid, value, curve.format(value) if curve else '',
            raw_u32, tag, par.full_name if par else '?'))

    known = (P.PARAM_SET, P.STATUS, P.POLL_RESP, P.FILE, P.TEXT, P.TEXT_ALT)
    last_poll = {}

    @tm.on_message
    def show_raw(msg_type, payload):
        stamp = time.strftime('%H:%M:%S')
        if raw and msg_type not in known:
            out('%s MSG %d.%d len=%d %s' % ((stamp,) + msg_type + (len(payload), payload[:64].hex())))
        elif raw and msg_type == P.FILE:
            off, data, _ = P.decode_file_chunk(payload)
            if off in (P.FILE_START, P.FILE_END):
                out('%s FILE %s %s' % (stamp, 'start' if off == P.FILE_START else 'end',
                                       data.decode('ascii', 'replace')))
        elif polls and msg_type == P.POLL_RESP:
            body = payload[4:]                       # drop the sequence number
            prev = last_poll.get(len(body))
            last_poll[len(body)] = body
            if prev is not None and prev != body:
                out('%s POLL len=%d %s' % (stamp, len(body), body.hex()))     # raw: the layout is not known


def cmd_monitor(args):
    tm = TouchMix(pick_host(args), port=args.port, name=args.name)
    out = make_writer(args.log, echo=not args.quiet)
    log = args.log
    attach_logger(tm, out, status=args.all, raw=args.raw, polls=args.polls)

    tm.connect()
    tm.wait_ready()
    out('%s CONNECTED %s' % (time.strftime('%H:%M:%S'), tm.mixer_name))
    try:
        if log:
            print('Logging to %s. Before each action, type what you are about to do and press Enter.' % args.log)
            print('An empty line or Ctrl+C stops.')
            while tm.connected:
                note = input('note> ').strip()
                if not note:
                    break
                out('%s NOTE %s' % (time.strftime('%H:%M:%S'), note))
        else:
            print('Connected to %s. Change something on the mixer (Ctrl+C to stop).' % tm.mixer_name)
            while tm.connected:
                time.sleep(0.5)
        if not tm.connected:
            out('%s DISCONNECTED %s' % (time.strftime('%H:%M:%S'), tm.error))
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        tm.close()
        out.close()
        if log:
            print('Saved %s' % args.log)


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
    name = {'gain': 'Gain', 'freq': 'Freq', 'shelf': 'BndEnbl'}[args.what]
    pid = ParamTable().find('%s > EQ' % strip_path(args.channel), name, args.band - 1).index
    if args.what == 'shelf':
        on = on_off(args.value)
        act = lambda tm: tm.eq_shelf(args.channel, args.band, on)
    elif args.what == 'gain':
        act = lambda tm: tm.eq_gain(args.channel, args.band, float(args.value))
    else:
        act = lambda tm: tm.eq_freq(args.channel, args.band, float(args.value))
    return apply_and_verify(args, act, pid)


def cmd_set(args):
    table = ParamTable()
    probe = TouchMix('-', params=table)
    pid = probe.resolve(args.param)
    value = ('int', int(args.value[4:], 0)) if args.value.startswith('int:') else float(args.value)

    def action(tm):
        tm.allow_risky = args.force
        tm.set(pid, value)
    return apply_and_verify(args, action, pid)


RECORD_CHANNELS = [str(n) for n in range(1, 17)] + ['st17', 'st19', '2trk']


def record_status(tm):
    p = tm.params
    val = lambda path, name: tm.config.get(p.find(path, name).index)
    armed = [ch for ch in RECORD_CHANNELS
             if val('2-Trk Rec > Control' if ch == '2trk' else '%s > Control' % strip_path(ch),
                    '2-Track Arm' if ch == '2trk' else 'Arm') not in ('0', '0.0', None)]
    print('USB drive:  %s' % ('present' if val('Global > Misc Input', 'USB present') not in ('0', None) else 'NOT present'))
    print('Transport:  %s' % transport_name(val('Global > Transport', 'Transport') or 0))
    print('Multitrack: %s' % ('on' if val('Global > Transport', 'Multi-track Enable') not in ('0', None) else 'off'))
    print('Armed:      %s' % (', '.join(armed) or 'nothing'))
    print('Busy:       %s' % val('Global > Transport', 'Busy'))


def cmd_record(args):
    """record status | arm CH... | disarm CH... | start | stop | play | rtz"""
    tm = open_mixer(args)
    try:
        if args.action == 'status':
            record_status(tm)
            return
        warn_no_access(tm)
        if args.action in ('arm', 'disarm'):
            chans = RECORD_CHANNELS if args.channels in (['all'], []) else \
                [c for part in args.channels for c in parse_channels(part)]
            for ch in chans:
                tm.arm(ch, args.action == 'arm')
            print('%s %s' % (args.action, ', '.join(chans)))
        else:
            state = {'start': 'record'}.get(args.action, args.action)
            tm.transport(state)
            print('transport -> %s' % state)
        time.sleep(0.5)
    finally:
        tm.close()
    if args.verify:
        time.sleep(0.5)
        check = open_mixer(args)
        try:
            record_status(check)
        finally:
            check.close()


def mp3_status(tm, wait=1.0):
    p = tm.params
    val = lambda path, name: tm.config.get(p.find(path, name).index)
    title_pid = p.find('Global > Control', 'Current Song').index
    time.sleep(wait)        # titles arrive as 9.1 messages shortly after connect
    mode = val('Global > Transport', 'Multi-track Enable') in ('0', '0.0')
    index = int(float(val('Global > Control', 'Mp3 playlist') or 0))
    print('Mode:       %s' % ('MP3 playback' if mode else 'multitrack'))
    print('Song:       %d  %s' % (index + 1, tm.config_text.get(title_pid, '')))
    print('Transport:  %s' % transport_name(val('Global > Transport', 'Transport') or 0))


MP3_LOAD_TIME = 1.0


def cmd_mp3(args):
    """mp3 status | mode on|off | play | stop | next | previous | top | select N"""
    tm = open_mixer(args)
    try:
        if args.action == 'status':
            mp3_status(tm)
            return
        warn_no_access(tm)
        if args.action == 'mode':
            tm.mp3_mode(on_off(args.value or 'on'))
        elif args.action == 'select' or (args.action == 'play' and args.value):
            if not args.value or not args.value.isdigit() or int(args.value) < 1:
                sys.exit('mp3 %s needs a song number, 1 = first' % args.action)
            tm.mp3_select(int(args.value) - 1)
            if args.action == 'play':
                time.sleep(MP3_LOAD_TIME)   # selecting stops the player; a play sent at once is lost
                tm.transport('play')
        else:
            tm.transport({'top': 'rtz'}.get(args.action, args.action))
        print('mp3 %s %s' % (args.action, args.value or ''))
        time.sleep(0.5)
    finally:
        tm.close()


def channel_names_from_mixer(args):
    """Channel names from the mixer, or {} (with a note) if it can't be reached."""
    try:
        host = args.host or os.environ.get('TOUCHMIX_HOST')
        if host:
            tm = TouchMix(pick_host(args), port=args.port, name=args.name)
        else:
            found = discover(args.subnet, port=args.port)
            if not found:
                raise TouchMixError('no mixer found on the network')
            tm = TouchMix(found[0].ip, port=found[0].port, name=args.name)
        tm.connect()
        try:
            tm.wait_ready()
            names = C.mixer_channel_names(tm)
        finally:
            tm.close()
        print('Channel names from %s' % tm.mixer_name)
        return names
    except (TouchMixError, OSError) as e:
        print('(mixer not reachable: %s; using In 1, In 2, ...)' % e)
        return {}


def cmd_collect(args):
    """Copy a recorded session off a TouchMix USB drive, named after the channels."""
    drives = [args.drive] if args.drive else C.find_drives()
    if not drives:
        sys.exit('No TouchMix drive found. Plug the USB drive into this PC, or pass --drive.')
    if len(drives) > 1:
        sys.exit('Several TouchMix drives found (%s); pick one with --drive.' % ', '.join(drives))
    found = C.sessions(drives[0])
    if not found:
        sys.exit('No sessions on %s.' % drives[0])
    if args.list:
        print('Sessions on %s, newest first:' % drives[0])
        for s in found:
            print('  %-24s %s  %2d tracks  %7.1f MB%s' % (
                s.name, time.strftime('%Y-%m-%d %H:%M', time.localtime(s.mtime)), len(s.tracks),
                s.size / 1e6, '' if s.has_header else '  (no header: not stopped properly?)'))
        return
    if args.session:
        match = [s for s in found if s.name.lower() == args.session.lower()]
        if not match:
            sys.exit('No session %r on %s (see collect --list).' % (args.session, drives[0]))
        session = match[0]
    else:
        session = found[0]
    if not session.tracks:
        sys.exit('Session %s has no recorded tracks (nothing was armed?).' % session.name)
    if not session.has_header:
        print('!! %s has no header file: recording may not have been stopped properly.' % session.name)
    names = {} if args.no_names else channel_names_from_mixer(args)
    C.label_tracks(session, names)
    out = os.path.join(args.out or C.default_out_dir(),
                       '%s %s' % (session.name, time.strftime('%Y-%m-%d %H%M', time.localtime(session.mtime))))
    plan = C.plan_copy(session, out)
    print('Session %s: %d tracks, %d files, %.1f MB -> %s' % (
        session.name, len(session.tracks), len(plan), session.size / 1e6, out))
    copied, skipped = C.copy_session(plan)
    print('Done: %d copied, %d already there.' % (copied, skipped))


def cmd_scene(args):
    """scene list | recall NAME"""
    tm = open_mixer(args)
    try:
        if args.action == 'list':
            for s in tm.scenes():
                print('  %s %-8s %-24s %s' % ('*' if s['current'] else ' ', s['location'], s['name'], s['uuid']))
            print('(* = last saved or recalled)')
            return
        if not args.name:
            sys.exit('scene recall needs a scene name (see scene list)')
        warn_no_access(tm)
        scene = tm.find_scene(args.name)
        print('Recalling %s scene %r ...' % (scene['location'], scene['name']))
        if tm.recall_scene(args.name, trigger=args.trigger):
            print('The mixer sent its new state: recalled.')
            return 0
        print('No answer from the mixer within %d s: the recall probably did not happen.' % 6)
        if args.trigger == 0:
            print('Try again with --trigger 1.')
        return 2
    finally:
        tm.close()


def cmd_fx(args):
    """fx N type [KIND]: show or pick FX N's processor."""
    pid = ParamTable().find('FX %d > Editor' % args.fx, 'FX type').index
    preset = ParamTable().find('FX %d > Control' % args.fx, 'Preset').index
    if args.kind is None:
        tm = open_mixer(args)
        try:
            n = int(float(tm.config.get(pid) or 0))
            print('FX %d: %s (preset %r)' % (args.fx, FX_TYPE_NAMES.get(n, n), tm.config_text.get(preset, '')))
        finally:
            tm.close()
        return
    n = fx_type_number(args.kind)
    return apply_and_verify(args, lambda tm: tm.fx_type(args.fx, n), pid)


def cmd_name(args):
    pid = ParamTable().find('%s > Control' % strip_path(args.channel), 'Name').index
    tm = open_mixer(args)
    warn_no_access(tm)
    try:
        old = tm.config_text.get(pid, '')
        tm.rename(args.channel, args.name)
        time.sleep(0.3)
    finally:
        tm.close()
    print('renamed %s: %r -> %r' % (strip_path(args.channel), old, args.name))
    if args.verify:
        time.sleep(0.5)
        check = open_mixer(args)
        now = check.config_text.get(pid)
        check.close()
        print('mixer now says %r: %s' % (now, 'CHANGED' if now == args.name else 'unchanged'))
        return 0 if now == args.name else 2


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


def cmd_map_tour(args):
    """Walk through actions on the mixer's own screen, logging everything it reports."""
    wanted = [s.strip() for s in args.sections.split(',')] if args.sections else list(M.TOUR)
    unknown = [s for s in wanted if s not in M.TOUR]
    if unknown:
        sys.exit('unknown section %s (choose from %s)' % (', '.join(unknown), ', '.join(M.TOUR)))
    steps = [(sec, tuple(s) + (None,) * (3 - len(s))) for sec in wanted for s in M.TOUR[sec]]
    path = args.out or 'tour-%s-%s.log' % ('-'.join(wanted) if args.sections else 'all', time.strftime('%Y%m%d-%H%M%S'))
    host = pick_host(args)
    polls = 'meters' in wanted
    tm = TouchMix(host, port=args.port, name=args.name)
    out = make_writer(path, echo=False)
    attach_logger(tm, out, status=True, raw=True, polls=polls)
    tm.connect()
    tm.wait_ready()

    def read_back(watch, tag):
        """Reconnect (the mixer only sends its state on connect) and log the watched values."""
        nonlocal tm
        port = tm.port
        tm.close()
        time.sleep(0.3)
        tm = TouchMix(host, port=port, name=args.name)
        tm.connect()
        tm.wait_ready()
        attach_logger(tm, out, status=True, raw=True, polls=polls)
        for param in watch:
            pid = tm.resolve(param)
            out('%s READ %s 0x%04x %s value=%s text=%r' % (time.strftime('%H:%M:%S'), tag, pid, param,
                                                         tm.config.get(pid), tm.config_text.get(pid, '')))
    out('%s CONNECTED %s' % (time.strftime('%H:%M:%S'), tm.mixer_name))
    print(M.TOUR_INTRO)
    print('Logging to %s. Ctrl+C stops at any point (what you did so far is saved).\n' % path)
    section = None
    try:
        for i, (sec, (prompt, repeat, watch)) in enumerate(steps, 1):
            if sec != section:
                section = sec
                print('\n== %s ==' % sec)
                out('%s SECTION %s' % (time.strftime('%H:%M:%S'), sec))
            out('%s STEP %d %s' % (time.strftime('%H:%M:%S'), i, prompt))
            print('\n%d/%d  %s' % (i, len(steps), prompt))
            if repeat:
                print('     Do it once, type what the screen shows now (e.g. the mode name), Enter.')
                print('     Repeat for every option. Empty Enter when you are back at the first one.')
                n = 0
                while True:
                    label = input('     shows> ').strip()
                    if not label:
                        break
                    n += 1
                    out('%s LABEL %d.%d %s' % (time.strftime('%H:%M:%S'), i, n, label))
                    if watch:
                        read_back(watch, '%d.%d' % (i, n))
            else:
                note = input('     Enter when done (or type a note first) > ').strip()
                if note:
                    out('%s LABEL %d %s' % (time.strftime('%H:%M:%S'), i, note))
                if watch:
                    read_back(watch, str(i))
            time.sleep(0.3)             # let the last messages for this step arrive
            out('%s END %d' % (time.strftime('%H:%M:%S'), i))
            if not tm.connected:
                print('!! lost the connection: %s' % tm.error)
                break
        print('\nAll done.')
    except (KeyboardInterrupt, EOFError):
        print('\nStopped.')
    finally:
        tm.close()
        out.close()
        print('Saved %s. Tell Claude when it is there.' % path)


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
    m.add_argument('--raw', action='store_true', help='also print unknown message types and file transfers')
    m.add_argument('--polls', action='store_true', help='also print poll replies (meters?) whenever they change')
    m.add_argument('--log', help='write everything to this file, and let you type notes between actions')
    m.add_argument('--quiet', action='store_true', help='with --log: write only to the file, not the screen')
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
    ctl('eq', 'EQ band gain (dB), freq (Hz), or shelf on/off (bands 1 and 4)', cmd_eq,
        ('band', dict(type=int, choices=[1, 2, 3, 4, 5, 6], help='1-4 on inputs, 1-6 on main/aux')),
        ('what', dict(choices=['gain', 'freq', 'shelf'])),
        ('value', dict(help='dB, Hz, or on/off for shelf')))

    s = sub.add_parser('set', help='set any parameter by index or "Path > Name" to a raw 0..1 value')
    s.add_argument('param')
    s.add_argument('value', help='normalized float, or int:N for integer parameters')
    s.add_argument('--verify', action='store_true')
    s.add_argument('--force', action='store_true',
                   help='allow network, security, Powerdown and Delete parameters (can lock you out)')
    s.set_defaults(fn=cmd_set)

    rec = sub.add_parser('record', help='USB recorder: status, arm/disarm tracks, start/stop/play/rtz')
    rec.add_argument('action', choices=['status', 'arm', 'disarm', 'start', 'stop', 'play', 'rtz', 'next', 'previous'])
    rec.add_argument('channels', nargs='*', help='for arm/disarm: 1-16, st17, st19, 2trk, ranges like 1-8, or all')
    rec.add_argument('--verify', action='store_true', help='reconnect and show the recorder status afterwards')
    rec.set_defaults(fn=cmd_record)

    mp = sub.add_parser('mp3', help='USB MP3 player: status, mode on/off, play, stop, next, previous, top, select N')
    mp.add_argument('action', choices=['status', 'mode', 'play', 'stop', 'next', 'previous', 'top', 'select'])
    mp.add_argument('value', nargs='?', help='mode: on/off; select or play: song number, 1 = first')
    mp.set_defaults(fn=cmd_mp3)

    co = sub.add_parser('collect', help='copy the newest recording off the USB drive, one file per channel')
    co.add_argument('--drive', help='drive or folder holding the sessions (default: find it)')
    co.add_argument('--session', help='session name (default: the newest)')
    co.add_argument('--list', action='store_true', help='list the sessions on the drive and stop')
    co.add_argument('--out', help='where to put it (default: ~/Music/TouchMix)')
    co.add_argument('--no-names', action='store_true', help="don't ask the mixer for channel names")
    co.set_defaults(fn=cmd_collect)

    sc = sub.add_parser('scene', help='list scenes, or recall one: scene recall TEST')
    sc.add_argument('action', choices=['list', 'recall'])
    sc.add_argument('name', nargs='?', help='scene name, file name or UUID')
    sc.add_argument('--trigger', type=float, default=0.0, help='value written to FileMan > Recall (default 0)')
    sc.set_defaults(fn=cmd_scene)

    fx = sub.add_parser('fx', help='show or pick an FX processor: fx 1 type lush')
    fx.add_argument('fx', type=int, choices=[1, 2, 3, 4])
    fx.add_argument('what', choices=['type'])
    fx.add_argument('kind', nargs='?', help='none, chorus, delay, pitch, dense, lush, mono-delay (omit to show)')
    fx.add_argument('--verify', action='store_true', help='reconnect and check the mixer stored it')
    fx.set_defaults(fn=cmd_fx)

    nm = sub.add_parser('name', help='rename a channel, e.g. name 16 Kick')
    nm.add_argument('channel', help='1-16, st17, st19, main, aux1-10, fx1-4, dca1-8')
    nm.add_argument('name', help='the new name (ASCII, keep it short)')
    nm.add_argument('--verify', action='store_true', help='reconnect and check the mixer stored it')
    nm.set_defaults(fn=cmd_name)

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
    tr = msub.add_parser('tour', help='walk through actions on the mixer screen, logging what it reports')
    tr.add_argument('sections', nargs='?', help='comma-separated: %s (default: all)' % ', '.join(M.TOUR))
    tr.add_argument('--out', help='log file (default tour-<sections>-<time>.log)')
    tr.set_defaults(fn=cmd_map_tour)
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
