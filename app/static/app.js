/* salah-times dashboard — vanilla JS, no build step */
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const PRESETS = [5, 10, 15, 20, 30, 45, 60];
const PRAYER_KEYS = ['imsak', 'gunes', 'ogle', 'ikindi', 'aksam', 'yatsi'];

let CFG = null;
let TIMES = null;
let NOTIFY = {};
let TARGET = null;   // epoch ms of the next prayer

function clone(o) { return JSON.parse(JSON.stringify(o)); }

/* ------------------------------------------------------------------ load */
async function load() {
  const [c, t] = await Promise.all([
    fetch('/api/config').then(r => r.json()),
    fetch('/api/times').then(r => r.json()),
  ]);
  CFG = c.config;
  TIMES = t;
  NOTIFY = clone(CFG.notify || {});
  $('#dot').className = 'dot ' + (t.configured ? 'ok' : '');

  if (!t.configured) {
    $('#setup').classList.remove('hidden');
    $('#times-wrap').classList.add('hidden');
    $('#reminders-wrap').classList.add('hidden');
    $('#schedule-wrap').classList.add('hidden');
    $('#sub').textContent = 'no location configured yet';
    $('#loc-tz').value = c.timezone_detected || '';
    $('#method').value = CFG.method || 'diyanet';
    return;
  }

  $('#setup').classList.remove('hidden');
  $('#times-wrap').classList.remove('hidden');
  $('#reminders-wrap').classList.remove('hidden');
  $('#schedule-wrap').classList.remove('hidden');

  const loc = t.location || {};
  $('#sub').textContent = `${loc.t || ''} · ${t.date} · ${t.method} · qibla ${t.qibla}°`;
  $('#loc-name').value = loc.t || '';
  $('#loc-lat').value = loc.lat ?? '';
  $('#loc-lon').value = loc.lon ?? '';
  $('#loc-cc').value = loc.cc || '';
  $('#loc-tz').value = t.timezone || '';
  $('#method').value = t.method || 'diyanet';
  $('#voice-on').checked = !!(t.voice && t.voice.on);

  renderTimes();
  renderReminders();
  renderSchedule();
  loadMonth();
  loadVoices();
  startCountdown();

  const svc = t.service || {};
  $('#meta').innerHTML = [
    `notifier: <span class="${svc.active === 'active' ? 'ok' : ''}">${esc(svc.active)}</span>`,
    `config: <span style="font-family:var(--mono)">${esc(c.path)}</span>`,
    `next prayer ${t.next ? esc(t.next.time) + (t.next.tomorrow ? ' (tomorrow)' : '') : '—'}`,
  ].join(' &nbsp;·&nbsp; ');
}

/* --------------------------------------------------------------- render */
function renderTimes() {
  const nowKey = TIMES.next && !TIMES.next.tomorrow ? TIMES.next.key : null;
  $('#times').innerHTML = (TIMES.prayers || []).map(p => {
    const v = (TIMES.times || {})[p.key] || '—';
    return `<div class="tcard ${p.key === nowKey ? 'now' : ''}">
      <div class="n">${esc(p.tr)}<br><span class="muted">${esc(p.en)}</span></div>
      <div class="t">${esc(v)}</div></div>`;
  }).join('');
}

function startCountdown() {
  if (TARGET) clearInterval(TARGET);
  if (!TIMES || !TIMES.next) { $('#next').textContent = ''; return; }
  const t0 = Date.now() + Math.max(0, TIMES.next.in_minutes) * 60000;
  const tick = () => {
    const left = t0 - Date.now();
    if (left <= 0) { $('#next').textContent = 'updating…'; clearInterval(TARGET); load(); return; }
    const m = Math.floor(left / 60000), s = Math.floor(left % 60000 / 1000);
    const nm = (TIMES.prayers.find(p => p.key === TIMES.next.key) || {}).tr || TIMES.next.key;
    $('#next').textContent = `${nm} in ${m}:${String(s).padStart(2, '0')}`;
  };
  tick();
  TARGET = setInterval(tick, 1000);
}

function renderReminders() {
  const body = $('#reminders').querySelector('tbody');
  body.innerHTML = PRAYER_KEYS.map(key => {
    const items = NOTIFY[key] || [];
    const ontime = items.find(i => i.when === 'ontime' && i.on);
    const chips = [`<span class="chip ontime ${ontime ? 'on' : ''}" data-key="${key}" data-when="ontime" data-min="0">on time</span>`];
    for (const m of PRESETS) {
      const it = items.find(i => i.when === 'before' && Number(i.minutes) === m);
      chips.push(`<span class="chip ${it && it.on ? 'on' : ''}" data-key="${key}" data-when="before" data-min="${m}">−${m}</span>`);
    }
    const custom = items.filter(i => i.when === 'before' && !PRESETS.includes(Number(i.minutes)));
    for (const c of custom) {
      chips.push(`<span class="chip on" data-key="${key}" data-when="before" data-min="${c.minutes}">−${c.minutes}</span>`);
    }
    return `<tr><td>${esc(key)}</td>
      <td><span class="chips">${chips.slice(0, 1).join('')}</span></td>
      <td><span class="chips">${chips.slice(1).join('')}</span></td>
      <td class="muted">${items.filter(i => i.on).length} active</td></tr>`;
  }).join('');
}

function renderSchedule() {
  const body = $('#schedule').querySelector('tbody');
  const rows = TIMES.schedule || [];
  body.innerHTML = rows.length ? rows.map(r => `<tr>
      <td class="t">${esc(r.fire.replace('T', ' '))}</td>
      <td>${esc(r.label)}</td>
      <td>${r.minutes_before ? '−' + r.minutes_before + ' min' : 'on time'}</td>
      <td class="muted">${esc(r.message)}</td></tr>`).join('')
    : '<tr><td colspan="4" class="muted">no reminders enabled</td></tr>';
}

async function loadMonth() {
  const now = new Date();
  const d = await fetch(`/api/month?year=${now.getFullYear()}&month=${now.getMonth() + 1}`).then(r => r.json());
  const prayers = (TIMES && TIMES.prayers) || [];
  const head = $('#month').querySelector('thead tr');
  head.innerHTML = '<th>date</th><th>day</th>' + prayers.map(p => `<th>${esc(p.tr)}</th>`).join('');
  const body = $('#month').querySelector('tbody');
  body.innerHTML = (d.days || []).map(row => `<tr>
      <td class="t">${esc(row.date)}</td><td class="muted">${esc(row.weekday)}</td>
      ${prayers.map(p => `<td class="t">${esc((row.times || {})[p.key] || '—')}</td>`).join('')}
    </tr>`).join('');
}

async function loadVoices() {
  const engine = (CFG && CFG.voice_engine) || 'spd-say';
  const d = await fetch(`/api/voices?engine=${encodeURIComponent(engine)}`).then(r => r.json());
  const body = $('#voices').querySelector('tbody');
  body.innerHTML = (d.voices || []).length
    ? d.voices.map(v => `<tr><td>${esc(v.engine)}</td><td>${esc(v.lang)}</td>
        <td>${esc(v.name)} <button class="pick" data-name="${esc(v.name)}">use</button></td></tr>`).join('')
    : '<tr><td colspan="3" class="muted">no voices found (install speech-dispatcher or espeak-ng)</td></tr>';
}

/* --------------------------------------------------------------- actions */
async function saveConfig(patch) {
  const body = Object.assign(clone(CFG), patch);
  const r = await fetch('/api/config', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  const d = await r.json();
  if (!d.ok) { alert('save failed'); return; }
  CFG = d.config;
  await load();
}

function toggleReminder(key, when, minutes) {
  const items = NOTIFY[key] || (NOTIFY[key] = []);
  const idx = items.findIndex(i => i.when === when && Number(i.minutes) === Number(minutes));
  if (idx >= 0) items[idx].on = !items[idx].on;
  else items.push({ on: true, when, minutes: Number(minutes) });
  renderReminders();
}

document.addEventListener('click', async (ev) => {
  const chip = ev.target.closest('.chip[data-key]');
  if (chip) return toggleReminder(chip.dataset.key, chip.dataset.when, chip.dataset.min);

  if (ev.target.id === 'save-loc') {
    const lat = parseFloat($('#loc-lat').value), lon = parseFloat($('#loc-lon').value);
    if (Number.isNaN(lat) || Number.isNaN(lon)) return alert('latitude and longitude are required');
    return saveConfig({
      location: { t: $('#loc-name').value, e: $('#loc-name').value,
                  cc: $('#loc-cc').value || 'TR', lat, lon },
      timezone: $('#loc-tz').value || null,
      method: $('#method').value,
    });
  }
  if (ev.target.id === 'save-notify') {
    await saveConfig({ notify: NOTIFY });
    $('#notify-msg').textContent = 'saved — the notifier picks it up within a minute';
    return;
  }
  if (ev.target.id === 'test') {
    const d = await fetch('/api/test', { method: 'POST' }).then(r => r.json());
    if (!d.ok) alert('test failed: ' + (d.detail || ''));
    return;
  }
  if (ev.target.id === 'reload') return load();
  if (ev.target.classList.contains('pick')) {
    return saveConfig({ voice: true, voice_name: ev.target.dataset.name });
  }
});

$('#voice-on').addEventListener('change', (e) => saveConfig({ voice: e.target.checked }));

load().catch(e => { $('#sub').textContent = 'failed to load: ' + e; });
setInterval(() => { if (document.visibilityState === 'visible') load(); }, 120000);
