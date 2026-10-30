#!/usr/bin/env python3
"""
check_app/salah_times.py — smoke-test the prayer-times (namaz vakitleri) feature.

Covers the three moving parts that cannot be eyeballed:

  1. data/namaz/            the 8 528-location database decoded from the
                            reference APK by tools/extract_namaz.py
  2. salah_times.py               the sun maths (Diyanet angles + temkin), qibla,
                            config, schedule — pinned to 792 published Diyanet
                            days (İstanbul + Ankara, 396 each: every time is
                            within 1 min, ~97 % to the exact minute), plus
                            polar-day cases
  3. daemon.py           the "which reminders are due" selection the
                            daemon loops on

Exit code 0 = everything consistent. Run before committing:

    python3 tools/check_namaz.py
"""
import gzip
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT))

import salah_times              # noqa: E402
import daemon as qt_namazd      # noqa: E402

errors = []
checks = 0


def check(cond, label):
    global checks
    checks += 1
    if not cond:
        errors.append(label)
        print(f'  FAIL  {label}')
    return cond


def check_eq(got, want, label):
    return check(got == want, f'{label} — got {got!r}, want {want!r}')


# ---------------------------------------------------------------- the data

def test_database():
    print('· data/namaz/')
    idx_path = ROOT / 'data' / 'namaz' / 'index.json'
    tree_path = ROOT / 'data' / 'namaz' / 'tree.json.gz'
    if not check(idx_path.exists() and tree_path.exists(),
                 'data/namaz/{index.json,tree.json.gz} present '
                 '(run: python3 tools/extract_app/salah_times.py <apk-assets> data/namaz)'):
        return
    idx = json.loads(idx_path.read_text(encoding='utf-8'))
    check_eq(idx['records'], 8528, 'check_bytes.and records')
    check_eq(idx['leaves'], 8528, 'leaves emitted')
    check_eq(idx['countries'], 210, 'countries parsed')
    check_eq(idx['max_depth'], 2, 'tree depth (country → region → leaf)')

    tree = json.load(gzip.open(tree_path, 'rt', encoding='utf-8'))
    check_eq(len(tree), 209, 'country nodes in tree')

    leaves = both_names = 0
    depth = 0

    def walk(nodes, d=0):
        nonlocal leaves, both_names, depth
        depth = max(depth, d)
        for n in nodes:
            check('t' in n and n['t'], 'node has a name')
            if 'c' in n:
                walk(n['c'], d + 1)
            else:
                leaves += 1
                if n.get('e') and n['e'] != n['t']:
                    both_names += 1
                ok = (-90.0 <= n.get('lat', 999) <= 90.0
                      and -180.0 <= n.get('lon', 999) <= 180.0)
                if not ok:
                    check(False, f'lat/lon in range for {n.get("t")!r}')
                    return

    walk(tree)
    check_eq(leaves, 8528, 'selectable leaves')
    check_eq(depth, 2, 'max depth')
    check(both_names > 0, 'some nodes have distinct tr/en names')

    # spot-check the Türkiye → İstanbul branch (city centre + ilçeler)
    tr = next((c for c in tree if c['cc'] == 'TR'), None)
    check(tr is not None, 'Türkiye country node exists')
    if tr:
        ist = next((r for r in tr['c'] if r['t'] == 'İSTANBUL'), None)
        check(ist is not None, 'İSTANBUL region node exists')
        if ist:
            check(len(ist['c']) == 19,
                  f'İstanbul has 19 selectable places (got {len(ist["c"])})')
            centre = next((x for x in ist['c'] if x['t'] == 'İSTANBUL'), None)
            check(centre is not None and abs(centre['lat'] - 41.01) < 0.01,
                  'İstanbul centre carries its coordinates')


# ----------------------------------------------------------- the sun maths

# The engine is pinned against *published* Diyanet times: every row in
# tools/data/diyanet_reference.json is one calendar day from
# namazvakitleri.diyanet.gov.tr (İstanbul + Ankara, 396 days each, fetched
# 2026-10-19). Their calendar evaluates the sun at 0h UT and prints the
# astronomical value plus the classical temkin (güneş −7, öğle +5, ikindi +4,
# akşam +7 min) — those two details are what make the two agree.
REFERENCE = ROOT / 'tools' / 'data' / 'diyanet_reference.json'

# Exact-minute agreement over those 396 days per place. Pinned on purpose: if
# a maths change moves the sub-minute agreement, this fails and the change
# must be looked at deliberately (the missed days are values sitting within
# seconds of a minute boundary, where Diyanet's own rounding decides).
EXACT_PINNED = {
    'İstanbul': {'imsak': 390, 'gunes': 388, 'ogle': 378,
                 'ikindi': 393, 'aksam': 388, 'yatsi': 391},
    'Ankara':   {'imsak': 380, 'gunes': 372, 'ogle': 386,
                 'ikindi': 376, 'aksam': 380, 'yatsi': 366},
}


def _minutes(hhmm):
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


def test_times():
    print('· prayer times vs the published Diyanet calendar')
    if not check(REFERENCE.exists(), 'tools/data/diyanet_reference.json present'):
        return
    ref = json.loads(REFERENCE.read_text(encoding='utf-8'))
    for place in ref['places']:
        name = place['name']
        days = place['days']
        check_eq(len(days), 396, f'{name}: 396 published days loaded')
        exact = {k: 0 for k in salah_times.PRAYER_KEYS}
        worst = {k: 0 for k in salah_times.PRAYER_KEYS}
        unordered = 0
        for day, want in days.items():
            y, mo, d = map(int, day.split('-'))
            t = salah_times.prayer_times(place['lat'], place['lon'], y, mo, d, place['tz'])
            vals = [t[k] for k in salah_times.PRAYER_KEYS if t[k] is not None]
            if not all(a < b for a, b in zip(vals, vals[1:])):
                unordered += 1
            for i, k in enumerate(salah_times.PRAYER_KEYS):
                diff = abs(_minutes(salah_times.format_time(t[k])) - _minutes(want[i]))
                exact[k] += diff == 0
                worst[k] = max(worst[k], diff)
        check_eq(unordered, 0, f'{name}: six times strictly ordered every day')
        for k in salah_times.PRAYER_KEYS:
            check(worst[k] <= 1, f'{name} {k}: within 1 min of the published '
                                 f'time (worst {worst[k]} min)')
            check_eq(exact[k], EXACT_PINNED[name][k],
                     f'{name} {k}: exact-minute agreement')

    # the temkin belongs to the diyanet method only
    tuned = salah_times.prayer_times(41.010, 28.965, 2026, 10, 19, 3.0)
    plain = salah_times.prayer_times(41.010, 28.965, 2026, 10, 19, 3.0, method='mwl')
    for k, delta in (('imsak', 0), ('gunes', -7), ('ogle', 5),
                     ('ikindi', 4), ('aksam', 7), ('yatsi', 0)):
        got = (tuned[k] - plain[k]) * 60.0
        check(abs(got - delta) < 1e-9,
              f'{k}: temkin {delta:+d} min on diyanet vs mwl (got {got:+.3f} min)')


def test_polar():
    print('· polar latitudes (undefined prayers must be None, not garbage)')
    t = salah_times.prayer_times(69.65, 18.96, 2026, 6, 21, 2.0)      # midnight sun
    check_eq(t['imsak'], None, 'Tromsø Jun: imsak undefined')
    check_eq(t['yatsi'], None, 'Tromsø Jun: yatsı undefined')
    check_eq(t['gunes'], None, 'Tromsø Jun: güneş undefined')
    check(t['ogle'] is not None, 'Tromsø Jun: öğle still defined')

    t = salah_times.prayer_times(69.65, 18.96, 2026, 12, 21, 1.0)     # polar night
    check_eq(t['gunes'], None, 'Tromsø Dec: güneş undefined')
    check_eq(t['aksam'], None, 'Tromsø Dec: akşam undefined')
    check_eq(t['ikindi'], None, 'Tromsø Dec: ikindi undefined (sun never up)')
    check(t['imsak'] is not None and t['yatsi'] is not None,
          'Tromsø Dec: imsak/yatsı still defined')


def test_qibla():
    print('· qibla')
    for name, lat, lon, want in [
        ('İstanbul', 41.010, 28.965, 151.59),
        ('London', 51.507, -0.128, 118.99),
        ('New York', 40.71, -74.006, 58.48),
    ]:
        got = salah_times.qibla(lat, lon)
        check(abs(got - want) < 0.1, f'{name} qibla ≈ {want}° (got {got:.2f}°)')


# -------------------------------------------------------- config & schedule

def test_config_roundtrip():
    print('· config')
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / 'salah_times.json'
        cfg = salah_times.load_config(p)                     # defaults when missing
        check(cfg['location'] is None, 'default has no location')
        check_eq(sorted(cfg['notify']), sorted(salah_times.PRAYER_KEYS),
                 'default notify table covers all six prayers')
        check(all(isinstance(v, list) for v in cfg['notify'].values()),
              'every prayer holds a *list* of reminders')
        check_eq(cfg['voice_name'], '', 'default voice name is empty (engine default)')

        cfg['location'] = {'t': 'İSTANBUL', 'e': 'ISTANBUL', 'cc': 'TR',
                           'lat': 41.01, 'lon': 28.965}
        cfg['notify']['ikindi'] = [{'on': True, 'when': 'before', 'minutes': 10},
                                   {'on': True, 'when': 'before', 'minutes': 30},
                                   {'on': True, 'when': 'ontime', 'minutes': 0}]
        cfg['voice_name'] = 'Turkish+Adam'
        salah_times.save_config(cfg, p)
        back = salah_times.load_config(p)
        check_eq(back['location']['t'], 'İSTANBUL', 'location round-trips')
        check_eq([(s['when'], s['minutes']) for s in back['notify']['ikindi']],
                 [('before', 10), ('before', 30), ('ontime', 0)],
                 'three reminders on one prayer round-trip')
        check_eq(back['voice_name'], 'Turkish+Adam', 'voice name round-trips')

        # a config written before multiple reminders existed still loads
        legacy = {'notify': {'ikindi': {'on': True, 'when': 'before', 'minutes': 10}}}
        old = Path(td) / 'legacy.json'
        old.write_text(json.dumps(legacy), encoding='utf-8')
        got = salah_times.load_config(old)
        check_eq(got['notify']['ikindi'],
                 [{'on': True, 'when': 'before', 'minutes': 10}],
                 'legacy single-dict reminder becomes a one-item list')
        check_eq(len(got['notify']['imsak']), 1,
                 'prayers absent from the file keep their defaults')

    dirty = {'location': {'t': 'x' * 500, 'lat': 'not-a-number', 'lon': 5},
             'method': 'nonsense', 'voice': 'true', 'voice_name': 'v' * 500,
             'notify': {'imsak': [{'on': 1, 'when': 'before', 'minutes': 10 ** 6},
                                  {'on': True, 'when': 'before', 'minutes': 10 ** 6},
                                  {'on': True, 'when': 'ontime', 'minutes': 0}],
                        'ivan': [{'on': True}]}}
    clean = salah_times.sanitize_config(dirty)
    check(clean['location'] is None, 'sanitize drops a bogus location')
    check_eq(clean['method'], 'diyanet', 'sanitize rejects an unknown method')
    check_eq(clean['voice_name'], 'v' * 80, 'sanitize truncates the voice name')
    check_eq([s['minutes'] for s in clean['notify']['imsak']], [180, 0],
             'sanitize clamps minutes and collapses duplicate lead times')
    check('ivan' not in clean['notify'], 'sanitize drops unknown prayers')

    # the number of reminders per prayer is bounded
    many = salah_times.normalize_notify({'imsak': [{'on': True, 'when': 'before',
                                             'minutes': m} for m in range(1, 40)]})
    check_eq(len(many['imsak']), salah_times.MAX_REMINDERS,
             f'at most {salah_times.MAX_REMINDERS} reminders per prayer')


def test_schedule_and_due():
    print('· schedule + daemon due-reminders')
    cfg = salah_times.default_config()
    cfg['location'] = {'t': 'İSTANBUL', 'e': 'ISTANBUL', 'cc': 'TR',
                       'lat': 41.01, 'lon': 28.965}
    cfg['timezone'] = 'Europe/Istanbul'
    for key in salah_times.PRAYER_KEYS:                      # every prayer on, once
        cfg['notify'][key] = [{'on': True, 'when': 'ontime', 'minutes': 0}]

    sched = salah_times.build_schedule(cfg, hours=48)
    check_eq(len(sched), 12, '48 h × 6 prayers = 12 reminders')
    check(all(isinstance(x[0], datetime) for x in sched), 'schedule rows are datetimes')
    check(sched == sorted(sched), 'schedule is time-sorted')

    # several reminders for one prayer all reach the schedule, in order
    cfg['notify']['ikindi'] = [{'on': True, 'when': 'before', 'minutes': 30},
                               {'on': True, 'when': 'before', 'minutes': 10},
                               {'on': True, 'when': 'ontime', 'minutes': 0},
                               {'on': False, 'when': 'before', 'minutes': 5}]
    sched = salah_times.build_schedule(cfg, hours=24)
    ikindi = [(f, b) for f, k, b in sched if k == 'ikindi']
    check_eq(len(ikindi), 3, 'a prayer with 3 enabled reminders yields 3 rows (off one skipped)')
    check_eq([b for _, b in ikindi], [30, 10, 0], 'its reminders fire 30, 10 and 0 min before')
    check(all(a[0] < b[0] for a, b in zip(ikindi, ikindi[1:])),
          'its reminders are strictly ordered in time')
    # an empty list means the prayer is off
    cfg['notify']['aksam'] = []
    check(not [k for _, k, _ in salah_times.build_schedule(cfg, hours=24) if k == 'aksam'],
          'an empty reminder list switches the prayer off')

    now = datetime.now().replace(microsecond=0)
    due_in = now - timedelta(seconds=20)
    schedule = [(due_in, 'aksam', 0),
                (now + timedelta(hours=3), 'yatsi', 0)]
    fired = set()
    due = qt_namazd.due_reminders(schedule, now, fired)
    check_eq(len(due), 1, 'only the elapsed reminder is due')
    check_eq(due[0][1], 'aksam', 'the due one is akşam')
    fired.add(qt_namazd._ident(due_in, 'aksam', 0))
    check_eq(qt_namazd.due_reminders(schedule, now, fired), [],
             'a fired reminder is not repeated')
    future = [(now + timedelta(minutes=5), 'ogle', 0)]
    check_eq(qt_namazd.due_reminders(future, now, set()), [],
             'future reminders are not due yet')
    stale = [(now - timedelta(seconds=qt_namazd.GRACE_SECONDS + 5), 'imsak', 0)]
    check_eq(qt_namazd.due_reminders(stale, now, set()), [],
             'reminders older than the grace window are skipped')

    # two reminders of the SAME prayer are distinct to the daemon
    a = (due_in, 'ikindi', 30)
    b = (due_in, 'ikindi', 10)
    due = qt_namazd.due_reminders([a, b], now, set())
    check_eq(len(due), 2, 'two reminders of one prayer both fire')
    fired = {qt_namazd._ident(*a)}
    check_eq([x[2] for x in qt_namazd.due_reminders([a, b], now, fired)], [10],
             'firing the 30-min one leaves the 10-min one pending')


def test_voice():
    print('· voice configuration')
    cfg = salah_times.sanitize_config({'voice': True, 'voice_engine': 'espeak-ng',
                                 'voice_lang': 'tr', 'voice_name': 'turkish'})
    check_eq(cfg['voice_engine'], 'espeak-ng', 'a known engine is kept')
    check_eq(salah_times.sanitize_config({'voice_engine': 'say-it'})['voice_engine'],
             'spd-say', 'an unknown engine falls back to spd-say')
    # listing must not raise even where an engine is missing
    voices = salah_times.list_voices()
    check(isinstance(voices, list), 'list_voices() returns a list')
    check(all({'engine', 'name', 'lang', 'label'} <= set(v) for v in voices),
          'every listed voice carries engine/name/lang/label')
    if voices:
        check(len({(v['engine'], v['name']) for v in voices}) == len(voices),
              'listed voices are unique per engine')
    tr = salah_times.list_voices(lang='tr')
    check(all(v['lang'] == 'tr' for v in tr), 'the lang filter is honoured')
    # the wake-up prefix burns the audio device's ~1-2 s wake-up time so the
    # actual reminder is never swallowed
    check_eq(salah_times.with_wake_prefix('İkindi vakti girdi'),
             salah_times.SPEECH_WAKE_PREFIX + 'İkindi vakti girdi',
             'spoken text is prefixed with the wake-up phrase')
    check_eq(salah_times.with_wake_prefix(salah_times.with_wake_prefix('x')),
             salah_times.SPEECH_WAKE_PREFIX + 'x',
             'the wake-up prefix is idempotent')


def main():
    print('checking the namaz feature…')
    test_database()
    test_times()
    test_polar()
    test_qibla()
    test_config_roundtrip()
    test_voice()
    test_schedule_and_due()
    print()
    if errors:
        print(f'{len(errors)} FAILED of {checks} checks')
        return 1
    print(f'OK — {checks} checks passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
