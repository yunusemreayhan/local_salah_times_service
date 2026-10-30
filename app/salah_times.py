#!/usr/bin/env python3
"""
salah_times.py — prayer times, qibla direction, and the notifier config.

This is the single source of truth for the Salah feature. It is pure stdlib
(no third-party deps) so it runs unchanged in the daemon, the CLI, and the
serve.py config API.

  * Prayer times — the sun's declination and equation of time at 0h UT
    (the convention the Diyanet calendar uses), turned into the six times:
        imsak (fajr)   sun at −18°        güneş (sunrise) −0.833°
        öğle  (dhuhr)  solar noon         ikindi (asr)     shadow ×1
        akşam (maghrib)−0.833°            yatsı (isha)     −17°
    The `diyanet` method then adds the classical temkin — the safety margins
    the published Turkish calendar carries on top of the astronomical values:
    güneş −7 min, öğle +5, ikindi +4, akşam +7 (imsak/yatsı: none). Both
    details were verified against namazvakitleri.diyanet.gov.tr over 792
    published days — İstanbul + Ankara (tools/check_namaz.py). Every angle
    and temkin entry is configurable per method.

  * Qibla — great-circle bearing from the location to the Kaaba.

  * Config — the notifier configuration file (~/.config/salah-times/config.json),
    shared by the web UI, `salah-times` (CLI) and the `salah-times-notifier` daemon.
    Each prayer carries a *list* of reminders (several lead times plus on-time
    can fire for one prayer), and the voice is chosen by engine, language and
    an optional exact voice name (`qt-namaz voices` lists them).
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# prayer-time mathematics
# ---------------------------------------------------------------------------

# The 6 canonical prayers, in day order. Keys are stable ids; labels are the
# Turkish names the reference app uses (namaz_vakit_dialog_*).
PRAYERS = [
    {'key': 'imsak',  'tr': 'İmsak',   'en': 'Fajr'},
    {'key': 'gunes',  'tr': 'Güneş',   'en': 'Sunrise'},
    {'key': 'ogle',   'tr': 'Öğle',    'en': 'Dhuhr'},
    {'key': 'ikindi', 'tr': 'İkindi',  'en': 'Asr'},
    {'key': 'aksam',  'tr': 'Akşam',   'en': 'Maghrib'},
    {'key': 'yatsi',  'tr': 'Yatsı',   'en': 'Isha'},
]
PRAYER_KEYS = [p['key'] for p in PRAYERS]
PRAYER_TR = {p['key']: p['tr'] for p in PRAYERS}
PRAYER_EN = {p['key']: p['en'] for p in PRAYERS}

# How many separate reminders one prayer may carry (e.g. 30 / 10 min before +
# on time). Bounded so a hand-written or POSTed config cannot flood the
# schedule.
MAX_REMINDERS = 8

# The speech back-ends `speak()` knows how to drive.
VOICE_ENGINES = ('spd-say', 'espeak-ng', 'espeak')

# Every spoken reminder starts with this wake-up phrase. An audio sink that has
# been idle for a while takes ~1-2 s to open, and without the prefix the first
# seconds of the actual message get swallowed. The phrase burns that wake-up
# time on itself.
SPEECH_WAKE_PREFIX = 'Attention, attention. '

# Calculation method presets (angles in degrees *below* the horizon; asr is the
# shadow factor: 1 = Shafi'i/standard, 2 = Hanafi; temkin = minutes added to
# the astronomical time — the Diyanet calendar's published safety margins:
# güneş is printed early so the night prayer ends before real sunrise, while
# öğle/ikindi/akşam are printed late so each prayer starts safely after the
# event).
METHODS = {
    'diyanet': {'fajr': 18.0, 'isha': 17.0, 'asr': 1, 'sunrise': 0.833,
                'temkin': {'imsak': 0, 'gunes': -7, 'ogle': 5,
                           'ikindi': 4, 'aksam': 7, 'yatsi': 0}},
    'mwl':     {'fajr': 18.0, 'isha': 17.0, 'asr': 1, 'sunrise': 0.833},
    'isna':    {'fajr': 15.0, 'isha': 15.0, 'asr': 1, 'sunrise': 0.833},
    'hanafi':  {'fajr': 18.0, 'isha': 17.0, 'asr': 2, 'sunrise': 0.833},
}
DEFAULT_METHOD = 'diyanet'

KAABA = (21.422487, 39.826206)  # lat, lon of the Kaaba


def _sin(x): return math.sin(math.radians(x))
def _cos(x): return math.cos(math.radians(x))
def _tan(x): return math.tan(math.radians(x))
def _arcsin(x): return math.degrees(math.asin(x))
def _arccos(x): return math.degrees(math.acos(x))
def _arctan2(y, x): return math.degrees(math.atan2(y, x))
def _arccot(x): return math.degrees(math.atan(1.0 / x))


def _fix_angle(a):
    a = a - 360.0 * math.floor(a / 360.0)
    return a + 360.0 if a < 0.0 else a


def _fix_hour(a):
    a = a - 24.0 * math.floor(a / 24.0)
    return a + 24.0 if a < 0.0 else a


def julian(year, month, day):
    if month <= 2:
        year -= 1
        month += 12
    a = math.floor(year / 100)
    b = 2 - a + math.floor(a / 4)
    return (math.floor(365.25 * (year + 4716))
            + math.floor(30.6001 * (month + 1)) + day + b - 1524.5)


def sun_position(jd):
    """Return (declination_deg, equation_of_time_hours) at a Julian day."""
    d = jd - 2451545.0
    g = _fix_angle(357.529 + 0.98560028 * d)          # mean anomaly
    q = _fix_angle(280.459 + 0.98564736 * d)          # mean longitude
    L = _fix_angle(q + 1.915 * _sin(g) + 0.020 * _sin(2 * g))  # ecliptic lon
    e = 23.439 - 0.00000036 * d                        # obliquity
    ra = _arctan2(_cos(e) * _sin(L), _cos(L)) / 15.0
    eqt = q / 15.0 - _fix_hour(ra)                     # equation of time (h)
    decl = _arcsin(_sin(e) * _sin(L))
    return decl, eqt


def _hour_angle(lat, decl, altitude):
    """Hour angle (degrees) for a target sun altitude (negative below the
    horizon); None if the altitude is never reached."""
    x = (_sin(altitude) - _sin(lat) * _sin(decl)) / (_cos(lat) * _cos(decl))
    if x < -1.0 or x > 1.0:
        return None
    return _arccos(x)


def _asr_altitude(lat, decl, factor):
    return _arccot(factor + _tan(abs(lat - decl)))


def prayer_times(lat, lon, year, month, day, tz_hours, method=DEFAULT_METHOD):
    """Six prayer times for a local calendar date, as fractions of a day in
    [0, 24). `tz_hours` is the UTC offset of that date (DST-resolved).
    Returns {key: float_hours|None}; None when the sun never reaches the
    required altitude (high-latitude midnight sun)."""
    m = METHODS[method]
    sunrise = -m['sunrise']

    # Diyanet's calendar evaluates the sun once per day at 0h UT (the plain
    # almanac convention) — not at local solar midnight the way PrayTimes
    # does; the two conventions differ by up to a minute in the printed times.
    jd = julian(year, month, day)
    decl, eqt = sun_position(jd)
    midday = _fix_hour(12.0 - eqt)                     # local solar noon

    def morning(alt):
        ha = _hour_angle(lat, decl, alt)
        return None if ha is None else _fix_hour(midday - ha / 15.0)

    def afternoon(alt):
        ha = _hour_angle(lat, decl, alt)
        return None if ha is None else _fix_hour(midday + ha / 15.0)

    raw = {
        'imsak':  morning(-m['fajr']),
        'gunes':  morning(sunrise),
        'ogle':   midday,
        # asr only exists while the sun is actually up; at polar latitudes the
        # shadow-length formula can land below the horizon — treat as undefined
        'ikindi': (afternoon(_asr_altitude(lat, decl, m['asr']))
                   if _asr_altitude(lat, decl, m['asr']) > sunrise else None),
        'aksam':  afternoon(sunrise),
        'yatsi':  afternoon(-m['isha']),
    }

    # local solar time → local clock time, plus the method's temkin (if any)
    temkin = m.get('temkin', {})
    out = {}
    for k, v in raw.items():
        if v is None:
            out[k] = None
        else:
            out[k] = _fix_hour(v + tz_hours - lon / 15.0 + temkin.get(k, 0) / 60.0)
    return out


def format_time(hours, fmt='%H:%M'):
    if hours is None:
        return None
    h = int(hours)
    minutes = int(round((hours - h) * 60.0))
    if minutes == 60:                       # rounding overflow (23:60)
        h = (h + 1) % 24
        minutes = 0
    return f'{h:02d}:{minutes:02d}'


def qibla(lat, lon):
    """Qibla bearing in degrees clockwise from true north."""
    lat_k, lon_k = KAABA
    dlon = lon_k - lon
    num = _sin(dlon)
    den = _cos(lat) * _tan(lat_k) - _sin(lat) * _cos(dlon)
    return (_fix_angle(_arctan2(num, den)) + 360.0) % 360.0


# ---------------------------------------------------------------------------
# timezone
# ---------------------------------------------------------------------------

def default_tz_name():
    """Best-effort IANA timezone of this machine (e.g. 'Europe/Istanbul').

    `datetime.now().astimezone()` returns a fixed-offset object (no `.key`),
    so read the zone name from the usual sources instead.
    """
    tz = os.environ.get('TZ')
    if tz and '/' in tz:                     # e.g. TZ=Europe/Istanbul
        return tz
    try:                                     # Debian/Ubuntu
        name = Path('/etc/timezone').read_text(encoding='utf-8').strip()
        if name:
            return name
    except OSError:
        pass
    try:                                     # symlink → zoneinfo/Region/City
        target = os.path.realpath('/etc/localtime')
        for prefix in ('/usr/share/zoneinfo/', '/usr/lib/zoneinfo/', '/etc/zoneinfo/'):
            if target.startswith(prefix):
                return target[len(prefix):]
    except OSError:
        pass
    return 'UTC'


def tz_offset_hours(tz_name, year, month, day):
    """UTC offset in hours for a local date in `tz_name` (DST-aware)."""
    import time as _time
    try:
        from zoneinfo import ZoneInfo
        z = ZoneInfo(tz_name)
    except Exception:                         # unknown zone / no tzdata
        # fall back to the machine's *current* fixed offset
        return -_time.timezone / 3600.0
    noon_utc = datetime(year, month, day, 12, 0, 0, tzinfo=dt_timezone.utc)
    local = noon_utc.astimezone(z)
    return local.utcoffset().total_seconds() / 3600.0


def today_times(lat, lon, tz_name=None):
    """Today's six prayer times as 'HH:MM' strings (local clock)."""
    tz = tz_name or default_tz_name()
    now = datetime.now()
    off = tz_offset_hours(tz, now.year, now.month, now.day)
    t = prayer_times(lat, lon, now.year, now.month, now.day, off)
    return {k: format_time(v) for k, v in t.items()}


def _fire_dt(day, hours):
    """A midnight datetime + a float hour-of-day → a minute-rounded datetime,
    the same rounding format_time() uses."""
    total = round(hours * 60.0)
    return day + timedelta(minutes=total)


def next_prayer(lat, lon, tz_name=None):
    """The next of the six daily times relative to *now* (wall clock).

    Returns {'key', 'time', 'in_minutes', 'tomorrow'} or None when the times
    are undefined (polar day/night)."""
    tz = tz_name or default_tz_name()
    now = datetime.now()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    off = tz_offset_hours(tz, day.year, day.month, day.day)
    t = prayer_times(lat, lon, day.year, day.month, day.day, off)
    best = None
    for key in PRAYER_KEYS:
        v = t.get(key)
        if v is None:
            continue
        fire = _fire_dt(day, v)
        if fire <= now:
            continue
        if best is None or fire < best[0]:
            best = (fire, key)
    if best is not None:
        return {'key': best[1], 'time': format_time(t[best[1]]),
                'in_minutes': int((best[0] - now).total_seconds() // 60),
                'tomorrow': False}
    nd = day + timedelta(days=1)
    off = tz_offset_hours(tz, nd.year, nd.month, nd.day)
    t2 = prayer_times(lat, lon, nd.year, nd.month, nd.day, off)
    for key in PRAYER_KEYS:
        if t2.get(key) is not None:
            fire = _fire_dt(nd, t2[key])
            return {'key': key, 'time': format_time(t2[key]),
                    'in_minutes': int((fire - now).total_seconds() // 60),
                    'tomorrow': True}
    return None


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

CONFIG_KEY = 'config.json'


def config_dir():
    base = os.environ.get('XDG_CONFIG_HOME')
    if base:
        return Path(base) / 'salah-times'
    return Path.home() / '.config' / 'salah-times'


LEGACY_CONFIG = Path.home() / '.config' / 'quran-tools' / 'namaz.json'


def config_path():
    return config_dir() / CONFIG_KEY


def _migrate_legacy(path):
    """First run: adopt the quran-tools namaz.json so the location and the
    reminder setup survive the extraction."""
    if path.exists() or not LEGACY_CONFIG.exists():
        return
    try:
        raw = json.loads(LEGACY_CONFIG.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(raw, dict):
        save_config(sanitize_config(raw), path)


def default_config():
    return {
        'location': None,               # {t, e, cc, lat, lon} as chosen in the UI
        'timezone': None,               # IANA name; None = follow the machine
        'method': DEFAULT_METHOD,       # diyanet | mwl | isna | hanafi
        'voice': False,                 # speak the reminders aloud
        'voice_engine': 'spd-say',      # spd-say | espeak-ng | espeak
        'voice_lang': 'tr',             # ISO code handed to the engine
        'voice_name': '',               # exact synthesis voice ('' = engine default)
        # One *list* of reminders per prayer — several may fire for the same
        # prayer (30 min before, 10 min before, on time…). An empty list or
        # `on: false` means that reminder is off.
        'notify': {
            'imsak':  [{'on': True,  'when': 'ontime', 'minutes': 0}],
            'gunes':  [{'on': False, 'when': 'ontime', 'minutes': 0}],
            'ogle':   [{'on': True,  'when': 'before', 'minutes': 15}],
            'ikindi': [{'on': False, 'when': 'before', 'minutes': 10}],
            'aksam':  [{'on': True,  'when': 'ontime', 'minutes': 0}],
            'yatsi':  [{'on': True,  'when': 'before', 'minutes': 5}],
        },
        '_comment': (
            'Config for the local salah-times notifier. `notify` maps each '
            'prayer to a list of reminders; a reminder has `on`, `when` '
            '("ontime" or "before") and `minutes` (how long before). Several '
            'reminders may share one prayer. `voice` speaks them aloud, with '
            '`voice_engine` / `voice_lang` / `voice_name` choosing the voice '
            '(list them with `qt-namaz voices`). Edited by the #/namaz web '
            'view, the salah-times CLI, or by hand.'
        ),
    }


def normalize_notify(raw):
    """Coerce any accepted `notify` shape into {prayer: [reminder, …]}.

    Two shapes are accepted: the modern list of reminders per prayer, and the
    legacy single-dict form (`{"on":…, "when":…, "minutes":…}`), which becomes
    a one-item list — so configs written before multiple reminders existed
    keep working untouched. Values are clamped and identical reminders (same
    prayer, same lead time) are collapsed.
    """
    out = {key: [] for key in PRAYER_KEYS}
    if not isinstance(raw, dict):
        return out
    for key in PRAYER_KEYS:
        spec = raw.get(key)
        if spec is None:
            continue
        items = spec if isinstance(spec, list) else [spec]
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            when = 'before' if item.get('when') == 'before' else 'ontime'
            try:
                minutes = int(item.get('minutes') or 0)
            except (TypeError, ValueError):
                minutes = 0
            minutes = max(0, min(minutes, 180))
            lead = minutes if when == 'before' else 0
            if lead in seen:                   # same moment → one reminder
                continue
            seen.add(lead)
            out[key].append({'on': bool(item.get('on', True)),
                             'when': when, 'minutes': minutes})
            if len(out[key]) >= MAX_REMINDERS:
                break
    return out


def _apply_notify(cfg, raw):
    """Overlay a config file's `notify` block onto `cfg`, keeping defaults for
    the prayers the block does not mention."""
    if not isinstance(raw, dict):
        return
    norm = normalize_notify(raw)
    for key in PRAYER_KEYS:
        if key in raw:
            cfg['notify'][key] = norm[key]


def sanitize_config(raw):
    """Coerce an untrusted config dict (e.g. POSTed by the web UI) into a
    valid config: unknown keys dropped, numbers clamped, strings bounded."""
    cfg = default_config()
    if not isinstance(raw, dict):
        return cfg
    loc = raw.get('location')
    if isinstance(loc, dict):
        try:
            lat = float(loc.get('lat'))
            lon = float(loc.get('lon'))
        except (TypeError, ValueError):
            lat = lon = None
        if lat is not None and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            cfg['location'] = {'t': str(loc.get('t') or '')[:80],
                               'e': str(loc.get('e') or '')[:80],
                               'cc': str(loc.get('cc') or '')[:8],
                               'lat': round(lat, 4), 'lon': round(lon, 4)}
    tz = raw.get('timezone')
    if isinstance(tz, str) and tz and '/' in tz and len(tz) <= 64:
        cfg['timezone'] = tz
    if raw.get('method') in METHODS:
        cfg['method'] = raw['method']
    cfg['voice'] = bool(raw.get('voice'))
    if raw.get('voice_engine') in VOICE_ENGINES:
        cfg['voice_engine'] = raw['voice_engine']
    if isinstance(raw.get('voice_lang'), str) and raw['voice_lang']:
        cfg['voice_lang'] = raw['voice_lang'][:16]
    if isinstance(raw.get('voice_name'), str):
        cfg['voice_name'] = raw['voice_name'][:80]
    _apply_notify(cfg, raw.get('notify'))
    return cfg


def load_config(path=None):
    path = Path(path) if path else config_path()
    if path == config_path():        # never migrate into an explicit path
        _migrate_legacy(path)
    cfg = default_config()
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return cfg
    # deep-merge over the defaults so new keys always exist
    for k in ('location', 'timezone', 'method', 'voice', 'voice_engine',
              'voice_lang', 'voice_name'):
        if k in raw:
            cfg[k] = raw[k]
    _apply_notify(cfg, raw.get('notify'))
    return cfg


def save_config(cfg, path=None):
    path = Path(path) if path else config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # atomic replace: the daemon watches mtime and must never read a
    # half-written file (a torn read would fall back to the defaults)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n',
                   encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# desktop notification + voice delivery
# ---------------------------------------------------------------------------

def user_runtime_env():
    """Environment that reaches this user's D-Bus session and audio server.

    Needed because the daemon can run as a *system* service (root or a target
    user) with no XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS in its env — this
    reconstructs them from /run/user/<uid> so `notify-send` and `spd-say` /
    `espeak-ng` can talk to the user's session.
    """
    env = dict(os.environ)
    uid = os.getuid()
    runtime = Path(f'/run/user/{uid}')
    if runtime.is_dir():
        env.setdefault('XDG_RUNTIME_DIR', str(runtime))
        env.setdefault('DBUS_SESSION_BUS_ADDRESS', f'unix:path={runtime}/bus')
        pulse = runtime / 'pulse' / 'native'
        if pulse.exists() and 'PULSE_SERVER' not in env:
            env['PULSE_SERVER'] = f'unix:{pulse}'
        # display sockets — notify-send/spd-say don't need them (they talk
        # D-Bus), but any X11/Wayland-only helper would
        if 'WAYLAND_DISPLAY' not in env and (runtime / 'wayland-0').exists():
            env['WAYLAND_DISPLAY'] = 'wayland-0'
    if 'DISPLAY' not in env:
        xsocks = sorted(Path('/tmp/.X11-unix').glob('X*'))
        if xsocks:
            env['DISPLAY'] = ':' + xsocks[0].name[1:]
    return env


def _run(cmd, env, timeout=15):
    import subprocess
    try:
        subprocess.run(cmd, env=env, timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def send_notification(title, body, urgency='normal'):
    """Show a desktop notification. Tries notify-send, then gdbus."""
    env = user_runtime_env()
    urgency_map = {'low': 0, 'normal': 1, 'critical': 2}
    u = urgency_map.get(urgency, 1)
    if _run(['notify-send', '-u', ['low', 'normal', 'critical'][u],
             '--app-name', 'Quran Tools', title, body], env):
        return True
    # fallback: raw D-Bus call (works without notify-send installed)
    return _run(['gdbus', 'call', '--session', '--dest',
                 'org.freedesktop.Notifications',
                 '/org/freedesktop/Notifications',
                 'org.freedesktop.Notifications.Notify',
                 'Quran Tools', '0', '', title, body, '[]', '{}', '5000'], env)


def with_wake_prefix(text):
    """Prepend the wake-up phrase (idempotent), so an idle audio sink cannot
    swallow the first words of the message."""
    text = '' if text is None else str(text)
    if text.lstrip().lower().startswith('attention, attention'):
        return text
    return SPEECH_WAKE_PREFIX + text


def speak(text, engine='spd-say', lang='tr', voice=None):
    """Speak `text` aloud via speech-dispatcher or espeak/espeak-ng.

    `voice` is an exact synthesis voice: the `spd-say -L` name (e.g.
    'Turkish+Adam') for spd-say, or the espeak voice name ('turkish',
    'turkish-mbrola-2'). None/'' keeps the engine's default for `lang`.

    The text is always prefixed with SPEECH_WAKE_PREFIX: the audio device
    takes a moment to wake after being idle and would otherwise cut off the
    beginning of the reminder.
    """
    text = with_wake_prefix(text)
    env = user_runtime_env()
    if engine in ('espeak-ng', 'espeak'):
        exe = 'espeak-ng' if engine == 'espeak-ng' else 'espeak'
        base = [exe, '-v', voice or lang, text]
        if _run(base, env):
            return True
        other = 'espeak' if exe == 'espeak-ng' else 'espeak-ng'
        return _run([other, '-v', voice or lang, text], env)
    # spd-say (default)
    cmd = ['spd-say', '-l', lang]
    if voice:
        cmd += ['-y', voice]
    if _run(cmd + [text], env):
        return True
    return _run(['espeak-ng', '-v', voice or lang, text], env)


def list_voices(engine='spd-say', lang=None):
    """The voices the installed engines can actually speak, as
    [{'engine', 'name', 'lang', 'label'}].

    Parsed from `spd-say -L` (one voice per line: name, language, variant) and
    `espeak --voices`; an engine that is not installed simply yields nothing,
    which is also how the CLI reports what is available here.
    """
    import shutil
    import subprocess
    env = user_runtime_env()
    out = []
    want = (engine,) if engine else VOICE_ENGINES
    if 'spd-say' in want and shutil.which('spd-say'):
        try:
            res = subprocess.run(['spd-say', '-L'], env=env, timeout=20,
                                 capture_output=True, text=True)
            # columns are NAME · LANGUAGE · VARIANT, but a long name overflows
            # its column, so split from the right instead of by position;
            # speech-dispatcher also emits a few rows where the language and
            # variant columns run together (the Half-Life voice) — those have
            # no parseable language and are skipped.
            import re as _re
            tag = _re.compile(r'^[a-z]{2,3}(?:[-_][A-Za-z0-9]+)*$')
            seen = set()
            for line in res.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) < 3:
                    continue
                variant, lg = parts[-1], parts[-2]
                name = ' '.join(parts[:-2])
                if not tag.match(lg) or not name:
                    continue
                if (name, lg) in seen:
                    continue
                seen.add((name, lg))
                if lang and lg != lang:
                    continue
                label = name if variant in ('none', '') else f'{name} ({variant})'
                out.append({'engine': 'spd-say', 'name': name,
                            'lang': lg, 'label': label})
        except Exception:
            pass
    for exe in ('espeak-ng', 'espeak'):
        if exe not in want or not shutil.which(exe):
            continue
        try:
            res = subprocess.run([exe, '--voices'], env=env, timeout=20,
                                 capture_output=True, text=True)
        except Exception:
            continue
        for line in res.stdout.splitlines()[1:]:
            parts = line.split()
            # Pty Language Age/Gender VoiceName File [Other Languages]
            if len(parts) < 5 or not parts[1].isalpha():
                continue
            lg, name = parts[1], parts[3]
            if lang and lg != lang:
                continue
            out.append({'engine': exe, 'name': name, 'lang': lg,
                        'label': f'{name} ({lg})'})
        break                      # the two share a voice table
    return out


# ---------------------------------------------------------------------------
# notification schedule
# ---------------------------------------------------------------------------

def _prayer_message(key, minutes_before):
    """Human message for a prayer reminder (Turkish, matching the app)."""
    tr = PRAYER_TR.get(key, key)
    if minutes_before > 0:
        return f'{tr} vaktine {minutes_before} dakika kaldı'
    return f'{tr} vakti girdi'


def build_schedule(cfg, hours=48):
    """Upcoming reminders as [(fire, key, minutes_before)] sorted by time.

    `fire` is a naive local wall-clock datetime. Each prayer contributes one
    row per *enabled* reminder, so a prayer may legitimately appear several
    times a day (30 min before, 10 min before, on time…). 'before' reminders
    fire `minutes` ahead of the prayer. Prayers at extreme latitudes whose sun
    never reaches the required altitude are skipped.
    """
    from datetime import timedelta
    loc = cfg.get('location') or {}
    if not loc.get('lat') or not loc.get('lon'):
        return []
    tz = cfg.get('timezone') or default_tz_name()
    method = cfg.get('method') or DEFAULT_METHOD
    now = datetime.now()
    out = []
    days = max(1, math.ceil(hours / 24))
    notify = normalize_notify(cfg.get('notify'))
    for d in range(days):
        day = (now + timedelta(days=d)).replace(hour=0, minute=0, second=0,
                                                microsecond=0)
        off = tz_offset_hours(tz, day.year, day.month, day.day)
        times = prayer_times(loc['lat'], loc['lon'], day.year, day.month,
                             day.day, off, method)
        for key in PRAYER_KEYS:
            th = times.get(key)
            if th is None:
                continue
            for spec in notify.get(key, ()):
                if not spec.get('on'):
                    continue
                before = (int(spec.get('minutes') or 0)
                          if spec.get('when') == 'before' else 0)
                # round to the minute the same way format_time() does, so the
                # schedule, the UI and the printed times always agree
                fire = day + timedelta(minutes=round(th * 60.0) - before)
                out.append((fire, key, before))
    return sorted(out)


if __name__ == '__main__':
    # quick self-test: print today's times for a few cities
    for city, (lat, lon) in {
        'İstanbul': (41.010, 28.965),
        'Ankara': (39.920, 32.854),
        'Mekke': (21.423, 39.826),
    }.items():
        t = today_times(lat, lon, 'Europe/Istanbul' if city != 'Mekke' else 'Asia/Riyadh')
        print(city, t, 'qibla %.1f°' % qibla(lat, lon))
