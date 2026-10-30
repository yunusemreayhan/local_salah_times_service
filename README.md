# local_salah_times_service

Standalone prayer-time (salah / namaz) service for this laptop:

- **web dashboard** — `http://127.0.0.1:8381/` — today's six times, live
  countdown to the next one, qibla, the month table, the reminder editor and
  the voice picker
- **notifier daemon** — desktop popup + optional speech at the times you chose
  (`salah-times-notifier.service`)
- **CLI** — `salah-times times | next | config | qibla | set-location | set-notify | voices | test-notify`

Every piece is **pure stdlib Python** — no venv, no pip, no third-party
package. The only system dependencies are optional: `spd-say`
(speech-dispatcher) or `espeak-ng` for spoken reminders, and
`notify-send`/D-Bus for the popups.

## Extracted from

This repository is the salah-times subsystem of `quran_tools`, lifted out so it
can run (and be versioned, and be deployed) on its own:

| here | there |
|---|---|
| `app/salah_times.py` | `namaz.py` |
| `cli.py` | `qt-namaz` |
| `daemon.py` | `qt_namazd.py` |
| `app/server.py` | the `/api/namaz/*` handlers in `serve.py` |
| `app/static/*` | the `#/namaz` view (`app/js/plugins/namaz.js`) |
| `tools/check_namaz.py` | unchanged, 8 993 assertions |
| `data/namaz/` | the decoded Diyanet location tree |

The prayers, qibla and temkin maths are unchanged and still verified against
**792 published Diyanet days** (İstanbul + Ankara) — `python3 tools/check_namaz.py`
must stay at `OK — 8993 checks passed`.

## Config

`~/.config/salah-times/config.json` (0600, atomic writes).

On first run the service **adopts** the old
`~/.config/quran-tools/namaz.json` if the new file does not exist yet, so the
location, reminders and voice survive the extraction. An explicit path passed
to `load_config(path)` is never migrated into.

```json
{
  "location": {"t": "İSTANBUL", "e": "ISTANBUL", "cc": "TR", "lat": 41.01, "lon": 28.965},
  "timezone": null,
  "method": "diyanet",
  "voice": true,
  "voice_engine": "spd-say",
  "voice_lang": "tr",
  "voice_name": "Turkish+whisper",
  "notify": {
    "imsak":  [{"on": true,  "when": "before", "minutes": 10}],
    "ogle":   [{"on": true,  "when": "ontime", "minutes": 0}],
    "aksam":  [{"on": true,  "when": "before", "minutes": 10},
               {"on": true,  "when": "ontime", "minutes": 0}]
  }
}
```

Methods: `diyanet` (Turkish angles + temkin), `mwl`, `isna`, `hanafi`.

## Install

```bash
./install.sh
systemctl --user status salah-times salah-times-notifier
```

That symlinks the units (they are `enabled` + linger, so they start at boot),
puts `salah-times` on `PATH`, and starts both services.

## API

```
GET  /healthz
GET  /api/config              config + path + prayers + methods + unit state
POST /api/config              validate (sanitize_config) + save
GET  /api/times               today's times, qibla, next prayer, next 12 reminders
GET  /api/month?year&month    the whole month
GET  /api/qibla?lat&lon
GET  /api/voices?engine&lang
POST /api/test                test popup (+ voice when enabled)
```

`local_cockpit` shows this service as a card and embeds the dashboard; add it
there with:

```json
{"key": "salah", "name": "Salah Times", "port": 8381, "unit": "salah-times"}
```

## Notes

- `#/namaz` inside `quran_tools` still exists and still works from its own copy;
  this repo is the standalone service. Pointing the quran-tools UI at
  `http://127.0.0.1:8381/api/*` instead of its own handlers is a one-file change
  in `serve.py` if you want a single source of truth.
- Extreme latitudes: prayers whose sun never reaches the required altitude come
  back as `null` and are skipped — asserted by the polar tests.
- The daemon re-reads the config on mtime change, so saving in the UI takes
  effect within a minute without a restart.
