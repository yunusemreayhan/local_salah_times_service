#!/usr/bin/env python3
"""salah-times server — prayer times, qibla and the reminder config over HTTP.

Pure stdlib (http.server) so the service has no virtualenv and no third-party
dependency: the prayer-time maths in salah_times.py is stdlib too.

    GET  /                      the dashboard page
    GET  /healthz               liveness probe (used by local_cockpit)
    GET  /api/config            config + path + prayers + methods + service state
    POST /api/config            validate + save (~/.config/salah-times/config.json)
    GET  /api/times             today's six times, qibla, next prayer, upcoming reminders
    GET  /api/month?year&month  the whole month (table view)
    GET  /api/voices?engine&lang the synthesis voices installed on this machine
    GET  /api/qibla?lat&lon     bearing to the Kaaba
    POST /api/test              fire a test notification (+ voice when enabled)

The notifier daemon (daemon.py) reads the same config file and reloads it on
mtime change, so saving from the UI never needs a restart.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import salah_times as st  # noqa: E402

STATIC = HERE / 'static'
DEFAULT_PORT = 8381
MAX_BODY = 64 * 1024
NOTIFIER_UNIT = 'salah-times-notifier.service'
LEGACY_UNIT = 'quran-tools-namaz.service'      # pre-extraction system unit


def legacy_notifier() -> dict:
    """The old system-wide notifier still delivers the same reminders.

    Running both means every reminder fires twice, which reads like "the times
    moved". Detect it and tell the user once instead of silently doubling.
    """
    try:
        res = subprocess.run(['systemctl', 'is-active', LEGACY_UNIT],
                             capture_output=True, text=True, timeout=3)
        state = (res.stdout or '').strip() or 'unknown'
        return {'unit': LEGACY_UNIT, 'active': state,
                'duplicate': state == 'active'}
    except Exception:                                  # noqa: BLE001
        return {'unit': LEGACY_UNIT, 'active': 'unknown', 'duplicate': False}


def service_state(unit: str) -> dict:
    try:
        res = subprocess.run(['systemctl', '--user', 'is-active', unit],
                             capture_output=True, text=True, timeout=3)
        state = (res.stdout or '').strip() or 'unknown'
        enabled = subprocess.run(['systemctl', '--user', 'is-enabled', unit],
                                 capture_output=True, text=True, timeout=3)
        return {'unit': unit, 'active': state,
                'enabled': (enabled.stdout or '').strip() or 'unknown'}
    except Exception:                                  # noqa: BLE001
        return {'unit': unit, 'active': 'unknown', 'enabled': 'unknown'}


class Handler(BaseHTTPRequestHandler):
    server_version = 'salah-times/1.0'

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):                 # quieter journal
        sys.stderr.write('%s - %s\n' % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False,
                                      default=str).encode(), 'application/json; charset=utf-8')

    def _text(self, status: int, text: str, ctype='text/plain; charset=utf-8') -> None:
        self._send(status, text.encode(), ctype)

    def _file(self, path: Path, ctype: str) -> None:
        try:
            self._send(200, path.read_bytes(), ctype)
        except OSError:
            self._json(404, {'detail': 'Not Found'})

    def _cfg(self) -> dict:
        return st.load_config()

    def _loc_tz(self, cfg: dict):
        loc = cfg.get('location') or {}
        lat, lon = loc.get('lat'), loc.get('lon')
        tz = cfg.get('timezone') or st.default_tz_name()
        return lat, lon, tz

    # -- GET --------------------------------------------------------------
    def do_GET(self):                                  # noqa: N802
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)

        if path in ('/', '/index.html'):
            return self._file(STATIC / 'index.html', 'text/html; charset=utf-8')
        if path.startswith('/static/'):
            name = path[len('/static/'):]
            if '/' in name or name.startswith('.'):
                return self._json(404, {'detail': 'Not Found'})
            ext = name.rsplit('.', 1)[-1].lower()
            ctype = {'css': 'text/css', 'js': 'application/javascript',
                     'svg': 'image/svg+xml', 'png': 'image/png',
                     'json': 'application/json'}.get(ext, 'application/octet-stream')
            return self._file(STATIC / name, ctype + '; charset=utf-8'
                              if ctype.startswith(('text', 'application/json',
                                                   'application/javascript')) else ctype)
        if path == '/favicon.ico':
            return self._file(STATIC / 'favicon.svg', 'image/svg+xml')
        if path == '/healthz':
            return self._json(200, {'ok': True, 'ts': time.time()})

        if path == '/api/config':
            cfg = self._cfg()
            return self._json(200, {
                'config': cfg,
                'path': str(st.config_path()),
                'prayers': st.PRAYERS,
                'methods': sorted(st.METHODS),
                'voice_engines': list(st.VOICE_ENGINES),
                'service': service_state(NOTIFIER_UNIT),
                'legacy_notifier': legacy_notifier(),
                'timezone_detected': st.default_tz_name(),
            })

        if path == '/api/times':
            cfg = self._cfg()
            lat, lon, tz = self._loc_tz(cfg)
            if lat is None or lon is None:
                return self._json(200, {'configured': False, 'config': cfg})
            times = st.today_times(lat, lon, tz)
            next_p = st.next_prayer(lat, lon, tz)
            schedule = []
            for fire, key, before in st.build_schedule(cfg, hours=48)[:12]:
                schedule.append({
                    'fire': fire.strftime('%Y-%m-%dT%H:%M'),
                    'key': key,
                    'label': st.PRAYER_TR.get(key, key),
                    'minutes_before': before,
                    'message': st._prayer_message(key, before),
                })
            return self._json(200, {
                'configured': True,
                'date': datetime.now().strftime('%Y-%m-%d'),
                'server_time': datetime.now().strftime('%H:%M:%S'),
                'location': cfg.get('location'),
                'timezone': tz,
                'method': cfg.get('method'),
                'times': times,
                'prayers': st.PRAYERS,
                'qibla': round(st.qibla(lat, lon), 1),
                'next': next_p,
                'schedule': schedule,
                'voice': {'on': bool(cfg.get('voice')),
                          'engine': cfg.get('voice_engine'),
                          'lang': cfg.get('voice_lang'),
                          'name': cfg.get('voice_name')},
                'service': service_state(NOTIFIER_UNIT),
                'legacy_notifier': legacy_notifier(),
            })

        if path == '/api/month':
            cfg = self._cfg()
            lat, lon, tz = self._loc_tz(cfg)
            if lat is None or lon is None:
                return self._json(200, {'configured': False, 'days': []})
            now = datetime.now()
            try:
                year = int(q.get('year', [now.year])[0])
                month = int(q.get('month', [now.month])[0])
            except ValueError:
                return self._json(400, {'detail': 'year/month must be integers'})
            if not (2000 <= year <= 2100 and 1 <= month <= 12):
                return self._json(400, {'detail': 'year/month out of range'})
            import calendar
            days = []
            for day in range(1, calendar.monthrange(year, month)[1] + 1):
                off = st.tz_offset_hours(tz, year, month, day)
                t = st.prayer_times(lat, lon, year, month, day, off,
                                    cfg.get('method') or st.DEFAULT_METHOD)
                days.append({
                    'date': f'{year:04d}-{month:02d}-{day:02d}',
                    'weekday': datetime(year, month, day).strftime('%a'),
                    'times': {k: st.format_time(v) for k, v in t.items()},
                })
            return self._json(200, {'year': year, 'month': month,
                                    'location': cfg.get('location'),
                                    'days': days})

        if path == '/api/voices':
            engine = (q.get('engine') or ['spd-say'])[0]
            lang = (q.get('lang') or [None])[0]
            return self._json(200, {'voices': st.list_voices(engine, lang)})

        if path == '/api/qibla':
            cfg = self._cfg()
            lat, lon, _ = self._loc_tz(cfg)
            try:
                lat = float((q.get('lat') or [lat])[0])
                lon = float((q.get('lon') or [lon])[0])
            except (TypeError, ValueError):
                return self._json(400, {'detail': 'lat/lon required'})
            return self._json(200, {'lat': lat, 'lon': lon,
                                    'qibla': round(st.qibla(lat, lon), 2)})

        return self._json(404, {'detail': 'Not Found'})

    # -- POST -------------------------------------------------------------
    def do_POST(self):                                 # noqa: N802
        u = urlparse(self.path)
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            return self._json(413, {'detail': 'body too large'})
        raw = self.rfile.read(length) if length else b''
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return self._json(400, {'detail': 'invalid JSON'})

        if u.path == '/api/config':
            cfg = st.sanitize_config(body)
            path = st.save_config(cfg)
            return self._json(200, {'ok': True, 'config': cfg, 'path': str(path)})

        if u.path == '/api/test':
            cfg = self._cfg()
            loc = cfg.get('location') or {}
            name = loc.get('t') or 'salah-times'
            try:
                st.send_notification(f'{name} — test',
                                     'salah-times bildirimi calisiyor.')
                if cfg.get('voice'):
                    st.speak('Namaz vakitleri bildirimi testi.',
                             cfg.get('voice_engine'), cfg.get('voice_lang'),
                             cfg.get('voice_name'))
            except Exception as exc:                   # noqa: BLE001
                return self._json(500, {'ok': False, 'detail': str(exc)})
            return self._json(200, {'ok': True, 'voice': bool(cfg.get('voice'))})

        return self._json(404, {'detail': 'Not Found'})


def main() -> int:
    ap = argparse.ArgumentParser(description='salah-times web service')
    ap.add_argument('--port', type=int,
                    default=int(__import__('os').environ.get('SALAH_PORT', DEFAULT_PORT)))
    ap.add_argument('--host', default='127.0.0.1')
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'salah-times serving on http://{args.host}:{args.port}/ '
          f'(config {st.config_path()})', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
