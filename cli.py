#!/usr/bin/env python3
"""
salah-times — command-line interface to the Quran Tools prayer-time notifier.

    salah-times times                today's six prayer times (uses the config'd location)
    salah-times times --lat 41.01 --lon 28.96 [--tz Europe/Istanbul]
    salah-times qibla [--lat X --lon Y]
    salah-times next                 next reminder + countdown
    salah-times config               print the current config (JSON)
    salah-times set-location "İSTANBUL" 41.01 28.965 TR
    salah-times set-notify ikindi add 10        add a reminder 10 min before
    salah-times set-notify ikindi add 30        …and one 30 min before (both fire)
    salah-times set-notify ikindi add           …and the on-time reminder
    salah-times set-notify ikindi remove 30     drop the 30-min one
    salah-times set-notify ikindi set 5         replace them all with a 5-min one
    salah-times set-notify ikindi clear         remove them all
    salah-times voices [--lang tr]   list the synthesis voices this machine has
    salah-times set-voice on --lang tr --name 'Turkish+Adam'
    salah-times test-notify          fire a test popup (+ voice if enabled)
    salah-times daemon               run the scheduler loop (systemd ExecStart)
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'app'))

import salah_times  # noqa: E402


def _config_or_die():
    cfg = salah_times.load_config()
    if not (cfg.get('location') or {}).get('lat'):
        print('error: no location configured — set it in the #/namaz view or '
              '`salah-times set-location "İSTANBUL" 41.01 28.965 TR`', file=sys.stderr)
        sys.exit(1)
    return cfg


def cmd_times(args):
    cfg = salah_times.load_config()
    lat = args.lat if args.lat is not None else (cfg.get('location') or {}).get('lat')
    lon = args.lon if args.lon is not None else (cfg.get('location') or {}).get('lon')
    if lat is None or lon is None:
        _config_or_die()
    tz = args.tz or cfg.get('timezone') or salah_times.default_tz_name()
    t = salah_times.today_times(lat, lon, tz)
    for key in salah_times.PRAYER_KEYS:
        print(f'{salah_times.PRAYER_TR[key]:8} {t[key]}')
    print(f'qibla {salah_times.qibla(lat, lon):.1f}°')


def cmd_qibla(args):
    cfg = salah_times.load_config()
    lat = args.lat if args.lat is not None else (cfg.get('location') or {}).get('lat')
    lon = args.lon if args.lon is not None else (cfg.get('location') or {}).get('lon')
    if lat is None or lon is None:
        _config_or_die()
    print(f'{salah_times.qibla(lat, lon):.1f}°')


def cmd_next(args):
    from datetime import datetime, timedelta
    cfg = _config_or_die()
    sched = salah_times.build_schedule(cfg)
    future = [s for s in sched if s[0] > datetime.now()]
    if not future:
        print('no upcoming reminders')
        return
    fire, key, before = future[0]
    delta = fire - datetime.now()
    mins = int(delta.total_seconds() // 60)
    if before:
        pt = fire + timedelta(minutes=before)
        print(f'next: {salah_times.PRAYER_TR[key]} {pt:%H:%M}  →  reminder {fire:%H:%M} '
              f'(−{before} min, in {mins} min)')
    else:
        print(f'next: {salah_times.PRAYER_TR[key]} {fire:%H:%M}  (in {mins} min)')
    print(f'      "{salah_times._prayer_message(key, before)}"')


def cmd_config(args):
    print(json.dumps(salah_times.load_config(), ensure_ascii=False, indent=2))


def cmd_set_location(args):
    cfg = salah_times.load_config()
    cfg['location'] = {'t': args.name, 'e': args.name, 'cc': args.cc,
                       'lat': args.lat, 'lon': args.lon}
    salah_times.save_config(cfg)
    print(f"location set to {args.name} ({args.lat}, {args.lon})")


def cmd_set_notify(args):
    """Add / remove / replace the reminders of one prayer."""
    cfg = salah_times.load_config()
    if args.key not in cfg['notify']:
        print(f'error: unknown prayer key {args.key!r} '
              f'(one of {", ".join(salah_times.PRAYER_KEYS)})', file=sys.stderr)
        sys.exit(1)
    key = args.key
    # legacy single-dict configs load as lists already, but be defensive
    items = salah_times.normalize_notify(cfg['notify'])[key]
    if args.action == 'clear':
        items = []
    elif args.action == 'remove':
        before = len(items)
        items = [s for s in items
                 if (0 if s['when'] != 'before' else s['minutes']) != args.minutes]
        if len(items) == before:
            print(f'no reminder {args.minutes} min before {salah_times.PRAYER_TR[key]}',
                  file=sys.stderr)
            sys.exit(1)
    else:                                   # add | set
        entry = {'on': True,
                 'when': 'before' if args.minutes else 'ontime',
                 'minutes': args.minutes}
        if args.action == 'set':
            items = [entry]
        else:
            items = [s for s in items if (0 if s['when'] != 'before'
                                          else s['minutes']) != args.minutes]
            items.append(entry)
    cfg['notify'][key] = salah_times.normalize_notify({key: items})[key]
    salah_times.save_config(cfg)
    shown = ', '.join(f"−{s['minutes']} dk" if s['when'] == 'before' else 'vakti'
                      for s in cfg['notify'][key] if s['on']) or 'kapalı (off)'
    print(f"{salah_times.PRAYER_TR[key]}: {shown}")


def cmd_voices(args):
    voices = salah_times.list_voices(args.engine, args.lang)
    if not voices:
        print('no synthesis voices found — install speech-dispatcher (spd-say) '
              'or espeak-ng', file=sys.stderr)
        sys.exit(1)
    for v in voices:
        print(f"{v['engine']:9} {v['lang']:5} {v['name']}")
    print(f"\n{len(voices)} voice(s). Use one with: "
          f"salah-times set-voice on --name '<name>'")


def cmd_set_voice(args):
    cfg = salah_times.load_config()
    if args.state is not None:
        cfg['voice'] = args.state == 'on'
    if args.engine is not None:
        cfg['voice_engine'] = args.engine
    if args.lang is not None:
        cfg['voice_lang'] = args.lang
    if args.name is not None:
        cfg['voice_name'] = '' if args.name in ('', 'default') else args.name
    salah_times.save_config(cfg)
    print(f"voice {'on' if cfg['voice'] else 'off'} · engine {cfg['voice_engine']} "
          f"· lang {cfg['voice_lang']} · name {cfg['voice_name'] or '(default)'}")


def cmd_test_notify(args):
    cfg = salah_times.load_config()
    loc = cfg.get('location') or {}
    name = (loc.get('t') or 'Prayer time')
    salah_times.send_notification(f'{name} — test', 'Quran Tools bildirimi çalışıyor.')
    if cfg.get('voice'):
        salah_times.speak('Namaz vakitleri bildirimi testi.',
                    cfg.get('voice_engine'), cfg.get('voice_lang'),
                    cfg.get('voice_name'))
    print('test notification sent')


def main():
    ap = argparse.ArgumentParser(description='Quran Tools prayer-time CLI')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('times')
    p.add_argument('--lat', type=float)
    p.add_argument('--lon', type=float)
    p.add_argument('--tz')

    p = sub.add_parser('qibla')
    p.add_argument('--lat', type=float)
    p.add_argument('--lon', type=float)

    sub.add_parser('next')
    sub.add_parser('config')

    p = sub.add_parser('set-location')
    p.add_argument('name')
    p.add_argument('lat', type=float)
    p.add_argument('lon', type=float)
    p.add_argument('cc', default='TR', nargs='?')

    p = sub.add_parser('set-notify', help='add/remove/set the reminders of a prayer')
    p.add_argument('key')
    p.add_argument('action', choices=['add', 'remove', 'set', 'clear'],
                   nargs='?', default='add')
    p.add_argument('minutes', type=int, nargs='?', default=0,
                   help='minutes before the prayer (0 = on time)')

    p = sub.add_parser('voices', help='list the synthesis voices available here')
    p.add_argument('--engine', choices=salah_times.VOICE_ENGINES)
    p.add_argument('--lang')

    p = sub.add_parser('set-voice', help='turn voice on/off and choose the voice')
    p.add_argument('state', choices=['on', 'off'], nargs='?')
    p.add_argument('--engine', choices=salah_times.VOICE_ENGINES)
    p.add_argument('--lang')
    p.add_argument('--name', help="exact voice (see `salah-times voices`); 'default' clears it")

    sub.add_parser('test-notify')
    sub.add_parser('daemon')

    args = ap.parse_args()

    if args.cmd == 'times':
        cmd_times(args)
    elif args.cmd == 'qibla':
        cmd_qibla(args)
    elif args.cmd == 'next':
        cmd_next(args)
    elif args.cmd == 'config':
        cmd_config(args)
    elif args.cmd == 'set-location':
        cmd_set_location(args)
    elif args.cmd == 'set-notify':
        cmd_set_notify(args)
    elif args.cmd == 'voices':
        cmd_voices(args)
    elif args.cmd == 'set-voice':
        cmd_set_voice(args)
    elif args.cmd == 'test-notify':
        cmd_test_notify(args)
    elif args.cmd == 'daemon':
        from qt_namazd import main as daemon_main  # deferred import
        sys.exit(daemon_main())
    else:
        ap.print_help()


if __name__ == '__main__':
    main()
