/* Image leads: floor UI only. All storage, compression and upload lives in
 * image_queue.js; this module renders the FAB, the capture sheet, the camera,
 * the viewer and the Image Leads tab. */

import * as queue from './image_queue.js';
import { leadStatus, canUpload, formatBytes, shortId } from './image_core.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const errText = (err) => (err && err.message) || String(err || 'Something went wrong');
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const pad2 = (n) => String(n).padStart(2, '0');
const fmtTime = (s) => {
  const d = new Date(s);
  if (!s || Number.isNaN(d.getTime())) return '';
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
};
const bySlot = (a, b) => (a.slot || 0) - (b.slot || 0);
const EXT = { 'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp',
              'image/heic': 'heic', 'image/heif': 'heif' };
const CAMERA = { video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 },
                          height: { ideal: 1080 } }, audio: false };
const BADGE = { draft: 'mute', queued: 'crit', uploading: 'crit', done: 'ok', failed: 'err' };

let ctx = null;
let inited = false;
let started = false;
let sheet = null;
let opening = false;
let cam = null;
let camReq = 0;
let viewer = null;
let dialogOpen = null;
let pickSlot = 1;
let saveTimer = null;
let saveChain = Promise.resolve();
let listSeq = 0;
let listTimer = null;

const session = () => (ctx && ctx.getSession ? ctx.getSession() : null);
const clients = () => ((ctx && ctx.getClients ? ctx.getClients() : null) || []);
const toast = (msg, kind) => { if (ctx && ctx.toast) ctx.toast(msg, kind); };

/* --- object URLs --------------------------------------------------------- */
function localUrl(photo) {
  if (!photo) return null;
  if (photo.bytes) {
    try { return URL.createObjectURL(new Blob([photo.bytes], { type: photo.mime || 'image/jpeg' })); }
    catch { return null; }
  }
  try { return queue.photoUrl(photo) || null; } catch { return null; }
}

/* One pool per view; URLs not reused by the next render are revoked. */
function urlPool() {
  let cur = new Map();
  let next = null;
  const key = (p) => `${p.id}|${p.state}|${p.size}|${p.bytes ? p.bytes.byteLength || p.bytes.size : 0}`;
  return {
    begin() { next = new Map(); },
    get(p) {
      if (!p) return null;
      const k = key(p);
      let u = (next && next.get(k)) || cur.get(k);
      if (!u) u = localUrl(p);
      if (u && next) next.set(k, u);
      return u;
    },
    end() {
      for (const [k, u] of cur) if (!next.has(k)) URL.revokeObjectURL(u);
      cur = next; next = null;
    },
    clear() {
      for (const u of cur.values()) URL.revokeObjectURL(u);
      cur = new Map(); next = null;
    },
  };
}
const sheetPool = urlPool();
const listPool = urlPool();

/* --- hosts / open state -------------------------------------------------- */
function ensureHosts() {
  let host = $('imgHost');
  if (!host) {
    host = document.createElement('div');
    host.id = 'imgHost';
    document.body.appendChild(host);
  }
  if (!$('ilSheetHost')) {
    host.innerHTML = '<div id="ilSheetHost"></div><div id="ilCamHost"></div>'
      + '<div id="ilViewHost"></div><div id="ilDlgHost"></div>';
  }
  return host;
}

function syncOpen() {
  document.body.classList.toggle('il-open', !!(sheet || cam || viewer || dialogOpen));
}

/* --- confirm dialog ------------------------------------------------------ */
function confirmDialog({ title, body, yes, no }) {
  ensureHosts();
  if (dialogOpen) dialogOpen(false);
  return new Promise((resolve) => {
    const host = $('ilDlgHost');
    const done = (v) => {
      if (dialogOpen !== done) return;
      dialogOpen = null;
      host.innerHTML = '';
      syncOpen();
      resolve(v);
    };
    dialogOpen = done;
    host.innerHTML = `<div class="scrim il-dlg" id="ilDlgScrim">
      <div class="modal" role="alertdialog" aria-modal="true" aria-labelledby="ilDlgT" aria-describedby="ilDlgB">
      <header><h3 id="ilDlgT">${esc(title)}</h3></header>
      <div class="mbody" id="ilDlgB">${esc(body)}</div>
      <footer><button class="btn sub" type="button" id="ilDlgYes">${esc(yes)}</button>
        <button class="btn" type="button" id="ilDlgNo">${esc(no)}</button></footer></div></div>`;
    $('ilDlgYes').onclick = () => done(true);
    $('ilDlgNo').onclick = () => done(false);
    $('ilDlgScrim').onclick = (e) => { if (e.target.id === 'ilDlgScrim') done(false); };
    syncOpen();
    $('ilDlgNo').focus();
  });
}

/* --- sheet state --------------------------------------------------------- */
function fields(sh) {
  return {
    client_id: sh.client_id || null,
    client_name: sh.client_id ? (sh.client_name || '') : null,
    other_name: sh.client_id ? '' : sh.other_name.trim(),
    note: sh.note.trim(),
  };
}

const busyAny = (sh) => !!(sh.busy[1] || sh.busy[2]);

function hasContent(sh) {
  const f = fields(sh);
  return !!(f.client_id || f.other_name || f.note || sh.photos.length || busyAny(sh));
}

function flushSave(sh, force) {
  clearTimeout(saveTimer);
  saveTimer = null;
  if (!sh) return saveChain;
  saveChain = saveChain.then(async () => {
    if (!sh.leadId && !force && !hasContent(sh)) return;
    const payload = fields(sh);
    if (sh.leadId) payload.id = sh.leadId;
    const lead = await queue.saveDraft(session(), payload);
    if (lead && lead.id) { sh.leadId = lead.id; sh.lead = lead; }
  }).catch((err) => { toast(`Could not save the draft: ${errText(err)}`, 'err'); });
  return saveChain;
}

function changed(sh) {
  sh.submitErr = '';
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => flushSave(sh), 400);
  renderFoot();
}

async function refreshSheet(sh) {
  if (!sh || !sh.leadId) return;
  let rec = null;
  try { rec = await queue.getLead(sh.leadId); } catch { rec = null; }
  if (sheet !== sh) return;
  if (rec && rec.lead) sh.lead = rec.lead;
  sh.photos = ((rec && rec.photos) || []).filter(Boolean).sort(bySlot);
  renderPhotos();
  renderFoot();
}

async function openSheet(leadId) {
  if (sheet || opening) return;
  opening = true;
  try {
    const sh = { leadId: null, lead: null, photos: [], client_id: null, client_name: '',
                 other_name: '', otherStash: '', note: '', mode: 'search', query: '', help: null,
                 slotErr: {}, busy: {}, replacing: {}, submitErr: '', submitting: false };
    if (leadId) {
      let rec = null;
      try { await queue.recoverInterrupted(leadId); } catch { /* shown as processing; Replace still works */ }
      try { rec = await queue.getLead(leadId); } catch { rec = null; }
      if (!rec || !rec.lead) { toast('That draft is no longer on this phone', 'err'); return; }
      const l = rec.lead;
      if (l.state && l.state !== 'draft') { openViewer(leadId); return; }
      Object.assign(sh, {
        leadId: l.id, lead: l, photos: (rec.photos || []).filter(Boolean).sort(bySlot),
        client_id: l.client_id || null, client_name: l.client_name || '',
        other_name: l.other_name || '', note: l.note || '',
      });
      sh.mode = sh.client_id ? 'picked' : sh.other_name ? 'other' : 'search';
    }
    ensureHosts();
    sheet = sh;
    renderSheet();
    syncOpen();
  } finally {
    opening = false;
  }
}

function teardownSheet() {
  closeCamera();
  sheetPool.clear();
  const host = $('ilSheetHost');
  if (host) host.innerHTML = '';
  sheet = null;
  syncOpen();
}

async function closeSheet() {
  const sh = sheet;
  if (!sh) return;
  const keep = hasContent(sh);
  teardownSheet();
  if (keep) {
    await flushSave(sh);
    toast('Draft saved');
  } else {
    clearTimeout(saveTimer);
    await saveChain;
    if (sh.leadId) { try { await queue.deleteDraft(sh.leadId); } catch { /* already gone */ } }
  }
  scheduleList();
}

async function deleteDraft() {
  const sh = sheet;
  if (!sh || !sh.leadId) return;
  const ok = await confirmDialog({ title: 'Delete this draft?',
    body: 'The client, note and photos in this draft will be removed from this phone.',
    yes: 'Delete draft', no: 'Keep draft' });
  if (!ok || sheet !== sh) return;
  clearTimeout(saveTimer);
  teardownSheet();
  await saveChain;
  try {
    await queue.deleteDraft(sh.leadId);
    toast('Draft deleted');
  } catch (err) {
    toast(`Could not delete the draft: ${errText(err)}`, 'err');
  }
  scheduleList();
}

async function upload() {
  const sh = sheet;
  if (!sh || sh.submitting) return;
  sh.submitting = true;
  sh.submitErr = '';
  renderFoot();
  try {
    await flushSave(sh, true);
    if (!sh.leadId) throw new Error('The draft could not be saved.');
    const r = await queue.submit(sh.leadId);
    if (!r || !r.ok) {
      sh.submitErr = (r && r.reason) || 'Could not upload this lead.';
      return;
    }
    teardownSheet();
    toast(navigator.onLine ? 'Uploading…' : 'Saved on this phone — uploading when there is signal', 'ok');
    scheduleList();
  } catch (err) {
    sh.submitErr = errText(err);
  } finally {
    sh.submitting = false;
    if (sheet === sh) renderFoot();
  }
}

/* --- sheet rendering ----------------------------------------------------- */
function renderSheet() {
  const sh = sheet;
  $('ilSheetHost').innerHTML = `<div class="il-sheet" role="dialog" aria-modal="true" aria-labelledby="ilTitle">
    <div class="il-panel">
      <header class="il-h"><h3 id="ilTitle">New image lead</h3>
        <button class="x" type="button" data-il="close" aria-label="Close">&times;</button></header>
      <div class="il-body">
        <div class="fld"><label>Client</label><div id="ilClient"></div></div>
        <div class="fld"><label for="ilNote">Note (optional)</label>
          <textarea class="inp" id="ilNote" maxlength="1000" placeholder="Anything to remember about this card"></textarea></div>
        <div class="fld"><label>Photos</label><div class="il-slots" id="ilPhotos"></div></div>
        <div id="ilHelp"></div>
      </div>
      <footer class="il-f" id="ilFoot"></footer>
    </div></div>`;
  $('ilNote').value = sh.note;
  renderClient();
  renderPhotos();
  renderHelp();
  renderFoot();
}

function renderClient() {
  const sh = sheet;
  const box = $('ilClient');
  if (!sh || !box) return;
  if (sh.mode === 'picked' && sh.client_id) {
    box.innerHTML = `<div class="il-chip"><span class="n">${esc(sh.client_name || 'Client')}</span>
      <button class="btn sub tiny" type="button" data-il="change">Change</button></div>`;
  } else if (sh.mode === 'other') {
    box.innerHTML = `<input class="inp" id="ilOther" maxlength="200" autocomplete="off"
        aria-label="Other name" placeholder="Other name — client or agency">
      <button class="il-toggle" type="button" data-il="list">Pick from the client list instead</button>`;
    $('ilOther').value = sh.other_name;
  } else {
    box.innerHTML = `<input class="inp" id="ilQ" type="search" autocomplete="off" spellcheck="false"
        aria-label="Search clients" placeholder="Search clients…">
      <div id="ilMatches"></div>
      <button class="il-toggle" type="button" data-il="other">Not in the list? Type the name</button>`;
    $('ilQ').value = sh.query;
    renderMatches();
  }
}

function renderMatches() {
  const sh = sheet;
  const box = $('ilMatches');
  if (!sh || !box) return;
  const all = clients().filter((c) => c && c.id && c.name);
  const q = sh.query.trim().toLowerCase();
  if (!all.length) {
    box.innerHTML = '<div class="il-hint">No clients on this phone yet — type the name instead.</div>';
    return;
  }
  if (!q) {
    box.innerHTML = `<div class="il-hint">Type to search ${plural(all.length, 'client', 'clients')}.</div>`;
    return;
  }
  const hits = all.filter((c) => String(c.name).toLowerCase().includes(q))
    .sort((a, b) => (String(b.name).toLowerCase().startsWith(q) - String(a.name).toLowerCase().startsWith(q))
      || String(a.name).localeCompare(String(b.name)))
    .slice(0, 8);
  box.innerHTML = hits.length
    ? `<div class="il-matches">${hits.map((c) =>
      `<button class="il-match" type="button" data-cid="${esc(c.id)}">${esc(c.name)}</button>`).join('')}</div>`
    : '<div class="il-hint">No client matches. Check the spelling or type the name instead.</div>';
}

function slotStatus(sh, slot, p) {
  if (sh.slotErr[slot]) return { text: sh.slotErr[slot], err: true };
  if (sh.busy[slot] && (!p || p.state !== 'processing')) return { text: 'Processing…' };
  if (!p) return { text: '' };
  if (p.state === 'processing') return { text: 'Processing…' };
  if (p.state === 'failed') return { text: p.error || 'This photo could not be saved.', err: true };
  return { text: p.size ? formatBytes(p.size) : '' };
}

function renderPhotos() {
  const sh = sheet;
  const box = $('ilPhotos');
  if (!sh || !box) return;
  sheetPool.begin();
  box.innerHTML = [1, 2].map((slot) => {
    /* During a replace the current photo and its replacement share the slot. */
    const inSlot = sh.photos.filter((x) => x && x.slot === slot);
    const p = inSlot.find((x) => x.state !== 'processing') || inSlot[0];
    const st = slotStatus(sh, slot, p);
    const status = `<div class="il-st${st.err ? ' err' : ''}">${esc(st.text)}</div>`;
    const pick = `<div class="btns">
      <button class="btn" type="button" data-il="camera" data-slot="${slot}">Camera</button>
      <button class="btn ghost" type="button" data-il="gallery" data-slot="${slot}">Gallery</button>
      ${p ? `<button class="btn sub tiny" type="button" data-il="replace-cancel" data-slot="${slot}">Cancel</button>` : ''}
    </div>`;
    if (!p) {
      return `<div class="il-slot empty"><div class="il-slot-t">Photo ${slot}</div>
        <div class="il-thumb">${sh.busy[slot] ? 'Processing…' : 'No photo'}</div>
        ${sh.busy[slot] ? status : `${status}${pick}`}</div>`;
    }
    const url = sheetPool.get(p);
    return `<div class="il-slot"><div class="il-slot-t">Photo ${slot}</div>
      <div class="il-thumb">${url ? `<img src="${esc(url)}" alt="Photo ${slot}">` : esc(st.text || 'Photo')}</div>
      ${status}
      ${sh.replacing[slot] ? pick : `<div class="btns">
        <button class="btn ghost" type="button" data-il="replace" data-slot="${slot}">Replace</button>
        <button class="btn sub" type="button" data-il="remove" data-slot="${slot}">Remove</button></div>`}
    </div>`;
  }).join('');
  sheetPool.end();
}

function renderFoot() {
  const sh = sheet;
  const box = $('ilFoot');
  if (!sh || !box) return;
  const lead = { ...(sh.lead || {}), id: sh.leadId, ...fields(sh), state: 'draft' };
  let check;
  try { check = canUpload(lead, sh.photos) || { ok: false, reason: '' }; }
  catch (err) { check = { ok: false, reason: errText(err) }; }
  if (busyAny(sh) && check.ok) check = { ok: false, reason: 'Wait for the photos to finish saving.' };
  const reason = sh.submitErr || (sh.submitting ? 'Saving…' : check.ok ? '' : check.reason || '');
  box.innerHTML = `<div class="il-fbtns">
      ${sh.leadId && hasContent(sh) ? '<button class="btn sub" type="button" data-il="delete">Delete draft</button>' : ''}
      <span class="il-sp"></span>
      <button class="btn ghost" type="button" data-il="save">Save draft</button>
      <button class="btn" type="button" data-il="upload"${check.ok && !sh.submitting ? '' : ' disabled'}>Upload</button>
    </div>
    ${reason ? `<div class="il-reason${sh.submitErr ? ' err' : ''}">${esc(reason)}</div>` : ''}`;
}

function renderHelp() {
  const sh = sheet;
  const box = $('ilHelp');
  if (!sh || !box) return;
  const h = sh.help;
  if (!h) { box.innerHTML = ''; return; }
  const titles = {
    blocked: 'The camera is blocked for this site',
    prompt: 'The camera is not allowed yet',
    unknown: 'The camera is not allowed',
    unavailable: 'The camera could not start',
  };
  const how = h.kind === 'unavailable'
    ? '<p>Another app may be using it, or the browser cannot reach a camera. You can still use the phone camera app or pick a photo from the gallery.</p>'
    : `<p>${h.kind === 'prompt'
      ? 'Tap <b>Try again</b> and choose <b>Allow</b> when the phone asks.'
      : 'To take photos here, allow the camera for this site:'}</p>
      <ul>
        <li><b>iPhone:</b> Settings › Safari › Camera › Allow — or tap the <b>aA</b> menu in the address bar › Website Settings › Camera › Allow.</li>
        <li><b>Android (Chrome):</b> tap the lock icon in the address bar › Permissions › Camera › Allow.</li>
      </ul>
      <p>Then tap <b>Try again</b>.</p>`;
  box.innerHTML = `<div class="il-help" role="alert"><h4>${esc(titles[h.kind] || titles.unknown)}</h4>${how}
    <div class="rowtools">
      <button class="btn" type="button" data-il="cam-retry">Try again</button>
      <button class="btn ghost" type="button" data-il="cam-app">Use phone camera app</button>
      <button class="btn ghost" type="button" data-il="gallery" data-slot="${h.slot}">Choose from gallery</button>
      <button class="btn sub tiny" type="button" data-il="help-close">Close</button>
    </div></div>`;
  box.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function setHelp(help) {
  if (!sheet) return;
  sheet.help = help;
  renderHelp();
}

/* --- photos -------------------------------------------------------------- */
function photoErr(err) {
  const code = err && err.code;
  if (code === 'TOO_LARGE') return err.message || 'That photo is too large. Try another one.';
  if (code === 'UNSUPPORTED') return err.message || 'That file type is not supported. Use a JPEG, PNG or HEIC photo.';
  return errText(err);
}

async function addBlob(slot, blob) {
  const sh = sheet;
  if (!sh || !blob) return;
  sh.slotErr[slot] = '';
  sh.busy[slot] = true;
  sh.replacing[slot] = false;
  sh.submitErr = '';
  if (sh.help) setHelp(null);
  renderPhotos();
  renderFoot();
  try {
    await flushSave(sh, true);
    if (!sh.leadId) throw new Error('The draft could not be saved.');
    await queue.addPhoto(session(), sh.leadId, slot, blob);
  } catch (err) {
    sh.slotErr[slot] = photoErr(err);
  } finally {
    sh.busy[slot] = false;
    if (sheet === sh) {
      if (sh.leadId) await refreshSheet(sh);
      else { renderPhotos(); renderFoot(); }
    }
  }
}

async function removePhoto(slot) {
  const sh = sheet;
  if (!sh || !sh.leadId) return;
  sh.slotErr[slot] = '';
  sh.replacing[slot] = false;
  try { await queue.removePhoto(sh.leadId, slot); }
  catch (err) { toast(`Could not remove the photo: ${errText(err)}`, 'err'); }
  await refreshSheet(sh);
}

function pickFile(inputId, slot) {
  pickSlot = slot;
  const input = $(inputId);
  if (input) input.click();
}

function onFilePicked(e) {
  const input = e.target;
  const file = input.files && input.files[0];
  input.value = '';
  if (file) addBlob(pickSlot, file);
}

/* --- camera -------------------------------------------------------------- */
async function permissionState() {
  try {
    if (!navigator.permissions || !navigator.permissions.query) return '';
    const r = await navigator.permissions.query({ name: 'camera' });
    return (r && r.state) || '';
  } catch {
    return '';
  }
}

async function openCamera(slot) {
  pickSlot = slot;
  const md = navigator.mediaDevices;
  if (!md || typeof md.getUserMedia !== 'function') { pickFile('ilCapture', slot); return; }
  const req = ++camReq;
  let stream;
  try {
    stream = await md.getUserMedia(CAMERA);
    /* The user may have left, cancelled or tapped again while the camera was
     * starting; a stream nobody asked for any more must not stay live. */
    if (!sheet || document.hidden || req !== camReq) { stopStream(stream); return; }
  } catch (err) {
    if (req !== camReq) return;
    const name = err && err.name;
    if (name === 'NotAllowedError' || name === 'SecurityError' || name === 'PermissionDeniedError') {
      const st = await permissionState();
      setHelp({ slot, kind: st === 'denied' ? 'blocked' : st === 'prompt' ? 'prompt' : 'unknown' });
      return;
    }
    /* iOS may drop the programmatic click after the await; the panel keeps a tappable fallback. */
    setHelp({ slot, kind: 'unavailable' });
    pickFile('ilCapture', slot);
    return;
  }
  setHelp(null);
  showCamera(stream, slot);
}

function stopStream(stream) {
  try { stream.getTracks().forEach((t) => t.stop()); } catch { /* already stopped */ }
}

function showCamera(stream, slot) {
  closeCamera();
  camReq += 1;
  cam = { stream, slot };
  $('ilCamHost').innerHTML = `<div class="il-cam" role="dialog" aria-modal="true" aria-label="Camera">
    <video id="ilVideo" playsinline muted autoplay></video>
    <div class="il-cam-bar">
      <button class="btn sub" type="button" data-il="cam-cancel">Cancel</button>
      <button class="il-shutter" type="button" data-il="shutter" aria-label="Take photo"></button>
      <span></span>
    </div></div>`;
  const v = $('ilVideo');
  v.muted = true;
  v.setAttribute('playsinline', '');
  v.srcObject = stream;
  const p = v.play();
  if (p && p.catch) p.catch(() => { /* autoplay attribute retries on metadata */ });
  syncOpen();
}

function closeCamera() {
  camReq += 1;
  if (cam) stopStream(cam.stream);
  const v = $('ilVideo');
  if (v) v.srcObject = null;
  cam = null;
  const host = $('ilCamHost');
  if (host) host.innerHTML = '';
  syncOpen();
}

function shoot() {
  const c = cam;
  const v = $('ilVideo');
  if (!c || !v) return;
  const w = v.videoWidth;
  const h = v.videoHeight;
  if (!w || !h) { toast('The camera is still starting — try again'); return; }
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  canvas.getContext('2d').drawImage(v, 0, 0, w, h);
  const slot = c.slot;
  closeCamera();
  canvas.toBlob((blob) => {
    if (blob) addBlob(slot, blob);
    else toast('Could not take the photo — try again', 'err');
  }, 'image/jpeg', 0.92);
}

/* --- viewer -------------------------------------------------------------- */
async function openViewer(leadId, idx = 0) {
  let rec = null;
  try { rec = await queue.getLead(leadId); } catch { rec = null; }
  if (!rec || !rec.lead) { toast('That image lead is no longer on this phone', 'err'); return; }
  closeViewer();
  ensureHosts();
  const photos = (rec.photos || []).filter(Boolean).sort(bySlot);
  viewer = { lead: rec.lead, photos, idx: Math.max(0, Math.min(idx, photos.length - 1)), urls: new Map() };
  renderViewer();
  syncOpen();
}

function closeViewer() {
  if (viewer) for (const u of viewer.urls.values()) if (String(u).startsWith('blob:')) URL.revokeObjectURL(u);
  viewer = null;
  const host = $('ilViewHost');
  if (host) host.innerHTML = '';
  syncOpen();
}

function renderViewer() {
  const v = viewer;
  if (!v) return;
  const l = v.lead;
  const n = v.photos.length;
  $('ilViewHost').innerHTML = `<div class="il-view" role="dialog" aria-modal="true" aria-label="Image lead photos">
    <div class="il-vh"><div class="t">${esc(leadName(l))}</div>
      ${n ? `<span class="c">${v.idx + 1} / ${n}</span>` : ''}
      <button class="x" type="button" data-il="view-close" aria-label="Close">&times;</button></div>
    <div class="il-stage" id="ilStage"></div>
    <div class="il-vbar">
      ${n > 1 ? `<button class="btn sub" type="button" data-il="view-prev"${v.idx === 0 ? ' disabled' : ''}>Prev</button>
        <button class="btn sub" type="button" data-il="view-next"${v.idx >= n - 1 ? ' disabled' : ''}>Next</button>` : ''}
      ${n ? '<button class="btn" type="button" data-il="view-save">Save to Gallery</button>' : ''}
      <button class="btn ghost" type="button" data-il="view-close">Close</button>
    </div></div>`;
  loadStage();
}

function stageMsg(text) {
  const stage = $('ilStage');
  if (stage) stage.innerHTML = `<div class="il-vmsg">${esc(text)}</div>`;
}

function stageImg(url, slot) {
  const stage = $('ilStage');
  if (stage) stage.innerHTML = `<img src="${esc(url)}" alt="Photo ${esc(slot)}">`;
}

async function loadStage() {
  const v = viewer;
  if (!v) return;
  const p = v.photos[v.idx];
  if (!p) { stageMsg('This lead has no photos.'); return; }
  let url = v.urls.get(p.id);
  if (!url) {
    url = localUrl(p);
    if (url) v.urls.set(p.id, url);
  }
  if (url) { stageImg(url, p.slot); return; }
  if (!navigator.onLine) { stageMsg('Connect to the internet to view this photo.'); return; }
  stageMsg('Loading…');
  try { url = await queue.fetchRemotePhoto(p); } catch { url = null; }
  if (url && viewer === v) v.urls.set(p.id, url);
  else if (url) URL.revokeObjectURL(url);
  if (viewer !== v || v.photos[v.idx] !== p) return;
  if (url) stageImg(url, p.slot);
  else if (p.content_url) stageImg(p.content_url, p.slot);
  else stageMsg(navigator.onLine ? 'Could not load this photo. Try again when the signal is better.'
    : 'Connect to the internet to view this photo.');
}

function downloadFile(file, name) {
  const url = URL.createObjectURL(file);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

async function saveToGallery() {
  const v = viewer;
  const p = v && v.photos[v.idx];
  if (!p) return;
  const type = p.mime || 'image/jpeg';
  const name = `image-lead-${shortId(v.lead.id)}-${p.slot}.${EXT[type] || 'jpg'}`;
  let blob = p.bytes ? new Blob([p.bytes], { type }) : null;
  if (!blob) {
    const src = v.urls.get(p.id) || p.content_url;
    if (!src) { toast('Connect to the internet to save this photo', 'err'); return; }
    try {
      const r = await fetch(src);
      if (!r.ok) throw new Error(String(r.status));
      blob = await r.blob();
    } catch {
      toast('Could not get this photo — try again with signal', 'err');
      return;
    }
  }
  const file = new File([blob], name, { type: blob.type || type });
  try {
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      await navigator.share({ files: [file] });
      return;
    }
  } catch (err) {
    if (err && err.name === 'AbortError') return;
  }
  downloadFile(file, name);
}

/* --- list ---------------------------------------------------------------- */
function leadName(l) {
  if (!l) return 'Image lead';
  if (l.client_id) {
    const c = clients().find((x) => x && x.id === l.client_id);
    return l.client_name || (c && c.name) || 'Client';
  }
  return l.other_name || l.client_name || 'No client yet';
}

function statusOf(lead, photos) {
  try { return leadStatus(lead, photos) || { key: 'draft', label: 'Draft' }; }
  catch { return { key: lead.state || 'draft', label: lead.state || 'Draft' }; }
}

function cardHtml({ lead: l, photos }) {
  const st = statusOf(l, photos);
  const draft = st.key === 'draft';
  const note = String(l.note || '');
  const thumbs = photos.slice(0, 2).map((p) => {
    const url = listPool.get(p);
    if (url) return `<div class="il-thumb"><img src="${esc(url)}" alt="Photo ${esc(p.slot)}" loading="lazy"></div>`;
    const label = p.state === 'processing' ? 'Processing…'
      : p.content_url || p.state === 'uploaded' ? 'Tap to load' : 'No image';
    return `<div class="il-thumb">${esc(label)}</div>`;
  }).join('');
  return `<article class="il-card${draft ? ' draft' : ''}" data-lead="${esc(l.id)}" data-draft="${draft ? '1' : '0'}"
      role="button" tabindex="0">
    <div class="il-ch"><div style="min-width:0">
        <div class="il-cn">${esc(leadName(l))}${!l.client_id && l.other_name ? ' <span class="badge mute">Other</span>' : ''}</div>
        ${l.captured_at ? `<div class="il-cm">${esc(fmtTime(l.captured_at))}</div>` : ''}</div>
      <span class="badge ${BADGE[st.key] || 'mute'}">${esc(
        st.key === 'queued' && (l.attempts || 0) > 0 && navigator.onLine ? 'Retrying' : (st.label || st.key))}</span></div>
    ${note ? `<div class="il-cnote">${esc(note.length > 140 ? `${note.slice(0, 140)}…` : note)}</div>` : ''}
    ${thumbs ? `<div class="il-cthumbs">${thumbs}</div>` : ''}
    ${st.key === 'failed' && l.error ? `<div class="il-cerr">${esc(l.error)}</div>` : ''}
    ${st.key === 'queued' && (l.attempts || 0) >= 3 && l.error
    ? `<div class="il-cerr">Still trying (${esc(l.attempts)} attempts): ${esc(l.error)}</div>` : ''}
    ${draft ? `<div class="il-cact"><button class="btn tiny" type="button" data-il-act="resume" data-id="${esc(l.id)}">Complete draft</button></div>` : ''}
    ${st.key === 'failed' ? `<div class="il-cact">
      <button class="btn tiny" type="button" data-il-act="retry" data-id="${esc(l.id)}">Retry</button>
      ${!l.server_created ? `<button class="btn ghost tiny" type="button" data-il-act="edit" data-id="${esc(l.id)}">Edit</button>` : ''}
      <button class="btn sub tiny" type="button" data-il-act="discard" data-id="${esc(l.id)}" data-server="${l.server_created ? '1' : '0'}">Remove from phone</button>
    </div>` : ''}
  </article>`;
}

function countsLine(rows) {
  const n = { draft: 0, waiting: 0, failed: 0, done: 0 };
  for (const r of rows) {
    const k = r.st.key;
    if (k === 'queued' || k === 'uploading') n.waiting++;
    else if (n[k] !== undefined) n[k]++;
  }
  return [
    n.draft && plural(n.draft, 'draft', 'drafts'),
    n.waiting && `${n.waiting} waiting to upload`,
    n.failed && `${n.failed} failed`,
    n.done && `${n.done} uploaded`,
  ].filter(Boolean).join(' · ');
}

function scheduleList() {
  clearTimeout(listTimer);
  listTimer = setTimeout(renderList, 60);
}

async function renderList() {
  const box = $('imageLeadList');
  const s = session();
  if (!box || !s) return;
  const seq = ++listSeq;
  let rows = [];
  try { rows = (await queue.listLeads(s)) || []; } catch (err) { console.warn('image leads list failed:', err); }
  if (seq !== listSeq) return;
  rows = rows.filter((r) => r && r.lead && r.lead.id).map((r) => {
    const photos = (r.photos || []).filter(Boolean).sort(bySlot);
    return { lead: r.lead, photos, st: statusOf(r.lead, photos) };
  });
  rows.sort((a, b) => ((b.st.key === 'draft') - (a.st.key === 'draft'))
    || String(b.lead.captured_at || '').localeCompare(String(a.lead.captured_at || '')));

  const pill = $('pImageLeads');
  if (pill) {
    pill.textContent = rows.length;
    pill.className = `pill${rows.some((r) => r.st.key === 'failed') ? ' urgent' : ''}`;
  }
  const counts = $('imageLeadCounts');
  if (counts) counts.textContent = countsLine(rows);

  listPool.begin();
  box.innerHTML = rows.length ? rows.map(cardHtml).join('')
    : `<div class="empty" style="grid-column:1/-1"><b>No image leads yet</b>
      Tap the camera button to photograph a business card or badge.</div>`;
  listPool.end();
}

async function onListClick(e) {
  const act = e.target.closest('[data-il-act]');
  if (act) {
    e.stopPropagation();
    const id = act.dataset.id;
    if (act.dataset.ilAct === 'resume') { openSheet(id); return; }
    if (act.dataset.ilAct === 'edit') {
      try {
        if (await queue.reopenFailed(id)) openSheet(id);
      } catch (err) { toast(`Could not reopen: ${errText(err)}`, 'err'); }
      scheduleList();
      return;
    }
    if (act.dataset.ilAct === 'discard') {
      const onServer = act.dataset.server === '1';
      const ok = await confirmDialog({
        title: 'Remove this image lead from the phone?',
        body: onServer
          ? 'Part of it already reached the office, and it stays there. Only the copy on this phone is removed.'
          : 'It never reached the office. The client, note and photos on this phone will be removed.',
        yes: 'Remove', no: 'Keep' });
      if (!ok) return;
      try { await queue.discardFailed(id); toast('Removed from this phone'); }
      catch (err) { toast(`Could not remove: ${errText(err)}`, 'err'); }
      scheduleList();
      return;
    }
    if (act.dataset.ilAct === 'retry') {
      act.disabled = true;
      try {
        await queue.retry(id);
        toast(navigator.onLine ? 'Uploading…' : 'Will upload when there is signal');
      } catch (err) {
        toast(`Could not retry: ${errText(err)}`, 'err');
      }
      scheduleList();
    }
    return;
  }
  const card = e.target.closest('[data-lead]');
  if (!card) return;
  if (card.dataset.draft === '1') openSheet(card.dataset.lead);
  else openViewer(card.dataset.lead);
}

/* --- delegated sheet / camera / viewer actions --------------------------- */
function onHostClick(e) {
  const cid = e.target.closest('[data-cid]');
  const sh = sheet;
  if (cid && sh) {
    const c = clients().find((x) => x && String(x.id) === cid.dataset.cid);
    if (!c) return;
    Object.assign(sh, { client_id: c.id, client_name: c.name, other_name: '', mode: 'picked', query: '' });
    renderClient();
    changed(sh);
    return;
  }
  const b = e.target.closest('[data-il]');
  if (!b) return;
  const slot = Number(b.dataset.slot) || (sh && sh.help && sh.help.slot) || 1;
  switch (b.dataset.il) {
    case 'close': closeSheet(); break;
    case 'save': closeSheet(); break;
    case 'delete': deleteDraft(); break;
    case 'upload': upload(); break;
    case 'change':
      Object.assign(sh, { client_id: null, client_name: '', mode: 'search' });
      renderClient();
      changed(sh);
      $('ilQ').focus();
      break;
    case 'other':
      Object.assign(sh, { client_id: null, client_name: '', other_name: sh.otherStash, mode: 'other' });
      renderClient();
      changed(sh);
      $('ilOther').focus();
      break;
    case 'list':
      Object.assign(sh, { otherStash: sh.other_name, other_name: '', mode: 'search' });
      renderClient();
      changed(sh);
      $('ilQ').focus();
      break;
    case 'camera': openCamera(slot); break;
    case 'gallery': pickFile('ilGallery', slot); break;
    case 'replace': sh.replacing[slot] = true; renderPhotos(); break;
    case 'replace-cancel': sh.replacing[slot] = false; renderPhotos(); break;
    case 'remove': removePhoto(slot); break;
    case 'cam-retry': openCamera(slot); break;
    case 'cam-app': pickFile('ilCapture', slot); break;
    case 'help-close': setHelp(null); break;
    case 'cam-cancel': closeCamera(); break;
    case 'shutter': shoot(); break;
    case 'view-close': closeViewer(); break;
    case 'view-prev':
    case 'view-next':
      if (viewer) {
        viewer.idx = Math.max(0, Math.min(viewer.photos.length - 1,
          viewer.idx + (b.dataset.il === 'view-next' ? 1 : -1)));
        renderViewer();
      }
      break;
    case 'view-save': saveToGallery(); break;
    default: break;
  }
}

function onHostInput(e) {
  const sh = sheet;
  if (!sh) return;
  const t = e.target;
  if (t.id === 'ilQ') { sh.query = t.value; renderMatches(); }
  else if (t.id === 'ilOther') { sh.other_name = t.value; changed(sh); }
  else if (t.id === 'ilNote') { sh.note = t.value; changed(sh); }
}

function onKey(e) {
  if (e.key !== 'Escape') return;
  if (dialogOpen) dialogOpen(false);
  else if (cam) closeCamera();
  else if (viewer) closeViewer();
  else if (sheet) closeSheet();
  else return;
  e.preventDefault();
}

function onQueueChanged(e) {
  const id = e && e.detail && e.detail.lead_id;
  scheduleList();
  if (sheet && sheet.leadId && (!id || id === sheet.leadId)) refreshSheet(sheet);
}

/* --- public -------------------------------------------------------------- */
export function init(c) {
  ctx = c || ctx;
  if (!inited) {
    inited = true;
    const host = ensureHosts();
    host.addEventListener('click', onHostClick);
    host.addEventListener('input', onHostInput);
    document.addEventListener('keydown', onKey);
    const fab = $('imgFab');
    if (fab) { fab.classList.remove('hide'); fab.onclick = () => openSheet(null); }
    const list = $('imageLeadList');
    if (list) {
      list.addEventListener('click', onListClick);
      list.addEventListener('keydown', (e) => {
        if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('[data-lead]')) {
          e.preventDefault();
          onListClick(e);
        }
      });
    }
    ['ilGallery', 'ilCapture'].forEach((id) => { const el = $(id); if (el) el.onchange = onFilePicked; });
    if (queue.bus && queue.bus.addEventListener) {
      queue.bus.addEventListener('changed', onQueueChanged);
      queue.bus.addEventListener('auth', () => toast('Sign in again to upload image leads', 'err'));
    }
    /* e.g. a HEIC original on Android Chrome: the file is fine and uploads, the
     * browser just cannot draw it. Error events don't bubble, hence capture. */
    document.addEventListener('error', (e) => {
      const img = e.target;
      if (!(img instanceof HTMLImageElement) || !img.closest('#imageLeadList, #imgHost')) return;
      const note = document.createElement('span');
      note.className = 'il-noprev';
      note.textContent = 'Preview not available';
      img.replaceWith(note);
    }, true);
    window.addEventListener('online', () => { if (viewer) loadStage(); });
    window.addEventListener('pagehide', closeCamera);
    document.addEventListener('visibilitychange', () => { if (document.hidden) closeCamera(); });
  }
  const s = session();
  if (s && !started) {
    started = true;
    Promise.resolve()
      .then(() => queue.loadConfig())
      .catch((err) => console.warn('image leads config not loaded:', err))
      .then(() => queue.start(s))
      .catch((err) => console.warn('image leads queue did not start:', err))
      .then(scheduleList);
  } else {
    scheduleList();
  }
}

export async function beforeSignOut(s) {
  const sess = s || session();
  if (!sess) return true;
  let c = { drafts: 0, unsent: 0 };
  try { c = (await queue.counts(sess)) || c; } catch (err) { console.warn('image leads counts failed:', err); }
  const unsent = Number(c.unsent) || 0;
  const drafts = Number(c.drafts) || 0;
  if (unsent > 0 || drafts > 0) {
    const head = unsent > 0
      ? `${plural(unsent, 'image lead is', 'image leads are')} not uploaded yet${
        drafts ? ` (and ${plural(drafts, 'draft', 'drafts')})` : ''}.`
      : `${plural(drafts, 'image lead draft is', 'image lead drafts are')} not uploaded yet.`;
    const ok = await confirmDialog({
      title: 'Image leads not uploaded',
      body: `${head} They stay on this phone and upload next time you sign in here. Sign out anyway?`,
      yes: 'Sign out anyway',
      no: 'Stay signed in',
    });
    if (!ok) return false;
  }
  try { await queue.clearUploadedBytes(sess); } catch (err) { console.warn('image leads cleanup failed:', err); }
  return true;
}
