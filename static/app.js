/* Floor UI. Every read comes from the IndexedDB replica — the network is only
 * ever used by sync.js in the background. Nothing here awaits a fetch to render.
 */

import * as docsmod from './docs.js';
import * as idb from './idb.js';
import * as sync from './sync.js';
import { formatReadiness } from './sync_core.js';
import * as clientFilters from './client_filters.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const uid = () => (crypto.randomUUID ? crypto.randomUUID()
  : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`);

const PRIORITY = { must: 'Must meet', target: 'Target', watch: 'Watch' };
let S = { session: null, clients: [], pocs: [], documents: [], meetings: [], updates: [],
          leads: [], outbox: [], receipts: [] };

/* --- toast / modal ------------------------------------------------------- */
function toast(msg, kind) {
  const t = $('toast');
  t.textContent = msg;
  t.className = `toast on${kind ? ` ${kind}` : ''}`;
  clearTimeout(t._t);
  t._t = setTimeout(() => { t.className = 'toast'; }, 2800);
}

function modal({ title, body, okLabel, onOk }) {
  closeModal();
  $('modalHost').innerHTML = `<div class="scrim" id="scrim"><div class="modal">
    <header><h3>${esc(title)}</h3><button class="x" id="mX">&times;</button></header>
    <div class="mbody">${body}</div>
    <footer><button class="btn sub" id="mCancel">Cancel</button>
    ${okLabel ? `<button class="btn" id="mOk">${esc(okLabel)}</button>` : ''}</footer>
  </div></div>`;
  $('mX').onclick = closeModal;
  $('mCancel').onclick = closeModal;
  if ($('mOk')) $('mOk').onclick = () => { if (onOk() !== false) closeModal(); };
  $('scrim').onclick = (e) => { if (e.target.id === 'scrim') closeModal(); };
}
const closeModal = () => { $('modalHost').innerHTML = ''; };

/* --- session ------------------------------------------------------------- */
async function loadSession() {
  const s = await idb.get('meta', 'session');
  if (!s) return null;
  if (s.expires_at && new Date(s.expires_at) < new Date()) return null;
  return s;
}

async function signIn(username, password, remember) {
  const res = await fetch('/api/v1/auth/login', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ username, password, remember, device_label: navigator.userAgent }),
  });
  const body = await res.json();
  if (!res.ok) throw new Error(body.error ? body.error.message : 'Sign-in failed');

  const me = await (await fetch('/api/v1/auth/me')).json();
  const event = (me.events || [])[0];
  if (!event) throw new Error('Your account is not on any event yet — ask an admin to add you.');

  const session = {
    key: 'session', token: body.token, expires_at: body.expires_at,
    user: { id: me.id, name: me.full_name, username: me.username, role: me.role },
    event,
  };
  await idb.put('meta', session);
  return session;
}

async function signOut() {
  try { await fetch('/api/v1/auth/logout', { method: 'POST' }); } catch { /* offline is fine */ }
  await idb.del('meta', 'session');
  location.reload();
}

/* --- replica reads ------------------------------------------------------- */
async function reload() {
  const [clients, pocs, documents, meetings, updates, leads, outbox, receipts] = await Promise.all([
    idb.getAll('clients'), idb.getAll('client_pocs'), idb.getAll('documents'),
    idb.getAll('meetings'), idb.getAll('updates'), idb.getAll('leads'), idb.getAll('outbox'),
    idb.get('meta', 'receipts'),
  ]);
  Object.assign(S, { clients, pocs, documents, meetings, updates, leads, outbox,
                     receipts: (receipts && receipts.items) || [] });
  /* An acknowledgement still sitting in the outbox has not reached the server, but
   * the user tapped it — reflect that locally so the button does not spring back. */
  for (const op of outbox.filter((o) => o.type === 'receipt' && o.state === 'pending')) {
    const found = S.receipts.find((r) => r.update_id === op.payload.update_id);
    if (found) found.done_at = op.payload.done_at;
    else S.receipts.push({ update_id: op.payload.update_id, ...op.payload });
  }
  renderAll();
}

const clientById = (id) => S.clients.find((c) => c.id === id);
const pocsFor = (id) => S.pocs.filter((p) => p.client_id === id)
  .sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0));

/* --- rendering ----------------------------------------------------------- */
function sigStrip(c) {
  const g = c.signals || {};
  const out = [];
  const chip = (label, val, cls) => {
    if (!val) return;
    out.push(`<span${cls ? ` class="${cls}"` : ''}>${label ? `<i>${esc(label)}</i> ` : ''}${esc(val)}</span>`);
  };
  const tone = { crit: 'critx', warn: 'warnx', good: 'good' }[g.healthTone] || '';
  chip('Health', g.health, tone);
  chip('Feedback', g.sentiment, /negativ|unhappy|vocal/i.test(g.sentiment || '') ? 'critx'
    : /positiv|advocate/i.test(g.sentiment || '') ? 'good' : '');
  chip('AI interest', g.ai, /high|strong|very/i.test(g.ai || '') ? 'blue' : '');
  chip('FTE', g.fte);
  chip('', g.tenure, g.newClient ? 'blue' : '');
  chip('Billing', g.billing);
  chip('Book', g.book);
  chip('Revenue', g.revenue);
  const mix = [g.pl && `${g.pl}% PL`, g.cl && `${g.cl}% CL`, g.eb && `${g.eb}% EB`]
    .filter(Boolean).join(' / ');
  chip('Mix', mix);
  chip('AM', g.am || c.account_manager);
  chip('Team', g.team);
  return out.length ? `<div class="sig">${out.join('')}</div>` : '';
}

/* Scope of service & whitespace — grouped by P&C / EB, one checklist per line.
 * Fed by signals.activities (see importer.py find_activity_columns). */
function scopeBlock(c) {
  const acts = (c.signals && c.signals.activities) || [];
  if (!acts.length) return '';
  const order = [];
  const groups = {};
  for (const a of acts) {
    if (!groups[a.line]) { groups[a.line] = []; order.push(a.line); }
    groups[a.line].push(a);
  }
  const cols = order.map((line) => {
    const items = groups[line];
    const on = items.filter((a) => a.on).length;
    const rows = items.map((a) => `<li class="${a.on ? 'on' : 'off'}">
      <span class="tick">${a.on ? '✓' : ''}</span>${esc(a.label)}${
        a.on && a.count ? `<span class="cnt">${esc(a.count)}</span>` : ''}</li>`).join('');
    return `<div class="svc-col"><div class="svc-h">${esc(line)}
      <span class="svc-count">${on} / ${items.length}</span></div>
      <ul class="svc-list">${rows}</ul></div>`;
  }).join('');
  return `<div class="blk svc"><div class="bt">Scope of service &amp; whitespace</div>
    <div class="svc-grid">${cols}</div></div>`;
}

/* How to play it at the booth — situational cues (see importer.py build_cues). */
function cuesBlock(c) {
  const cues = (c.signals && c.signals.cues) || [];
  if (!cues.length) return '';
  return `<div class="blk cues"><div class="bt">How to play it at the booth</div>
    <ul>${cues.map((t) => `<li>${esc(t)}</li>`).join('')}</ul></div>`;
}

function clientCard(c) {
  const docs = S.documents.filter((d) => d.client_id === c.id);
  const pocs = pocsFor(c.id);
  const mtgs = S.meetings.filter((m) => m.client_id === c.id);
  const list = (arr) => arr.map((s) => `<li>${esc(s)}</li>`).join('');
  return `<article class="ccard p-${esc(c.priority || 'watch')}">
    <header class="ctop" role="button" tabindex="0" aria-expanded="true">
      <div style="display:flex;gap:9px;align-items:flex-start;justify-content:space-between">
        <div class="cname">${esc(c.name)}</div>
        <span class="badge ${c.priority === 'must' ? 'crit' : c.priority === 'target' ? '' : 'mute'}">
          ${esc(PRIORITY[c.priority] || 'Watch')}</span>
      </div>
      <div class="cmeta">
        ${c.location ? `<span>${esc(c.location)}</span>` : ''}
        ${c.product ? `<span>· ${esc(c.product)}</span>` : ''}
        ${c.owner ? `<span>· Owner: <b>${esc(c.owner)}</b></span>` : ''}
      </div>
      <span class="ctog" aria-hidden="true">▾</span>
    </header>
    <div class="cbody">
      ${sigStrip(c)}
      ${c.summary ? `<div class="blk"><div class="bt">Where we stand</div>
        <div style="font-size:13.5px">${esc(c.summary)}</div></div>` : ''}
      <div class="cbody-more">
        ${(c.talking_points || []).length ? `<div class="blk say"><div class="bt">Key points to make</div>
          <ul>${list(c.talking_points)}</ul></div>` : ''}
        ${(c.avoid_points || []).length ? `<div class="blk avoid"><div class="bt">Do not raise</div>
          <ul>${list(c.avoid_points)}</ul></div>` : ''}
        ${scopeBlock(c)}
        ${pocs.length ? `<div class="blk"><div class="bt">Who you will meet</div>
          ${pocs.map((p) => `<div class="poc"><div class="pn">${esc(p.name)}${
            p.title ? ` <span class="pt">— ${esc(p.title)}</span>` : ''}</div>${
            p.note ? `<div style="font-size:12.5px;color:var(--gray-700);margin-top:3px">${esc(p.note)}</div>` : ''
          }</div>`).join('')}</div>` : ''}
        ${cuesBlock(c)}
        ${mtgs.length ? `<div class="blk"><div class="bt">Scheduled</div>${mtgs.map((m) =>
          `<div style="font-size:13px">${esc(m.meeting_date || '')} · ${esc(m.start_time || 'TBD')}${
            m.location ? ` · ${esc(m.location)}` : ''}</div>`).join('')}</div>` : ''}
        ${docs.length ? `<div class="blk"><div class="bt">Attached documents</div><div class="attach">
          ${docs.map((d) => `<a data-doc="${esc(d.id)}">${esc(d.filename)}</a>`).join('')}
        </div></div>` : ''}
      </div>
    </div>
    <footer>
      <div class="chips">${(c.tags || []).map((t) => `<span class="badge mute">${esc(t)}</span>`).join('')}</div>
      <button class="btn sub tiny" data-lead-for="${esc(c.id)}">Log outcome</button>
    </footer>
  </article>`;
}

function getClientFilterValues() {
  return {
    accountManager: $('fAM') ? $('fAM').value : 'all',
    ams: $('fAMS') ? $('fAMS').value : 'all',
    status: $('fStatus') ? $('fStatus').value : 'all',
    health: $('fHealth') ? $('fHealth').value : 'all',
  };
}

function populateClientFilterOptions() {
  const opts = clientFilters.collectClientFilterOptions(S.clients);
  const filters = [
    { select: $('fAM'), values: opts.accountManagers, label: 'All Account Managers' },
    { select: $('fAMS'), values: opts.ams, label: 'All AMS' },
    { select: $('fStatus'), values: opts.statuses, label: 'All Status' },
    { select: $('fHealth'), values: opts.healths, label: 'All Health' },
  ];

  for (const item of filters) {
    if (!item.select) continue;
    const current = item.select.value || 'all';
    item.select.innerHTML = `<option value="all">${item.label}</option>${item.values
      .map((value) => `<option value="${esc(value)}">${esc(value)}</option>`).join('')}`;
    const valid = item.values.some((value) => value.toLowerCase() === current.toLowerCase());
    item.select.value = valid ? current : 'all';
  }
}

function renderClients() {
  const q = ($('qClients').value || '').trim().toLowerCase();
  const pri = [...document.querySelectorAll('#cPriority .chip[aria-pressed="true"]')]
    .map((b) => b.dataset.v);
  const rank = { must: 0, target: 1, watch: 2 };
  const list = S.clients.filter((c) => clientFilters.matchesClientFilter(
    c,
    q,
    pri,
    getClientFilterValues(),
  )).sort((a, b) => (rank[a.priority] ?? 3) - (rank[b.priority] ?? 3)
    || String(a.name).localeCompare(String(b.name)));

  $('clientCards').innerHTML = list.length
    ? list.map(clientCard).join('')
    : `<div class="empty" style="grid-column:1/-1"><b>${
      S.clients.length ? 'No client matches those filters' : 'No client briefs yet'}</b>${
      S.clients.length ? 'Clear the search or filter dropdowns.'
        : 'Ask the office to publish the briefing pack, then pull to refresh.'}</div>`;
  applyCardCollapse();
}

/* Card collapse: a global "Collapse all"/"Expand all" toggle, plus each card's
 * own header can be tapped to flip just that one card. Re-rendering (search,
 * filters) re-applies the last global state to every card. */
let cardsCollapsed = false;

function setCardCollapsed(card, collapsed) {
  card.classList.toggle('collapsed', collapsed);
  const top = card.querySelector('.ctop');
  if (top) top.setAttribute('aria-expanded', String(!collapsed));
}

function applyCardCollapse() {
  document.querySelectorAll('#clientCards .ccard').forEach((card) => setCardCollapsed(card, cardsCollapsed));
}

function renderFeed() {
  const sorted = [...S.updates].sort((a, b) => (b.pinned - a.pinned)
    || String(b.created_at).localeCompare(String(a.created_at)));
  $('feed').innerHTML = sorted.length ? sorted.map((u) => {
    const r = S.receipts.find((x) => x.update_id === u.id) || {};
    return `<article class="fitem${u.level === 'urgent' ? ' urgent' : ''}${u.pinned ? ' pinned' : ''}">
      <div class="fh">
        ${u.pinned ? '<span class="badge mute">Pinned</span>' : ''}
        ${u.level === 'urgent' ? '<span class="badge crit">Urgent</span>' : ''}
        ${u.is_action ? `<span class="badge ${r.done_at ? 'ok' : 'dark'}">${
          r.done_at ? 'Action done' : 'Action needed'}</span>` : ''}
        <span class="ft">${esc(u.title)}</span>
        <span class="fm">${esc((u.created_at || '').replace('T', ' ').replace('Z', ''))}</span>
      </div>
      ${u.body ? `<div class="fb">${esc(u.body)}</div>` : ''}
      ${u.is_action ? `<div class="rowtools" style="margin-top:9px">
        <button class="btn sub tiny" data-ack="${esc(u.id)}" data-done="${r.done_at ? '0' : '1'}">
        ${r.done_at ? 'Reopen' : 'Mark done'}</button></div>` : ''}
    </article>`;
  }).join('') : '<div class="empty"><b>No updates from the office yet</b>Anything urgent will appear here.</div>';
}

/* Schedule tab disabled
function renderSchedule() {
  const byDay = {};
  for (const m of S.meetings) (byDay[m.meeting_date || 'Unscheduled'] ??= []).push(m);
  const today = new Date().toISOString().slice(0, 10);
  const days = Object.keys(byDay).sort();
  $('schedList').innerHTML = days.length ? days.map((d) => `<div class="day">
    <div class="day-h"><h3>${esc(d)}</h3><span class="dd">${byDay[d].length} scheduled</span>
    ${d === today ? '<span class="today">Today</span>' : ''}</div>
    ${byDay[d].sort((a, b) => String(a.start_time).localeCompare(String(b.start_time)))
      .map((m) => {
        const c = m.client_id ? clientById(m.client_id) : null;
        return `<div class="mrow${m.status === 'done' ? ' done' : ''}">
        <div class="mt">${esc(m.start_time || 'TBD')}${
          m.duration_minutes ? `<small>${esc(m.duration_minutes)} min</small>` : ''}</div>
        <div><div class="mw">${esc(m.title || (c ? `${c.name} meeting` : 'Meeting'))}</div>
          <div class="mi">${c ? `Client: <b>${esc(c.name)}</b> · ` : ''}${
            m.location ? `${esc(m.location)} · ` : ''}${m.owner ? `FBSPL: ${esc(m.owner)}` : ''}</div>
          ${m.notes ? `<div class="mi" style="color:var(--black)">${esc(m.notes)}</div>` : ''}</div>
        <div><span class="badge ${m.status === 'done' ? 'ok' : m.status === 'cancelled' ? 'err' : 'mute'}">
          ${esc(m.status || 'scheduled')}</span></div></div>`;
      }).join('')}</div>`).join('')
    : '<div class="empty"><b>Nothing scheduled yet</b>Meetings appear here once the office publishes them.</div>';
}
*/

async function renderDocs() {
  const st = await docsmod.status();
  $('readiness').textContent = st.label;
  $('readiness').className = st.ready ? 'badge ok' : 'badge crit';
  $('docList').innerHTML = S.documents.length ? S.documents.map((d) => {
    const cached = !st.missing.some((m) => m.id === d.id);
    const ext = (d.filename.split('.').pop() || 'file').toUpperCase().slice(0, 4);
    const c = d.client_id ? clientById(d.client_id) : null;
    return `<div class="dcard">
      <div class="dicon${ext === 'PDF' ? ' pdf' : ''}">${esc(ext)}</div>
      <div style="min-width:0;flex:1">
        <div class="dn">${esc(d.filename)}</div>
        <div class="dm">${Math.round(d.size_bytes / 1024)} KB · ${c ? esc(c.name) : 'General'}
          · ${cached ? 'available offline' : '<b>not downloaded</b>'}</div>
        <div class="rowtools" style="margin-top:8px">
          <button class="btn ghost tiny" data-doc="${esc(d.id)}">Open</button></div>
      </div></div>`;
  }).join('') : '<div class="empty"><b>No documents yet</b></div>';
}

function renderLeads() {
  const rows = [...S.leads].sort((a, b) =>
    String(b.captured_at).localeCompare(String(a.captured_at)));
  const pending = S.outbox.filter((o) => o.state === 'pending').length;
  $('leadPending').textContent = pending ? `${pending} pending sync` : 'all synced';
  $('leadPending').className = pending ? 'pending' : 'badge ok';
  $('leadBody').innerHTML = rows.length ? rows.map((l) => `<tr>
    <td style="white-space:nowrap">${esc((l.captured_at || '').replace('T', ' ').replace('Z', ''))}</td>
    <td><b>${esc(l.name || '—')}</b>${l.company
      ? `<div style="color:var(--gray-700);font-size:12px">${esc(l.company)}</div>` : ''}</td>
    <td style="font-size:12.5px">${esc(l.email || '')}${l.phone ? `<div>${esc(l.phone)}</div>` : ''}</td>
    <td>${esc(l.interest || '')}</td>
    <td>${esc(l.next_step || '')}</td>
    <td>${l.synced ? '<span class="badge ok">synced</span>'
      : '<span class="badge crit">queued</span>'}</td></tr>`).join('')
    : '<tr><td colspan="6" style="color:var(--gray-700)">No leads logged yet.</td></tr>';
}

function renderAll() {
  populateClientFilterOptions();
  $('pClients').textContent = S.clients.length;
  $('pDocs').textContent = S.documents.length;
  // $('pSched').textContent = S.meetings.length; // Schedule tab disabled
  $('pLeads').textContent = S.leads.length;
  const open = S.updates.filter((u) => u.is_action
    && !(S.receipts.find((r) => r.update_id === u.id) || {}).done_at).length;
  $('pBrief').textContent = open || S.updates.length;
  $('pBrief').className = `pill${open ? ' urgent' : ''}`;
  renderFeed(); renderClients(); renderLeads(); renderDocs(); // renderSchedule() disabled
}

/* --- lead capture (offline-first) ---------------------------------------- */
function logLead(clientId) {
  const opts = S.clients.map((c) =>
    `<option value="${esc(c.id)}"${c.id === clientId ? ' selected' : ''}>${esc(c.name)}</option>`).join('');
  modal({
    title: 'Log a lead',
    okLabel: 'Save',
    body: `<div class="grid2">
        <div class="fld"><label>Person met *</label><input class="inp" id="lN"></div>
        <div class="fld"><label>Agency / company</label><input class="inp" id="lCo"></div></div>
      <div class="grid2">
        <div class="fld"><label>Email</label><input class="inp" id="lE" type="email"></div>
        <div class="fld"><label>Phone</label><input class="inp" id="lP"></div></div>
      <div class="fld"><label>Existing client</label><select class="inp" id="lC">
        <option value="">— none —</option>${opts}</select></div>
      <div class="fld"><label>Interested in</label><input class="inp" id="lI"></div>
      <div class="fld"><label>Agreed next step</label><input class="inp" id="lNx"></div>
      <div class="fld"><label>Notes</label><textarea class="inp" id="lNo"></textarea></div>
      <div class="note">Saved on this device immediately. It syncs on its own when there is signal.</div>`,
    onOk: () => {
      const name = $('lN').value.trim();
      if (!name) { toast('Who did you meet?', 'err'); return false; }
      saveLead({
        name,
        company: $('lCo').value.trim(),
        email: $('lE').value.trim(),
        phone: $('lP').value.trim(),
        client_id: $('lC').value || null,
        interest: $('lI').value.trim(),
        next_step: $('lNx').value.trim(),
        notes: $('lNo').value.trim(),
      });
      return true;
    },
  });
}

async function saveLead(fields) {
  const id = uid();
  /* Device clock, recorded at capture. The server stores it verbatim — a lead
   * taken at 10:04 in the hall and synced at 18:30 must report 10:04. */
  const captured_at = new Date().toISOString();
  const lead = { id, ...fields, captured_at, synced: 0,
                 captured_by_name: S.session.user.name };
  const op = { id, type: 'lead', state: 'pending', created_at: captured_at, attempts: 0,
               payload: { ...fields, captured_at } };

  await idb.captureLead(lead, op);      // one transaction: both or neither
  await reload();
  toast('Lead saved on this device', 'ok');
  sync.sync(S.session.event.id, { reason: 'lead' });
}

async function ackUpdate(updateId, done) {
  const op = { id: uid(), type: 'receipt', state: 'pending', created_at: new Date().toISOString(),
               attempts: 0,
               payload: { update_id: updateId, read_at: new Date().toISOString(),
                          done_at: done ? new Date().toISOString() : null } };
  await idb.put('outbox', op);
  const existing = S.receipts.find((r) => r.update_id === updateId);
  if (existing) existing.done_at = op.payload.done_at;
  else S.receipts.push({ update_id: updateId, ...op.payload });
  renderFeed();
  sync.sync(S.session.event.id, { reason: 'ack' });
}

async function openDoc(docId) {
  const doc = S.documents.find((d) => d.id === docId);
  if (!doc) return;
  try {
    const url = await docsmod.openDocument(doc);
    window.open(url, '_blank');
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (err) {
    if (err.code === 'NOT_DOWNLOADED') {
      toast('Not downloaded — tap "Prepare for offline" while you have signal', 'err');
    } else {
      toast(`Could not open: ${err.message}`, 'err');
    }
  }
}

/* --- status strip -------------------------------------------------------- */
function setStatus() {
  const online = navigator.onLine;
  $('netDot').className = `dot ${online ? 'on' : 'off'}`;
  $('netText').textContent = online ? 'Online' : 'Offline — reading from this device';
  const pending = S.outbox.filter((o) => o.state === 'pending').length;
  $('syncPending').textContent = pending ? `${pending} waiting to sync` : '';
  $('syncPending').className = pending ? 'pending' : 'hide';
}

/* --- boot ---------------------------------------------------------------- */
function showApp(session) {
  document.body.classList.remove('locked');
  $('whoName').textContent = session.user.name;
  $('whoRole').textContent = session.user.role === 'admin'
    ? 'Admin · can publish' : 'Floor team';
  $('evtName').textContent = session.event.name;
  $('evtVenue').textContent = session.event.venue || '';
}

function bind() {
  document.querySelectorAll('#tabs button').forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll('#tabs button').forEach((x) => x.setAttribute('aria-selected', x === b));
      document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('on', p.id === `p-${b.dataset.p}`));
    };
  });

  $('qClients').oninput = renderClients;
  ['fAM', 'fAMS', 'fStatus', 'fHealth'].forEach((id) => {
    const el = $(id);
    if (el) el.onchange = renderClients;
  });
  document.querySelectorAll('#cPriority .chip').forEach((b) => {
    b.onclick = () => {
      b.setAttribute('aria-pressed', b.getAttribute('aria-pressed') !== 'true');
      renderClients();
    };
  });

  $('collapseAllBtn').onclick = () => {
    cardsCollapsed = !cardsCollapsed;
    applyCardCollapse();
    $('collapseAllBtn').textContent = cardsCollapsed ? 'Expand all' : 'Collapse all';
    $('collapseAllBtn').setAttribute('aria-pressed', String(cardsCollapsed));
  };

  const toggleCard = (top) => {
    const card = top.closest('.ccard');
    if (card) setCardCollapsed(card, !card.classList.contains('collapsed'));
  };
  $('clientCards').addEventListener('click', (e) => {
    const top = e.target.closest('.ctop');
    if (top) toggleCard(top);
  });
  $('clientCards').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const top = e.target.closest('.ctop');
    if (!top) return;
    e.preventDefault();
    toggleCard(top);
  });

  $('addLead').onclick = () => logLead(null);
  $('signOut').onclick = signOut;
  $('btnPrepare').onclick = async () => {
    toast('Syncing…');
    const sr = await sync.sync(S.session.event.id, { reason: 'manual' });
    if (sr && sr.error) toast(`Sync failed: ${sr.error} — still preparing documents…`, 'err');

    toast('Downloading documents…');
    const dr = await docsmod.prefetchAll();
    await renderDocs();

    if (sr && sr.error) {
      toast(`Sync failed: ${sr.error}`, 'err');
    } else if (dr.failed.length) {
      toast(`${dr.failed.length} document(s) failed — retry on better wifi`, 'err');
    } else {
      toast(formatReadiness(dr), 'ok');
    }
  };

  document.body.addEventListener('click', (e) => {
    const d = e.target.closest('[data-doc]');
    if (d) { e.preventDefault(); openDoc(d.dataset.doc); return; }
    const l = e.target.closest('[data-lead-for]');
    if (l) { e.preventDefault(); logLead(l.dataset.leadFor); return; }
    const a = e.target.closest('[data-ack]');
    if (a) { e.preventDefault(); ackUpdate(a.dataset.ack, a.dataset.done === '1'); }
  });

  $('gateForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    $('gateErr').className = 'gate-err';
    $('gGo').disabled = true;
    try {
      S.session = await signIn($('gUser').value, $('gPass').value, $('gKeep').checked);
      showApp(S.session);
      await docsmod.requestPersistence();
      await sync.sync(S.session.event.id, { reason: 'first-run' });
      await afterSync();
      toast(`Signed in as ${S.session.user.name}`, 'ok');
    } catch (err) {
      $('gateErr').className = 'gate-err on';
      $('gateErr').textContent = err.message;
    } finally {
      $('gGo').disabled = false;
    }
  });

  window.addEventListener('online', setStatus);
  window.addEventListener('offline', setStatus);

  sync.bus.addEventListener('done', afterSync);
  sync.bus.addEventListener('outbox', () => { reload().then(setStatus); });
  sync.bus.addEventListener('rebased', (e) => {
    if (e.detail.leads) toast(`Event data refreshed — ${e.detail.leads} saved lead(s) re-sent`, 'ok');
  });
  sync.bus.addEventListener('unauthorised', () => {
    toast('Your session was ended by an administrator. Sign in again.', 'err');
    idb.del('meta', 'session').then(() => setTimeout(() => location.reload(), 2500));
  });
  sync.bus.addEventListener('failed', (e) => {
    if (navigator.onLine) toast(`Sync problem: ${e.detail.message}`, 'err');
  });
  docsmod.bus.addEventListener('storage-warning', (e) => toast(e.detail.message, 'err'));
}

async function afterSync() {
  await docsmod.reconcile();     // drop cached bytes for documents the server deleted
  await reload();
  await renderDocs();
  setStatus();
}

async function boot() {
  bind();
  if ('serviceWorker' in navigator) {
    /* update() on every boot, because without it a shipped fix can sit on the
     * server for days while the device keeps serving the cached shell. The new
     * worker calls skipWaiting + claim, so it takes over immediately; reload
     * once when it does, or this page keeps running the old code it was loaded
     * with. Only when there WAS a controller — on a first visit the claim is
     * the normal path, not an update. */
    const had = !!navigator.serviceWorker.controller;
    let reloaded = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (!had || reloaded) return;
      reloaded = true;
      location.reload();
    });
    navigator.serviceWorker.register('/sw.js')
      .then((reg) => reg.update())
      .catch((e) => console.warn('service worker did not register — offline will not work:', e));
  }

  S.session = await loadSession();
  if (!S.session) { document.body.classList.add('locked'); return; }

  /* Cold offline boot: render straight from the replica. No network call on this
   * path at all — not even an optimistic one, because a captive portal makes it
   * hang for thirty seconds. */
  showApp(S.session);
  await reload();
  setStatus();
  sync.startAutoSync(S.session.event.id);
  sync.sync(S.session.event.id, { reason: 'boot' });
}

boot();
