#!/usr/bin/env python3
"""
salah-timesd — the always-running prayer-time notifier daemon.

Loop (driven by systemd, see deploy/quran-tools-salah_times.service):

  1. load the config (qt-salah_times.json) — created/edited by the #/namaz web view
     or `salah-times`
  2. build the next ~48 h of reminders from the prayer-time calculation
  3. fire any whose wall-clock time has arrived: desktop popup + optional voice
  4. sleep until the next reminder (bounded at 60 s so config edits, DST and
     clock changes are picked up within a minute), then repeat

The daemon re-reads the config file whenever its mtime changes, so no restart
is needed after a config edit. It runs fine under a *system* service because
user_runtime_env() reconstructs the D-Bus session + audio socket paths.
"""
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'app'))

import salah_times  # noqa: E402

GRACE_SECONDS = 120      # window after a reminder time in which it may fire
POLL_SECONDS = 30        # config re-check / re-schedule cadence
MAX_SLEEP = 60.0         # never sleep longer than this (clock/DST safety)

_running = True


def _stop(signum, frame):
    global _running
    _running = False


def _config_mtime(cfg_path):
    try:
        return cfg_path.stat().st_mtime
    except OSError:
        return 0.0


def _ident(fire, key, before):
    return (fire.strftime('%Y-%m-%d'), key, before)


def due_reminders(schedule, now, fired, grace=GRACE_SECONDS):
    """[(fire, key, before)] whose time has arrived within `grace` seconds and
    has not fired yet. Pure, so the loop and the tests share one definition of
    "due"."""
    out = []
    for fire, key, before in schedule:
        if _ident(fire, key, before) in fired:
            continue
        age = (now - fire).total_seconds()
        if 0 <= age < grace:
            out.append((fire, key, before))
    return out


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    cfg_path = salah_times.config_path()
    cfg = salah_times.load_config(cfg_path)
    last_mtime = _config_mtime(cfg_path)
    fired = set()                    # (date, key, before) already notified
    log = lambda *a: print(*a, flush=True)

    loc = cfg.get('location') or {}
    if not loc.get('lat'):
        log('[salah-timesd] no location configured — run `salah-times set-location` '
            'or open the #/namaz view. Sleeping.')
    else:
        log(f"[salah-timesd] started for {loc.get('t', '?')} "
            f"({loc.get('lat')}, {loc.get('lon')}) — {cfg_path}")

    while _running:
        # reload config if edited on disk
        mt = _config_mtime(cfg_path)
        if mt != last_mtime:
            last_mtime = mt
            cfg = salah_times.load_config(cfg_path)
            log('[salah-timesd] config reloaded')

        now = datetime.now()
        schedule = salah_times.build_schedule(cfg)

        # fire due reminders (and drop fired entries older than today)
        fired = {k for k in fired if k[0] >= now.strftime('%Y-%m-%d')}
        for fire, key, before in due_reminders(schedule, now, fired):
            text = salah_times._prayer_message(key, before)
            log(f'[salah-timesd] notify: {text}')
            salah_times.send_notification('🕌 ' + salah_times.PRAYER_TR[key], text)
            if cfg.get('voice'):
                salah_times.speak(text, cfg.get('voice_engine'), cfg.get('voice_lang'),
                            cfg.get('voice_name'))
            fired.add(_ident(fire, key, before))

        # sleep until the next un-fired reminder, bounded by MAX_SLEEP
        next_fire = None
        for fire, key, before in schedule:
            ident = (fire.strftime('%Y-%m-%d'), key, before)
            if ident in fired:
                continue
            if fire >= now:
                next_fire = fire
                break
        if next_fire is None:
            time.sleep(POLL_SECONDS)
        else:
            wait = max(0.5, min((next_fire - now).total_seconds(), MAX_SLEEP))
            time.sleep(wait)

    log('[salah-timesd] stopped')
    return 0


if __name__ == '__main__':
    sys.exit(main())
