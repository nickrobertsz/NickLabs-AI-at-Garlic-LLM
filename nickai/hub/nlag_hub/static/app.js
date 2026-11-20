'use strict';
const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const gb = b => b ? (b / 1e9).toFixed(2) + ' GB' : '—';
const ago = t => { if (!t) return '—'; const s = Math.max(0, Math.round(Date.now() / 1000 - t)); return s < 90 ? s + 's ago' : s < 5400 ? Math.round(s / 60) + 'm ago' : Math.round(s / 3600) + 'h ago'; };
const VIEWS = ['consoles', 'models', 'playground', 'agents', 'logs', 'settings'];
let S = null, ollama = null, notice = '', lastLog = '';
const sel = {};   // per-console model choice in the cards

function say(msg) { $('notice').textContent = msg; }
$('notice').onclick = () => say('');
const authHeader = () => { try { const k = sessionStorage.getItem('nlag.key'); return k ? {Authorization: 'Bearer ' + k} : {}; } catch (_) { return {}; } };
async function api(path, opt = {}) {
  let r = await fetch(path, {...opt, headers: {'Content-Type': 'application/json', ...authHeader(), ...(opt.headers || {})}});
  if (r.status === 401) {   // an API key is set on this hub: ask once per browser tab
    const k = prompt('This hub requires an API key:'); if (k) { try { sessionStorage.setItem('nlag.key', k); } catch (_) { /* private mode */ } r = await fetch(path, {...opt, headers: {'Content-Type': 'application/json', ...authHeader(), ...(opt.headers || {})}}); }
  }
  let d = {}; try { d = await r.json(); } catch (_) { /* empty body */ }
  if (!r.ok) throw new Error(d.error?.message || d.error || `HTTP ${r.status}`);
  return d;
}
const post = (p, b) => api(p, {method: 'POST', body: JSON.stringify(b ?? {})});
const act = async (fn, ok) => { try { const r = await fn(); if (ok) say(ok); refresh(); return r; } catch (e) { say(e.message); } };

// ------------------------------------------------------------------ routing
function route() {
  const v = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'consoles';
  for (const s of document.querySelectorAll('[data-view]')) s.hidden = s.dataset.view !== v;
  for (const a of document.querySelectorAll('#tabs a')) a.toggleAttribute('aria-current', a.getAttribute('href') === '#' + v), a.ariaCurrent = a.getAttribute('href') === '#' + v ? 'page' : null;
  if (v === 'models' && !ollama) loadOllama();
  if (v === 'logs') loadLogs();
  render();
}
addEventListener('hashchange', route);
const view = () => location.hash.slice(1) || 'consoles';

// ------------------------------------------------------------------ state
async function refresh() {
  try { S = await api('/hub/v1/state'); } catch (e) { $('subtitle').textContent = 'Cannot reach the hub: ' + e.message; return; }
  render();
}
function render() {
  if (!S) return;
  $('subtitle').textContent = `${S.consoles.length} console(s) · ${S.app} ${S.version}`;
  renderConsoles(); if (view() === 'models') renderModels(); if (view() === 'playground') renderPlayground(); if (view() === 'agents') renderAgents(); if (view() === 'settings') renderSettings();
}

function consoleCard(c) {
  const p = c.pending, names = c.models.map(m => m.name);
  const chosen = names.includes(sel[c.id]) ? sel[c.id] : (names.includes(c.loaded) ? c.loaded : names[0]);
  const busy = Boolean(p), off = !c.online;
  const state = off ? `<span class="badge bad">offline</span>` : p ? `<span class="badge warn">${p.action === 'load' ? 'loading' : 'unloading'}</span>` : c.loaded ? `<span class="badge ok">ready</span>` : `<span class="badge">idle</span>`;
  const line = off ? esc(c.error || 'not reachable: is the app running on the console?') : p ? (p.action === 'load' ? `Loading ${esc(p.model)}… (a switch restarts the app first; up to 2 minutes)` : 'Unloading… the app restarts to free the GPU') :
    c.loaded ? `Loaded: <b>${esc(c.loaded)}</b>${c.tokens_per_second ? ' · ' + Number(c.tokens_per_second).toFixed(1) + ' tok/s' : ''} · GPU ${gb(c.gpu_reserved_bytes)}` : 'No model loaded: GPU memory is free.';
  const ag = c.agents || {};
  const agents = ag.speaking ? `<p class="muted">Speaking: <b>${esc(ag.speaking)}</b>${Object.keys(ag.queued || {}).length ? ' · waiting: ' + Object.entries(ag.queued).map(([a, n]) => esc(a) + ' ×' + n).join(', ') : ''}</p>` : '';
  const opts = c.models.map(m => `<option value="${esc(m.name)}"${m.name === chosen ? ' selected' : ''}>${esc(m.name)}${m.size ? ' · ' + gb(m.size) : ''}${m.loaded ? ' · loaded' : ''}</option>`).join('');
  return `<div class="card" data-console="${esc(c.id)}"><h3><span>${esc(c.name)} <small class="muted">${esc(c.host)}</small></span>${state}</h3><p class="muted">${line}</p>${agents}
  <div class="row"><label>Model<select data-sel="${esc(c.id)}"${busy || off ? ' disabled' : ''}>${opts}</select></label>
  <button data-act="load" data-c="${esc(c.id)}"${busy || off || !chosen || chosen === c.loaded ? ' disabled' : ''}>Load</button>
  <button class="quiet" data-act="unload" data-c="${esc(c.id)}"${busy || off || !c.loaded ? ' disabled' : ''}>Unload</button></div>
  <details><summary>More</summary><div class="row" style="margin-top:8px"><button class="quiet" data-act="check" data-c="${esc(c.id)}">Check connection</button>
  <button class="quiet" data-act="install" data-c="${esc(c.id)}"${S.package.available ? '' : ' disabled'}>Send app package</button><button class="danger" data-act="remove" data-c="${esc(c.id)}">Remove</button></div>
  <p class="muted" data-check="${esc(c.id)}"></p><p class="muted">App ${esc(c.build || '—')} · up ${c.uptime_s ? Math.round(c.uptime_s / 60) + ' min' : '—'} · ${S.capture[c.id]?.klog_connected ? 'logs recording' : 'logs: waiting for the console log port'}</p></details></div>`;
}
function renderConsoles() {
  const m = S.metrics, online = S.consoles.filter(c => c.online).length;
  const speeds = S.consoles.map(c => c.tokens_per_second).filter(Boolean);
  $('stats').innerHTML = [['Consoles online', `${online} / ${S.consoles.length}`], ['Models loaded', S.consoles.filter(c => c.loaded).length], ['Speaking now', S.consoles.filter(c => c.agents?.speaking).length],
    ['Decode speed', speeds.length ? (speeds.reduce((a, b) => a + b) / speeds.length).toFixed(1) + ' tok/s' : '—'], ['Median time to first token', m.ttft_ms_median != null ? Math.round(m.ttft_ms_median) + ' ms' : '—'], ['Requests OK / failed (24h)', `${m.ok} / ${m.failed}`]]
    .map(([k, v]) => `<div class="stat"><span>${k}</span><strong>${v}</strong></div>`).join('');
  $('setup-guide').hidden = S.consoles.length > 0;
  if (!S.consoles.length) $('setup-guide').innerHTML = `<h2>Welcome. Let's add your first console.</h2><ol><li>On the PlayStation 4 (jailbroken, GoldHEN): enable the <b>FTP server</b> (port 2121) and keep the console on the same network as this computer.</li>
    <li>Open <b>Add a console</b> below and enter its IP address (Settings → Network → View Connection Status on the PS4).</li><li>Press <b>Send app package</b> on its card, then on the PS4 open <b>Settings → Debug Settings → Game → Package Installer</b> and install “NickLabs AI on Garlic LLM”. Start it.</li>
    <li>Add a model on the <a href="#models">Models</a> page and send it to the console, then press Load.</li></ol>`;
  const dsc = S.discovery || {}, cands = dsc.candidates || [];
  $('discovery').hidden = !(cands.length || (dsc.networks || []).length && !S.consoles.length);
  $('discovery').innerHTML = `${cands.length ? `<p><b>Possible PlayStation 4 found on your network</b> (the app is not installed there yet):</p>` + cands.map(c => `<div class="row"><code>${esc(c.host)}</code><button data-act="adopt" data-host="${esc(c.host)}">Add this console</button></div>`).join('') : ''}
    <p class="muted">Looked at ${esc((dsc.networks || []).join(', ') || 'no network')}${dsc.time ? ' ' + ago(dsc.time) : ''}. Consoles that run the app are added automatically. <button class="quiet" data-act="scan">Scan now</button> <button class="quiet" data-act="forget">Re-add consoles I removed</button></p>`;
  if (!$('console-cards').contains(document.activeElement)) $('console-cards').innerHTML = S.consoles.map(consoleCard).join('');
}

document.addEventListener('change', e => { const s = e.target.closest?.('[data-sel]'); if (s) { sel[s.dataset.sel] = s.value; renderConsoles(); } });
document.addEventListener('click', e => {
  const b = e.target.closest('[data-act]'); if (!b) return;
  const c = b.dataset.c, card = b.closest('[data-console]');
  switch (b.dataset.act) {
    case 'load': return void act(() => post(`/hub/v1/consoles/${encodeURIComponent(c)}/load`, {model: card.querySelector('select').value}), 'Loading started.');
    case 'unload': return void act(() => post(`/hub/v1/consoles/${encodeURIComponent(c)}/unload`), 'Unloading: the app restarts on the console.');
    case 'remove': if (confirm('Remove this console from the hub? Nothing is deleted on the console.')) act(() => api(`/hub/v1/consoles/${encodeURIComponent(c)}`, {method: 'DELETE'}), 'Console removed.'); return;
    case 'install': return void act(() => post(`/hub/v1/consoles/${encodeURIComponent(c)}/install-pkg`), 'Sending the package: see Models → Jobs. Then install it on the console with Package Installer.');
    case 'check': return void (async () => { const r = await api(`/hub/v1/consoles/${encodeURIComponent(c)}/check`).catch(x => ({error: x.message})); const el = document.querySelector(`[data-check="${CSS.escape(c)}"]`); if (el) el.textContent = r.error || ['ftp', 'app_api', 'klog'].map(k => `${k}: ${r[k].ok ? 'ok' : 'NOT reachable (' + r[k].detail + ')'}`).join(' · '); })();
    case 'push': return void act(() => post(`/hub/v1/library/${encodeURIComponent(b.dataset.m)}/push`, {console: b.closest('tr').querySelector('select').value}), 'Sending the model: see Jobs.');
    case 'del': if (confirm('Delete this model from your computer?')) act(() => api(`/hub/v1/library/${encodeURIComponent(b.dataset.m)}`, {method: 'DELETE'}), 'Deleted.'); return;
    case 'imp-local': return void importModel('ollama-local:' + b.dataset.ref);
    case 'cancel': return void act(() => post(`/hub/v1/jobs/${b.dataset.j}/cancel`));
    case 'scan': return void act(() => post('/hub/v1/discover', {}), 'Scan finished.');
    case 'forget': return void act(() => post('/hub/v1/discover', {forget_removed: true}), 'Consoles you removed are searched for again.');
    case 'adopt': return void act(() => post('/hub/v1/consoles', {host: b.dataset.host}), 'Console added: now send it the app package (More -> Send app package).');
  }
});
$('add-form').onsubmit = async e => {
  e.preventDefault();
  const r = await act(() => post('/hub/v1/consoles', {host: $('add-host').value.trim(), name: $('add-name').value.trim() || undefined, api_port: +$('add-api').value, ftp_port: +$('add-ftp').value, klog_port: +$('add-klog').value}), 'Console added.');
  if (r) { e.target.reset(); const p = r.probe; say(`Added ${r.console.name}. FTP ${p.ftp.ok ? 'ok' : 'NOT reachable'} · app ${p.app_api.ok ? 'running' : 'not running yet'} · log port ${p.klog.ok ? 'ok' : 'not reachable'}`); }
};

// ------------------------------------------------------------------ models
async function loadOllama() { try { ollama = await api('/hub/v1/ollama'); } catch (e) { ollama = {stores: [], models: []}; } if (view() === 'models') renderModels(); }
function targets() { return [...$('imp-targets').selectedOptions].map(o => o.value); }
async function importModel(source) { await act(() => post('/hub/v1/models/import', {source, push_to: targets()}), 'Started: see Jobs below.'); }
$('imp-ollama').onsubmit = e => { e.preventDefault(); importModel('ollama:' + $('imp-ollama-ref').value.trim()); };
$('imp-hf').onsubmit = e => { e.preventDefault(); importModel('hf:' + $('imp-hf-ref').value.trim()); };
function renderModels() {
  const tg = $('imp-targets'), keep = new Set(targets());
  const sig = JSON.stringify(S.consoles.map(c => [c.id, c.name]));
  if (tg.dataset.sig !== sig) { tg.innerHTML = S.consoles.map(c => `<option value="${esc(c.id)}"${keep.has(c.id) ? ' selected' : ''}>${esc(c.name)}</option>`).join(''); tg.dataset.sig = sig; }
  $('library').innerHTML = S.library.length ? `<table><thead><tr><th>Model</th><th>Size</th><th>Origin</th><th>Send to</th></tr></thead><tbody>` + S.library.map(m =>
    `<tr><td><b>${esc(m.id)}</b><br><small class="muted">${esc(m.arch)}</small></td><td>${gb(m.bytes)}</td><td>${esc(m.origin)}</td><td><select>${S.consoles.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}</select> <button data-act="push" data-m="${esc(m.id)}"${S.consoles.length ? '' : ' disabled'}>Send</button> <button class="danger" data-act="del" data-m="${esc(m.id)}">Delete</button></td></tr>`).join('') + '</tbody></table>' : '<p class="muted">Nothing here yet. Add a model below.</p>';
  if (ollama) $('ollama-local').innerHTML = ollama.models.length ? `<table><tbody>` + ollama.models.map(m => `<tr><td>${esc(m.ref)}<br><small class="muted">${gb(m.bytes)} · ${esc(m.arch || '?')}${m.supported ? '' : ' · ' + esc(m.reason)}</small></td><td><button data-act="imp-local" data-ref="${esc(m.ref)}"${m.supported ? '' : ' disabled'}>Convert and use</button></td></tr>`).join('') + '</tbody></table>'
    : `<p class="muted">No Ollama models found on this computer${ollama.stores.length ? '' : ' (the hub could not see an Ollama folder: see the README "Using your Ollama models")'}.</p>`;
  const jobs = S.jobs;
  $('jobs').innerHTML = jobs.length ? jobs.map(j => `<div style="margin-bottom:10px"><div class="row" style="justify-content:space-between"><span><b>${esc(j.title)}</b> <span class="badge ${j.state === 'done' ? 'ok' : j.state === 'failed' ? 'bad' : j.state === 'running' ? 'warn' : ''}">${esc(j.state)}</span></span>${j.state === 'running' || j.state === 'queued' ? `<button class="quiet" data-act="cancel" data-j="${esc(j.id)}">Cancel</button>` : ''}</div>
    <div class="bar"><i style="width:${Math.round(j.progress * 100)}%"></i></div><small class="muted">${esc(j.message)}</small></div>`).join('') : '<p class="muted">No jobs yet.</p>';
}

// ------------------------------------------------------------------ agents
function renderAgents() {
  $('agent-cards').innerHTML = S.consoles.map(c => { const a = c.agents || {}; return `<div class="card"><h3>${esc(c.name)}<span class="badge ${a.speaking ? 'ok' : ''}">${a.speaking ? 'speaking' : 'quiet'}</span></h3><p class="muted">${c.loaded ? esc(c.loaded) : 'no model'}</p>
    <p>${a.speaking ? `<b>${esc(a.speaking)}</b> has been speaking for ${a.speaking_for_s}s` : 'Nobody is speaking.'}</p><p class="muted">Waiting: ${Object.keys(a.queued || {}).length ? Object.entries(a.queued).map(([k, n]) => esc(k) + ' ×' + n).join(', ') : 'nobody'}</p></div>`; }).join('') || '<p class="muted">Add a console first.</p>';
  const seen = Object.entries(S.agents.seen).sort((x, y) => y[1].last - x[1].last);
  $('agent-table').innerHTML = seen.length ? `<table><thead><tr><th>Agent</th><th>Console</th><th>Requests</th><th>Last request</th></tr></thead><tbody>${seen.map(([a, v]) => `<tr><td>${esc(a)}</td><td>${esc(v.console)}</td><td>${v.requests}</td><td>${ago(v.last)}</td></tr>`).join('')}</tbody></table>` : '<p class="muted">No agent has spoken yet.</p>';
}

// ------------------------------------------------------------------ playground
let panes = [], paneN = 0, sig = '';
function modelOptions() {
  const o = [], names = new Set();
  for (const c of S.consoles) for (const m of c.models) { names.add(m.name); o.push({v: `${m.name}@${c.id}`, label: `${c.name} · ${m.name} (${!c.online ? 'offline' : c.pending ? 'busy' : m.loaded ? 'loaded' : 'not loaded'})`, console: c.id, name: m.name, loaded: m.loaded}); }
  for (const n of names) o.push({v: n, label: `Any console · ${n} (automatic)`, name: n});
  return o;
}
function addPane() {
  paneN++; const id = 'p' + paneN;
  const cs = S?.consoles || [], pick = cs[(paneN - 1) % Math.max(cs.length, 1)];
  panes.push({id, model: '', agent: 'agent-' + paneN, hist: [], busy: null, hint: pick?.id});
  paintPanes(true);
}
function paneOf(el) { return panes.find(p => p.id === el.closest('.pane').dataset.pane); }
function paintPanes(force) {
  const opts = S ? modelOptions() : [], s2 = JSON.stringify(opts.map(o => o.label)) + panes.map(p => p.id).join();
  const host = $('panes');
  if (force || s2 !== sig) {
    sig = s2;
    for (const p of panes) {
      let el = host.querySelector(`[data-pane="${p.id}"]`);
      if (!el) { el = document.createElement('div'); el.className = 'pane'; el.dataset.pane = p.id; el.innerHTML = `<div class="row"><label class="grow">Model<select data-p="model"></select></label><label>Agent<input data-p="agent" size="12"></label></div>
        <div class="row"><button type="button" data-p="load">Load</button><button type="button" class="quiet" data-p="unload">Unload</button><button type="button" class="quiet" data-p="close">Close</button><span class="muted" data-p="state"></span></div><div class="log"></div><div class="pane-stats" data-p="stats"></div>`; host.append(el); el.querySelector('[data-p=agent]').value = p.agent; }
      const select = el.querySelector('[data-p=model]'), keep = p.model || select.value;
      select.innerHTML = opts.map(o => `<option value="${esc(o.v)}">${esc(o.label)}</option>`).join('');
      const pref = opts.find(o => o.console === p.hint && (o.loaded)) || opts.find(o => o.console === p.hint) || opts[0];
      select.value = opts.some(o => o.v === keep) ? keep : pref?.v ?? ''; p.model = select.value;
    }
    for (const el of [...host.children]) if (!panes.some(p => p.id === el.dataset.pane)) el.remove();
  }
  for (const p of panes) {
    const el = host.querySelector(`[data-pane="${p.id}"]`); if (!el) continue;
    const o = opts.find(x => x.v === p.model), c = o?.console && S.consoles.find(x => x.id === o.console);
    el.querySelector('[data-p=load]').hidden = el.querySelector('[data-p=unload]').hidden = !c;
    if (c) {
      el.querySelector('[data-p=load]').disabled = !c.online || Boolean(c.pending) || c.loaded === o.name; el.querySelector('[data-p=unload]').disabled = !c.online || Boolean(c.pending) || !c.loaded;
      el.querySelector('[data-p=load]').textContent = 'Load on ' + c.name; el.querySelector('[data-p=unload]').textContent = 'Unload ' + c.name;
      el.querySelector('[data-p=state]').textContent = !c.online ? 'offline' : c.pending ? (c.pending.action === 'load' ? 'loading ' + c.pending.model + '…' : 'unloading…') : c.loaded ? 'loaded: ' + c.loaded : 'no model loaded';
    } else el.querySelector('[data-p=state]').textContent = o ? 'the hub picks the console' : '';
  }
  paneWarning();
  $('send').disabled = !panes.length || panes.every(p => p.busy);
}
function paneWarning() {
  const msgs = [], by = {};
  for (const p of panes) { (by[p.model] = by[p.model] || []).push(p); }
  for (const [m, ps] of Object.entries(by)) if (m && ps.length > 1) msgs.push(`${ps.length} chats use the same model (${m.replace('@', ' on ')}): a console answers one request at a time, so they take turns instead of speaking together.`);
  const onConsole = {};
  for (const p of panes) { const at = p.model.split('@')[1]; if (at) (onConsole[at] = onConsole[at] || new Set()).add(p.model.split('@')[0]); }
  for (const [c, set] of Object.entries(onConsole)) if (set.size > 1) msgs.push(`Several models are chosen on console ${c}, but it holds one model at a time: loading one replaces the other.`);
  const agents = {}; for (const p of panes) agents[p.agent] = (agents[p.agent] || 0) + 1;
  for (const [a, n] of Object.entries(agents)) if (n > 1) msgs.push(`${n} chats are the same agent (${a}): give each chat its own agent name to see agents take turns.`);
  $('pw').hidden = !msgs.length; $('pw').textContent = msgs.join(' ');
}
$('panes').addEventListener('change', e => { const p = paneOf(e.target); if (!p) return; if (e.target.dataset.p === 'model') { p.model = e.target.value; p.hist = []; e.target.closest('.pane').querySelector('.log').textContent = ''; } if (e.target.dataset.p === 'agent') p.agent = e.target.value.replace(/[^A-Za-z0-9._:@-]/g, '').slice(0, 64) || 'agent'; paintPanes(); });
$('panes').addEventListener('click', e => {
  const b = e.target.closest('[data-p]'); if (!b) return; const p = paneOf(b); const o = modelOptions().find(x => x.v === p.model);
  if (b.dataset.p === 'close') { panes = panes.filter(x => x !== p); paintPanes(true); }
  if (b.dataset.p === 'load' && o?.console) act(() => post(`/hub/v1/consoles/${encodeURIComponent(o.console)}/load`, {model: o.name}), 'Loading started.');
  if (b.dataset.p === 'unload' && o?.console) act(() => post(`/hub/v1/consoles/${encodeURIComponent(o.console)}/unload`), 'Unloading.');
});
$('addpane').onclick = addPane;
$('clear').onclick = () => { for (const p of panes) { if (p.busy) continue; p.hist = []; $('panes').querySelector(`[data-pane="${p.id}"] .log`).textContent = ''; } };
$('stop').onclick = () => panes.forEach(p => p.busy?.abort());
function bubble(el, role, text) { const b = document.createElement('div'); b.className = 'bubble ' + role; b.textContent = text; const l = el.querySelector('.log'); l.append(b); l.scrollTop = l.scrollHeight; return b; }
async function runPane(p, prompt, max) {
  const el = $('panes').querySelector(`[data-pane="${p.id}"]`), o = modelOptions().find(x => x.v === p.model), stats = el.querySelector('[data-p=stats]');
  bubble(el, 'user', prompt);
  if (o?.console && !o.loaded) { bubble(el, 'assistant', `[${o.name} is not loaded on this console. Press Load, wait until it says loaded, then send again.]`); return; }
  p.hist.push({role: 'user', content: prompt}); const out = bubble(el, 'assistant', ''); const ctl = new AbortController(); p.busy = ctl; paintPanes();
  const t0 = performance.now(); let first = null, usage = null, served = '', failed = false;
  try {
    const r = await fetch('/v1/chat/completions', {method: 'POST', signal: ctl.signal, headers: {'Content-Type': 'application/json', ...authHeader(), 'X-Agent-Id': p.agent}, body: JSON.stringify({model: p.model, messages: p.hist, max_tokens: max, stream: true, stream_options: {include_usage: true}})});
    if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.error?.message || 'HTTP ' + r.status); }
    served = r.headers.get('X-NickLabs-Console') || ''; stats.textContent = 'answered by ' + served + '…';
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = '';
    for (;;) {
      const {value, done} = await rd.read(); if (done) break; buf += dec.decode(value, {stream: true}); let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 2); if (!line.startsWith('data: ') || line === 'data: [DONE]') continue;
        const j = JSON.parse(line.slice(6)); if (j.error) { failed = true; out.textContent += `\n[${j.error.message || j.error}]`; continue; } if (j.usage) usage = j.usage;
        const piece = j.choices?.[0]?.delta?.content; if (piece) { if (first === null) first = performance.now(); out.textContent += piece; el.querySelector('.log').scrollTop = 1e9; }
      }
    }
    const end = performance.now(), n = usage?.completion_tokens;
    stats.textContent = `${served} · first token ${first ? Math.round(first - t0) + ' ms' : '—'} · ${n && first && end > first ? (n / ((end - first) / 1000)).toFixed(1) + ' tok/s' : '—'} · ${((end - t0) / 1000).toFixed(1)} s`;
    if (failed) p.hist.pop(); else p.hist.push({role: 'assistant', content: out.textContent});
  } catch (e) { p.hist.pop(); out.textContent += e.name === 'AbortError' ? '\n[stopped]' : `\n[${e.message}]`; }
  finally { p.busy = null; paintPanes(); }
}
$('chat').onsubmit = async e => {
  e.preventDefault(); const prompt = $('prompt').value; $('prompt').value = ''; $('stop').disabled = false;
  await Promise.all(panes.filter(p => !p.busy).map(p => runPane(p, prompt, +$('tokens').value))); $('stop').disabled = true; refresh();
};
function renderPlayground() { if (!panes.length) { addPane(); addPane(); } else paintPanes(); }

// ------------------------------------------------------------------ logs / settings
async function loadLogs() {
  try {
    const d = await api('/hub/v1/logs'); const cur = $('log-file').value;
    const names = d.files.filter(f => !f.path.endsWith('.tmp')).map(f => f.path);
    if ($('log-file').dataset.sig !== names.join()) { $('log-file').innerHTML = names.map(n => `<option>${esc(n)}</option>`).join(''); $('log-file').dataset.sig = names.join(); if (names.includes(cur)) $('log-file').value = cur; else { const ev = names.filter(n => /events-/.test(n)).pop(); if (ev) $('log-file').value = ev; } }
    $('capture-health').innerHTML = Object.entries(d.capture).map(([c, s]) => `<p class="muted"><b>${esc(c)}</b>: console log ${s.klog_connected ? '<span class="badge ok">recording</span>' : '<span class="badge bad">not connected</span>'} · ${s.klog_lines || 0} lines · FTP ${s.ftp_ok ? 'ok' : 'no'} · app ${s.api_up ? 'up' : 'down'}</p>`).join('');
    await tailLog();
  } catch (e) { say(e.message); }
}
async function tailLog() { const f = $('log-file').value; if (!f) { $('log-view').textContent = 'No logs yet.'; return; } try { const d = await api(`/hub/v1/logs/tail?path=${encodeURIComponent(f)}&n=${$('log-n').value}`); const v = d.lines.join('\n'); if (v !== lastLog) { const at = $('log-view').scrollHeight - $('log-view').scrollTop - $('log-view').clientHeight < 40; $('log-view').textContent = v; lastLog = v; if (at || $('log-follow').checked) $('log-view').scrollTop = 1e9; } } catch (e) { $('log-view').textContent = e.message; } }
$('log-file').onchange = tailLog; $('log-n').onchange = tailLog;
let toolTab = 'openai', toolKey = 'local', hubUrl = '', newKeyShown = '';
function hubBase() { return ($('hub-url').value || S?.public_url || location.origin).replace(/\/+$/, ''); }
function snippet() {
  const u = hubBase(), k = toolKey || 'local', m = $('tool-model').value || 'qwen3.5-4b-garlic', win = navigator.platform.startsWith('Win');
  const exp = (n, v) => win ? `$env:${n} = "${v}"` : `export ${n}="${v}"`;
  const lines = {
    openai: [exp('OPENAI_BASE_URL', u + '/v1'), exp('OPENAI_API_KEY', k), '', `openai api chat.completions.create -m ${m} -g user "Hello"`, '', '# Python SDK:', `# from openai import OpenAI; c = OpenAI(base_url="${u}/v1", api_key="${k}")`, `# c.chat.completions.create(model="${m}", messages=[{"role":"user","content":"Hello"}])`],
    claude: [exp('ANTHROPIC_BASE_URL', u), exp('ANTHROPIC_AUTH_TOKEN', k), exp('ANTHROPIC_MODEL', m), exp('ANTHROPIC_DEFAULT_SONNET_MODEL', m), exp('ANTHROPIC_DEFAULT_OPUS_MODEL', m), exp('ANTHROPIC_DEFAULT_HAIKU_MODEL', m), exp('ANTHROPIC_SMALL_FAST_MODEL', m),
      exp('CLAUDE_CODE_MAX_CONTEXT_TOKENS', '16384'), exp('MAX_THINKING_TOKENS', '0'), exp('CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC', '1'), '', 'claude'],
    ollama: [exp('OLLAMA_HOST', u), '', `ollama list`, `ollama run ${m} "Hello"`, '', '# Ollama clients do not send a key: leave "Require a key" off for them, or use the OpenAI/Claude settings above.'],
    curl: [`curl ${u}/v1/chat/completions \\`, `  -H "Authorization: Bearer ${k}" -H "X-Agent-Id: my-agent" -H "Content-Type: application/json" \\`, `  -d '{"model":"${m}","messages":[{"role":"user","content":"Hello"}]}'`, '', `# pick a console:  "model":"${m}@ps4-1"`]};
  return lines[toolTab].join('\n');
}
function renderTools() {
  if (!S) return;
  const models = [...new Set(S.consoles.flatMap(c => c.models.map(m => m.name)))], sel2 = $('tool-model');
  if (sel2.dataset.sig !== models.join()) { const keep = sel2.value; sel2.innerHTML = models.map(n => `<option>${esc(n)}</option>`).join(''); sel2.dataset.sig = models.join(); if (models.includes(keep)) sel2.value = keep; }
  if (!$('hub-url').value) $('hub-url').value = S.public_url || location.origin;
  for (const b of document.querySelectorAll('[data-tool]')) b.classList.toggle('quiet', b.dataset.tool !== toolTab);
  $('tool-snippet').innerHTML = `<pre id="snip">${esc(snippet())}</pre><div class="row"><button type="button" id="copy-snip">Copy</button><span class="muted">${toolKey === 'local' ? 'The key is “local”: any text works on your network.' : 'Using your new key.'}${/localhost|127\.0\.0\.1/.test(hubBase()) ? ' “localhost” only works on this computer: for other computers type this computer\'s address in the box above.' : ''}</span></div>`;
  $('key-table').innerHTML = S.keys.length ? `<table><thead><tr><th>Key</th><th>Name</th><th>Speaks as</th><th>Created</th><th>Last used</th><th></th></tr></thead><tbody>${S.keys.map(k => `<tr><td><code>${esc(k.prefix)}</code></td><td>${esc(k.name)}</td><td>${k.agent ? '<b>' + esc(k.agent) + '</b>' : '<span class="muted">any agent (from the request)</span>'}</td><td>${ago(k.created)}</td><td>${k.last_used ? ago(k.last_used) : 'never'}</td><td><button class="danger" data-act="revoke-key" data-k="${esc(k.id)}">Revoke</button></td></tr>`).join('')}</tbody></table>` : '<p class="muted">No keys yet. You do not need one on your own network.</p>';
}
document.addEventListener('click', e => {
  const t = e.target.closest('[data-tool]'); if (t) { toolTab = t.dataset.tool; renderTools(); }
  if (e.target.id === 'copy-snip') navigator.clipboard?.writeText($('snip').textContent).then(() => say('Copied.'), () => say('Select the text and press Ctrl+C.'));
  const r = e.target.closest('[data-act=revoke-key]'); if (r && confirm('Revoke this key? Tools using it stop working.')) act(() => api('/hub/v1/keys/' + encodeURIComponent(r.dataset.k), {method: 'DELETE'}), 'Key revoked.');
});
$('hub-url').oninput = renderTools; $('tool-model').onchange = renderTools;
$('key-form').onsubmit = async e => { e.preventDefault(); try { const r = await post('/hub/v1/keys', {name: $('key-name').value, agent: $('key-agent').value}); newKeyShown = r.key; $('new-key-value').textContent = r.key; $('new-key').hidden = false; toolKey = r.key; e.target.reset(); say('Key created. Copy it now: it is not shown again.'); refresh(); } catch (x) { say(x.message); } };
$('copy-key').onclick = () => navigator.clipboard?.writeText(newKeyShown).then(() => say('Key copied.'), () => say('Select the key and press Ctrl+C.'));
$('use-key').onclick = () => { toolKey = newKeyShown; $('new-key').hidden = true; newKeyShown = ''; $('new-key-value').textContent = ''; renderTools(); };
function renderSettings() {
  renderTools();
  if (document.activeElement && $('settings-form').contains(document.activeElement)) return;
  const s = S.settings; $('set-nets').value = s.trusted_networks.join('\n'); $('set-routing').value = s.agent_routing; $('set-autoload').value = s.auto_load; $('set-days').value = s.log_retention_days; $('set-mb').value = s.log_max_mb;
  $('set-discovery').checked = s.auto_discovery; $('set-require').checked = s.require_keys; $('set-key').placeholder = s.api_key_set ? '(an admin key is set)' : '(no admin key)';
}
$('settings-form').onsubmit = e => { e.preventDefault(); const b = {auto_discovery: $('set-discovery').checked, require_keys: $('set-require').checked, trusted_networks: $('set-nets').value.split('\n').map(x => x.trim()).filter(Boolean), agent_routing: $('set-routing').value, auto_load: $('set-autoload').value, log_retention_days: +$('set-days').value, log_max_mb: +$('set-mb').value}; if ($('set-key').value) b.api_key = $('set-key').value; act(() => api('/hub/v1/settings', {method: 'PUT', body: JSON.stringify(b)}), 'Saved.'); $('set-key').value = ''; };

addEventListener('error', e => say('Unexpected error: ' + e.message));
route(); refresh(); setInterval(refresh, 2500); setInterval(() => { if (view() === 'logs' && $('log-follow').checked) tailLog(); }, 3000);
